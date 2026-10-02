"""Conversões e normalizações compartilhadas pelo histórico ROTALOG."""

from __future__ import annotations

import datetime
import re
import typing
from zoneinfo import ZoneInfo


def parse_float_br(val: typing.Any) -> float:
    if val is None:
        return 0.0
    value = str(val).strip()
    if not value or value == "-":
        return 0.0
    match = re.search(r"([\d.,]+)", value)
    if not match:
        return 0.0
    try:
        return round(float(match.group(1).replace(".", "").replace(",", ".")), 2)
    except ValueError:
        return 0.0


def parse_iso_datetime(
    dt_str: typing.Any,
    base_day_iso: str | None = None,
    *,
    timezone: ZoneInfo,
) -> str | None:
    if not dt_str or not str(dt_str).strip() or str(dt_str).strip() == "-":
        return None
    value = str(dt_str).strip()
    for fmt in ("%d/%m/%y %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.datetime.strptime(value, fmt).replace(tzinfo=timezone).isoformat()
        except ValueError:
            pass
    if base_day_iso and re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", value):
        try:
            hour = [int(part) for part in value.split(":")]
            day = [int(part) for part in base_day_iso.split("-")]
            return datetime.datetime(
                day[0], day[1], day[2], hour[0], hour[1],
                hour[2] if len(hour) > 2 else 0, tzinfo=timezone,
            ).isoformat()
        except (ValueError, IndexError):
            pass
    return None


def extrair_data_iso_de_evento(
    row: dict[str, typing.Any],
    default_day: str | None = None,
    *,
    timezone: ZoneInfo,
) -> str:
    for column in ("Inicio Deslo", "Inicio Exec", "Fim Exec", "Retorno"):
        match = re.search(r"(\d{2})/(\d{2})/(\d{2,4})", str(row.get(column) or "").strip())
        if match:
            day, month, year = match.groups()
            normalized_year = int(year) if len(year) == 4 else 2000 + int(year)
            return f"{normalized_year:04d}-{month}-{day}"
    return default_day or datetime.datetime.now(timezone).date().isoformat()


def parse_target_date(data_str: str | None = None, *, timezone: ZoneInfo) -> datetime.date:
    if not data_str or data_str.strip().lower() in ("ontem", "d-1", "yesterday", "true", ""):
        return datetime.datetime.now(timezone).date() - datetime.timedelta(days=1)
    value = data_str.strip()
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Formato de data inválido: '{data_str}'. Use AAAA-MM-DD ou DD/MM/AAAA.")
