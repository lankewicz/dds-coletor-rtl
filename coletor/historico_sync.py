"""Sincronização idempotente dos artefatos históricos com o Storage."""

from __future__ import annotations

import datetime
import hashlib
import json
import typing
from pathlib import Path

from .file_lock import exclusive_file_lock
from .storage import load_json, write_json


def stable_payload_digest(payload: dict[str, typing.Any]) -> str:
    def stable(value: typing.Any) -> typing.Any:
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in value.items()
                if key not in {"collectedAt", "updatedAt", "updatedAtIso"}
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    raw = json.dumps(
        stable(payload), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upload_payload_if_changed(
    firebase_store: typing.Any,
    remote_blob: str,
    payload: dict[str, typing.Any],
    receipt_path: Path,
) -> str:
    digest = stable_payload_digest(payload)
    receipt = load_json(receipt_path, {})
    if (
        isinstance(receipt, dict)
        and receipt.get("bucket") == firebase_store.bucket_name
        and receipt.get("blob") == remote_blob
        and receipt.get("sha256") == digest
    ):
        return "unchanged"
    firebase_store.save_blob(remote_blob, payload)
    write_json(receipt_path, {
        "schemaVersion": 1,
        "bucket": firebase_store.bucket_name,
        "blob": remote_blob,
        "sha256": digest,
        "uploadedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    return "uploaded"


def enqueue_historical_upload(
    queue_path: Path, key: str, local_path: Path, remote_blob: str, receipt_path: Path,
) -> None:
    """Registra a pendência em disco antes de qualquer tentativa de envio."""
    with exclusive_file_lock(queue_path.with_name(queue_path.name + ".lock")):
        queue = load_json(queue_path, {})
        items = queue.get("items", {}) if isinstance(queue, dict) else {}
        items[key] = {
            **items.get(key, {}),
            "localPath": str(local_path.resolve()),
            "remoteBlob": remote_blob,
            "receiptPath": str(receipt_path.resolve()),
            "queuedAt": items.get(key, {}).get("queuedAt")
            or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "attempts": int(items.get(key, {}).get("attempts") or 0),
        }
        write_json(queue_path, {"schemaVersion": 1, "items": items})


def flush_historical_uploads(queue_path: Path, firebase_store: typing.Any) -> dict[str, typing.Any]:
    """Envia pendências locais; só as remove depois da confirmação gravada."""
    results: dict[str, str] = {}
    with exclusive_file_lock(queue_path.with_name(queue_path.name + ".lock")):
        queue = load_json(queue_path, {})
        items = queue.get("items", {}) if isinstance(queue, dict) else {}
        for key, item in list(items.items()):
            try:
                local_path = Path(item["localPath"])
                payload = load_json(local_path, {})
                if not payload:
                    raise RuntimeError(f"Arquivo local ausente ou inválido: {local_path}")
                upload_status = upload_payload_if_changed(
                    firebase_store, item["remoteBlob"], payload, Path(item["receiptPath"]),
                )
            except Exception as exc:
                item["attempts"] = int(item.get("attempts") or 0) + 1
                item["lastAttemptAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                item["lastError"] = str(exc)
                results[key] = "pending"
            else:
                del items[key]
                results[key] = upload_status
            write_json(queue_path, {"schemaVersion": 1, "items": items})
    pending_items = list(items.values())
    return {
        "results": results,
        "pending": len(pending_items),
        "totalAttempts": sum(int(item.get("attempts") or 0) for item in pending_items),
        "oldestQueuedAt": min(
            (str(item.get("queuedAt")) for item in pending_items if item.get("queuedAt")),
            default=None,
        ),
        "lastAttemptAt": max(
            (str(item.get("lastAttemptAt")) for item in pending_items if item.get("lastAttemptAt")),
            default=None,
        ),
        "lastError": next(
            (str(item.get("lastError")) for item in reversed(pending_items) if item.get("lastError")),
            None,
        ),
    }
