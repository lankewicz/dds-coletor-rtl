import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coleta_unica import storage


class StorageTests(unittest.TestCase):
    def test_missing_mount_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temp:
            mount = Path(temp) / 'ssd'
            output = mount / 'dados'
            with patch.object(storage, 'mount_token', side_effect=RuntimeError('SSD ausente')):
                with self.assertRaises(RuntimeError):
                    storage.SSDGuard(mount, output)
            self.assertFalse(output.exists())

    def test_changed_mount_and_paths_outside_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            mount = Path(temp).resolve()
            with patch.object(storage, 'mount_token', return_value=('10', '8:1', '/dev/sda1')):
                guard = storage.SSDGuard(mount, mount / 'dados')
                guard.check(mount / 'dados' / 'dia' / 'dados.json')
                with self.assertRaises(RuntimeError):
                    guard.check(mount / 'outra-pasta')
            with patch.object(storage, 'mount_token', return_value=('20', '8:1', '/dev/sda1')):
                with self.assertRaises(RuntimeError):
                    guard.check()

    def test_loss_before_publish_keeps_original_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'dados.json'
            target.write_text('original', encoding='utf-8')
            guard = Mock()
            guard.check.side_effect = [None, None, RuntimeError('SSD removido')]
            with patch.object(storage, '_guard', guard):
                with self.assertRaises(RuntimeError):
                    storage.atomic_text(target, 'novo')
            self.assertEqual(target.read_text(encoding='utf-8'), 'original')

    def test_mountinfo_parses_mount_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as temp:
            mount = (Path(temp) / 'ssd disk').resolve()
            encoded = str(mount).replace(' ', r'\040')
            info = f'42 1 8:1 / {encoded} rw - ext4 /dev/sda1 rw\n'
            with patch.object(Path, 'read_text', return_value=info):
                self.assertEqual(storage.mount_token(mount), ('42', '8:1', '/dev/sda1'))


if __name__ == '__main__':
    unittest.main()
