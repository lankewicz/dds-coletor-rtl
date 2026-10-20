"""Backend opcional de curses, paleta e primitivas de desenho."""
from __future__ import annotations

try:
    import curses
except ImportError:
    try:
        import windows_curses as curses
    except ImportError:
        curses = None

C_DEFAULT = 1
C_TITLE = 2
C_OK = 3
C_WARN = 4
C_ERROR = 5
C_STATUS_OK = 6
C_STATUS_WARN = 7
C_STATUS_ERR = 8
C_STATUS_RUN = 9
C_DIM = 10
C_ACCENT = 11
C_HEADER_BAR = 12


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
