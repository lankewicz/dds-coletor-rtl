"""Seleção explícita de armazenamento local, sem montar ou formatar discos."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zlib

from .storage_io import company_key

_ACTIVE_SELECTION = None


class StorageUnavailable(OSError):
    pass


@dataclass(frozen=True)
class MountedDisk:
    uuid: str
    source: str
    mount: str
    filesystem: str
    readonly: bool = False


@dataclass(frozen=True)
class StorageSelection:
    mode: str
    data_dir: str
    company: str
    uuid: str | None = None
    mount: str | None = None
    device: str | None = None
    reason: str = "configured-directory"

    def payload(self):
        return asdict(self)


def mounted_disk(mount: Path) -> MountedDisk | None:
    """Uma pasta existente não prova que o SSD está montado."""
    mount = mount.absolute()
    if not sys.platform.startswith("linux") or not os.path.ismount(mount):
        return None
    try:
        result = subprocess.run(
            ["findmnt", "--json", "--mountpoint", str(mount),
             "--output", "TARGET,SOURCE,FSTYPE,UUID,OPTIONS"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        entries = json.loads(result.stdout)["filesystems"]
        if len(entries) != 1:
            raise ValueError("Montagem ambígua")
        entry = entries[0]
        if Path(entry["target"]).absolute() != mount or not entry.get("uuid"):
            raise ValueError("Montagem sem UUID")
        return MountedDisk(entry["uuid"], entry["source"], str(mount), entry["fstype"],
                           "ro" in (entry.get("options") or "").split(","))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise StorageUnavailable("Não foi possível validar a montagem/UUID") from exc


def read_marker(mount: Path, disk: MountedDisk, company: str) -> Path:
    expected = mount / "dds-coletor-rtl" / company_key(company) / "dados-local"
    marker = json.loads((expected.parent / ".dds-storage.json").read_text(encoding="utf-8"))
    if (marker.get("schemaVersion") != 1 or marker.get("uuid") != disk.uuid
            or marker.get("company") != company or marker.get("dataPath") != expected.relative_to(mount).as_posix()):
        raise StorageUnavailable("Disco não cadastrado para esta empresa/UUID")
    if expected.is_symlink() or not expected.is_dir() or not expected.resolve().is_relative_to(mount.resolve()):
        raise StorageUnavailable("Diretório de dados inválido")
    return expected


def validate_company(root: Path, company: str):
    index = root / "rotalog/equipes/current/index.json.gz"
    plain = index.with_suffix("")
    candidate = index if index.exists() else plain
    if candidate.exists():
        raw = candidate.read_bytes()
        data = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
        if not isinstance(data, dict) or data.get("company") != company:
            raise StorageUnavailable("Dados de outra empresa ou índice inválido")


def last_collection(root: Path):
    from datetime import datetime
    path = root / "rotalog/equipes/current/index.json.gz"
    if not path.exists():
        path = path.with_suffix("")
    if not path.exists():
        return None
    raw = path.read_bytes()
    data = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
    value = data.get("lastCollectedAt") or data.get("updatedAtIso")
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise StorageUnavailable("Checkpoint sem fuso: conferir atualidade antes de trocar o disco")
    return dt


def assert_available(selection: StorageSelection):
    if selection.mode != "ssd":
        return
    disk = mounted_disk(Path(selection.mount))
    if disk is None or disk.uuid != selection.uuid or disk.readonly:
        raise StorageUnavailable("SSD retirado, substituído ou somente leitura")
    if read_marker(Path(selection.mount), disk, selection.company) != Path(selection.data_dir):
        raise StorageUnavailable("Destino do SSD mudou")


def activate_storage(selection):
    global _ACTIVE_SELECTION
    _ACTIVE_SELECTION = selection


def guard_write(path):
    selection = _ACTIVE_SELECTION
    if (selection is not None and selection.mode == "ssd"
            and Path(path).absolute().is_relative_to(Path(selection.data_dir))):
        assert_available(selection)


def select_storage(card: Path, company: str, *, mode="card", mount=Path("/mnt/ssd"),
                   allowed_uuids=(), writable=True) -> StorageSelection:
    card = card.resolve()
    if mode not in ("card", "auto"):
        raise ValueError("DDS_STORAGE_MODE deve ser card ou auto")
    if mode == "card":
        return StorageSelection("card", str(card), company)
    try:
        disk = mounted_disk(mount)
        if disk is None:
            return StorageSelection("card", str(card), company, reason="ssd-not-mounted")
        if disk.readonly or (allowed_uuids and disk.uuid not in allowed_uuids):
            raise StorageUnavailable("UUID não autorizado ou disco somente leitura")
        target = read_marker(mount, disk, company)
        validate_company(target, company)
        validate_company(card, company)
        card_time, ssd_time = last_collection(card), last_collection(target)
        if card_time and (ssd_time is None or card_time > ssd_time):
            return StorageSelection("card", str(card), company, reason="card-newer:reconcile-required")
        if writable:
            # Prova de escrita apenas na seleção, nunca a cada consulta da API.
            with tempfile.TemporaryFile(dir=target) as probe:
                probe.write(b"dds-storage-probe")
                probe.flush()
                os.fsync(probe.fileno())
        result = StorageSelection("ssd", str(target), company, disk.uuid, disk.mount, disk.source,
                                  "registered-ssd")
        assert_available(result)
        return result
    except (OSError, ValueError, KeyError, EOFError, zlib.error) as exc:
        return StorageSelection("card", str(card), company, reason="ssd-unusable:" + type(exc).__name__)


def resolve_storage(requested: Path, company: str, *, project_root: Path, writable=True):
    """Diretórios de teste explícitos continuam independentes do disco de produção."""
    from dotenv import dotenv_values
    settings = {**dotenv_values(project_root / ".storage.env"), **os.environ}
    card = Path(settings.get("DDS_CARD_DATA_DIR") or str(project_root / "dados-local")).resolve()
    requested = requested.resolve()
    mode = (settings.get("DDS_STORAGE_MODE") or "card").lower().strip()
    if requested != card:
        return StorageSelection("card", str(requested), company, reason="explicit-directory")
    uuids = tuple(value.strip() for value in (settings.get("DDS_STORAGE_UUIDS") or "").split(",") if value.strip())
    return select_storage(card, company, mode=mode,
                          mount=Path(settings.get("DDS_SSD_MOUNT") or "/mnt/ssd"),
                          allowed_uuids=uuids, writable=writable)


def tree_digest(root: Path):
    import hashlib
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise StorageUnavailable("Dados com symlink não podem ser migrados automaticamente")
        if path.is_file() and not path.name.endswith((".tmp", ".lock")):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result[str(path.relative_to(root))] = digest.hexdigest()
    return result


def prepare_ssd(card: Path, mount: Path, company: str):
    """Migração inicial com cópia verificada; origem e disco existente preservados."""
    disk = mounted_disk(mount)
    if disk is None or disk.readonly:
        raise StorageUnavailable("SSD não está montado para escrita")
    card, mount = card.resolve(), mount.resolve()
    if card.is_relative_to(mount) or mount.is_relative_to(card) or not card.is_dir():
        raise StorageUnavailable("Origem e montagem inválidas ou sobrepostas")
    validate_company(card, company)
    target = mount / "dds-coletor-rtl" / company_key(company) / "dados-local"
    marker_path = target.parent / ".dds-storage.json"
    if target.exists() or marker_path.exists():
        raise StorageUnavailable("SSD já preparado: não sobrescrever; conferir dados antes de reutilizar")
    expected = tree_digest(card)
    required = sum((card / name).stat().st_size for name in expected)
    if shutil.disk_usage(mount).free < required + 32 * 1024 * 1024:
        raise StorageUnavailable("Espaço insuficiente no SSD")
    if not target.parent.resolve().is_relative_to(mount):
        raise StorageUnavailable("Destino fora da montagem")
    if not expected:
        raise StorageUnavailable("Origem vazia: restaurar os dados antes de preparar o SSD")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".dds-migration-", dir=target.parent))
    # Uma cópia interrompida permanece para diagnóstico; não apaga dados existentes.
    for name in expected:
        assert_disk = mounted_disk(mount)
        if assert_disk is None or assert_disk.uuid != disk.uuid or assert_disk.readonly:
            raise StorageUnavailable("SSD mudou durante a migração")
        destination = staging / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(card / name, destination)
        with destination.open("r+b") as stream:
            os.fsync(stream.fileno())
    if tree_digest(card) != expected or tree_digest(staging) != expected:
        raise StorageUnavailable("Dados mudaram ou cópia divergente; interromper migração")
    current = mounted_disk(mount)
    if current is None or current.uuid != disk.uuid:
        raise StorageUnavailable("SSD mudou antes da publicação")
    staging.rename(target)
    payload = {"schemaVersion": 1, "uuid": disk.uuid, "company": company,
               "dataPath": target.relative_to(mount).as_posix()}
    with marker_path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return StorageSelection("ssd", str(target), company, disk.uuid, disk.mount, disk.source,
                            "migration-verified")
