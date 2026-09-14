"""Módulo de gerenciamento de logs de execução e leitor incremental.

Implementa:
1. Rotação diária com compactação automática em GZIP (.jsonl.gz).
2. Política de retenção de arquivos históricos compactados.
3. Leitor incremental (JsonlTailReader) resiliente a concorrência e rotação para a TUI.
"""

from __future__ import annotations

import datetime
import gzip
import json
import logging
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOG = logging.getLogger("coletor-logs")
TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))


def _extract_date_from_entry(entry: dict[str, Any]) -> str | None:
    for key in ("finishedAt", "startedAt"):
        val = entry.get(key)
        if isinstance(val, str) and len(val) >= 10:
            try:
                datetime.date.fromisoformat(val[:10])
                return val[:10]
            except ValueError:
                pass
    return None


def compress_file_gzip(source_path: Path, target_path: Path) -> None:
    """Compacta source_path para target_path (.gz) de forma atômica."""
    temp_target = target_path.with_suffix(".tmp.gz")
    try:
        with source_path.open("rb") as f_in, gzip.open(temp_target, "wb", compresslevel=9) as f_out:
            while chunk := f_in.read(64 * 1024):
                f_out.write(chunk)
        if temp_target.exists():
            temp_target.replace(target_path)
    finally:
        if temp_target.exists():
            try:
                temp_target.unlink()
            except OSError:
                pass


def append_to_gzip(target_gz_path: Path, lines: list[str]) -> None:
    """Anexa linhas de texto em um arquivo .jsonl.gz existente ou cria um novo."""
    target_gz_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target_gz_path, "at", encoding="utf-8", compresslevel=9) as stream:
        for line in lines:
            line_str = line.strip()
            if line_str:
                stream.write(line_str + "\n")


def rotate_daily_execution_logs(
    log_path: Path,
    current_day: str,
    retention_days: int = 30,
) -> None:
    """Verifica se há entradas de dias anteriores no log ativo e as compacta.
    
    Qualquer dia encerrado (< current_day) é compactado para execucoes-AAAA-MM-DD.jsonl.gz.
    O arquivo ativo passa a conter apenas as entradas do dia corrente.
    Arquivos compactados com mais de retention_days são removidos.
    """
    if not log_path.is_file() or log_path.stat().st_size == 0:
        return

    try:
        lines = []
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line_str = line.strip()
                if line_str:
                    lines.append(line_str)

        if not lines:
            return

        # Agrupa linhas por dia
        by_day: dict[str, list[str]] = {}
        for line_str in lines:
            day = None
            try:
                data = json.loads(line_str)
                day = _extract_date_from_entry(data)
            except Exception:
                pass
            
            day = day or current_day
            by_day.setdefault(day, []).append(line_str)

        # Se há entradas de dias anteriores a hoje
        days_to_archive = [d for d in by_day if d < current_day]
        if days_to_archive:
            for past_day in days_to_archive:
                archive_file = log_path.parent / f"execucoes-{past_day}.jsonl.gz"
                append_to_gzip(archive_file, by_day[past_day])
                LOG.info("Logs do dia %s compactados em %s", past_day, archive_file.name)

            # Reescreve o log ativo apenas com as linhas do dia de hoje (ou posteriores)
            current_day_lines = []
            for d in sorted(by_day.keys()):
                if d >= current_day:
                    current_day_lines.extend(by_day[d])

            temp_log = log_path.with_suffix(".tmp")
            with temp_log.open("w", encoding="utf-8") as stream:
                for line_str in current_day_lines:
                    stream.write(line_str + "\n")
            temp_log.replace(log_path)
    except Exception as exc:
        LOG.warning("Erro ao verificar/rotacionar logs diários em %s: %s", log_path, exc)

    # Purga de arquivos com mais de retention_days
    prune_old_log_archives(log_path.parent, current_day, retention_days=retention_days)


def prune_old_log_archives(
    logs_dir: Path,
    current_day: str,
    retention_days: int = 30,
) -> int:
    """Remove arquivos execucoes-AAAA-MM-DD.jsonl.gz com mais de retention_days."""
    if not logs_dir.is_dir() or retention_days <= 0:
        return 0

    deleted = 0
    try:
        current_date = datetime.date.fromisoformat(current_day)
        cutoff_date = current_date - datetime.timedelta(days=retention_days)

        for item in logs_dir.glob("execucoes-*.jsonl.gz"):
            # Nome esperado: execucoes-AAAA-MM-DD.jsonl.gz
            stem = item.name.replace("execucoes-", "").replace(".jsonl.gz", "")
            try:
                file_date = datetime.date.fromisoformat(stem)
                if file_date < cutoff_date:
                    item.unlink()
                    deleted += 1
                    LOG.info("Arquivo de log antigo purgado: %s", item.name)
            except ValueError:
                continue
    except Exception as exc:
        LOG.debug("Falha durante a purga de logs antigos: %s", exc)

    return deleted


