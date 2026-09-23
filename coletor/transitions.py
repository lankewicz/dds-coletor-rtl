"""Motor de detecção de mudanças (diff) e resumos humanizados de transições operacionais."""

from __future__ import annotations

import json
import typing

TRACKED_FIELDS = (
    "isOnline",
    "statusConexao",
    "identificadorEquipamento",
    "veiculo",
    "colaborador",
    "estadoConsolidado",
    "turno",
    "intervalo",
    "intervalos",
    "atividadeAtual",
    "ssExecutadas",
    "ssEmAndamento",
    "ssPendentes",
)


def _operational_value(value: typing.Any) -> typing.Any:
    if isinstance(value, dict):
        return {
            k: _operational_value(v)
            for k, v in value.items()
            if k not in {"eventIdx", "fonteProtocolo", "validacaoProtocolo", "observadoEm"}
        }
    if isinstance(value, list):
        return sorted(
            (_operational_value(v) for v in value),
            key=lambda v: json.dumps(v, sort_keys=True, default=str),
        )
    return value


def changed_fields(
    previous: dict[str, typing.Any] | None,
    current: dict[str, typing.Any],
) -> dict[str, dict[str, typing.Any]]:
    """Retorna o diff entre o documento anterior e o atual (compatível com Schema v2 e v1)."""
    if previous is None:
        return {"novo": {"anterior": None, "novo": True}}

    changes: dict[str, dict[str, typing.Any]] = {}

    # Se o documento atual utiliza Schema v2:
    if "ordensServico" in current or "jornada" in current:
        prev_conexao = previous.get("conexao") or {}
        curr_conexao = current.get("conexao") or {}
        for k in ("isOnline", "veiculo", "colaborador"):
            if prev_conexao.get(k) != curr_conexao.get(k):
                changes[f"conexao.{k}"] = {"anterior": prev_conexao.get(k), "novo": curr_conexao.get(k)}

        prev_jornada = previous.get("jornada") or {}
        curr_jornada = current.get("jornada") or {}
        if prev_jornada.get("emIntervalo") != curr_jornada.get("emIntervalo"):
            changes["jornada.emIntervalo"] = {
                "anterior": prev_jornada.get("emIntervalo"),
                "novo": curr_jornada.get("emIntervalo"),
            }

        prev_turno = prev_jornada.get("turno") or {}
        curr_turno = curr_jornada.get("turno") or {}
        for k in ("status", "inicio", "fim"):
            if prev_turno.get(k) != curr_turno.get(k):
                changes[f"jornada.turno.{k}"] = {"anterior": prev_turno.get(k), "novo": curr_turno.get(k)}

        if _operational_value(prev_jornada.get("intervalos")) != _operational_value(curr_jornada.get("intervalos")):
            changes["jornada.intervalos"] = {
                "anterior": prev_jornada.get("intervalos"),
                "novo": curr_jornada.get("intervalos"),
            }

        prev_os = previous.get("ordensServico") or {}
        curr_os = current.get("ordensServico") or {}
        if _operational_value(prev_os.get("atual")) != _operational_value(curr_os.get("atual")):
            changes["ordensServico.atual"] = {"anterior": prev_os.get("atual"), "novo": curr_os.get("atual")}

        prev_concluidos = prev_os.get("totalConcluidos")
        if prev_concluidos is None:
            prev_concluidos = len(prev_os.get("historico") or [])
        curr_concluidos = curr_os.get("totalConcluidos")
        if curr_concluidos is None:
            curr_concluidos = len(curr_os.get("historico") or [])

        if prev_concluidos != curr_concluidos:
            changes["ordensServico.historico"] = {"anterior": prev_concluidos, "novo": curr_concluidos}
        elif "historico" in prev_os and "historico" in curr_os:
            if _operational_value(prev_os["historico"]) != _operational_value(curr_os["historico"]):
                changes["ordensServico.historico"] = {
                    "anterior": prev_os["historico"],
                    "novo": curr_os["historico"],
                }

        return changes

    for field in TRACKED_FIELDS:
        old_value = previous.get(field)
        new_value = current.get(field)
        if _operational_value(old_value) != _operational_value(new_value):
            changes[field] = {"anterior": old_value, "novo": new_value}
    return changes


def _summarize_history_correction(
    previous: dict[str, typing.Any] | None,
    current: dict[str, typing.Any],
) -> str:
    """Identifica sinteticamente qual ajuste foi realizado nas OS do histórico."""
    if not previous:
        return "Correção de OS"
    prev_os = previous.get("ordensServico") or {}
    curr_os = current.get("ordensServico") or {}
    prev_history = prev_os.get("historico") or []
    curr_history = curr_os.get("historico") or []

    prev_map = {
        srv.get("serviceId", str(i)): srv
        for i, srv in enumerate(prev_history)
        if isinstance(srv, dict)
    }

    has_protocol = False
    has_time = False
    has_gps = False
    has_type = False
    has_queue = False

    for i, curr_srv in enumerate(curr_history):
        if not isinstance(curr_srv, dict):
            continue
        sid = curr_srv.get("serviceId", str(i))
        prev_srv = prev_map.get(sid)
        if not prev_srv and i < len(prev_history) and isinstance(prev_history[i], dict):
            prev_srv = prev_history[i]
        if not prev_srv:
            continue

        if prev_srv.get("protocolo") != curr_srv.get("protocolo") and curr_srv.get("protocolo"):
            has_protocol = True
        for t_field in ("inicioDeslocamento", "inicioExecucao", "fimExecucao", "retorno"):
            if prev_srv.get(t_field) != curr_srv.get(t_field):
                has_time = True
        if (
            prev_srv.get("latitude") != curr_srv.get("latitude")
            or prev_srv.get("longitude") != curr_srv.get("longitude")
        ):
            if curr_srv.get("latitude") is not None:
                has_gps = True
        if prev_srv.get("tipo") != curr_srv.get("tipo") and curr_srv.get("tipo"):
            has_type = True
        if prev_srv.get("filaNaConclusao") != curr_srv.get("filaNaConclusao"):
            has_queue = True

    if has_protocol:
        return "OS (+Protocolo)"
    if has_time:
        return "OS (Horário)"
    if has_gps:
        return "OS (+GPS)"
    if has_type:
        return "OS (Tipo)"
    if has_queue:
        return "OS (Fila)"
    return "Correção de OS"


