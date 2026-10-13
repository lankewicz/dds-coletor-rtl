# Contrato entre o DDS e o dds-coletor-rtl

Documento para uso também em `D:\programas\dds-coletor-rtl`.
Revisado em 08/10/2026 com base no código dos dois projetos.

## Responsabilidades

O **dds-coletor-rtl**, no Orange Pi, é o único responsável por autenticar no
Rotalog, raspar, reconciliar dados, manter históricos e publicar resultados.
O **DDS** apenas lê esses resultados para a torre, o histórico e as APIs do
Android. O novo módulo de leitura é `bdo/services/rotalog_reader.py`.
O DDS não chama módulos Python do coletor externo nem precisa de suas credenciais.

Nesta etapa não foi necessário alterar o formato emitido pelo coletor externo.
Foram preservados os caminhos e a adaptação de schema v2 para os consumidores
existentes. Não é uma migração da interface para Firebase Hosting.

## Arquivos esperados

Bucket padrão: `dds-treinamentos.firebasestorage.app`.
Raiz para ChicoEletro: `dados/chicoeletro/rotalog`.

| Arquivo remoto | Uso no DDS |
| --- | --- |
| `equipes/current/index.json.gz` | Índice compacto de todas as equipes, usado pela torre. |
| `equipes/current/<EQUIPE>.json.gz` | Consulta individual alternativa quando a equipe não está no índice. |
| `equipes/daily/AAAA-MM-DD/<EQUIPE>.json.gz` | Histórico diário e APIs do Android. |
| `logs/AAAA-MM-DD.json.gz` | Leitura de logs antigos, quando existirem; opcional. |

O índice é obrigatório para a torre; os diários são necessários para o histórico
completo. Arquivos individuais em `current` são uma alternativa, não uma exigência
para equipes presentes no índice. O DDS não precisa dos consolidados locais ou
de arquivos Parquet para carregar a torre.

Configurações correspondentes no DDS:

- `DDS_BUCKET_NAME`: bucket.
- `ROTALOG_GCS_CACHE_BLOB`: caminho do índice remoto.
- `ROTALOG_TEAM_STORAGE_PREFIX`: raiz `dados/chicoeletro/rotalog/equipes`.
- `ROTALOG_ORANGE_INDEX_URL`: endpoint privado do índice no Orange.
- `ROTALOG_ORANGE_TIMEOUT_SECONDS`: limite de espera, padrão 2 segundos.
- `ROTALOG_ORANGE_PROXY`: proxy SOCKS da conexão Tailscale quando usado.
- `ROTALOG_LOCAL_DATA_DIR` / `ROTALOG_INDEX_PATH`: alternativas locais.

O leitor de logs mantém a raiz histórica de ChicoEletro. Suporte a outra empresa
deve ser validado nas rotas, nos prefixos e na autorização antes de publicação.

## Formato e identidade

O transporte remoto usa JSON UTF-8 compactado com gzip. O leitor também aceita
JSON sem gzip, inclusive `index.json` local. O endpoint privado pode devolver
JSON ou bytes gzip, e deve responder HTTP 200 para um índice válido.

O índice contém um objeto `equipes`, com cada chave representando uma equipe.
`snapshots` continua aceito para compatibilidade com o formato antigo.
O mapa vazio `{ "equipes": {} }` é válido; uma falha de coleta não deve ser
publicada como índice vazio. Preserve o último resultado válido e registre o erro.

Equipes usam a identidade base em maiúsculas, por exemplo `E3T01`.
Sufixos do AUTOTRACK, como `E3T01(2)`, devem continuar consolidados pelo coletor.
Use a mesma chave no índice, no `teamKey` e no nome do arquivo diário.

Campos do schema v2 consumidos pela torre:

- `teamKey`, `teamId`, `updatedAt`, `operationalDate`, `version`, `timezone`.
- `conexao`: indicadores de conexão disponibilizados pelo coletor.
- `jornada.turno`: `status`, `inicio` e `fim` quando conhecidos.
- `jornada.emIntervalo`: indicador do intervalo atual.
- `ordensServico.atual`: serviço atual, ou `null` quando não houver.
- `fila.comercial` e `fila.emergencia`: contadores não negativos.

O coletor também publica `executadosHoje`. Não renomeie campos ou mude unidades
sem adaptar e testar o DDS. O índice compacto não precisa conter todo o histórico.

Campos do diário usados pelo DDS:

- `teamKey`, `date`, `updatedAt`, `version` e `operationalDate`, quando disponível.
- `jornada.turno`, `jornada.intervalos` e `ordensServico.historico`.
- `ordensServico.atual`: serviço em andamento quando houver.
- Serviços: `protocolo`/identificador, `tipo`, `categoria`, `statusAtual`,
  `inicioDeslocamento`, `inicioExecucao`, `fimExecucao`/`termino`, `retorno`.

