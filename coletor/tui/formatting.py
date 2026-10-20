"""Formatação de valores para a TUI, sem desenho ou leitura de sensores."""
from __future__ import annotations

import os
from datetime import datetime
from coletor.fleet_control import parse_iso
from .state import TZ, DAILY_HISTORY_MAX


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(seconds))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}h {mins:02d}m {secs:02d}s"
    if mins:
        return f"{mins}m {secs:02d}s"
    return f"{secs}s"


def _format_bytes(num_bytes: int | float | None) -> str:
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    num = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(num)} B"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} GB"


def _format_health(health: dict) -> tuple[str, ...]:
    """Saúde do dispositivo e telemetria do disco, sem consultar sensores."""
    overall_labels = {
        "healthy": "SAUDAVEL",
        "warning": "ATENCAO",
        "critical": "CRITICO",
    }
    overall = overall_labels.get(str(health.get("overall") or ""), "DESCONHECIDA")
    temperature = health.get("temperatureC")
    disk = health.get("disk") or {}
    storage_mode = (health.get("storage") or {}).get("mode")
    disk_label = "Disco SSD" if storage_mode == "ssd" else "Disco cartao/local" if storage_mode == "card" else "Disco"
    memory = health.get("memory") or {}
    load = health.get("loadAverage") or []
    collector = health.get("collector") or {}

    temp_text = f"{float(temperature):.1f} C" if temperature is not None else "indisponivel"
    disk_used = disk.get("usedPercent")
    disk_text = f"{float(disk_used):.1f}% usado" if disk_used is not None else "indisponivel"
    memory_available = memory.get("availablePercent")
    memory_text = (
        f"{float(memory_available):.1f}% livre"
        if memory_available is not None
        else "indisponivel"
    )
    uptime_text = _format_duration(health.get("uptimeSeconds"))
    cpu_count = health.get("cpuCount") or os.cpu_count() or 1
    try:
        cpu_count = max(1, int(cpu_count))
    except (TypeError, ValueError):
        cpu_count = 1
    load_percent = [float(value) / cpu_count * 100 for value in load[:3]]
    load_text = (
        "/".join(f"{value:.1f}%" for value in load_percent)
        if load_percent
        else "indisponivel"
    )
    service = collector.get("serviceActive")
    service_text = "ATIVO" if service is True else "INATIVO" if service is False else "DESCONHECIDO"
    last_upload_value = collector.get("lastIndexUploadAt")
    last_upload_dt = parse_iso(str(last_upload_value)) if last_upload_value else None
    last_upload = (
        last_upload_dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
        if last_upload_dt
        else "-"
    )
    issues = health.get("issues") or []
    issue_text = ", ".join(str(item.get("code") or item) for item in issues) or "nenhum"
    heartbeat_dt = parse_iso(health.get("_heartbeatAt"))
    heartbeat_at = heartbeat_dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S") if heartbeat_dt else "-"

    return (
        f"Estado: {overall}  |  CPU: {temp_text}  |  {disk_label}: {disk_text}  |  Memoria: {memory_text}",
        f"Coletor: {service_text}  |  Uptime: {uptime_text}  |  Carga: {load_text}  |  Ultimo envio: {last_upload}",
        f"Heartbeat local: {heartbeat_at}  |  Alertas: {issue_text}",
        *_format_storage(health),
    )


def _format_storage(health: dict) -> tuple[str, ...]:
    storage = health.get("storage") or {}
    disk = health.get("disk") or {}
    mode = {"ssd": "SSD", "card": "CARTAO / DIRETORIO LOCAL"}.get(storage.get("mode"), "DESCONHECIDO")
    free = _format_bytes(disk["freeBytes"]) if disk.get("freeBytes") is not None else "indisponivel"
    total = _format_bytes(disk["totalBytes"]) if disk.get("totalBytes") is not None else "indisponivel"
    lines = [f"Dados: {mode}  |  Livre: {free} / {total}  |  {storage.get('data_dir') or disk.get('path') or '-'}"]
    if storage.get("mode") == "ssd":
        lines.append(f"SSD: {disk.get('model') or '-'}  |  {disk.get('device') or storage.get('device') or '-'}  |  UUID: {storage.get('uuid') or '-'}")
        smart = disk.get("smart") or {}
        labels = {"passed": "OK", "warning": "ATENCAO", "failing": "FALHA", "unavailable": "INDISPONIVEL"}
        status = labels.get(smart.get("status"), "AGUARDANDO HEARTBEAT")
        details = [f"SMART: {status}"]
        if smart.get("reason"):
            details.append(smart["reason"])
        if smart.get("temperatureC") is not None:
            details.append(f"SSD: {smart['temperatureC']} C")
        if smart.get("powerOnHours") is not None:
            details.append(f"Ligado: {smart['powerOnHours']} h")
        if smart.get("wearUsedPercent") is not None:
            details.append(f"Desgaste NVMe: {smart['wearUsedPercent']}%")
        lines.append("  |  ".join(details))
        counters = [("reallocatedSectors", "Realocados"), ("pendingSectors", "Pendentes"),
                    ("uncorrectableSectors", "Incorretiveis"), ("crcErrors", "Erros CRC"),
                    ("mediaErrors", "Erros de midia"), ("powerCycles", "Ciclos de energia")]
        available = [f"{label}: {smart[key]}" for key, label in counters if smart.get(key) is not None]
        if available:
            lines.append("  |  ".join(available))
    else:
        lines.append(f"Selecao: {storage.get('reason') or '-'}")
    return tuple(lines)


def _entry_day(entry: dict) -> str | None:
    for field in ("finishedAt", "startedAt"):
        value = entry.get(field)
        if isinstance(value, str) and len(value) >= 10:
            try:
                return datetime.fromisoformat(value[:10]).date().isoformat()
            except ValueError:
                pass
    return None


def _today_entries(entries: list[dict], today: str | None = None) -> list[dict]:
    """Mantém no painel apenas os ciclos do dia corrente."""
    day = today or datetime.now(TZ).date().isoformat()
    return [entry for entry in entries if _entry_day(entry) == day][-DAILY_HISTORY_MAX:]


def _format_columns(items: list[str], width: int, col_width: int = 35) -> list[str]:
    """Formata lista de strings em colunas de tamanho fixo separadas por ' | '."""
    if not items:
        return []
    sep = " | "
    num_cols = max(1, (width - 1) // (col_width + len(sep)))
    lines = []
    for i in range(0, len(items), num_cols):
        chunk = items[i : i + num_cols]
        formatted_row = sep.join(item.ljust(col_width)[:col_width] for item in chunk)
        lines.append(formatted_row)
    return lines
