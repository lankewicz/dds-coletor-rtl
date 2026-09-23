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
from main import (
    LocalRotalogRunner,
    executar_fechamento_mes,
    executar_historico,
    get_adaptive_interval_seconds,
)

TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))
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


def _format_health(health: dict) -> tuple[str, str, str]:
    """Retorna as três linhas compactas exibidas no painel de saúde."""
    overall_labels = {
        "healthy": "SAUDAVEL",
        "warning": "ATENCAO",
        "critical": "CRITICO",
    }
    overall = overall_labels.get(str(health.get("overall") or ""), "DESCONHECIDA")
    temperature = health.get("temperatureC")
    disk = health.get("disk") or {}
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
    load_text = "/".join(str(value) for value in load[:3]) if load else "indisponivel"
    service = collector.get("serviceActive")
    service_text = "ATIVO" if service is True else "INATIVO" if service is False else "DESCONHECIDO"
    last_upload = str(collector.get("lastIndexUploadAt") or "-").replace("T", " ")[:19]
    issues = health.get("issues") or []
    issue_text = ", ".join(str(item.get("code") or item) for item in issues) or "nenhum"
    heartbeat_at = str(health.get("_heartbeatAt") or "-").replace("T", " ")[:19]

    return (
        f"Estado: {overall}  |  CPU: {temp_text}  |  Disco: {disk_text}  |  Memoria: {memory_text}",
        f"Coletor: {service_text}  |  Uptime: {uptime_text}  |  Carga: {load_text}  |  Ultimo envio: {last_upload}",
        f"Heartbeat local: {heartbeat_at}  |  Alertas: {issue_text}",
    )


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
        except Exception:
            pass

        time.sleep(1.0)