O leitor do DDS adapta o diário v2 para `services`, `turno` e `current`, usados
pelos consumidores existentes. O coletor não precisa duplicar esses campos
legados por causa desta remoção da raspagem.

## Datas e mudanças de status

Use `AAAA-MM-DD` para a data do diário e ISO 8601 com fuso para os horários
completos, por exemplo `2026-10-08T17:00:00-03:00`.
Referência local: `America/Sao_Paulo`. Preserve a data real dos eventos em
turnos que atravessam a meia-noite, sem substituir pela data da publicação.

`updatedAt`/`updatedAtIso` indicam atualização ou publicação, não o horário de
uma mudança de status. Um turno que continua fechado deve manter o mesmo
horário de fim; não reescrever esse horário a cada coleta. Horários desconhecidos
devem permanecer ausentes, sem inventar abertura ou fechamento.

Quando disponíveis, preserve `transitions` dos serviços, com `status` e horário
real (`timestampMs`, `hora` ou `timestamp`). O feed só reconstrói as transições
que recebe. Como o índice compacto contém apenas o serviço atual, ele não
garante sozinho um histórico completo de conclusões ou eventos intermediários.
Uma futura ampliação do feed precisará de histórico compacto publicado pelo
coletor ou de leitura explícita dos diários; não faz parte desta separação.

## Publicação e disponibilidade

Mantenha a política atual: atualizar o índice remoto quando mudarem os dados
operacionais e publicar diários nos eventos previstos pelo coletor. A fila
persistente deve continuar tentando uploads que falharem. No Firebase,
`updatedAtIso` do índice pode indicar a última publicação, não a última raspagem.

O DDS tenta o índice privado do Orange, depois o Firebase Storage, depois os
arquivos locais disponíveis. Se todas as fontes falharem, preserva o último
cache válido. O endpoint esperado no Orange é `GET /api/monitor/turnos`,
normalmente em `http://100.106.248.106:8766`, acessível pela rede privada.
O utilitário DDS `scripts/serve_rotalog_index.py` permanece como leitor somente;
ele não raspa e não deve ser confundido com o daemon de coleta.

O README atual do coletor informa que os logs de execução ficam somente no
Orange. Por isso `/api/rotalog/sync-log` pode responder 404 no DDS. Não é
necessário passar a publicar logs para manter a torre e o Android funcionando.

## Compatibilidade de APIs

Permanecem as consultas `/api/rotalog/tempo-real`, `/teams/<equipe>/current`,
`/teams/<equipe>/daily`, `/mobile/<equipe>/current` e `/mobile/<equipe>/daily`
sob o prefixo `/api/rotalog`, com a autenticação já existente.

As antigas ações de coleta `/api/rotalog/sync-now` e
`/boletim-x-ponto/api/import/rotalog` permanecem como respostas HTTP 410 para
clientes autenticados antigos. Elas não iniciam raspagem nem transferem
comandos ao Orange. Atualizar a torre somente relê os resultados disponíveis.

## Verificação antes de publicar

1. Confirmar o coletor ativo no Orange e a publicação no bucket correto.
2. Validar o índice e um diário de uma equipe com serviço em andamento.
3. Conferir abertura, intervalo, fechamento e horários no DDS.
4. Conferir o mesmo serviço e seus horários no aplicativo Android.
5. Simular indisponibilidade do endpoint privado e confirmar leitura do Firebase.
6. Repetir uma coleta sem mudanças e confirmar que os eventos não são renovados.

Testes do lado DDS: `tests/test_rotalog_reader.py`,
`tests/test_rotalog_monitor_source.py`, `tests/test_monitor_files_only.py`,
`tests/test_monitor_status_feed.py` e `tests/test_monitor_feed.js`.

## Recursos antigos no Google Cloud

O novo `deploy.ps1` publica somente Site e Monitor e não associa as credenciais
de raspagem. A remoção local não desliga recursos já existentes na nuvem.
Antes de aposentar `rotalog-sync`, `rotalog-daily-archive` e suas agendas,
inventarie o estado e confirme que o coletor externo cobre suas tarefas.
Prefira pausar agendas primeiro. O leitor não precisa receber
`ROTALOG_USUARIO` ou `ROTALOG_SENHA`; essas credenciais pertencem ao coletor.

Esta etapa não executa deploy, não apaga serviços/agendas da nuvem e não altera
o código do coletor externo. O desligamento de infraestrutura deve ser uma
ação posterior acompanhada de verificação.
