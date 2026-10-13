# Contrato da torre no Orange — versão 1

A saída é validada e a composição local dos cards/eventos está implementada.
A validação com dados reais, instalação no Orange e troca do frontend continuam pendentes. A API rejeita documentos parciais com HTTP 503.

## Saída

`dados-local/torre/current.json` contém:

- `schemaVersion`: inteiro 1; `company`: empresa exata da credencial.
- `collectedAt`: horário da última coleta Rotalog bem-sucedida.
- `composedAt`: horário da composição; ambos ISO 8601 com fuso.
- `stale`: booleano, verdadeiro quando qualquer fonte está desatualizada.
- `sources`: objetos `rotalog`, `cadastro` e `dds`, cada um com `updatedAt`
  e `stale`. Uma fonte ausente não equivale a uma fonte vazia válida.
- `items`: cards com identidade `teamKey` única e canônica, `equipe`, `setor`,
  `estado`, `turnStatus`, `active`, `updatedAt`, `participantes`, `operacional`,
  `ddsHistory`, `ddsDays`, `ddsTimes` e `ddsToday` (`ok` ou `neutral`).
  Preservar também os demais campos consumidos pelo frontend atual.
- `events`: lista com `id`, `teamKey`, `type` e `occurredAt` com fuso.
  Identidade e horário persistem entre coletas e reinícios. Primeira observação
  fechada não representa fechamento recente. Não usar composedAt como evento.

O validador verifica a estrutura mínima; não certifica a equivalência das regras
operacionais com o DDS. Essa equivalência precisa de testes da composição.
A função `publish_projection` valida antes de usar a gravação atômica existente.
Uma coleta ou composição que falha preserva o último arquivo válido.

## Entradas e regras da composição

1. Rotalog: índice e diários locais existentes, incluindo histórico de serviços.
2. Cadastro: exportação versionada por empresa do cadastro utilizado pelo DDS,
   incluindo aliases, setor, participantes, motorista, veículo e tipo da equipe.
   O TeamRegistry do coletor complementa identidades; não substitui esse cadastro.
3. DDS: sincronizar as projeções compactas já existentes no Storage
   (`dados/<empresa>/dds/controle/daily/AAAA-MM-DD.json.gz`) e registrar a última
   sincronização bem-sucedida. Confirmar o prefixo configurado do DDS antes de usar.
   A coleção disponível, inclusive vazia, deve ser distinguida de falha de leitura.

Cadastros e projeções devem ser sincronizados por ciclo/intervalo controlado,
com cache local e isolamento por empresa; nunca por acesso do navegador.
O exportador está em `scripts/export_torre_catalog.py` e o sincronizador em
`coletor/torre_inputs.py`. A exportação usa `list_teams_map(active=None)` e os
aliases da leitura oficial do DDS, preservando equipes inativas e metadados.
O cadastro global atual foi validado apenas para ChicoEletro; o exportador
rejeita outras empresas. O sincronizador isola cada empresa e valida os payloads.

O módulo `coletor/torre_cards.py` porta as regras do DDS. A validação real deve comparar as regras de atividade em sete dias,
fechamento das equipes DDS às 18h, turnos atravessando meia-noite, aliases,
contadores e históricos do DDS. Validar comparando os cards das duas fontes.
Recomposição também precisa ocorrer em mudanças de data e às 18h, mesmo que
não haja mudança operacional Rotalog.

## Disponibilidade

O consumidor deve calcular também a idade dos timestamps: se o coletor parar,
`stale` gravado na última saída não muda sozinho. Não interpretar `stale: false`
como prova de atualidade sem comparar o horário da fonte com o relógio atual.
Definir o limite conforme a frequência real da coleta antes da integração.

## Compatibilidade e instalação

A API antiga, privada, e a API pública autenticada são contratos separados.
Os índices e diários usados por DDS/Android continuam com seus formatos atuais.
Instalar também `torre_contract.py` ao lado de `torre_api.py` e `torre_access.py`.
As cópias em `DDS/orange-integration` precisam ser atualizadas antes do deploy.
Não ativar a troca de fonte até validar as entradas, composição, eventos e
contingência. Não publicar credenciais ou dados reais nos exemplos/testes.

## Sincronização implementada em 10/10/2026

O cadastro deve ser exportado com o Python/ambiente do DDS, que já possui as
dependências e credenciais de leitura do Firestore e escrita no Storage.
A chamada sem `--publish` apenas grava um arquivo local. Exemplo de argumentos:

```text
scripts/export_torre_catalog.py --dds-root D:/programas/DDS/dds-webtools --empresa ChicoEletro --output dados-local/torre/cadastro-export.json.gz
```

Executar o script a partir da raiz do coletor. Adicionar `--publish` publica em
`dados/chicoeletro/dds/controle/cadastro/current.json.gz`; não publica a torre.
O destino pode ser configurado por `--bucket` e `--prefix`.
Nenhuma exportação real ou publicação foi executada nesta etapa.

