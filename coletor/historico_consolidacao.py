"""Regras puras de consolidação dos dados históricos do ROTALOG."""

from __future__ import annotations

import datetime
import json
import os
import re
import typing
from pathlib import Path
from zoneinfo import ZoneInfo

from .equipes import normalize_team_key
from .historico_parsing import (
    extrair_data_iso_de_evento as _extract_event_date,
    parse_float_br,
    parse_iso_datetime as _parse_iso_datetime,
)
from .parser import formatar_protocolo_copel
from .storage import company_key
from .team_registry import TeamRegistry

TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))
CONTRATOS_ALVO: tuple[str, ...] = ("4600026988", "4600025149")


def parse_iso_datetime(value: typing.Any, base_day_iso: str | None = None) -> str | None:
    return _parse_iso_datetime(value, base_day_iso, timezone=TZ)


def extrair_data_iso_de_evento(row: dict[str, typing.Any], default_day: str | None = None) -> str:
    return _extract_event_date(row, default_day, timezone=TZ)

_CAMPOS_KM_EQUIPE = (
    "kmInformado", "kmGlosadoCritico", "kmGlosadoParecer", "kmAutorizadoInicial",
    "kmRecuperadoParecer", "kmAutorizadoFinal", "kmAguardandoJustificativa", "kmEmAnalise",
)


def _quantidade_e_km(value: typing.Any) -> tuple[int, float]:
    raw = str(value or "").strip()
    quantidade_match = re.search(r"\d+", raw)
    km_match = re.search(r"\(([\d.,]+)\s*Km\)", raw, re.IGNORECASE)
    return (
        int(quantidade_match.group(0)) if quantidade_match else 0,
        parse_float_br(km_match.group(1)) if km_match else 0.0,
    )


def _electrician_identity(row: dict[str, typing.Any], number: int) -> tuple[str, str, str]:
    display = str(row.get(f"Eletricista {number}") or "").strip()
    registration = str(row.get(f"Eletricista {number} Registro") or "").strip()
    name = str(row.get(f"Eletricista {number} Nome") or "").strip()
    if (not registration or not name) and display:
        match = re.match(r"\s*(\d+)\s*-\s*(.+?)\s*$", display)
        if match:
            registration = registration or match.group(1)
            name = name or match.group(2)
        elif not name:
            # A listagem principal normalmente traz apenas o nome abreviado.
            # Ele ainda é útil para o histórico sem exigir uma consulta HTTP
            # adicional ao detalhe de cada equipe.
            name = display
    return registration, name, display or " - ".join(value for value in (registration, name) if value)


def _normalized_event_type(row: dict[str, typing.Any]) -> str:
    for key in ("Evento", "Tipo Evento", "Tipo de Evento", "Descricao", "Descrição", "Status"):
        value = str(row.get(key) or "").strip()
        if value:
            return re.sub(r"\s+", " ", value).casefold()
    return ""


def deduplicar_eventos_consolidados(
    records: list[dict[str, typing.Any]],
) -> tuple[list[dict[str, typing.Any]], dict[str, int]]:
    """Remove duplicatas do consolidado sem alterar o arquivo bruto de auditoria."""
    exact_seen: set[str] = set()
    last_technical_state: dict[tuple[str, str], str] = {}
    kept: list[dict[str, typing.Any]] = []
    exact_removed = 0
    burst_removed = 0
    technical_types = {"fim de turno", "inicio de turno", "início de turno"}

    for row in records:
        exact_key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if exact_key in exact_seen:
            exact_removed += 1
            continue
        exact_seen.add(exact_key)

        event_type = _normalized_event_type(row)
        if event_type in technical_types:
            team = normalize_team_key(row.get("Veiculo") or row.get("Veículo"))
            contract = str(row.get("Contrato") or "").strip()
            logical_key = (team, contract)
            normalized_state = event_type.replace("í", "i")
            if last_technical_state.get(logical_key) == normalized_state:
                burst_removed += 1
                continue
            last_technical_state[logical_key] = normalized_state
        kept.append(row)

    return kept, {
        "recebidos": len(records),
        "mantidos": len(kept),
        "duplicatasExatasRemovidas": exact_removed,
        "repeticoesTecnicasRemovidas": burst_removed,
        "totalRemovidos": exact_removed + burst_removed,
    }


