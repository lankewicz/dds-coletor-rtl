"""Resumo histórico local para a TUI quando o tempo real não está disponível."""
from datetime import date, timedelta
from pathlib import Path

from coletor.equipes import normalize_team_key
from coletor.fleet_control import parse_iso
from coletor.storage import company_key, load_json_with_status


def daily_rows(payload):
    """Prefere a medição oficial; formatos antigos usam a listagem de serviços."""
    official = (payload.get("resumoEquipes") or {}).get("registros")
    totals = {}
    if isinstance(official, list):
        for record in official:
            key = normalize_team_key(record.get("equipe"))
            if not key:
                continue
            row = totals.setdefault(key, [0, 0.0, 0.0])
            row[0] += int(record["servicosExecutados"])
            row[1] += float(record["kmInformado"])
            row[2] += float(record["kmAutorizadoFinal"])
    else:
        for key, total in (payload.get("totaisPorEquipe") or {}).items():
            totals[normalize_team_key(key)] = [0, float(total["kmInformado"]), float(total["kmAutorizadoFinal"])]
        services = payload.get("servicos")
        if services is None:
            services = list((payload.get("protocolos") or {}).values())
        for service in services:
            key = normalize_team_key(service.get("equipe"))
            if key in totals:
                totals[key][0] += 1
    return [(key, *values) for key, values in sorted(totals.items())]


def load_latest_daily(output_dir: Path, company: str, now):
    """Lê apenas arquivos locais, completos e da empresa selecionada."""
    directory = output_dir / "rotalog" / "quilometragem" / "diario"
    errors = []
    for path in sorted(directory.glob("*.json.gz"), reverse=True):
        try:
            day = date.fromisoformat(path.name.removesuffix(".json.gz"))
        except ValueError:
            continue
        if day >= now.date():
            continue
        payload, status = load_json_with_status(path, {})
        try:
            if status != "ok" or payload.get("data") != day.isoformat():
                raise ValueError("arquivo incompleto ou corrompido")
            identity = payload.get("empresaKey") or payload.get("empresa")
            if not identity or company_key(identity) != company_key(company):
                raise ValueError("empresa divergente")
            stamp = parse_iso(payload.get("collectedAt"))
            if not stamp or stamp > now:
                raise ValueError("horário de coleta inválido")
            if not isinstance((payload.get("resumoEquipes") or {}).get("registros"), list) and not isinstance(payload.get("totaisPorEquipe"), dict):
                raise ValueError("resumo diário ausente")
            rows = daily_rows(payload)
            return {"data": day.isoformat(), "collectedAt": stamp.isoformat(), "rows": rows,
                    "outdated": day < now.date() - timedelta(days=1)}, "; ".join(errors) or None
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(f"{day}: {exc}")
    return {}, "; ".join(errors) or "Nenhuma coleta diaria local disponivel"


def use_daily(data, last, now):
    if not data.get("daily_snapshot"):
        return False
    stamp = parse_iso(data.get("teams_collected_at"))
    return bool(data.get("teams_error") or last.get("status") == "error" or not stamp
                or now - stamp > timedelta(seconds=data["teams_stale_seconds"]))
