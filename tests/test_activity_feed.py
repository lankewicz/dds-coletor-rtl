import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from coletor.activity_feed import update_activity_feed, sync_activity_feed


class ActivityFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events/index.json.gz"
        self.now = datetime.fromisoformat("2026-10-20T10:00:00-03:00")
        self.doc = {"company": "EmpresaTeste", "teamKey": "E2269", "jornada": {},
                    "ordensServico": {"atual": {
                        "serviceId": "E2269_service1", "protocolo": "20265804191700", "categoria": "COMERCIAL",
                        "statusAtual": "EXECUCAO", "inicioDeslocamento": "2026-10-20T08:00:00-03:00",
                        "inicioExecucao": "2026-10-20T08:20:00-03:00"}}}

    def update(self, documents=None, now=None):
        return update_activity_feed(self.path, "EmpresaTeste", documents if documents is not None else {"E2269": self.doc}, now or self.now)

    def test_stable_file_deduplication_and_protocol_preserved(self):
        result = self.update()
        contents = self.path.read_bytes()
        next_result = self.update(now=self.now + timedelta(minutes=2))
        self.assertFalse(next_result["changed"])
        self.assertEqual(self.path.read_bytes(), contents)
        self.assertEqual(len(result["payload"]["items"]), 2)
        event = result["payload"]["items"][0]
        self.assertEqual(event["protocol"], "20265804191700")
        self.assertEqual(event["type"], "C")
        self.assertEqual(event["status"], "EXECUCAO")

    def test_correction_replaces_same_event_and_invalid_time_removes_it(self):
        old = self.update()["payload"]["items"][0]
        service = self.doc["ordensServico"]["atual"]
        service["inicioExecucao"] = "2026-10-20T08:30:00-03:00"
        new = self.update()["payload"]["items"][0]
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["occurredAt"], old["occurredAt"])
        service["inicioExecucao"] = "2026-10-20T07:00:00-03:00"
        self.assertEqual([e["status"] for e in self.update()["payload"]["items"]], ["DESLOCAMENTO"])

    def test_window_restart_and_temporarily_missing_team(self):
        self.update()
        self.assertEqual(len(self.update({})["payload"]["items"]), 2)
        self.assertEqual(self.update({}, self.now + timedelta(hours=25))["payload"]["items"], [])

    def test_all_teams_categories_future_times_and_distinct_attendances(self):
        other = copy.deepcopy(self.doc)
        other["teamKey"] = "E3552"
        service = other["ordensServico"]["atual"]
        service.update(serviceId="E3552_other", categoria="EMERGENCIA", inicioExecucao="2026-10-20T15:00:00-03:00")
        events = self.update({"E2269": self.doc, "E3552": other})["payload"]["items"]
        self.assertEqual(len(events), 3)
        self.assertEqual({e["type"] for e in events}, {"C", "E"})
        service["serviceId"] = "E3552_repeat"
        events = self.update({"E3552": other})["payload"]["items"]
        self.assertEqual(len(events), 4)

    def test_shifts_intervals_and_invalid_shift(self):
        self.doc["jornada"] = {"turnos": [
            {"turnoId": "valid", "inicio": "2026-10-20T07:00:00-03:00", "fim": "2026-10-20T09:00:00-03:00"},
            {"turnoId": "bad", "inicio": "2026-10-20T09:00:00-03:00", "fim": "2026-10-20T06:00:00-03:00"}],
            "intervalos": [{"inicio": "2026-10-20T08:30:00-03:00", "fim": "2026-10-20T08:40:00-03:00"}]}
        events = self.update()["payload"]["items"]
        self.assertEqual(len(events), 6)
        self.assertTrue(all(e["protocol"] is None for e in events if e["entityId"] == "valid"))

    def test_corrupt_and_foreign_files_are_preserved(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"invalid")
        with self.assertRaises(ValueError):self.update()
        self.assertEqual(self.path.read_bytes(), b"invalid")
        self.path.write_text(json.dumps({"schemaVersion": 1, "company": "OutraEmpresa", "items": []}))
        contents = self.path.read_bytes()
        with self.assertRaises(ValueError):self.update()
        self.assertEqual(self.path.read_bytes(), contents)

    def test_runner_creates_file_and_isolates_corruption(self):
        from main import LocalRotalogRunner
        runner = LocalRotalogRunner(Path(self.tmp.name) / "runner", "EmpresaTeste")
        team = {"equipe_codigo": "E2269", "is_online": True}
        with patch("main.extrair_dados_tempo_real", return_value=[team]):
            result = runner.run_once()
            self.assertEqual(result["status"], "success", result)
            path = Path(result["activityFeedPath"])
            self.assertTrue(path.exists())
            path.write_bytes(b"corrupt")
            retry = runner.run_once()
        self.assertEqual(retry["status"], "success", retry)
        self.assertIn("corrompido", retry["activityFeedError"])
        self.assertEqual(path.read_bytes(), b"corrupt")

    def test_upload_retry_receipt_and_destination(self):
        payload = self.update()["payload"]
        store = Mock(bucket_name="bucket", blob_name="dados/empresateste/rotalog/equipes/current/index.json.gz")
        store.save_blob.side_effect = OSError("offline")
        with self.assertRaises(OSError):sync_activity_feed(self.path, payload, store)
        store.save_blob.side_effect = None
        self.assertEqual(sync_activity_feed(self.path, payload, store), "uploaded")
        self.assertEqual(sync_activity_feed(self.path, payload, store), "unchanged")
        self.assertEqual(store.save_blob.call_count, 2)
        self.assertEqual(store.save_blob.call_args.args[0], "dados/empresateste/rotalog/events/current/index.json.gz")