def estruturar_resumo_equipes(
    records: list[dict[str, typing.Any]],
    contratos_alvo: tuple[str, ...] = CONTRATOS_ALVO,
) -> dict[str, typing.Any]:
    """Consolida a medição oficial diária/mensal exibida pela tela de equipes."""
    contratos = set(contratos_alvo) if contratos_alvo else None
    por_contrato: dict[str, dict[str, typing.Any]] = {}
    registros: list[dict[str, typing.Any]] = []
    for row in records:
        contrato = str(row.get("Contrato") or "").strip()
        if contratos and contrato not in contratos:
            continue
        equipe = normalize_team_key(row.get("Veiculo") or row.get("Veículo"))
        if not equipe:
            continue
        aguardando_qtd, aguardando_km = _quantidade_e_km(row.get("Aguardando justificativa"))
        analise_qtd, analise_km = _quantidade_e_km(row.get("Em analise") or row.get("Em análise"))
        eletricista1_registro, eletricista1_nome, eletricista1 = _electrician_identity(row, 1)
        eletricista2_registro, eletricista2_nome, eletricista2 = _electrician_identity(row, 2)
        registro = {
            "equipe": equipe,
            "tablet": str(row.get("Tablet") or "").replace("*", "").strip(),
            "agencia": str(row.get("Agencia") or row.get("Agência") or "").strip(),
            "contrato": contrato,
            "turnoReferencia": str(row.get("Data Referencia - Turno") or row.get("Data Referência - Turno") or "").strip(),
            "eletricista1": eletricista1,
            "eletricista1Registro": eletricista1_registro,
            "eletricista1Nome": eletricista1_nome,
            "eletricista2": eletricista2,
            "eletricista2Registro": eletricista2_registro,
            "eletricista2Nome": eletricista2_nome,
            "servicosExecutados": int(parse_float_br(row.get("Servicos executados") or row.get("Serviços executados"))),
            "kmInformado": parse_float_br(row.get("Informado com limitador (km)")),
            "kmGlosadoCritico": parse_float_br(row.get("Glosado critico (km)") or row.get("Glosado crítico (km)")),
            "kmGlosadoParecer": parse_float_br(row.get("Glosado por parecer (km)")),
            "kmAutorizadoInicial": parse_float_br(row.get("Autorizado (km)")),
            "kmRecuperadoParecer": parse_float_br(row.get("Recuperados por Parecer (km)")),
            "kmAutorizadoFinal": parse_float_br(row.get("Autorizado Final (km)")),
            "aguardandoJustificativa": aguardando_qtd,
            "kmAguardandoJustificativa": aguardando_km,
            "emAnalise": analise_qtd,
            "kmEmAnalise": analise_km,
            "alertas": int(parse_float_br(row.get("Alertas"))),
        }
        registros.append(registro)
        grupo = por_contrato.setdefault(contrato, {
            **{campo: 0.0 for campo in _CAMPOS_KM_EQUIPE},
            "servicosExecutados": 0, "aguardandoJustificativa": 0, "emAnalise": 0,
            "alertas": 0, "equipes": {},
        })
        total_equipe = grupo["equipes"].setdefault(equipe, {
            **{campo: 0.0 for campo in _CAMPOS_KM_EQUIPE},
            "servicosExecutados": 0, "aguardandoJustificativa": 0, "emAnalise": 0,
            "alertas": 0, "dias": [], "eletricistas": [], "agencias": [], "tablets": [],
        })
        for target in (grupo, total_equipe):
            for campo in _CAMPOS_KM_EQUIPE:
                target[campo] = round(target[campo] + registro[campo], 2)
            for campo in ("servicosExecutados", "aguardandoJustificativa", "emAnalise", "alertas"):
                target[campo] += registro[campo]
        dia_match = re.search(r"\d{2}/\d{2}/\d{4}", registro["turnoReferencia"])
        if dia_match and dia_match.group(0) not in total_equipe["dias"]:
            total_equipe["dias"].append(dia_match.group(0))
        for field, value in (
            ("eletricistas", registro["eletricista1"]), ("eletricistas", registro["eletricista2"]),
            ("agencias", registro["agencia"]), ("tablets", registro["tablet"]),
        ):
            if value and value not in total_equipe[field]:
                total_equipe[field].append(value)

    totais = {campo: round(sum(g[campo] for g in por_contrato.values()), 2) for campo in _CAMPOS_KM_EQUIPE}
    for campo in ("servicosExecutados", "aguardandoJustificativa", "emAnalise", "alertas"):
        totais[campo] = sum(g[campo] for g in por_contrato.values())
    return {
        "fonte": "ROTALOG_EQUIPES",
        "granularidade": "EQUIPE_DIA",
        "totalRegistros": len(registros),
        "totais": totais,
        "totaisPorContrato": por_contrato,
        "registros": registros,
    }


