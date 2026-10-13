"""Entradas persistentes da torre; falha remota nunca vira cadastro/histórico vazio."""
from __future__ import annotations

import gzip
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from coletor.file_lock import exclusive_file_lock
from coletor.storage_io import LOCAL_TZ, company_key, load_json_with_status, write_json
from torre_contract import timestamp


def source_prefix(company, prefix=None):
    key = company_key(company)
    if key == "default":
        raise ValueError("Empresa obrigatória")
    value = (prefix or "dados/{empresa}/dds/controle").replace("{empresa}", key).strip("/")
    if key not in value.split("/") or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("Prefixo DDS deve isolar a empresa")
    return value


def validate_team_map(teams):
    if not isinstance(teams, dict):
        raise ValueError("Mapa de equipes ausente")
    for key, entry in teams.items():
        if (not isinstance(key, str) or not key.strip() or key != key.strip().upper()
                or not isinstance(entry, dict)):
            raise ValueError("Equipe inválida")
    return teams


def validate_catalog(data, company):
    if (not isinstance(data, dict) or data.get("company") != company
            or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1):
        raise ValueError("Cadastro de outra empresa ou versão inválida")
    timestamp(data.get("generatedAt"))
    for key, team in validate_team_map(data.get("teams")).items():
        if team.get("teamKey") != key or type(team.get("active")) is not bool:
            raise ValueError("Identidade/atividade do cadastro inválida")
        aliases = team.get("aliases")
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
            raise ValueError("Aliases do cadastro ausentes")
    return data


def validate_daily(data, company, day):
    if (not isinstance(data, dict) or data.get("company") != company
            or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1
            or data.get("date") != day):
        raise ValueError("Projeção DDS de outra empresa/data ou versão inválida")
    date.fromisoformat(day)
    timestamp(data.get("generatedAt"))
    for entry in validate_team_map(data.get("teams")).values():
        timestamp(entry.get("completedAt"))
    return data


class GcsInputs:
    """Leitura com erros explícitos e versão fixada durante o download."""
    def __init__(self, client, bucket):
        self.bucket = client.bucket(bucket)
        self.source_id = "gcs:" + bucket

    def listing(self, prefix):
        return [blob.name for blob in self.bucket.list_blobs(prefix=prefix, timeout=15, retry=None)]

    def fetch(self, name, cached=None):
        blob = self.bucket.blob(name)
        blob.reload(timeout=15, retry=None)
        revision = str(blob.generation)
        if cached and cached.get("revision") == revision:
            return cached
        raw = blob.download_as_bytes(if_generation_match=int(revision), timeout=15, retry=None)
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Fonte não é objeto JSON")
        return {"revision": revision, "data": value}


