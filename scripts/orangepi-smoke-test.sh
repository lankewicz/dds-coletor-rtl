#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-/var/tmp/dds-coletor-rtl-teste}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"

case "${OUTPUT_DIR}" in
  /|/opt|/opt/dds-coletor-rtl|/var/lib/dds-coletor|/var/lib/dds-coletor/)
    echo "ERRO: diretório de saída reservado para produção: ${OUTPUT_DIR}" >&2
    exit 2
    ;;
esac

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERRO: ambiente virtual não encontrado em ${PYTHON_BIN}" >&2
  echo "Crie-o com: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 2
fi

if [[ -z "${ROTALOG_USUARIO:-}" || -z "${ROTALOG_SENHA:-}" ]]; then
  if [[ ! -f "${APP_DIR}/.env" ]]; then
    echo "ERRO: credenciais ausentes; configure ROT... no .env da pasta de teste." >&2
    exit 2
  fi
fi

mkdir -p "${OUTPUT_DIR}"

echo "[1/3] Executando testes automatizados..."
"${PYTHON_BIN}" -m unittest discover -s "${APP_DIR}/tests"

echo "[2/3] Coletando ontem sem Firebase em ${OUTPUT_DIR}..."
"${PYTHON_BIN}" "${APP_DIR}/main.py" \
  --historico ontem \
  --no-firebase \
  --output-dir "${OUTPUT_DIR}"

YESTERDAY="$(date -d yesterday +%F)"
RAW_FILE="${OUTPUT_DIR}/rotalog/eventos/diario/${YESTERDAY}.json.gz"
KM_FILE="${OUTPUT_DIR}/rotalog/quilometragem/diario/${YESTERDAY}.json.gz"

echo "[3/3] Validando artefatos locais..."
test -s "${RAW_FILE}"
test -s "${KM_FILE}"

"${PYTHON_BIN}" - "${RAW_FILE}" "${KM_FILE}" <<'PY'
import gzip
import json
import sys

for filename in sys.argv[1:]:
    with gzip.open(filename, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or not payload:
        raise SystemExit(f"Artefato inválido: {filename}")
    print(f"OK: {filename}")
PY

echo "Smoke test concluído. Produção e Firebase não foram alterados."
