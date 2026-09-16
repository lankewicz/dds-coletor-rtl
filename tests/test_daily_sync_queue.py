import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from coletor.storage import load_json, write_json
from main import LocalRotalogRunner


class DailySyncQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Mock(enabled=True, bucket_name="bucket", blob_name="index.json.gz")
        self.repo = Mock()
        self.runner = self.new_runner()
        self.day = "2026-09-14"
        self.team_key = "E3733"
        self.document = {
            "teamKey": self.team_key,
            "date": self.day,
            "jornada": {"turno": {"status": "ABERTO"}, "intervalos": []},
            "ordensServico": {"historico": []},
        }
        self.daily_path = self.root / "rotalog/equipes/daily" / self.day / f"{self.team_key}.json.gz"
        write_json(self.daily_path, self.document)

    def new_runner(self):
        runner = LocalRotalogRunner(self.root, "EmpresaTeste")
        runner.enable_firebase = True
        runner.firebase_store = self.store
        runner.team_repo = self.repo
        return runner

    def queue_items(self):
        return load_json(self.runner.daily_sync_queue_path, {}).get("items", {})

    def test_pending_is_written_before_upload_and_removed_only_after_success(self):
        self.runner._enqueue_daily_sync(self.day, self.team_key, ["turno_aberto"])
        self.assertIn(f"{self.day}/{self.team_key}", self.queue_items())
        self.repo.save_daily.side_effect = OSError("offline")
        failed = self.runner._flush_daily_sync_queue()
        self.assertEqual(failed, {"uploaded": 0, "failed": 1, "pending": 1})
        self.assertEqual(self.queue_items()[f"{self.day}/{self.team_key}"]["attempts"], 1)

        self.repo.save_daily.side_effect = None
        restarted = self.new_runner()
        recovered = restarted._flush_daily_sync_queue()
        self.assertEqual(recovered, {"uploaded": 1, "failed": 0, "pending": 0})
        self.assertEqual(self.queue_items(), {})

    def test_repeated_events_coalesce_and_upload_latest_local_document(self):
        self.runner._enqueue_daily_sync(self.day, self.team_key, ["turno_aberto"])
        self.runner._enqueue_daily_sync(self.day, self.team_key, ["servico_concluido"])
        latest = copy.deepcopy(self.document)
        latest["ordensServico"]["historico"] = [{"protocolo": "12345678"}]
        write_json(self.daily_path, latest)
        self.runner._flush_daily_sync_queue()
        self.repo.save_daily.assert_called_once_with(latest, self.day)

    def test_queue_remains_when_firebase_is_unavailable(self):
        self.runner._enqueue_daily_sync(self.day, self.team_key, ["turno_aberto"])
        self.runner.firebase_store = None
        self.runner.team_repo = None
        self.assertEqual(
            self.runner._flush_daily_sync_queue(),
            {"uploaded": 0, "failed": 0, "pending": 1},
        )

    def test_event_policy_covers_open_completion_and_close_leaving_corrections_local(self):
        opened = self.document
        self.assertEqual(self.runner._daily_sync_reasons({}, opened), ["turno_aberto"])

        completed = copy.deepcopy(opened)
        completed["ordensServico"]["historico"] = [{"protocolo": "12345678", "fimExecucao": "10:00"}]
        self.assertEqual(
            self.runner._daily_sync_reasons(opened, completed),
            ["servico_concluido"],
        )

        # Ajustes e correções de OS permanecem locais (sem fila para nuvem)
        corrected = copy.deepcopy(completed)
        corrected["ordensServico"]["historico"][0]["fimExecucao"] = "10:15"
        self.assertEqual(
            self.runner._daily_sync_reasons(completed, corrected),
            [],
        )

        closed = copy.deepcopy(corrected)
        closed["jornada"]["turno"]["status"] = "FECHADO"
        self.assertEqual(self.runner._daily_sync_reasons(corrected, closed), ["turno_fechado"])

    def test_corrupt_sync_queue_is_quarantined_and_reinitialized(self):
        queue_file = self.runner.daily_sync_queue_path
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        queue_file.write_bytes(b"corrupted-non-gzip-content-or-truncated")

        loaded = self.runner._load_daily_sync_queue()
        self.assertEqual(loaded, {"schemaVersion": 1, "items": {}})
        self.assertFalse(queue_file.exists())
        corrupt_files = list(queue_file.parent.glob("pending-daily.json.gz.corrupt-*"))
        self.assertEqual(len(corrupt_files), 1)


if __name__ == "__main__":
    unittest.main()
