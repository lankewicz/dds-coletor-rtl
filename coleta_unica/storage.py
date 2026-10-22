"""Gravação atômica e proteção opcional de montagem no Linux."""
import os
import re
from pathlib import Path


def mount_token(mount):
    for line in Path('/proc/self/mountinfo').read_text(encoding='utf-8').splitlines():
        left, right = line.split(' - ', 1)
        fields = left.split()
        location = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
        if Path(location).resolve() == mount:
            return fields[0], fields[2], right.split()[1]
    raise RuntimeError(f'SSD não está montado em {mount}. Nenhum fallback para o cartão.')


class SSDGuard:
    def __init__(self, mount, output):
        self.mount = Path(mount).resolve()
        self.output = Path(output).resolve()
        if self.mount == Path(self.mount.anchor):
            raise RuntimeError('Informe o ponto de montagem do SSD, não a raiz do sistema.')
        self.token = mount_token(self.mount)
        self.device = self.mount.stat().st_dev
        self.check(self.output)

    def check(self, path=None):
        if mount_token(self.mount) != self.token or self.mount.stat().st_dev != self.device:
            raise RuntimeError('A montagem do SSD mudou. Reinicie a coleta com o disco correto.')
        path = Path(path or self.output).resolve()
        if not path.is_relative_to(self.output) or not self.output.is_relative_to(self.mount):
            raise RuntimeError(f'Destino fora da pasta de coleta no SSD: {path}')
        ancestor = path
        while not ancestor.exists():
            ancestor = ancestor.parent
        if ancestor.stat().st_dev != self.device:
            raise RuntimeError(f'O destino não pertence ao SSD selecionado: {path}')


_guard = None


def configure_ssd(mount, output):
    global _guard
    _guard = SSDGuard(mount, output) if mount else None


def check_storage(path=None):
    if _guard is not None:
        _guard.check(path)


def atomic_text(path, text, encoding='utf-8'):
    path = Path(path)
    check_storage(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    check_storage(temporary)
    with temporary.open('w', encoding=encoding) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    check_storage(path)
    temporary.replace(path)
