import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from main import LocalRotalogRunner
from coletor.storage import load_json


class LocalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Mock(enabled=True, bucket_name="bucket", blob_name="index.json.gz")
        self.repo = Mock()
        self.repo.daily_path.return_value = "daily/2026-09-14/E3733.json.gz"
        self.runner = LocalRotalogRunner(self.root, "EmpresaTeste")
        self.runner.firebase_store = self.store
        self.runner.team_repo = self.repo
        self.path = self.root / "rotalog/equipes/daily/2026-09-14/E3733.json.gz"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(gzip.compress(b"{}")[:-3])
        self.remote = {
            "teamKey": "E3733",
            "date": "2026-09-14",
            "jornada": {"turno": {"status": "ABERTO"}},
            "ordensServico": {"historico": [{"protocolo": "12345678"}]},
        }

    def test_corrupt_daily_is_recovered_and_original_quarantined(self):
        self.store.load_blob.return_value = self.remote
        recovered = self.runner._load_daily_with_recovery(self.path, "2026-09-14", "E3733")
        self.assertEqual(recovered, self.remote)
        self.assertEqual(load_json(self.path, {}), self.remote)
        self.assertEqual(len(list(self.path.parent.glob("E3733.json.gz.corrupt-*"))), 1)

    def test_corrupt_daily_is_preserved_when_remote_is_invalid(self):
        original = self.path.read_bytes()
        self.store.load_blob.return_value = {}
        with self.assertRaises(RuntimeError):
            self.runner._load_daily_with_recovery(self.path, "2026-09-14", "E3733")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob("*.corrupt-*")))

    def test_wrong_team_remote_copy_is_rejected(self):
        self.remote["teamKey"] = "E9999"
        self.store.load_blob.return_value = self.remote
        with self.assertRaises(RuntimeError):
            self.runner._load_daily_with_recovery(self.path, "2026-09-14", "E3733")

    def test_corrupt_index_is_recovered_from_firebase(self):
        index_path = self.runner.index_path
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_bytes(b"invalid")
        self.store.load.return_value = {
            "updatedAtIso": "2026-09-14T08:00:00-03:00",
            "equipes": {"E3733": self.remote},
        }
        with patch("main.extrair_dados_tempo_real", return_value=[]):
            result = self.runner.run_once()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(load_json(index_path, {})["totalEquipes"], 1)
        self.assertEqual(len(list(index_path.parent.glob("index.json.gz.corrupt-*"))), 1)

    def test_corrupt_index_is_preserved_without_valid_remote_copy(self):
        index_path = self.runner.index_path
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_bytes(b"invalid")
        self.store.load.return_value = {}
        result = self.runner.run_once()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(index_path.read_bytes(), b"invalid")
        self.assertFalse(list(index_path.parent.glob("*.corrupt-*")))


if __name__ == "__main__":
    unittest.main()
