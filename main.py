"""Executa a raspagem ROTALOG localmente com suporte a sincronização no Firebase Storage.

Os snapshots e históricos ficam no diretório informado em --output-dir (dados-local).
Com a flag --firebase, os arquivos e o snapshot consolidado (index.json.gz) são
enviados diretamente para o Firebase Storage, alimentando o Monitor de Turnos do DDS.
Use --once para um teste único; sem essa opção o processo permanece em ciclo contínuo.
"""

from __future__ import annotations

import argparse
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

from coletor.historico import (
    executar_coleta_historico_dia,
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
    compactar_equipe_para_index,
    load_json,
    merge_daily_document,
    normalize_team_key,
    queue_counts,
    write_json,
)

TZ = ZoneInfo(os.getenv("DDS_TIMEZONE", "America/Sao_Paulo"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
LOG = logging.getLogger("coletor-rotalog")


def _init_firebase_storage():
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

        bucket_name = os.getenv("DDS_BUCKET_NAME", "dds-treinamentos.firebasestorage.app")
        blob_name = os.getenv(
            "ROTALOG_GCS_CACHE_BLOB",
            "dados/chicoeletro/rotalog/equipes/current/index.json.gz",
        )
        store = RotalogGcsSnapshotStore(bucket_name, blob_name, client_factory=client_factory)
        team_repo = RotalogTeamFileRepository(store)
        exec_log = RotalogExecutionLog(store)
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
        self.enable_firebase = enable_firebase
        self.firebase_store = None
        self.team_repo = None
        self.exec_log = None

        self.last_monthly_sweep_day: str | None = None

        if self.enable_firebase:
            self.firebase_store, self.team_repo, self.exec_log = _init_firebase_storage()

    @property
    def firebase_enabled(self) -> bool:
        return bool(self.firebase_store and self.firebase_store.enabled)

    def run_once(self) -> dict:
        started_clock = time.perf_counter()
        started_at = datetime.now(TZ)

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

        previous = load_json(self.index_path, {}).get("equipes", {})
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

        try:
            teams = extrair_dados_tempo_real(snapshots_anteriores=previous)
            scraped_at = datetime.now(TZ)
            timestamp = scraped_at.isoformat()
            day = scraped_at.date().isoformat()
            updates = {}
            ignored = 0

            for team in teams:
                code = str(team.get("equipe_codigo") or "").strip().upper()
                if not code:
                    continue
                team_key = normalize_team_key(code)
                document = build_rotalog_document(
                    team, self.empresa, team_key, timestamp, queue_counts(team)
                )

                daily_path = self.output_dir / "rotalog" / "equipes" / "daily" / day / f"{team_key}.json.gz"
                merged_daily = merge_daily_document(load_json(daily_path, {}), document, day)

                old = previous.get(team_key)
                if old and not changed_fields(old, merged_daily) and old.get("date") == day:
                    ignored += 1
                    merged_daily["updatedAt"] = timestamp
                else:
                    updates[team_key] = merged_daily

                    # 1. Grava SEMPRE no disco local (fidelidade máxima da linha do tempo)
                    write_json(daily_path, merged_daily)

                    # 2. Envia ao Firebase Storage apenas em eventos-chave (conclusão de serviço ou fechamento de turno)
                    old_concluidos = (
                        old.get("ordensServico", {}).get("totalConcluidos")
                        if old and "totalConcluidos" in (old.get("ordensServico") or {})
                        else len(old.get("ordensServico", {}).get("historico", [])) if old else 0
                    )
                    curr_concluidos = len(merged_daily.get("ordensServico", {}).get("historico", []))
                    concluiu_servico = (old is None and curr_concluidos > 0) or (curr_concluidos > old_concluidos)

                    old_turno = old.get("jornada", {}).get("turno", {}).get("status") if old else None
                    curr_turno = merged_daily.get("jornada", {}).get("turno", {}).get("status")
                    fechou_turno = (curr_turno == "FECHADO" and old_turno != "FECHADO")

                    if self.firebase_enabled and self.team_repo and (concluiu_servico or fechou_turno):
                        try:
                            self.team_repo.save_daily(merged_daily, day)
                        except Exception as exc:
                            LOG.error("Erro ao sincronizar equipe %s no Storage: %s", team_key, exc)

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

            # Grava o índice consolidado (index.json.gz) no Firebase Storage
            firebase_uploaded = False
            if self.firebase_enabled and self.firebase_store:
                try:
                    self.firebase_store.save({
                        "schemaVersion": 2,
                        "company": self.empresa,
                        "updatedAtIso": timestamp,
                        "totalEquipes": len(index_equipes),
                        "equipes": index_equipes,
                        "snapshots": index_equipes,
                    })
                    firebase_uploaded = True
                except Exception as exc:
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
                        },
                    )
                except Exception as exc:
                    LOG.warning("Erro ao gravar log diário de execução no Storage: %s", exc)

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
            }
        except Exception as exc:
            result = {
                "status": "error",
                "startedAt": started_at.isoformat(),
                "finishedAt": datetime.now(TZ).isoformat(),
                "durationSeconds": round(time.perf_counter() - started_clock, 3),
                "error": str(exc),
                "firebaseUploaded": False,
            }
            LOG.exception("Falha na raspagem")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
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
        firebase_store, _, _ = _init_firebase_storage()

    try:
        dt_inicio = parse_target_date(target_date_str)
        dt_fim = parse_target_date(data_fim_str) if data_fim_str else dt_inicio
    except Exception as exc:
        LOG.error("Erro ao interpretar data para coleta do histórico: %s", exc)
        return 1

    if dt_inicio > dt_fim:
        LOG.error("Data inicial (%s) posterior à data final (%s)", dt_inicio, dt_fim)
        return 1

    LOG.info(
        "Iniciando coleta de histórico separada: %s até %s (Empresa: %s, Firebase: %s)",
        dt_inicio.isoformat(),
        dt_fim.isoformat(),
        empresa,
        enable_firebase,
    )

    current = dt_inicio
    total_sucesso = 0
    total_dias = (dt_fim - dt_inicio).days + 1

    while current <= dt_fim:
        try:
            res = executar_coleta_historico_dia(
                target_date=current,
                output_dir=out_path,
                empresa=empresa,
                enable_firebase=enable_firebase,
                firebase_store=firebase_store,
            )
            print(json.dumps(res, ensure_ascii=False), flush=True)
            if res.get("status") == "success":
                total_sucesso += 1
        except Exception as exc:
            LOG.exception("Falha ao coletar histórico da data %s: %s", current.isoformat(), exc)
        current += timedelta(days=1)

    LOG.info("Coleta de histórico finalizada. Dias processados com sucesso: %d/%d", total_sucesso, total_dias)
    return 0 if total_sucesso == total_dias else 1


