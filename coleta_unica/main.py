"""Execute: python -m coleta_unica --fim 2026-01-01."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

if __package__:
    from .scraper import Scraper, SCHEMA_VERSION, DETAIL_FIELDS
    from .identity_cache import IdentityCache
    from .execution_time import ExecutionTime
    from .storage import atomic_text, check_storage, configure_ssd
else:
    from scraper import Scraper, SCHEMA_VERSION, DETAIL_FIELDS
    from identity_cache import IdentityCache
    from execution_time import ExecutionTime
    from storage import atomic_text, check_storage, configure_ssd

HERE = Path(__file__).resolve().parent


def atomic(path, text):
    atomic_text(path, text, encoding='utf-8-sig' if path.suffix == '.csv' else 'utf-8')


def save_day(folder, result):
    for name in ('resumo', 'turnos', 'eventos'):
        records = result[name]
        fields = list(records[0]) if records else result.get('paginacao', {}).get(name, {}).get('colunas', [])
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fields, delimiter=';')
        writer.writeheader()
        writer.writerows(records)
        atomic(folder / f'{name}.csv', stream.getvalue())
    # Este arquivo é o recibo de conclusão, gravado depois dos três CSVs.
    atomic(folder / 'dados.json', json.dumps(result, ensure_ascii=False, indent=2))


def complete(folder, day):
    try:
        result = json.loads((folder / 'dados.json').read_text(encoding='utf-8'))
        return (result.get('completo') is True and result.get('data') == day.isoformat()
                and result.get('schemaVersion') == SCHEMA_VERSION
                and len(result['resumo']) == 15
                and all(all(key in row for key in DETAIL_FIELDS) and row['Agência Nome completo'] for row in result['turnos'])
                and all(len(result[name]) == result['paginacao'][name]['total'] for name in ('turnos', 'eventos'))
                and all((folder / f'{name}.csv').is_file() for name in ('resumo', 'turnos', 'eventos')))
    except (OSError, ValueError, KeyError, TypeError):
        return False


@contextmanager
def output_lock(folder):
    check_storage(folder)
    folder.mkdir(parents=True, exist_ok=True)
    check_storage(folder / '.execucao.lock')
    with (folder / '.execucao.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Já existe uma coleta usando esta pasta de saída.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Stopped(Exception):
    pass


@contextmanager
def timing_session(run):
    history = ExecutionTime(run.args.saida / 'tempo-execucao.json')
    with run.lock:
        run.state['ativo_anterior'] = history.previous_active
        run.state['decorrido_anterior'] = history.previous_elapsed
        run.state['duracoes_dias'] = history.document.get('duracoesDias', [])[-7:]
    run.history = history
    try:
        run.checkpoint_time(force=True)
        yield
    finally:
        exception = sys.exc_info()[0]
        try:
            run.checkpoint_time(force=True, closed=True,
                                status=('interrompida' if issubclass(exception, Stopped) else 'erro') if exception else run.state['status'])
        finally:
            run.history = None


class Run:
    def __init__(self, args):
        self.args = args
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.lock = threading.Lock()
        self.state = dict(status='Preparando', dia='—', etapa='—', pagina=0, paginas=0,
                          linhas=0, dias=0, retomados=0, erro='', inicio=time.monotonic(),
                          detalhes_consultados=0, detalhes_reutilizados=0)
        self.state.update(dia_numero=0, equipes_total=None, equipes_feitas=0,
                          equipe_index=0, equipe='—', equipe_etapa=0,
                          equipe_acao='Aguardando leitura das tabelas', dia_validado=False,
                          pausado_total=0.0, pausa_inicio=None, dia_inicio=None, duracoes_dias=[])
        self.state.update(ativo_anterior=0.0, decorrido_anterior=0.0)
        self.history = None
        self.exit_code = 0

    def update(self, **values):
        self.check()
        with self.lock:
            self.state.update(values)
        self.checkpoint_time()
        if self.args.sem_tui:
            print(' | '.join(f'{k}: {v}' for k, v in values.items()), flush=True)

    def checkpoint_time(self, force=False, closed=False, status='em execução'):
        if self.history is None:
            return
        now = time.monotonic()
        with self.lock:
            active = active_time(self.state, now)
            elapsed = max(0, now - self.state['inicio'])
            self.history.document['duracoesDias'] = list(self.state['duracoes_dias'])
        self.history.checkpoint(active, elapsed, status=status, force=force, closed=closed)

    def check(self):
        check_storage()
        while self.pause.is_set() and not self.stop.is_set():
            self.stop.wait(.15)
        if self.stop.is_set():
            raise Stopped()

    def toggle_pause(self):
        with self.lock:
            now = time.monotonic()
            if self.pause.is_set():
                self.state['pausado_total'] += now - self.state['pausa_inicio']
                self.state['pausa_inicio'] = None
                self.pause.clear()
            else:
                self.state['pausa_inicio'] = now
                self.pause.set()

    def work(self):
        scraper = None
        try:
            with output_lock(self.args.saida), timing_session(self):
                self.update(etapa='Lendo profissionais já identificados')
                identities = IdentityCache(self.args.saida / 'profissionais-cache.json')
                identities.seed(self.args.saida)
                day = self.args.inicio
                while day <= self.args.fim:
                    self.check()
                    folder = self.args.saida / day.isoformat()
                    self.update(dia=day.isoformat(), dia_numero=(day - self.args.inicio).days + 1)
                    if complete(folder, day):
                        saved = json.loads((folder / 'dados.json').read_text(encoding='utf-8'))
                        count = len(saved['turnos'])
                        self.update(retomados=self.state['retomados'] + 1, dia_validado=True,
                                    equipes_total=count, equipes_feitas=count, equipe='—',
                                    equipe_index=0, equipe_etapa=0, equipe_acao='Dia já concluído')
                        day += timedelta(days=1)
                        continue
                    self.update(dia=day.isoformat(), status='Coletando', etapa='Autenticação', pagina=0, paginas=0, linhas=0,
                                detalhes_consultados=0, detalhes_reutilizados=0)
                    with self.lock:
                        self.state['dia_inicio'] = active_time(self.state, time.monotonic())
                    for attempt in range(1, 4):
                        try:
                            self.update(equipes_total=None, equipes_feitas=0, equipe_index=0,
                                        equipe='—', equipe_etapa=0, dia_validado=False,
                                        equipe_acao='Aguardando leitura das tabelas')
                            if scraper is None:
                                verify = os.getenv('ROTALOG_CA_BUNDLE', '').strip() or os.getenv('ROTALOG_VERIFY_TLS', 'true').lower() not in {'0', 'false', 'no', 'off'}
                                scraper = Scraper(os.getenv('ROTALOG_USUARIO'), os.getenv('ROTALOG_SENHA'),
                                                  verify=verify, delay=self.args.intervalo, notify=self.update,
                                                  identity_cache=identities)
                                scraper.login()
                            result = scraper.collect(day, lambda name, text: atomic(folder / 'brutos' / name, text))
                            self.check()
                            self.update(etapa='Salvando arquivos do dia')
                            save_day(folder, result)
                            with self.lock:
                                duration = active_time(self.state, time.monotonic()) - self.state['dia_inicio']
                                self.state['duracoes_dias'] = (self.state['duracoes_dias'] + [duration])[-7:]
                                self.state['dia_inicio'] = None
                            self.update(dias=self.state['dias'] + 1, erro='', dia_validado=True)
                            break
                        except Stopped:
                            raise
                        except Exception as exc:
                            if scraper:
                                scraper.close()
                                scraper = None
                            self.update(erro=f'Tentativa {attempt}/3: {exc}')
                            if attempt == 3:
                                raise
                            for _ in range(20):
                                self.check()
                                self.stop.wait(.25)
                    day += timedelta(days=1)
                self.update(status='Concluído', etapa='Todos os dias gravados e validados')
        except Stopped:
            self.exit_code = 130
            with self.lock:
                self.state.update(status='Interrompido', etapa='Execute novamente para retomar')
        except Exception as exc:
            self.exit_code = 1
            with self.lock:
                self.state.update(status='Erro', erro=str(exc))
        finally:
            if scraper:
                scraper.close()


def progress_bar(done, total, width):
    ratio = min(1.0, max(0.0, done / total)) if total else 0.0
    filled = int(width * ratio)
    return '█' * filled + '░' * (width - filled)


def active_time(state, now):
    paused = state['pausado_total']
    if state['pausa_inicio'] is not None:
        paused += now - state['pausa_inicio']
    return max(0.0, now - state['inicio'] - paused)


def estimate_remaining(state, total, active):
    remaining = max(0, total - state['dias'] - state['retomados'])
    if not remaining:
        return 0.0, False
    samples = state['duracoes_dias']
    started = state['dia_inicio']
    elapsed = max(0, active - started) if started is not None else 0
    teams = state['equipes_total']
    fraction = min(1.0, state['equipes_feitas'] / teams) if teams else 0
    if samples:
        average = sum(samples) / len(samples)
    elif started is not None and fraction >= .1 and elapsed > 0:
        # Primeira previsão inclui pesquisa e paginação já realizadas no dia.
        average = elapsed / fraction
    else:
        return None, True
    if started is None:
        return average * remaining, len(samples) < 3
    day_remaining = max(0, average - elapsed, average * (1 - fraction))
    return day_remaining + (remaining - 1) * average, len(samples) < 3


def eta_text(state, total, now):
    if state['pausa_inicio'] is not None:
        return ' Previsão suspensa durante a pausa'
    if state['status'] in ('Erro', 'Interrompido'):
        return ' Previsão indisponível: coleta interrompida'
    seconds, provisional = estimate_remaining(state, total, active_time(state, now))
    if seconds is None:
        return ' Previsão: calculando o ritmo da coleta…'
    if seconds <= 0:
        return ' Tempo restante: 0 min | Coleta concluída'
    minutes = max(1, int((seconds + 59) // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    duration = f'{days}d {hours:02d}h {mins:02d}min' if days else f'{hours}h {mins:02d}min' if hours else f'{mins} min'
    finish = datetime.now(ZoneInfo('America/Sao_Paulo')) + timedelta(seconds=seconds)
    suffix = ' | provisória' if provisional else ''
    return f' Restante: ~{duration} | Término previsto: {finish:%d/%m/%Y %H:%M}{suffix}'


def elapsed_text(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def screen(run, total, color=False):
    with run.lock:
        s = dict(run.state)
    processed = s['dias'] + s['retomados']
    width = max(1, shutil.get_terminal_size((100, 24)).columns - 1)
    paused = 'PAUSADO' if run.pause.is_set() else s['status']
    now = time.monotonic()
    elapsed = active_time(s, now)
    accumulated = s['ativo_anterior'] + elapsed
    paused_total = s['decorrido_anterior'] + now - s['inicio'] - accumulated
    palette = {'header': '1;38;5;231;48;5;24', 'text': '38;5;252',
               'muted': '38;5;245', 'total': '1;38;5;81', 'daily': '1;38;5;114',
               'team': '1;38;5;177', 'eta': '38;5;222', 'error': '1;38;5;203',
               'keys': '38;5;153', 'track': '38;5;236'}

    def row(text='', style='text'):
        text = text[:width].ljust(width)
        return f'\033[{palette[style]}m{text}\033[0m' if color else text

    def paired(left, right, style):
        gap = max(2, width - len(left) - len(right) - 1)
        return row(left + ' ' * gap + right, style)

    def bar(done, count, style):
        plain = progress_bar(done, count, width)
        if not color:
            return plain
        filled = plain.count('█')
        return (f'\033[{palette[style]}m' + plain[:filled]
                + f'\033[{palette["track"]}m' + plain[filled:] + '\033[0m')

    lines = [
        paired('  RTLWEB  /  COLETA ÚNICA 2026', f'● {paused.upper()}', 'header'),
        row(f'  PERÍODO  {run.args.inicio:%d/%m/%Y} → {run.args.fim:%d/%m/%Y}', 'muted'),
        row(f'  SESSÃO  {elapsed_text(elapsed)}     TOTAL ATIVO  {elapsed_text(accumulated)}     PAUSAS  {elapsed_text(paused_total)}'),
        row(' ' + eta_text(s, total, now), 'eta'),
        row(),
        paired(f'  01  COMPLETO  ·  Dia {s["dia_numero"]} de {total}', f'{100 * processed / total:.1f}%', 'total'),
        bar(processed, total, 'total'),
        row(f'  {processed} dias concluídos     {s["dias"]} gravados agora     {s["retomados"]} já concluídos', 'muted'),
        row(),
    ]
    teams = s['equipes_total']
    if teams is None:
        daily_label = f'  02  DIÁRIO  ·  {s["dia"]}  ·  Identificando equipes'
    elif teams == 0:
        daily_label = f'  02  DIÁRIO  ·  {s["dia"]}  ·  Nenhuma equipe nesta data'
    else:
        current = s['equipe_index'] or s['equipes_feitas']
        daily_label = f'  02  DIÁRIO  ·  {s["dia"]}  ·  Equipe {current} de {teams}'
    daily_percentage = f'{100 * s["equipes_feitas"] / teams:.1f}%' if teams else '—'
    lines += [
        paired(daily_label, daily_percentage, 'daily'),
        bar(s['equipes_feitas'] if teams else int(s['dia_validado']), teams or 1, 'daily'),
        row(f'  {s["equipes_feitas"]} equipes concluídas  ·  {s["etapa"]}  ·  Página {s["pagina"]} de {s["paginas"]}  ·  Linhas {s["linhas"]}', 'muted'),
        row(),
        paired(f'  03  EQUIPE  ·  {s["equipe"]}', f'Etapa {s["equipe_etapa"]} de 3', 'team'),
        bar(s['equipe_etapa'], 3, 'team'),
        row(f'  {s["equipe_acao"]}  ·  Diários consultados: {s["detalhes_consultados"]}  ·  Reaproveitados: {s["detalhes_reutilizados"]}', 'muted'),
        row(),
        row('─' * width, 'track'),
        row(f'  SAÍDA  {run.args.saida}', 'muted'),
        row(f'  AVISO  {s["erro"] or "—"}', 'error' if s['erro'] else 'muted'),
        row('  [ESPAÇO] Pausar / continuar     [Q] Encerrar e retomar depois', 'keys'),
    ]
    return '\n'.join(lines)


@contextmanager
def keyboard():
    if os.name == 'nt':
        import msvcrt
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        ok = kernel.GetConsoleMode(handle, ctypes.byref(mode))
        if ok:
            kernel.SetConsoleMode(handle, mode.value | 4)
        try:
            yield lambda: msvcrt.getwch() if msvcrt.kbhit() else ''
        finally:
            if ok:
                kernel.SetConsoleMode(handle, mode.value)
    else:
        import select
        import termios
        import tty
        original = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        try:
            yield lambda: sys.stdin.read(1) if select.select([sys.stdin], [], [], 0)[0] else ''
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Coleta única e independente do RTLWeb; retoma dias concluídos.')
    parser.add_argument('--inicio', type=date.fromisoformat, default=date(2026, 1, 1), help='AAAA-MM-DD (padrão: 2026-01-01)')
    parser.add_argument('--fim', type=date.fromisoformat, default=datetime.now(ZoneInfo('America/Sao_Paulo')).date() - timedelta(days=1), help='AAAA-MM-DD (padrão: ontem em São Paulo)')
    parser.add_argument('--saida', type=Path, default=HERE / 'dados')
    parser.add_argument('--ssd-mount', type=Path, help='Linux: exigir que a saída permaneça neste SSD montado')
    parser.add_argument('--env', type=Path, default=HERE / '.env' if (HERE / '.env').exists() else HERE.parent / '.env')
    parser.add_argument('--intervalo', type=float, default=.5, help='Espera mínima entre requisições, em segundos')
    parser.add_argument('--sem-tui', action='store_true', help='Progresso em texto para execução sem terminal')
    args = parser.parse_args(argv)
    if args.fim < args.inicio or args.intervalo < 0:
        parser.error('Período inválido ou intervalo negativo.')
    args.saida = args.saida.resolve()
    try:
        configure_ssd(args.ssd_mount, args.saida)
    except (OSError, RuntimeError) as exc:
        parser.error(str(exc))
    load_dotenv(args.env)
    args.sem_tui = args.sem_tui or not (sys.stdin.isatty() and sys.stdout.isatty())
    run = Run(args)
    worker = threading.Thread(target=run.work, daemon=False)
    total = (args.fim - args.inicio).days + 1
    try:
        if args.sem_tui:
            worker.start()
            while worker.is_alive():
                worker.join(.25)
        else:
            with keyboard() as key:
                print('\033[?1049h\033[?25l\033[2J', end='', flush=True)
                try:
                    worker.start()
                    while worker.is_alive():
                        colored = 'NO_COLOR' not in os.environ and os.getenv('TERM') != 'dumb'
                        frame = screen(run, total, color=colored)
                        print('\033[H' + frame.replace('\n', '\033[K\n') + '\033[K\033[J', end='', flush=True)
                        pressed = key().lower()
                        if pressed == 'q':
                            run.stop.set()
                        elif pressed == ' ':
                            run.toggle_pause()
                        worker.join(.15)
                finally:
                    print('\033[?25h\033[?1049l', end='', flush=True)
    except KeyboardInterrupt:
        run.stop.set()
        print('\nEncerrando após a requisição atual…', flush=True)
        while worker.is_alive():
            worker.join(.25)
    print(f'{run.state["status"]}: {run.state["dias"]} dias gravados, {run.state["retomados"]} retomados. {args.saida}')
    print(f'Tempo ativo acumulado: {elapsed_text(run.state["ativo_anterior"] + active_time(run.state, time.monotonic()))}')
    if run.state['erro']:
        print(run.state['erro'], file=sys.stderr)
    return run.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
