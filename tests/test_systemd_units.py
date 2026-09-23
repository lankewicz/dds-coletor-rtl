from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SystemdUnitTests(unittest.TestCase):
    def test_daily_history_timer_runs_at_0530_sao_paulo(self):
        timer = (ROOT / "rotalog-history.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 05:30:00 America/Sao_Paulo", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=rotalog-history.service", timer)

    def test_daily_history_service_collects_yesterday_with_firebase(self):
        service = (ROOT / "rotalog-history.service").read_text(encoding="utf-8")
        self.assertIn("--historico ontem --firebase", service)
        self.assertIn("--output-dir /home/orangepi/dds-coletor-rtl/dados-local", service)
        self.assertIn("EnvironmentFile=/home/orangepi/dds-coletor-rtl/.env", service)


if __name__ == "__main__":
    unittest.main()
