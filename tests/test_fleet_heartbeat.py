import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import fleet_agent


class HeartbeatTests(unittest.TestCase):
    def store(self):
        store = Mock()
        store.store.bucket_name = "bucket"
        store.store.blob_name = "index"
        return store

    @patch("fleet_agent.collect_health", return_value={"ok": True})
    @patch("fleet_agent.write_json")
    @patch("fleet_agent.load_json")
    def test_recent_heartbeat_suppresses_new_heartbeat(self, read, write, health):
        now = datetime.now(timezone.utc).isoformat()
        read.side_effect = [{}, {
            "nodeId": "pi",
            "bucket": "bucket",
            "blob": "index",
            "heartbeatAt": now,
            "remoteHeartbeatAt": now,
        }]
        store = self.store()
        fleet_agent.publish_heartbeat(store, "pi")
        store.save.assert_not_called()
        write.assert_not_called()

    @patch("fleet_agent.collect_health", return_value={"ok": True})
    @patch("fleet_agent.current_commit", return_value="abc")
    @patch("fleet_agent.write_json")
    @patch("fleet_agent.load_json", return_value={})
    def test_failed_remote_heartbeat_preserves_local_health(self, read, write, commit, health):
        store = self.store()
        store.save.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            fleet_agent.publish_heartbeat(store, "pi")
        write.assert_called_once()

    @patch("fleet_agent.collect_health", return_value={"ok": True})
    @patch("fleet_agent.current_commit", return_value="abc")
    @patch("fleet_agent.write_json")
    @patch("fleet_agent.load_json")
    def test_index_receipt_does_not_suppress_periodic_heartbeat(self, read, write, commit, health):
        read.side_effect = [{"nodeId": "other", "bucket": "bucket", "blob": "index",
                             "uploadedAt": datetime.now(timezone.utc).isoformat()}, {}]
        store = self.store()
        fleet_agent.publish_heartbeat(store, "pi")
        store.save.assert_called_once()
        self.assertEqual(2, write.call_count)

    def test_heartbeat_intervals_follow_operational_window(self):
        peak = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
        off_peak = datetime(2026, 9, 22, 3, tzinfo=timezone.utc)
        self.assertEqual(1800, fleet_agent.heartbeat_interval_seconds(peak))
        self.assertEqual(7200, fleet_agent.heartbeat_interval_seconds(off_peak))
        self.assertEqual(120, fleet_agent.local_heartbeat_interval_seconds())

    def test_health_summary_classifies_critical_temperature(self):
        health = fleet_agent.evaluate_health({
            "temperatureC": 86.0,
            "disk": {"usedPercent": 40},
            "memory": {"totalBytes": 100, "availableBytes": 50},
            "collector": {"serviceActive": True},
            "rebootRequired": False,
        })
        self.assertEqual("critical", health["overall"])
        self.assertEqual("TEMPERATURE_CRITICAL", health["issues"][0]["code"])

    def test_health_summary_reports_healthy_device(self):
        health = fleet_agent.evaluate_health({
            "temperatureC": 55.0,
            "disk": {"usedPercent": 40},
            "memory": {"totalBytes": 100, "availableBytes": 50},
            "collector": {"serviceActive": True},
            "rebootRequired": False,
        })
        self.assertEqual("healthy", health["overall"])
        self.assertEqual([], health["issues"])

    @patch.dict("os.environ", {}, clear=False)
    @patch("fleet_agent.FleetStore.from_environment")
    @patch("fleet_agent.publish_heartbeat")
    def test_commands_are_paused_by_default(self, heartbeat, store_factory):
        store = store_factory.return_value

        self.assertEqual(0, fleet_agent.run_once())

        heartbeat.assert_called_once()
        store.pending_commands.assert_not_called()
