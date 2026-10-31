import os
import re
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from coleta_unica.main import Run, screen, progress_bar, active_time, estimate_remaining, eta_text, technical_pause_time
from coleta_unica.scraper import Scraper
from coleta_unica.test_identity_cache import record, HTML


class ProgressTests(unittest.TestCase):
    def test_eta_ignores_skipped_days_and_pauses(self):
        args = SimpleNamespace(sem_tui=False)
        with patch('coleta_unica.main.time.monotonic', return_value=1000):
            run = Run(args)
        run.state.update(retomados=2, equipes_total=100, equipes_feitas=50, dia_inicio=0)
        self.assertEqual(estimate_remaining(run.state, 10, 100), (1500, True))
        run.state.update(dias=1, duracoes_dias=[100], dia_inicio=200)
        self.assertEqual(estimate_remaining(run.state, 10, 250), (650, True))
        with patch('coleta_unica.main.time.monotonic', return_value=1100):
            run.toggle_pause()
        self.assertEqual(active_time(run.state, 2100), 100)
        self.assertIn('suspensa', eta_text(run.state, 10, 2100))
        with patch('coleta_unica.main.time.monotonic', return_value=2100):
            run.toggle_pause()
        self.assertEqual(active_time(run.state, 2150), 150)

    def test_technical_pause_is_excluded_from_active_time(self):
        run = Run(SimpleNamespace(sem_tui=False))
        run.state.update(pausa_tecnica_total=30, pausa_tecnica_inicio=run.state['inicio'] + 70)
        now = run.state['inicio'] + 100
        self.assertEqual(technical_pause_time(run.state, now), 60)
        self.assertEqual(active_time(run.state, now), 40)
        run.state['pausa_tecnica_inicio'] = None
        run.state['pausa_tecnica_total'] = 60
        self.assertEqual(active_time(run.state, now), 40)

    def test_manual_pause_suspends_technical_pause_without_double_counting(self):
        with patch('coleta_unica.main.time.monotonic', return_value=0):
            run = Run(SimpleNamespace(sem_tui=False))
        run.state.update(status='Pausa técnica', pausa_tecnica_inicio=10)
        with patch('coleta_unica.main.time.monotonic', return_value=20):
            run.toggle_pause()
        self.assertEqual(run.state['pausa_tecnica_total'], 10)
        self.assertIsNone(run.state['pausa_tecnica_inicio'])
        self.assertEqual(active_time(run.state, 30), 10)
        with patch('coleta_unica.main.time.monotonic', return_value=40):
            run.toggle_pause()
        self.assertEqual(run.state['pausado_total'], 20)
        self.assertEqual(run.state['pausa_tecnica_inicio'], 40)
        self.assertEqual(active_time(run.state, 50), 10)

    def test_technical_pause_waits_for_configured_duration(self):
        clock = [100.0]
        args = SimpleNamespace(sem_tui=True)
        with patch('coleta_unica.main.time.monotonic', side_effect=lambda: clock[0]), \
                patch('coleta_unica.main.TECHNICAL_PAUSE_SECONDS', 1), \
                patch('coleta_unica.main.check_storage'):
            run = Run(args)
            run.stop = SimpleNamespace(
                is_set=lambda: False,
                wait=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            )
            run.pause_between_days()
        self.assertAlmostEqual(run.state['pausa_tecnica_total'], 1.0)
        self.assertEqual(active_time(run.state, clock[0]), 0.0)

    def test_eta_waits_for_progress_and_finishes_at_zero(self):
        run = Run(SimpleNamespace(sem_tui=False))
        run.state.update(retomados=20, dia_inicio=0)
        self.assertEqual(estimate_remaining(run.state, 30, 100), (None, True))
        run.state.update(duracoes_dias=[100, 120, 80], dia_inicio=None)
        self.assertEqual(estimate_remaining(run.state, 30, 500), (1000, False))
        self.assertIn('Término previsto:', eta_text(run.state, 30, time_now := run.state['inicio']))
        run.state.update(retomados=30)
        self.assertEqual(estimate_remaining(run.state, 30, 500), (0, False))
        self.assertIn('concluída', eta_text(run.state, 30, time_now))

    def test_three_bars_fill_terminal_width_after_resize(self):
        args = SimpleNamespace(inicio=date(2026, 1, 1), fim=date(2026, 1, 10),
                               saida=Path('dados'), sem_tui=False)
        run = Run(args)
        run.state.update(dias=4, retomados=1, dia_numero=6, dia='2026-01-06',
                         equipes_total=10, equipes_feitas=5, equipe_index=6,
                         equipe='E001', equipe_etapa=1, equipe_acao='Consultando diário')
        for columns in (30, 80, 160):
            with patch('coleta_unica.main.shutil.get_terminal_size', return_value=os.terminal_size((columns, 24))):
                rendered = screen(run, 10)
            lines = rendered.splitlines()
            self.assertTrue(all(len(line) == columns - 1 for line in lines))
            bars = [line for line in lines if set(line) <= {'█', '░'}]
            self.assertEqual(len(bars), 3)
            self.assertEqual(bars[0].count('█'), (columns - 1) // 2)
            self.assertEqual(bars[1].count('█'), (columns - 1) // 2)
            self.assertEqual(bars[2].count('█'), (columns - 1) // 3)
        self.assertEqual(progress_bar(10, 10, 80), '█' * 80)
        self.assertEqual(progress_bar(0, 0, 80), '░' * 80)

    def test_team_progress_advances_after_success_not_before_request(self):
        events = []
        scraper = Scraper('test', 'test', delay=0, notify=lambda **kw: events.append(kw))
        def response(*args, **kwargs):
            self.assertEqual(events[-1]['equipe_etapa'], 1)
            self.assertEqual(next(e['equipes_feitas'] for e in reversed(events) if 'equipes_feitas' in e), 0)
            return SimpleNamespace(text=HTML)
        scraper.request = Mock(side_effect=response)
        try:
            scraper.enrich([record()], lambda *args: None)
            self.assertEqual([e['equipe_etapa'] for e in events if 'equipe_etapa' in e], [0, 0, 1, 2, 3])
            self.assertEqual(events[-2]['equipes_feitas'], 1)
            self.assertEqual(events[-2]['equipe'], 'E001') if 'equipe' in events[-2] else None
            events.clear()
            scraper.enrich([record()], lambda *args: None)
            self.assertEqual([e['equipe_etapa'] for e in events if 'equipe_etapa' in e], [0, 0, 1, 3])
            self.assertEqual(scraper.request.call_count, 1)
        finally:
            scraper.close()

    def test_colors_preserve_width_and_information(self):
        run = Run(SimpleNamespace(inicio=date(2026, 1, 1), fim=date(2026, 10, 21),
                                  saida=Path('dados'), sem_tui=False))
        run.state.update(equipe='E3W9E', equipe_acao='Consultando diário')
        with patch('coleta_unica.main.shutil.get_terminal_size', return_value=os.terminal_size((120, 24))):
            colored = screen(run, 294, color=True)
            plain = re.sub(r'\x1b\[[0-9;]*m', '', colored)
            self.assertTrue(all(len(line) == 119 for line in plain.splitlines()))
            self.assertLessEqual(len(plain.splitlines()), 23)
            for label in ('COMPLETO', 'DIÁRIO', 'EQUIPE', 'TOTAL ATIVO', 'TÉCNICAS',
                          'E3W9E', 'Consultando diário', 'Reaproveitados'):
                self.assertIn(label, plain)
