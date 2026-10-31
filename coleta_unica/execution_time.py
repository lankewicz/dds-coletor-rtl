"""Histórico local do tempo de execução, incluindo sessões interrompidas."""
import json
import math
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from .storage import atomic_text
else:
    from storage import atomic_text


def timestamp():
    return datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat()


class ExecutionTime:
    def __init__(self, path):
        self.path = Path(path)
        self.document = {'version': 1, 'sessoes': []}
        if self.path.exists():
            try:
                self.document = json.loads(self.path.read_text(encoding='utf-8'))
                if self.document['version'] != 1 or not isinstance(self.document['sessoes'], list):
                    raise ValueError('Histórico inválido')
                samples = self.document.get('duracoesDias', [])
                if not isinstance(samples, list) or not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in samples):
                    raise ValueError('Amostras de duração inválidas')
                for session in self.document['sessoes']:
                    active, elapsed = session['ativoSegundos'], session['decorridoSegundos']
                    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in (active, elapsed)) or active > elapsed:
                        raise ValueError('Duração inválida')
                    stages = session.get('etapasSegundos', {})
                    if (not isinstance(stages, dict)
                            or not all(isinstance(name, str) and isinstance(value, (int, float))
                                       and math.isfinite(value) and value >= 0
                                       for name, value in stages.items())):
                        raise ValueError('Tempos por etapa inválidos')
                    technical_pause = session.get('pausaTecnicaSegundos', 0.0)
                    if (not isinstance(technical_pause, (int, float))
                            or not math.isfinite(technical_pause)
                            or technical_pause < 0
                            or technical_pause > elapsed - active + 1e-6):
                        raise ValueError('Duração de pausa técnica inválida')
                    if session.get('status') == 'em execução':
                        session['status'] = 'interrompida sem fechamento'
                        session['fim'] = session['atualizadoEm']
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f'Histórico de tempo inválido; arquivo preservado: {self.path}') from exc
        self.previous_active = sum(s['ativoSegundos'] for s in self.document['sessoes'])
        self.previous_elapsed = sum(s['decorridoSegundos'] for s in self.document['sessoes'])
        self.previous_technical_pause = sum(s.get('pausaTecnicaSegundos', 0.0) for s in self.document['sessoes'])
        self.previous_stage_times = dict(self.document['sessoes'][-1].get('etapasSegundos', {})) if self.document['sessoes'] else {}
        self.session = dict(inicio=timestamp(), fim=None, atualizadoEm=timestamp(),
                            ativoSegundos=0.0, decorridoSegundos=0.0, pausaSegundos=0.0,
                            pausaTecnicaSegundos=0.0, etapasSegundos={}, status='em execução')
        self.document['sessoes'].append(self.session)
        self.last_checkpoint = None

    def checkpoint(self, active, elapsed, status='em execução', force=False, closed=False,
                   technical_pause=0.0, stage_times=None):
        now = time.monotonic()
        if not force and self.last_checkpoint is not None and now - self.last_checkpoint < 15:
            return
        active, elapsed = max(0.0, active), max(0.0, elapsed)
        elapsed = max(active, elapsed)
        technical_pause = min(max(0.0, technical_pause), elapsed - active)
        self.session.update(ativoSegundos=active, decorridoSegundos=elapsed,
                            pausaSegundos=elapsed - active, pausaTecnicaSegundos=technical_pause,
                            etapasSegundos=dict(stage_times or {}),
                            atualizadoEm=timestamp(), status=status)
        if closed:
            self.session['fim'] = self.session['atualizadoEm']
        self.document.update(ativoTotalSegundos=self.previous_active + active,
                             decorridoTotalSegundos=self.previous_elapsed + elapsed,
                             pausaTotalSegundos=self.previous_elapsed + elapsed - self.previous_active - active,
                             pausaTecnicaTotalSegundos=self.previous_technical_pause + technical_pause)
        atomic_text(self.path, json.dumps(self.document, ensure_ascii=False, indent=2))
        self.last_checkpoint = now
