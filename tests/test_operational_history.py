import json
from pathlib import Path
import tempfile
import unittest

from coletor.storage import merge_daily_document
from main import LocalRotalogRunner


class OperationalHistoryTests(unittest.TestCase):
    def test_overnight_shift_keeps_next_day_service_in_opening_day_file(self):
        current = {
            "equipe": "E3733",
            "empresa": "Empresa",
            "updatedAtIso": "2026-09-25T04:05:00-03:00",
            "turno": {
                "inicio_iso": "2026-09-24T09:00:00-03:00",
                "fim_iso": "2026-09-25T04:00:00-03:00",
                "classificacao": "FECHADO",
            },
            "ssExecutadas": [{
                "protocolo": "50961067",
                "categoria": "EMERGENCIA",
                "status": "CONCLUSAO",
                "inicioDeslocamento": "2026-09-25T01:10:00-03:00",
                "fimExecucao": "2026-09-25T02:10:00-03:00",
                "retorno": "2026-09-25T02:15:00-03:00",
            }],
        }

        merged = merge_daily_document({}, current, "2026-09-24")

        self.assertEqual(merged["operationalDate"], "2026-09-24")
        self.assertEqual(len(merged["services"]), 1)
        self.assertEqual(merged["services"][0]["calendarDates"], ["2026-09-25"])
        self.assertEqual(merged["services"][0]["operationalDate"], "2026-09-24")

    def test_new_shift_does_not_copy_service_from_previous_overnight_shift(self):
        current = {
            "equipe": "E3733",
            "empresa": "Empresa",
            "updatedAtIso": "2026-09-25T15:10:00-03:00",
            "turno": {"inicio_iso": "2026-09-25T15:01:00-03:00", "aberto": True},
            "ssExecutadas": [{
                "protocolo": "50961067",
                "status": "CONCLUSAO",
                "inicioDeslocamento": "2026-09-25T01:10:00-03:00",
                "retorno": "2026-09-25T02:15:00-03:00",
            }],
        }

        merged = merge_daily_document({}, current, "2026-09-25")

        self.assertEqual(merged["services"], [])

    def test_daily_local_file_is_indented_plain_json(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LocalRotalogRunner(Path(directory), "Empresa")
            path = runner._daily_path("2026-09-25", "E3733")
            from coletor.storage import write_json
            write_json(path, {"teamKey": "E3733", "services": []})

            raw = path.read_text(encoding="utf-8")
            self.assertTrue(path.name.endswith(".json"))
            self.assertIn("\n  \"teamKey\"", raw)
            self.assertEqual(json.loads(raw)["teamKey"], "E3733")


if __name__ == "__main__":
    unittest.main()
