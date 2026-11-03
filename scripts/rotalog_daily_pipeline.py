"""Executa a coleta unica antes da reconciliacao, sem impedir o fallback normal."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from coletor.historico_agenda import janela_do_dia

ROOT = Path(__file__).resolve().parents[1]
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def collection_window(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    window = janela_do_dia(today)
    return today - datetime.timedelta(days=window), today - datetime.timedelta(days=1)


def run_pipeline(today: datetime.date | None = None) -> int:
    environment = os.environ.copy()
    ssd_mount = Path(environment.get("COLETA_UNICA_SSD_MOUNT", "/mnt/ssd")).expanduser()
    output_dir = Path(environment.get(
        "COLETA_UNICA_SAIDA",
        str(ssd_mount / "dds-coletor-rtl" / "coleta-unica" / "dados"),
    )).expanduser()
    today = today or datetime.datetime.now(LOCAL_TZ).date()
    start, end = collection_window(today)
    unique_collection = subprocess.run(
        [
            sys.executable, "-m", "coleta_unica",
            "--inicio", start.isoformat(),
            "--fim", end.isoformat(),
            "--refazer",
            "--ssd-mount", str(ssd_mount),
            "--saida", str(output_dir),
            "--env", str(ROOT / ".env"),
            "--sem-tui",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
    )
    normal_collection = subprocess.run(
        [
            sys.executable, "main.py", "--reconciliar-agendado", "--firebase",
            "--output-dir", str(ROOT / "dados-local"),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
    )
    return normal_collection.returncode or unique_collection.returncode


if __name__ == "__main__":
    raise SystemExit(run_pipeline())