def enriquecer_cadastro_permanente_equipes(
    output_dir: Path,
    records: list[dict[str, typing.Any]],
    day_iso: str,
) -> dict[str, int]:
    """Incorpora todas as equipes da página, independentemente do contrato."""
    registry = TeamRegistry(output_dir / "rotalog" / "equipes" / "team-registry.json")
    enriched_teams: set[str] = set()
    registrations: set[str] = set()
    with registry.exclusive_update():
        for row in records:
            vehicle = normalize_team_key(row.get("Veiculo") or row.get("Veículo"))
            if not vehicle:
                continue
            shift_reference = str(
                row.get("Data Referencia - Turno") or row.get("Data Referência - Turno") or ""
            )
            shift_match = re.search(r"(\d{2}/\d{2}/\d{4}).*?(\d{2}:\d{2})", shift_reference)
            observed_at = f"{day_iso}T12:00:00-03:00"
            if shift_match:
                try:
                    shift_dt = datetime.datetime.strptime(
                        f"{shift_match.group(1)} {shift_match.group(2)}", "%d/%m/%Y %H:%M"
                    ).replace(tzinfo=TZ)
                    observed_at = shift_dt.isoformat()
                except ValueError:
                    pass

            professionals = []
            for number in (1, 2):
                registration, full_name, _ = _electrician_identity(row, number)
                if registration or full_name:
                    professionals.append({"registration": registration, "fullName": full_name})
                    if registration:
                        registrations.add(registration)
            team_id = registry.enrich_professionals(
                vehicle, professionals, observed_at, shift_reference,
            )
            if team_id:
                enriched_teams.add(team_id)
    return {
        "sourceRows": len(records),
        "teamsEnriched": len(enriched_teams),
        "professionalsIdentified": len(registrations),
    }