def executar_fechamento_mes(
    mes_str: str | None = None,
    output_dir: Path | str = "dados-local",
    empresa: str = "ChicoEletro",
    enable_firebase: bool = False,
) -> int:
    """Função separada para varredura completa de um mês (fechamento/notas de cobrança).
    
    Atualiza as quilometragens homologadas de todas as equipes no mês e consolida resumo_quilometragem.json.gz.
    """
    out_path = Path(output_dir).resolve()
    firebase_store = None
    if enable_firebase:
        firebase_store, _, _ = _init_firebase_storage()

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
                output_dir=out_path,
                empresa=empresa,
                enable_firebase=enable_firebase,
                firebase_store=firebase_store,
            )
        else:
            res = varrer_mes_anterior(
                output_dir=out_path,
                empresa=empresa,
                enable_firebase=enable_firebase,
                firebase_store=firebase_store,
            )

        print(json.dumps(res, ensure_ascii=False), flush=True)
        return 0 if res.get("status") == "success" else 1
    except Exception as exc:
        LOG.exception("Erro durante a varredura mensal: %s", exc)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="dados-local", help="Diretório local para JSONs")
    parser.add_argument("--interval-seconds", type=int, default=120, help="Intervalo entre coletas (segundos)")
    parser.add_argument("--empresa", default=os.getenv("DDS_EMPRESA_PADRAO", "ChicoEletro"))
    parser.add_argument("--firebase", action="store_true", help="Ativa sincronização automática com Firebase Storage")
    parser.add_argument("--no-firebase", action="store_true", help="Força desativação do Firebase Storage (apenas local)")
    parser.add_argument("--once", action="store_true", help="Executa somente uma vez e finaliza")
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

    if args.interval_seconds < 30:
        parser.error("--interval-seconds deve ser no mínimo 30")

    runner = LocalRotalogRunner(Path(args.output_dir).resolve(), args.empresa, enable_firebase=enable_firebase)
    while True:
        result = runner.run_once()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if result["status"] == "success" else 1
        elapsed = float(result.get("durationSeconds", 0))
        time.sleep(max(1, args.interval_seconds - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())

