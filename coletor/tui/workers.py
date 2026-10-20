"""Leitores locais e trabalhadores de coleta usados pela TUI."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from main import LocalRotalogRunner
from coletor.logs import JsonlTailReader
from coletor.storage import load_json
from coletor.fleet_control import default_node_id, parse_iso
from coletor.data_storage import assert_available, StorageSelection
from .state import TuiState, TZ, DAILY_HISTORY_MAX
from .formatting import _today_entries, _format_duration


def _worker(runner: LocalRotalogRunner, state: TuiState) -> None:
    while not state.stop.is_set():
        with state.lock:
            state.is_running = True
            state.status_message = "COLETANDO DADOS DO ROTALOG..."
            state.next_run = None

        try:
            if state.storage:
                assert_available(StorageSelection(**state.storage))
            result = runner.run_once()
        except Exception as exc:
            now_str = datetime.now(TZ).isoformat()
            result = {
                "status": "error",
                "startedAt": now_str,
                "finishedAt": now_str,
                "durationSeconds": 0,
                "error": str(exc),
            }

        with state.lock:
            state.is_running = False
            state.last_result = result
            now = datetime.now(TZ)
            state.history = _today_entries([*state.history, result], now.date().isoformat())
            interval = state.get_interval(now)
            state.next_run = now + timedelta(seconds=interval)
            if result.get("status") == "error":
                state.status_message = f"FALHA NO CICLO: {result.get('error') or 'Erro'}"
            else:
                state.status_message = "AGUARDANDO PROXIMO CICLO"

        end_wait = time.time() + interval
        while time.time() < end_wait and not state.stop.is_set():
            if state.run_now.is_set():
                state.run_now.clear()
                break
            time.sleep(0.2)


def _update_viewer_state_timing(state: TuiState) -> None:
    if not state.last_result:
        state.is_stale = False
        state.status_message = "AGUARDANDO REGISTROS EM execucoes.jsonl..."
        return

    fin_str = state.last_result.get("finishedAt")
    if not fin_str:
        return

    try:
        fin_dt = datetime.fromisoformat(fin_str)
        if fin_dt.tzinfo is None:
            fin_dt = fin_dt.replace(tzinfo=TZ)
        now = datetime.now(TZ)
        current_interval = state.get_interval(now)
        state.next_run = fin_dt + timedelta(seconds=current_interval)

        elapsed = (now - fin_dt).total_seconds()
        stale_threshold = max(current_interval * 2.5, 300.0)

        if elapsed > stale_threshold:
            state.is_stale = True
            state.status_message = f"ALERTA: SERVICO INATIVO / SEM REGISTROS (ultimo ciclo ha {_format_duration(elapsed)})"
        else:
            state.is_stale = False
            state.status_message = "SERVICO SYSTEMD ATIVO (MODO VISUALIZADOR)"
    except Exception:
        pass


def _load_runtime_status(output_dir: Path) -> dict:
    path = output_dir / "rotalog" / "logs" / "runtime-status.json"
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _viewer_worker(state: TuiState, output_dir: Path) -> None:
    log_file = output_dir / "rotalog" / "logs" / "execucoes.jsonl"
    reader = JsonlTailReader(log_file, max_history=DAILY_HISTORY_MAX)

    # Carga inicial rápida da cauda em bloco reverso O(1)
    initial_entries = reader.read_initial()
    if initial_entries:
        with state.lock:
            state.history = _today_entries(initial_entries)
            state.last_result = state.history[-1] if state.history else initial_entries[-1]
            _update_viewer_state_timing(state)

    while not state.stop.is_set():
        try:
            if state.storage:
                assert_available(StorageSelection(**state.storage))
            runtime_status = _load_runtime_status(output_dir)
            new_entries, rotated = reader.read_incremental()
            if rotated:
                # Arquivo rotacionado (novo dia ou truncado): recarrega a nova cauda
                with state.lock:
                    state.history = _today_entries(new_entries)
                    if state.history:
                        state.last_result = state.history[-1]
                    _update_viewer_state_timing(state)
            elif new_entries:
                with state.lock:
                    state.history = _today_entries([*state.history, *new_entries])
                    state.last_result = state.history[-1]
                    _update_viewer_state_timing(state)
            else:
                # Nenhuma linha nova, mas atualiza o status de serviço inativo se estourar o tempo
                with state.lock:
                    if state.last_result:
                        _update_viewer_state_timing(state)
            with state.lock:
                state.runtime_status = runtime_status
        except OSError as exc:
            with state.lock:
                state.is_stale = True
                state.runtime_status = {}
                state.status_message = f"ARMAZENAMENTO INDISPONIVEL: {exc}; reabra o TUI apos recuperar o disco"
        except Exception:
            pass

        time.sleep(1.0)


def _team_state_worker(state: TuiState, output_dir: Path) -> None:
    """Atualiza apenas o índice local, preservando a última leitura em falhas."""
    from coletor.storage import load_json_with_status
    path = output_dir / "rotalog/equipes/current/index.json.gz"
    while not state.stop.is_set():
        try:
            if state.storage:
                assert_available(StorageSelection(**state.storage))
            payload, status = load_json_with_status(path, {})
            if status != "ok" or not isinstance(payload.get("equipes"), dict):
                raise ValueError("Indice local indisponivel")
            with state.lock:
                state.team_snapshots = payload["equipes"]
                state.teams_collected_at = payload.get("lastCollectedAt")
                state.teams_error = None
            feed, feed_status = load_json_with_status(output_dir / "rotalog/events/current/index.json.gz", {})
            feed_valid = (feed_status == "ok" and feed.get("schemaVersion") == 1
                          and feed.get("company") == payload.get("company")
                          and isinstance(feed.get("items"), list))
            with state.lock:
                if feed_valid:
                    state.team_events = feed["items"]
                state.team_events_error = None if feed_valid else "Arquivo de eventos indisponivel"
        except (OSError, ValueError) as exc:
            with state.lock:
                state.teams_error = str(exc)
        state.stop.wait(2)


def _health_worker(state: TuiState, output_dir: Path) -> None:
    """Lê a cópia local do heartbeat, sem consultar Firebase ou sensores novamente."""
    interval = max(5, int(os.getenv("TUI_HEALTH_INTERVAL_SECONDS", "120")))
    heartbeat_path = output_dir / "rotalog" / "fleet" / f"{default_node_id()}-heartbeat.json"
    while not state.stop.is_set():
        try:
            if state.storage:
                assert_available(StorageSelection(**state.storage))
            heartbeat = load_json(heartbeat_path, {})
            health = dict(heartbeat.get("health") or {})
            heartbeat_at = heartbeat.get("heartbeatAt")
            heartbeat_dt = parse_iso(heartbeat_at)
            if heartbeat_dt and (datetime.now(TZ) - heartbeat_dt).total_seconds() > max(180, interval * 2):
                health["overall"] = "warning" if health.get("overall") != "critical" else "critical"
                health["issues"] = [*(health.get("issues") or []), {"code": "HEARTBEAT_DESATUALIZADO", "severity": "warning"}]
            if health:
                health["_heartbeatAt"] = heartbeat_at
            else:
                health = {
                    "overall": "warning",
                    "_heartbeatAt": heartbeat_at,
                    "issues": [{"code": "HEARTBEAT_LOCAL_INDISPONIVEL", "severity": "warning"}],
                }
            if state.storage and health.get("storage") != state.storage:
                health["disk"] = {}
                health["overall"] = "warning"
                health["issues"] = [*(health.get("issues") or []), {"code": "AGUARDANDO_HEARTBEAT_ARMAZENAMENTO", "severity": "warning"}]
            health["storage"] = dict(state.storage) or health.get("storage", {})
            with state.lock:
                state.device_health = health
                state.device_health_updated_at = parse_iso(heartbeat_at) or datetime.now(TZ)
        except Exception as exc:
            with state.lock:
                state.device_health = {
                    "overall": "warning",
                    "storage": dict(state.storage),
                    "disk": {"error": "storage_unavailable"},
                    "issues": [{"code": f"HEALTH_READ_ERROR: {exc}", "severity": "warning"}],
                }
                state.device_health_updated_at = datetime.now(TZ)
        state.stop.wait(interval)
