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
            "cpuCount": 4,
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
        self.assertIn("Carga: 3.0%/5.0%/7.5%", lines[1])
        self.assertIn("Ultimo envio: 2026-09-22 13:12:22", lines[1])
        self.assertIn("Heartbeat local: 2026-09-22 13:15:00", lines[2])
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

    def test_ssd_capacity_and_physical_warning_are_distinct(self):
        lines = _format_health({
            "storage": {"mode": "ssd", "data_dir": "/mnt/ssd/data", "uuid": "uuid", "device": "/dev/sda1"},
            "disk": {"totalBytes": 220 * 1024**3, "freeBytes": 208 * 1024**3,
                     "model": "KINGSTON", "device": "/dev/sda", "smart": {
                         "status": "warning", "temperatureC": 38, "powerOnHours": 100,
                         "reallocatedSectors": 2, "crcErrors": 1}},
        })
        self.assertIn("Dados: SSD", lines[3])
        self.assertIn("208.0 GB / 220.0 GB", lines[3])
        self.assertIn("UUID: uuid", lines[4])
        self.assertIn("SMART: ATENCAO", lines[5])
        self.assertIn("38 C", lines[5])
        self.assertIn("Realocados: 2", lines[6])

    def test_missing_smart_is_not_reported_as_ok(self):
        lines = _format_health({"storage": {"mode": "ssd"}, "disk": {
            "smart": {"status": "unavailable", "reason": "smartctl nao instalado"}}})
        self.assertIn("SMART: INDISPONIVEL", lines[5])
        self.assertIn("smartctl nao instalado", lines[5])

    def test_viewer_resolves_storage_before_opening_logs(self):
        from unittest.mock import patch
        from pathlib import Path
        from coletor.data_storage import StorageSelection
        import tui
        selected = StorageSelection("ssd", "/mnt/ssd/data", "ChicoEletro", "uuid", "/mnt/ssd", "/dev/sda1")
        with patch("sys.argv", ["tui.py", "--view"]), \
             patch("tui.resolve_storage", return_value=selected) as resolve, \
             patch("tui.activate_storage"), patch("tui.assert_available"), \
             patch("tui._setup_tui_logging", return_value=Path("tui.log")) as logs, \
             patch("tui.curses") as terminal:
            self.assertEqual(0, tui.main())
        logs.assert_called_once_with(Path(selected.data_dir))
        self.assertFalse(resolve.call_args.kwargs["writable"])
        state = terminal.wrapper.call_args.args[2]
        self.assertEqual(selected.payload(), state.storage)


if __name__ == "__main__":
    unittest.main()
