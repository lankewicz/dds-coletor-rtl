"""Apresentação dos resultados das coletas no terminal."""

from __future__ import annotations

import sys
import typing


def formatar_numero_br(valor: float | int | None, casas: int = 2) -> str:
    if valor is None:
        return "0,00" if casas > 0 else "0"
    try:
        val_float = float(valor)
    except (ValueError, TypeError):
        return str(valor)
    if casas == 0:
        return f"{int(round(val_float)):,}".replace(",", ".")
    partes = f"{val_float:,.{casas}f}".split(".")
    return f"{partes[0].replace(',', '.')},{partes[1]}"


def atualizar_status_terminal(texto: str, final: bool = False) -> None:
    largura = 110
    linha_limpa = texto.replace("\n", " ").replace("\r", "")
    linha_formatada = (
        linha_limpa[:largura - 3] + "..."
        if len(linha_limpa) > largura
        else linha_limpa.ljust(largura)
    )
    suffix = "\n" if final else ""
    sys.stdout.write(f"\r{linha_formatada}{suffix}")
    sys.stdout.flush()


def formatar_resumo_mensal_terminal(res: dict[str, typing.Any]) -> str:
    if res.get("status") == "pending":
        pending = ", ".join(res.get("diasPendentes") or [])
        return (
            f"Fechamento mensal pendente. Dias ainda não coletados: {pending}. "
            f"Nova tentativa agendada para {res.get('nextRetryDate')}."
        )
    queue = res.get("uploadQueue") or {}
    linhas = [
        "", "=" * 80,
        "             FECHAMENTO MENSAL E DIÁRIO CONCLUÍDO COM SUCESSO",
        "=" * 80,
        f" Mês de Referência       : {res.get('month', '')}",
        f" Dias Processados        : {res.get('diasProcessados', 0)}/{res.get('diasTotal', 0)} dias",
        f" Quantidade de Serviços  : {formatar_numero_br(res.get('totalServicos', 0), 0)}",
        f" Total KM Informado      : {formatar_numero_br(res.get('totalKmInformado', 0.0), 2)} km",
        f" Total KM Autorizado     : {formatar_numero_br(res.get('totalKmAutorizadoFinal', 0.0), 2)} km",
        f" Total KM Recuperado     : {formatar_numero_br(res.get('totalKmRecuperadoParecer', 0.0), 2)} km",
        f" Arquivo Mensal JSON     : {res.get('localArquivoMensal', '-')}",
        f" Sincronizado Firebase   : {'Sim' if res.get('firebaseSynced') else 'Não'}",
        f" Envios Pendentes        : {queue.get('pending', 0)}",
        f" Último Erro de Envio    : {queue.get('lastError') or '-'}",
        f" Tempo de Execução       : {res.get('durationSeconds', 0.0)}s",
        "=" * 80, "",
    ]
    return "\n".join(linhas)


def formatar_resumo_diario_terminal(res: dict[str, typing.Any]) -> str:
    if res.get("status") == "error":
        return "\n".join([
            "", "=" * 80,
            "                         COLETA DIÁRIA FALHOU",
            "=" * 80,
            f" Data de Referência      : {res.get('date', '-')}",
            f" Erro                    : {res.get('error') or res.get('erroUpload') or 'erro não informado'}",
            " Os dados desse dia não foram marcados como coletados.",
            "=" * 80, "",
        ])
    queue = res.get("uploadQueue") or {}
    phases = res.get("phaseDurations") or {}
    linhas = [
        "", "=" * 80,
        "                    COLETA DIÁRIA CONCLUÍDA COM SUCESSO",
        "=" * 80,
        f" Data de Referência      : {res.get('date', '')}",
        f" Total de Equipes        : {formatar_numero_br(res.get('totalEquipes', 0), 0)}",
        f" Quantidade de Serviços  : {formatar_numero_br(res.get('totalServicos', 0), 0)}",
        f" Total KM Informado      : {formatar_numero_br(res.get('totalKmInformado', 0.0), 2)} km",
        f" Total KM Autorizado     : {formatar_numero_br(res.get('totalKmAutorizadoFinal', 0.0), 2)} km",
        f" Total KM Recuperado     : {formatar_numero_br(res.get('totalKmRecuperadoParecer', 0.0), 2)} km",
        f" Arquivo Diário JSON     : {res.get('localArquivoKm', '-')}",
        f" Sincronizado Firebase   : {'Sim' if res.get('firebaseSynced') else 'Não'}",
        f" Envios Pendentes        : {queue.get('pending', 0)}",
        f" Tentativas Pendentes    : {queue.get('totalAttempts', 0)}",
        f" Último Erro de Envio    : {queue.get('lastError') or '-'}",
        f" Tempo de Execução       : {res.get('durationSeconds', 0.0)}s",
        f" Coleta de Serviços      : {phases.get('eventosScrapeSeconds', 0.0)}s",
        f" Coleta de Equipes       : {phases.get('equipesScrapeSeconds', 0.0)}s",
        f" Consolidação            : {phases.get('consolidacaoSeconds', 0.0)}s",
        f" Cadastro de Equipes     : {phases.get('cadastroEquipesSeconds', 0.0)}s",
        f" Gravação dos Arquivos   : {phases.get('arquivosRelatorioSeconds', 0.0)}s",
        "=" * 80, "",
    ]
    return "\n".join(linhas)
