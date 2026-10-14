import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from coletor.data_storage import (
    MountedDisk, StorageSelection, StorageUnavailable, activate_storage, assert_available,
    mounted_disk, prepare_ssd, resolve_storage, select_storage,
)
from coletor.storage_io import write_json


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: activate_storage(None))
        self.root = Path(self.tmp.name)
        self.card = self.root / "app/dados-local"
        self.mount = self.root / "mounted"
        self.card.mkdir(parents=True)
        self.mount.mkdir()
        self.disk = MountedDisk("uuid-a", "/dev/sdb1", str(self.mount), "ext4")
        self.marker = self.mount / "dds-coletor-rtl/chicoeletro/.dds-storage.json"
        self.index = {"company": "ChicoEletro", "lastCollectedAt": "2026-10-14T08:00:00-03:00", "equipes": {}}
        write_json(self.card / "rotalog/equipes/current/index.json.gz", self.index)

    def prepared(self):
        with patch("coletor.data_storage.mounted_disk", return_value=self.disk):
            return prepare_ssd(self.card, self.mount, "ChicoEletro")

    def select(self, **kw):
        with patch("coletor.data_storage.mounted_disk", return_value=self.disk):
            return select_storage(self.card, "ChicoEletro", mode="auto", mount=self.mount, **kw)

    def test_copy_verified_and_original_preserved(self):
        original = (self.card / "rotalog/equipes/current/index.json.gz").read_bytes()
        selected = self.prepared()
        self.assertEqual(selected.uuid, "uuid-a")
        self.assertEqual((Path(selected.data_dir) / "rotalog/equipes/current/index.json.gz").read_bytes(), original)
        self.assertEqual((self.card / "rotalog/equipes/current/index.json.gz").read_bytes(), original)
        self.assertEqual(self.select().mode, "ssd")

    def test_directory_without_mount_does_not_receive_data(self):
        with patch("coletor.data_storage.mounted_disk", return_value=None):
            selected = select_storage(self.card, "ChicoEletro", mode="auto", mount=self.mount)
            self.assertEqual(selected.mode, "card")
            with self.assertRaises(StorageUnavailable):
                prepare_ssd(self.card, self.mount, "ChicoEletro")
        self.assertFalse((self.mount / "dds-coletor-rtl").exists())

    def test_unregistered_disk_and_readonly_use_card(self):
        self.assertEqual(self.select().mode, "card")
        self.prepared()
        self.assertEqual(self.select(allowed_uuids=("different",)).mode, "card")
        readonly = MountedDisk("uuid-a", "/dev/sdb1", str(self.mount), "ext4", True)
        with patch("coletor.data_storage.mounted_disk", return_value=readonly):
            self.assertEqual(select_storage(self.card, "ChicoEletro", mode="auto", mount=self.mount).mode, "card")

    def test_registered_disk_can_move_between_projects(self):
        expected = self.prepared()
        other_card = self.root / "another-pi/dados-local"
        other_card.mkdir(parents=True)
        with patch("coletor.data_storage.mounted_disk", return_value=self.disk):
            selected = select_storage(other_card, "ChicoEletro", mode="auto", mount=self.mount)
        self.assertEqual(selected.data_dir, expected.data_dir)
        self.assertEqual(selected.uuid, expected.uuid)

    def test_foreign_company_or_different_uuid_rejected(self):
        self.prepared()
        marker = self.marker
        data = json.loads(marker.read_text())
        data["company"] = "Outra"
        marker.write_text(json.dumps(data))
        self.assertEqual(self.select().mode, "card")
        data.update(company="ChicoEletro", uuid="uuid-other")
        marker.write_text(json.dumps(data))
        self.assertEqual(self.select().mode, "card")

    def test_card_newer_prevents_automatic_failback_over_new_data(self):
        self.prepared()
        newer = {**self.index, "lastCollectedAt": "2026-10-14T09:00:00-03:00"}
        write_json(self.card / "rotalog/equipes/current/index.json.gz", newer)
        selected = self.select()
        self.assertEqual(selected.mode, "card")
        self.assertIn("reconcile-required", selected.reason)

    def test_existing_ssd_is_not_overwritten(self):
        self.prepared()
        with patch("coletor.data_storage.mounted_disk", return_value=self.disk):
            with self.assertRaises(StorageUnavailable):
                prepare_ssd(self.card, self.mount, "ChicoEletro")

    def test_disk_loss_blocks_atomic_write_before_creating_files(self):
        selection = self.prepared()
        activate_storage(selection)
        target = Path(selection.data_dir) / "torre/new.json"
        with patch("coletor.data_storage.mounted_disk", return_value=None):
            with self.assertRaises(StorageUnavailable):
                write_json(target, {"ok": True})
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())

    def test_disk_exchange_blocks_running_writer(self):
        selection = self.prepared()
        other = MountedDisk("uuid-b", "/dev/sdb1", str(self.mount), "ext4")
        with patch("coletor.data_storage.mounted_disk", return_value=other):
            with self.assertRaises(StorageUnavailable):
                assert_available(selection)

    def test_explicit_test_directory_not_redirected(self):
        self.prepared()
        with patch.dict("os.environ", {"DDS_STORAGE_MODE": "auto", "DDS_SSD_MOUNT": str(self.mount)}):
            selected = resolve_storage(self.root / "test-data", "ChicoEletro", project_root=self.card.parent)
        self.assertEqual(selected.reason, "explicit-directory")

    def test_findmnt_requires_real_mount_and_uuid(self):
        with patch("coletor.data_storage.os.path.ismount", return_value=False), \
                patch("coletor.data_storage.subprocess.run") as run:
            self.assertIsNone(mounted_disk(self.mount))
            run.assert_not_called()
        payload = {"filesystems": [{"target": str(self.mount), "source": "/dev/sdb1", "uuid": "uuid-a",
                                     "fstype": "ext4", "options": "rw,relatime"}]}
        with patch("coletor.data_storage.sys.platform", "linux"), \
                patch("coletor.data_storage.os.path.ismount", return_value=True), \
                patch("coletor.data_storage.subprocess.run", return_value=Mock(stdout=json.dumps(payload))):
            self.assertEqual(mounted_disk(self.mount).uuid, "uuid-a")

    def test_changed_source_does_not_publish_marker(self):
        from coletor.data_storage import tree_digest
        real_digest = tree_digest
        calls = 0
        def change_digest(path):
            nonlocal calls
            calls += 1
            result = real_digest(path)
            if calls == 2:
                result["changed"] = "different"
            return result
        with patch("coletor.data_storage.mounted_disk", return_value=self.disk), \
                patch("coletor.data_storage.tree_digest", side_effect=change_digest):
            with self.assertRaises(StorageUnavailable):
                prepare_ssd(self.card, self.mount, "ChicoEletro")
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
