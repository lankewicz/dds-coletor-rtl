"""Controle opcional dos LEDs de status expostos pelo Linux em /sys/class/leds.

Os nomes dos LEDs variam entre modelos de Orange Pi. Por isso os caminhos são
configurados no ambiente e o coletor continua funcionando normalmente quando
o hardware não disponibiliza LEDs controláveis.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path


LOG = logging.getLogger("coletor-rotalog.led")


def list_system_leds(root: Path = Path("/sys/class/leds")) -> list[dict[str, str]]:
    """Retorna LEDs detectados e seus gatilhos atuais, sem alterar o sistema."""
    if not root.is_dir():
        return []
    leds: list[dict[str, str]] = []
    for led_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        brightness = led_dir / "brightness"
        trigger = led_dir / "trigger"
        if not brightness.is_file():
            continue
        try:
            current = brightness.read_text(encoding="utf-8").strip()
        except OSError:
            current = "?"
        try:
            trigger_value = trigger.read_text(encoding="utf-8").strip()
        except OSError:
            trigger_value = ""
        leds.append({
            "name": led_dir.name,
            "path": str(led_dir),
            "brightness": current,
            "trigger": trigger_value,
        })
    return leds


class ProcessingLed:
    """Alterna LEDs vermelho/verde durante um ciclo de coleta."""

    def __init__(self, red_path: str | None = None, green_path: str | None = None):
        self.red_path = self._brightness_path(red_path or os.getenv("ROTALOG_LED_RED_PATH"))
        self.green_path = self._brightness_path(green_path or os.getenv("ROTALOG_LED_GREEN_PATH"))
        self.enabled = self._as_bool(os.getenv("ROTALOG_LED_ENABLED", "false"))
        self._warned = False

        if self.enabled and (self.red_path is None or self.green_path is None):
            LOG.warning(
                "Controle de LED desativado: defina ROTALOG_LED_RED_PATH e "
                "ROTALOG_LED_GREEN_PATH com diretórios válidos em /sys/class/leds."
            )
            self.enabled = False

    @staticmethod
    def _as_bool(value: str) -> bool:
        return value.strip().lower() in {"1", "true", "yes", "sim", "on"}

    @staticmethod
    def _brightness_path(value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        return path / "brightness" if path.name != "brightness" else path

    @staticmethod
    def _trigger_path(brightness_path: Path) -> Path:
        return brightness_path.with_name("trigger")

    def _write(self, path: Path | None, value: str) -> None:
        if path is None:
            return
        try:
            trigger = self._trigger_path(path)
            if trigger.is_file():
                trigger.write_text("none", encoding="utf-8")
            path.write_text(value, encoding="utf-8")
        except OSError as exc:
            if not self._warned:
                LOG.warning("Não foi possível atualizar LED em %s: %s", path, exc)
                self._warned = True

    def processing(self) -> None:
        """Exibe vermelho enquanto a coleta, persistência e sincronização ocorrem."""
        if not self.enabled:
            return
        self._write(self.green_path, "0")
        self._write(self.red_path, "1")

    def idle(self) -> None:
        """Retorna ao verde depois que o ciclo já gravou seu último arquivo."""
        if not self.enabled:
            return
        self._write(self.red_path, "0")
        self._write(self.green_path, "1")
