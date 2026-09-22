import unittest

from tui import _format_health


class TuiHealthTests(unittest.TestCase):
    def test_formats_complete_health_summary(self):
        lines = _format_health({
            "overall": "healthy",
            "temperatureC": 54.2,
            "disk": {"usedPercent": 31.4},
            "memory": {"availablePercent": 62.5},
            "loadAverage": [0.12, 0.2, 0.3],
            "uptimeSeconds": 3661,
            "collector": {
                "serviceActive": True,
                "lastIndexUploadAt": "2026-09-22T16:12:22Z",
            },
            "_heartbeatAt": "2026-09-22T16:15:00Z",
            "issues": [],
        })
        self.assertIn("Estado: SAUDAVEL", lines[0])
        self.assertIn("CPU: 54.2 C", lines[0])
        self.assertIn("Coletor: ATIVO", lines[1])
        self.assertIn("Uptime: 1h 01m 01s", lines[1])
        self.assertIn("Heartbeat local: 2026-09-22 16:15:00", lines[2])
        self.assertIn("Alertas: nenhum", lines[2])

    def test_formats_unavailable_platform_metrics(self):
        lines = _format_health({
            "overall": "warning",
            "disk": {},
            "memory": {},
            "collector": {"serviceActive": None},
            "issues": [{"code": "COLLECTOR_STATUS_UNKNOWN", "severity": "warning"}],
        })
        self.assertIn("Estado: ATENCAO", lines[0])
        self.assertIn("CPU: indisponivel", lines[0])
        self.assertIn("Coletor: DESCONHECIDO", lines[1])
        self.assertIn("COLLECTOR_STATUS_UNKNOWN", lines[2])


if __name__ == "__main__":
    unittest.main()
