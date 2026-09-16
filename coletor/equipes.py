"""Identidade canônica das equipes ROTALOG."""

from __future__ import annotations

import re


_AUTOTRACK_SUFFIX = re.compile(r"\s*\(\s*\d+\s*\)\s*$")
_REGIONAL_PREFIXES = ("CA", "CB", "LO", "MA", "PG")


def normalize_team_key(value: object) -> str:
    """Retorna a equipe base; E3T01(2), E3T01(3), ... pertencem a E3T01."""
    code = str(value or "").strip().upper()
    code = _AUTOTRACK_SUFFIX.sub("", code)
    return re.sub(r"[^A-Z0-9_-]+", "", code)


def valid_team_key(value: object) -> bool:
    """Valida a identidade depois de remover o sufixo de dispositivo AUTOTRACK."""
    return bool(re.fullmatch(r"E[A-Z0-9]{3,7}", normalize_team_key(value)))


def canonicalize_team_snapshots(snapshots: dict) -> dict:
    """Migra chaves AUTOTRACK antigas para a identidade base da equipe."""
    canonical: dict = {}
    for source_key, source_document in snapshots.items():
        if not isinstance(source_document, dict):
            continue
        team_key = normalize_team_key(
            source_document.get("teamKey") or source_document.get("equipe") or source_key
        )
        if not valid_team_key(team_key):
            continue
        document = dict(source_document)
        document["teamKey"] = team_key
        document["equipe"] = team_key
        current = canonical.get(team_key)
        if current is None or str(document.get("updatedAt") or "") > str(current.get("updatedAt") or ""):
            canonical[team_key] = document
    return canonical


def _numeric_tablet_id(raw_id: str) -> str:
    clean = str(raw_id or "").strip().upper().replace(" ", "")
    for prefix in _REGIONAL_PREFIXES:
        if clean.startswith(prefix) and len(clean) > len(prefix):
            return clean[len(prefix):]
    return clean


def resolve_team_group(
    meta: dict[str, str],
    identificador_para_equipe: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve a equipe base pelo código, dispositivo ou integrantes do grupo."""
    resolved = dict(meta)
    team_original = str(meta.get("equipe_codigo") or "").strip().upper()
    device = str(meta.get("veiculo") or "").strip().upper().replace(" ", "")
    member = str(meta.get("colaborador") or "").strip().upper()

    resolved["equipe_codigo_original"] = team_original
    resolved["identificador_equipamento"] = device
    resolved["origem_resolucao"] = "PREFIXO_EQUIPE"

    if valid_team_key(team_original):
        resolved["equipe_codigo"] = normalize_team_key(team_original)
        return resolved
    if valid_team_key(device):
        resolved["equipe_codigo"] = normalize_team_key(device)
        resolved["origem_resolucao"] = "VEICULO_COM_PREFIXO_EQUIPE"
        return resolved

    lookup = {
        str(key).strip().upper(): normalize_team_key(value)
        for key, value in (identificador_para_equipe or {}).items()
    }
    mapped_team = lookup.get(device)
    if valid_team_key(mapped_team):
        resolved["equipe_codigo"] = mapped_team
        resolved["origem_resolucao"] = "IDENTIFICACAO_TABLET"
        return resolved

    numeric_id = _numeric_tablet_id(device)
    numeric_team = lookup.get(f"NUMERIC_TABLET:{numeric_id}") if numeric_id else None
    if valid_team_key(numeric_team):
        resolved["equipe_codigo"] = numeric_team
        resolved["origem_resolucao"] = "TABLET_NUMERICO_TRANSITORIO"
        return resolved

    if member:
        tokens = [token for token in re.sub(r"[^A-Z0-9\s]", "", member).split() if len(token) >= 3]
        candidates: dict[str, int] = {}
        for key, team_code in lookup.items():
            if key.startswith("MEMBER_NAME:"):
                for token in tokens:
                    if token in key[12:]:
                        candidates[team_code] = candidates.get(team_code, 0) + 1
        if candidates:
            ranked = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
            top_team, top_score = ranked[0]
            if top_score >= 1 and (len(ranked) == 1 or top_score > ranked[1][1]):
                resolved["equipe_codigo"] = top_team
                resolved["origem_resolucao"] = "INTEGRANTES_EQUIPE"
                return resolved
            if top_score >= 2:
                resolved["equipe_codigo"] = top_team
                resolved["origem_resolucao"] = "INTEGRANTES_EQUIPE"
                return resolved

    resolved["equipe_codigo"] = ""
    resolved["origem_resolucao"] = "NAO_RELACIONADO"
    return resolved
