#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SSD_MOUNT="${COLETA_UNICA_SSD_MOUNT:-/mnt/ssd}"
OUTPUT_DIR="${COLETA_UNICA_SAIDA:-${SSD_MOUNT}/dds-coletor-rtl/coleta-unica/dados}"
PYTHON_BIN="${COLETA_UNICA_PYTHON:-}"

if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in "$APP_DIR/.venv-3.14/bin/python" "$APP_DIR/.venv/bin/python" "$APP_DIR/venv/bin/python"; do
    if [[ -x "$candidate" ]]; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo 'Ambiente Python não encontrado. Configure COLETA_UNICA_PYTHON ou siga ORANGE_PI.md.' >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1
cd -- "$APP_DIR"
exec "$PYTHON_BIN" -m coleta_unica --ssd-mount "$SSD_MOUNT" --saida "$OUTPUT_DIR" "$@"
