"""Gerenciamento de armazenamento local e sincronização com o Firebase Storage (GCS)."""

from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import typing
import unicodedata
import uuid
from zoneinfo import ZoneInfo

from .parser import _eh_protocolo_valido, formatar_protocolo_copel
from .equipes import normalize_team_key

LOCAL_TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))
logger = logging.getLogger(__name__)

TRACKED_FIELDS = (
    "isOnline",
    "statusConexao",
    "identificadorEquipamento",
    "veiculo",
    "colaborador",
    "estadoConsolidado",
    "turno",
    "intervalo",
    "intervalos",
    "atividadeAtual",
    "ssExecutadas",
    "ssEmAndamento",
    "ssPendentes",
)


def company_key(value: str) -> str:
    """Gera a chave estável usada para isolar os dados de uma empresa."""
    normalized = unicodedata.normalize("NFKD", str(value or "").strip())
    key = re.sub(
        r"[^a-z0-9]+",
        "-",
        normalized.encode("ascii", "ignore").decode("ascii").lower(),
    ).strip("-")
    return key or "default"


def rotalog_gcs_paths(
    empresa: str,
    *,
    root_prefix: str | None = None,
    index_blob: str | None = None,
) -> dict[str, str]:
    """Deriva todos os destinos ROTALOG de uma única raiz validada por empresa."""
    empresa_key = company_key(empresa)
    suffix = "/equipes/current/index.json.gz"

    configured_root = str(root_prefix or "").strip().strip("/")
    if configured_root:
        configured_root = configured_root.replace("{empresa}", empresa_key).replace("{company}", empresa_key)

    configured_index = str(index_blob or "").strip().strip("/")
    if configured_index:
        configured_index = configured_index.replace("{empresa}", empresa_key).replace("{company}", empresa_key)
        if not configured_index.lower().endswith(suffix):
            raise ValueError(f"ROTALOG_GCS_CACHE_BLOB deve terminar com {suffix}")
        inferred_root = configured_index[:-len(suffix)].strip("/")
        if configured_root and configured_root.lower() != inferred_root.lower():
            raise ValueError("ROTALOG_GCS_ROOT_PREFIX e ROTALOG_GCS_CACHE_BLOB apontam para raízes diferentes")
        configured_root = inferred_root

    root = configured_root or f"dados/{empresa_key}/rotalog"
    if empresa_key not in {segment.lower() for segment in root.split("/")}:
        raise ValueError(
            f"Raiz GCS '{root}' não contém a chave da empresa '{empresa_key}'; sincronização bloqueada"
        )

    return {
        "companyKey": empresa_key,
        "root": root,
        "index": f"{root}/equipes/current/index.json.gz",
        "teams": f"{root}/equipes",
        "logs": f"{root}/logs",
        "kilometers": f"{root}/quilometragem",
    }


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
    team_key = _safe_team_key(team_key or eq.get("equipe_codigo"))
    return {
        "empresa": empresa,
        "equipe": team_key,
        "teamKey": team_key,
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


def _operational_value(value: typing.Any) -> typing.Any:
    if isinstance(value, dict):
        return {k: _operational_value(v) for k, v in value.items()
                if k not in {"eventIdx", "fonteProtocolo", "validacaoProtocolo", "observadoEm"}}
    if isinstance(value, list):
        return sorted((_operational_value(v) for v in value),
                      key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return value


def changed_fields(
    previous: dict[str, typing.Any] | None,
    current: dict[str, typing.Any],
) -> dict[str, dict[str, typing.Any]]:
    """Retorna o diff entre o documento anterior e o atual (compatível com Schema v2 e v1)."""
    if previous is None:
        return {"novo": {"anterior": None, "novo": True}}

    changes: dict[str, dict[str, typing.Any]] = {}

    # Se o documento atual utiliza Schema v2:
    if "ordensServico" in current or "jornada" in current:
        prev_conexao = previous.get("conexao") or {}
        curr_conexao = current.get("conexao") or {}
        for k in ("isOnline", "veiculo", "colaborador"):
            if prev_conexao.get(k) != curr_conexao.get(k):
                changes[f"conexao.{k}"] = {"anterior": prev_conexao.get(k), "novo": curr_conexao.get(k)}

        prev_jornada = previous.get("jornada") or {}
        curr_jornada = current.get("jornada") or {}
        if prev_jornada.get("emIntervalo") != curr_jornada.get("emIntervalo"):
            changes["jornada.emIntervalo"] = {"anterior": prev_jornada.get("emIntervalo"), "novo": curr_jornada.get("emIntervalo")}

        prev_turno = prev_jornada.get("turno") or {}
        curr_turno = curr_jornada.get("turno") or {}
        for k in ("status", "inicio", "fim"):
            if prev_turno.get(k) != curr_turno.get(k):
                changes[f"jornada.turno.{k}"] = {"anterior": prev_turno.get(k), "novo": curr_turno.get(k)}

        if _operational_value(prev_jornada.get("intervalos")) != _operational_value(curr_jornada.get("intervalos")):
            changes["jornada.intervalos"] = {"anterior": prev_jornada.get("intervalos"), "novo": curr_jornada.get("intervalos")}

        prev_os = previous.get("ordensServico") or {}
        curr_os = current.get("ordensServico") or {}
        if _operational_value(prev_os.get("atual")) != _operational_value(curr_os.get("atual")):
            changes["ordensServico.atual"] = {"anterior": prev_os.get("atual"), "novo": curr_os.get("atual")}

        prev_concluidos = prev_os.get("totalConcluidos")
        if prev_concluidos is None:
            prev_concluidos = len(prev_os.get("historico") or [])
        curr_concluidos = curr_os.get("totalConcluidos")
        if curr_concluidos is None:
            curr_concluidos = len(curr_os.get("historico") or [])

        if prev_concluidos != curr_concluidos:
            changes["ordensServico.historico"] = {"anterior": prev_concluidos, "novo": curr_concluidos}
        elif "historico" in prev_os and "historico" in curr_os:
            if _operational_value(prev_os["historico"]) != _operational_value(curr_os["historico"]):
                changes["ordensServico.historico"] = {"anterior": prev_os["historico"], "novo": curr_os["historico"]}

        return changes

    for field in TRACKED_FIELDS:
        old_value = previous.get(field)
        new_value = current.get(field)
        if _operational_value(old_value) != _operational_value(new_value):
            changes[field] = {"anterior": old_value, "novo": new_value}
    return changes


def _summarize_history_correction(
    previous: dict[str, typing.Any] | None,
    current: dict[str, typing.Any],
) -> str:
    """Identifica sinteticamente qual ajuste foi realizado nas OS do histórico."""
    if not previous:
        return "Correção de OS"
    prev_os = previous.get("ordensServico") or {}
    curr_os = current.get("ordensServico") or {}
    prev_history = prev_os.get("historico") or []
    curr_history = curr_os.get("historico") or []

    prev_map = {
        srv.get("serviceId", str(i)): srv
        for i, srv in enumerate(prev_history)
        if isinstance(srv, dict)
    }

    has_protocol = False
    has_time = False
    has_gps = False
    has_type = False
    has_queue = False

    for i, curr_srv in enumerate(curr_history):
        if not isinstance(curr_srv, dict):
            continue
        sid = curr_srv.get("serviceId", str(i))
        prev_srv = prev_map.get(sid)
        if not prev_srv and i < len(prev_history) and isinstance(prev_history[i], dict):
            prev_srv = prev_history[i]
        if not prev_srv:
            continue

        if prev_srv.get("protocolo") != curr_srv.get("protocolo") and curr_srv.get("protocolo"):
            has_protocol = True
        for t_field in ("inicioDeslocamento", "inicioExecucao", "fimExecucao", "retorno"):
            if prev_srv.get(t_field) != curr_srv.get(t_field):
                has_time = True
        if (prev_srv.get("latitude") != curr_srv.get("latitude")
                or prev_srv.get("longitude") != curr_srv.get("longitude")):
            if curr_srv.get("latitude") is not None:
                has_gps = True
        if prev_srv.get("tipo") != curr_srv.get("tipo") and curr_srv.get("tipo"):
            has_type = True
        if prev_srv.get("filaNaConclusao") != curr_srv.get("filaNaConclusao"):
            has_queue = True

    if has_protocol:
        return "OS (+Protocolo)"
    if has_time:
        return "OS (Horário)"
    if has_gps:
        return "OS (+GPS)"
    if has_type:
        return "OS (Tipo)"
    if has_queue:
        return "OS (Fila)"
    return "Correção de OS"


def summarize_team_transition(
    previous: dict[str, typing.Any] | None,
    current: dict[str, typing.Any],
    sync_reasons: list[str] | None = None,
) -> str:
    """Gera um resumo legível e específico da transição operacional da equipe."""
    reasons = set(sync_reasons or [])
    if "servico_concluido" in reasons:
        return "Execução --> Conclusão"
    if "correcao_servico_concluido" in reasons:
        return _summarize_history_correction(previous, current)
    if "turno_aberto" in reasons:
        return "Início de Turno"
    if "turno_fechado" in reasons:
        return "Fim de Turno"

    if previous is None:
        return "Início"

    prev_jornada = previous.get("jornada") or {}
    curr_jornada = current.get("jornada") or {}

    prev_turno = prev_jornada.get("turno") or {}
    curr_turno = curr_jornada.get("turno") or {}
    prev_turno_status = str(prev_turno.get("status") or "").upper()
    curr_turno_status = str(curr_turno.get("status") or "").upper()

    if curr_turno_status == "ABERTO" and prev_turno_status != "ABERTO":
        return "Início de Turno"
    if curr_turno_status == "FECHADO" and prev_turno_status != "FECHADO":
        return "Fim de Turno"

    # Intervalo
    prev_intervalo = bool(prev_jornada.get("emIntervalo"))
    curr_intervalo = bool(curr_jornada.get("emIntervalo"))
    if not prev_intervalo and curr_intervalo:
        return "Início de Intervalo"
    if prev_intervalo and not curr_intervalo:
        return "Fim de Intervalo"

    # Ordens de Serviço
    prev_os = previous.get("ordensServico") or {}
    curr_os = current.get("ordensServico") or {}

    prev_concluidos = prev_os.get("totalConcluidos")
    if prev_concluidos is None:
        prev_concluidos = len(prev_os.get("historico") or [])
    curr_concluidos = curr_os.get("totalConcluidos")
    if curr_concluidos is None:
        curr_concluidos = len(curr_os.get("historico") or [])

    if curr_concluidos > prev_concluidos:
        return "Execução --> Conclusão"

    prev_history = prev_os.get("historico") or []
    curr_history = curr_os.get("historico") or []
    if prev_history and curr_history and _operational_value(prev_history) != _operational_value(curr_history):
        return _summarize_history_correction(previous, current)

    status_map = {
        "DESLOCAMENTO": "Deslocamento",
        "EXECUCAO": "Execução",
        "CONCLUIDO": "Conclusão",
        "CONCLUSAO": "Conclusão",
        None: "Livre",
        "": "Livre",
    }
    prev_atual = prev_os.get("atual") or {}
    curr_atual = curr_os.get("atual") or {}

    # Suporta tanto statusAtual (padrão v2) quanto status (legado)
    prev_status_raw = None
    if isinstance(prev_atual, dict):
        prev_status_raw = prev_atual.get("statusAtual") or prev_atual.get("status")
    curr_status_raw = None
    if isinstance(curr_atual, dict):
        curr_status_raw = curr_atual.get("statusAtual") or curr_atual.get("status")

    if prev_status_raw != curr_status_raw:
        p_label = status_map.get(prev_status_raw, str(prev_status_raw).capitalize() if prev_status_raw else "Livre")
        c_label = status_map.get(curr_status_raw, str(curr_status_raw).capitalize() if curr_status_raw else "Livre")
        return f"{p_label} --> {c_label}"

    prev_prot = prev_atual.get("protocolo") if isinstance(prev_atual, dict) else None
    curr_prot = curr_atual.get("protocolo") if isinstance(curr_atual, dict) else None
    if prev_prot != curr_prot and curr_prot:
        c_label = status_map.get(curr_status_raw, "OS")
        return f"Nova OS ({c_label})"

    prev_conn = previous.get("conexao") or {}
    curr_conn = current.get("conexao") or {}
    if prev_conn.get("isOnline") != curr_conn.get("isOnline"):
        return "Online" if curr_conn.get("isOnline") else "Offline"

    if prev_conn.get("veiculo") != curr_conn.get("veiculo") and curr_conn.get("veiculo"):
        return f"Veículo ({curr_conn.get('veiculo')})"

    if prev_conn.get("colaborador") != curr_conn.get("colaborador") and curr_conn.get("colaborador"):
        return "Equipe Alterada"

    if curr_status_raw:
        c_label = status_map.get(curr_status_raw, str(curr_status_raw).capitalize())
        if isinstance(prev_atual, dict) and isinstance(curr_atual, dict):
            if (prev_atual.get("latitude") != curr_atual.get("latitude")
                    or prev_atual.get("longitude") != curr_atual.get("longitude")):
                return f"GPS ({c_label})"
        return f"Em {c_label}"

    if curr_turno_status == "ABERTO":
        return "Livre"

    return "Atualizado"


def compactar_equipe_para_index(document: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Gera a versão compacta da equipe para o index.json (tempo real / torre de controle).
    
    Remove o array pesado de histórico, mantendo apenas o serviço atual (se em turno aberto)
    e contadores resumidos para acompanhamento operacional em tempo real.
    """
    jornada = document.get("jornada") or {}
    turno = jornada.get("turno") or {}
    turno_status = str(turno.get("status") or "").upper()

    os_section = document.get("ordensServico") or {}
    servico_atual = os_section.get("atual")
    historico = os_section.get("historico") or []

    # Se o turno estiver FECHADO, o serviço atual fica nulo
    if turno_status == "FECHADO":
        servico_atual = None

    ult_concluido = historico[-1] if historico else None
    ult_hora = (
        ult_concluido.get("retorno")
        or ult_concluido.get("fimExecucao")
        or ult_concluido.get("inicioExecucao")
    ) if ult_concluido else None

    return {
        "schemaVersion": document.get("schemaVersion", 2),
        "teamKey": document.get("teamKey"),
        "date": document.get("date"),
        "updatedAt": document.get("updatedAt"),
        "timezone": document.get("timezone", "America/Sao_Paulo"),
        "version": document.get("version", 1),
        "conexao": document.get("conexao") or {},
        "jornada": {
            "turno": turno,
            "emIntervalo": bool(jornada.get("emIntervalo")),
            "totalIntervalos": len(jornada.get("intervalos") or []) if "intervalos" in jornada else jornada.get("totalIntervalos", 0),
        },
        "ordensServico": {
            "atual": servico_atual,
            "totalConcluidos": len(historico) if "historico" in os_section else os_section.get("totalConcluidos", 0),
            "historicoUpdatedAt": ult_hora if "historico" in os_section else os_section.get("historicoUpdatedAt"),
        },
    }

def _json_cache_default(value: typing.Any) -> typing.Any:
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _decode_json_object(payload: bytes) -> dict[str, typing.Any]:
    raw = gzip.decompress(payload) if payload.startswith(b"\x1f\x8b") else payload
    loaded = json.loads(raw.decode("utf-8-sig"))
    return loaded if isinstance(loaded, dict) else {}


def load_json_with_status(path: Path, fallback: typing.Any) -> tuple[typing.Any, str]:
    """Lê um JSON e distingue arquivo ausente de conteúdo local corrompido."""
    target = path
    if not target.exists():
        if target.name.endswith(".gz"):
            fallback_target = target.with_name(target.name[:-3])
            if fallback_target.exists():
                target = fallback_target
        else:
            fallback_target = target.with_name(target.name + ".gz")
            if fallback_target.exists():
                target = fallback_target

    if not target.exists():
        return fallback, "missing"

    try:
        payload = target.read_bytes()
        raw = gzip.decompress(payload) if payload.startswith(b"\x1f\x8b") else payload
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, type(fallback)):
            return fallback, "corrupt"
        return value, "ok"
    except (json.JSONDecodeError, OSError, EOFError, UnicodeDecodeError):
        return fallback, "corrupt"


def load_json(path: Path, fallback: typing.Any) -> typing.Any:
    value, _ = load_json_with_status(path, fallback)
    return value


def write_json(path: Path, value: typing.Any, compress: bool | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    should_compress = compress if compress is not None else path.name.endswith(".gz")

    if should_compress:
        raw_bytes = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        compressed_bytes = gzip.compress(raw_bytes, compresslevel=6)
        with temporary.open("wb") as stream:
            stream.write(compressed_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())

    temporary.replace(path)


# ---------------------------------------------------------------------------
# Daily Document Merging
# ---------------------------------------------------------------------------

def _safe_team_key(value: str) -> str:
    key = normalize_team_key(value)
    if not re.fullmatch(r"E[A-Z0-9]{3,7}", key):
        raise ValueError("Codigo de equipe invalido")
    return key


def _iso_local(value: typing.Any, day: str | None = None) -> str | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)) and value > 1000000000000:
        return datetime.datetime.fromtimestamp(value / 1000, LOCAL_TZ).isoformat()
    raw = str(value).strip()
    if len(raw) == 5 and raw[2] == ":" and day:
        try:
            return datetime.datetime.fromisoformat(f"{day}T{raw}:00").replace(tzinfo=LOCAL_TZ).isoformat()
        except ValueError:
            return None
    if len(raw) == 8 and raw[2] == ":" and raw[5] == ":" and day:
        try:
            return datetime.datetime.fromisoformat(f"{day}T{raw}").replace(tzinfo=LOCAL_TZ).isoformat()
        except ValueError:
            return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.astimezone(LOCAL_TZ).isoformat()
    except ValueError:
        return None


def _extrair_data_base(service: dict[str, typing.Any], day: str) -> str:
    for field in ("inicioIso", "inicio_iso", "fimIso", "fim_iso"):
        val = str(service.get(field) or "").strip()
        if len(val) >= 10 and val[4] == "-" and val[7] == "-":
            try:
                datetime.date.fromisoformat(val[:10])
                return val[:10]
            except ValueError:
                pass

    for field in ("inicio_ms", "timestampMs", "start"):
        val = service.get(field)
        if isinstance(val, (int, float)) and val > 1000000000000:
            dt = datetime.datetime.fromtimestamp(val / 1000, LOCAL_TZ)
            return dt.date().isoformat()

    hora_str = str(service.get("inicioDeslocamento") or service.get("inicioExecucao") or "").strip()
    if len(hora_str) == 5 and hora_str[2] == ":":
        try:
            ref_dt = datetime.datetime.fromisoformat(f"{day}T{hora_str}:00").replace(tzinfo=LOCAL_TZ)
            agora = datetime.datetime.now(LOCAL_TZ)
            if day == agora.date().isoformat() and ref_dt > agora + datetime.timedelta(minutes=15):
                ontem = (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()
                return ontem
        except Exception:
            pass

    return day


def _formatar_horarios_servico(service: dict[str, typing.Any], base_day: str) -> dict[str, str | None]:
    raw_desloc = service.get("inicioDeslocamento") or service.get("inicioIso")
    raw_exec = service.get("inicioExecucao") or service.get("inicioIso")
    raw_fim = service.get("termino") or service.get("fimIso") or service.get("fimExecucao")
    raw_retorno = service.get("retorno")

    cur_day = datetime.date.fromisoformat(base_day)
    prev_dt: datetime.datetime | None = None

    result: dict[str, str | None] = {
        "inicioDeslocamento": None,
        "inicioExecucao": None,
        "fimExecucao": None,
        "retorno": None,
    }

    for key, raw_val in [
        ("inicioDeslocamento", raw_desloc),
        ("inicioExecucao", raw_exec),
        ("fimExecucao", raw_fim),
        ("retorno", raw_retorno),
    ]:
        if not raw_val or str(raw_val).strip() in ("", "-"):
            continue

        if isinstance(raw_val, (int, float)) and raw_val > 1000000000000:
            dt_val = datetime.datetime.fromtimestamp(raw_val / 1000, LOCAL_TZ)
            prev_dt = dt_val
            result[key] = dt_val.isoformat()
            continue

        raw_str = str(raw_val).strip()
        dt_val: datetime.datetime | None = None

        if len(raw_str) == 5 and raw_str[2] == ":":
            try:
                candidate = datetime.datetime.fromisoformat(f"{cur_day.isoformat()}T{raw_str}:00").replace(tzinfo=LOCAL_TZ)
                if prev_dt and prev_dt.hour >= 21 and int(raw_str[:2]) < 6:
                    cur_day = cur_day + datetime.timedelta(days=1)
                    candidate = datetime.datetime.fromisoformat(f"{cur_day.isoformat()}T{raw_str}:00").replace(tzinfo=LOCAL_TZ)
                dt_val = candidate
            except ValueError:
                dt_val = None
        elif len(raw_str) == 8 and raw_str[2] == ":" and raw_str[5] == ":":
            try:
                candidate = datetime.datetime.fromisoformat(f"{cur_day.isoformat()}T{raw_str}").replace(tzinfo=LOCAL_TZ)
                if prev_dt and prev_dt.hour >= 21 and int(raw_str[:2]) < 6:
                    cur_day = cur_day + datetime.timedelta(days=1)
                    candidate = datetime.datetime.fromisoformat(f"{cur_day.isoformat()}T{raw_str}").replace(tzinfo=LOCAL_TZ)
                dt_val = candidate
            except ValueError:
                dt_val = None
        else:
            try:
                parsed = datetime.datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=LOCAL_TZ)
                dt_val = parsed.astimezone(LOCAL_TZ)
            except ValueError:
                dt_val = None

        if dt_val:
            prev_dt = dt_val
            result[key] = dt_val.isoformat()

    if result["inicioDeslocamento"] and result["inicioExecucao"]:
        if result["inicioExecucao"] < result["inicioDeslocamento"]:
            result["inicioExecucao"] = result["inicioDeslocamento"]

    if result["inicioExecucao"] and result["fimExecucao"]:
        if result["fimExecucao"] < result["inicioExecucao"]:
            result["fimExecucao"] = result["inicioExecucao"]

    if result["fimExecucao"] and result["retorno"]:
        if result["retorno"] < result["fimExecucao"]:
            result["retorno"] = result["fimExecucao"]

    return result


def _service_id(team_key: str, service: dict[str, typing.Any]) -> str:
    existing = str(service.get("serviceId") or "").strip()
    if existing:
        if not existing.startswith(team_key + "_"):
            raise ValueError("serviceId pertence a outra equipe")
        return existing
    identity = "|".join(
        str(value or "")
        for value in (
            team_key,
            service.get("inicioIso") or service.get("inicioDeslocamento") or service.get("inicioExecucao"),
        )
    )
    return f"{team_key}_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def compact_service(team_key: str, day: str, service: dict[str, typing.Any]) -> dict[str, typing.Any]:
    lat = service.get("latitude")
    lon = service.get("longitude")
    if lat is None and isinstance(service.get("geolocalizacao"), dict):
        lat = service["geolocalizacao"].get("latitude")
        lon = service["geolocalizacao"].get("longitude")

    fila_conclusao = service.get("filaNaConclusao")
    if isinstance(fila_conclusao, dict):
        fila_conclusao = {
            "emergencia": int(fila_conclusao.get("emergencia") or 0),
            "comercial": int(fila_conclusao.get("comercial") or 0),
        }

    status_atual = service.get("status") or service.get("statusAtual")
    base_day = _extrair_data_base(service, day)
    horarios = _formatar_horarios_servico(service, base_day)

    result = {
        "categoria": service.get("categoria"),
        "tipo": service.get("tipo"),
        "protocolo": service.get("protocolo") or service.get("ssId") or None,
        "inicioDeslocamento": horarios["inicioDeslocamento"],
        "inicioExecucao": horarios["inicioExecucao"],
        "fimExecucao": horarios["fimExecucao"],
        "retorno": horarios["retorno"],
        "latitude": lat,
        "longitude": lon,
        "serviceId": _service_id(team_key, service),
        "statusAtual": status_atual,
        "sequencia": service.get("sequencia") or None,
        "baseDay": base_day,
        "camposEstimados": list(service.get("camposEstimados") or []),
    }
    if fila_conclusao is not None and status_atual == "CONCLUSAO":
        result["filaNaConclusao"] = fila_conclusao
    return result


def _compact_interval(day: str, interval: dict[str, typing.Any]) -> dict[str, typing.Any] | None:
    start = _iso_local(interval.get("inicioIso") or interval.get("inicio"), day)
    if not start and interval.get("inicio_ms"):
        start = _iso_local(int(interval["inicio_ms"]), day)
    if not start:
        return None
    end = _iso_local(interval.get("fimIso") or interval.get("fim"), day)
    if not end and interval.get("fim_ms"):
        end = _iso_local(int(interval["fim_ms"]), day)
    duracao = None
    if start and end:
        try:
            d_start = datetime.datetime.fromisoformat(start)
            d_end = datetime.datetime.fromisoformat(end)
            duracao = int((d_end - d_start).total_seconds() // 60)
        except Exception:
            duracao = None
    return {"inicio": start, "fim": end, "duracaoMinutos": duracao}


def _merge_service_records(team_key: str, records: list[dict[str, typing.Any]]) -> dict[str, dict[str, typing.Any]]:
    result = {}
    keys = {}
    starts = {}
    for incoming in records:
        item = dict(incoming)
        sid = _service_id(team_key, item)
        start = item.get("inicioDeslocamento") or item.get("inicioExecucao")
        prot = str(item.get("protocolo") or "").strip()
        prot_valid = _eh_protocolo_valido(prot)
        key = (team_key, start, prot) if start and prot_valid else None

        target = None
        if key and key in keys:
            target = keys[key]
        elif sid in result:
            target = sid
        elif start and (team_key, start) in starts:
            target = starts[(team_key, start)]
        else:
            target = sid

        old = result.get(target, {})
        old_prot = str(old.get("protocolo") or "").strip()
        old_start = old.get("inicioDeslocamento") or old.get("inicioExecucao")
        if old and old_prot and prot_valid and old_prot != prot:
            suffix = hashlib.sha256(repr((sid, start, prot)).encode()).hexdigest()[:24]
            target = f"{team_key}_{suffix}"
            old = result.get(target, {})

        merged = dict(old)
        old_time = old.get("observadoEm")
        new_time = item.get("observadoEm")
        stale = bool(old_time and new_time and
                     datetime.datetime.fromisoformat(new_time) < datetime.datetime.fromisoformat(old_time))
        for field, value in item.items():
            if field == "filaNaConclusao" and merged.get("filaNaConclusao") is not None:
                continue
            if value is not None and value != "" and (not stale or merged.get(field) in (None, "")):
                merged[field] = value
        if old.get("statusAtual") == "CONCLUSAO" and item.get("statusAtual") in ("EXECUCAO", "DESLOCAMENTO"):
            merged["statusAtual"] = "CONCLUSAO"
        tipos = list(old.get("historicoTipos") or [])
        for tipo in (old.get("tipo"), item.get("tipo")):
            if tipo and tipo not in tipos:
                tipos.append(tipo)
        if tipos:
            merged["historicoTipos"] = tipos
        merged["serviceId"] = target
        result[target] = merged
        if key:
            keys[key] = target
        if start:
            starts[(team_key, start)] = target
    return result


def merge_daily_document(
    previous: dict[str, typing.Any] | None,
    current: dict[str, typing.Any],
    day: str,
) -> dict[str, typing.Any]:
    team_key = _safe_team_key(current.get("teamKey") or current.get("equipe"))
    previous = previous or {}
    if previous.get("teamKey") and previous["teamKey"] != team_key:
        raise ValueError("Historico pertence a outra equipe")
    if previous.get("date") and previous["date"] != day:
        raise ValueError("Historico pertence a outra data")

    # Recupera serviços anteriores (compatível com v2 ordensServico.historico ou v1 services)
    records = []
    if isinstance(previous.get("ordensServico"), dict):
        records = [dict(s) for s in previous["ordensServico"].get("historico", [])]
        if previous["ordensServico"].get("atual"):
            records.append(dict(previous["ordensServico"]["atual"]))
    elif "services" in previous:
        records = [dict(s) for s in previous.get("services", [])]

    existing_concluded_ids = {
        _service_id(team_key, s)
        for s in records
        if isinstance(s, dict) and s.get("statusAtual") == "CONCLUSAO" and s.get("filaNaConclusao") is not None
    }

    for field in ("ssExecutadas", "ssEmAndamento", "services", "bdoList"):
        for raw in current.get(field) or []:
            item = compact_service(team_key, day, raw)
            item["observadoEm"] = current.get("updatedAtIso") or current.get("updatedAt")
            for meta in ("fonteProtocolo", "validacaoProtocolo", "protocoloBruto"):
                if raw.get(meta) is not None:
                    item[meta] = raw[meta]
            if item.get("statusAtual") == "CONCLUSAO" and "filaNaConclusao" not in item:
                sid_check = _service_id(team_key, item)
                if sid_check not in existing_concluded_ids:
                    item["filaNaConclusao"] = {
                        "emergencia": int(current.get("ssPendentesEmergenciaCount") or 0),
                        "comercial": int(current.get("ssPendentesComercialCount") or 0),
                    }
            records.append(item)
    service_map = _merge_service_records(team_key, records)

    # Intervalos anteriores e novos
    prev_intervals = []
    if isinstance(previous.get("jornada"), dict):
        prev_intervals = previous["jornada"].get("intervalos") or []
    elif isinstance(previous.get("turno"), dict):
        prev_intervals = previous["turno"].get("intervalos") or []

    interval_map = {
        str(item.get("inicio")): dict(item)
        for item in prev_intervals
        if isinstance(item, dict) and item.get("inicio")
    }
    raw_intervals = current.get("intervalos") or []
    if not raw_intervals and current.get("intervalo"):
        raw_intervals = [current["intervalo"]]
    for raw_interval in raw_intervals:
        compact_interval = _compact_interval(day, raw_interval)
        if compact_interval:
            previous_interval = interval_map.get(compact_interval["inicio"], {})
            interval_map[compact_interval["inicio"]] = {
                **previous_interval,
                **{key: value for key, value in compact_interval.items() if value is not None},
            }

    turno = current.get("turno") or {}
    services = [
        srv for srv in sorted(service_map.values(), key=lambda item: str(item.get("inicioDeslocamento") or item.get("inicioExecucao") or ""))
        if (srv.get("baseDay") == day or str(srv.get("inicioDeslocamento") or srv.get("inicioExecucao") or "")[:10] == day)
    ]
    activity_raw = current.get("atividadeAtual") or {}
    current_service = compact_service(team_key, day, activity_raw) if activity_raw else None

    # Garantia de unicidade de atividade ativa
    active_in_list = [srv for srv in services if srv.get("statusAtual") in ("EXECUCAO", "DESLOCAMENTO")]
    if active_in_list:
        if current_service is None:
            for srv in active_in_list:
                srv["statusAtual"] = "REDIRECIONADO"
                srv["semExecucaoType"] = "REDIRECIONADO"
                srv["fimExecucao"] = srv.get("fimExecucao") or srv.get("inicioExecucao") or srv.get("inicioDeslocamento")
                srv["retorno"] = srv["fimExecucao"]
        elif len(active_in_list) > 1:
            active_most_recent = max(active_in_list, key=lambda item: str(item.get("inicioDeslocamento") or item.get("inicioExecucao") or ""))
            for srv in active_in_list:
                if srv != active_most_recent:
                    proximo_ini = active_most_recent.get("inicioDeslocamento") or active_most_recent.get("inicioExecucao")
                    srv["statusAtual"] = "REDIRECIONADO"
                    srv["semExecucaoType"] = "REDIRECIONADO"
                    srv["fimExecucao"] = proximo_ini or srv.get("inicioExecucao") or srv.get("inicioDeslocamento")
                    srv["retorno"] = srv["fimExecucao"]

    # Fila de pendências (usada para registrar filaNaAbertura se o turno estiver abrindo)
    fila_total = int(current.get("ssPendentesCount") or 0)
    fila_emergencia = int(current.get("ssPendentesEmergenciaCount") or 0)
    fila_comercial = int(current.get("ssPendentesComercialCount") or 0)

    # Status e horários do turno
    status_turno = "ABERTO" if (turno.get("aberto") or current.get("estadoConsolidado") == "ABERTO" or current_service) else ("FECHADO" if turno.get("classificacao") == "FECHADO" else "FECHADO")
    ini_turno = _iso_local(turno.get("inicio_iso") or turno.get("inicioIso") or turno.get("inicio"), day)
    fim_turno = _iso_local(turno.get("fim_iso") or turno.get("fimIso") or turno.get("fim"), day)
    duracao_minutos = None
    if ini_turno and fim_turno:
        try:
            d_ini = datetime.datetime.fromisoformat(ini_turno)
            d_fim = datetime.datetime.fromisoformat(fim_turno)
            duracao_minutos = int((d_fim - d_ini).total_seconds() // 60)
        except Exception:
            duracao_minutos = None

    # Fila na abertura de turno
    prev_fila_abertura = None
    if isinstance(previous.get("jornada"), dict):
        prev_fila_abertura = (previous["jornada"].get("turno") or {}).get("filaNaAbertura")
    elif isinstance(previous.get("turno"), dict):
        prev_fila_abertura = previous["turno"].get("filaNaAbertura")

    if prev_fila_abertura is not None:
        fila_na_abertura = prev_fila_abertura
    elif status_turno == "ABERTO":
        fila_na_abertura = {
            "total": fila_total,
            "emergencia": fila_emergencia,
            "comercial": fila_comercial,
        }
    else:
        fila_na_abertura = None

    # Histórico de serviços concluídos / redirecionados
    historico = [srv for srv in services if srv.get("statusAtual") in ("CONCLUSAO", "REDIRECIONADO", "CANCELADO")]

    prev_version = int(previous.get("version") or (previous.get("current") or {}).get("version") or 0)
    prev_date = str(previous.get("date") or "")
    daily_version = 1 if prev_date != day else (prev_version + 1 if previous else 1)

    return {
        "schemaVersion": 2,
        "company": current.get("empresa") or previous.get("company") or previous.get("empresa"),
        "companyKey": company_key(
            current.get("empresa") or previous.get("company") or previous.get("empresa")
        ),
        "teamKey": team_key,
        "date": day,
        "updatedAt": current.get("updatedAtIso") or current.get("updatedAt"),
        "timezone": str(LOCAL_TZ),
        "version": daily_version,
        "conexao": {
            "isOnline": bool(current.get("isOnline")),
            "status": current.get("statusConexao") or "offline",
            "veiculo": current.get("veiculo") or "",
            "identificadorEquipamento": current.get("identificadorEquipamento") or "",
            "colaborador": current.get("colaborador") or "",
            "origemResolucao": current.get("origemResolucaoEquipe") or "",
        },
        "jornada": {
            "turno": {
                "status": status_turno,
                "inicio": ini_turno,
                "fim": fim_turno,
                "duracaoMinutos": duracao_minutos,
                "filaNaAbertura": fila_na_abertura,
            },
            "emIntervalo": bool(
                ((current.get("intervalo") or {}).get("em_intervalo") and not (current.get("intervalo") or {}).get("fim_ms") and not (current.get("intervalo") or {}).get("fimIso"))
                or current.get("estadoConsolidado") == "INTERVALO"
            ) and current_service is None,
            "intervalos": sorted(interval_map.values(), key=lambda item: str(item.get("inicio") or "")),
        },
        "ordensServico": {
            "atual": current_service,
            "historico": historico,
        },
    }


# ---------------------------------------------------------------------------
# GCS / Firebase Storage Classes
# ---------------------------------------------------------------------------

class RotalogGcsSnapshotStore:
    """Armazenamento persistente e atômico em bucket GCS/Firebase Storage com GZIP."""

    def __init__(
        self,
        bucket_name: str,
        blob_name: str,
        *,
        root_prefix: str | None = None,
        client_factory: typing.Callable[[], typing.Any] | None = None,
    ):
        self.bucket_name = bucket_name.strip()
        self.blob_name = blob_name.strip().lstrip("/")
        self.root_prefix = str(root_prefix or "").strip().strip("/")
        index_suffix = "/equipes/current/index.json.gz"
        if not self.root_prefix and self.blob_name.lower().endswith(index_suffix):
            self.root_prefix = self.blob_name[:-len(index_suffix)].strip("/")
        self._client_factory = client_factory
        self._client = None
        self._lock = threading.RLock()
        self.bytes_uploaded = 0
        self.bytes_downloaded = 0
        self.cycle_bytes_uploaded = 0
        self.cycle_bytes_downloaded = 0

    @property
    def enabled(self) -> bool:
        return bool(self.bucket_name and self.blob_name)

    def reset_cycle_bytes(self) -> tuple[int, int]:
        """Retorna (bytes_enviados, bytes_lidos) no ciclo atual e zera os contadores do ciclo."""
        with self._lock:
            up = self.cycle_bytes_uploaded
            down = self.cycle_bytes_downloaded
            self.cycle_bytes_uploaded = 0
            self.cycle_bytes_downloaded = 0
            return up, down

    def _blob_named(self, blob_name: str):
        if not self.enabled:
            raise RuntimeError("Cache GCS do ROTALOG não configurado.")
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from google.cloud import storage
                self._client = storage.Client()
        return self._client.bucket(self.bucket_name).blob(blob_name.strip().lstrip("/"))

    def _blob(self):
        return self._blob_named(self.blob_name)

    def load(self) -> dict[str, typing.Any]:
        with self._lock:
            try:
                blob = self._blob()
                try:
                    compressed = blob.download_as_bytes(raw_download=True)
                except TypeError:
                    compressed = blob.download_as_bytes()
                if compressed:
                    self.bytes_downloaded += len(compressed)
                    self.cycle_bytes_downloaded += len(compressed)
                return _decode_json_object(compressed)
            except Exception as exc:
                if getattr(exc, "code", None) == 404:
                    return {}
                logger.debug("Aviso ao carregar blob remoto %s: %s", self.blob_name, exc)
                return {}

    def save(self, snapshots: dict[str, typing.Any]) -> None:
        self.save_blob(self.blob_name, snapshots)

    def load_blob(self, blob_name: str) -> dict[str, typing.Any]:
        with self._lock:
            try:
                blob = self._blob_named(blob_name)
                try:
                    compressed = blob.download_as_bytes(raw_download=True)
                except TypeError:
                    compressed = blob.download_as_bytes()
                if compressed:
                    self.bytes_downloaded += len(compressed)
                    self.cycle_bytes_downloaded += len(compressed)
                return _decode_json_object(compressed)
            except Exception as exc:
                if getattr(exc, "code", None) == 404:
                    return {}
                raise

    def save_blob(self, blob_name: str, payload: dict[str, typing.Any]) -> None:
        with self._lock:
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=_json_cache_default,
            ).encode("utf-8")
            blob = self._blob_named(blob_name)
            blob.content_encoding = "gzip"
            compressed = gzip.compress(raw, compresslevel=6)
            blob.upload_from_string(
                compressed,
                content_type="application/json",
            )
            self.bytes_uploaded += len(compressed)
            self.cycle_bytes_uploaded += len(compressed)

    def update_blob(self, blob_name: str, transform: typing.Callable[[dict], dict]) -> dict[str, typing.Any]:
        """Compare-and-swap atômico para evitar conflitos concorrentes de gravação."""
        for tentativa in range(5):
            blob = self._blob_named(blob_name)
            try:
                blob.reload()
                generation = int(blob.generation)
                try:
                    raw_bytes = blob.download_as_bytes(raw_download=True, if_generation_match=generation)
                except TypeError:
                    raw_bytes = blob.download_as_bytes(if_generation_match=generation)
                if raw_bytes:
                    with self._lock:
                        self.bytes_downloaded += len(raw_bytes)
                        self.cycle_bytes_downloaded += len(raw_bytes)
                previous = _decode_json_object(raw_bytes)
            except Exception as exc:
                if getattr(exc, "code", None) == 404:
                    generation, previous = 0, {}
                elif getattr(exc, "code", None) == 412:
                    continue
                else:
                    raise
            merged = transform(previous)
            raw = json.dumps(merged, ensure_ascii=False, default=_json_cache_default).encode("utf-8")
            try:
                blob.content_encoding = "gzip"
                compressed = gzip.compress(raw)
                blob.upload_from_string(
                    compressed,
                    content_type="application/json",
                    if_generation_match=generation,
                )
                with self._lock:
                    self.bytes_uploaded += len(compressed)
                    self.cycle_bytes_uploaded += len(compressed)
                return merged
            except Exception as exc:
                if getattr(exc, "code", None) != 412:
                    raise
        raise RuntimeError("Conflito concorrente persistente no JSON RTL")


class RotalogTeamFileRepository:
    def __init__(self, store: RotalogGcsSnapshotStore, root_prefix: str):
        self.store = store
        self.root_prefix = root_prefix.strip().strip("/")
        self._lock = threading.RLock()
        self._daily_cache: dict[tuple[str, str], dict[str, typing.Any]] = {}

    def current_path(self, team_key: str) -> str:
        return f"{self.root_prefix}/current/{_safe_team_key(team_key)}.json.gz"

    def daily_path(self, day: str, team_key: str) -> str:
        return f"{self.root_prefix}/daily/{day}/{_safe_team_key(team_key)}.json.gz"

    def save_current(self, document: dict[str, typing.Any]) -> None:
        path = self.current_path(document.get("teamKey") or document.get("equipe"))
        self.store.save_blob(path, document)

    def save_daily(self, document: dict[str, typing.Any], day: str) -> None:
        """Gravação única e direta no Storage da equipe consolidada no dia."""
        team_key = _safe_team_key(document.get("teamKey") or document.get("equipe"))
        path = self.daily_path(day, team_key)
        self.store.save_blob(path, document)

    def merge_and_save_daily(self, current: dict[str, typing.Any], day: str) -> dict[str, typing.Any]:
        team_key = _safe_team_key(current.get("teamKey") or current.get("equipe"))
        cache_key = (day, team_key)
        with self._lock:
            daily_path = self.daily_path(day, team_key)
            merged = self.store.update_blob(
                daily_path,
                lambda previous: merge_daily_document(previous, current, day),
            )
            self._daily_cache[cache_key] = merged
            return merged


class RotalogExecutionLog:
    def __init__(self, store: RotalogGcsSnapshotStore, root_prefix: str):
        self.store = store
        self.root_prefix = root_prefix.strip().strip("/")

    def path(self, day: str) -> str:
        return f"{self.root_prefix}/{day}.json.gz"

    def record(
        self,
        status: str,
        *,
        started_at: datetime.datetime,
        finished_at: datetime.datetime,
        duration_seconds: float,
        details: dict[str, typing.Any] | None = None,
    ) -> dict[str, typing.Any]:
        local_start = started_at.astimezone(LOCAL_TZ)
        local_finish = finished_at.astimezone(LOCAL_TZ)
        day = local_start.date().isoformat()
        entry = {
            "startedAt": local_start.isoformat(),
            "finishedAt": local_finish.isoformat(),
            "status": status,
            "durationSeconds": round(max(0.0, duration_seconds), 3),
            **(details or {}),
        }

        def merge(previous):
            entries = list(previous.get("entries") or [])
            entries.append(entry)
            successes = sum(item.get("status") == "success" for item in entries)
            skipped = sum(item.get("status") == "skipped" for item in entries)
            failures = sum(item.get("status") == "failed" for item in entries)
            durations = [
                float(item.get("durationSeconds") or 0)
                for item in entries
                if item.get("status") == "success"
            ]
            return {
                "schemaVersion": 1,
                "date": day,
                "timezone": str(LOCAL_TZ),
                "updatedAt": local_finish.isoformat(),
                "summary": {
                    "attempts": len(entries),
                    "successes": successes,
                    "skipped": skipped,
                    "failures": failures,
                    "averageDurationSeconds": round(sum(durations) / len(durations), 3) if durations else 0,
                    "maximumDurationSeconds": round(max(durations), 3) if durations else 0,
                },
                "entries": entries,
            }

        return self.store.update_blob(self.path(day), merge)
