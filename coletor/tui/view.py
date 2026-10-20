"""Composição da tela principal e resumo operacional."""
from __future__ import annotations

import os
from datetime import datetime
from main import LocalRotalogRunner
from coletor.fleet_control import parse_iso
from . import terminal
from .state import TuiState, TZ
from .formatting import _format_bytes, _format_duration
from .workers import _load_runtime_status
from .details import _render_details
from .panels import _render_operational_panels
from .overview import _render_overview
from .terminal import C_DEFAULT, C_TITLE, C_OK, C_WARN, C_ERROR, C_STATUS_OK, C_STATUS_WARN, C_STATUS_ERR, C_STATUS_RUN, C_DIM, C_ACCENT, C_HEADER_BAR
from .terminal import _box, _cp, _fill, _health_color, _init_colors, _line


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


def _render(screen, runner: LocalRotalogRunner | None, state: TuiState) -> None:
    height, width = screen.getmaxyx()
    if height < 18 or width < 75:
        screen.erase()
        _line(screen, 0, 0, "Terminal muito pequeno (min. 75x18). Redimensione a janela.", width, _cp(C_WARN, terminal.curses.A_BOLD))
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
            "teams_expanded": state.teams_expanded,
            "team_events": list(state.team_events),
            "team_events_error": state.team_events_error,
            "overview_group": state.overview_group,
            "overview_scroll": list(state.overview_scroll),
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
    _line(screen, 0, 0, "■ DDS COLETOR ROTALOG", width, _cp(C_TITLE, terminal.curses.A_BOLD))
    mode_attr = _cp(C_ACCENT, terminal.curses.A_BOLD) if is_viewer else _cp(C_OK, terminal.curses.A_BOLD)
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
    _line(screen, 1, width - 12, clock_str, 10, _cp(C_TITLE, terminal.curses.A_BOLD))
    screen.hline(2, 0, terminal.curses.ACS_HLINE, width - 1, _cp(C_TITLE))

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
    _line(screen, status_y, 0, "STATUS:", 8, _cp(C_TITLE, terminal.curses.A_BOLD))
    _line(screen, status_y, 8, status_tag, len(status_tag) + 1, _cp(tag_color, terminal.curses.A_BOLD))
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

    if data["teams_expanded"] and events_h >= 9:
        offsets = _render_overview(screen, data, events_top, events_h, width, now)
        with state.lock:
            state.overview_scroll = offsets
    elif data["teams_expanded"] and events_h >= 3:
        _box(screen, events_top, 0, events_h, width - 1, "SITUACAO DAS EQUIPES")
        _line(screen, events_top + 1, 2, "Amplie a altura para consultar os blocos. [e/Esc] Voltar", width - 4, _cp(C_DIM))
    elif events_h >= 6:
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
    _line(screen, hist_top + 1, 16, f"Sucesso: {len(successful)}", 16, _cp(C_OK, terminal.curses.A_BOLD))
    err_color = _cp(C_ERROR, terminal.curses.A_BOLD) if errors > 0 else _cp(C_DIM)
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
    if data["teams_expanded"]:
        items = [(key, "Voltar" if key == "e" else label) for key, label in items]
        items = [(key, "Bloco" if key == "Tab" else label) for key, label in items]

    cur_x = 1
    for key_char, label in items:
        badge = f"[{key_char}]"
        _line(screen, footer_y, cur_x, badge, len(badge), _cp(C_WARN, terminal.curses.A_BOLD))
        cur_x += len(badge) + 1
        desc = f"{label}  "
        _line(screen, footer_y, cur_x, desc, len(desc), _cp(C_DEFAULT))
        cur_x += len(desc)

    screen.refresh()
