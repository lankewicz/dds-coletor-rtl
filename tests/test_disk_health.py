import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from coletor.data_storage import StorageSelection, StorageUnavailable
from coletor.disk_health import collect_disk_health, parse_smart, read_smart
from fleet_agent import evaluate_health


class DiskHealthTests(unittest.TestCase):
    def test_nonzero_exit_keeps_physical_failure(self):
        smart = parse_smart({"smart_status": {"passed": False},
                             "temperature": {"current": 44}}, 8)
        self.assertEqual("failing", smart["status"])
        self.assertEqual(44, smart["temperatureC"])
        health = evaluate_health({"disk": {"smart": smart}, "collector": {"serviceActive": True}})
        self.assertEqual("critical", health["overall"])
        self.assertEqual("SSD_SMART_FAILING", health["issues"][0]["code"])

    def test_history_errors_are_warning_not_unavailable(self):
        smart = parse_smart({"smart_status": {"passed": True}}, 64)
        self.assertEqual("warning", smart["status"])

    def test_partial_failed_command_never_claims_ok(self):
        smart = parse_smart({"smart_status": {"passed": True}}, 4)
        self.assertEqual("unavailable", smart["status"])

    def test_pending_sector_overrides_passed(self):
        smart = parse_smart({"smart_status": {"passed": True}, "ata_smart_attributes": {
            "table": [{"id": 197, "raw": {"value": 3}},
                      {"id": 231, "value": 96, "raw": {"value": 0}}]}}, 0)
        self.assertEqual("warning", smart["status"])
        self.assertEqual(3, smart["pendingSectors"])
        self.assertNotIn("wearUsedPercent", smart)

    def test_nvme_wear_and_media_errors(self):
        smart = parse_smart({"smart_status": {"passed": True}, "nvme_smart_health_information_log": {
            "percentage_used": 102, "media_errors": 5}}, 0)
        self.assertEqual("warning", smart["status"])
        self.assertEqual(102, smart["wearUsedPercent"])
        self.assertEqual(5, smart["mediaErrors"])

    @patch("coletor.disk_health.shutil.which", return_value=None)
    def test_missing_binary(self, which):
        self.assertEqual("smartctl nao instalado", read_smart("/dev/sda")["reason"])

    @patch.dict("os.environ", {"DDS_SMART_DEVICE_TYPE": "sat"})
    @patch("coletor.disk_health.shutil.which", return_value="/usr/sbin/smartctl")
    @patch("coletor.disk_health.subprocess.run")
    def test_read_only_command_and_nonzero_exit(self, run, which):
        run.return_value = Mock(stdout=json.dumps({"smart_status": {"passed": False}}), returncode=8)
        self.assertEqual("failing", read_smart("/dev/sda")["status"])
        self.assertEqual(["/usr/sbin/smartctl", "-j", "-a", "-d", "sat", "/dev/sda"], run.call_args.args[0])
        self.assertEqual(8, run.call_args.kwargs["timeout"])

    @patch("coletor.disk_health.shutil.which", return_value="smartctl")
    @patch("coletor.disk_health.subprocess.run", side_effect=subprocess.TimeoutExpired("smartctl", 8))
    def test_timeout_keeps_health_available(self, run, which):
        self.assertEqual("unavailable", read_smart("/dev/sda")["status"])

    @patch.dict("os.environ", {"DDS_SMART_USE_SUDO": "true", "DDS_SMART_DEVICE_TYPE": ""})
    @patch("coletor.disk_health.shutil.which", side_effect=["/usr/sbin/smartctl", "/usr/bin/sudo"])
    @patch("coletor.disk_health.subprocess.run")
    def test_sudo_opt_in_is_noninteractive_and_read_only(self, run, which):
        run.return_value = Mock(stdout=json.dumps({"smart_status": {"passed": True}}), returncode=0)
        self.assertEqual("passed", read_smart("/dev/sda")["status"])
        self.assertEqual(["/usr/bin/sudo", "-n", "/usr/sbin/smartctl", "-j", "-a", "/dev/sda"], run.call_args.args[0])

    @patch.dict("os.environ", {"DDS_SMART_USE_SUDO": "true", "DDS_SMART_DEVICE_TYPE": ""})
    @patch("coletor.disk_health.shutil.which", side_effect=["/usr/sbin/smartctl", "/usr/bin/sudo"])
    @patch("coletor.disk_health.subprocess.run", return_value=Mock(stdout="", returncode=1))
    def test_sudo_denied_returns_unavailable_without_retry(self, run, which):
        result = read_smart("/dev/sda")
        self.assertEqual("unavailable", result["status"])
        self.assertIn("sudoers", result["reason"])
        run.assert_called_once()

    @patch.dict("os.environ", {"DDS_SMART_USE_SUDO": "true", "DDS_SMART_DEVICE_TYPE": ""})
    @patch("coletor.disk_health.shutil.which", side_effect=["/usr/sbin/smartctl", None])
    @patch("coletor.disk_health.subprocess.run")
    def test_sudo_missing_does_not_execute(self, run, which):
        self.assertEqual("sudo nao instalado", read_smart("/dev/sda")["reason"])
        run.assert_not_called()

    @patch("coletor.disk_health.shutil.disk_usage")
    @patch("coletor.disk_health.assert_available", side_effect=StorageUnavailable("SSD retirado"))
    def test_removed_disk_does_not_measure_card_mountpoint(self, check, usage):
        storage = StorageSelection("ssd", "/mnt/ssd/data", "ChicoEletro", "uuid", "/mnt/ssd", "/dev/sda1")
        self.assertEqual("storage_unavailable", collect_disk_health(storage)["error"])
        usage.assert_not_called()

    @patch("coletor.disk_health.read_smart")
    @patch("coletor.disk_health.shutil.disk_usage", return_value=Mock(total=1000, used=100, free=900))
    def test_card_capacity_does_not_probe_smart(self, usage, smart):
        health = collect_disk_health(StorageSelection("card", "dados-local", "ChicoEletro"))
        self.assertEqual(10, health["usedPercent"])
        self.assertEqual(900, health["freeBytes"])
        smart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