def estruturar_quilometragem_diaria(
    eventos_records: list[dict[str, typing.Any]],
    target_day: str,
    empresa: str,
    contratos_alvo: tuple[str, ...] = CONTRATOS_ALVO,
    equipes_records: list[dict[str, typing.Any]] | None = None,
) -> dict[str, typing.Any]:
    """Cria a estrutura enxuta única do dia com totais por equipe e protocolos filtrados pelos contratos alvo."""
    emp_key = company_key(empresa)
    eventos_records, dedup_stats = deduplicar_eventos_consolidados(eventos_records)
    protocolos: dict[str, dict[str, typing.Any]] = {}
    servicos: list[dict[str, typing.Any]] = []
    totais_equipe: dict[str, dict[str, typing.Any]] = {}
    totais_contrato: dict[str, dict[str, typing.Any]] = {}
    alvo_set = set(contratos_alvo) if contratos_alvo else None

    for r in eventos_records:
        contrato = str(r.get("Contrato") or "").strip()
        if alvo_set and contrato not in alvo_set:
            continue

        team_raw = str(r.get("Veículo") or r.get("Veiculo") or "").strip()
        team_key = normalize_team_key(team_raw)
        if not team_key:
            continue

        km_inf = parse_float_br(r.get("Informado com limitador (km)"))
        km_aut = parse_float_br(r.get("Autorizado final(km)"))

        grupo = totais_contrato.setdefault(contrato, {
            "kmInformado": 0.0, "kmAutorizadoFinal": 0.0, "kmRecuperado": None, "equipes": {},
        })
        equipe_contrato = grupo["equipes"].setdefault(team_key, {
            "kmInformado": 0.0, "kmAutorizadoFinal": 0.0, "kmRecuperado": None,
        })
        for total in (grupo, equipe_contrato):
            total["kmInformado"] = round(total["kmInformado"] + km_inf, 2)
            total["kmAutorizadoFinal"] = round(total["kmAutorizadoFinal"] + km_aut, 2)

        if team_key not in totais_equipe:
            totais_equipe[team_key] = {
                "kmInformado": 0.0,
                "kmAutorizadoFinal": 0.0,
                "contrato": contrato,
            }

        eq = totais_equipe[team_key]
        eq["kmInformado"] = round(eq["kmInformado"] + km_inf, 2)
        eq["kmAutorizadoFinal"] = round(eq["kmAutorizadoFinal"] + km_aut, 2)

        prot_raw = str(r.get("Protocolo") or "").strip()
        prot_clean = formatar_protocolo_copel(prot_raw) if prot_raw else ""
        servico_info = {
            "protocolo": prot_clean or prot_raw,
            "equipe": team_key,
            "contrato": contrato,
            "inicioDeslocamento": str(r.get("Inicio Deslo") or r.get("Início Deslo") or "").strip(),
            "inicioExecucao": str(r.get("Inicio Exec") or r.get("Início Exec") or "").strip(),
            "fimExecucao": str(r.get("Fim Exec") or "").strip(),
            "retorno": str(r.get("Retorno") or "").strip(),
            "kmInformado": km_inf,
            "kmAutorizadoFinal": km_aut,
            "diferencaKm": round(km_inf - km_aut, 2),
        }
        servicos.append(servico_info)
        if prot_clean:
            protocolos[prot_clean] = servico_info

    total_km_inf = sum(eq["kmInformado"] for eq in totais_equipe.values())
    total_km_aut = sum(eq["kmAutorizadoFinal"] for eq in totais_equipe.values())

    resumo_equipes = estruturar_resumo_equipes(equipes_records or [], contratos_alvo)
    return {
        "schemaVersion": 3,
        "data": target_day,
        "empresa": empresa,
        "empresaKey": emp_key,
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "contratos": list(contratos_alvo) if contratos_alvo else [],
        "totalEquipes": len(totais_equipe),
        "totalServicos": len(servicos) or len(protocolos),
        "totalKmInformado": round(total_km_inf, 2),
        "totalKmAutorizadoFinal": round(total_km_aut, 2),
        "totaisPorEquipe": totais_equipe,
        "totaisPorContrato": totais_contrato,
        "protocolos": protocolos,
        "servicos": servicos,
        "detalhamentoServicos": {
            "fonte": "ROTALOG_LISTAGEM_EVENTOS",
            "granularidade": "SERVICO",
            "totalKmInformado": round(total_km_inf, 2),
            "totalKmAutorizadoFinal": round(total_km_aut, 2),
        },
        "deduplicacaoEventos": dedup_stats,
        "resumoEquipes": resumo_equipes,
    }
