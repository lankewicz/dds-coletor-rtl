"""Apresentação do painel de detalhes de execução e dispositivo."""
from __future__ import annotations

import os
from coletor.fleet_control import parse_iso
from . import terminal
from .state import TZ
from .formatting import _format_bytes, _format_duration
from .terminal import C_DEFAULT, C_TITLE, C_OK, C_WARN, C_ERROR, C_STATUS_OK, C_STATUS_WARN, C_STATUS_ERR, C_STATUS_RUN, C_DIM, C_ACCENT, C_HEADER_BAR
from .terminal import _box, _cp, _fill, _health_color, _init_colors, _line


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
    _line(screen, 4, 1, "DETALHES  /  EXECUCAO E DISPOSITIVO", width - 3, _cp(C_TITLE, terminal.curses.A_BOLD))
    _line(screen, 5, 1, "Horarios de Brasilia  |  Verde: OK   Amarelo: atencao   Vermelho: falha", width - 3, _cp(C_DIM))
    lines = _detail_rows(last, health, width - 3)
    visible = max(1, height - 8)
    offset = min(max(0, scroll), max(0, len(lines) - visible))
    for row, segments in enumerate(lines[offset:offset + visible], start=7):
        for x, text, color in segments:
            _line(screen, row, x + 1, text, width - x - 3, _cp(color, terminal.curses.A_BOLD if color in (C_TITLE, C_ERROR, C_OK, C_WARN) else 0))
    _fill(screen, height - 1, 0, width - 1, _cp(C_HEADER_BAR))
    _line(screen, height - 1, 1, f"[d/Esc] Voltar  [Setas/PgUp/PgDn] Rolar  [q] Sair | {offset + 1}-{min(offset + visible, len(lines))}/{len(lines)}", width - 3, _cp(C_HEADER_BAR))
    screen.refresh()
    return offset
