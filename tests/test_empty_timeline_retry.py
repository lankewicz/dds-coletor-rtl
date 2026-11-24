import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from main import LocalRotalogRunner
from coletor.parser import EmptyTimelineError, extrair_dados_tempo_real
from coletor.storage import write_json
from coletor.tui.state import TuiState
from coletor.tui.workers import _update_viewer_state_timing


class EmptyTimelineRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = LocalRotalogRunner(Path(self.temp.name), "EmpresaTeste")
        self.runner.processing_led = Mock()
        write_json(self.runner.index_path, {
            "company": "EmpresaTeste", "equipes": {"E3733": {"teamKey": "E3733"}},
        })

    def test_parser_identifies_empty_timeline(self):
        crawler = Mock()
        crawler.criar_sessao_autenticada.return_value.get.return_value = Mock(
            status_code=200, text='<script>PrimeFaces.cw("Timeline", {});</script>',
        )
        with self.assertRaises(EmptyTimelineError):
            extrair_dados_tempo_real(crawler=crawler)

    def test_six_local_probes_then_normal_cycle_and_repeat(self):
        runner = self.runner
        runner.firebase_store = Mock(enabled=True)
        runner.torre_inputs = Mock()
        runner.torre_composer = Mock()
        runner.torre_composer.publish.return_value = {"stale": True, "items": [], "events": []}
        original_index = runner.index_path.read_bytes()
        with patch("main.extrair_dados_tempo_real", side_effect=EmptyTimelineError("vazia")), \
                patch.object(runner, "_flush_daily_sync_queue", return_value={"uploaded": 0, "failed": 0, "pending": 1}) as flush:
            first = runner.run_once()
            self.assertEqual(first["retryDelaySeconds"], 30)
            flush.assert_called_once()
            runner.firebase_store.reset_mock()
            runner.torre_inputs.reset_mock()
            runner.torre_composer.reset_mock()
            flush.reset_mock()
            results = [runner.run_once() for _ in range(6)]
            self.assertEqual([r["retryDelaySeconds"] for r in results], [30] * 5 + [180])
            self.assertEqual([r["nextQuickRetry"] for r in results], [2, 3, 4, 5, 6, 0])
            self.assertTrue(all(r["quickRetry"] for r in results))
            flush.assert_not_called()
            self.assertEqual(runner.firebase_store.mock_calls, [])
            self.assertEqual(runner.torre_inputs.mock_calls, [])
            self.assertEqual(runner.torre_composer.mock_calls, [])
            self.assertEqual(runner.index_path.read_bytes(), original_index)
            next_cycle = runner.run_once()
            self.assertEqual(next_cycle["nextQuickRetry"], 1)
            flush.assert_called_once()
            runner.torre_inputs.sync.assert_called_once()
        records = [json.loads(line) for line in runner.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 8)
        self.assertEqual(records[6]["retryDelaySeconds"], 180)

    def test_valid_probe_resumes_processing_without_second_request(self):
        self.runner._empty_retry_count = 3
        with patch("main.extrair_dados_tempo_real", return_value=[{"equipe_codigo": "E3733", "is_online": True}]) as scrape:
            result = self.runner.run_once()
        self.assertEqual(result["status"], "success", result)
        scrape.assert_called_once()
        self.assertEqual(self.runner._empty_retry_count, 0)
        self.assertNotIn("retryDelaySeconds", result)

    def test_other_error_exits_quick_sequence(self):
        self.runner._empty_retry_count = 2
        with patch("main.extrair_dados_tempo_real", side_effect=OSError("rede")), \
                patch.object(self.runner, "_flush_daily_sync_queue") as flush:
            result = self.runner.run_once()
        self.assertEqual(result["errorCode"], "QUICK_RETRY_FAILED")
        self.assertEqual(self.runner._empty_retry_count, 0)
        self.assertNotIn("retryDelaySeconds", result)
        flush.assert_not_called()

    def test_empty_timeline_retries_even_without_initial_snapshot(self):
        self.runner.index_path.unlink()
        self.runner._empty_retry_count = 1
        with patch("main.extrair_dados_tempo_real", side_effect=EmptyTimelineError("vazia")):
            result = self.runner.run_once()
        self.assertEqual(result["retryDelaySeconds"], 30)
        self.assertEqual(result["nextQuickRetry"], 2)
        self.assertFalse(self.runner.index_path.exists())

    def test_viewer_uses_recorded_retry_delay(self):
        from datetime import datetime
        from coletor.tui.state import TZ
        state = TuiState(interval_seconds=180)
        finished = datetime.now(TZ)
        state.last_result = {"finishedAt": finished.isoformat(), "retryDelaySeconds": 30}
        _update_viewer_state_timing(state)
        self.assertEqual((state.next_run - finished).total_seconds(), 30)
