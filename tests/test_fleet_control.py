import unittest
from datetime import datetime, timedelta, timezone

from coletor.fleet_control import FleetPaths, FleetStore, normalize_commit


class FakeFleetStore(FleetStore):
    def __init__(self, payloads):
        self.paths = FleetPaths("dados/teste/rotalog")
        self.payloads = payloads

    def load(self, path):
        return self.payloads.get(path, {})

    def list_payloads(self, prefix):
        marker = prefix.rstrip("/") + "/"
        return [value for key, value in self.payloads.items() if key.startswith(marker)]


class FleetControlTests(unittest.TestCase):
    def test_normalize_commit_accepts_short_and_full_hashes(self):
        self.assertEqual("8f93d814", normalize_commit("8F93D814"))
        full = "a" * 40
        self.assertEqual(full, normalize_commit(full, allow_short=False))

    def test_normalize_commit_rejects_invalid_hash(self):
        with self.assertRaises(ValueError):
            normalize_commit("main")
        with self.assertRaises(ValueError):
            normalize_commit("abc123")

    def test_active_nodes_ignores_stale_heartbeats(self):
        paths = FleetPaths("dados/teste/rotalog")
        now = datetime.now(timezone.utc)
        store = FakeFleetStore({
            paths.node("orange-01"): {
                "nodeId": "orange-01",
                "heartbeatAt": now.isoformat(),
            },
            paths.node("orange-02"): {
                "nodeId": "orange-02",
                "heartbeatAt": (now - timedelta(minutes=10)).isoformat(),
            },
        })
        nodes = store.active_nodes(max_age_seconds=180)
        self.assertEqual(["orange-01"], [node["nodeId"] for node in nodes])

    def test_pending_command_skips_completed_result(self):
        paths = FleetPaths("dados/teste/rotalog")
        command = {
            "commandId": "deploy-1",
            "action": "deploy",
            "target": "orange-01",
            "requestedAt": datetime.now(timezone.utc).isoformat(),
            "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        store = FakeFleetStore({
            paths.command("deploy-1"): command,
            paths.result("deploy-1", "orange-01"): {"status": "healthy"},
        })
        self.assertEqual([], store.pending_commands("orange-01"))

    def test_command_can_target_all_or_one_node(self):
        self.assertTrue(FleetStore.command_targets_node({"target": "all"}, "orange-01"))
        self.assertTrue(FleetStore.command_targets_node({"target": "orange-01"}, "orange-01"))
        self.assertFalse(FleetStore.command_targets_node({"target": "orange-02"}, "orange-01"))


if __name__ == "__main__":
    unittest.main()
