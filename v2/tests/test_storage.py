import gzip
from pathlib import Path
import tempfile
import unittest

from rotalog_v2.storage import LocalSnapshotStore, read_json_gzip


class StorageTests(unittest.TestCase):
    def test_saves_raw_archive_and_current_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSnapshotStore(Path(directory))
            snapshot = {
                "schemaVersion": 1,
                "collectedAt": "2026-09-24T10:15:00-03:00",
                "events": [
                    {"index": 0, "group": "E3733-CA127 NOME", "content": "T"},
                    {"index": 1, "group": "veiculo?-CA078 NOME", "content": "T"},
                ],
                "teams": {
                    "E3733": {
                        "currentActivity": None,
                        "completedServices": [],
                        "unclassifiedEvents": [],
                    },
                    "CA078": {
                        "currentActivity": None,
                        "completedServices": [],
                        "unclassifiedEvents": [1],
                    },
                },
            }
            changes = {
                "E3733": [{"kind": "SHIFT_OPENED"}],
                "CA078": [{"kind": "SERVICE_NEW", "message": "12345678: NOVO -> CONCLUSÃO 10:15"}],
            }
            paths = store.save(
                "<html>á</html>", snapshot, day="2026-09-24", stamp="101500", changes=changes
            )
            self.assertEqual(gzip.decompress(paths["raw"].read_bytes()).decode("utf-8"), "<html>á</html>")
            self.assertEqual(read_json_gzip(paths["archive"]), snapshot)
            self.assertEqual(read_json_gzip(paths["current"]), snapshot)
            tower = read_json_gzip(paths["controlTower"])
            self.assertEqual(tower["teamCount"], 2)
            self.assertNotIn("events", tower)
            self.assertEqual(tower["shiftServices"], {"completed": 0, "active": 0})
            self.assertEqual(tower["queue"], {"emergency": 0, "commercial": 0})
            self.assertEqual(tower["teams"]["E3733"]["queue"], {"emergency": 0, "commercial": 0})
            team_current = Path(directory) / "rotalog/teams/current/E3733.json.gz"
            team_archive = Path(directory) / "rotalog/teams/snapshots/2026-09-24/E3733/101500.json.gz"
            self.assertTrue(team_current.exists())
            self.assertTrue(team_archive.exists())
            self.assertEqual(read_json_gzip(team_current)["events"][0]["content"], "T")
            ca_current = Path(directory) / "rotalog/teams/current/CA078.json.gz"
            self.assertTrue(ca_current.exists())
            self.assertEqual(read_json_gzip(ca_current)["teamKey"], "CA078")
            self.assertEqual(read_json_gzip(ca_current)["eventCount"], 1)
            self.assertEqual(paths["teamFilesWritten"], 4)

    def test_does_not_write_team_without_allowed_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSnapshotStore(Path(directory))
            snapshot = {
                "events": [],
                "teams": {"E3733": {"completedServices": [], "currentActivity": None}},
            }
            paths = store.save("html", snapshot, day="2026-09-24", stamp="120000", changes={})
            self.assertEqual(paths["teamFilesWritten"], 0)
            self.assertFalse((Path(directory) / "rotalog/teams/current/E3733.json.gz").exists())

    def test_team_history_only_adds_new_services(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSnapshotStore(Path(directory))

            def make_snapshot(collected_at, services):
                return {
                    "schemaVersion": 1,
                    "collectedAt": collected_at,
                    "events": [],
                    "teams": {"E3733": {
                        "completedServices": services,
                        "currentActivity": None,
                        "counts": {"completed": len(services)},
                    }},
                }

            first = make_snapshot("2026-09-24T10:00:00-03:00", [
                {"protocol": "50900001", "status": "COMPLETED", "returnTime": "09:00"},
            ])
            store.save(
                "html", first, day="2026-09-24", stamp="100000",
                changes={"E3733": [{"kind": "SERVICE_NEW", "message": "50900001: NOVO -> CONCLUSÃO"}]},
            )
            second = make_snapshot("2026-09-24T11:00:00-03:00", [
                {"protocol": "50900002", "status": "COMPLETED", "returnTime": "10:30"},
            ])
            store.save(
                "html", second, day="2026-09-24", stamp="110000",
                changes={"E3733": [{"kind": "SERVICE_NEW", "message": "50900002: NOVO -> CONCLUSÃO"}]},
            )
            document = read_json_gzip(Path(directory) / "rotalog/teams/current/E3733.json.gz")
            protocols = [item["protocol"] for item in document["team"]["completedServices"]]
            self.assertEqual(protocols, ["50900001", "50900002"])
            self.assertEqual(document["historyServiceCount"], 2)
            self.assertEqual(len(document["writeHistory"]), 2)

    def test_queue_history_distinguishes_idle_from_waiting_with_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSnapshotStore(Path(directory))

            def snap(at, pending):
                return {
                    "collectedAt": at,
                    "events": [],
                    "teams": {"E3733": {
                        "state": "AVAILABLE", "online": True,
                        "currentActivity": None,
                        "completedServices": [],
                        "pendingServices": pending,
                        "counts": {},
                    }},
                }

            store.save("html", snap("2026-09-24T10:00:00-03:00", []), day="2026-09-24", stamp="100000")
            store.save("html", snap("2026-09-24T10:03:00-03:00", []), day="2026-09-24", stamp="100300")
            queued = [{"sequence": "1", "category": "COMERCIAL"}]
            paths = store.save("html", snap("2026-09-24T10:06:00-03:00", queued), day="2026-09-24", stamp="100600")
            history = read_json_gzip(paths["queueHistory"])["teams"]["E3733"]
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["availability"], "IDLE_NO_QUEUE")
            self.assertEqual(history[0]["firstObservedAt"], "2026-09-24T10:00:00-03:00")
            self.assertEqual(history[0]["lastObservedAt"], "2026-09-24T10:03:00-03:00")
            self.assertEqual(history[1]["availability"], "WAITING_WITH_QUEUE")
            self.assertEqual(history[1]["pendingCommercial"], 1)

    def test_control_tower_counts_distinct_services_in_current_shift(self):
        snapshot = {
            "collectedAt": "2026-09-24T12:00:00-03:00",
            "teams": {"E3733": {
                "shift": {"status": "OPEN", "openedAt": "2026-09-24T08:00:00-03:00"},
                "completedServices": [
                    {"protocol": "50900000", "startAt": "2026-09-24T07:30:00-03:00"},
                    {"protocol": "50900001", "startAt": "2026-09-24T09:00:00-03:00"},
                    {"protocol": "50900001", "startAt": "2026-09-24T09:10:00-03:00"},
                ],
                "currentActivity": {"protocol": "50900002"},
                "counts": {"pendingEmergency": 7, "pendingCommercial": 2},
            }},
        }
        tower = LocalSnapshotStore._control_tower(snapshot)
        self.assertEqual(tower["shiftServices"], {"completed": 1, "active": 1})
        self.assertEqual(tower["teams"]["E3733"]["shiftServices"], tower["shiftServices"])
        self.assertEqual(tower["teams"]["E3733"]["queue"], {"emergency": 7, "commercial": 2})
        self.assertEqual(tower["queue"], {"emergency": 7, "commercial": 2})


if __name__ == "__main__":
    unittest.main()
