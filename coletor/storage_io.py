"""Infraestrutura de I/O em disco (atômico com GZIP) e sincronização com o Firebase Storage / GCS."""

from __future__ import annotations

import datetime
import gzip
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import typing
import unicodedata
from zoneinfo import ZoneInfo

from .equipes import normalize_team_key

LOCAL_TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))
logger = logging.getLogger(__name__)


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


def _safe_team_key(value: str) -> str:
    key = normalize_team_key(value)
    if not re.fullmatch(r"E[A-Z0-9]{3,7}", key):
        raise ValueError("Codigo de equipe invalido")
    return key


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
        self.cycle_read_operations = 0
        self.cycle_write_operations = 0

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

    def reset_cycle_metrics(self) -> dict[str, int]:
        """Retorna e zera métricas reais de acesso ao Storage no ciclo atual."""
        with self._lock:
            metrics = {
                "bytesUploaded": self.cycle_bytes_uploaded,
                "bytesDownloaded": self.cycle_bytes_downloaded,
                "readOperations": self.cycle_read_operations,
                "writeOperations": self.cycle_write_operations,
            }
            self.cycle_bytes_uploaded = 0
            self.cycle_bytes_downloaded = 0
            self.cycle_read_operations = 0
            self.cycle_write_operations = 0
            return metrics

    def _count_read(self) -> None:
        with self._lock:
            self.cycle_read_operations += 1

    def _count_write(self) -> None:
        with self._lock:
            self.cycle_write_operations += 1

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
                self._count_read()
                try:
                    payload = blob.download_as_bytes()
                except TypeError:
                    payload = blob.download_as_string()
                self.bytes_downloaded += len(payload)
                self.cycle_bytes_downloaded += len(payload)
                return _decode_json_object(payload)
            except Exception as exc:
                status_code = getattr(exc, "code", None)
                if status_code == 404:
                    return {}
                logger.warning("Falha ao ler snapshot ROTALOG do GCS (%s): %s", self.blob_name, exc)
                return {}

    def load_blob(self, blob_name: str) -> dict[str, typing.Any]:
        with self._lock:
            try:
                blob = self._blob_named(blob_name)
                self._count_read()
                try:
                    payload = blob.download_as_bytes()
                except TypeError:
                    payload = blob.download_as_string()
                self.bytes_downloaded += len(payload)
                self.cycle_bytes_downloaded += len(payload)
                return _decode_json_object(payload)
            except Exception as exc:
                status_code = getattr(exc, "code", None)
                if status_code == 404:
                    return {}
                logger.warning("Falha ao ler blob ROTALOG do GCS (%s): %s", blob_name, exc)
                return {}

    def save(self, payload: dict[str, typing.Any]) -> None:
        self.save_blob(self.blob_name, payload)

    def save_blob(self, blob_name: str, payload: dict[str, typing.Any]) -> None:
        with self._lock:
            blob = self._blob_named(blob_name)
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                default=_json_cache_default,
            ).encode("utf-8")
            compressed = gzip.compress(raw)
            self._count_write()
            blob.upload_from_string(
                compressed,
                content_type="application/json",
            )
            self.bytes_uploaded += len(compressed)
            self.cycle_bytes_uploaded += len(compressed)

    def update_blob(
        self,
        blob_name: str,
        mutator: typing.Callable[[dict[str, typing.Any]], dict[str, typing.Any]],
    ) -> dict[str, typing.Any]:
        with self._lock:
            blob = self._blob_named(blob_name)
            for attempt in range(5):
                try:
                    self._count_read()
                    try:
                        blob.reload()
                        generation = blob.generation
                        try:
                            payload = blob.download_as_bytes()
                        except TypeError:
                            payload = blob.download_as_string()
                        self.bytes_downloaded += len(payload)
                        self.cycle_bytes_downloaded += len(payload)
                        current = _decode_json_object(payload)
                    except Exception as exc:
                        if getattr(exc, "code", None) == 404:
                            generation = 0
                            current = {}
                        else:
                            raise

                    merged = mutator(current)
                    raw = json.dumps(
                        merged,
                        ensure_ascii=False,
                        indent=2,
                        default=_json_cache_default,
                    ).encode("utf-8")
                    compressed = gzip.compress(raw)
                    self._count_write()
                    blob.upload_from_string(
                        compressed,
                        content_type="application/json",
                        if_generation_match=generation,
                    )
                    self.bytes_uploaded += len(compressed)
                    self.cycle_bytes_uploaded += len(compressed)
                    return merged
                except Exception as exc:
                    if getattr(exc, "code", None) != 412:
                        raise
                    time.sleep(0.1 * (2 ** attempt))
            raise RuntimeError(f"Conflito concorrente persistente no blob {blob_name}")


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
