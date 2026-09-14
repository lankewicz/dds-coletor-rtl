"""Testes para o intervalo adaptativo de coleta (3 min pico / 10 min fora de pico)."""

from __future__ import annotations

import datetime
import unittest
from zoneinfo import ZoneInfo

from main import get_adaptive_interval_seconds
from tui import TuiState

TZ = ZoneInfo("America/Sao_Paulo")


class AdaptiveIntervalTests(unittest.TestCase):
    def test_peak_hours_return_180_seconds(self):
        # 07:00:00 (início do pico)
        dt_0700 = datetime.datetime(2026, 9, 14, 7, 0, 0, tzinfo=TZ)
        self.assertEqual(get_adaptive_interval_seconds(dt_0700), 180)

        # 12:30:00 (meio do dia)
        dt_noon = datetime.datetime(2026, 9, 14, 12, 30, 0, tzinfo=TZ)
        self.assertEqual(get_adaptive_interval_seconds(dt_noon), 180)

        # 19:59:59 (último segundo do pico)
        dt_1959 = datetime.datetime(2026, 9, 14, 19, 59, 59, tzinfo=TZ)
        self.assertEqual(get_adaptive_interval_seconds(dt_1959), 180)

    def test_offpeak_hours_return_600_seconds(self):
        # 06:59:59 (madrugada antes do pico)
        dt_0659 = datetime.datetime(2026, 9, 14, 6, 59, 59, tzinfo=TZ)
        self.assertEqual(get_adaptive_interval_seconds(dt_0659), 600)

        # 20:00:00 (início do período noturno)
        dt_2000 = datetime.datetime(2026, 9, 14, 20, 0, 0, tzinfo=TZ)
        self.assertEqual(get_adaptive_interval_seconds(dt_2000), 600)

        # 23:45:00 (noite)
        dt_2345 = datetime.datetime(2026, 9, 14, 23, 45, 0, tzinfo=TZ)
        self.assertEqual(get_adaptive_interval_seconds(dt_2345), 600)

        # 02:15:00 (madrugada profunda)
        dt_0215 = datetime.datetime(2026, 9, 14, 2, 15, 0, tzinfo=TZ)
        self.assertEqual(get_adaptive_interval_seconds(dt_0215), 600)

    def test_custom_intervals(self):
        dt_peak = datetime.datetime(2026, 9, 14, 10, 0, 0, tzinfo=TZ)
        dt_offpeak = datetime.datetime(2026, 9, 14, 22, 0, 0, tzinfo=TZ)

        res_peak = get_adaptive_interval_seconds(dt_peak, peak_interval=120, offpeak_interval=300)
        res_offpeak = get_adaptive_interval_seconds(dt_offpeak, peak_interval=120, offpeak_interval=300)

        self.assertEqual(res_peak, 120)
        self.assertEqual(res_offpeak, 300)

    def test_tui_state_fixed_override(self):
        # Quando interval_seconds fixo é informado, ele sobrepõe o adaptativo
        state = TuiState(interval_seconds=45)
        dt_peak = datetime.datetime(2026, 9, 14, 10, 0, 0, tzinfo=TZ)
        dt_offpeak = datetime.datetime(2026, 9, 14, 22, 0, 0, tzinfo=TZ)

        self.assertEqual(state.get_interval(dt_peak), 45)
        self.assertEqual(state.get_interval(dt_offpeak), 45)

    def test_tui_state_adaptive_default(self):
        # Quando interval_seconds é None, usa grade adaptativa padrão (180s / 600s)
        state = TuiState(interval_seconds=None)
        dt_peak = datetime.datetime(2026, 9, 14, 10, 0, 0, tzinfo=TZ)
        dt_offpeak = datetime.datetime(2026, 9, 14, 22, 0, 0, tzinfo=TZ)

        self.assertEqual(state.get_interval(dt_peak), 180)
        self.assertEqual(state.get_interval(dt_offpeak), 600)


if __name__ == "__main__":
    unittest.main()
