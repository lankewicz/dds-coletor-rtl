from datetime import datetime
import unittest

from coletor.tui.overview import _overview_groups
from tests.test_tui_layout import Screen, TERMINAL, sample_state
from unittest.mock import patch
from coletor.tui.view import _render


class OverviewTests(unittest.TestCase):
    def test_exclusive_current_states_and_separate_completion_history(self):
        now = datetime.fromisoformat("2026-10-20T10:00:00-03:00")
        snapshots = {
            "E2001": {"jornada": {"turno": {"status": "ABERTO"}},
                      "ordensServico": {"atual": {"statusAtual": "EXECUCAO", "inicioExecucao": "2026-10-20T09:00:00-03:00"}}},
            "E2002": {"jornada": {"turno": {"status": "ABERTO", "inicio": "2026-10-20T08:00:00-03:00"}}, "fila": {"comercial": 3, "emergencia": 1}},
            "E2003": {"jornada": {"emIntervalo": True, "turno": {"status": "ABERTO"}, "intervaloAtual": {"inicio": "2026-10-20T09:20:00-03:00"}}},
            "E2004": {"jornada": {"turno": {"status": "FECHADO"}}},
            "E2005": {"jornada": {"turno": {"status": "ABERTO"}},
                      "ordensServico": {"atual": {"statusAtual": "DESLOCAMENTO", "inicioDeslocamento": "2026-10-20T09:30:00-03:00"}}},
        }
        event = {"id": "conclusion1", "teamKey": "E2001", "status": "CONCLUSAO", "occurredAt": "2026-10-20T08:30:00-03:00"}
        events = [event, event, {**event, "id": "future", "occurredAt": "2026-10-20T15:00:00-03:00"},
                  {**event, "id": "expired", "occurredAt": "2026-10-18T15:00:00-03:00"}]
        groups = _overview_groups(snapshots, events, now)
        self.assertEqual([[row[0] for row in group] for group in groups],
                         [["E2005"], ["E2001"], ["E2003"], ["E2002"], ["E2004"], ["E2001"]])
        self.assertEqual(groups[3][0][2:4], ("08:00:00", "3/1"))

    def test_small_overview_shows_selected_group_and_empty_feedback(self):
        state = sample_state()
        state.teams_expanded = True
        state.active_panel = "teams"
        state.overview_group = 2
        screen = Screen(24, 80)
        with patch("coletor.tui.terminal.curses", TERMINAL):
            _render(screen, None, state)
        text = "\n".join(screen.text())
        self.assertIn("INTERVALO (0)", text)
        self.assertIn("Nenhum registro", text)
        self.assertNotIn("EVENTOS DE EQUIPES", text)
        self.assertIn("[e] Voltar", text)
