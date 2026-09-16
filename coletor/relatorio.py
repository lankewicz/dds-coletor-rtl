"""Relatórios diário e mensal locais, sem dependências adicionais."""

from html import escape
from pathlib import Path


def _formatar_numero(valor: float) -> str:
    return f"{valor:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def gerar_relatorio_diario(payload: dict, destino: Path) -> Path:
    """Renderiza a listagem de eventos/serviços diários por contrato e equipe em HTML imprimível."""
    empresa = escape(str(payload.get("empresa") or ""))
    data_ref = escape(str(payload.get("data") or ""))
    collected_at = escape(str(payload.get("collectedAt") or ""))
    contratos_monitorados = escape(", ".join(payload.get("contratos") or []))

    totais_contrato = payload.get("totaisPorContrato") or {}
    servicos = payload.get("servicos") or []
    if not servicos and payload.get("protocolos"):
        servicos = list(payload["protocolos"].values())

    # Agrupa serviços por contrato e equipe
    servicos_por_contrato: dict[str, dict[str, list[dict]]] = {}
    for s in servicos:
        c = str(s.get("contrato") or "OUTROS").strip()
        eq = str(s.get("equipe") or "SEM_EQUIPE").strip()
        servicos_por_contrato.setdefault(c, {}).setdefault(eq, []).append(s)

    cabecalho_tabela = (
        "<thead><tr>"
        "<th>Protocolo</th>"
        "<th>Equipe</th>"
        "<th>Início Desloc.</th>"
        "<th>Início Exec.</th>"
        "<th>Fim Exec.</th>"
        "<th>Retorno</th>"
        "<th>KM Informado</th>"
        "<th>KM Autorizado</th>"
        "<th>Diferença (km)</th>"
        "</tr></thead>"
    )

    secoes = []
    total_geral_km_inf = 0.0
    total_geral_km_aut = 0.0
    total_geral_servicos = 0

    for contrato, equipes_dict in sorted(servicos_por_contrato.items()):
        linhas_html = []
        sub_km_inf = 0.0
        sub_km_aut = 0.0
        sub_servicos = 0

        for equipe, lista_servicos in sorted(equipes_dict.items()):
            eq_km_inf = 0.0
            eq_km_aut = 0.0
            for s in lista_servicos:
                p = escape(str(s.get("protocolo") or "—"))
                eq_nome = escape(str(s.get("equipe") or "—"))
                ini_d = escape(str(s.get("inicioDeslocamento") or "—"))
                ini_e = escape(str(s.get("inicioExecucao") or "—"))
                fim_e = escape(str(s.get("fimExecucao") or "—"))
                ret = escape(str(s.get("retorno") or "—"))
                inf = float(s.get("kmInformado") or 0.0)
                aut = float(s.get("kmAutorizadoFinal") or 0.0)
                dif = round(inf - aut, 2)
                eq_km_inf += inf
                eq_km_aut += aut

                linhas_html.append(
                    f"<tr>"
                    f"<th>{p}</th>"
                    f"<td>{eq_nome}</td>"
                    f"<td>{ini_d}</td>"
                    f"<td>{ini_e}</td>"
                    f"<td>{fim_e}</td>"
                    f"<td>{ret}</td>"
                    f"<td>{_formatar_numero(inf)}</td>"
                    f"<td>{_formatar_numero(aut)}</td>"
                    f"<td>{_formatar_numero(dif)}</td>"
                    f"</tr>"
                )

            dif_eq = round(eq_km_inf - eq_km_aut, 2)
            linhas_html.append(
                f'<tr class="subtotal-equipe">'
                f'<th colspan="6">Subtotal Equipe {escape(equipe)} ({len(lista_servicos)} serviços)</th>'
                f"<td>{_formatar_numero(eq_km_inf)}</td>"
                f"<td>{_formatar_numero(eq_km_aut)}</td>"
                f"<td>{_formatar_numero(dif_eq)}</td>"
                f"</tr>"
            )

            sub_km_inf += eq_km_inf
            sub_km_aut += eq_km_aut
            sub_servicos += len(lista_servicos)

        sub_dif = round(sub_km_inf - sub_km_aut, 2)
        linhas_html.append(
            f'<tr class="total">'
            f'<th colspan="6">Subtotal Contrato {escape(contrato)} ({sub_servicos} serviços)</th>'
            f"<td>{_formatar_numero(sub_km_inf)}</td>"
            f"<td>{_formatar_numero(sub_km_aut)}</td>"
            f"<td>{_formatar_numero(sub_dif)}</td>"
            f"</tr>"
        )

        total_geral_km_inf += sub_km_inf
        total_geral_km_aut += sub_km_aut
        total_geral_servicos += sub_servicos

        secoes.append(
            f"<h2>Contrato {escape(contrato)}</h2>"
            f"<table>{cabecalho_tabela}<tbody>{''.join(linhas_html)}</tbody></table>"
        )

    dif_geral = round(total_geral_km_inf - total_geral_km_aut, 2)
    tabela_total_geral = (
        f"<h2>Total Geral Diário</h2>"
        f"<table>{cabecalho_tabela}<tbody>"
        f'<tr class="total">'
        f'<th colspan="6">Todas as equipes ({total_geral_servicos} serviços)</th>'
        f"<td>{_formatar_numero(total_geral_km_inf)}</td>"
        f"<td>{_formatar_numero(total_geral_km_aut)}</td>"
        f"<td>{_formatar_numero(dif_geral)}</td>"
        f"</tr>"
        f"</tbody></table>"
    )
    secoes.append(tabela_total_geral)

    aviso = "" if servicos else "<p>Nenhum evento/serviço encontrado para os contratos monitorados nesta data.</p>"

    html = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relatório Diário — {empresa} — {data_ref}</title>
