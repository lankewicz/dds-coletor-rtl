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
import uuid
from zoneinfo import ZoneInfo

from .parser import _eh_protocolo_valido, formatar_protocolo_copel

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


def normalize_team_key(team_code: str) -> str:
    """Normaliza o código da equipe como chave única (ex: 'E3733' -> 'E3733')."""
    return re.sub(r"[^A-Z0-9_-]+", "", str(team_code or "").strip().upper())


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
    return {
        "empresa": empresa,
        "equipe": eq.get("equipe_codigo") or team_key,
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
    """Retorna o diff de primeiro nível entre o snapshot anterior e o atual."""
    if previous is None:
        return {
            field: {"anterior": None, "novo": current.get(field)}
            for field in TRACKED_FIELDS
            if current.get(field) is not None
        }

    changes: dict[str, dict[str, typing.Any]] = {}
    for field in TRACKED_FIELDS:
        old_value = previous.get(field)
        new_value = current.get(field)
        if _operational_value(old_value) != _operational_value(new_value):
            changes[field] = {"anterior": old_value, "novo": new_value}
    return changes


def _json_cache_default(value: typing.Any) -> typing.Any:
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _decode_json_object(payload: bytes) -> dict[str, typing.Any]:
    raw = gzip.decompress(payload) if payload.startswith(b"\x1f\x8b") else payload
    loaded = json.loads(raw.decode("utf-8-sig"))
    return loaded if isinstance(loaded, dict) else {}


def load_json(path: Path, fallback: typing.Any) -> typing.Any:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
            return value if isinstance(value, type(fallback)) else fallback
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def write_json(path: Path, value: typing.Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
    temporary.replace(path)


# ---------------------------------------------------------------------------
# Daily Document Merging
# ---------------------------------------------------------------------------

def _safe_team_key(value: str) -> str:
    key = str(value or "").strip().upper()
    if not re.fullmatch(r"E[A-Z0-9]{3,7}", key):
        raise ValueError("Codigo de equipe invalido")
    return key


def _iso_local(value: typing.Any, day: str | None = None) -> str | None:
    if value in (None, "", "-"):
        return None
    raw = str(value).strip()
    if len(raw) == 5 and raw[2] == ":" and day:
        try:
            return datetime.datetime.fromisoformat(f"{day}T{raw}:00").replace(tzinfo=LOCAL_TZ).isoformat()
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
            service.get("sequencia"),
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
    start = _iso_local(interval.get("inicioIso"), day)
    if not start and interval.get("inicio_ms"):
        start = _iso_local(datetime.datetime.fromtimestamp(int(interval["inicio_ms"]) / 1000, datetime.timezone.utc).isoformat(), day)
    if not start:
        return None
    end = _iso_local(interval.get("fimIso"), day)
    if not end and interval.get("fim_ms"):
        end = _iso_local(datetime.datetime.fromtimestamp(int(interval["fim_ms"]) / 1000, datetime.timezone.utc).isoformat(), day)
    return {"inicio": start, "fim": end}


def _merge_service_records(team_key: str, records: list[dict[str, typing.Any]]) -> dict[str, dict[str, typing.Any]]:
    result = {}
    keys = {}
    for incoming in records:
        item = dict(incoming)
        sid = _service_id(team_key, item)
        start = item.get("inicioDeslocamento") or item.get("inicioExecucao")
        prot = str(item.get("protocolo") or "").strip()
        key = (team_key, start, prot) if start and _eh_protocolo_valido(prot) else None
        target = keys.get(key, sid) if key else sid
        old = result.get(target, {})
        old_prot = str(old.get("protocolo") or "").strip()
        old_start = old.get("inicioDeslocamento") or old.get("inicioExecucao")
        if old and ((old_prot and prot and old_prot != prot) or
                    (old_start and start and old_start != start)):
            suffix = hashlib.sha256(repr((sid, start, prot)).encode()).hexdigest()[:24]
            target = f"{team_key}_{suffix}"
            old = result.get(target, {})
        merged = dict(old)
        old_time = old.get("observadoEm")
        new_time = item.get("observadoEm")
        stale = bool(old_time and new_time and
                     datetime.datetime.fromisoformat(new_time) < datetime.datetime.fromisoformat(old_time))
        for field, value in item.items():
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
    records = [dict(s) for s in previous.get("services", [])]
    for field in ("ssExecutadas", "ssEmAndamento", "services"):
        for raw in current.get(field) or []:
            item = compact_service(team_key, day, raw)
            item["observadoEm"] = current.get("updatedAtIso")
            for meta in ("fonteProtocolo", "validacaoProtocolo", "protocoloBruto"):
                if raw.get(meta) is not None:
                    item[meta] = raw[meta]
            if item.get("statusAtual") == "CONCLUSAO" and "filaNaConclusao" not in item:
                item["filaNaConclusao"] = {
                    "emergencia": int(current.get("ssPendentesEmergenciaCount") or 0),
                    "comercial": int(current.get("ssPendentesComercialCount") or 0),
                }
            records.append(item)
    service_map = _merge_service_records(team_key, records)

    interval_map = {
        str(item.get("inicio")): dict(item)
        for item in ((previous.get("turno") or {}).get("intervalos") or [])
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

    prev_version = int((previous.get("current") or {}).get("version") or 0)
    prev_date = str(previous.get("date") or "")
    daily_version = 1 if prev_date != day else (prev_version + 1 if previous else 1)

    return {
        "schemaVersion": 1,
        "teamKey": team_key,
        "date": day,
        "updatedAt": current.get("updatedAtIso"),
        "timezone": str(LOCAL_TZ),
        "current": {
            "version": daily_version,
            "statusConexao": current.get("statusConexao"),
            "isOnline": current.get("isOnline"),
            "veiculo": current.get("veiculo"),
            "identificadorEquipamento": current.get("identificadorEquipamento"),
            "origemResolucaoEquipe": current.get("origemResolucaoEquipe"),
            "colaborador": current.get("colaborador"),
            "estadoConsolidado": current.get("estadoConsolidado"),
            "atividadeAtual": current_service,
        },
        "turno": {
            "inicio": _iso_local(turno.get("inicio_iso") or turno.get("inicioIso"), day),
            "fim": _iso_local(turno.get("fim_iso") or turno.get("fimIso"), day),
            "intervalos": sorted(interval_map.values(), key=lambda item: str(item.get("inicio") or "")),
        },
        "services": services,
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
        client_factory: typing.Callable[[], typing.Any] | None = None,
    ):
        self.bucket_name = bucket_name.strip()
        self.blob_name = blob_name.strip().lstrip("/")
        self._client_factory = client_factory
        self._client = None
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return bool(self.bucket_name and self.blob_name)

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
            blob.upload_from_string(
                gzip.compress(raw, compresslevel=6),
                content_type="application/json",
            )

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
                blob.upload_from_string(
                    gzip.compress(raw),
                    content_type="application/json",
                    if_generation_match=generation,
                )
                return merged
            except Exception as exc:
                if getattr(exc, "code", None) != 412:
                    raise
        raise RuntimeError("Conflito concorrente persistente no JSON RTL")


class RotalogTeamFileRepository:
    def __init__(self, store: RotalogGcsSnapshotStore, root_prefix: str = "dados/chicoeletro/rotalog/equipes"):
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
    def __init__(self, store: RotalogGcsSnapshotStore, root_prefix: str = "dados/chicoeletro/rotalog/logs"):
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
