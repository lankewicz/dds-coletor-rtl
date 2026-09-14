from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from main import LocalRotalogRunner


class IndexSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Mock(enabled=True, bucket_name="test-bucket", blob_name="index.json.gz")
        self.runner = self.new_runner()
        self.teams = {"E3733": {
            "date": "2026-09-14", "updatedAt": "08:00", "version": 1,
            "conexao": {"isOnline": True},
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"atual": {"protocolo": "12345678", "observadoEm": "08:00"}},
        }}

    def new_runner(self):
        runner = LocalRotalogRunner(self.root, "EmpresaTeste")
        runner.firebase_store = self.store
        return runner

    def test_first_upload_then_metadata_only_skipped_after_restart(self):
        self.assertEqual(self.runner._sync_index(self.teams, "08:00"), "uploaded")
        self.teams["E3733"].update(updatedAt="08:02", version=2)
        self.teams["E3733"]["ordensServico"]["atual"]["observadoEm"] = "08:02"
        self.assertEqual(self.new_runner()._sync_index(self.teams, "08:02"), "unchanged")
        self.store.save.assert_called_once()

    def test_operational_changes_upload(self):
        self.runner._sync_index(self.teams, "08:00")
        for section, field, value in (
            ("conexao", "isOnline", False),
            ("jornada", "emIntervalo", True),
            ("ordensServico", "totalConcluidos", 1),
        ):
            self.teams["E3733"][section][field] = value
            self.assertEqual(self.runner._sync_index(self.teams, "08:02"), "uploaded")

    def test_failure_retries_after_restart_even_without_new_change(self):
        self.runner._sync_index(self.teams, "08:00")
        self.teams["E3733"]["conexao"]["isOnline"] = False
        self.store.save.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.runner._sync_index(self.teams, "08:02")
        self.store.save.side_effect = None
        self.assertEqual(self.new_runner()._sync_index(self.teams, "08:04"), "uploaded")
        self.assertEqual(self.runner._sync_index(self.teams, "08:06"), "unchanged")

    def test_destination_change_requires_upload(self):
        self.runner._sync_index(self.teams, "08:00")
        self.store.blob_name = "other/index.json.gz"
        self.assertEqual(self.runner._sync_index(self.teams, "08:02"), "uploaded")

    def test_disabled_does_not_confirm_upload(self):
        self.store.enabled = False
        self.assertEqual(self.runner._sync_index(self.teams, "08:00"), "disabled")
        self.assertFalse(self.runner.index_sync_path.exists())
        self.store.save.assert_not_called()

    def test_identical_scrapes_skip_second_upload(self):
        team = {"equipe_codigo": "E3733", "is_online": True}
        with patch("main.extrair_dados_tempo_real", return_value=[team]), patch("main.varrer_mes_anterior"):
            first = self.runner.run_once()
            second = self.new_runner().run_once()
        self.assertEqual(first["firebaseSyncStatus"], "uploaded", first)
        self.assertEqual(second["firebaseSyncStatus"], "unchanged", second)
        self.assertFalse(second["firebaseUploaded"])
        self.store.save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