def record_execution_log(
    log_path: Path,
    result: dict[str, Any],
    retention_days: int = 30,
) -> None:
    """Grava o resultado do ciclo com rotação diária automática e compactação GZIP."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    current_day = _extract_date_from_entry(result) or datetime.datetime.now(TZ).date().isoformat()

    # Executa rotação antes da escrita se houver dados do dia anterior
    rotate_daily_execution_logs(log_path, current_day, retention_days=retention_days)

    # Grava a linha do ciclo atual
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        stream.flush()


class JsonlTailReader:
    """Leitor de cauda de arquivo JSONL resiliente a concorrência e rotação de logs.
    
    Projetado para leitura instantânea O(1) de novos ciclos adicionados ao final do arquivo,
    sem reler ou processar o histórico anterior a cada verificação.
    """

    def __init__(self, file_path: Path, max_history: int = 50):
        self.file_path = file_path
        self.max_history = max_history
        self.offset = 0
        self.last_inode: int | None = None
        self.last_mtime: float = 0.0
        self.last_size: int = 0

    def read_initial(self) -> list[dict[str, Any]]:
        """Lê os últimos max_history registros do arquivo em bloco reverso sem carregar o arquivo todo."""
        if not self.file_path.is_file():
            self.offset = 0
            self.last_inode = None
            self.last_mtime = 0.0
            self.last_size = 0
            return []

        st = self.file_path.stat()
        file_size = st.st_size
        self.last_mtime = st.st_mtime
        self.last_size = file_size
        self.last_inode = getattr(st, "st_ino", None)

        if file_size == 0:
            self.offset = 0
            return []

        # Lê bloco do final do arquivo (até 64KB ou arquivo inteiro se menor)
        block_size = min(file_size, max(64 * 1024, self.max_history * 1024))
        read_start = max(0, file_size - block_size)

        with self.file_path.open("rb") as stream:
            stream.seek(read_start)
            raw_bytes = stream.read()

        # O offset do leitor é posicionado no final do arquivo atual
        self.offset = file_size

        text = raw_bytes.decode("utf-8", errors="replace")
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        # Se o bloco cortou a primeira linha no meio e não lemos desde o início, descarta a primeira linha parcial
        if read_start > 0 and lines:
            lines = lines[1:]

        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue

        return entries[-self.max_history:]

    def read_incremental(self) -> tuple[list[dict[str, Any]], bool]:
        """Retorna (novos_registros, foi_rotacionado).
        
        Se o arquivo foi truncado ou substituído, recarrega a cauda e retorna rotated=True.
        Se apenas novas linhas foram anexadas, lê estritamente os bytes novos desde self.offset.
        """
        if not self.file_path.is_file():
            return [], False

        try:
            st = self.file_path.stat()
        except OSError:
            return [], False

        current_inode = getattr(st, "st_ino", None)
        current_size = st.st_size

        # Detecção de rotação:
        # 1. Tamanho do arquivo diminuiu em relação ao offset anterior (arquivo truncado ou novo).
        # 2. Inode mudou (arquivo substituído ou recriado).
        is_rotated = False
        if current_size < self.offset:
            is_rotated = True
        elif self.last_inode is not None and current_inode != self.last_inode and (current_inode or 0) > 0:
            is_rotated = True

        if is_rotated:
            new_tail = self.read_initial()
            return new_tail, True

        # Se tamanho e mtime não mudaram, não há nada de novo
        if st.st_mtime == self.last_mtime and current_size == self.last_size:
            return [], False

        # Se nada foi adicionado após o offset atual
        if current_size <= self.offset:
            self.last_mtime = st.st_mtime
            self.last_size = current_size
            return [], False

        # Lê apenas os bytes novos a partir do offset
        with self.file_path.open("rb") as stream:
            stream.seek(self.offset)
            new_bytes = stream.read()

        if not new_bytes:
            return [], False

        # Proteção contra escrita concorrente: se não terminar em \n, há uma linha em gravação incompleta
        last_newline = new_bytes.rfind(b"\n")
        if last_newline == -1:
            # Nenhuma linha completa gravada ainda; aguarda o próximo ciclo
            return [], False

        valid_bytes = new_bytes[: last_newline + 1]
        self.offset += len(valid_bytes)
        self.last_mtime = st.st_mtime
        self.last_size = current_size
        self.last_inode = current_inode

        text = valid_bytes.decode("utf-8", errors="replace")
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        new_entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                new_entries.append(json.loads(line))
            except Exception:
                continue

        return new_entries, False
