# Instrucoes para o site e o aplicativo DDS: alteracoes de equipes

Use este documento como contexto/prompt ao implementar a tela e a integracao no
site e no aplicativo DDS. O repositorio `dds-coletor-rtl` e o coletor executado
no Orange Pi; os codigos do site e do aplicativo podem estar em repositorios
separados.

## Objetivo do produto

O Orange Pi deve coletar e publicar observacoes do ROTALOG. O site deve comparar
as observacoes com os dados atualmente aceitos, apresentar alteracoes sugeridas
e permitir que uma pessoa autorizada aceite ou rejeite cada alteracao. O
aplicativo DDS deve apresentar o estado aceito e, conforme os fluxos existentes,
as alteracoes pendentes e o historico.

Uma observacao do ROTALOG ou uma diferenca calculada automaticamente nao e uma
alteracao aprovada. Nunca atualize o estado oficial de uma equipe somente porque
chegou uma nova coleta. A aprovacao manual e a decisao de negocio.

## Prompt de implementacao

> Implemente no site e no aplicativo DDS uma funcionalidade de acompanhamento
> de alteracoes de equipes, usando os dados coletados pelo `dds-coletor-rtl`.
>
> **Leia antes o contrato existente entre DDS e coletor**
> (`README_INTEGRACAO_DDS.md` neste repositorio; confira tambem a copia
> correspondente no repositorio DDS). Ele define a coleta no Orange e o DDS
> como consumidor dos resultados publicados, incluindo caminhos, schemas e APIs.
> O fluxo de aprovacao manual descrito aqui acrescenta uma responsabilidade de
> escrita/decisao ao site, que nao esta definida no contrato atual. Antes de
> implementar, identifique explicitamente essa diferenca e atualize o contrato
> nos repositorios envolvidos para definir a fonte oficial, a API de decisao,
> autorizacao, persistencia e auditoria. Nao altere silenciosamente os contratos
> existentes de leitura da torre, historico ou Android.
>
> Antes de editar, inspecione os repositorios, a arquitetura, os modelos de
> dados, a autenticacao/autorizacao, as APIs, os padroes de UI e os testes
> existentes. Confirme onde ficam o site e o aplicativo; este repositorio do
> Orange pode conter apenas documentacao de integracao. Reutilize os servicos,
> componentes, design system e convencoes existentes. Nao presuma framework,
> banco, API ou caminho remoto sem verificar o codigo.
>
> **Separacao de responsabilidades**
>
> - O Orange Pi autentica no ROTALOG, coleta, reconcilia e publica observacoes.
>   Ele nao aprova, rejeita nem altera automaticamente o cadastro oficial.
> - O site e o ponto de revisao e decisao manual: mostra o que mudou, permite
>   aceitar ou rejeitar e registra quem decidiu, quando e, opcionalmente, uma
>   justificativa.
> - O aplicativo DDS consome o estado aprovado e o historico. Mostre pendencias
>   se isso se encaixar nos fluxos atuais, mas nao crie um segundo fluxo de
>   aprovacao no aplicativo sem requisito explicito e autorizacao compativel.
> - Separe observacoes coletadas, alteracoes pendentes, decisoes e estado
>   oficial aceito. Uma coleta nunca deve sobrescrever uma decisao manual.
>
> **Campos a acompanhar**
>
> - Identidade estavel da equipe; nao use o veiculo como identidade permanente.
> - Eletricistas: matricula e nome, quando disponiveis.
> - Base/agencia.
> - Numero do contrato associado a observacao, quando disponivel. Trate-o como
>   atributo que pode mudar por data/observacao; mostre-o para contexto e
>   compare-o como alteracao somente se a regra de negocio confirmar que contrato
>   faz parte da atribuicao aprovada da equipe.
> - Veiculo.
> - Tablet/identificador, se util nos fluxos existentes.
> - Data operacional, horario observado com fuso, fonte e referencia ao registro
>   original.
>
> Considere alteracao de cada campo independentemente. Uma troca de veiculo e
> uma alteracao mesmo quando os eletricistas e a base nao mudam. Nao interprete
> valor ausente, incompleto ou explicitamente nao identificado como remocao ou
> troca confirmada; apresente como dado insuficiente ou pendencia de revisao.
>
> **Fluxo esperado**
>
> 1. Ler as observacoes publicadas e preservar sua procedencia.
> 2. Comparar a observacao com o ultimo valor aprovado para a mesma equipe.
> 3. Criar/atualizar uma sugestao pendente por equipe e campo, mostrando valor
>    anterior, valor observado, data e origem.
> 4. No site, permitir aceitar ou rejeitar a sugestao com controle de acesso.
> 5. Ao aceitar, atualizar o estado oficial e adicionar uma entrada imutavel de
>    auditoria com usuario, horario, valores, origem e decisao. Ao rejeitar,
>    manter o estado oficial e registrar a rejeicao.
> 6. Se uma coleta posterior corrigir uma sugestao pendente, atualizar a
>    sugestao sem duplicar. Se a sugestao ja foi decidida, nao altere essa
>    decisao: registre a nova observacao e crie uma nova sugestao quando houver
>    uma diferenca material em relacao ao estado aprovado.
> 7. Uma repeticao da mesma coleta deve ser idempotente.
>
> **Interface**
>
> - Criar uma lista de alteracoes pendentes com filtros por equipe, campo,
>   periodo e status, seguindo os componentes e padroes visuais existentes.
> - Exibir claramente o antes/depois, a data operacional e a origem. Diferenciar
>   "observado", "pendente", "aceito" e "rejeitado"; nao apresentar pendencia
>   como fato confirmado.
> - Oferecer acoes de aceitar e rejeitar com confirmacao apropriada, feedback de
>   sucesso/erro e protecao contra duplo envio ou decisao concorrente.
> - Disponibilizar uma linha do tempo por equipe com observacoes e decisoes
>   auditaveis. No aplicativo, priorizar leitura clara em tela pequena e manter
>   os dados aceitos identificaveis como oficiais.
> - Tratar carregamento, vazio, indisponibilidade, dados incompletos e falta de
>   permissao sem exibir sucesso falso nem apagar o ultimo dado valido.
>
> **Dados existentes no coletor**
>
> - O README do coletor descreve os arquivos e caminhos operacionais:
>   `dados-local/rotalog/` no disco e `dados/{empresa}/rotalog/` no Firebase.
> - `rotalog/equipes/team-registry.json` e o cadastro permanente, sincronizado
>   em formato compactado pelo coletor. Cada equipe pode conter
>   `assignmentHistory`, observacoes por turno com data, veiculo, base/agencia,
>   tablet e eletricistas. `assignmentChanges` e um diff automatico entre
>   observacoes; ele **nao** representa aprovacao humana nem auditoria de
>   decisao.
> - `rotalog/eletricistas/diario/AAAA-MM-DD.json.gz` e o indice diario compacto
>   por equipe, com tablet e eletricistas identificados.
> - O resumo diario `rotalog/quilometragem/diario/AAAA-MM-DD.json.gz` inclui
>   `resumoEquipes.registros`, com o campo `contrato` por registro/equipe. A
>   coleta recebe esse valor da coluna `Contrato` da tela de equipes. Verifique
>   o conteudo e o caminho remoto publicado antes de integrar; o numero do
>   contrato ainda nao esta incluido em `assignmentHistory` no cadastro
>   permanente do coletor.
> - `rotalog/equipes/daily/AAAA-MM-DD/<EQUIPE>.json.gz` guarda o historico
>   operacional diario da equipe (turno e servicos); nao o confunda com o
>   cadastro de atribuicoes aceitas.
> - Os nomes e campos acima descrevem o coletor atual. Verifique no codigo e
>   nos dados publicados os caminhos remotos exatos, envelope JSON, schema,
>   isolamento por empresa e disponibilidade antes de implementar o leitor.
>   Nao dependa de arquivos locais do Orange se o site/aplicativo nao tiver
>   acesso autorizado a eles.
>
> **Seguranca, consistencia e compatibilidade**
>
> - Respeitar o isolamento por empresa/tenant e as permissoes existentes.
>   Somente perfis autorizados podem decidir alteracoes; nao confiar em
>   autorizacao somente no cliente.
> - Fazer escrita atomica/transacional ou usar controle de versao/conflito
>   equivalente, para que decisoes concorrentes nao sobrescrevam dados.
> - Nao colocar credenciais do ROTALOG ou segredos do Orange no navegador ou no
>   aplicativo. Usar APIs e mecanismos de autenticacao existentes.
> - Preservar consumidores, APIs e formatos existentes. Evitar migracao
>   destrutiva; prever leitura de registros antigos sem campos de atribuicao.
> - Nao tratar `assignmentChanges` como fonte de verdade. A fonte do estado
>   oficial deve ser o registro de atribuicao aceito e sua auditoria.
>
> **Testes e entrega**
>
> - Cobrir diferencas de eletricistas, base e veiculo isoladamente; dado ausente;
>   repeticao/correcao da coleta; aprovar/rejeitar; concorrencia; permissoes;
>   isolamento por empresa; historico e compatibilidade com registros antigos.
> - Executar os testes, lint e verificacoes de tipo existentes nos projetos
>   alterados. Nao modificar o coletor Orange alem do necessario para expor de
>   forma confiavel observacoes brutas e idempotentes.
> - Ao final, documentar os arquivos/API alterados, o fluxo de aprovacao,
>   eventuais migracoes e os comandos/resultados de validacao.

