from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from coletor.tui.controller import _curses_main
from coletor.tui.state import TuiState


class TuiControllerTests(unittest.TestCase):
    terminal = SimpleNamespace(curs_set=Mock(), KEY_DOWN=258, KEY_UP=259,
                               KEY_NPAGE=338, KEY_PPAGE=339, KEY_RESIZE=410)

    def run_keys(self, state, keys):
        screen = Mock()
        screen.getch.side_effect = keys + [ord("q")]
        with patch("coletor.tui.terminal.curses", self.terminal), \
             patch("coletor.tui.controller._init_colors"), \
             patch("coletor.tui.controller._render"), \
             patch("coletor.tui.controller.threading.Thread") as threads:
            _curses_main(screen, None, state, Path("dados-local"))
        self.assertTrue(state.stop.is_set())
        return threads

    def test_panel_and_detail_scrolling_remain_independent(self):
        state = TuiState(viewer_mode=True)
        self.run_keys(state, [258, 9, 338, ord("d"), 258, 27, 259])
        self.assertEqual(state.events_scroll, 1)
        self.assertEqual(state.teams_scroll, 9)
        self.assertEqual(state.details_scroll, 1)
        self.assertEqual(state.active_panel, "teams")
        self.assertFalse(state.show_details)

    def test_viewer_does_not_start_direct_collection_or_request_run(self):
        from coletor.tui.workers import _viewer_worker, _worker
        state = TuiState(viewer_mode=True)
        threads = self.run_keys(state, [ord("r")])
        targets = [call.kwargs["target"] for call in threads.call_args_list]
        self.assertIn(_viewer_worker, targets)
        self.assertNotIn(_worker, targets)
        self.assertFalse(state.run_now.is_set())

    def test_active_mode_keeps_run_now_shortcut(self):
        state = TuiState(viewer_mode=False)
        self.run_keys(state, [ord("r")])
        self.assertTrue(state.run_now.is_set())

    def test_e_opens_teams_and_escape_returns(self):
        state = TuiState(viewer_mode=True)
        self.run_keys(state, [ord("e"), 338])
        self.assertTrue(state.teams_expanded)
        self.assertEqual(state.active_panel, "teams")
        self.assertEqual(state.overview_scroll[0], 10)
        state = TuiState(viewer_mode=True)
        self.run_keys(state, [ord("e"), 27])
        self.assertFalse(state.teams_expanded)
        self.assertEqual(state.active_panel, "events")

    def test_tab_selects_overview_blocks_with_independent_scroll(self):
        state = TuiState(viewer_mode=True)
        self.run_keys(state, [ord("e"), 258, 9, 338])
        self.assertTrue(state.teams_expanded)
        self.assertEqual(state.overview_group, 1)
        self.assertEqual(state.overview_scroll[:2], [1, 10])
        self.assertEqual(state.teams_scroll, 0)
