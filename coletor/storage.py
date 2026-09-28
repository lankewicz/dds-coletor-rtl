"""Fachada e serializadores para armazenamento local e sincronização com o Firebase Storage.

Este módulo atua como fachada unificada (reexportando I/O, diffs e merge), mantendo 100% de
retrocompatibilidade com todos os pontos de importação do projeto.
"""

from __future__ import annotations

import datetime
import typing
from zoneinfo import ZoneInfo

# Submódulo: I/O em disco e nuvem (Firebase / GCS)
from .storage_io import (
    LOCAL_TZ,
    RotalogExecutionLog,
    RotalogGcsSnapshotStore,
    RotalogTeamFileRepository,
    _decode_json_object,
    _json_cache_default,
    _safe_team_key,
    company_key,
    load_json,
    load_json_with_status,
    rotalog_gcs_paths,
    write_json,
)

# Submódulo: Motor de diff e observabilidade operacional
from .transitions import (
    TRACKED_FIELDS,
    _operational_value,
    _summarize_history_correction,
    changed_fields,
    summarize_team_transition,
)

# Submódulo: Lógica de consolidação e mesclagem diária
from .merger import (
    _compact_interval,
    _extrair_data_base,
    _formatar_horarios_servico,
    _iso_local,
    _merge_service_records,
    _service_id,
    compact_service,
    merge_daily_document,
)

__all__ = [
    "LOCAL_TZ",
    "TRACKED_FIELDS",
    "RotalogExecutionLog",
    "RotalogGcsSnapshotStore",
    "RotalogTeamFileRepository",
    "build_rotalog_document",
    "changed_fields",
    "company_key",
    "compact_service",
    "compactar_equipe_para_index",
    "load_json",
    "load_json_with_status",
    "merge_daily_document",
    "queue_counts",
    "rotalog_gcs_paths",
    "summarize_team_transition",
    "write_json",
]


def queue_counts(team: dict[str, typing.Any]) -> dict[str, int]:
    """Calcula quantidade de serviços na fila separados por emergência e comercial."""
    pending = team.get("ss_pendentes") or []
    return {
        "emergencia": sum(1 for item in pending if str(item.get("tipo") or "").upper() == "EMERGENCIA"),
        "comercial": sum(1 for item in pending if str(item.get("tipo") or "").upper() == "COMERCIAL"),
    }


def _extrair_timestamp_rotalog(eq: dict[str, typing.Any]) -> int:
    """Obtém o timestamp mais recente em ms de qualquer evento operacional da equipe."""
    timestamps = []
    for srv in (eq.get("ss_em_andamento") or []) + (eq.get("ss_executadas") or []):
        for trans in srv.get("transitions") or []:
            ts = trans.get("timestampMs")
            if isinstance(ts, (int, float)) and ts > 0:
                timestamps.append(int(ts))
        if srv.get("inicioIso"):
            try:
                dt = datetime.datetime.fromisoformat(srv["inicioIso"])
                timestamps.append(int(dt.timestamp() * 1000))
            except Exception:
                pass
    for marker in eq.get("turno_marcadores_t") or []:
        if marker.get("start"):
            timestamps.append(int(marker["start"]))
    for interval in eq.get("intervalos") or []:
        if interval.get("inicio_ms"):
            timestamps.append(int(interval["inicio_ms"]))

    return max(timestamps) if timestamps else 0


