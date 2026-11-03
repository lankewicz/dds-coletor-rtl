from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SystemdUnitTests(unittest.TestCase):
    def test_daily_history_timer_runs_at_0100_sao_paulo(self):
        timer = (ROOT / "rotalog-history.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 01:00:00 America/Sao_Paulo", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=rotalog-history.service", timer)

    def test_daily_history_service_runs_shared_collection_pipeline(self):
        service = (ROOT / "rotalog-history.service").read_text(encoding="utf-8")
        self.assertIn("-m scripts.rotalog_daily_pipeline", service)
        self.assertIn("TimeoutStartSec=0", service)
        self.assertIn("EnvironmentFile=/home/orangepi/dds-coletor-rtl/.env", service)

    def test_one_time_collection_remains_available_as_a_manual_service(self):
        service = (ROOT / "coleta-unica.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("User=orangepi", service)
        self.assertIn("EnvironmentFile=/home/orangepi/dds-coletor-rtl/.env", service)
        self.assertIn(
            "ExecStart=/usr/bin/env bash /home/orangepi/dds-coletor-rtl/coleta_unica/iniciar-orange.sh --sem-tui",
            service,
        )
        self.assertIn("TimeoutStartSec=0", service)

    def test_monthly_recovery_runs_daily_at_0300(self):
        timer = (ROOT / "rotalog-monthly.timer").read_text(encoding="utf-8")
        service = (ROOT / "rotalog-monthly.service").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-10 01:00:00 America/Sao_Paulo", timer)
        self.assertIn("Unit=rotalog-history.service", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("--reconciliar-agendado --firebase", service)


if __name__ == "__main__":
    unittest.main()
