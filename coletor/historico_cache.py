"""Leitura validada dos recibos diarios da coleta unica."""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
import unicodedata
from typing import Any
from zoneinfo import ZoneInfo

LOG = logging.getLogger("coletor-historico")
COLETA_UNICA_SCHEMA_VERSION = 2
MAX_CACHE_AGE = datetime.timedelta(hours=24)
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
DETAIL_FIELDS = (
    "Diário URL",
    "Agência Nome completo",
    "Eletricista 1 Nome completo",
    "Eletricista 1 Matrícula",
    "Eletricista 2 Nome completo",
    "Eletricista 2 Matrícula",
)

EVENT_FIELD_ALIASES = (
    ("Veículo", "Veiculo"),
    ("Contrato",),
    ("Informado com limitador (km)", "Km Informado", "Km Inform"),
    ("Autorizado final(km)", "Autorizado Final (km)", "Km Autorizado", "Km Auto"),
)


def default_cache_dir() -> Path:
    configured_output = os.getenv("COLETA_UNICA_SAIDA", "").strip()
    if configured_output:
        return Path(configured_output).expanduser()
    mount = Path(os.getenv("COLETA_UNICA_SSD_MOUNT", "/mnt/ssd")).expanduser()
    return mount / "dds-coletor-rtl" / "coleta-unica" / "dados"


def _column_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if char.isalnum())


def _is_complete(document: Any, day: datetime.date, folder: Path) -> tuple[bool, str]:
    if not isinstance(document, dict):
        return False, "documento raiz invalido"
    if document.get("completo") is not True:
        return False, "recibo nao marca o dia como completo"
    if document.get("schemaVersion") != COLETA_UNICA_SCHEMA_VERSION:
        return False, "versao de esquema incompatível"
    if document.get("data") != day.isoformat():
        return False, "data do recibo divergente"
    if document.get("filtroVeiculo"):
        return False, "recibo filtrado por veiculo"
    if not isinstance(document.get("resumo"), list) or len(document["resumo"]) != 15:
        return False, "resumo diario incompleto"
    if not (folder / "resumo.csv").is_file():
        return False, "CSV ausente em resumo"

    pagination = document.get("paginacao")
    if not isinstance(pagination, dict):
        return False, "paginacao ausente"
    for name in ("turnos", "eventos"):
        records = document.get(name)
        metadata = pagination.get(name)
        if not isinstance(records, list) or not isinstance(metadata, dict):
            return False, f"tabela {name} ausente"
        total = metadata.get("total")
        columns = metadata.get("colunas")
        if not isinstance(total, int) or isinstance(total, bool) or total != len(records):
            return False, f"contagem divergente em {name}"
        if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
            return False, f"colunas ausentes em {name}"
        if not (folder / f"{name}.csv").is_file():
            return False, f"CSV ausente em {name}"
        if any(not isinstance(record, dict) for record in records):
            return False, f"registro invalido em {name}"

    turnos = document["turnos"]
    for record in turnos:
        if not all(isinstance(record.get(field), str) for field in DETAIL_FIELDS):
            return False, "detalhes de equipe ausentes"
        if not record["Agência Nome completo"]:
            return False, "nome completo da agencia ausente"
        for number in (1, 2):
            name = record[f"Eletricista {number} Nome completo"]
            registration = record[f"Eletricista {number} Matrícula"]
            if bool(name) != bool(registration) or (registration and not registration.isdigit()):
                return False, f"identificacao do eletricista {number} incompleta"

    event_columns = {_column_key(column) for column in pagination["eventos"]["colunas"]}
    if any(not any(_column_key(alias) in event_columns for alias in aliases)
           for aliases in EVENT_FIELD_ALIASES):
        return False, "colunas de eventos incompatíveis com a coleta normal"
    return True, ""


def load_unique_day(
    day: datetime.date,
    cache_dir: Path | None = None,
    now: datetime.datetime | None = None,
) -> dict[str, Any] | None:
    """Retorna o recibo completo do dia ou ``None`` para permitir raspagem normal."""
    folder = Path(cache_dir or default_cache_dir()) / day.isoformat()
    receipt = folder / "dados.json"
    if not receipt.is_file():
        return None
    try:
        document = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        LOG.warning("Cache da coleta unica ilegivel para %s (%s); usando o portal", day, exc)
        return None
    complete, reason = _is_complete(document, day, folder)
    if not complete:
        LOG.warning("Cache da coleta unica invalido para %s (%s); usando o portal", day, reason)
        return None
    collected_at = document.get("coletadoEm")
    if not isinstance(collected_at, str):
        LOG.warning("Cache da coleta unica sem horario para %s; usando o portal", day)
        return None
    try:
        collected = datetime.datetime.fromisoformat(collected_at)
    except ValueError:
        LOG.warning("Horario invalido no cache da coleta unica para %s; usando o portal", day)
        return None
    if collected.tzinfo is None:
        LOG.warning("Horario sem fuso no cache da coleta unica para %s; usando o portal", day)
        return None
    current_time = now or datetime.datetime.now(LOCAL_TZ)
    if current_time.tzinfo is None:
        raise ValueError("now precisa incluir fuso horario.")
    age = current_time.astimezone(LOCAL_TZ) - collected.astimezone(LOCAL_TZ)
    if age < datetime.timedelta(minutes=-5) or age > MAX_CACHE_AGE:
        LOG.info("Cache da coleta unica expirado para %s; usando o portal", day)
        return None
    return document


def _normalized_value(record: dict[str, Any], *fields: str) -> str:
    value = next((str(record.get(field) or "").strip() for field in fields
                  if str(record.get(field) or "").strip()), "")
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if char.isalnum())


def team_cache_key(record: dict[str, Any]) -> tuple[str, str, str, str] | None:
    vehicle = _normalized_value(record, "Veículo", "Veiculo")
    contract = _normalized_value(record, "Contrato")
    agency = _normalized_value(record, "Agência", "Agencia")
    shift = _normalized_value(record, "Data Referência - Turno", "Data Referencia - Turno")
    if not all((vehicle, contract, agency, shift)):
        return None
    return vehicle, contract, agency, shift


def cached_team_details(turnos: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], list[dict[str, str]]]:
    """Indexa detalhes por equipe, contrato, agencia e referencia do turno."""
    indexed: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
    for record in turnos:
        key = team_cache_key(record)
        if key is None:
            continue
        details: dict[str, str] = {
            "Agência Nome completo": record["Agência Nome completo"],
        }
        for number in (1, 2):
            details[f"Eletricista {number} Registro"] = record[f"Eletricista {number} Matrícula"]
            details[f"Eletricista {number} Nome"] = record[f"Eletricista {number} Nome completo"]
        indexed.setdefault(key, []).append(details)
    return indexed
