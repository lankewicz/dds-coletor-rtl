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

from main import LocalRotalogRunner, executar_fechamento_mes, executar_historico

TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))


class TuiState:
    def __init__(self, interval_seconds: int, viewer_mode: bool = False):
        self.interval_seconds = interval_seconds
        self.viewer_mode = viewer_mode
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.run_now = threading.Event()
        self.is_running = False
        self.historico_running = False
        self.last_result: dict | None = None
        self.history: list[dict] = []
        self.next_run: datetime | None = None
        self.status_message = "Inicializando..."


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


def _worker(runner: LocalRotalogRunner, state: TuiState) -> None:
    while not state.stop.is_set():
        with state.lock:
            state.is_running = True
            state.status_message = "COLETANDO DADOS DO ROTALOG..."
            state.next_run = None

        result = runner.run_once()

        with state.lock:
            state.is_running = False
            state.last_result = result
            state.history.append(result)
            state.history = state.history[-50:]
            now = datetime.now(TZ)
            state.next_run = now + timedelta(seconds=state.interval_seconds)
            state.status_message = "AGUARDANDO PROXIMO CICLO"

        end_wait = time.time() + state.interval_seconds
        while time.time() < end_wait and not state.stop.is_set():
            if state.run_now.is_set():
                state.run_now.clear()
                break
            time.sleep(0.2)


def _viewer_worker(state: TuiState, output_dir: Path) -> None:
    log_file = output_dir / "rotalog" / "logs" / "execucoes.jsonl"
    last_mtime = 0.0

    while not state.stop.is_set():
        try:
            if log_file.is_file():
                current_mtime = log_file.stat().st_mtime
                if current_mtime != last_mtime:
                    last_mtime = current_mtime
                    entries = []
                    with log_file.open("r", encoding="utf-8") as stream:
                        for line in stream:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass

                    if entries:
                        with state.lock:
                            state.history = entries[-50:]
                            state.last_result = entries[-1]
                            fin_str = state.last_result.get("finishedAt")
                            if fin_str:
                                try:
                                    fin_dt = datetime.fromisoformat(fin_str)
                                    state.next_run = fin_dt + timedelta(seconds=state.interval_seconds)
                                except Exception:
                                    pass
                            state.status_message = "SERVICO SYSTEMD ATIVO (MODO VISUALIZADOR)"
        except Exception:
            pass

        time.sleep(1.0)


def _line(screen, y: int, text: str, width: int, attr: int = 0) -> None:
    if y < 0:
        return
    max_y, max_x = screen.getmaxyx()
    if y >= max_y:
        return
    text = text[: max(0, min(width, max_x - 1))]
    try:
        screen.addstr(y, 0, text.ljust(min(width, max_x - 1)), attr)
    except curses.error:
        pass


