"""Reconciliação diária recuperável das janelas de 7, 30 e 60 dias."""

from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .file_lock import exclusive_file_lock
from .storage import company_key, load_json, write_json

LOG = logging.getLogger(__name__)


def janela_do_dia(today: datetime.date) -> int:
    if today.day == 10:
        return 60
    return 30 if today.weekday() == 6 else 7


def reconciliar_historico(
    output_dir: Path,
    empresa: str,
    *,
    enable_firebase: bool = False,
    firebase_store=None,
    today: datetime.date | None = None,
    max_attempts: int = 10,
    collect=None,
    close_month=None,
    sleep=None,
) -> dict:
    from .historico import executar_coleta_historico_dia, varrer_mes

    collect = collect or executar_coleta_historico_dia
    close_month = close_month or varrer_mes
    sleep = sleep or time.sleep
    today = today or datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    root = output_dir / "rotalog" / "sync" / company_key(empresa) / "historico"
    runs_dir = root / "reconciliacoes"
    window = janela_do_dia(today)
    path = runs_dir / f"{today.isoformat()}.json"
    # Uma única rotina escolhe a maior janela. O lock abrange todos os ciclos
    # agendados dessa empresa e é liberado pelo sistema se o processo morrer.
    with exclusive_file_lock(root / "reconciliacao.lock", timeout_seconds=60):
        runs_dir.mkdir(parents=True, exist_ok=True)
        state = load_json(path, {})
        if not state:
            state = {
                "schemaVersion": 1, "runDate": today.isoformat(), "windowDays": window,
                "pendingDays": [(today - datetime.timedelta(days=i)).isoformat()
                                for i in range(window, 0, -1)],
                "attemptsByDay": {}, "errorsByDay": {},
                "monthlyPending": today.day == 10,
                "monthlyReference": (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m"),
            }
            write_json(path, state)
        runs = {path: state}
        for previous_path in runs_dir.glob("*.json"):
            if previous_path == path:
                continue
            previous = load_json(previous_path, {})
            if previous.get("pendingDays") or previous.get("monthlyPending"):
                runs[previous_path] = previous
        pending = sorted({day for run in runs.values() for day in run.get("pendingDays", [])})

        def persist():
            for run_path, run in runs.items():
                run["status"] = "pending" if run.get("pendingDays") or run.get("monthlyPending") else "complete"
                run["nextRetryDate"] = (today + datetime.timedelta(days=1)).isoformat() if run["status"] == "pending" else None
                write_json(run_path, run)

        for round_number in range(max(1, max_attempts)):
            if not pending:
                break
            if round_number:
                delay = min(60 * 2 ** (round_number - 1), 900)
                LOG.info("Repetindo %d dias pendentes após %d segundos", len(pending), delay)
                sleep(delay)
            failed = []
            for day in pending:
                for run in runs.values():
                    if day in run.get("pendingDays", []):
                        attempts = run.setdefault("attemptsByDay", {})
                        attempts[day] = int(attempts.get(day, 0)) + 1
                persist()
                try:
                    result = collect(
                        target_date=datetime.date.fromisoformat(day), output_dir=output_dir,
                        empresa=empresa, enable_firebase=enable_firebase,
                        firebase_store=firebase_store, atualizar_terminal=False,
                    )
                    if result.get("status") != "success":
                        raise RuntimeError(result.get("error") or "Coleta incompleta")
                except Exception as exc:
                    failed.append(day)
                    for run in runs.values():
                        if day in run.get("pendingDays", []):
                            run.setdefault("errorsByDay", {})[day] = str(exc)
                    LOG.warning("Falha na reconciliação de %s: %s", day, exc)
                else:
                    for run in runs.values():
                        if day in run.get("pendingDays", []):
                            run["pendingDays"].remove(day)
                            run.setdefault("errorsByDay", {}).pop(day, None)
                persist()
            pending = failed
        for run in runs.values():
            if run.get("monthlyPending") and not run.get("pendingDays"):
                year, month = map(int, run["monthlyReference"].split("-"))
                try:
                    result = close_month(
                        year, month, output_dir, empresa,
                        enable_firebase=enable_firebase, firebase_store=firebase_store,
                        coletar_diarios=False,
                    )
                    if result.get("status") != "success":
                        raise RuntimeError(result.get("erroUpload") or "Fechamento incompleto")
                    run["monthlyPending"] = False
                    run.pop("monthlyError", None)
                except Exception as exc:
                    run["monthlyError"] = str(exc)
        persist()
        upload_queue = None
        if enable_firebase and firebase_store and firebase_store.enabled:
            from .historico_sync import flush_historical_uploads

            upload_queue = flush_historical_uploads(root / "pending-uploads.json", firebase_store)
        return {
            "status": "pending" if any(run["status"] == "pending" for run in runs.values()) else "success",
            "windowDays": window, "pendingDays": pending,
            "monthlyPending": any(run.get("monthlyPending") for run in runs.values()),
            "stateFile": str(path),
            "uploadQueue": upload_queue,
        }
