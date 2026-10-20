"""Interface interativa de terminal (Curses TUI) para monitoramento do coletor ROTALOG.

Pode rodar de duas formas:
1. Modo Visualizador Passivo (recomendado quando o systemd já está rodando):
   python tui.py --view --output-dir dados-local

2. Modo Ativo (executa a raspagem diretamente no terminal):
   python tui.py --firebase --interval-seconds 120"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from main import LocalRotalogRunner
from coletor.data_storage import resolve_storage, activate_storage, assert_available, guard_write
from . import terminal
from .state import TuiState
from .controller import _curses_main

ROOT = Path(__file__).resolve().parents[2]


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

    if terminal.curses is None:
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
        terminal.curses.wrapper(_curses_main, runner, state, output_dir)
    finally:
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        safe_stream.close()
    return 0
