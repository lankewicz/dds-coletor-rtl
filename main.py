"""Executa a raspagem ROTALOG localmente com suporte a sincronização no Firebase Storage.

Os snapshots e históricos ficam no diretório informado em --output-dir (dados-local).
Com a flag --firebase, os arquivos e o snapshot consolidado (index.json.gz) são
enviados diretamente para o Firebase Storage, alimentando o Monitor de Turnos do DDS.
Use --once para um teste único; sem essa opção o processo permanece em ciclo contínuo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from coletor.logs import record_execution_log
from coletor.led import ProcessingLed, list_system_leds
from coletor.equipes import canonicalize_team_snapshots, normalize_team_key
from coletor.historico import (
    atualizar_status_terminal,
    executar_coleta_historico_dia,
    formatar_numero_br,
    formatar_resumo_diario_terminal,
    formatar_resumo_mensal_terminal,
    parse_target_date,
    varrer_mes,
    varrer_mes_anterior,
)
from coletor.parser import extrair_dados_tempo_real
from coletor.storage import (
    RotalogExecutionLog,
    RotalogGcsSnapshotStore,
    RotalogTeamFileRepository,
    build_rotalog_document,
    changed_fields,
    company_key,
    compactar_equipe_para_index,
    load_json,
    load_json_with_status,
    merge_daily_document,
    queue_counts,
    rotalog_gcs_paths,
    summarize_team_transition,
    write_json,
)

TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))
if not logging.root.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
LOG = logging.getLogger("coletor-rotalog")


def _init_firebase_storage(empresa: str):
    """Inicializa os repositórios GCS caso as credenciais estejam disponíveis."""
    try:
        from google.cloud import storage

        cred_candidates = [
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
            os.getenv("FIREBASE_CREDENTIALS"),
            str(ROOT / "serviceAccountKey.json"),
            str(ROOT / "firebase_config.json"),
            str(ROOT / "firebase_credentials.json"),
        ]
        cred_path = next((p for p in cred_candidates if p and os.path.isfile(p)), None)

        client_factory = None
        if cred_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path
            client_factory = lambda: storage.Client.from_service_account_json(cred_path)
            LOG.info("Storage inicializado via credencial de arquivo: %s", cred_path)
        else:
            LOG.info("Storage inicializado com credenciais padrão do ambiente (ADC)")

        paths = rotalog_gcs_paths(
            empresa,
            root_prefix=os.getenv("ROTALOG_GCS_ROOT_PREFIX"),
            index_blob=os.getenv("ROTALOG_GCS_CACHE_BLOB"),
        )
        bucket_name = os.getenv("DDS_BUCKET_NAME", "dds-treinamentos.firebasestorage.app")
        store = RotalogGcsSnapshotStore(
            bucket_name,
            paths["index"],
            root_prefix=paths["root"],
            client_factory=client_factory,
        )
        team_repo = RotalogTeamFileRepository(store, paths["teams"])
        exec_log = RotalogExecutionLog(store, paths["logs"])
        LOG.info("Destinos ROTALOG isolados para %s em %s", paths["companyKey"], paths["root"])
        return store, team_repo, exec_log
    except Exception as exc:
        LOG.warning("Não foi possível inicializar conexão com Firebase Storage: %s", exc)
        return None, None, None


class LocalRotalogRunner:
    def __init__(self, output_dir: Path, empresa: str, enable_firebase: bool = False):
        self.output_dir = output_dir
        self.empresa = empresa
        self.index_path = output_dir / "rotalog" / "equipes" / "current" / "index.json.gz"
        self.log_path = output_dir / "rotalog" / "logs" / "execucoes.jsonl"
        self.index_sync_path = self.index_path.with_name("firebase-sync.json")
        self.company_key = company_key(empresa)
        self.daily_sync_queue_path = (
            output_dir / "rotalog" / "sync" / self.company_key / "pending-daily.json.gz"
        )
        self.enable_firebase = enable_firebase
        self.firebase_store = None
        self.team_repo = None
        self.exec_log = None

        self.last_monthly_sweep_day: str | None = None
        self.processing_led = ProcessingLed()

        if self.enable_firebase:
            self.firebase_store, self.team_repo, self.exec_log = _init_firebase_storage(self.empresa)

    @property
    def firebase_enabled(self) -> bool:
        return bool(self.firebase_store and self.firebase_store.enabled)

    def _sync_index(self, equipes: dict, timestamp: str) -> str:
        """Confirma em disco somente o conteúdo enviado com sucesso ao destino atual."""
        if not self.firebase_enabled:
            return "disabled"

        def operational(value):
            if isinstance(value, dict):
                return {key: operational(item) for key, item in value.items()
                        if key not in {"observadoEm", "eventIdx"}}
            if isinstance(value, list):
                return [operational(item) for item in value]
            return value

        comparable = {
            key: operational({field: value for field, value in doc.items()
                              if field not in {"updatedAt", "updatedAtIso", "version"}})
            for key, doc in equipes.items()
        }
        content = {"schemaVersion": 2, "company": self.empresa, "equipes": comparable}
        digest = hashlib.sha256(json.dumps(
            content, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        confirmation = {
            "bucket": self.firebase_store.bucket_name,
            "blob": self.firebase_store.blob_name,
            "sha256": digest,
        }
        if load_json(self.index_sync_path, {}) == confirmation:
            return "unchanged"
        self.firebase_store.save({
            "schemaVersion": 2,
            "company": self.empresa,
            "updatedAtIso": timestamp,
            "totalEquipes": len(equipes),
            "equipes": equipes,
            "snapshots": equipes,
        })
        write_json(self.index_sync_path, confirmation)
        return "uploaded"

    @staticmethod
    def _quarantine_corrupt_file(path: Path) -> Path:
        target = path
        if not target.exists():
            if target.name.endswith(".gz"):
                fallback = target.with_name(target.name[:-3])
                if fallback.exists():
                    target = fallback
            else:
                fallback = target.with_name(target.name + ".gz")
                if fallback.exists():
                    target = fallback
        if not target.exists():
            return path
        quarantine = target.with_name(f"{target.name}.corrupt-{time.time_ns()}")
        target.replace(quarantine)
        return quarantine

    def _valid_daily_recovery(self, document: dict, day: str, team_key: str) -> bool:
        recovered_company = str(document.get("companyKey") or "").strip().lower()
        if not recovered_company and document.get("company"):
            recovered_company = company_key(document["company"])
        return bool(
            document
            and normalize_team_key(document.get("teamKey") or document.get("equipe")) == team_key
            and document.get("date") == day
            and (not recovered_company or recovered_company == self.company_key)
            and isinstance(document.get("jornada"), dict)
            and isinstance(document.get("ordensServico"), dict)
        )

    def _load_daily_with_recovery(self, path: Path, day: str, team_key: str) -> dict:
        local, status = load_json_with_status(path, {})
        if status != "corrupt":
            local_company = str(local.get("companyKey") or "").strip().lower() if local else ""
            if not local_company and local and local.get("company"):
                local_company = company_key(local["company"])
            if local_company and local_company != self.company_key:
                raise RuntimeError(
                    f"Arquivo diário pertence à empresa '{local_company}', mas o coletor está configurado para "
                    f"'{self.company_key}'; use outro --output-dir"
                )
            return local
        if not self.firebase_enabled or not self.team_repo:
            raise RuntimeError(
                f"Histórico local corrompido para {team_key} em {day}; Firebase indisponível para recuperação"
            )

        remote_path = self.team_repo.daily_path(day, team_key)
        remote = self.firebase_store.load_blob(remote_path)
        if not self._valid_daily_recovery(remote, day, team_key):
            raise RuntimeError(
                f"Histórico local corrompido para {team_key} em {day}; cópia válida não encontrada no Firebase"
            )

        quarantine = self._quarantine_corrupt_file(path)
        write_json(path, remote)
        LOG.warning(
            "Histórico de %s recuperado do Firebase; arquivo corrompido preservado em %s",
            team_key,
            quarantine,
        )
        return remote

    @staticmethod
    def _daily_sync_reasons(previous: dict, current: dict) -> list[str]:
        reasons = []
        prev_turno = ((previous.get("jornada") or {}).get("turno") or {})
        curr_turno = ((current.get("jornada") or {}).get("turno") or {})
        prev_status = str(prev_turno.get("status") or "").upper()
        curr_status = str(curr_turno.get("status") or "").upper()
        if curr_status == "ABERTO" and prev_status != "ABERTO":
            reasons.append("turno_aberto")
        if curr_status == "FECHADO" and prev_status != "FECHADO":
            reasons.append("turno_fechado")

        prev_history = (previous.get("ordensServico") or {}).get("historico") or []
        curr_history = (current.get("ordensServico") or {}).get("historico") or []
        if len(curr_history) > len(prev_history):
            reasons.append("servico_concluido")
        return reasons

    def _load_daily_sync_queue(self) -> dict:
        payload, status = load_json_with_status(self.daily_sync_queue_path, {})
        if status == "corrupt":
            quarantine = self._quarantine_corrupt_file(self.daily_sync_queue_path)
            LOG.warning(
                "Fila de sincronização corrompida em %s; arquivo movido para quarentena %s e fila reinicializada",
                self.daily_sync_queue_path,
                quarantine,
            )
            return {"schemaVersion": 1, "items": {}}
        items = payload.get("items", {}) if isinstance(payload, dict) else {}
        if not isinstance(items, dict):
            quarantine = self._quarantine_corrupt_file(self.daily_sync_queue_path)
            LOG.warning(
                "Fila de sincronização inválida em %s; arquivo movido para quarentena %s e fila reinicializada",
                self.daily_sync_queue_path,
                quarantine,
            )
            return {"schemaVersion": 1, "items": {}}
        return {"schemaVersion": 1, "items": items}

    def _save_daily_sync_queue(self, queue: dict) -> None:
        queue["schemaVersion"] = 1
        queue["updatedAt"] = datetime.now(TZ).isoformat()
        write_json(self.daily_sync_queue_path, queue)

    def _enqueue_daily_sync(self, day: str, team_key: str, reasons: list[str]) -> None:
        if not self.enable_firebase or not reasons:
            return
        queue = self._load_daily_sync_queue()
        item_key = f"{day}/{team_key}"
        existing = queue["items"].get(item_key, {})
        queue["items"][item_key] = {
            "day": day,
            "teamKey": team_key,
            "reasons": sorted(set((existing.get("reasons") or []) + reasons)),
            "queuedAt": existing.get("queuedAt") or datetime.now(TZ).isoformat(),
            "attempts": int(existing.get("attempts") or 0),
            "lastAttemptAt": existing.get("lastAttemptAt"),
            "lastError": existing.get("lastError"),
        }
        # A pendência deve existir em disco antes de qualquer tentativa de upload.
        self._save_daily_sync_queue(queue)

    def _flush_daily_sync_queue(self) -> dict[str, int]:
        queue = self._load_daily_sync_queue()
        stats = {"uploaded": 0, "failed": 0, "pending": len(queue["items"])}
        if not queue["items"] or not self.firebase_enabled or not self.team_repo:
            return stats

        for item_key, item in list(queue["items"].items()):
            day = str(item.get("day") or "")
            team_key = normalize_team_key(item.get("teamKey") or "")
            attempted_at = datetime.now(TZ).isoformat()
            try:
                if not day or not team_key:
                    raise ValueError("Pendência sem data ou equipe válida")
                daily_path = self.output_dir / "rotalog" / "equipes" / "daily" / day / f"{team_key}.json.gz"
                document = self._load_daily_with_recovery(daily_path, day, team_key)
                if not self._valid_daily_recovery(document, day, team_key):
                    raise RuntimeError("Arquivo diário local ausente ou inválido")
                self.team_repo.save_daily(document, day)
            except Exception as exc:
                item["attempts"] = int(item.get("attempts") or 0) + 1
                item["lastAttemptAt"] = attempted_at
                item["lastError"] = str(exc)
                queue["items"][item_key] = item
                stats["failed"] += 1
                self._save_daily_sync_queue(queue)
                LOG.error("Pendência diária %s mantida após falha de sincronização: %s", item_key, exc)
            else:
                del queue["items"][item_key]
                stats["uploaded"] += 1
                self._save_daily_sync_queue(queue)
                LOG.info("Histórico diário sincronizado e removido da fila: %s", item_key)

        stats["pending"] = len(queue["items"])
        return stats

    def _local_failure_result(self, started_at: datetime, started_clock: float, message: str) -> dict:
        daily_sync = {"uploaded": 0, "failed": 0, "pending": 0}
        try:
            daily_sync = self._flush_daily_sync_queue()
        except Exception as sync_exc:
            LOG.error("Não foi possível processar a fila diária: %s", sync_exc)
        result = {
            "status": "error",
            "startedAt": started_at.isoformat(),
            "finishedAt": datetime.now(TZ).isoformat(),
            "durationSeconds": round(time.perf_counter() - started_clock, 3),
            "error": message,
            "firebaseUploaded": False,
            "firebaseSyncStatus": "pending" if self.firebase_enabled else "disabled",
            "dailySyncPending": daily_sync["pending"],
            "dailySyncUploaded": daily_sync["uploaded"],
            "dailySyncFailed": daily_sync["failed"],
            "bytesUploaded": 0,
            "bytesDownloaded": 0,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        return result

    def run_once(self) -> dict:
        """Executa um ciclo mantendo o LED vermelho até a última gravação local."""
        self.processing_led.processing()
        try:
            return self._run_once()
        finally:
            self.processing_led.idle()

    def _run_once(self) -> dict:
        started_clock = time.perf_counter()
        started_at = datetime.now(TZ)
        daily_sync = {"uploaded": 0, "failed": 0, "pending": 0}
        if self.firebase_store and hasattr(self.firebase_store, "reset_cycle_bytes"):
            self.firebase_store.reset_cycle_bytes()

        # Automação: No 1º dia de cada mês, executa a varredura do mês anterior para fechar quilometragens
        today_iso = started_at.date().isoformat()
        if started_at.day == 1 and self.last_monthly_sweep_day != today_iso:
            try:
                LOG.info("Dia 1º detectado: iniciando varredura do mês anterior para fechamento de faturamento...")
                res_mes = varrer_mes_anterior(
                    output_dir=self.output_dir,
                    empresa=self.empresa,
                    enable_firebase=self.firebase_enabled,
                    firebase_store=self.firebase_store,
                )
                LOG.info("Varredura mensal concluída com sucesso: %s", res_mes.get("month"))
                self.last_monthly_sweep_day = today_iso
            except Exception as exc:
                LOG.error("Erro na varredura mensal automática: %s", exc)

        local_index, index_status = load_json_with_status(self.index_path, {})
        local_company = str(local_index.get("company") or "").strip() if local_index else ""
        if index_status == "ok" and local_company and company_key(local_company) != self.company_key:
            message = (
                f"O --output-dir contém o índice da empresa '{company_key(local_company)}', mas o coletor "
                f"está configurado para '{self.company_key}'; use um diretório local separado"
            )
            LOG.error(message)
            return self._local_failure_result(started_at, started_clock, message)
        if index_status == "corrupt":
            if not self.firebase_enabled:
                message = "Índice local corrompido; Firebase indisponível para recuperação"
                LOG.error(message)
                return self._local_failure_result(started_at, started_clock, message)
            remote_payload = self.firebase_store.load()
            remote_equipes = remote_payload.get("equipes") if isinstance(remote_payload, dict) else None
            if not isinstance(remote_equipes, dict) or not remote_equipes:
                message = "Índice local corrompido; cópia válida não encontrada no Firebase"
                LOG.error(message)
                return self._local_failure_result(started_at, started_clock, message)
            quarantine = self._quarantine_corrupt_file(self.index_path)
            local_index = {
                "schemaVersion": 2,
                "company": self.empresa,
                "lastCollectedAt": remote_payload.get("updatedAtIso"),
                "totalEquipes": len(remote_equipes),
                "equipes": remote_equipes,
            }
            write_json(self.index_path, local_index)
            LOG.warning("Índice local recuperado do Firebase; arquivo corrompido preservado em %s", quarantine)

        previous = local_index.get("equipes", {})
        if not isinstance(previous, dict):
            previous = {}

        # Hidrata cache inicial da nuvem caso local esteja vazio
        if not previous and self.firebase_enabled:
            try:
                remote_payload = self.firebase_store.load()
                remote_snapshots = (
                    remote_payload.get("equipes")
                    or remote_payload.get("snapshots")
                    if isinstance(remote_payload, dict)
                    else {}
                )
                if isinstance(remote_snapshots, dict) and remote_snapshots:
                    previous = remote_snapshots
                    write_json(self.index_path, {
                        "schemaVersion": 2,
                        "company": self.empresa,
                        "lastCollectedAt": remote_payload.get("updatedAtIso") or datetime.now(TZ).isoformat(),
                        "totalEquipes": len(previous),
                        "equipes": previous,
                    })
                    LOG.info("Cache local inicial hidratado com %d equipes do Storage", len(previous))
            except Exception as exc:
                LOG.warning("Não foi possível pré-carregar cache inicial do Storage: %s", exc)

        previous = canonicalize_team_snapshots(previous)

        try:
            teams = extrair_dados_tempo_real(snapshots_anteriores=previous)
            scraped_at = datetime.now(TZ)
            timestamp = scraped_at.isoformat()
            day = scraped_at.date().isoformat()
            updates = {}
            ignored = 0
            local_events = []
            cloud_events = []

            for team in teams:
                team_key = normalize_team_key(team.get("equipe_codigo"))
                if not team_key:
                    continue
                document = build_rotalog_document(
                    team, self.empresa, team_key, timestamp, queue_counts(team)
                )

                daily_path = self.output_dir / "rotalog" / "equipes" / "daily" / day / f"{team_key}.json.gz"
                previous_daily = self._load_daily_with_recovery(daily_path, day, team_key)
                merged_daily = merge_daily_document(previous_daily, document, day)

                changes = changed_fields(previous_daily, merged_daily) if previous_daily else {"novo": {}}
                if previous_daily and not changes and previous_daily.get("date") == day:
                    ignored += 1
                    merged_daily["updatedAt"] = timestamp
                else:
                    updates[team_key] = merged_daily

                    # 1. Grava SEMPRE no disco local (fidelidade máxima da linha do tempo)
                    write_json(daily_path, merged_daily)

                    # 2. Persiste o evento antes do upload. Intervalos permanecem na torre.
                    sync_reasons = self._daily_sync_reasons(previous_daily, merged_daily)
                    self._enqueue_daily_sync(
                        day,
                        team_key,
                        sync_reasons,
                    )

                    transition = summarize_team_transition(
                        previous_daily,
                        merged_daily,
                        sync_reasons=sync_reasons,
                    )
                    event_item = {"team": team_key, "action": transition}
                    if sync_reasons:
                        cloud_events.append(event_item)
                    else:
                        local_events.append(event_item)

                previous[team_key] = merged_daily

            index_equipes = {
                team_k: compactar_equipe_para_index(doc)
                for team_k, doc in previous.items()
            }

            # Grava o índice consolidado local (formato tempo real / torre de controle)
            write_json(self.index_path, {
                "schemaVersion": 2,
                "company": self.empresa,
                "lastCollectedAt": timestamp,
                "totalEquipes": len(index_equipes),
                "equipes": index_equipes,
            })

            # Envia pendências antigas e novas usando sempre a versão local mais recente.
            daily_sync = self._flush_daily_sync_queue()

            # Grava o índice consolidado (index.json.gz) no Firebase Storage
            firebase_uploaded = False
            firebase_sync_status = "disabled"
            if self.firebase_enabled and self.firebase_store:
                try:
                    firebase_sync_status = self._sync_index(index_equipes, timestamp)
                    firebase_uploaded = firebase_sync_status == "uploaded"
                except Exception as exc:
                    firebase_sync_status = "pending"
                    LOG.error("Erro ao salvar snapshot consolidado no Firebase Storage: %s", exc)

            finished_at = datetime.now(TZ)
            duration_total = round(time.perf_counter() - started_clock, 3)

            # Log diário de auditoria no Storage
            if self.firebase_enabled and self.exec_log:
                try:
                    self.exec_log.record(
                        status="success",
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_seconds=duration_total,
                        details={
                            "totalTeams": len(teams),
                            "updatedTeams": len(updates),
                            "ignoredTeams": ignored,
                            "scrapeDurationSeconds": round((scraped_at - started_at).total_seconds(), 3),
                            "dailySyncPending": daily_sync["pending"],
                            "dailySyncUploaded": daily_sync["uploaded"],
                            "dailySyncFailed": daily_sync["failed"],
                        },
                    )
                except Exception as exc:
                    LOG.warning("Erro ao gravar log diário de execução no Storage: %s", exc)

            bytes_uploaded, bytes_downloaded = 0, 0
            if self.firebase_store and hasattr(self.firebase_store, "reset_cycle_bytes"):
                res = self.firebase_store.reset_cycle_bytes()
                if isinstance(res, (tuple, list)) and len(res) == 2:
                    bytes_uploaded, bytes_downloaded = int(res[0]), int(res[1])

            result = {
                "status": "success",
                "startedAt": started_at.isoformat(),
                "finishedAt": finished_at.isoformat(),
                "durationSeconds": duration_total,
                "scrapeDurationSeconds": round((scraped_at - started_at).total_seconds(), 3),
                "totalTeams": len(teams),
                "updatedTeams": len(updates),
                "ignoredTeams": ignored,
                "firebaseUploaded": firebase_uploaded,
                "firebaseSyncStatus": firebase_sync_status,
                "dailySyncPending": daily_sync["pending"],
                "dailySyncUploaded": daily_sync["uploaded"],
                "dailySyncFailed": daily_sync["failed"],
                "bytesUploaded": bytes_uploaded,
                "bytesDownloaded": bytes_downloaded,
                "events": {
                    "local": local_events,
                    "cloud": cloud_events,
                },
            }
        except Exception as exc:
            try:
                daily_sync = self._flush_daily_sync_queue()
            except Exception as sync_exc:
                LOG.error("Não foi possível processar a fila diária: %s", sync_exc)
            result = {
                "status": "error",
                "startedAt": started_at.isoformat(),
                "finishedAt": datetime.now(TZ).isoformat(),
                "durationSeconds": round(time.perf_counter() - started_clock, 3),
                "error": str(exc),
                "firebaseUploaded": False,
                "firebaseSyncStatus": "pending" if self.firebase_enabled else "disabled",
                "dailySyncPending": daily_sync["pending"],
                "dailySyncUploaded": daily_sync["uploaded"],
                "dailySyncFailed": daily_sync["failed"],
                "bytesUploaded": 0,
                "bytesDownloaded": 0,
                "events": {
                    "local": [],
                    "cloud": [],
                },
            }
            LOG.exception("Falha no ciclo de coleta")

        record_execution_log(self.log_path, result)
        return result


def executar_historico(
    target_date_str: str | None = None,
    data_fim_str: str | None = None,
    output_dir: Path | str = "dados-local",
    empresa: str = "ChicoEletro",
    enable_firebase: bool = False,
) -> int:
    """Função separada para raspagem e arquivamento do histórico do ROTALOG.

    Pode ser executada sob demanda via chave CLI (--historico) ou importada por outros módulos.
    """
    out_path = Path(output_dir).resolve()
    firebase_store = None
    if enable_firebase:
        firebase_store, _, _ = _init_firebase_storage(empresa)

    try:
        dt_inicio = parse_target_date(target_date_str)
        dt_fim = parse_target_date(data_fim_str) if data_fim_str else dt_inicio
    except Exception as exc:
        LOG.error("Erro ao interpretar data para coleta do histórico: %s", exc)
        return 1

    if dt_inicio > dt_fim:
        LOG.error("Data inicial (%s) posterior à data final (%s)", dt_inicio, dt_fim)
        return 1

    # Silencia mensagens rotineiras de logging na tela para não quebrar a linha de progresso
    logging.getLogger("coletor-historico").setLevel(logging.WARNING)
    logging.getLogger("coletor-rotalog").setLevel(logging.WARNING)

    current = dt_inicio
    total_sucesso = 0
    total_dias = (dt_fim - dt_inicio).days + 1
    total_servicos_acum = 0
    total_km_inf_acum = 0.0
    total_km_aut_acum = 0.0
    ultimo_res: dict[str, typing.Any] = {}

    while current <= dt_fim:
        dia_br = current.strftime("%d/%m/%Y")
        try:
            res = executar_coleta_historico_dia(
                target_date=current,
                output_dir=out_path,
                empresa=empresa,
                enable_firebase=enable_firebase,
                firebase_store=firebase_store,
                atualizar_terminal=(total_dias == 1),
            )
            ultimo_res = res
            if res.get("status") == "success":
                total_sucesso += 1
                total_servicos_acum += res.get("totalServicos", 0)
                total_km_inf_acum += res.get("totalKmInformado", 0.0)
                total_km_aut_acum += res.get("totalKmAutorizadoFinal", 0.0)

            if total_dias > 1:
                atualizar_status_terminal(
                    f"[{dia_br}] Dia {total_sucesso}/{total_dias} | "
                    f"Serviços: {formatar_numero_br(total_servicos_acum, 0)} | "
                    f"KM Inf: {formatar_numero_br(total_km_inf_acum, 2)} | "
                    f"KM Aut: {formatar_numero_br(total_km_aut_acum, 2)}"
                )
        except Exception as exc:
            LOG.error("Falha ao coletar histórico da data %s: %s", current.isoformat(), exc)
        current += timedelta(days=1)

    if total_dias > 1:
        atualizar_status_terminal("Coleta de histórico concluída.", final=True)
        res_consolidado = {
            "date": f"{dt_inicio.isoformat()} a {dt_fim.isoformat()}",
            "durationSeconds": "-",
            "totalEquipes": ultimo_res.get("totalEquipes", 0),
            "totalServicos": total_servicos_acum,
            "totalKmInformado": round(total_km_inf_acum, 2),
            "totalKmAutorizadoFinal": round(total_km_aut_acum, 2),
            "totalKmRecuperadoParecer": 0.0,
            "localArquivoKm": f"dados-local/rotalog/quilometragem/diario/ ({total_sucesso} arquivos)",
            "localRelatorioHtml": f"dados-local/rotalog/quilometragem/diario/ ({total_sucesso} relatórios)",
            "firebaseSynced": enable_firebase,
        }
        print(formatar_resumo_diario_terminal(res_consolidado), flush=True)
    else:
        atualizar_status_terminal("", final=True)
        print(formatar_resumo_diario_terminal(ultimo_res), flush=True)

    return 0 if total_sucesso == total_dias else 1


def executar_fechamento_mes(
    mes_str: str | None = None,
    output_dir: Path | str = "dados-local",
    empresa: str = "ChicoEletro",
    enable_firebase: bool = False,
    progresso=None,
) -> int:
    """Função separada para varredura completa de um mês (fechamento/notas de cobrança).
    
    Coleta dados diários e mensais, gerando JSONs e relatórios HTML diários e mensal.
    """
    out_path = Path(output_dir).resolve()
    firebase_store = None
    if enable_firebase:
        firebase_store, _, _ = _init_firebase_storage(empresa)

    # Silencia mensagens rotineiras de logging na tela para não quebrar a linha de progresso
    logging.getLogger("coletor-historico").setLevel(logging.WARNING)
    logging.getLogger("coletor-rotalog").setLevel(logging.WARNING)

    try:
        if mes_str and mes_str.lower() not in ("anterior", "last", "true"):
            if "/" in mes_str:
                parts = mes_str.strip().split("/")
                mes_num, ano_num = int(parts[0]), int(parts[1])
            elif "-" in mes_str:
                parts = mes_str.strip().split("-")
                ano_num, mes_num = int(parts[0]), int(parts[1])
            else:
                raise ValueError(f"Formato de mês inválido: '{mes_str}'. Use AAAA-MM ou MM/AAAA.")
            res = varrer_mes(
                ano=ano_num,
                mes=mes_num,
                progresso=progresso,
                output_dir=out_path,
                empresa=empresa,
                enable_firebase=enable_firebase,
                firebase_store=firebase_store,
                coletar_diarios=True,
            )
        else:
            res = varrer_mes_anterior(
                progresso=progresso,
                output_dir=out_path,
                empresa=empresa,
                enable_firebase=enable_firebase,
                firebase_store=firebase_store,
                coletar_diarios=True,
            )

        print(formatar_resumo_mensal_terminal(res), flush=True)
        return 0 if res.get("status") == "success" else 1
    except Exception as exc:
        LOG.exception("Erro durante a varredura mensal: %s", exc)
        return 1


def get_adaptive_interval_seconds(
    now: datetime.datetime | None = None,
    peak_interval: int = 180,
    offpeak_interval: int = 600,
    peak_start_hour: int = 7,
    peak_end_hour: int = 20,
) -> int:
    """Calcula o intervalo de coleta adaptativo baseado na janela horária operacional:
    - 07:00 às 20:00: 3 minutos (180s) [pico operacional de equipes em campo]
    - 20:00 às 07:00: 10 minutos (600s) [fora de pico / plantão noturno]
    """
    target = now or datetime.datetime.now(TZ)
    if peak_start_hour <= target.hour < peak_end_hour:
        return peak_interval
    return offpeak_interval


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="dados-local", help="Diretório local para JSONs")
    parser.add_argument("--interval-seconds", type=int, default=None, help="Intervalo fixo em segundos (desativa adaptação horária)")
    parser.add_argument("--peak-interval", type=int, default=180, help="Intervalo de pico em segundos (07:00 às 20:00, padrão: 180s)")
    parser.add_argument("--offpeak-interval", type=int, default=600, help="Intervalo noturno em segundos (20:00 às 07:00, padrão: 600s)")
    parser.add_argument("--empresa", default=os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro"))
    parser.add_argument("--firebase", action="store_true", help="Ativa sincronização automática com Firebase Storage")
    parser.add_argument("--no-firebase", action="store_true", help="Força desativação do Firebase Storage (apenas local)")
    parser.add_argument("--once", action="store_true", help="Executa somente uma vez e finaliza")
    parser.add_argument(
        "--list-leds",
        action="store_true",
        help="Lista LEDs disponíveis em /sys/class/leds e finaliza",
    )
    parser.add_argument(
        "--historico",
        nargs="?",
        const="ontem",
        default=None,
        help="Chave para rodar a coleta do histórico separadamente (ontem/D-1 por padrão, ou informe AAAA-MM-DD / DD/MM/AAAA)",
    )
    parser.add_argument(
        "--historico-fim",
        default=None,
        help="Data final caso queira raspar um intervalo de dias no histórico",
    )
    parser.add_argument(
        "--mes-anterior",
        action="store_true",
        help="Chave para rodar a varredura retroativa completa do mês anterior e consolidar faturamento/quilometragens",
    )
    parser.add_argument(
        "--mes",
        default=None,
        help="Chave para rodar a varredura de um mês específico (formato AAAA-MM ou MM/AAAA)",
    )
    args = parser.parse_args()

    if args.list_leds:
        print(json.dumps(list_system_leds(), ensure_ascii=False, indent=2))
        return 0

    if args.no_firebase:
        enable_firebase = False
    else:
        enable_firebase = args.firebase or os.getenv("ROTALOG_UPLOAD_FIREBASE", "false").strip().lower() in ("true", "1", "yes")

    # Chave para rodar a varredura do mês (fechamento de faturamento)
    if args.mes_anterior or args.mes:
        return executar_fechamento_mes(
            mes_str=args.mes or "anterior",
            output_dir=args.output_dir,
            empresa=args.empresa,
            enable_firebase=enable_firebase,
        )

    # Chave para rodar a coleta de histórico diário separadamente
    if args.historico is not None:
        return executar_historico(
            target_date_str=args.historico,
            data_fim_str=args.historico_fim,
            output_dir=args.output_dir,
            empresa=args.empresa,
            enable_firebase=enable_firebase,
        )

    if args.interval_seconds is not None and args.interval_seconds < 30:
        parser.error("--interval-seconds deve ser no mínimo 30")
    if args.peak_interval < 30 or args.offpeak_interval < 30:
        parser.error("--peak-interval e --offpeak-interval devem ser no mínimo 30")

    runner = LocalRotalogRunner(Path(args.output_dir).resolve(), args.empresa, enable_firebase=enable_firebase)
    while True:
        result = runner.run_once()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if result["status"] == "success" else 1
        elapsed = float(result.get("durationSeconds", 0))

        if args.interval_seconds is not None:
            cycle_interval = args.interval_seconds
        else:
            cycle_interval = get_adaptive_interval_seconds(
                peak_interval=args.peak_interval,
                offpeak_interval=args.offpeak_interval,
            )

        time.sleep(max(1, cycle_interval - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
