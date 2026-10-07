import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from main import LocalRotalogRunner
from coletor.storage import load_json, write_json


class ConsolidatedDailyTests(unittest.TestCase):
    def test_full_documents_are_saved_before_individual_files(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LocalRotalogRunner(Path(directory), "EmpresaTeste")
            teams = [{"equipe_codigo": "E3733", "is_online": True},
                     {"equipe_codigo": "E9999", "is_online": False}]
            original_write = write_json
            writes = []

            def record_write(path, value, *args, **kwargs):
                writes.append(path)
                return original_write(path, value, *args, **kwargs)

            with patch("main.extrair_dados_tempo_real", return_value=teams), \
                    patch("main.write_json", side_effect=record_write):
                result = runner.run_once()
            self.assertEqual(result["status"], "success", result)
            consolidated_path = Path(result["localConsolidatedDaily"])
            self.assertEqual(consolidated_path.suffix, ".json")
            self.assertEqual(json.loads(consolidated_path.read_text(encoding="utf-8"))["totalEquipes"], 2)
            payload = load_json(consolidated_path, {})
            self.assertEqual(payload["totalEquipes"], 2)
            for key, document in payload["equipes"].items():
                individual = runner._daily_path(document["operationalDate"], key)
                self.assertEqual(document, load_json(individual, {}))
                self.assertIn("historico", document["ordensServico"])
                self.assertIn("turnos", document["jornada"])
                self.assertLess(writes.index(consolidated_path), writes.index(individual))
            self.assertFalse(runner.daily_sync_queue_path.exists())

    def test_missing_teams_restart_and_day_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = LocalRotalogRunner(root, "EmpresaTeste")
            first = runner._save_consolidated_daily("2026-10-07", "08:00", {
                "E3733": {"services": [{"protocolo": "123"}]},
                "E9999": {"services": [{"protocolo": "456"}]},
            })
            restarted = LocalRotalogRunner(root, "EmpresaTeste")
            restarted._save_consolidated_daily("2026-10-07", "09:00", {
                "E3733": {"services": [{"protocolo": "123", "status": "CONCLUSAO"}]},
            })
            saved = load_json(first, {})
            self.assertEqual(saved["totalEquipes"], 2)
            self.assertEqual(saved["equipes"]["E9999"]["services"][0]["protocolo"], "456")
            second = restarted._save_consolidated_daily("2026-10-08", "00:01", {})
            self.assertNotEqual(first, second)
            self.assertEqual(load_json(second, {})["totalEquipes"], 0)
            self.assertEqual(load_json(first, {}), saved)

    def test_corrupt_or_foreign_archive_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LocalRotalogRunner(Path(directory), "EmpresaTeste")
            path = runner._save_consolidated_daily("2026-10-07", "08:00", {})
            for contents in (b"invalid json",):
                path.write_bytes(contents)
                with self.assertRaises(RuntimeError):
                    runner._save_consolidated_daily("2026-10-07", "09:00", {})
                self.assertEqual(path.read_bytes(), contents)
            write_json(path, {"company": "OutraEmpresa", "equipes": {}})
            contents = path.read_bytes()
            with self.assertRaises(RuntimeError):
                runner._save_consolidated_daily("2026-10-07", "09:00", {})
            self.assertEqual(path.read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
