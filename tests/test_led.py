from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coletor.led import ProcessingLed, list_system_leds


class ProcessingLedTest(unittest.TestCase):
    def _led(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / "brightness").write_text("0", encoding="utf-8")
        (path / "trigger").write_text("default-on", encoding="utf-8")
        return path

    def test_processing_and_idle_switch_colors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            red = self._led(root, "red")
            green = self._led(root, "green")
            with patch.dict("os.environ", {"ROTALOG_LED_ENABLED": "true"}, clear=False):
                led = ProcessingLed(str(red), str(green))
                led.processing()
                self.assertEqual((red / "brightness").read_text(), "1")
                self.assertEqual((green / "brightness").read_text(), "0")
                led.idle()
                self.assertEqual((red / "brightness").read_text(), "0")
                self.assertEqual((green / "brightness").read_text(), "1")

    def test_lists_leds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._led(root, "green")
            self.assertEqual(list_system_leds(root)[0]["name"], "green")

