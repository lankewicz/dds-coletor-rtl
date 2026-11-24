from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from coletor.storage import write_json
from coletor.tui.daily import daily_rows, load_latest_daily, use_daily
from coletor.tui.state import TZ


class DailyFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 24, 10, tzinfo=TZ)

    def save(self, day="2026-11-23", **overrides):
        payload = {
            "data": day, "empresa": "EmpresaTeste",
            "collectedAt": "2026-11-24T05:00:00-03:00",
            "resumoEquipes": {"registros": [
                {"equipe": "E3733", "servicosExecutados": 5, "kmInformado": 100, "kmAutorizadoFinal": 90},
                {"equipe": "E3733", "servicosExecutados": 2, "kmInformado": 20, "kmAutorizadoFinal": 18},
            ]},
            **overrides,
        }
        path = self.root / "rotalog/quilometragem/diario" / f"{day}.json.gz"
        write_json(path, payload)
        return path

    def test_official_metrics_sum_shifts_without_using_service_totals(self):
        self.save(totaisPorEquipe={"E3733": {"kmInformado": 999, "kmAutorizadoFinal": 999}})
        daily, error = load_latest_daily(self.root, "EmpresaTeste", self.now)
        self.assertIsNone(error)
        self.assertEqual(daily["rows"], [("E3733", 7, 120, 108)])
        self.assertFalse(daily["outdated"])

    def test_latest_reference_wins_over_more_recent_recollection_of_old_day(self):
        self.save()
        self.save("2026-11-22", collectedAt="2026-11-24T09:00:00-03:00")
        daily, _ = load_latest_daily(self.root, "EmpresaTeste", self.now)
        self.assertEqual(daily["data"], "2026-11-23")

    def test_corrupt_latest_uses_older_valid_day_and_reports_warning(self):
        self.save("2026-11-22")
        self.save().write_bytes(b"corrupt")
        daily, error = load_latest_daily(self.root, "EmpresaTeste", self.now)
        self.assertEqual(daily["data"], "2026-11-22")
        self.assertTrue(daily["outdated"])
        self.assertIn("2026-11-23", error)

    def test_other_company_today_future_collection_and_missing_metrics_are_rejected(self):
        self.save(empresa="OutraEmpresa")
        self.save("2026-11-24")
        self.save("2026-11-22", collectedAt="2026-11-25T05:00:00-03:00")
        self.save("2026-11-21", resumoEquipes={})
        daily, error = load_latest_daily(self.root, "EmpresaTeste", self.now)
        self.assertEqual(daily, {})
        self.assertIsNotNone(error)

    def test_missing_daily_does_not_replace_stale_realtime(self):
        self.assertEqual(load_latest_daily(self.root, "EmpresaTeste", self.now)[0], {})
        self.assertFalse(use_daily({"daily_snapshot": {}}, {"status": "error"}, self.now))

    def test_legacy_services_and_km_are_supported(self):
        rows = daily_rows({"totaisPorEquipe": {"E3733": {"kmInformado": 20, "kmAutorizadoFinal": 18}},
                           "servicos": [{"equipe": "E3733"}, {"equipe": "E3733"}]})
        self.assertEqual(rows, [("E3733", 2, 20, 18)])

    def test_automatic_daily_selection_and_return_to_valid_realtime(self):
        data = {"daily_snapshot": {"data": "2026-11-23"}, "teams_error": None,
                "teams_collected_at": self.now.isoformat(), "teams_stale_seconds": 450}
        self.assertTrue(use_daily(data, {"status": "error"}, self.now))
        self.assertFalse(use_daily(data, {"status": "success"}, self.now))
        data["teams_collected_at"] = "2026-11-22T10:00:00-03:00"
        self.assertTrue(use_daily(data, {"status": "success"}, self.now))