class TorreInputsSync:
    def __init__(self, root, company, remote, *, prefix=None, interval=300, days=25, catalog_max_age=86400):
        if interval < 30 or not 1 <= days <= 90 or catalog_max_age < interval:
            raise ValueError("Intervalo/janela inválidos")
        self.company, self.remote = company, remote
        self.source_id = getattr(remote, "source_id", "custom")
        self.prefix = source_prefix(company, prefix)
        self.interval, self.days = interval, days
        self.catalog_max_age = catalog_max_age
        self.path = Path(root) / "torre" / "inputs" / company_key(company) / "current.json.gz"
        self.status_path = self.path.with_name("status.json")

    def sync(self, now=None, force=False):
        now = now or datetime.now(LOCAL_TZ)
        if now.tzinfo is None:
            raise ValueError("Relógio deve conter fuso")
        with exclusive_file_lock(self.path.with_name("sync.lock")):
            return self._sync(now, force)

    def _sync(self, now, force):
        cached, cache_state = load_json_with_status(self.path, {})
        status, _ = load_json_with_status(self.status_path, {})
        # Não reutilizar um bundle pertencente a outra empresa/prefixo.
        if cached and (cached.get("company") != self.company or cached.get("prefix") != self.prefix):
            raise ValueError("Cache de outra empresa/fonte")
        if cached and cached.get("remoteSource") != self.source_id:
            cached, cache_state = {}, "corrupt"
        if cache_state == "corrupt":
            cached = {}
        if cached:
            try:
                if cached.get("schemaVersion") != 1 or not isinstance(cached.get("daily"), dict):
                    raise ValueError("Cache inválido")
                timestamp(cached.get("syncedAt"))
                validate_catalog(cached["catalog"]["data"], self.company)
                for day, entry in cached["daily"].items():
                    validate_daily(entry["data"], self.company, day)
            except (ValueError, KeyError, TypeError, AttributeError):
                cached, cache_state = {}, "corrupt"
        try:
            elapsed = (now - timestamp(status.get("attemptedAt"))).total_seconds()
        except (ValueError, TypeError):
            elapsed = self.interval
        if (not force and cache_state != "corrupt" and status.get("company") == self.company
                and status.get("prefix") == self.prefix and status.get("remoteSource") == self.source_id
                and (cached or not status.get("available"))
                and 0 <= elapsed < self.interval):
            return {**status, "skipped": True}
        result = {"company": self.company, "prefix": self.prefix, "remoteSource": self.source_id, "attemptedAt": now.isoformat(),
                  "lastSuccessAt": cached.get("syncedAt"), "available": bool(cached), "stale": True}
        try:
            catalog_blob = self.prefix + "/cadastro/current.json.gz"
            catalog = self.remote.fetch(catalog_blob, cached.get("catalog"))
            validate_catalog(catalog["data"], self.company)
            catalog_age = (now - timestamp(catalog["data"]["generatedAt"])).total_seconds()
            if catalog_age < -30 or catalog_age > self.catalog_max_age:
                raise ValueError("Exportação do cadastro desatualizada")
            folder = self.prefix + "/daily/"
            names = self.remote.listing(folder)
            selected = []
            for name in names:
                match = re.fullmatch(re.escape(folder) + r"(\d{4}-\d{2}-\d{2})\.json\.gz", name)
                if match:
                    day = match[1]
                    if date.fromisoformat(day) <= now.date():
                        selected.append((day, name))
            daily = {}
            for day, name in sorted(set(selected))[-self.days:]:
                entry = self.remote.fetch(name, cached.get("daily", {}).get(day))
                validate_daily(entry["data"], self.company, day)
                daily[day] = entry
            bundle = {"schemaVersion": 1, "company": self.company, "prefix": self.prefix,
                      "remoteSource": self.source_id,
                      "syncedAt": now.isoformat(), "catalog": catalog, "daily": daily}
            write_json(self.path, bundle)
            result.update(lastSuccessAt=now.isoformat(), available=True, stale=False)
        except Exception as exc:
            # Apenas classe do erro: mensagens de SDK podem conter dados sensíveis.
            result["errorType"] = type(exc).__name__
        write_json(self.status_path, result)
        return result

    def snapshot(self, now=None):
        """Lê somente disco; calcula atualidade mesmo quando o processo parou."""
        now = now or datetime.now(LOCAL_TZ)
        data, state = load_json_with_status(self.path, {})
        if (state != "ok" or data.get("schemaVersion") != 1 or data.get("company") != self.company
                or data.get("prefix") != self.prefix or data.get("remoteSource") != self.source_id):
            raise ValueError("Entradas locais indisponíveis ou de outra fonte")
        synced_at = timestamp(data.get("syncedAt"))
        validate_catalog(data["catalog"]["data"], self.company)
        if not isinstance(data.get("daily"), dict):
            raise ValueError("Histórico DDS local inválido")
        for day, entry in data["daily"].items():
            validate_daily(entry["data"], self.company, day)
        status, _ = load_json_with_status(self.status_path, {})
        sync_age = (now - synced_at).total_seconds()
        catalog_at = timestamp(data["catalog"]["data"]["generatedAt"])
        catalog_age = (now - catalog_at).total_seconds()
        failed = (status.get("remoteSource") != self.source_id or status.get("stale") is not False
                  or status.get("lastSuccessAt") != data["syncedAt"])
        dds_stale = failed or sync_age < -30 or sync_age > self.interval
        catalog_stale = dds_stale or catalog_age < -30 or catalog_age > self.catalog_max_age
        return {**data, "stale": dds_stale or catalog_stale,
                "sources": {
                    "cadastro": {"updatedAt": catalog_at.isoformat(), "stale": catalog_stale},
                    "dds": {"updatedAt": synced_at.isoformat(), "stale": dds_stale},
                }}
