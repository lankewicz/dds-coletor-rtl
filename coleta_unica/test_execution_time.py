import json
import tempfile
import unittest
from pathlib import Path

from coleta_unica.execution_time import ExecutionTime


class ExecutionTimeTests(unittest.TestCase):
    def test_sessions_add_active_time_and_pauses_without_downtime(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tempo.json'
            first = ExecutionTime(path)
            first.document['duracoesDias'] = [90]
            first.checkpoint(100, 130, status='interrompida', force=True, closed=True)
            second = ExecutionTime(path)
            self.assertEqual(second.previous_active, 100)
            self.assertEqual(second.previous_elapsed, 130)
            self.assertEqual(second.document['duracoesDias'], [90])
            second.checkpoint(50, 60, status='Concluído', force=True, closed=True)
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved['ativoTotalSegundos'], 150)
            self.assertEqual(saved['decorridoTotalSegundos'], 190)
            self.assertEqual(saved['pausaTotalSegundos'], 40)
            self.assertEqual(len(saved['sessoes']), 2)
            self.assertTrue(all(s['fim'] for s in saved['sessoes']))

    def test_crash_keeps_only_time_confirmed_by_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tempo.json'
            first = ExecutionTime(path)
            first.checkpoint(20, 25, force=True)
            resumed = ExecutionTime(path)
            self.assertEqual(resumed.previous_active, 20)
            self.assertEqual(resumed.document['sessoes'][0]['status'], 'interrompida sem fechamento')
            resumed.checkpoint(5, 5, force=True)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['ativoTotalSegundos'], 25)

    def test_invalid_history_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tempo.json'
            path.write_text('{broken', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                ExecutionTime(path)
            self.assertEqual(path.read_text(encoding='utf-8'), '{broken')