def _health_worker(state: TuiState, output_dir: Path) -> None:
    """Lê a cópia local do heartbeat, sem consultar Firebase ou sensores novamente."""
    interval = max(5, int(os.getenv("TUI_HEALTH_INTERVAL_SECONDS", "120")))
    heartbeat_path = output_dir / "rotalog" / "fleet" / f"{default_node_id()}-heartbeat.json"
    while not state.stop.is_set():
        try:
            heartbeat = load_json(heartbeat_path, {})
            health = dict(heartbeat.get("health") or {})
            heartbeat_at = heartbeat.get("heartbeatAt")
            if health:
                health["_heartbeatAt"] = heartbeat_at
            else:
                health = {
                    "overall": "warning",
                    "_heartbeatAt": heartbeat_at,
                    "issues": [{"code": "HEARTBEAT_LOCAL_INDISPONIVEL", "severity": "warning"}],
                }
            with state.lock:
                state.device_health = health
                state.device_health_updated_at = parse_iso(heartbeat_at) or datetime.now(TZ)
        except Exception as exc:
            with state.lock:
                state.device_health = {
                    "overall": "warning",
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

    # -- 2. Barra de Status de Alto Contraste (Banner) ---------------------
    runtime = data.get("runtime_status") or {}
    runtime_phase = str(runtime.get("phase") or "").upper()
    runtime_message = str(runtime.get("message") or "").strip()
    status_y = 3

    if runtime_phase in {"COLETANDO", "PROCESSANDO"}:
        status_text = f" STATUS: {runtime_phase}...  |  {runtime_message} "
        status_color = C_STATUS_RUN
    elif data["is_running"]:
        status_text = f" STATUS: {data['status_message']} "
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

    if last_success:
        success_time = str(last_success.get("finishedAt") or last_success.get("startedAt") or "-")
        _line(screen, status_y + 1, 0, "Ultima coleta com sucesso:", 27, _cp(C_DEFAULT))
        _line(screen, status_y + 1, 28, f"{success_time[11:19]}", 10, _cp(C_OK, curses.A_BOLD))
    else:
        _line(screen, status_y + 1, 0, "Ultima coleta com sucesso: sem registro hoje", width, _cp(C_WARN))

    # -- 3. Caixa: Última Execução -----------------------------------------
    exec_top = status_y + 2
    exec_h = 6
    _box(screen, exec_top, 0, exec_h, width - 1, title="ULTIMA EXECUCAO", color=C_TITLE)
    last = data["last_result"]
    if last:
        result_status = str(last.get("status", "-")).upper()
        result_color = C_OK if result_status == "SUCCESS" else C_ERROR
        _line(screen, exec_top + 1, 2, f"Resultado: {result_status}", 22, _cp(result_color, curses.A_BOLD))
        res_meta = (
            f"inicio: {str(last.get('startedAt', '-'))[11:19]}"
            f"  |  fim: {str(last.get('finishedAt', '-'))[11:19]}"
            f"  |  duracao: {last.get('durationSeconds', '-')}s (raspagem: {last.get('scrapeDurationSeconds', '-')}s)"
        )
        _line(screen, exec_top + 1, 24, res_meta, width - 26, _cp(C_DEFAULT))

        fb_status = last.get("firebaseSyncStatus")
        if fb_status == "disabled":
            upload_flag, upload_color = "nuvem: DESATIVADA (LOCAL)", C_WARN
        elif fb_status == "unchanged":
            upload_flag, upload_color = "torre: SEM ALTERACAO", C_TITLE
        elif last.get("firebaseUploaded") or fb_status == "uploaded":
            upload_flag, upload_color = "nuvem: SINCRONIZADO OK", C_OK
        else:
            upload_flag, upload_color = "nuvem: PENDENTE", C_WARN

        team_line = (
            f"Equipes: {last.get('totalTeams', '-')} total"
            f"  |  {last.get('updatedTeams', '-')} atualizadas"
            f"  |  {last.get('ignoredTeams', '-')} sem alteracao  |  "
        )
        _line(screen, exec_top + 2, 2, team_line, width - 4, _cp(C_DEFAULT))
        _line(screen, exec_top + 2, 2 + len(team_line), upload_flag, width - 4 - len(team_line), _cp(upload_color, curses.A_BOLD))

        last_up = int(last.get("bytesUploaded") or 0)
        last_local_writes = int(last.get("updatedTeams") or 0)
        gravacoes_info = (
            f"Gravacoes: Local {last_local_writes} | Eventos {last.get('cloudEventWrites', last.get('dailySyncUploaded', 0))}"
            f" | Indice {last.get('cloudIndexWrites', 1 if last.get('firebaseUploaded') else 0)}"
            f" | Auditoria 0 (local) | Heartbeat separado | {_format_bytes(last_up)}"
        )
        _line(screen, exec_top + 3, 2, gravacoes_info, width - 4, _cp(C_DEFAULT))

        if last.get("error"):
            _line(screen, exec_top + 4, 2, f"Erro: {last['error']}", width - 4, _cp(C_ERROR, curses.A_BOLD))
        else:
            daily_sync_info = (
                f"Historicos Nuvem:  enviados: {last.get('dailySyncUploaded', 0)}"
                f"  |  pendentes: {last.get('dailySyncPending', 0)}"
                f"  |  falhas: {last.get('dailySyncFailed', 0)}"
            )
            _line(screen, exec_top + 4, 2, daily_sync_info, width - 4, _cp(C_DIM))
    else:
        _line(screen, exec_top + 1, 2, "Aguardando registros do servico em execucoes.jsonl...", width - 4, _cp(C_DIM))

    # -- 4. Caixa: Saúde do Dispositivo ------------------------------------
    health = data.get("device_health") or {}
    health_top = exec_top + exec_h
    health_h = 5
    health_color = _health_color(health)
    _box(screen, health_top, 0, health_h, width - 1, title="SAUDE DO DISPOSITIVO", color=health_color)
    if health:
        health_lines = _format_health(health)
        _line(screen, health_top + 1, 2, health_lines[0], width - 4, _cp(health_color, curses.A_BOLD if health_color != C_OK else 0))
        _line(screen, health_top + 2, 2, health_lines[1], width - 4, _cp(C_DEFAULT))
        issues_attr = _cp(C_WARN, curses.A_BOLD) if (health.get("issues") or health_color != C_OK) else _cp(C_OK)
        _line(screen, health_top + 3, 2, health_lines[2], width - 4, issues_attr)
    else:
        _line(screen, health_top + 1, 2, "Coletando informacoes de saude...", width - 4, _cp(C_DIM))

    # -- 5 e 6. Eventos Operacionais e Histórico Diário --------------------
    bottom_reserved = 5
    hist_top = max(health_top + health_h, height - bottom_reserved)
    events_top = health_top + health_h
    events_h = hist_top - events_top

    if events_h >= 3:
        _box(screen, events_top, 0, events_h, width - 1, title="EVENTOS DE EQUIPES", color=C_TITLE)
        cur_y = events_top + 1
        max_event_y = events_top + events_h - 1

        if last:
            events = last.get("events") or {}
            local_events = events.get("local") or []
            cloud_events = events.get("cloud") or []

            if local_events or cloud_events:
                if local_events and cur_y < max_event_y:
                    _line(screen, cur_y, 2, f"LOCAL ({len(local_events)} equipes com transicao apenas local):", width - 4, _cp(C_WARN, curses.A_BOLD))
                    cur_y += 1
                    local_strs = [f"{e['team']}: {e['action']}" for e in local_events]
                    local_rows = _format_columns(local_strs, width - 4, col_width=35)

                    reserved_for_cloud = min(len(cloud_events) + 2, 4) if cloud_events else 0
                    allowed_local_rows = max(1, max_event_y - cur_y - reserved_for_cloud)

                    for row in local_rows[:allowed_local_rows]:
                        if cur_y >= max_event_y:
                            break
                        _line(screen, cur_y, 4, row, width - 6, _cp(C_DEFAULT))
                        cur_y += 1

                    if len(local_rows) > allowed_local_rows and cur_y < max_event_y:
                        items_per_row = max(1, (width - 4) // 38)
                        remaining_local = len(local_events) - (allowed_local_rows * items_per_row)
                        if remaining_local > 0:
                            _line(screen, cur_y, 4, f"... e mais {remaining_local} equipes", width - 6, _cp(C_DIM))
                            cur_y += 1

                if cloud_events and cur_y < max_event_y:
                    _line(screen, cur_y, 2, f"NUVEM ({len(cloud_events)} eventos sincronizados no Firebase):", width - 4, _cp(C_OK, curses.A_BOLD))
                    cur_y += 1
                    cloud_strs = [f"{e['team']}: {e['action']}" for e in cloud_events]
                    cloud_rows = _format_columns(cloud_strs, width - 4, col_width=35)
                    allowed_cloud_rows = max(1, max_event_y - cur_y)

                    for row in cloud_rows[:allowed_cloud_rows]:
                        if cur_y >= max_event_y:
                            break
                        _line(screen, cur_y, 4, row, width - 6, _cp(C_DEFAULT))
                        cur_y += 1

                    if len(cloud_rows) > allowed_cloud_rows and cur_y < max_event_y:
                        items_per_row = max(1, (width - 4) // 38)
                        remaining_cloud = len(cloud_events) - (allowed_cloud_rows * items_per_row)
                        if remaining_cloud > 0:
                            _line(screen, cur_y, 4, f"... e mais {remaining_cloud} eventos na nuvem", width - 6, _cp(C_DIM))
            else:
                _line(screen, cur_y, 2, "Nenhuma transicao de equipe no ultimo ciclo.", width - 4, _cp(C_DIM))
        else:
            _line(screen, cur_y, 2, "Aguardando execucoes para exibir eventos.", width - 4, _cp(C_DIM))

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
    _line(screen, hist_top + 1, 60, f"|  {cloud_daily}", width - 62, _cp(C_DEFAULT))
    _line(screen, hist_top + 2, 2, "Arquivos: equipes/current/index.json.gz  |  logs/execucoes.jsonl (rotacao diaria .jsonl.gz)", width - 4, _cp(C_DIM))

    # -- 7. Barra de Rodapé / Teclas com Alto Contraste ------------------
    footer_y = height - 1
    _fill(screen, footer_y, 0, width - 1, _cp(C_DEFAULT))

    if is_viewer:
        items = [("h", "Historico (ontem)"), ("m", "Fechar mes anterior"), ("c", "Limpar tela"), ("q", "Sair")]
    else:
        items = [("r", "Executar agora"), ("h", "Historico (ontem)"), ("m", "Fechar mes anterior"), ("c", "Limpar tela"), ("q", "Sair")]

    cur_x = 1
    for key_char, label in items:
        badge = f"[{key_char}]"
        _line(screen, footer_y, cur_x, badge, len(badge), _cp(C_WARN, curses.A_BOLD))
        cur_x += len(badge) + 1
        desc = f"{label}    "
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
    try:
        while True:
            _render(screen, runner, state)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                break
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

    output_dir = Path(args.output_dir).resolve()
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
