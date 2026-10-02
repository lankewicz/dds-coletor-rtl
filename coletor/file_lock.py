"""Bloqueio de arquivo entre processos para atualizações locais compartilhadas."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


def _try_lock(stream: TextIO) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, BlockingIOError):
        return False


def _unlock(stream: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(path: Path, timeout_seconds: float = 120.0) -> Iterator[None]:
    """Obtém um lock exclusivo, liberado automaticamente até em caso de erro."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with path.open("a+", encoding="ascii") as stream:
        if stream.tell() == 0:
            stream.write("0")
            stream.flush()
        while not _try_lock(stream):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Tempo esgotado aguardando bloqueio de {path}")
            time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(stream)
