"""Exporta cadastro usando a leitura normalizada do DDS; publicação explícita."""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coletor.storage_io import write_json
from coletor.torre_inputs import source_prefix, validate_catalog


def build_catalog(teams, company, aliases_for, now=None):
    out = {}
    for original_key, team in teams.items():
        key = str(original_key).strip().upper()
        if key in out:
            raise ValueError("Identidade de cadastro duplicada")
        if not isinstance(team, dict):
            raise ValueError("Equipe inválida")
        # Preserva metadados da leitura oficial, incluindo equipment e members.
        out[key] = {**team, "teamKey": key,
                    "aliases": sorted(set(aliases_for(original_key, team)) | {key})}
    payload = {"schemaVersion": 1, "company": company,
               "generatedAt": (now or datetime.now(timezone.utc)).isoformat(), "teams": out}
    return validate_catalog(payload, company)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dds-root", required=True, help="Pasta dds-webtools com ambiente DDS disponível")
    parser.add_argument("--empresa", default="ChicoEletro")
    parser.add_argument("--output", required=True)
    parser.add_argument("--credentials", help="Arquivo de credenciais; conteúdo nunca é exibido")
    parser.add_argument("--publish", action="store_true", help="Publicar cadastro no Storage")
    parser.add_argument("--bucket", default="dds-treinamentos.firebasestorage.app")
    parser.add_argument("--prefix", default=None)
    args = parser.parse_args()
    # O cadastro atual dds_teams é global, sem isolamento por empresa.
    if args.empresa != "ChicoEletro":
        parser.error("Exportação de dds_teams global validada apenas para ChicoEletro")
    prefix = source_prefix(args.empresa, args.prefix)
    dds_root = Path(args.dds_root).resolve()
    if not (dds_root / "monitor/services/teams_service.py").is_file():
        parser.error("--dds-root deve apontar para dds-webtools")
    sys.path.insert(0, str(dds_root))
    sys.path.insert(0, str(dds_root / "monitor"))
    if args.credentials:
        credentials = Path(args.credentials).resolve()
        if not credentials.is_file():
            parser.error("Arquivo de credenciais não encontrado")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials)
    from monitor.services.teams_service import clear_teams_cache, list_teams_map, _get_team_aliases
    clear_teams_cache()
    payload = build_catalog(list_teams_map(active=None), args.empresa, _get_team_aliases)
    write_json(Path(args.output), payload)
    if args.publish:
        from coletor.storage_io import RotalogGcsSnapshotStore
        name = prefix + "/cadastro/current.json.gz"
        RotalogGcsSnapshotStore(args.bucket, name).save_blob(name, payload)
    print("Cadastro validado: " + str(len(payload["teams"])) + " equipes; publicado=" + str(args.publish))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