def build_rotalog_document(
    eq: dict[str, typing.Any],
    empresa: str,
    team_key: str,
    timestamp_iso: str,
    fila_counts: dict[str, int],
) -> dict[str, typing.Any]:
    """Monta a estrutura canônica de documento ROTALOG para persistência e diff."""
    team_key = _safe_team_key(team_key or eq.get("equipe_codigo"))
    return {
        "empresa": empresa,
        "equipe": team_key,
        "teamKey": team_key,
        "teamId": eq.get("team_id") or eq.get("teamId"),
        "membersKey": eq.get("members_key") or eq.get("membersKey"),
        "identitySource": eq.get("identity_source") or eq.get("identitySource"),
        "groupRaw": eq.get("group_raw") or "",
        "veiculo": eq.get("veiculo") or "",
        "identificadorEquipamento": eq.get("identificador_equipamento"),
        "origemResolucaoEquipe": eq.get("origem_resolucao"),
        "colaborador": eq.get("colaborador") or "",
        "statusConexao": eq.get("status_conexao") or "offline",
        "isOnline": bool(eq.get("is_online")),
        "estadoConsolidado": eq.get("estado_consolidado") or "DESCONHECIDO",
        "turno": eq.get("turno") or {},
        "intervalo": eq.get("intervalo") or {},
        "intervalos": eq.get("intervalos") or [],
        "atividadeAtual": eq.get("atividade_atual"),
        "bdoList": eq.get("bdo_list") or [],
        "ssExecutadasCount": len(eq.get("ss_executadas") or []),
        "ssExecutadas": eq.get("ss_executadas") or [],
        "ssEmAndamento": eq.get("ss_em_andamento") or [],
        "ssPendentesCount": len(eq.get("ss_pendentes") or []),
        "ssPendentesEmergenciaCount": fila_counts.get("emergencia", 0),
        "ssPendentesComercialCount": fila_counts.get("comercial", 0),
        "ssPendentes": eq.get("ss_pendentes") or [],
        "eventTimestampMs": _extrair_timestamp_rotalog(eq),
        "updatedAtIso": timestamp_iso,
    }


def compactar_equipe_para_index(
    document: dict[str, typing.Any],
    current: dict[str, typing.Any] | None = None,
    calendar_day: str | None = None,
) -> dict[str, typing.Any]:
    """Projeta somente o estado atual e os contadores rápidos usados pela torre."""
    jornada = document.get("jornada") or {}
    turno = jornada.get("turno") or {}
    turno_status = str(turno.get("status") or "").upper()

    os_section = document.get("ordensServico") or {}
    servico_atual = os_section.get("atual")
    historico = os_section.get("historico") or []

    # Se o turno estiver FECHADO, o serviço atual fica nulo
    if turno_status == "FECHADO":
        servico_atual = None

    current = current or {}
    target_day = calendar_day or str(document.get("updatedAt") or document.get("date") or "")[:10]
    executados = dict(document.get("executadosHoje") or {"comercial": 0, "emergencia": 0})
    for service in historico:
        if not isinstance(service, dict) or service.get("statusAtual") != "CONCLUSAO":
            continue
        completed_at = (
            service.get("retorno") or service.get("fimExecucao")
            or service.get("concluidoEm") or service.get("fimIso")
        )
        if target_day and str(completed_at or "")[:10] != target_day:
            continue
        category = str(service.get("categoria") or "").upper()
        key = "emergencia" if category == "EMERGENCIA" else "comercial"
        executados[key] += 1

    fila = dict(document.get("fila") or {"comercial": 0, "emergencia": 0})
    if current:
        fila = {
            "comercial": int(current.get("ssPendentesComercialCount") or 0),
            "emergencia": int(current.get("ssPendentesEmergenciaCount") or 0),
        }

    return {
        "schemaVersion": document.get("schemaVersion", 2),
        "teamKey": document.get("teamKey"),
        "teamId": document.get("teamId"),
        "operationalDate": document.get("operationalDate") or document.get("date"),
        "updatedAt": document.get("updatedAt"),
        "timezone": document.get("timezone", "America/Sao_Paulo"),
        "version": document.get("version", 1),
        "conexao": document.get("conexao") or {},
        "jornada": {
            "turno": turno,
            "emIntervalo": bool(jornada.get("emIntervalo")),
        },
        "ordensServico": {
            "atual": servico_atual,
        },
        "executadosHoje": executados,
        "fila": fila,
    }
