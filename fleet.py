"""Comanda a atualização remota dos Orange Pis por meio do Firebase Storage."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from coletor.fleet_control import FleetStore, normalize_commit, utc_now_iso


ROOT = Path(__file__).resolve().parent


def run_git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "falha desconhecida do Git"
        raise RuntimeError(message)
    return result.stdout.strip()


def ensure_clean_worktree() -> None:
    dirty = run_git("status", "--porcelain")
    if dirty:
        raise RuntimeError(
            "O repositório possui alterações não commitadas. Faça commit ou preserve as alterações antes do --update."
        )


def push_current_branch() -> str:
    ensure_clean_worktree()
    branch = run_git("branch", "--show-current")
    if not branch:
        raise RuntimeError("O Windows está em detached HEAD; selecione uma branch antes do --update")
    remote = os.getenv("FLEET_GIT_REMOTE", "origin")
    print(f"Enviando {branch} para {remote}...")
    run_git("push", remote, branch)
    return resolve_commit("HEAD", require_remote=True)


def resolve_commit(ref: str, *, require_remote: bool = True) -> str:
    requested = normalize_commit(ref) if ref.upper() != "HEAD" else ref
    run_git("fetch", os.getenv("FLEET_GIT_REMOTE", "origin"), "--prune")
    commit = run_git("rev-parse", f"{requested}^{{commit}}")
    normalize_commit(commit, allow_short=False)
    if require_remote:
        branches = run_git("branch", "-r", "--contains", commit)
        remote = os.getenv("FLEET_GIT_REMOTE", "origin") + "/"
        if remote not in branches:
            raise RuntimeError(f"O commit {commit[:12]} não está disponível em nenhuma branch remota")
    return commit


def target_for_nodes(nodes: list[dict], selected_node: str | None) -> str | list[str]:
    active_ids = [str(node.get("nodeId")) for node in nodes]
    if selected_node:
        if selected_node not in active_ids:
            raise RuntimeError(f"O equipamento {selected_node!r} não está ativo")
        return selected_node
    return active_ids


def publish_deploy(store: FleetStore, commit: str, nodes: list[dict], selected_node: str | None) -> dict:
    command_id = f"deploy-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{commit[:8]}-{uuid.uuid4().hex[:6]}"
    requested_at = utc_now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    command = {
        "schemaVersion": 1,
        "commandId": command_id,
        "action": "deploy",
        "commit": commit,
        "target": target_for_nodes(nodes, selected_node),
        "requestedAt": requested_at,
        "expiresAt": expires_at,
        "requestedBy": os.getenv("USERNAME") or os.getenv("USER") or "unknown",
    }
    store.save(store.paths.command(command_id), command)
    return command


def wait_for_results(store: FleetStore, command: dict, node_ids: list[str], timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    last_statuses: dict[str, str] = {}
    terminal = {"healthy", "already_active", "failed", "rolled_back"}
    while time.monotonic() < deadline:
        all_done = True
        success = True
        for node_id in node_ids:
            result = store.load(store.paths.result(command["commandId"], node_id))
            status = str(result.get("status") or "waiting")
            if last_statuses.get(node_id) != status:
                detail = str(result.get("detail") or "")
                print(f"{node_id}: {status}" + (f" — {detail}" if detail else ""))
                last_statuses[node_id] = status
            if status not in terminal:
                all_done = False
            if status in {"failed", "rolled_back"}:
                success = False
        if all_done:
            return success
        time.sleep(3)
    print("Tempo limite excedido aguardando os equipamentos.", file=sys.stderr)
    return False


def print_nodes(nodes: list[dict]) -> None:
    print(f"Orange Pis ativos: {len(nodes)}")
    for node in nodes:
        print(
            f"- {node.get('nodeId')}: {node.get('status', 'unknown')} "
            f"saude={str((node.get('health') or {}).get('overall') or 'desconhecida')} "
            f"commit={str(node.get('runningCommit') or 'desconhecido')[:12]} "
            f"heartbeat={node.get('heartbeatAgeSeconds')}s"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--update", action="store_true", help="Faz push da branch atual e implanta o HEAD enviado")
    group.add_argument("--version", metavar="HASH", help="Implanta um commit específico já disponível no Git remoto")
    group.add_argument("--status", action="store_true", help="Mostra os Orange Pis com heartbeat recente")
    parser.add_argument("--node", help="Limita a operação a um Orange Pi")
    parser.add_argument("--timeout", type=int, default=900, help="Tempo máximo de espera em segundos")
    parser.add_argument("--no-wait", action="store_true", help="Publica o comando sem aguardar o resultado")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = FleetStore.from_environment(ROOT)
        if args.status:
            nodes = store.active_nodes()
            print_nodes(nodes)
            return 0
        commit = push_current_branch() if args.update else resolve_commit(args.version)
        print(f"Versão selecionada: {commit}")
        nodes = store.active_nodes()
        print_nodes(nodes)
        if not nodes:
            raise RuntimeError("Nenhum Orange Pi com comunicação recente foi encontrado")
        command = publish_deploy(store, commit, nodes, args.node)
        targets = command["target"] if isinstance(command["target"], list) else [command["target"]]
        print(f"Comando publicado: {command['commandId']} para {', '.join(targets)}")
        if args.no_wait:
            return 0
        return 0 if wait_for_results(store, command, targets, args.timeout) else 1
    except (RuntimeError, ValueError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
