"""Plano de controle da frota usando objetos pequenos no Firebase Storage/GCS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import socket
import typing

from dotenv import load_dotenv

from .storage import RotalogGcsSnapshotStore, company_key, rotalog_gcs_paths


TERMINAL_RESULT_STATUSES = {"healthy", "already_active", "failed", "rolled_back"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: typing.Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_commit(value: str, *, allow_short: bool = True) -> str:
    commit = str(value or "").strip().lower()
    minimum = 7 if allow_short else 40
    if not (minimum <= len(commit) <= 40) or any(ch not in "0123456789abcdef" for ch in commit):
        qualifier = "entre 7 e 40" if allow_short else "exatamente 40"
        raise ValueError(f"Hash Git inválido; informe {qualifier} caracteres hexadecimais")
    return commit


def default_node_id() -> str:
    configured = os.getenv("FLEET_NODE_ID", "").strip()
    return configured or socket.gethostname().strip().lower()


@dataclass(frozen=True)
class FleetPaths:
    root: str

    @property
    def control(self) -> str:
        return f"{self.root}/control"

    @property
    def commands(self) -> str:
        return f"{self.control}/commands"

    @property
    def nodes(self) -> str:
        return f"{self.control}/nodes"

    def command(self, command_id: str) -> str:
        return f"{self.commands}/{command_id}.json.gz"

    def node(self, node_id: str) -> str:
        return f"{self.nodes}/{node_id}.json.gz"

    def result(self, command_id: str, node_id: str) -> str:
        return f"{self.control}/results/{command_id}/{node_id}.json.gz"


class FleetStore:
    """Acesso mínimo ao plano de controle, compartilhado pela CLI e pelos agentes."""

    def __init__(self, store: RotalogGcsSnapshotStore, paths: FleetPaths):
        self.store = store
        self.paths = paths

    @classmethod
    def from_environment(cls, repo_root: Path | None = None) -> "FleetStore":
        root = (repo_root or Path.cwd()).resolve()
        load_dotenv(root / ".env")
        empresa = os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro")
        resolved = rotalog_gcs_paths(
            empresa,
            root_prefix=os.getenv("ROTALOG_GCS_ROOT_PREFIX"),
            index_blob=os.getenv("ROTALOG_GCS_CACHE_BLOB"),
        )
        bucket = os.getenv("DDS_BUCKET_NAME", "dds-treinamentos.firebasestorage.app")
        credentials = (
            os.getenv("FLEET_GOOGLE_APPLICATION_CREDENTIALS")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            or os.getenv("FIREBASE_CREDENTIALS")
        )
        if not credentials:
            credentials = next(
                (
                    str(candidate)
                    for candidate in (
                        root / "serviceAccountKey.json",
                        root / "firebase_config.json",
                        root / "firebase_credentials.json",
                    )
                    if candidate.is_file()
                ),
                None,
            )
        client_factory = None
        if credentials:
            credential_path = Path(credentials).expanduser().resolve()
            if not credential_path.is_file():
                raise RuntimeError(f"Credencial do Firebase não encontrada: {credential_path}")

            def client_factory():
                from google.cloud import storage

                return storage.Client.from_service_account_json(str(credential_path))

        store = RotalogGcsSnapshotStore(
            bucket,
            resolved["index"],
            root_prefix=resolved["root"],
            client_factory=client_factory,
        )
        return cls(store, FleetPaths(resolved["root"]))

    def load(self, path: str) -> dict[str, typing.Any]:
        return self.store.load_blob(path)

    def save(self, path: str, payload: dict[str, typing.Any]) -> None:
        self.store.save_blob(path, payload)

    def list_payloads(self, prefix: str) -> list[dict[str, typing.Any]]:
        client_blob = self.store._blob_named(prefix + "/.probe")
        bucket = client_blob.bucket
        payloads: list[dict[str, typing.Any]] = []
        for blob in bucket.list_blobs(prefix=prefix.rstrip("/") + "/"):
            try:
                payload = self.load(blob.name)
            except Exception:
                continue
            if payload:
                payloads.append(payload)
        return payloads

    def active_nodes(self, max_age_seconds: int | None = None) -> list[dict[str, typing.Any]]:
        now = datetime.now(timezone.utc)
        if max_age_seconds is None:
            hour = now.astimezone(ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))).hour
            max_age_seconds = 2700 if 7 <= hour < 20 else 9000
        candidates = {node.get("nodeId"): dict(node) for node in self.list_payloads(self.paths.nodes)}
        index = self.load(f"{self.paths.root}/equipes/current/index.json.gz")
        publisher = index.get("publisherNodeId")
        published = parse_iso(index.get("publishedAt"))
        if publisher and published and published <= now:
            node = candidates.setdefault(publisher, {"nodeId": publisher})
            heartbeat = parse_iso(node.get("heartbeatAt"))
            if heartbeat is None or published > heartbeat:
                node.update(heartbeatAt=index["publishedAt"], status="index_uploaded", runningCommit=None)
        nodes = []
        for node in candidates.values():
            heartbeat = parse_iso(node.get("heartbeatAt"))
            if heartbeat is None:
                continue
            age = (now - heartbeat).total_seconds()
            if 0 <= age <= max_age_seconds:
                node = dict(node)
                node["heartbeatAgeSeconds"] = max(0, round(age, 1))
                nodes.append(node)
        return sorted(nodes, key=lambda item: str(item.get("nodeId") or ""))

    @staticmethod
    def command_targets_node(command: dict[str, typing.Any], node_id: str) -> bool:
        target = command.get("target", "all")
        if target == "all":
            return True
        if isinstance(target, list):
            return node_id in {str(item) for item in target}
        return str(target) == node_id

    def pending_commands(self, node_id: str) -> list[dict[str, typing.Any]]:
        commands = []
        now = datetime.now(timezone.utc)
        for command in self.list_payloads(self.paths.commands):
            if not self.command_targets_node(command, node_id):
                continue
            expires = parse_iso(command.get("expiresAt"))
            if expires is not None and expires <= now:
                continue
            command_id = str(command.get("commandId") or "")
            if not command_id:
                continue
            result = self.load(self.paths.result(command_id, node_id))
            if result.get("status") in TERMINAL_RESULT_STATUSES:
                continue
            commands.append(command)
        return sorted(commands, key=lambda item: str(item.get("requestedAt") or ""))


def build_node_payload(
    node_id: str,
    *,
    running_commit: str | None,
    status: str,
    detail: str = "",
    health: dict[str, typing.Any] | None = None,
    last_index_upload_at: str | None = None,
) -> dict[str, typing.Any]:
    payload = {
        "schemaVersion": 1,
        "nodeId": node_id,
        "companyKey": company_key(os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro")),
        "hostname": socket.gethostname(),
        "status": status,
        "detail": detail,
        "runningCommit": running_commit,
        "heartbeatAt": utc_now_iso(),
    }
    if health is not None:
        payload["health"] = health
    if last_index_upload_at:
        payload["lastIndexUploadAt"] = last_index_upload_at
    return payload
