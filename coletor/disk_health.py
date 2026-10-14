"""Telemetria somente de leitura do armazenamento selecionado."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

from .data_storage import StorageSelection, assert_available


def parse_smart(payload: dict, exit_status: int) -> dict:
    """Códigos SMART são bits: um retorno não zero pode conter dados válidos."""
    passed = (payload.get("smart_status") or {}).get("passed")
    status = "unavailable"
    if passed is False or exit_status & 24:
        status = "failing"
    elif passed is True and not exit_status & 7:
        status = "warning" if exit_status & 224 else "passed"
    result = {"status": status, "exitStatus": exit_status}
    if status == "unavailable":
        result["reason"] = "leitura indisponivel: permissao, adaptador ou suporte SMART"
    for field, value in (
        ("model", payload.get("model_name")),
        ("temperatureC", (payload.get("temperature") or {}).get("current")),
        ("powerOnHours", (payload.get("power_on_time") or {}).get("hours")),
        ("powerCycles", payload.get("power_cycle_count")),
    ):
        if value is not None:
            result[field] = value
    attrs = {item.get("id"): item for item in
             (payload.get("ata_smart_attributes") or {}).get("table", [])}
    for attr_id, field in ((5, "reallocatedSectors"), (197, "pendingSectors"),
                           (198, "uncorrectableSectors"), (199, "crcErrors")):
        raw = (attrs.get(attr_id, {}).get("raw") or {}).get("value")
        if isinstance(raw, (int, float)):
            result[field] = raw
    nvme = payload.get("nvme_smart_health_information_log") or {}
    # Não inferir vida útil a partir de atributos ATA específicos do fabricante.
    for source, field in (("percentage_used", "wearUsedPercent"),
                          ("media_errors", "mediaErrors")):
        if nvme.get(source) is not None:
            result[field] = nvme[source]
    if nvme.get("critical_warning"):
        result["status"] = "failing"
    if result["status"] == "passed" and any(result.get(key, 0) > 0 for key in
            ("reallocatedSectors", "pendingSectors", "uncorrectableSectors", "crcErrors", "mediaErrors")):
        result["status"] = "warning"
    if result.get("wearUsedPercent", 0) >= 100 and result["status"] == "passed":
        result["status"] = "warning"
    return result


def read_smart(device: str) -> dict:
    executable = shutil.which("smartctl")
    if not executable:
        return {"status": "unavailable", "reason": "smartctl nao instalado"}
    command = [executable, "-j", "-a"]
    device_type = os.getenv("DDS_SMART_DEVICE_TYPE", "").strip()
    if device_type:
        if device_type not in ("auto", "sat", "sat,12", "sat,16", "scsi", "nvme"):
            return {"status": "unavailable", "reason": "DDS_SMART_DEVICE_TYPE invalido"}
        command += ["-d", device_type]
    try:
        result = subprocess.run(command + [device], capture_output=True, text=True,
                                timeout=8, check=False)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or result.returncode < 0:
            raise ValueError("Resposta SMART invalida")
        return parse_smart(payload, result.returncode)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unavailable", "reason": "leitura SMART falhou ou excedeu 8s"}


def collect_disk_health(storage: StorageSelection) -> dict:
    result = {"path": storage.data_dir, "checkedAt": datetime.now(timezone.utc).isoformat()}
    try:
        # Impede medir o cartão sob /mnt/ssd quando a unidade foi retirada.
        assert_available(storage)
        usage = shutil.disk_usage(storage.data_dir)
        result.update(totalBytes=usage.total, freeBytes=usage.free, usedBytes=usage.used,
                      usedPercent=round(usage.used / usage.total * 100, 1) if usage.total else None)
    except OSError:
        result["error"] = "storage_unavailable"
        return result
    if storage.mode == "ssd":
        source = Path(storage.device).resolve()
        block = Path("/sys/class/block") / source.name
        # O nome /dev/sda pode mudar. Derivar o disco da montagem atual.
        if (block / "partition").exists():
            device = "/dev/" + block.resolve().parent.name
        else:
            device = str(source)
        result["device"] = device
        try:
            result["model"] = (Path("/sys/class/block") / Path(device).name /
                               "device/model").read_text().strip()
        except OSError:
            pass
        result["smart"] = read_smart(device)
        if result["smart"].get("model"):
            result["model"] = result["smart"]["model"]
    return result