<style>
body{{font:14px system-ui,sans-serif;color:#172a3a;max-width:1250px;margin:30px auto;padding:0 24px;background:#fafbfc}}
h1{{margin-bottom:6px;font-size:24px;color:#0b2545}}h2{{margin-top:28px;font-size:18px;color:#133c55}}
p{{line-height:1.5;margin:4px 0}}
.card-resumo{{display:flex;gap:16px;margin:20px 0;flex-wrap:wrap}}
.card{{background:#fff;border:1px solid #dce3e8;border-radius:6px;padding:12px 18px;min-width:180px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.card .label{{font-size:12px;color:#526471;text-transform:uppercase}}
.card .val{{font-size:20px;font-weight:700;color:#0b2545;margin-top:4px}}
table{{border-collapse:collapse;width:100%;margin:14px 0;background:#fff;border:1px solid #dce3e8;border-radius:6px;overflow:hidden}}
th,td{{padding:8px 12px;border-bottom:1px solid #eef2f5;text-align:right;font-size:13px}}
th:first-child,td:nth-child(2){{text-align:left}}
thead{{background:#eef3f7;color:#172a3a;font-weight:600}}
.subtotal-equipe{{background:#f8fafc;font-weight:600}}
.total{{background:#e2ebf3;font-weight:700}}
.nota{{color:#526471;font-size:12px}}
button{{padding:8px 16px;cursor:pointer;background:#0b2545;color:#fff;border:none;border-radius:4px;font-weight:600;margin-bottom:16px}}
button:hover{{background:#133c55}}
@media print{{button{{display:none}}body{{margin:0;padding:0;background:#fff;font-size:9pt}}tr{{break-inside:avoid}}thead{{display:table-header-group}}}}
</style></head>
<body>
<button onclick="window.print()">Imprimir / Salvar PDF</button>
<h1>Relatório Diário de Serviços e Quilometragem</h1>
<p>{empresa} · Data de Referência: <strong>{data_ref}</strong> · Coleta: {collected_at}</p>
<p class="nota">Fonte: Copel ROTALOG — Listagem de Eventos (detalhamento por protocolo de serviço). Contratos: {contratos_monitorados}.</p>

<div class="card-resumo">
  <div class="card"><div class="label">Total de Serviços</div><div class="val">{total_geral_servicos}</div></div>
  <div class="card"><div class="label">KM Informado</div><div class="val">{_formatar_numero(total_geral_km_inf)}</div></div>
  <div class="card"><div class="label">KM Autorizado Final</div><div class="val">{_formatar_numero(total_geral_km_aut)}</div></div>
  <div class="card"><div class="label">Diferença (km)</div><div class="val">{_formatar_numero(dif_geral)}</div></div>
</div>

{aviso}
{''.join(secoes)}
</body></html>"""

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(html, encoding="utf-8")
    return destino


def gerar_relatorio_mensal(payload: dict, destino: Path) -> Path:
    """Renderiza o fechamento mensal da tela Equipes em HTML imprimível."""
    resumo_oficial = payload.get("resumoEquipes") or payload
    grupos_oficiais = resumo_oficial.get("totaisPorContrato") or payload.get("totaisPorContrato") or {}

    def linha_oficial(nome, valores, subtotal=False):
        informado = float(valores.get("kmInformado") or 0.0)
        final = float(valores.get("kmAutorizadoFinal") or 0.0)
        percentual = _formatar_numero((informado - final) / informado * 100) + "%" if informado else "—"
        classe = ' class="total"' if subtotal else ""
        return (
            f"<tr{classe}>"
            f"<th>{escape(nome)}</th>"
            f"<td>{valores.get('servicosExecutados', 0)}</td>"
            f"<td>{_formatar_numero(informado)}</td>"
            f"<td>{_formatar_numero(float(valores.get('kmGlosadoCritico') or 0.0))}</td>"
            f"<td>{_formatar_numero(float(valores.get('kmGlosadoParecer') or 0.0))}</td>"
            f"<td>{_formatar_numero(float(valores.get('kmAutorizadoInicial') or 0.0))}</td>"
            f"<td>{_formatar_numero(float(valores.get('kmRecuperadoParecer') or 0.0))}</td>"
            f"<td>{_formatar_numero(final)}</td>"
            f"<td>{_formatar_numero(float(valores.get('kmAguardandoJustificativa') or 0.0))}</td>"
            f"<td>{_formatar_numero(float(valores.get('kmEmAnalise') or 0.0))}</td>"
            f"<td>{percentual}</td>"
            f"</tr>"
        )

    cabecalho_oficial = (
        "<thead><tr>"
        "<th>Equipe</th>"
        "<th>Serviços</th>"
        "<th>KM informada</th>"
        "<th>Glosada crítica</th>"
        "<th>Glosada por parecer</th>"
        "<th>Autorizada inicial</th>"
        "<th>Recuperada</th>"
        "<th>Autorizada final</th>"
        "<th>Aguardando justificativa</th>"
        "<th>Em análise</th>"
        "<th>Diferença</th>"
        "</tr></thead>"
    )

    secoes = []
    if grupos_oficiais:
        for contrato, grupo in sorted(grupos_oficiais.items()):
            equipes = grupo.get("equipes") or {}
            linhas = [linha_oficial(eq, val) for eq, val in sorted(equipes.items())]
            linhas.append(linha_oficial("Subtotal do contrato", grupo, True))
            secoes.append(
                f"<h2>Contrato {escape(contrato)}</h2>"
                f"<table>{cabecalho_oficial}<tbody>{''.join(linhas)}</tbody></table>"
            )

        totais_gerais = resumo_oficial.get("totais") or {
            "servicosExecutados": payload.get("totalServicos", 0),
            "kmInformado": payload.get("totalKmInformado", 0.0),
            "kmGlosadoCritico": 0.0,
            "kmGlosadoParecer": 0.0,
            "kmAutorizadoInicial": 0.0,
            "kmRecuperadoParecer": payload.get("totalKmRecuperadoParecer", 0.0),
            "kmAutorizadoFinal": payload.get("totalKmAutorizadoFinal", 0.0),
            "kmAguardandoJustificativa": 0.0,
            "kmEmAnalise": 0.0,
        }
        secoes.append(
            f"<h2>Total geral</h2>"
            f"<table>{cabecalho_oficial}<tbody>{linha_oficial('Todas as equipes', totais_gerais, True)}</tbody></table>"
        )

    empresa = escape(str(payload.get("empresa") or ""))
    mes = escape(str(payload.get("mes") or ""))
    periodo = payload.get("periodo") or {}
    periodo_str = f"{escape(str(periodo.get('inicio') or ''))} a {escape(str(periodo.get('fim') or ''))}"
    collected_at = escape(str(payload.get("collectedAt") or ""))
    contratos = escape(", ".join(payload.get("contratos") or []))

    aviso = "" if grupos_oficiais else "<p>Nenhum registro encontrado para os contratos monitorados neste mês.</p>"
    pagina = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fechamento Mensal — {empresa} — {mes}</title>
<style>
body{{font:14px system-ui,sans-serif;color:#172a3a;max-width:1250px;margin:30px auto;padding:0 24px;background:#fafbfc}}
h1{{margin-bottom:6px;font-size:24px;color:#0b2545}}h2{{margin-top:28px;font-size:18px;color:#133c55}}
p{{line-height:1.5;margin:4px 0}}
table{{border-collapse:collapse;width:100%;margin:14px 0;background:#fff;border:1px solid #dce3e8;border-radius:6px;overflow:hidden}}
th,td{{padding:8px 12px;border-bottom:1px solid #eef2f5;text-align:right;font-size:13px}}
th:first-child{{text-align:left}}
thead{{background:#eef3f7;color:#172a3a;font-weight:600}}
.total{{background:#e2ebf3;font-weight:700}}
.nota{{color:#526471;font-size:12px}}
button{{padding:8px 16px;cursor:pointer;background:#0b2545;color:#fff;border:none;border-radius:4px;font-weight:600;margin-bottom:16px}}
button:hover{{background:#133c55}}
@media print{{button{{display:none}}body{{margin:0;padding:0;background:#fff;font-size:9pt}}tr{{break-inside:avoid}}thead{{display:table-header-group}}}}
</style></head>
<body>
<button onclick="window.print()">Imprimir / Salvar PDF</button>
<h1>Relatório Mensal de Fechamento de Equipes</h1>
<p>{empresa} · Mês: <strong>{mes}</strong><br>Período: {periodo_str}<br>Coleta: {collected_at}</p>
<p class="nota">Fonte: Copel ROTALOG — Fechamento Oficial da tela Equipes (/paginas/equipes).
KM recuperada = “Recuperados por Parecer”. Contratos monitorados: {contratos}.</p>
{aviso}
{''.join(secoes)}
</body></html>"""

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(pagina, encoding="utf-8")
    return destino