def summarize_team_transition(
    previous: dict[str, typing.Any] | None,
    current: dict[str, typing.Any],
    sync_reasons: list[str] | None = None,
) -> str:
    """Gera um resumo legível e específico da transição operacional da equipe."""
    reasons = set(sync_reasons or [])
    if "servico_concluido" in reasons:
        return "Execução --> Conclusão"
    if "correcao_servico_concluido" in reasons:
        return _summarize_history_correction(previous, current)
    if "turno_aberto" in reasons:
        return "Início de Turno"
    if "turno_fechado" in reasons:
        return "Fim de Turno"

    if previous is None:
        return "Início"

    prev_jornada = previous.get("jornada") or {}
    curr_jornada = current.get("jornada") or {}

    prev_turno = prev_jornada.get("turno") or {}
    curr_turno = curr_jornada.get("turno") or {}
    prev_turno_status = str(prev_turno.get("status") or "").upper()
    curr_turno_status = str(curr_turno.get("status") or "").upper()

    if curr_turno_status == "ABERTO" and prev_turno_status != "ABERTO":
        return "Início de Turno"
    if curr_turno_status == "FECHADO" and prev_turno_status == "ABERTO":
        return "Fim de Turno"

    # Intervalo
    prev_intervalo = bool(prev_jornada.get("emIntervalo"))
    curr_intervalo = bool(curr_jornada.get("emIntervalo"))
    if not prev_intervalo and curr_intervalo:
        return "Início de Intervalo"
    if prev_intervalo and not curr_intervalo:
        return "Fim de Intervalo"

    # Ordens de Serviço
    prev_os = previous.get("ordensServico") or {}
    curr_os = current.get("ordensServico") or {}

    prev_concluidos = prev_os.get("totalConcluidos")
    if prev_concluidos is None:
        prev_concluidos = len(prev_os.get("historico") or [])
    curr_concluidos = curr_os.get("totalConcluidos")
    if curr_concluidos is None:
        curr_concluidos = len(curr_os.get("historico") or [])

    if curr_concluidos > prev_concluidos:
        return "Execução --> Conclusão"

    prev_history = prev_os.get("historico") or []
    curr_history = curr_os.get("historico") or []
    if prev_history and curr_history and _operational_value(prev_history) != _operational_value(curr_history):
        return _summarize_history_correction(previous, current)

    status_map = {
        "DESLOCAMENTO": "Deslocamento",
        "EXECUCAO": "Execução",
        "CONCLUIDO": "Conclusão",
        "CONCLUSAO": "Conclusão",
        None: "Livre",
        "": "Livre",
    }
    prev_atual = prev_os.get("atual") or {}
    curr_atual = curr_os.get("atual") or {}

    prev_status_raw = None
    if isinstance(prev_atual, dict):
        prev_status_raw = prev_atual.get("statusAtual") or prev_atual.get("status")
    curr_status_raw = None
    if isinstance(curr_atual, dict):
        curr_status_raw = curr_atual.get("statusAtual") or curr_atual.get("status")

    if prev_status_raw != curr_status_raw:
        p_label = status_map.get(prev_status_raw, str(prev_status_raw).capitalize() if prev_status_raw else "Livre")
        c_label = status_map.get(curr_status_raw, str(curr_status_raw).capitalize() if curr_status_raw else "Livre")
        return f"{p_label} --> {c_label}"

    prev_prot = prev_atual.get("protocolo") if isinstance(prev_atual, dict) else None
    curr_prot = curr_atual.get("protocolo") if isinstance(curr_atual, dict) else None
    if prev_prot != curr_prot and curr_prot:
        c_label = status_map.get(curr_status_raw, "OS")
        return f"Nova OS ({c_label})"

    prev_conn = previous.get("conexao") or {}
    curr_conn = current.get("conexao") or {}
    if prev_conn.get("isOnline") != curr_conn.get("isOnline"):
        return "Online" if curr_conn.get("isOnline") else "Offline"

    if prev_conn.get("veiculo") != curr_conn.get("veiculo") and curr_conn.get("veiculo"):
        return f"Veículo ({curr_conn.get('veiculo')})"

    if prev_conn.get("colaborador") != curr_conn.get("colaborador") and curr_conn.get("colaborador"):
        return "Equipe Alterada"

    if curr_status_raw:
        c_label = status_map.get(curr_status_raw, str(curr_status_raw).capitalize())
        if isinstance(prev_atual, dict) and isinstance(curr_atual, dict):
            if (
                prev_atual.get("latitude") != curr_atual.get("latitude")
                or prev_atual.get("longitude") != curr_atual.get("longitude")
            ):
                return f"GPS ({c_label})"
        return f"Em {c_label}"

    if curr_turno_status == "ABERTO":
        return "Livre"

    return "Atualizado"
