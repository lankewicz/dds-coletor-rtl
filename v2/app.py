"""Ponto de entrada do coletor local ROTALOG v2."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

from dotenv import load_dotenv


V2_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = V2_ROOT.parent
sys.path.insert(0, str(V2_ROOT))
load_dotenv(V2_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env", override=False)

from rotalog_v2.client import RotalogClient, RotalogCredentials
from rotalog_v2.runner import LocalCollector
from rotalog_v2.storage import LocalSnapshotStore


def _environment_boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coletor experimental ROTALOG somente local")
    parser.add_argument("--once", action="store_true", help="Executa um ciclo e encerra")
    parser.add_argument("--interval-seconds", type=int, default=180)
    parser.add_argument("--output-dir", type=Path, default=V2_ROOT / "dados-local")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    if args.interval_seconds < 10:
        raise SystemExit("--interval-seconds deve ser pelo menos 10")
    collector = LocalCollector(
        RotalogClient(
            RotalogCredentials.from_environment(),
            verify_tls=_environment_boolean("ROTALOG_VERIFY_TLS", False),
        ),
        LocalSnapshotStore(args.output_dir.resolve()),
        os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"),
    )
    while True:
        try:
            print(json.dumps(collector.run_once(), ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