Antes de habilitar o sincronizador, definir a execução periódica do exportador
no ambiente DDS e sua atualização após mudanças de cadastro. O limite padrão
de idade da exportação é 24 horas; uma exportação antiga é rejeitada mesmo
quando a consulta ao Storage funciona. A automação desse exportador ainda
precisa ser instalada no projeto/ambiente DDS.

No coletor, `TORRE_INPUTS_ENABLED=true` ativa a leitura antes de cada coleta,
com intervalo mínimo entre tentativas persistido em disco (padrão 300 segundos).
Usa o mesmo bucket/credenciais do coletor, sem exigir habilitar uploads Rotalog.
`TORRE_DDS_CONTROL_PREFIX` aceita `{empresa}` e exige isolamento por empresa;
`TORRE_DDS_HISTORY_DAYS=25` seleciona as últimas 25 datas publicadas, até a
referência local atual, sem inventar dias ausentes ou limitar a dias corridos.

O conjunto validado fica em
`dados-local/torre/inputs/<empresa>/current.json.gz`, com cadastro, diários,
versões GCS e `syncedAt`. `status.json` registra tentativa, última sincronização
válida, disponibilidade e falha. Há lock entre processos e troca atômica do
conjunto completo. Um erro em qualquer fonte preserva o conjunto anterior.
Uma listagem remota bem-sucedida vazia é distinta de erro de listagem.
Mudanças/deleções válidas são refletidas na próxima sincronização completa.

Metadados GCS permitem evitar downloads de versões inalteradas. O download
usa a geração observada para rejeitar alterações durante a leitura. Revisões
não são reutilizadas entre buckets. As chamadas têm timeout e não fazem
retries internos; a próxima tentativa respeita o intervalo configurado.

`TorreInputsSync.snapshot()` valida e lê apenas disco, calcula a idade e expõe
as fontes para a futura composição. Em `sources.dds.updatedAt`, o horário
representa a verificação bem-sucedida do conjunto DDS, não a data do último
DDS realizado. Os horários reais/generatedAt continuam dentro dos diários.
Uma falha conhecida, intervalo vencido ou cadastro antigo produz `stale=true`.

A sincronização das entradas, sozinha, não gera a torre. A composição abaixo
usa essas entradas. O frontend e a sincronização Rotalog existente permanecem
com seus contratos atuais.

## Composição local — 11/10/2026

`coletor/torre_composer.py` gera `torre/current.json` após cada ciclo quando
`TORRE_COMPOSE_ENABLED=true` e `TORRE_INPUTS_ENABLED=true`. Ambas as opções
permanecem desativadas no exemplo até validar as fontes reais. O ciclo lê o
índice e os consolidados do dia atual/anterior, sem consultas por navegador.
Não é necessário haver alteração Rotalog para recompor as regras de horário.

São preservados setor, participantes, motorista, veículo, metadados de
cadastro, presença e histórico DDS, equipes sem telemetria e atividade recente.
`active` segue a atividade recente operacional do DDS; `cadastroActive` preserva
separadamente o valor administrativo do cadastro, inclusive equipes inativas.
Aliases ambíguos e snapshots duplicados impedem publicação, preservando a saída.
Os contadores executados usam `executadosHoje` do coletor; o índice compacto
não contém `totalConcluidos`, portanto não deve produzir um zero fictício.
`summary` mantém o resumo Rotalog original; totais de todas as equipes devem ser
validados na integração da interface, pois incluem agora equipes DDS.

O journal privado `torre/state/current.json.gz` guarda observações e eventos
junto com a projeção candidata antes da troca pública. Uma falha de publicação
é recuperada na próxima composição sem perder ou renovar eventos. Journal
corrompido impede composição até recuperação; não reinicializar silenciosamente.

A primeira observação estabelece a referência e não inventa eventos recentes.
Mudanças reais de turno/intervalo/serviço geram eventos uma vez. Os timestamps
reais dos serviços e suas transições recuperam estados intermediários entre
coletas; transições sem horário recuperável não podem ser reconstruídas.
Horários reais anteriores à composição anterior não viram eventos recentes
por simples correção tardia do histórico. Na ausência de horário em uma mudança
observada, o evento informa `timeSource: observation`; não trata a observação
como horário real de execução. Conclusão histórica inédita sem horário é
ignorada como evento recente. Eventos publicados têm janela de 24 horas.

Se a coleta falhar, a última visão ainda pode ser recomposta com o índice
preservado e `sources.rotalog.stale=true`. Falhas de composição preservam o
último JSON público. `TORRE_ROTALOG_MAX_AGE_SECONDS=1200` é o limite padrão de
idade Rotalog; o navegador também precisa verificar os timestamps se o processo
parar. A composição atual acontece por ciclo, portanto a mudança das 18h aparece
na coleta seguinte, não por um timer independente.

Testes locais: 75 testes passaram, cobrindo API, fontes, cards, eventos,
armazenamento, sincronização e isolamento por empresa. A comparação completa
com a torre em produção e a troca de consultas ainda não foram executadas.