## Ajustes pendentes no DDS/Site - registrados em 10/11/2026

Esta lista registra trabalho futuro nos consumidores. Nesta etapa, o trabalho
de implementacao continua apenas no coletor. Os itens abaixo ainda precisam
ser implementados e validados nos repositorios do DDS/Site.

### Historico de SS interrompidas

Contrato de campos e regra de deteccao:
[README_INTEGRACAO_DDS.md - SS interrompidas](README_INTEGRACAO_DDS.md#ss-interrompidas-pela-entrada-de-outra-ss).

- [ ] Adaptar o leitor/adaptador de diarios e as APIs que entregam esses
  registros ao site e ao aplicativo, preservando `statusAtual`,
  `semExecucaoType` e o objeto `interrupcao`.
- [ ] Exibir `DESLOCAMENTO_ISOLADO` como **Deslocamento Isolado** e
  `RETIRADO_PELO_COD` como **Retirado pelo COD**, no historico, detalhes e
  filtros de SS. Usar os valores publicados pelo coletor; nao tentar deduzir
  novamente a retirada no consumidor.
- [ ] Mostrar que a classificacao e inferida quando `interrupcao.inferida`
  for verdadeira. Apresentar `interrupcao.observadoEm` como horario em que a
  troca foi observada, sem chama-lo de horario confirmado da retirada.
- [ ] Vincular a SS seguinte por `servicoSeguinteId` e `protocoloSeguinte`,
  respeitando a equipe e a empresa do registro.
- [ ] Preservar os horarios recebidos de deslocamento e execucao. Mostrar
  conclusao/retorno ausentes como nao informados, sem preencher com a coleta
  ou com o inicio da proxima SS e sem calcular duracao final ficticia.
- [ ] Excluir ambos os estados dos contadores de servicos concluidos e dos
  indicadores que exigem conclusao. Se forem mostrados totais de interrupcoes,
  identifica-los separadamente.
- [ ] Nao apresentar uma SS ausente da atividade atual como ainda ativa apenas
  porque seu ultimo estado no historico e deslocamento/execucao. O simples
  desaparecimento nao comprova retirada; consultar `ordensServico.atual` para
  a atividade atual.
- [ ] Atualizar o registro existente quando uma coleta posterior trouxer a
  conclusao, removendo a indicacao inferida e evitando duplicar a SS ou os
  contadores. Preservar compatibilidade com diarios antigos e `REDIRECIONADO`;
  nao converter esse estado antigo automaticamente para um dos novos rotulos.
- [ ] Conferir a disponibilidade dos diarios publicados antes da integracao.
  O indice compacto, sozinho, nao contem todo o historico de interrupcoes.
- [ ] Testar SS anterior em deslocamento e em execucao, seguida por outra SS
  em deslocamento ou execucao; comercial, emergencial e Emergencia - Generica
  de EP; desaparecimento sem substituta; conclusao posterior; repeticao da
  leitura; contadores; dados antigos e isolamento por empresa.

### Mudancas de equipe e torre

- [ ] Implementar o fluxo de sugestoes, aprovacao/rejeicao, estado oficial e
  auditoria descrito neste documento, com autorizacao no servidor.
- [ ] Verificar quais campos sao efetivamente publicados antes de construir
  os filtros: o coletor calcula diferencas de eletricistas, base e veiculo;
  tablet e armazenado, mas ainda nao gera `assignmentChanges`, e contrato
  ainda nao consta em `assignmentHistory`.
- [ ] Validar a composicao local da torre com cadastro, historico DDS e dados
  reais antes de trocar sua fonte, conforme `README_API_TORRE.md` e
  `CONTRATO_TORRE.md`. Consolidar o contrato da rota utilizada pelos clientes.

## Nota sobre o estado atual

O coletor ja grava observacoes e calcula `assignmentChanges` no cadastro
permanente, mas esse calculo nao tem decisao manual associada. Ate o site e o
aplicativo adotarem o fluxo acima, trate esses dados como observacoes/sugestoes,
nao como alteracoes oficialmente aprovadas. A reconstrucao historica segue as
janelas de reconciliacao existentes e nao garante automaticamente todos os dias
antigos.
