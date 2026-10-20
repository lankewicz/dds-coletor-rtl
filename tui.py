"""Interface interativa de terminal (Curses TUI) para monitoramento do coletor ROTALOG.

Pode rodar de duas formas:
1. Modo Visualizador Passivo (recomendado quando o systemd já está rodando):
   python tui.py --view --output-dir dados-local

2. Modo Ativo (executa a raspagem diretamente no terminal):
   python tui.py --firebase --interval-seconds 120
"""

from __future__ import annotations

import argparse
try:
    import curses
except ImportError:
    try:
        import windows_curses as curses  # type: ignore
    except ImportError:
        curses = None

import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from coletor.logs import JsonlTailReader
from coletor.storage import load_json
from coletor.fleet_control import default_node_id, parse_iso
from coletor.data_storage import resolve_storage, activate_storage, assert_available, StorageSelection, guard_write
from main import (
    LocalRotalogRunner,
    executar_fechamento_mes,
    executar_historico,
    get_adaptive_interval_seconds,
)

# A interface operacional deve seguir sempre o horário oficial de Brasília.
# Timestamps técnicos persistidos/enviados continuam em UTC.
TZ = ZoneInfo("America/Sao_Paulo")
DAILY_HISTORY_MAX = 5_000


class _SafeStream:
    """Redireciona saídas diretas de stdout/stderr para arquivo de log durante a interface TUI."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._file = None

    def write(self, text: str) -> None:
        if not text:
            return
        try:
            guard_write(self.log_path)
            if self._file is None:
                self._file = open(self.log_path, "a", encoding="utf-8")
            self._file.write(text)
            self._file.flush()
        except Exception:
            pass

    def flush(self) -> None:
        if self._file:
            try:
                self._file.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None


def _setup_tui_logging(output_dir: Path) -> Path:
    """Configura logging para arquivo sem cuspir linhas cruas no terminal curses."""
    log_dir = output_dir / "rotalog" / "logs"
    guard_write(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    tui_log_file = log_dir / "tui.log"

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.StreamHandler):
            root_logger.removeHandler(handler)

    coletor_logger = logging.getLogger("coletor-rotalog")
    for handler in list(coletor_logger.handlers):
        if isinstance(handler, logging.StreamHandler):
            coletor_logger.removeHandler(handler)

    file_handler = logging.FileHandler(tui_log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.INFO)
    return tui_log_file


class TuiState:
    def __init__(
        self,
        interval_seconds: int | None = None,
        peak_interval: int = 180,
        offpeak_interval: int = 600,
        viewer_mode: bool = False,
    ):
        self.interval_seconds = interval_seconds
        self.peak_interval = peak_interval
        self.offpeak_interval = offpeak_interval
        self.viewer_mode = viewer_mode
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.run_now = threading.Event()
        self.is_running = False
        self.is_stale = False
        self.historico_running = False
        self.last_result: dict | None = None
        self.history: list[dict] = []
        self.next_run: datetime | None = None
        self.status_message = "Inicializando..."
        self.runtime_status: dict = {}
        self.device_health: dict = {}
        self.device_health_updated_at: datetime | None = None
        self.storage: dict = {}
        self.show_details = False
        self.details_scroll = 0
        self.team_snapshots: dict = {}
        self.teams_collected_at = None
        self.teams_error = "Aguardando indice local"
        self.active_panel = "events"
        self.events_scroll = 0
        self.teams_scroll = 0

    def get_interval(self, now: datetime | None = None) -> int:
        if self.interval_seconds is not None:
            return self.interval_seconds
        return get_adaptive_interval_seconds(
            now=now,
            peak_interval=self.peak_interval,
            offpeak_interval=self.offpeak_interval,
        )


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


# ---------------------------------------------------------------------------
# Paleta de cores com alto contraste semântico
# ---------------------------------------------------------------------------
C_DEFAULT = 1      # Texto padrão (Branco/Cinza claro sobre fundo escuro)
C_TITLE = 2        # Cabeçalhos e títulos de seção (Ciano em negrito)
C_OK = 3           # Sucesso / Saudável (Verde)
C_WARN = 4         # Atenção / Pendente (Amarelo)
C_ERROR = 5        # Erro / Crítico (Vermelho)
C_STATUS_OK = 6    # Barra status: normal/aguardando (Preto sobre Fundo Verde)
C_STATUS_WARN = 7  # Barra status: alerta/stale (Preto sobre Fundo Amarelo)
C_STATUS_ERR = 8   # Barra status: erro/falha (Branco sobre Fundo Vermelho)
C_STATUS_RUN = 9   # Barra status: coletando/processando (Branco sobre Fundo Azul)
C_DIM = 10         # Texto secundário / apagado
C_ACCENT = 11      # Destaque / Empresa (Magenta)
C_HEADER_BAR = 12  # Barra de teclas / rodapé (Preto sobre Fundo Ciano)


def _init_colors() -> None:
    """Inicializa os pares de cor com foco em alto contraste."""
    if not curses or not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK

    curses.init_pair(C_DEFAULT, curses.COLOR_WHITE, bg)
    curses.init_pair(C_TITLE, curses.COLOR_CYAN, bg)
    curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_ERROR, curses.COLOR_RED, bg)

    # Barras de status (alto contraste garantido)
    curses.init_pair(C_STATUS_OK, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(C_STATUS_WARN, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    curses.init_pair(C_STATUS_ERR, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(C_STATUS_RUN, curses.COLOR_WHITE, curses.COLOR_BLUE)

    curses.init_pair(C_DIM, curses.COLOR_WHITE, bg)
    curses.init_pair(C_ACCENT, curses.COLOR_MAGENTA, bg)
    curses.init_pair(C_HEADER_BAR, curses.COLOR_BLACK, curses.COLOR_CYAN)


def _cp(index: int, extra: int = 0) -> int:
    """Retorna o atributo de par de cor seguro mesmo se o terminal não tiver cores."""
    if curses is None or not curses.has_colors():
        return extra
    return curses.color_pair(index) | extra


def _health_color(health: dict) -> int:
    """Determina a cor de destaque da saúde do dispositivo."""
    overall = str(health.get("overall") or "").lower()
    if overall == "critical":
        return C_ERROR
    if overall == "warning":
        return C_WARN
    if overall == "healthy":
        return C_OK
    return C_WARN


def _line(screen, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    """Escreve uma linha de texto na posição (y, x), truncando se necessário."""
    if y < 0:
        return
    max_y, max_x = screen.getmaxyx()
    if y >= max_y or x >= max_x - 1:
        return
    available = max_x - 1 - x
    fit_w = max(0, min(width, available))
    try:
        screen.addstr(y, x, text[:fit_w], attr)
    except curses.error:
        pass


def _fill(screen, y: int, x: int, width: int, attr: int) -> None:
    """Preenche uma faixa com espaços usando o atributo/cor dado (para banners de alto contraste)."""
    max_y, max_x = screen.getmaxyx()
    if y >= max_y or y < 0:
        return
    available = max_x - 1 - x
    w = max(0, min(width, available))
    try:
        screen.addstr(y, x, " " * w, attr)
    except curses.error:
        pass


def _box(screen, top: int, left: int, height: int, width: int, title: str = "", color: int = C_TITLE) -> None:
    """Desenha caixa usando caracteres nativos ACS do Curses com título embutido."""
    max_y, max_x = screen.getmaxyx()
    if top < 0 or top + height > max_y or left < 0 or left + width > max_x:
        return
    win_attr = _cp(color)
    try:
        screen.addch(top, left, curses.ACS_ULCORNER, win_attr)
        screen.addch(top, left + width - 1, curses.ACS_URCORNER, win_attr)
        screen.addch(top + height - 1, left, curses.ACS_LLCORNER, win_attr)
        screen.addch(top + height - 1, left + width - 1, curses.ACS_LRCORNER, win_attr)
        screen.hline(top, left + 1, curses.ACS_HLINE, width - 2, win_attr)
        screen.hline(top + height - 1, left + 1, curses.ACS_HLINE, width - 2, win_attr)
        for row in range(top + 1, top + height - 1):
            screen.addch(row, left, curses.ACS_VLINE, win_attr)
            screen.addch(row, left + width - 1, curses.ACS_VLINE, win_attr)
        if title:
            label = f" {title} "
            screen.addstr(top, left + 2, label[: max(0, width - 4)], win_attr | curses.A_BOLD)
    except curses.error:
        pass


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


def _compact_summary(last: dict, health: dict, last_success: dict | None = None) -> list[tuple[str, int]]:
    """Três linhas operacionais; diagnóstico completo fica no painel de detalhes."""
    stamp = parse_iso(last.get("finishedAt") or last.get("startedAt"))
    when = stamp.astimezone(TZ).strftime("%H:%M:%S") if stamp else "-"
    result = "OK" if last.get("status") == "success" else "FALHA" if last else "AGUARDANDO"
    sync = last.get("firebaseSyncStatus")
    cloud = ("LOCAL" if sync == "disabled" else "SEM MUDANCA" if sync == "unchanged"
             else "OK" if last.get("firebaseUploaded") or sync == "uploaded" else "PENDENTE" if last else "-")
    pending, failed = last.get("dailySyncPending", 0), last.get("dailySyncFailed", 0)
    first = (f"Coleta {when} {result} {_format_duration(last.get('durationSeconds'))}"
             f" | Nuvem {cloud} | {last.get('updatedTeams', '-')}/{last.get('totalTeams', '-')} eq"
             f" | Fila {pending} / falhas {failed}")
    disk, smart, memory = health.get("disk") or {}, (health.get("disk") or {}).get("smart") or {}, health.get("memory") or {}
    mode = (health.get("storage") or {}).get("mode")
    disk_label = "SSD" if mode == "ssd" else "Cartao/local" if mode == "card" else "Disco"
    free = _format_bytes(disk["freeBytes"]) if disk.get("freeBytes") is not None else "?"
    cpu = f"{health['temperatureC']:.0f}C" if health.get("temperatureC") is not None else "?"
    ram = f"{memory['availablePercent']:.0f}%" if memory.get("availablePercent") is not None else "?"
    overall = {"healthy": "OK", "warning": "ATENCAO", "critical": "CRITICO"}.get(health.get("overall"), "?")
    second = f"Saude {overall} | CPU {cpu} | RAM {ram} livre | {disk_label} {free} livre"
    if mode == "ssd":
        smart_label = {"passed": "OK", "warning": "ATENCAO", "failing": "FALHA", "unavailable": "N/D"}.get(smart.get("status"), "N/D")
        # SMART antes da capacidade: permanece visível também em terminais estreitos.
        second = f"Saude {overall} | SMART {smart_label} | CPU {cpu} | RAM {ram} livre | SSD {free} livre"
        if smart.get("temperatureC") is not None:
            second += f" ({smart['temperatureC']}C)"
    issues = sorted(health.get("issues") or [], key=lambda item: item.get("severity") != "critical")
    alerts = [str(item.get("code") or item) for item in issues]
    if disk.get("error") and "STORAGE_UNAVAILABLE" not in alerts:
        alerts.insert(0, "ARMAZENAMENTO INDISPONIVEL")
    if smart.get("status") == "unavailable":
        alerts.append("SMART indisponivel")
    if pending or failed:
        alerts.insert(0, f"Sincronizacao: {pending} pendentes / {failed} falhas")
    if last.get("error"):
        alerts.insert(0, f"Coleta: {last['error']}")
    success_stamp = parse_iso((last_success or {}).get("finishedAt"))
    success_text = success_stamp.astimezone(TZ).strftime("%H:%M:%S") if success_stamp else "-"
    third = f"Alertas: {', '.join(alerts) or 'nenhum'} | Sucesso {success_text} | [d] Detalhes"
    first_color = C_ERROR if result == "FALHA" else C_WARN if pending or failed or cloud == "PENDENTE" else C_OK
    return [(first, first_color), (second, _health_color(health)),
            (third, C_ERROR if health.get("overall") == "critical" or last.get("error") else C_WARN if alerts else C_DIM)]


def _detail_timestamp(value) -> str:
    parsed = parse_iso(str(value)) if value else None
    return parsed.astimezone(TZ).strftime("%d/%m/%Y %H:%M:%S") if parsed else "-"


def _detail_sections(last: dict, health: dict) -> list[tuple[str, list[tuple[str, str, int]]]]:
    """Campos de apresentação; não consulta sensores ou altera dados operacionais."""
    def seconds(value):
        return f"{float(value):.1f} s" if value is not None else "-"

    def meter(value, suffix):
        if value is None:
            return "indisponivel"
        percent = max(0, min(100, float(value)))
        filled = round(percent / 10)
        return f"[{'#' * filled}{'-' * (10 - filled)}] {percent:.1f}% {suffix}"

    def number(key):
        value = last.get(key)
        return str(value) if value is not None else "-"

    disk, storage = health.get("disk") or {}, health.get("storage") or {}
    smart, collector = disk.get("smart") or {}, health.get("collector") or {}
    health_label = {"healthy": "SAUDAVEL", "warning": "ATENCAO", "critical": "CRITICO"}.get(health.get("overall"), "DESCONHECIDA")
    status = {"success": "SUCESSO", "error": "FALHA"}.get(last.get("status"), "AGUARDANDO")
    status_color = C_OK if status == "SUCESSO" else C_ERROR if status == "FALHA" else C_WARN
    cloud_raw = last.get("firebaseSyncStatus")
    cloud = {"uploaded": "ENVIADO", "unchanged": "SEM ALTERACOES", "pending": "PENDENTE", "disabled": "DESATIVADA"}.get(cloud_raw, "AGUARDANDO")
    cloud_color = C_OK if cloud_raw in ("uploaded", "unchanged") else C_DIM if cloud_raw == "disabled" else C_WARN
    smart_color = {"passed": C_OK, "warning": C_WARN, "failing": C_ERROR}.get(smart.get("status"), C_WARN)
    service = collector.get("serviceActive")
    try:
        cpu_count = max(1, int(health.get("cpuCount") or os.cpu_count() or 1))
    except (TypeError, ValueError):
        cpu_count = 1
    load = health.get("loadAverage") or []
    load_text = " / ".join(f"{float(value) / cpu_count * 100:.1f}%" for value in load[:3]) or "indisponivel"
    sections = [
        ("ULTIMA EXECUCAO", [
            ("Resultado", status, status_color),
            ("Inicio", _detail_timestamp(last.get("startedAt")), C_DEFAULT),
            ("Fim", _detail_timestamp(last.get("finishedAt")), C_DEFAULT),
            ("Duracao", seconds(last.get("durationSeconds")), C_TITLE),
            ("Raspagem", seconds(last.get("scrapeDurationSeconds")), C_TITLE),
            ("Equipes", f"{number('totalTeams')} total", C_DEFAULT),
            ("Atualizadas", number("updatedTeams"), C_OK),
            ("Sem alteracao", number("ignoredTeams"), C_DIM),
        ]),
        ("SINCRONIZACAO E GRAVACOES", [
            ("Nuvem", cloud, cloud_color),
            ("Enviado", _format_bytes(last.get("bytesUploaded")), C_TITLE),
            ("Ultimo envio", _detail_timestamp(collector.get("lastIndexUploadAt")), C_DEFAULT),
            ("Historicos enviados", number("dailySyncUploaded"), C_DEFAULT),
            ("Pendentes", number("dailySyncPending"), C_WARN if last.get("dailySyncPending") else C_DIM),
            ("Falhas de envio", number("dailySyncFailed"), C_ERROR if last.get("dailySyncFailed") else C_DIM),
            ("Gravacoes locais", number("updatedTeams"), C_DEFAULT),
            ("Eventos / indice", f"{number('cloudEventWrites')} / {number('cloudIndexWrites')}", C_DEFAULT),
            ("Auditoria", number("cloudAuditWrites"), C_DIM),
        ]),
        ("SAUDE DO DISPOSITIVO", [
            ("Estado", health_label, _health_color(health)),
            ("Coletor", "ATIVO" if service is True else "INATIVO" if service is False else "DESCONHECIDO", C_OK if service is True else C_WARN),
            ("CPU", f"{float(health['temperatureC']):.1f} C" if health.get("temperatureC") is not None else "indisponivel", C_DEFAULT),
            ("Memoria", meter((health.get("memory") or {}).get("availablePercent"), "livre"), C_TITLE),
            ("Uptime", _format_duration(health.get("uptimeSeconds")), C_DEFAULT),
            ("Carga 1/5/15 min", load_text, C_DEFAULT),
            ("Heartbeat local", _detail_timestamp(health.get("_heartbeatAt")), C_DIM),
            ("Alertas", ", ".join(str(item.get("code") or item) if isinstance(item, dict) else str(item) for item in health.get("issues") or []) or "nenhum", _health_color(health)),
        ]),
        ("ARMAZENAMENTO E SMART", [
            ("Destino", {"ssd": "SSD", "card": "CARTAO / DIRETORIO LOCAL"}.get(storage.get("mode"), "DESCONHECIDO"), C_TITLE),
            ("Ocupacao", meter(disk.get("usedPercent"), "usado"), C_DEFAULT),
            ("Espaco livre", f"{_format_bytes(disk.get('freeBytes'))} / {_format_bytes(disk.get('totalBytes'))}", C_TITLE),
            ("SMART", {"passed": "OK", "warning": "ATENCAO", "failing": "FALHA", "unavailable": "INDISPONIVEL"}.get(smart.get("status"), "SEM LEITURA"), smart_color),
            ("Temperatura SSD", f"{smart['temperatureC']} C" if smart.get("temperatureC") is not None else "-", C_DEFAULT),
            ("Horas ligado", str(smart.get("powerOnHours", "-")), C_DEFAULT),
            ("Erros CRC", str(smart.get("crcErrors", "-")), C_WARN if smart.get("crcErrors") else C_DIM),
            ("Ciclos de energia", str(smart.get("powerCycles", "-")), C_DIM),
        ]),
    ]
    for key, label in (("reallocatedSectors", "Setores realocados"), ("pendingSectors", "Setores pendentes"),
                       ("uncorrectableSectors", "Incorretiveis"), ("mediaErrors", "Erros de midia"),
                       ("wearUsedPercent", "Desgaste NVMe")):
        if smart.get(key) is not None:
            sections[-1][1].append((label, str(smart[key]) + ("%" if key == "wearUsedPercent" else ""), C_WARN if smart[key] else C_DIM))
    if smart.get("reason"):
        sections[-1][1].append(("Leitura SMART", str(smart["reason"]), smart_color))
    sections.append(("IDENTIFICACAO E ARQUIVOS", [
        ("Modelo / dispositivo", f"{disk.get('model') or '-'} / {disk.get('device') or storage.get('device') or '-'}", C_DEFAULT),
        ("UUID", str(storage.get("uuid") or "-"), C_DIM),
        ("Diretorio dos dados", str(storage.get("data_dir") or disk.get("path") or "-"), C_DIM),
        ("Selecao do destino", str(storage.get("reason") or "-"), C_DIM),
        ("Indice", "equipes/current/index.json.gz", C_DEFAULT),
        ("Logs", "logs/execucoes.jsonl (rotacao diaria .jsonl.gz)", C_DEFAULT),
    ]))
    return sections


def _detail_rows(last: dict, health: dict, width: int) -> list[list[tuple[int, str, int]]]:
    import textwrap
    # Cada segmento conserva sua cor ao quebrar linhas e ao rolar a tela.
    def card(section, size):
        title, fields = section
        rows = [[(0, "┌─ " + title + " " + "─" * max(0, size - len(title) - 5) + "┐", C_TITLE)]]
        label_width = min(21, max(12, size // 3))
        for label, value, color in fields:
            parts = textwrap.wrap(value, max(1, size - label_width - 5), replace_whitespace=False) or ["-"]
            for index, part in enumerate(parts):
                rows.append([(0, "│", C_TITLE), (2, (label + ":") if index == 0 else "", C_DIM),
                             (label_width + 3, part, color), (size - 1, "│", C_TITLE)])
        rows.append([(0, "└" + "─" * (size - 2) + "┘", C_TITLE)])
        return rows

    rows = []
    if last.get("error"):
        for line in textwrap.wrap("FALHA NA COLETA: " + str(last["error"]), max(1, width - 4)):
            rows.append([(2, line, C_ERROR)])
        rows.append([])
    sections = _detail_sections(last, health)
    for start in (0, 2, 4):
        group = sections[start:start + 2]
        if width >= 120 and len(group) == 2:
            left_width = (width - 2) // 2
            cards = [card(group[0], left_width), card(group[1], width - left_width - 2)]
            for index in range(max(map(len, cards))):
                row = list(cards[0][index]) if index < len(cards[0]) else []
                if index < len(cards[1]):
                    row += [(x + left_width + 2, text, color) for x, text, color in cards[1][index]]
                rows.append(row)
            rows.append([])
        else:
            for section in group:
                rows.extend(card(section, width))
                rows.append([])
    return rows


def _render_details(screen, last: dict, health: dict, scroll: int) -> int:
    height, width = screen.getmaxyx()
    _line(screen, 4, 1, "DETALHES  /  EXECUCAO E DISPOSITIVO", width - 3, _cp(C_TITLE, curses.A_BOLD))
    _line(screen, 5, 1, "Horarios de Brasilia  |  Verde: OK   Amarelo: atencao   Vermelho: falha", width - 3, _cp(C_DIM))
    lines = _detail_rows(last, health, width - 3)
    visible = max(1, height - 8)
    offset = min(max(0, scroll), max(0, len(lines) - visible))
    for row, segments in enumerate(lines[offset:offset + visible], start=7):
        for x, text, color in segments:
            _line(screen, row, x + 1, text, width - x - 3, _cp(color, curses.A_BOLD if color in (C_TITLE, C_ERROR, C_OK, C_WARN) else 0))
    _fill(screen, height - 1, 0, width - 1, _cp(C_HEADER_BAR))
    _line(screen, height - 1, 1, f"[d/Esc] Voltar  [Setas/PgUp/PgDn] Rolar  [q] Sair | {offset + 1}-{min(offset + visible, len(lines))}/{len(lines)}", width - 3, _cp(C_HEADER_BAR))
    screen.refresh()
    return offset


def _team_current_rows(snapshots: dict, now: datetime) -> list[tuple]:
    rows = []
    for key, doc in sorted(snapshots.items()):
        journey = doc.get("jornada") or {}
        shift = journey.get("turno") or {}
        service = (doc.get("ordensServico") or {}).get("atual") or {}
        value = None
        if journey.get("emIntervalo"):
            label, color = "Intervalo", C_ACCENT
            intervals = [i for i in journey.get("intervalos") or [] if not i.get("fim")]
            value = (journey.get("intervaloAtual") or {}).get("inicio") or (intervals[-1].get("inicio") if intervals else None)
        elif service.get("statusAtual") in ("EXECUCAO", "DESLOCAMENTO"):
            execution = service["statusAtual"] == "EXECUCAO"
            label, color = ("Execucao", C_WARN) if execution else ("Desloc.", C_TITLE)
            field = "inicioExecucao" if execution else "inicioDeslocamento"
            if field not in (service.get("camposEstimados") or []):
                value = service.get(field)
        else:
            label, color = {"ABERTO": ("Aberto", C_OK), "FECHADO": ("Fechado", C_DIM)}.get(shift.get("status"), ("Indef.", C_DIM))
            value = shift.get("fim") if shift.get("status") == "FECHADO" else shift.get("inicio") if shift.get("status") == "ABERTO" else None
        stamp = parse_iso(value) if value else None
        hour = stamp.astimezone(TZ).strftime("%H:%M:%S") if stamp and stamp <= now else "—"
        queue = doc.get("fila") or {}
        pending = f"{queue.get('comercial', '—')}/{queue.get('emergencia', '—')}"
        rows.append((key, label, hour, pending, color))
    return rows


def _render_operational_panels(screen, last, data, top, height, width, now):
    split = width >= 144
    show_teams = split or data["active_panel"] == "teams"
    right_width = (width - 1) // 4 if split else width - 1
    left_width = width - right_width - 2 if split else width - 1
    offsets = {"events_scroll": data["events_scroll"], "teams_scroll": data["teams_scroll"]}
    if split or not show_teams:
        _box(screen, top, 0, height, left_width, "EVENTOS DE EQUIPES", C_TITLE if data["active_panel"] == "events" else C_DIM)
        lines = []
        events = last.get("events") or {}
        for key, title, color in (("local", "LOCAL", C_WARN), ("cloud", "NUVEM", C_OK)):
            entries = events.get(key) or []
            if entries:
                lines.append((f"{title} ({len(entries)} eventos)", color))
                lines.extend((text, C_DEFAULT) for text in _format_columns(
                    [f"{e['team']}: {e['action']}" for e in entries], left_width - 5, col_width=35))
        if not lines:
            lines = [("Nenhuma transicao de equipe no ultimo ciclo." if last else "Aguardando execucoes para exibir eventos.", C_DIM)]
        visible = max(1, height - 3)
        offset = min(max(0, data["events_scroll"]), max(0, len(lines) - visible))
        offsets["events_scroll"] = offset
        for row, (text, color) in enumerate(lines[offset:offset + visible], top + 1):
            _line(screen, row, 2, text, left_width - 4, _cp(color))
        _line(screen, top + height - 2, 2, f"{offset + 1}-{min(offset + visible, len(lines))}/{len(lines)}  Tab: quadro | Setas/Pg: rolar", left_width - 4, _cp(C_DIM))
    if show_teams:
        x = left_width + 1 if split else 0
        _box(screen, top, x, height, right_width, "SITUACAO ATUAL", C_TITLE if data["active_panel"] == "teams" else C_DIM)
        rows = _team_current_rows(data["team_snapshots"], now)
        stamp = parse_iso(data["teams_collected_at"]) if data["teams_collected_at"] else None
        stamp_text = stamp.astimezone(TZ).strftime("%H:%M:%S") if stamp else "—"
        stale = stamp and now - stamp > timedelta(seconds=data["teams_stale_seconds"])
        note = "Indice indisponivel" if data["teams_error"] else f"Desatualizado | {stamp_text}" if stale else f"{len(rows)} equipes | Coleta {stamp_text}"
        _line(screen, top + 1, x + 2, note, right_width - 4, _cp(C_WARN if data["teams_error"] or stale else C_DIM))
        _line(screen, top + 2, x + 2, f"{'Equipe':<8} {'Situacao':<9} {'Hora':<8} C/E", right_width - 4, _cp(C_TITLE, curses.A_BOLD))
        visible = max(1, height - 5)
        offset = min(max(0, data["teams_scroll"]), max(0, len(rows) - visible))
        offsets["teams_scroll"] = offset
        for row, (team, label, hour, queue, color) in enumerate(rows[offset:offset + visible], top + 3):
            _line(screen, row, x + 2, f"{team:<8} {label:<9} {hour:<8} {queue}", right_width - 4, _cp(color))
        if not rows:
            _line(screen, top + 3, x + 2, "Sem equipes no indice" if not data["teams_error"] else "Aguardando indice local", right_width - 4, _cp(C_DIM))
        _line(screen, top + height - 2, x + 2, f"{offset + 1 if rows else 0}-{min(offset + visible, len(rows))}/{len(rows)} | C/E: fila", right_width - 4, _cp(C_DIM))
    return offsets


def _render(screen, runner: LocalRotalogRunner | None, state: TuiState) -> None:
    height, width = screen.getmaxyx()
    if height < 18 or width < 75:
        screen.erase()
        _line(screen, 0, 0, "Terminal muito pequeno (min. 75x18). Redimensione a janela.", width, _cp(C_WARN, curses.A_BOLD))
        screen.refresh()
        return

    with state.lock:
        data = {
            "is_running": state.is_running,
            "is_stale": state.is_stale,
            "last_result": state.last_result,
            "history": list(state.history),
            "next_run": state.next_run,
            "status_message": state.status_message,
            "runtime_status": dict(state.runtime_status),
            "device_health": dict(state.device_health),
            "device_health_updated_at": state.device_health_updated_at,
            "viewer_mode": state.viewer_mode,
            "storage": dict(state.storage),
            "show_details": state.show_details,
            "details_scroll": state.details_scroll,
            "team_snapshots": dict(state.team_snapshots),
            "teams_collected_at": state.teams_collected_at,
            "teams_error": state.teams_error,
            "active_panel": state.active_panel,
            "events_scroll": state.events_scroll,
            "teams_scroll": state.teams_scroll,
            "teams_stale_seconds": max(300, state.get_interval() * 2.5),
        }
    if runner is not None:
        data["runtime_status"] = _load_runtime_status(runner.output_dir)

    now = datetime.now(TZ)
    history = data["history"]
    successful = [item for item in history if item.get("status") == "success"]
    errors = sum(1 for item in history if item.get("status") != "success")
    durations = [float(item["durationSeconds"]) for item in successful if "durationSeconds" in item]
    last_success = successful[-1] if successful else None
    average = sum(durations) / len(durations) if durations else None
    total_bytes_up = sum(int(item.get("bytesUploaded") or 0) for item in successful)
    total_writes = sum(int(item.get("writeOperations") or 0) for item in successful)
    total_local_writes = sum(int(item.get("updatedTeams") or 0) for item in successful)

    screen.erase()
    is_viewer = data["viewer_mode"]
    title_mode = "VISUALIZADOR PASSIVO - SYSTEMD" if is_viewer else "EXECUCAO DIRETA"

    # -- 1. Cabeçalho --------------------------------------------------------
    _line(screen, 0, 0, "■ DDS COLETOR ROTALOG", width, _cp(C_TITLE, curses.A_BOLD))
    mode_attr = _cp(C_ACCENT, curses.A_BOLD) if is_viewer else _cp(C_OK, curses.A_BOLD)
    _line(screen, 0, 24, f"[{title_mode}]", width - 24, mode_attr)

    empresa_str = runner.empresa if runner else os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro")
    cur_interval = state.get_interval(now)
    is_peak = 7 <= now.hour < 20
    tag_intervalo = f"{cur_interval}s [PICO 07-20h]" if is_peak else f"{cur_interval}s [NOTURNO 20-07h]"
    if state.interval_seconds is not None:
        tag_intervalo = f"{state.interval_seconds}s [FIXO]"

    _line(screen, 1, 0, f"Empresa: {empresa_str}", width, _cp(C_ACCENT))
    _line(screen, 1, 24, f"Intervalo: {tag_intervalo}", width - 24, _cp(C_DEFAULT))
    clock_str = f"{now:%H:%M:%S}"
    _line(screen, 1, width - 12, clock_str, 10, _cp(C_TITLE, curses.A_BOLD))
    screen.hline(2, 0, curses.ACS_HLINE, width - 1, _cp(C_TITLE))

    # -- 2. Barra de Status com Alto Contraste -----------------------------
    runtime = data.get("runtime_status") or {}
    runtime_phase = str(runtime.get("phase") or "").upper()
    runtime_message = str(runtime.get("message") or "").strip()
    status_y = 3

    if runtime_phase in {"COLETANDO", "PROCESSANDO"}:
        status_tag = f"[{runtime_phase}]"
        tag_color = C_TITLE
        status_detail = runtime_message
    elif data["is_running"]:
        status_tag = "[COLETANDO]"
        tag_color = C_TITLE
        status_detail = data["status_message"]
    elif data.get("is_stale"):
        status_tag = "[ALERTA / INATIVO]"
        tag_color = C_WARN
        status_detail = data["status_message"]
    elif (data.get("last_result") or {}).get("status") == "error":
        status_tag = "[FALHA]"
        tag_color = C_ERROR
        next_run = data["next_run"]
        if next_run:
            rem = max(0, (next_run - now).total_seconds())
            retry_text = f"proxima tentativa: {next_run:%H:%M:%S} (em {_format_duration(rem)})"
        else:
            retry_text = "proxima tentativa: aguardando agendamento"
        err_msg = data["last_result"].get("error") or "Erro na execucao"
        status_detail = f"{err_msg}  |  {retry_text}"
    else:
        status_tag = "[AGUARDANDO]"
        tag_color = C_OK
        next_run = data["next_run"]
        if next_run:
            rem = (next_run - now).total_seconds()
            if rem > 0:
                status_detail = f"proxima coleta: {next_run:%H:%M:%S} (em {_format_duration(rem)})"
            else:
                status_detail = "aguardando ciclo do servico..."
        else:
            status_detail = "aguardando registros..."

    # Renderiza linha de status com alto contraste no fundo padrão
    _fill(screen, status_y, 0, width - 1, _cp(C_DEFAULT))
    _line(screen, status_y, 0, "STATUS:", 8, _cp(C_TITLE, curses.A_BOLD))
    _line(screen, status_y, 8, status_tag, len(status_tag) + 1, _cp(tag_color, curses.A_BOLD))
    _line(screen, status_y, 8 + len(status_tag) + 1, f"|  {status_detail}", width - (10 + len(status_tag)), _cp(C_DEFAULT))

    last = data["last_result"] or {}
    health = dict(data.get("device_health") or {})
    health.setdefault("storage", data["storage"])
    if data["show_details"]:
        offset = _render_details(screen, last, health, data["details_scroll"])
        with state.lock:
            state.details_scroll = offset
        return

    # Resumo fixo de três linhas; todo o restante da altura fica para eventos.
    summary_top, summary_h = 4, 5
    _box(screen, summary_top, 0, summary_h, width - 1, title="RESUMO OPERACIONAL  [d] DETALHES")
    for offset, (text, color) in enumerate(_compact_summary(last, health, last_success), start=1):
        _line(screen, summary_top + offset, 2, text, width - 4, _cp(color))

    # -- 5 e 6. Eventos Operacionais e Histórico Diário --------------------
    bottom_reserved = 5
    hist_top = height - bottom_reserved
    events_top = summary_top + summary_h
    events_h = hist_top - events_top

    if events_h >= 6:
        offsets = _render_operational_panels(screen, last, data, events_top, events_h, width, now)
        with state.lock:
            state.events_scroll = offsets["events_scroll"]
            state.teams_scroll = offsets["teams_scroll"]
    elif events_h >= 3:
        _box(screen, events_top, 0, events_h, width - 1, title="EVENTOS DE EQUIPES", color=C_TITLE)
        _line(screen, events_top + 1, 2, "Amplie a altura para consultar os quadros.", width - 4, _cp(C_DIM))

    # -- 6. Caixa: Histórico Diário ----------------------------------------
    _box(screen, hist_top, 0, 4, width - 1, title="HISTORICO DIARIO (HOJE)", color=C_TITLE)
    cloud_daily = (
        f"Local: {total_local_writes} gravacoes  |  Nuvem: {total_writes} gravacoes ({_format_bytes(total_bytes_up)} env)"
    )
    _line(screen, hist_top + 1, 2, f"Ciclos: {len(history)}", 14, _cp(C_DEFAULT))
    _line(screen, hist_top + 1, 16, f"Sucesso: {len(successful)}", 16, _cp(C_OK, curses.A_BOLD))
    err_color = _cp(C_ERROR, curses.A_BOLD) if errors > 0 else _cp(C_DIM)
    _line(screen, hist_top + 1, 32, f"Erros: {errors}", 12, err_color)
    _line(screen, hist_top + 1, 44, f"Media: {_format_duration(average)}", 16, _cp(C_DEFAULT))
    _line(screen, hist_top + 2, 2, cloud_daily, width - 4, _cp(C_DIM))

    # -- 7. Barra de Rodapé / Teclas com Alto Contraste ------------------
    footer_y = height - 1
    _fill(screen, footer_y, 0, width - 1, _cp(C_DEFAULT))

    if is_viewer:
        items = [("d", "Detalhes"), ("h", "Historico (ontem)"), ("m", "Fechar mes anterior"), ("c", "Limpar tela"), ("q", "Sair")]
    else:
        items = [("d", "Detalhes"), ("r", "Executar agora"), ("h", "Historico (ontem)"), ("m", "Fechar mes anterior"), ("c", "Limpar tela"), ("q", "Sair")]
    if width < 120:
        labels = {"d": "Detalhes", "r": "Agora", "h": "Hist.", "m": "Mes", "c": "Limpar", "q": "Sair"}
        items = [(key, labels[key]) for key, _ in items]
    if width < 144:
        items = [("q", "Sair"), ("d", "Detalhes"), ("Tab", "Quadro"), ("e", "Equipes"), ("h", "Hist."), ("m", "Mes")]
    else:
        items = [("Tab", "Quadro"), ("e", "Equipes"), *items]

    cur_x = 1
    for key_char, label in items:
        badge = f"[{key_char}]"
        _line(screen, footer_y, cur_x, badge, len(badge), _cp(C_WARN, curses.A_BOLD))
        cur_x += len(badge) + 1
        desc = f"{label}  "
        _line(screen, footer_y, cur_x, desc, len(desc), _cp(C_DEFAULT))
        cur_x += len(desc)

    screen.refresh()


def _curses_main(screen, runner: LocalRotalogRunner | None, state: TuiState, output_dir: Path) -> None:
    curses.curs_set(0)
    screen.timeout(250)
    _init_colors()

    if state.viewer_mode:
        worker = threading.Thread(target=_viewer_worker, args=(state, output_dir), daemon=True)
    else:
        worker = threading.Thread(target=_worker, args=(runner, state), daemon=True)

    worker.start()
    health_worker = threading.Thread(target=_health_worker, args=(state, output_dir), daemon=True)
    health_worker.start()
    teams_worker = threading.Thread(target=_team_state_worker, args=(state, output_dir), daemon=True)
    teams_worker.start()
    try:
        while True:
            _render(screen, runner, state)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                break
            if key in (ord("d"), ord("D")):
                with state.lock:
                    state.show_details = not state.show_details
                    state.details_scroll = 0
            if state.show_details:
                with state.lock:
                    if key == 27:
                        state.show_details = False
                    elif key in (getattr(curses, "KEY_DOWN", -1), getattr(curses, "KEY_NPAGE", -1)):
                        state.details_scroll += 1 if key == getattr(curses, "KEY_DOWN", -1) else 10
                    elif key in (getattr(curses, "KEY_UP", -1), getattr(curses, "KEY_PPAGE", -1)):
                        state.details_scroll = max(0, state.details_scroll - (1 if key == getattr(curses, "KEY_UP", -1) else 10))
                continue
            if key in (9, ord("e"), ord("E")):
                with state.lock:
                    state.active_panel = "teams" if state.active_panel == "events" else "events"
                continue
            if key in (getattr(curses, "KEY_DOWN", -1), getattr(curses, "KEY_NPAGE", -1),
                       getattr(curses, "KEY_UP", -1), getattr(curses, "KEY_PPAGE", -1)):
                with state.lock:
                    field = "teams_scroll" if state.active_panel == "teams" else "events_scroll"
                    step = 10 if key in (getattr(curses, "KEY_NPAGE", -1), getattr(curses, "KEY_PPAGE", -1)) else 1
                    if key in (getattr(curses, "KEY_UP", -1), getattr(curses, "KEY_PPAGE", -1)):
                        step = -step
                    setattr(state, field, max(0, getattr(state, field) + step))
                continue
            if key in (ord("r"), ord("R")) and not state.viewer_mode:
                state.run_now.set()
            if key in (ord("c"), ord("C"), 12):  # 12 is Ctrl+L
                screen.clear()
                screen.refresh()
            if key == getattr(curses, "KEY_RESIZE", -1):
                screen.clear()
            if key in (ord("h"), ord("H")):
                def _do_historico():
                    with state.lock:
                        if state.historico_running:
                            return
                        state.historico_running = True
                        state.status_message = "COLETANDO HISTORICO DO ROTALOG (ONTEM)..."
                    try:
                        emp = runner.empresa if runner else os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro")
                        fb = runner.enable_firebase if runner else False
                        ret_code = executar_historico(
                            target_date_str="ontem",
                            output_dir=output_dir,
                            empresa=emp,
                            enable_firebase=fb,
                        )
                        with state.lock:
                            state.status_message = (
                                "HISTORICO CONCLUIDO COM SUCESSO"
                                if ret_code == 0
                                else "FALHA NA COLETA DO HISTORICO"
                            )
                    except Exception as err:
                        with state.lock:
                            state.status_message = f"ERRO HISTORICO: {err}"
                    finally:
                        with state.lock:
                            state.historico_running = False

                threading.Thread(target=_do_historico, daemon=True).start()

            if key in (ord("m"), ord("M")):
                def _do_fechamento_mes():
                    def atualizar_progresso(mensagem):
                        with state.lock:
                            state.status_message = mensagem

                    with state.lock:
                        if state.historico_running:
                            return
                        state.historico_running = True
                        state.status_message = "VARRENDO MES ANTERIOR (FECHAMENTO DE KM)..."
                    try:
                        emp = runner.empresa if runner else os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro")
                        fb = runner.enable_firebase if runner else False
                        ret_code = executar_fechamento_mes(
                            progresso=atualizar_progresso,
                            mes_str="anterior",
                            output_dir=output_dir,
                            empresa=emp,
                            enable_firebase=fb,
                        )
                        with state.lock:
                            state.status_message = (
                                "FECHAMENTO DO MES ANTERIOR CONCLUIDO COM SUCESSO"
                                if ret_code == 0
                                else "FALHA NO FECHAMENTO DO MES ANTERIOR"
                            )
                    except Exception as err:
                        with state.lock:
                            state.status_message = f"ERRO FECHAMENTO MES: {err}"
                    finally:
                        with state.lock:
                            state.historico_running = False

                threading.Thread(target=_do_fechamento_mes, daemon=True).start()
    finally:
        state.stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="dados-local")
    parser.add_argument("--interval-seconds", type=int, default=None, help="Intervalo fixo em segundos (desativa adaptação horária)")
    parser.add_argument("--peak-interval", type=int, default=180, help="Intervalo de pico em segundos (07:00 às 20:00, padrão: 180s)")
    parser.add_argument("--offpeak-interval", type=int, default=600, help="Intervalo noturno em segundos (20:00 às 07:00, padrão: 600s)")
    parser.add_argument("--empresa", default=os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro"))
    parser.add_argument("--firebase", action="store_true", help="Ativa sincronização automática com Firebase Storage")
    parser.add_argument("--no-firebase", action="store_true", help="Força desativação do Firebase Storage (apenas local)")
    parser.add_argument("--view", action="store_true", help="Abre como visualizador passivo do serviço systemd (sem raspar)")
    args = parser.parse_args()

    if args.interval_seconds is not None and args.interval_seconds < 30:
        parser.error("--interval-seconds deve ser no mínimo 30")
    if args.peak_interval < 30 or args.offpeak_interval < 30:
        parser.error("--peak-interval e --offpeak-interval devem ser no mínimo 30")

    storage = resolve_storage(Path(args.output_dir), args.empresa, project_root=ROOT, writable=not args.view)
    activate_storage(storage)
    assert_available(storage)
    output_dir = Path(storage.data_dir)
    tui_log_file = _setup_tui_logging(output_dir)

    if args.no_firebase:
        enable_firebase = False
    else:
        enable_firebase = args.firebase or os.getenv("ROTALOG_UPLOAD_FIREBASE", "false").strip().lower() in ("true", "1", "yes")

    if args.view:
        runner = None
        state = TuiState(
            interval_seconds=args.interval_seconds,
            peak_interval=args.peak_interval,
            offpeak_interval=args.offpeak_interval,
            viewer_mode=True,
        )
    else:
        runner = LocalRotalogRunner(output_dir, args.empresa, enable_firebase=enable_firebase)
        state = TuiState(
            interval_seconds=args.interval_seconds,
            peak_interval=args.peak_interval,
            offpeak_interval=args.offpeak_interval,
            viewer_mode=False,
        )

    state.storage = storage.payload()

    if curses is None:
        print(
            "Aviso: O módulo curses não está disponível nativamente no Windows.\n"
            "No Linux (Orange Pi) o curses é nativo da biblioteca padrão.\n"
            "Para rodar a interface gráfica de terminal no Windows, instale: pip install windows-curses",
            file=sys.stderr,
        )
        return 1

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    safe_stream = _SafeStream(tui_log_file)
    sys.stdout = safe_stream
    sys.stderr = safe_stream
    try:
        curses.wrapper(_curses_main, runner, state, output_dir)
    finally:
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        safe_stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
