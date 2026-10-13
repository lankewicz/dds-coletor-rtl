import copy
import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from coletor.storage_io import load_json, write_json
from coletor.torre_inputs import GcsInputs, TorreInputsSync, source_prefix
from scripts.export_torre_catalog import build_catalog

NOW = datetime.fromisoformat("2026-10-10T10:00:00-03:00")
PREFIX = "dados/chicoeletro/dds/controle"


def catalog():
    return build_catalog({"E3733": {"teamKey": "E3733", "active": False, "setor": "SUL",
                                  "members": ["Pessoa"], "equipment": {"tablet": {"summary": "123"}}}},
                         "ChicoEletro", lambda key, team: {key, "ALIAS"}, NOW)


def daily(day="2026-10-10"):
    return {"schemaVersion": 1, "company": "ChicoEletro", "date": day,
            "generatedAt": NOW.isoformat(), "revision": 3,
            "teams": {"E3733": {"completedAt": NOW.isoformat(), "trainingId": "dds-1"}}}


class Remote:
    def __init__(self):
        self.data = {PREFIX + "/cadastro/current.json.gz": catalog(),
                     PREFIX + "/daily/2026-10-10.json.gz": daily()}
        self.fail = None
        self.downloads = 0
        self.revision = "1"

    def listing(self, prefix):
        if self.fail == "listing":
            raise OSError("offline")
        return [name for name in self.data if name.startswith(prefix)]

    def fetch(self, name, cached=None):
        if self.fail == name:
            raise OSError("offline")
        if cached and cached["revision"] == self.revision:
            return cached
        self.downloads += 1
        return {"revision": self.revision, "data": copy.deepcopy(self.data[name])}


class InputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = Remote()
        self.sync = TorreInputsSync(self.root, "ChicoEletro", self.remote)

    def test_sync_preserves_catalog_details_and_dds_revision(self):
        result = self.sync.sync(NOW)
        self.assertFalse(result["stale"])
        bundle = load_json(self.sync.path, {})
        team = bundle["catalog"]["data"]["teams"]["E3733"]
        self.assertFalse(team["active"])
        self.assertEqual(team["aliases"], ["ALIAS", "E3733"])
        self.assertEqual(team["equipment"]["tablet"]["summary"], "123")
        self.assertEqual(bundle["daily"]["2026-10-10"]["data"]["revision"], 3)

    def test_restart_throttles_and_unchanged_generation_skips_download(self):
        self.sync.sync(NOW)
        restarted = TorreInputsSync(self.root, "ChicoEletro", self.remote)
        self.assertTrue(restarted.sync(NOW + timedelta(seconds=60))["skipped"])
        restarted.sync(NOW + timedelta(seconds=301))
        self.assertEqual(self.remote.downloads, 2)

    def test_failure_preserves_complete_bundle_and_success_time(self):
        self.sync.sync(NOW)
        saved = self.sync.path.read_bytes()
        self.remote.revision = "2"
        self.remote.data[PREFIX + "/cadastro/current.json.gz"]["teams"] = {}
        self.remote.fail = PREFIX + "/daily/2026-10-10.json.gz"
        result = self.sync.sync(NOW + timedelta(minutes=6))
        self.assertTrue(result["available"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["lastSuccessAt"], NOW.isoformat())
        self.assertEqual(self.sync.path.read_bytes(), saved)

    def test_missing_catalog_is_unavailable_and_listing_error_is_not_empty(self):
        self.remote.fail = PREFIX + "/cadastro/current.json.gz"
        self.assertFalse(self.sync.sync(NOW)["available"])
        self.assertFalse(self.sync.path.exists())
        self.remote.fail = "listing"
        self.assertFalse(self.sync.sync(NOW, force=True)["available"])
        self.assertFalse(self.sync.path.exists())

    def test_explicit_empty_collection_is_valid_and_removes_old_days(self):
        self.sync.sync(NOW)
        self.remote.data.pop(PREFIX + "/daily/2026-10-10.json.gz")
        result = self.sync.sync(NOW + timedelta(minutes=6))
        self.assertFalse(result["stale"])
        self.assertEqual(load_json(self.sync.path, {})["daily"], {})

    def test_wrong_company_date_and_stale_catalog_preserve_cache(self):
        self.sync.sync(NOW)
        saved = self.sync.path.read_bytes()
        for which, edit in [
            ("cadastro/current.json.gz", lambda d: d.update(company="Outra")),
            ("daily/2026-10-10.json.gz", lambda d: d.update(date="2026-10-09")),
            ("cadastro/current.json.gz", lambda d: d.update(generatedAt=(NOW-timedelta(days=2)).isoformat())),
        ]:
            self.remote = Remote()
            self.sync.remote = self.remote
            self.remote.revision = "2"
            edit(self.remote.data[PREFIX + "/" + which])
            self.assertTrue(self.sync.sync(NOW, force=True)["stale"])
            self.assertEqual(self.sync.path.read_bytes(), saved)

    def test_latest_available_days_and_no_future_projection(self):
        for day in ["2026-10-08", "2026-10-09", "2026-10-11"]:
            self.remote.data[PREFIX + "/daily/" + day + ".json.gz"] = daily(day)
        self.sync.days = 2
        self.sync.sync(NOW)
        self.assertEqual(list(load_json(self.sync.path, {})["daily"]), ["2026-10-09", "2026-10-10"])

    def test_empty_catalog_and_empty_daily_are_valid(self):
        self.remote.data[PREFIX + "/cadastro/current.json.gz"]["teams"] = {}
        self.remote.data[PREFIX + "/daily/2026-10-10.json.gz"]["teams"] = {}
        self.assertFalse(self.sync.sync(NOW)["stale"])

    def test_prefix_and_local_company_isolation(self):
        with self.assertRaises(ValueError):
            source_prefix("Outra", PREFIX)
        self.sync.sync(NOW)
        bundle = load_json(self.sync.path, {})
        bundle["company"] = "Outra"
        write_json(self.sync.path, bundle)
        with self.assertRaises(ValueError):
            self.sync.sync(NOW, force=True)

    def test_missing_cache_and_corrupt_cache_are_recovered_despite_recent_status(self):
        self.sync.sync(NOW)
        self.sync.path.unlink()
        self.assertFalse(self.sync.sync(NOW + timedelta(seconds=10)).get("skipped", False))
        self.sync.path.write_bytes(b"invalid")
        self.assertFalse(self.sync.sync(NOW + timedelta(seconds=20))["stale"])

    def test_runner_reports_sync_even_if_collection_fails(self):
        from main import LocalRotalogRunner
        with patch.dict("os.environ", {"TORRE_INPUTS_ENABLED": "false"}):
            runner = LocalRotalogRunner(self.root, "ChicoEletro")
        runner.torre_inputs = Mock()
        runner.torre_inputs.sync.return_value = {"stale": True, "available": False}
        with patch.object(runner, "_run_once", return_value={"status": "error"}):
            result = runner.run_once()
        self.assertTrue(result["torreInputs"]["stale"])
        runner.torre_inputs.sync.assert_called_once()

    def test_snapshot_marks_old_or_failed_sync_as_stale_without_network(self):
        self.sync.sync(NOW)
        self.assertFalse(self.sync.snapshot(NOW)["stale"])
        self.assertTrue(self.sync.snapshot(NOW + timedelta(minutes=6))["stale"])
        self.remote.fail = "listing"
        self.sync.sync(NOW + timedelta(minutes=6))
        self.assertTrue(self.sync.snapshot(NOW + timedelta(minutes=6))["stale"])
        self.assertEqual(self.remote.downloads, 2)

    def test_bucket_change_does_not_reuse_same_generation(self):
        self.remote.source_id = "gcs:bucket-one"
        first = TorreInputsSync(self.root, "ChicoEletro", self.remote)
        first.sync(NOW)
        self.remote.source_id = "gcs:bucket-two"
        second = TorreInputsSync(self.root, "ChicoEletro", self.remote)
        with self.assertRaises(ValueError):
            second.snapshot(NOW)
        self.assertFalse(second.sync(NOW)["stale"])
        self.assertEqual(self.remote.downloads, 4)


class AdapterTests(unittest.TestCase):
    def test_generation_cache_and_conditional_download(self):
        client = Mock()
        blob = client.bucket.return_value.blob.return_value
        blob.generation = 123
        blob.download_as_bytes.return_value = gzip.compress(json.dumps(catalog()).encode())
        remote = GcsInputs(client, "bucket")
        entry = remote.fetch("catalog")
        self.assertEqual(entry["revision"], "123")
        blob.download_as_bytes.assert_called_once_with(if_generation_match=123, timeout=15, retry=None)
        self.assertIs(remote.fetch("catalog", entry), entry)
        self.assertEqual(blob.download_as_bytes.call_count, 1)

    def test_export_rejects_duplicate_normalized_keys(self):
        with self.assertRaises(ValueError):
            build_catalog({"e3733": {"active": True}, "E3733": {"active": True}},
                          "ChicoEletro", lambda k, t: {k}, NOW)
