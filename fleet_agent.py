"""Agente one-shot que publica heartbeat e aplica comandos de deploy da frota."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

from coletor.fleet_control import (
    FleetStore,
    build_node_payload,
    default_node_id,
    normalize_commit,
    parse_iso,
    utc_now_iso,
)
from coletor.storage import load_json, write_json
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent


def run(
    *args: str,
    cwd: Path = ROOT,
    check: bool = True,
    timeout: int | None = 300,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"comando retornou {result.returncode}"
        raise RuntimeError(f"{' '.join(args)}: {message}")
    return result


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    return run("git", *args, cwd=cwd, check=check).stdout.strip()


def current_commit() -> str | None:
    result = run("git", "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_clean_tracked_files() -> None:
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise RuntimeError("O repositório do Orange Pi possui alterações rastreadas; deploy recusado")


def resolve_remote_commit(requested: str) -> str:
    requested = normalize_commit(requested)
    remote = os.getenv("FLEET_GIT_REMOTE", "origin")
    git("fetch", remote, "--prune")
    resolved = git("rev-parse", f"{requested}^{{commit}}")
    normalize_commit(resolved, allow_short=False)
    remote_branches = git("branch", "-r", "--contains", resolved)
    if f"{remote}/" not in remote_branches:
        raise RuntimeError(f"Commit {resolved[:12]} não está disponível em uma branch de {remote}")
    return resolved


def python_for_validation() -> str:
    configured = os.getenv("FLEET_PYTHON", "").strip()
    return configured or sys.executable


def validate_release(commit: str) -> None:
    release_parent = Path(os.getenv("FLEET_STAGING_DIR", "/tmp/dds-fleet-staging"))
    release_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{commit[:8]}-", dir=release_parent))
    staging.rmdir()
    worktree_added = False
    try:
        git("worktree", "add", "--detach", str(staging), commit)
        worktree_added = True
        required = ("main.py", "fleet_agent.py", "coletor/fleet_control.py", "requirements.txt")
        missing = [name for name in required if not (staging / name).is_file()]
        if missing:
            raise RuntimeError(
                "A versão não contém o agente de atualização compatível: " + ", ".join(missing)
            )
        python = python_for_validation()
        run(
            python,
            "-m",
            "compileall",
            "-q",
            "main.py",
            "tui.py",
            "fleet.py",
            "fleet_agent.py",
            "coletor",
            cwd=staging,
        )
        requirements = staging / "requirements.txt"
        if requirements.is_file() and os.getenv("FLEET_INSTALL_REQUIREMENTS", "true").lower() in {"1", "true", "yes"}:
            run(python, "-m", "pip", "install", "-r", str(requirements), cwd=staging, timeout=900)
        tests = staging / "tests"
        if tests.is_dir() and os.getenv("FLEET_RUN_TESTS", "true").lower() in {"1", "true", "yes"}:
            run(python, "-m", "unittest", "discover", "-s", "tests", cwd=staging, timeout=900)
    finally:
        if worktree_added:
            git("worktree", "remove", "--force", str(staging), check=False)
        shutil.rmtree(staging, ignore_errors=True)
        git("worktree", "prune", check=False)


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    service = os.getenv("FLEET_SERVICE_NAME", "rotalog.service")
    prefix = [] if os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0 else ["sudo", "-n"]
    return run(*prefix, "systemctl", *args, service, check=check, timeout=90)


def service_is_active() -> bool:
    return systemctl("is-active", check=False).stdout.strip() == "active"


def switch_and_restart(commit: str, previous: str | None) -> tuple[str, str]:
    systemctl("stop")
    try:
        git("checkout", "--detach", commit)
        systemctl("start")
        deadline = time.monotonic() + int(os.getenv("FLEET_HEALTH_TIMEOUT_SECONDS", "45"))
        stable_since: float | None = None
        while time.monotonic() < deadline:
            if service_is_active():
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 10:
                    return "healthy", "Serviço reiniciado e permaneceu ativo"
            else:
                stable_since = None
            time.sleep(2)
        raise RuntimeError("O serviço não permaneceu ativo após a atualização")
    except Exception as exc:
        if previous:
            systemctl("stop", check=False)
            git("checkout", "--detach", previous)
            systemctl("start", check=False)
            if service_is_active():
                return "rolled_back", f"Nova versão falhou; rollback para {previous[:12]}: {exc}"
        raise


def save_result(store: FleetStore, command_id: str, node_id: str, **fields) -> None:
    previous = store.load(store.paths.result(command_id, node_id))
    payload = {
        **previous,
        "schemaVersion": 1,
        "commandId": command_id,
        "nodeId": node_id,
        "updatedAt": utc_now_iso(),
        **fields,
    }
    store.save(store.paths.result(command_id, node_id), payload)


def deploy(store: FleetStore, command: dict, node_id: str) -> None:
    command_id = str(command["commandId"])
    requested = str(command.get("commit") or "")
    previous = current_commit()
    save_result(
        store,
        command_id,
        node_id,
        status="preparing",
        detail="Validando commit, código, testes e dependências",
        requestedCommit=requested,
        previousCommit=previous,
        startedAt=utc_now_iso(),
    )
    ensure_clean_tracked_files()
    commit = resolve_remote_commit(requested)
    if previous == commit:
        save_result(
            store,
            command_id,
            node_id,
            status="already_active",
            detail="A versão solicitada já estava ativa",
            runningCommit=commit,
            completedAt=utc_now_iso(),
        )
        return
    validate_release(commit)
    save_result(
        store,
        command_id,
        node_id,
        status="switching",
        detail="Validação concluída; reiniciando o coletor",
        resolvedCommit=commit,
    )
    status, detail = switch_and_restart(commit, previous)
    save_result(
        store,
        command_id,
        node_id,
        status=status,
        detail=detail,
        runningCommit=current_commit(),
        completedAt=utc_now_iso(),
    )


def heartbeat_interval_seconds(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo")))
    return 1800 if 7 <= local.hour < 20 else 7200


def local_heartbeat_interval_seconds() -> int:
    return max(30, int(os.getenv("FLEET_LOCAL_HEARTBEAT_SECONDS", "120")))


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def evaluate_health(health: dict) -> dict:
    issues = []
    disk = health.get("disk") or {}
    memory = health.get("memory") or {}
    collector = health.get("collector") or {}
    temperature = health.get("temperatureC")

    if disk.get("error"):
        issues.append({"code": "STORAGE_UNAVAILABLE", "severity": "critical"})
    elif disk.get("usedPercent") is not None:
        used = float(disk["usedPercent"])
        if used >= 95:
            issues.append({"code": "DISK_CRITICAL", "severity": "critical", "value": used})
        elif used >= 85:
            issues.append({"code": "DISK_LOW", "severity": "warning", "value": used})

    total = memory.get("totalBytes")
    available = memory.get("availableBytes")
    if total and available is not None:
        available_percent = round((available / total) * 100, 1)
        memory["availablePercent"] = available_percent
        if available_percent <= 5:
            issues.append({"code": "MEMORY_CRITICAL", "severity": "critical", "value": available_percent})
        elif available_percent <= 10:
            issues.append({"code": "MEMORY_LOW", "severity": "warning", "value": available_percent})

    if temperature is not None:
        if temperature >= 85:
            issues.append({"code": "TEMPERATURE_CRITICAL", "severity": "critical", "value": temperature})
        elif temperature >= 75:
            issues.append({"code": "TEMPERATURE_HIGH", "severity": "warning", "value": temperature})

    if collector.get("serviceActive") is False:
        issues.append({"code": "COLLECTOR_INACTIVE", "severity": "critical"})
    elif collector.get("serviceActive") is None:
        issues.append({"code": "COLLECTOR_STATUS_UNKNOWN", "severity": "warning"})
    if health.get("rebootRequired"):
        issues.append({"code": "REBOOT_REQUIRED", "severity": "warning"})

    severities = {item["severity"] for item in issues}
    health["overall"] = "critical" if "critical" in severities else "warning" if issues else "healthy"
    health["issues"] = issues
    return health


def collect_health(data_dir: Path, receipt: dict) -> dict:
    temperatures = []
    thermal_root = Path("/sys/class/thermal")
    if thermal_root.is_dir():
        for path in thermal_root.glob("thermal_zone*/temp"):
            raw = _read_text(path)
            try:
                value = float(raw or "")
            except ValueError:
                continue
            temperatures.append(round(value / 1000 if value > 1000 else value, 1))

    disk = {}
    try:
        usage = shutil.disk_usage(data_dir)
        disk = {
            "freeBytes": usage.free,
            "totalBytes": usage.total,
            "usedPercent": round((usage.used / usage.total) * 100, 1) if usage.total else None,
        }
    except OSError:
        disk = {"error": "storage_unavailable"}

    memory = {}
    meminfo = _read_text(Path("/proc/meminfo"))
    if meminfo:
        values = {}
        for line in meminfo.splitlines():
            key, _, raw = line.partition(":")
            number = raw.strip().split(maxsplit=1)
            if number and number[0].isdigit():
                values[key] = int(number[0]) * 1024
        memory = {"totalBytes": values.get("MemTotal"), "availableBytes": values.get("MemAvailable")}

    uptime = None
    raw_uptime = _read_text(Path("/proc/uptime"))
    if raw_uptime:
        try:
            uptime = int(float(raw_uptime.split()[0]))
        except (IndexError, ValueError):
            pass

    runtime = load_json(data_dir / "rotalog/logs/runtime-status.json", {})
    try:
        collector_active = service_is_active()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        collector_active = None

    health = {
        "disk": disk,
        "memory": memory,
        "loadAverage": [round(value, 2) for value in os.getloadavg()] if hasattr(os, "getloadavg") else [],
        "uptimeSeconds": uptime,
        "rebootRequired": Path("/var/run/reboot-required").exists(),
        "collector": {
            "serviceActive": collector_active,
            "phase": runtime.get("phase"),
            "updatedAt": runtime.get("updatedAt"),
            "lastIndexUploadAt": receipt.get("uploadedAt"),
            "lastIndexSyncStatus": receipt.get("sha256") and "confirmed" or None,
        },
    }
    if temperatures:
        health["temperatureC"] = max(temperatures)
    return evaluate_health(health)


def publish_heartbeat(store, node_id, *, force=False, status="online", detail=""):
    data_dir = Path(os.getenv("FLEET_DATA_DIR", str(ROOT / "dados-local")))
    receipt = load_json(data_dir / "rotalog/equipes/current/firebase-sync.json", {})
    state_path = data_dir / "rotalog/fleet" / f"{node_id}-heartbeat.json"
    state = load_json(state_path, {})
    now = datetime.now(timezone.utc)
    valid_state = (
        state.get("nodeId") == node_id
        and state.get("bucket") == store.store.bucket_name
        and state.get("blob") == store.store.blob_name
    )
    previous_local = parse_iso(state.get("heartbeatAt")) if valid_state else None
    local_elapsed = (now - previous_local).total_seconds() if previous_local else None
    local_due = force or local_elapsed is None or not (0 <= local_elapsed < local_heartbeat_interval_seconds())

    if local_due:
        payload = build_node_payload(
            node_id,
            running_commit=current_commit(),
            status=status,
            detail=detail,
            health=collect_health(data_dir, receipt),
            last_index_upload_at=receipt.get("uploadedAt"),
        )
        state = {
            **payload,
            "bucket": store.store.bucket_name,
            "blob": store.store.blob_name,
            "remoteHeartbeatAt": state.get("remoteHeartbeatAt") if valid_state else None,
        }
        write_json(state_path, state)

    previous_remote = parse_iso(state.get("remoteHeartbeatAt")) if valid_state else None
    remote_elapsed = (now - previous_remote).total_seconds() if previous_remote else None
    remote_due = force or remote_elapsed is None or not (0 <= remote_elapsed < heartbeat_interval_seconds(now))
    if not remote_due:
        return

    remote_payload = {
        key: value
        for key, value in state.items()
        if key not in {"bucket", "blob", "remoteHeartbeatAt"}
    }
    store.save(store.paths.node(node_id), remote_payload)
    state["remoteHeartbeatAt"] = utc_now_iso()
    write_json(state_path, state)


def run_once() -> int:
    node_id = default_node_id()
    store = FleetStore.from_environment(ROOT)
    publish_heartbeat(store, node_id)
    commands = store.pending_commands(node_id)
    if not commands:
        return 0
    command = commands[0]
    command_id = str(command.get("commandId") or "")
    try:
        action = command.get("action")
        if action != "deploy":
            raise RuntimeError(f"Ação não permitida ou desconhecida: {action!r}")
        deploy(store, command, node_id)
    except Exception as exc:
        save_result(
            store,
            command_id,
            node_id,
            status="failed",
            detail=str(exc),
            runningCommit=current_commit(),
            completedAt=utc_now_iso(),
            traceback=traceback.format_exc(limit=8),
        )
        publish_heartbeat(store, node_id, force=True, status="error", detail=str(exc))
        return 1
    publish_heartbeat(store, node_id, force=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
