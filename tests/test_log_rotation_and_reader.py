"""Testes para rotação diária com compactação GZIP e leitor incremental JsonlTailReader."""

from __future__ import annotations

import datetime
import gzip
import json
import tempfile
from pathlib import Path
import unittest

from coletor.logs import (
    JsonlTailReader,
    record_execution_log,
    rotate_daily_execution_logs,
    prune_old_log_archives,
)


class LogRotationAndReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.log_path = self.root / "rotalog" / "logs" / "execucoes.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_record_execution_log_appends_normally_same_day(self):
        e1 = {"status": "success", "finishedAt": "2026-09-14T10:00:00-03:00", "totalTeams": 10}
        e2 = {"status": "success", "finishedAt": "2026-09-14T10:02:00-03:00", "totalTeams": 11}

        record_execution_log(self.log_path, e1)
        record_execution_log(self.log_path, e2)

        self.assertTrue(self.log_path.is_file())
        lines = self.log_path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["totalTeams"], 10)
        self.assertEqual(json.loads(lines[1])["totalTeams"], 11)

    def test_rotate_daily_archives_previous_day_to_gzip(self):
        # Cria registros do dia anterior
        yesterday_entry1 = {
            "status": "success",
            "startedAt": "2026-09-13T23:56:00-03:00",
            "finishedAt": "2026-09-13T23:58:00-03:00",
            "totalTeams": 5,
        }
        yesterday_entry2 = {
            "status": "success",
            "startedAt": "2026-09-13T23:58:00-03:00",
            "finishedAt": "2026-09-13T23:59:59-03:00",
            "totalTeams": 6,
        }
        record_execution_log(self.log_path, yesterday_entry1)
        record_execution_log(self.log_path, yesterday_entry2)

        # Agora grava o primeiro registro do novo dia (2026-09-14)
        today_entry = {
            "status": "success",
            "startedAt": "2026-09-14T00:00:01-03:00",
            "finishedAt": "2026-09-14T00:02:00-03:00",
            "totalTeams": 7,
        }
        record_execution_log(self.log_path, today_entry)

        # 1. Verifica se o arquivo arquivado do dia 2026-09-13 foi criado compactado
        archive_path = self.log_path.parent / "execucoes-2026-09-13.jsonl.gz"
        self.assertTrue(archive_path.is_file(), "Arquivo compactado de ontem deve existir")

        with gzip.open(archive_path, "rt", encoding="utf-8") as stream:
            archived_lines = [l.strip() for l in stream if l.strip()]
        self.assertEqual(len(archived_lines), 2)
        self.assertEqual(json.loads(archived_lines[0])["totalTeams"], 5)
        self.assertEqual(json.loads(archived_lines[1])["totalTeams"], 6)

        # 2. Verifica se o arquivo ativo execucoes.jsonl contém unicamente o registro de hoje
        current_lines = self.log_path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(current_lines), 1)
        self.assertEqual(json.loads(current_lines[0])["totalTeams"], 7)

    def test_prune_old_log_archives(self):
        logs_dir = self.log_path.parent
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Cria arquivo com 45 dias de idade
        old_archive = logs_dir / "execucoes-2026-07-30.jsonl.gz"
        old_archive.write_bytes(b"dummy")

        # Cria arquivo com 10 dias de idade
        recent_archive = logs_dir / "execucoes-2026-09-04.jsonl.gz"
        recent_archive.write_bytes(b"dummy")

        pruned = prune_old_log_archives(logs_dir, current_day="2026-09-14", retention_days=30)
        self.assertEqual(pruned, 1)
        self.assertFalse(old_archive.exists(), "Arquivo antigo deve ter sido removido")
        self.assertTrue(recent_archive.exists(), "Arquivo recente deve ser preservado")

    def test_jsonl_tail_reader_initial_read_and_incremental(self):
        reader = JsonlTailReader(self.log_path, max_history=3)
        self.assertEqual(reader.read_initial(), [])

        # Grava 5 registros
        for i in range(1, 6):
            entry = {"id": i, "status": "success", "finishedAt": "2026-09-14T10:00:00-03:00"}
            record_execution_log(self.log_path, entry)

        # Leitura inicial com max_history=3 deve retornar apenas os últimos 3 (3, 4, 5)
        initial = reader.read_initial()
        self.assertEqual(len(initial), 3)
        self.assertEqual([e["id"] for e in initial], [3, 4, 5])

        # Leitura incremental sem novas entradas deve retornar vazio
        new_entries, rotated = reader.read_incremental()
        self.assertEqual(new_entries, [])
        self.assertFalse(rotated)

        # Adiciona mais 2 registros
        record_execution_log(self.log_path, {"id": 6, "status": "success", "finishedAt": "2026-09-14T10:02:00-03:00"})
        record_execution_log(self.log_path, {"id": 7, "status": "success", "finishedAt": "2026-09-14T10:04:00-03:00"})

        # Leitura incremental deve retornar exatamente os 2 novos registros
        new_entries, rotated = reader.read_incremental()
        self.assertEqual(len(new_entries), 2)
        self.assertEqual([e["id"] for e in new_entries], [6, 7])
        self.assertFalse(rotated)

    def test_jsonl_tail_reader_detects_rotation(self):
        reader = JsonlTailReader(self.log_path, max_history=10)

        # Grava 3 registros do dia anterior
        for i in range(1, 4):
            record_execution_log(
                self.log_path,
                {"id": i, "finishedAt": "2026-09-13T10:00:00-03:00"}
            )

        initial = reader.read_initial()
        self.assertEqual(len(initial), 3)

        # Simula rotação diária ao gravar entrada do novo dia
        new_day_entry = {"id": 100, "finishedAt": "2026-09-14T08:00:00-03:00"}
        record_execution_log(self.log_path, new_day_entry)

        # O leitor incremental deve detectar que o arquivo encolheu (foi rotacionado)
        new_entries, rotated = reader.read_incremental()
        self.assertTrue(rotated, "Deve detectar rotação de arquivo")
        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0]["id"], 100)

    def test_jsonl_tail_reader_ignores_partial_line_until_newline(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            json.dumps({"id": 1}) + "\n",
            encoding="utf-8",
        )

        reader = JsonlTailReader(self.log_path, max_history=10)
        self.assertEqual(len(reader.read_initial()), 1)

        # Escreve linha parcial sem o '\n' final (escrita em andamento)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write('{"id": 2, "msg": "incompleto"')
            stream.flush()

        # O leitor incremental não deve consumir nem falhar com JSONDecodeError
        new_entries, rotated = reader.read_incremental()
        self.assertEqual(new_entries, [])

        # Completa a linha com o resto e '\n'
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write('}\n')
            stream.flush()

        # Agora deve ler a linha 2 completa
        new_entries, rotated = reader.read_incremental()
        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0]["id"], 2)


if __name__ == "__main__":
    unittest.main()
