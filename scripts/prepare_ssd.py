"""Verifica ou prepara um SSD já montado. Não formata, monta ou apaga a origem."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coletor.data_storage import mounted_disk, prepare_ssd, select_storage


def require_stopped_services():
    units = [name + ".service" for name in ("rotalog", "rotalog-history", "rotalog-monthly", "fleet-agent", "torre-api")]
    units += [name + ".timer" for name in ("rotalog-history", "rotalog-monthly", "fleet-agent")]
    for unit in units:
        result = subprocess.run(["systemctl", "show", unit, "--property=ActiveState", "--value"],
                                text=True, capture_output=True, timeout=5, check=True)
        if result.stdout.strip() in ("active", "activating", "deactivating", "reloading"):
            raise RuntimeError("Parar " + unit + " antes da cópia")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount", type=Path, default=Path("/mnt/ssd"))
    parser.add_argument("--source", type=Path, default=ROOT / "dados-local")
    parser.add_argument("--empresa", default="ChicoEletro")
    parser.add_argument("--apply", action="store_true", help="Copiar/verificar e habilitar a seleção automática")
    args = parser.parse_args()
    if not args.apply:
        disk = mounted_disk(args.mount)
        print(json.dumps({"disk": disk.__dict__ if disk else None,
                          "selection": select_storage(args.source, args.empresa, mode="auto",
                                                      mount=args.mount, writable=False).payload()}, indent=2))
        return 0
    if os.name != "posix":
        parser.error("Preparação deve ser executada no Orange Linux")
    if args.source.resolve() != (ROOT / "dados-local").resolve():
        parser.error("--apply exige a origem dados-local deste checkout para manter os serviços compatíveis")
    if (ROOT / ".storage.env").exists():
        parser.error(".storage.env já existe: conferir a configuração antes de alterar")
    require_stopped_services()
    selection = prepare_ssd(args.source, args.mount, args.empresa)
    # Não é segredo; não salvar no Git porque cada instalação tem seus caminhos.
    values = {"DDS_STORAGE_MODE": "auto", "DDS_SSD_MOUNT": str(args.mount.resolve()),
              "DDS_CARD_DATA_DIR": str(args.source.resolve())}
    for value in values.values():
        if any(c in value for c in ('\n', '\r', '"', '\\')):
            raise ValueError("Caminho não suportado pela configuração")
    config = ROOT / ".storage.env"
    with config.open("x", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f'{key}="{value}"\n')
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(selection.payload(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
