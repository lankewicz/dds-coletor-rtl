import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from coletor.historico_estado import MonthlyCollectionState


class MonthlyCollectionStateTests(unittest.TestCase):
    def test_estado_persiste_sucessos_falhas_e_tentativas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "estado.json"
            state = MonthlyCollectionState.load(
                path, month="2026-02", company_key="empresa", timezone=ZoneInfo("America/Sao_Paulo")
            )
            state.record_attempt("2026-02-01")
            state.mark_success("2026-02-01")
            state.record_attempt("2026-02-02")
            state.mark_failure("2026-02-02", RuntimeError("offline"))
            state.save("pending_next_day", year=2026, month=2, total_days=28)

            restored = MonthlyCollectionState.load(
                path, month="2026-02", company_key="empresa", timezone=ZoneInfo("America/Sao_Paulo")
            )
            self.assertEqual({"2026-02-01"}, restored.completed_days)
            self.assertEqual(1, restored.attempts_by_day["2026-02-02"])
            self.assertEqual("offline", restored.errors_by_day["2026-02-02"])
            self.assertNotIn("2026-02-01", restored.pending_days(2026, 2, 28))
            self.assertIn("2026-02-02", restored.pending_days(2026, 2, 28))
            self.assertIsNotNone(json.loads(path.read_text(encoding="utf-8"))["nextRetryDate"])

    def test_estado_completo_remove_proxima_tentativa(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "estado.json"
            state = MonthlyCollectionState.load(
                path, month="2026-02", company_key="empresa", timezone=ZoneInfo("America/Sao_Paulo")
            )
            state.completed_days.update(
                f"2026-02-{day:02d}" for day in range(1, 29)
            )
            state.save("complete", year=2026, month=2, total_days=28)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([], payload["pendingDays"])
            self.assertIsNone(payload["nextRetryDate"])


if __name__ == "__main__":
    unittest.main()
