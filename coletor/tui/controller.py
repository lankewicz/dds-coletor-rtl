"""Sessão interativa, navegação por teclado e disparo de comandos."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from main import LocalRotalogRunner, executar_historico, executar_fechamento_mes
from . import terminal
from .state import TuiState
from .workers import _worker, _viewer_worker, _health_worker, _team_state_worker
from .view import _render
from .terminal import _init_colors


def _curses_main(screen, runner: LocalRotalogRunner | None, state: TuiState, output_dir: Path) -> None:
    terminal.curses.curs_set(0)
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
                    elif key in (getattr(terminal.curses, "KEY_DOWN", -1), getattr(terminal.curses, "KEY_NPAGE", -1)):
                        state.details_scroll += 1 if key == getattr(terminal.curses, "KEY_DOWN", -1) else 10
                    elif key in (getattr(terminal.curses, "KEY_UP", -1), getattr(terminal.curses, "KEY_PPAGE", -1)):
                        state.details_scroll = max(0, state.details_scroll - (1 if key == getattr(terminal.curses, "KEY_UP", -1) else 10))
                continue
            if key in (ord("e"), ord("E")):
                with state.lock:
                    state.teams_expanded = not state.teams_expanded
                    state.active_panel = "teams" if state.teams_expanded else "events"
                continue
            if key == 27 and state.teams_expanded:
                with state.lock:
                    state.teams_expanded = False
                    state.active_panel = "events"
                continue
            if key == 9:
                with state.lock:
                    if state.teams_expanded:
                        state.overview_group = (state.overview_group + 1) % 6
                    else:
                        state.active_panel = "teams" if state.active_panel == "events" else "events"
                continue
            if key in (getattr(terminal.curses, "KEY_DOWN", -1), getattr(terminal.curses, "KEY_NPAGE", -1),
                       getattr(terminal.curses, "KEY_UP", -1), getattr(terminal.curses, "KEY_PPAGE", -1)):
                with state.lock:
                    step = 10 if key in (getattr(terminal.curses, "KEY_NPAGE", -1), getattr(terminal.curses, "KEY_PPAGE", -1)) else 1
                    if key in (getattr(terminal.curses, "KEY_UP", -1), getattr(terminal.curses, "KEY_PPAGE", -1)):
                        step = -step
                    if state.teams_expanded:
                        index = state.overview_group
                        state.overview_scroll[index] = max(0, state.overview_scroll[index] + step)
                    else:
                        field = "teams_scroll" if state.active_panel == "teams" else "events_scroll"
                        setattr(state, field, max(0, getattr(state, field) + step))
                continue
            if key in (ord("r"), ord("R")) and not state.viewer_mode:
                state.run_now.set()
            if key in (ord("c"), ord("C"), 12):  # 12 is Ctrl+L
                screen.clear()
                screen.refresh()
            if key == getattr(terminal.curses, "KEY_RESIZE", -1):
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