def _render(screen, runner: LocalRotalogRunner | None, state: TuiState) -> None:
    height, width = screen.getmaxyx()
    if height < 16 or width < 70:
        screen.clear()
        _line(screen, 0, "Terminal muito pequeno. Redimensione a janela.", width)
        screen.refresh()
        return

    with state.lock:
        data = {
            "is_running": state.is_running,
            "last_result": state.last_result,
            "history": list(state.history),
            "next_run": state.next_run,
            "status_message": state.status_message,
            "viewer_mode": state.viewer_mode,
        }

    now = datetime.now(TZ)
    history = data["history"]
    successful = [item for item in history if item.get("status") == "success"]
    errors = sum(1 for item in history if item.get("status") != "success")
    durations = [float(item["durationSeconds"]) for item in successful if "durationSeconds" in item]
    average = sum(durations) / len(durations) if durations else None
    total_updates = sum(int(item.get("updatedTeams", 0)) for item in successful)
    total_ignored = sum(int(item.get("ignoredTeams", 0)) for item in successful)

    screen.erase()
    is_viewer = data["viewer_mode"]
    title_mode = "[VISUALIZADOR PASSIVO - SYSTEMD]" if is_viewer else "[EXECUCAO DIRETA]"
    _line(screen, 0, f"=== DDS COLETOR ROTALOG {title_mode} ===", width, curses.A_BOLD)
    empresa_str = runner.empresa if runner else os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro")
    _line(screen, 1, f"Empresa: {empresa_str}  |  Intervalo: {state.interval_seconds}s  |  Hora: {now:%H:%M:%S}", width)
    _line(screen, 2, "-" * (width - 1), width)

    if data["is_running"]:
        _line(screen, 4, f"STATUS: {data['status_message']}", width, curses.A_REVERSE)
    else:
        next_run = data["next_run"]
        if next_run:
            remaining = (next_run - now).total_seconds()
            if remaining > 0:
                _line(screen, 4, f"STATUS: AGUARDANDO  |  proxima coleta: {next_run:%H:%M:%S} (em {_format_duration(remaining)})", width, curses.A_BOLD)
            else:
                _line(screen, 4, "STATUS: AGUARDANDO CICLO DO SERVICO...", width, curses.A_BOLD)
        else:
            _line(screen, 4, "STATUS: AGUARDANDO REGISTROS...", width, curses.A_BOLD)

    last = data["last_result"]
    _line(screen, 6, "ULTIMA EXECUCAO", width, curses.A_UNDERLINE)
    if last:
        upload_flag = "  |  nuvem: OK" if last.get("firebaseUploaded") else "  |  nuvem: LOCAL/PENDENTE"
        _line(screen, 7, f"Resultado: {last.get('status', '-').upper()}  |  inicio: {str(last.get('startedAt', '-'))[11:19]}  |  fim: {str(last.get('finishedAt', '-'))[11:19]}", width)
        _line(screen, 8, f"Tempo total: {last.get('durationSeconds', '-')}s  |  raspagem: {last.get('scrapeDurationSeconds', '-')}s", width)
        _line(screen, 9, f"Equipes: {last.get('totalTeams', '-')}  |  atualizadas: {last.get('updatedTeams', '-')}  |  ignoradas: {last.get('ignoredTeams', '-')}{upload_flag}", width)
        if last.get("error"):
            _line(screen, 10, f"Erro: {last['error']}", width, curses.A_BOLD)
    else:
        _line(screen, 7, "Aguardando registros do servico em execucoes.jsonl...", width)

    _line(screen, 12, "HISTORICO RECENTE", width, curses.A_UNDERLINE)
    _line(screen, 13, f"Ciclos: {len(history)}  |  sucesso: {len(successful)}  |  erros: {errors}  |  media: {_format_duration(average)}", width)
    _line(screen, 14, f"Equipes atualizadas: {total_updates}  |  ignoradas: {total_ignored}", width)
    _line(screen, 16, "Arquivos: equipes/current/index.json.gz  |  equipes/daily/AAAA-MM-DD/*.json.gz  |  logs/execucoes.jsonl", width)

    if is_viewer:
        _line(screen, height - 2, "Teclas: h = historico (ontem)   m = fechar mes anterior   q = fechar", width, curses.A_REVERSE)
    else:
        _line(screen, height - 2, "Teclas: r = executar agora   h = historico (ontem)   m = fechar mes anterior   q = sair", width, curses.A_REVERSE)

    screen.refresh()


def _curses_main(screen, runner: LocalRotalogRunner | None, state: TuiState, output_dir: Path) -> None:
    curses.curs_set(0)
    screen.timeout(250)

    if state.viewer_mode:
        worker = threading.Thread(target=_viewer_worker, args=(state, output_dir), daemon=True)
    else:
        worker = threading.Thread(target=_worker, args=(runner, state), daemon=True)

    worker.start()
    try:
        while True:
            _render(screen, runner, state)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                break
            if key in (ord("r"), ord("R")) and not state.viewer_mode:
                state.run_now.set()
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
                    with state.lock:
                        if state.historico_running:
                            return
                        state.historico_running = True
                        state.status_message = "VARRENDO MES ANTERIOR (FECHAMENTO DE KM)..."
                    try:
                        emp = runner.empresa if runner else os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro")
                        fb = runner.enable_firebase if runner else False
                        ret_code = executar_fechamento_mes(
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
    parser.add_argument("--interval-seconds", type=int, default=120)
    parser.add_argument("--empresa", default=os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro"))
    parser.add_argument("--firebase", action="store_true", help="Ativa sincronização automática com Firebase Storage")
    parser.add_argument("--no-firebase", action="store_true", help="Força desativação do Firebase Storage (apenas local)")
    parser.add_argument("--view", action="store_true", help="Abre como visualizador passivo do serviço systemd (sem raspar)")
    args = parser.parse_args()

    if args.interval_seconds < 30:
        parser.error("--interval-seconds deve ser no mínimo 30")

    output_dir = Path(args.output_dir).resolve()
    if args.no_firebase:
        enable_firebase = False
    else:
        enable_firebase = args.firebase or os.getenv("ROTALOG_UPLOAD_FIREBASE", "false").strip().lower() in ("true", "1", "yes")

    if args.view:
        runner = None
        state = TuiState(args.interval_seconds, viewer_mode=True)
    else:
        runner = LocalRotalogRunner(output_dir, args.empresa, enable_firebase=enable_firebase)
        state = TuiState(args.interval_seconds, viewer_mode=False)

    if curses is None:
        print(
            "Aviso: O módulo curses não está disponível nativamente no Windows.\n"
            "No Linux (Orange Pi) o curses é nativo da biblioteca padrão.\n"
            "Para rodar a interface gráfica de terminal no Windows, instale: pip install windows-curses",
            file=sys.stderr,
        )
        return 1

    curses.wrapper(_curses_main, runner, state, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
