"""Verifica o layout pelo mesmo renderizador usado no terminal."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tui


class ScreenError(Exception):
    pass


TERMINAL = SimpleNamespace(
    error=ScreenError, has_colors=lambda: True, color_pair=lambda value: value << 8,
    A_BOLD=1 << 16, ACS_ULCORNER="┌", ACS_URCORNER="┐", ACS_LLCORNER="└",
    ACS_LRCORNER="┘", ACS_HLINE="─", ACS_VLINE="│",
)


class Screen:
    def __init__(self, height=42, width=180):
        self.height, self.width = height, width
        self.erase()

    def erase(self):
        self.cells = [[(" ", 0) for _ in range(self.width)] for _ in range(self.height)]

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, row, column, text, attr=0):
        if row < 0 or row >= self.height or column < 0 or column + len(text) > self.width:
            raise AssertionError(f"Texto fora da tela: {row}, {column}, {text}")
        for offset, char in enumerate(text):
            self.cells[row][column + offset] = char, attr

    def addch(self, row, column, char, attr=0):
        self.addstr(row, column, char if isinstance(char, str) else chr(char), attr)

    def hline(self, row, column, char, length, attr=0):
        self.addstr(row, column, char * length, attr)

    def refresh(self):
        pass

    def text(self):
        return ["".join(char for char, _ in row).rstrip() for row in self.cells]


def sample_state():
    state = tui.TuiState(viewer_mode=True)
    state.last_result = {
        "status": "success", "startedAt": "2026-10-14T16:09:43-03:00",
        "finishedAt": "2026-10-14T16:10:18-03:00", "durationSeconds": 34.864,
        "scrapeDurationSeconds": 14.447, "totalTeams": 99, "updatedTeams": 17,
        "ignoredTeams": 82, "firebaseSyncStatus": "uploaded", "firebaseUploaded": True,
        "bytesUploaded": 256717, "dailySyncPending": 0, "dailySyncFailed": 0,
        "events": {"local": [{"team": f"E{3100 + i}", "action": "Correção de OS"} for i in range(36)],
                   "cloud": [{"team": "E4A20", "action": "51049390: Concluído"}]},
    }
    state.history = [state.last_result]
    state.storage = {"mode": "ssd", "data_dir": "/mnt/ssd/dds-coletor-rtl/chicoeletro/dados-local",
                     "uuid": "185b3d33-422b-493c-8d1a-ccd3b914f038", "device": "/dev/sda1"}
    state.device_health = {
        "overall": "healthy", "temperatureC": 65.8, "storage": state.storage,
        "memory": {"availablePercent": 46.3}, "issues": [],
        "disk": {"freeBytes": 207.7 * 1024**3, "totalBytes": 219 * 1024**3,
                 "usedPercent": 0.04, "model": "KINGSTON SA400S37240G", "device": "/dev/sda",
                 "smart": {"status": "passed", "temperatureC": 42, "powerOnHours": 11554,
                           "crcErrors": 0, "powerCycles": 1539}},
    }
    return state


class TuiLayoutTests(unittest.TestCase):
    def render(self, state, height=42, width=180):
        screen = Screen(height, width)
        with patch("tui.curses", TERMINAL):
            tui._render(screen, None, state)
        return screen.text()

    def test_compact_default_keeps_events_above_quarter_screen(self):
        rows = self.render(sample_state())
        event_row = next(i for i, row in enumerate(rows) if "EVENTOS DE EQUIPES" in row)
        self.assertLess(event_row, len(rows) / 4)
        self.assertIn("SMART OK", "\n".join(rows[:event_row]))
        self.assertNotIn("185b3d33", "\n".join(rows))
        self.assertIn("E4A20", "\n".join(rows))

    def test_failure_and_critical_health_remain_visible(self):
        state = sample_state()
        state.last_result.update(status="error", error="rede indisponivel", dailySyncFailed=2)
        state.device_health.update(overall="critical", issues=[{"code": "SSD_SMART_FAILING", "severity": "critical"}])
        state.device_health["disk"]["smart"]["status"] = "failing"
        top = "\n".join(self.render(state, height=24, width=75)[:9])
        self.assertIn("FALHA", top)
        self.assertIn("SMART FALHA", top)
        self.assertIn("rede indisponivel", top)

    def test_details_preserve_identity_and_counters(self):
        state = sample_state()
        state.show_details = True
        text = "\n".join(self.render(state))
        self.assertIn("185b3d33-422b-493c-8d1a-ccd3b914f038", text)
        self.assertIn("11554", text)
        self.assertIn("Erros CRC: 0", text)
        self.assertIn("raspagem", text.lower())
        self.assertNotIn("EVENTOS DE EQUIPES", text)

    def test_narrow_details_wrap_and_clamp_scroll(self):
        state = sample_state()
        state.show_details = True
        state.details_scroll = 10000
        rows = self.render(state, height=18, width=75)
        self.assertLess(state.details_scroll, 100)
        self.assertIn("execucoes.jsonl", "\n".join(rows))

    def test_minimum_screen_retains_events_and_quit_key(self):
        text = "\n".join(self.render(sample_state(), height=18, width=75))
        self.assertIn("EVENTOS DE EQUIPES", text)
        self.assertIn("[q] Sair", text)


if __name__ == "__main__":
    unittest.main()
