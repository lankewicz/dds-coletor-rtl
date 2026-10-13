# Continuidade: torre pronta no Orange Pi

Atualizado em 09/10/2026. Documento para retomar o trabalho ao abrir
`D:\programas\dds-coletor-rtl` no Codex.

## Pedido e decisões do usuário

Adaptar o raspador, que é um projeto separado do DDS, para preparar a torre
completa no Orange Pi e entregá-la diretamente ao navegador por HTTPS.
O computador do usuário NÃO pode precisar de Tailscale. Foi escolhida a
publicação via Tailscale Funnel, com autenticação na API.

Objetivo de custo: tirar do Cloud Run a coleta, a composição da torre e suas
atualizações periódicas. Firebase Hosting apresenta a interface; Orange
coleta e prepara os dados; Cloud Run permanece para autenticação/permissões,
relatórios, importações e operações de alteração ainda existentes.

Este documento não declara a migração concluída. Não houve ativação do Funnel,
instalação remota da API, geração da torre completa ou troca da fonte do site.

## Projetos e equipamento

- Raspador local: `D:\programas\dds-coletor-rtl`.
- Site/backend: `D:\programas\DDS\dds-webtools`.
- Orange: IP Tailscale `100.106.248.106`.
- Nome Tailscale: `ddspto1`; domínio `ddspto1.tail1344e5.ts.net`.
- Hostname do sistema: `orangepizero2`; Linux; Tailscale 1.102.4 na imagem.
- Instalação prevista pelas units: `/home/orangepi/dds-coletor-rtl`.
- Python previsto: `.venv-3.14/bin/python`; confirmar no equipamento.
- Tentativa SSH `orangepi@100.106.248.106` com BatchMode foi recusada
  (`Permission denied (publickey,password)`). Não houve alteração remota.
  Confirmar usuário/chave existentes; não pedir conteúdo da chave ou senha no chat.

## O que já está publicado no Google

Projeto `dds-treinamentos`, região `us-central1`.
Site público: https://chicoeletro.web.app.

11 páginas e assets foram publicados no Firebase Hosting. APIs e algumas
telas dinâmicas continuam no serviço `dds-admin-site`. Último deploy nesta
conversa: revisão `dds-admin-site-00248-rr5`, imagem
`gcr.io/dds-treinamentos/dds-webtools:20261009-103945`.

O código de raspagem foi retirado do DDS. Em 09/10/2026, a infraestrutura antiga
foi desligada e verificada:

- `rotalog-sync`: escalonamento manual, `manualInstanceCount: 0`; HTTP 503.
- Agenda `rotalog-daily-archive-0430`: `PAUSED`.
- Job `rotalog-daily-archive`: preservado; última execução listada concluída.

Não reativar os recursos antigos no deploy do raspador.
Em 10/10/2026, mínimo zero e CPU por requisição foram aplicados ao site,
por autorização do usuário. Revisão `dds-admin-site-00249-z7h`, 100% do
tráfego. O deploy.ps1 foi ajustado para preservar a configuração sob demanda.

## Arquivos iniciais já gravados no raspador

- `torre_api.py`: servidor HTTP somente leitura, biblioteca padrão Python,
  vinculado a `127.0.0.1:8081` para ser publicado por Funnel.
- `torre_access.py`: credencial assinada HMAC-SHA256, exclusiva para leitura,
  válida por 600 segundos, vinculada à empresa e ao usuário.
- `torre-api.service`: unit systemd separada da coleta; usuário `orangepi`,
  ambiente `.torre-api.env`, pasta e Python conforme instalação acima.
- `README_API_TORRE.md`: preparação da API e configuração HTTPS.
- `README_INTEGRACAO_DDS.md`: contrato e compatibilidade com o DDS.

Esses arquivos são um ponto de partida, ainda não instalados no Orange.
Não contêm chave real nem tornam os dados públicos por si mesmos.

Rotas preparadas:

- `/api/rotalog/index`: lê `dados-local/rotalog/equipes/current/index.json.gz`.
- `/api/torre`: lê `dados-local/torre/current.json`; exige `schemaVersion: 1`,
  `company` correspondente e `items` como lista. Retorna 503 até haver saída válida.

A API exige `Authorization: Bearer ...`, inclusive antes de responder 304.
Só permite essas rotas. CORS autoriza apenas origens configuradas, não substitui
autenticação. Credenciais, arquivos locais e outras rotas não são publicados.

## Alterações locais preparadas no DDS, ainda não publicadas

- `main.py`: GET `/api/torre/access` autentica a sessão e exige permissão
  `monitor`; emite credencial curta e endereço da API. Sem configuração retorna 503.
- `torre_access.py`: mesmo protocolo de assinatura usado no coletor.
- `hosting/orange-torre.js`: cliente opcional, credencial apenas em memória,
  renovação antes de expirar, chamadas diretas com cookies omitidos e timeout.
  Ainda NÃO carregado pelas páginas e NÃO substitui `app.js`.
- `orange-integration/`: cópias dos arquivos e documentação.
- `tests/test_orange_api.py`: quatro testes de assinatura, expiração, empresa,
  autenticação em 304, CORS, restrição de caminhos e arquivos inválidos.
- `tests/test_hosting.py`: seis testes, incluindo autorização do novo endpoint.

10 testes passaram e o cliente JavaScript passou em `node --check`.
Os testes da API estão no DDS; trazer testes equivalentes para o raspador antes
de alterar os arquivos copiados. Depois manter as cópias/protocolo consistentes.

## Implementação pendente: torre completa

1. Ler as instruções do repositório e analisar o estado atual antes de editar.
2. Examinar `main.py`, `coletor/storage.py`, `storage_io.py`, `transitions.py`,
   `team_registry.py` e os testes de índice/transições. Aproveitar os dados existentes.
3. Comparar com a torre atual no DDS:
   - `monitor/services/torre_monitor_builder.py`: composição e regras dos cards.
   - `monitor/services/turnos_service.py`: respostas de turnos/histórico.
   - `monitor/services/dds_control_projection.py`: presença e histórico DDS.
   - `monitor/routes/monitor_routes.py`: contratos de `/api/turnos`,
     `/api/turnos/dds`, `/api/activity-feed` e configurações.
   - `monitor/static/app.js` e módulo de feed: consumidores das respostas.
4. Produzir localmente uma visão pronta compatível com esses consumidores.
   Definir formalmente o contrato antes de alterar o frontend.
5. Preservar cadastro, aliases, empresa, setor, equipe ativa/inativa, equipes
   controladas por DDS sem telemetria, participantes, veículo, estados,
   serviços, contadores, histórico DDS e indicadores. Identificar de onde o
   Orange obterá os complementos DDS/cadastro e como atualizará essas fontes.
   Não presumir que o índice Rotalog sozinho contém toda a torre.
6. Gerar eventos recentes de mudança, com identidade e timestamp estáveis.
   Se um turno já estava fechado e continua fechado, NÃO renovar o evento.
   Primeira observação de uma equipe fechada não deve inventar um fechamento recente.
   Aplicar o mesmo princípio às mudanças dos serviços e persistir o estado
   necessário para evitar duplicação após reiniciar o processo.
7. Gravar `torre/current.json` atomicamente, preservando a última saída válida
   quando uma coleta falhar. Incluir empresa, versão, horário da coleta,
   horário da composição e indicação de dados desatualizados.
8. Construir a projeção por coleta/mudança, não por acesso de cada navegador.
   Manter leitura leve, ETag e publicação consistente. Avaliar cache de bytes
   no processo da API se a carga justificar; não recriar projeções por request.
9. Preservar os arquivos e contratos atuais consumidos pelo app móvel e DDS.
   Manter sincronização Firebase existente durante a validação. Não desligá-la
   antes de conferir todos os consumidores e a estratégia de contingência.

## Autenticação e segredos

Usar uma chave aleatória dedicada de pelo menos 32 bytes, instalada somente no
Orange e em Secret Manager/Cloud Run como `TORRE_SIGNING_KEY`.
Não reutilizar `APP_SECRET_KEY`; não salvar segredo em Git, JavaScript, URL,
logs ou neste documento. A transmissão HTTPS é obrigatória.

Ambiente da API no Orange (`.torre-api.env`, permissões restritas):

```text
TORRE_SIGNING_KEY=<segredo gerado com segurança>
DDS_EMPRESA_PADRAO=ChicoEletro
TORRE_ALLOWED_ORIGINS=https://chicoeletro.web.app,https://chicoeletro.firebaseapp.com
```

No Cloud Run: mesma chave dedicada e `TORRE_PUBLIC_URL` com a origem HTTPS real.
Incluir o secret no deploy.ps1 do DDS para não perdê-lo em deploys posteriores.
Usuário sem permissão monitor não recebe credencial. Uma credencial emitida
pode continuar válida por até dez minutos após revogação; documentar esse limite.
A renovação ocasiona chamadas ao Cloud Run, mesmo quando os dados vêm do Orange.

## Publicação HTTPS e instalação

Primeiro validar a API local e proteger os dados. Depois consultar o estado
atual do Serve/Funnel, MagicDNS, certificados e políticas Tailscale.
Funnel atende clientes sem Tailscale; Serve atende apenas a rede Tailscale.

Comandos de referência, depois de conferir portas/configuração existentes:

```bash
tailscale serve status
tailscale funnel status
tailscale funnel --bg 8081
tailscale funnel status
```

A mesma porta HTTPS não pode ser Serve privado e Funnel público ao mesmo tempo.
Não ativar Funnel sobre um servidor de arquivos ou a API antiga sem autenticação.
Não resetar configurações existentes sem analisar os demais usos.
Funnel está em beta e possui limites de banda. Avaliar carga real.

Instalar a unit preparada no systemd, confirmar diretório/Python/usuário, criar
ambiente seguro, executar daemon-reload e iniciar o serviço separado da raspagem.
Testar a URL pública de um cliente fora do Tailscale. Não basta testar o IP privado.

Referências oficiais:
- https://tailscale.com/docs/features/tailscale-funnel
- https://tailscale.com/docs/reference/tailscale-cli/funnel

## Validação e integração final

- Testar assinatura alterada, credencial vencida, empresa errada, usuário sem
  permissão, ausência de Bearer e tentativa de obter arquivos fora das rotas.
- Testar CORS/preflight, 200/304, arquivo inválido, queda da coleta, reinício,
  falha de internet e preservação da última projeção válida.
- Testar que status iguais não criam/renovam eventos; uma mudança real cria um
  evento uma vez e preserva o timestamp nas próximas coletas.
- Comparar as equipes, regras de estado, DDS e totais com a torre atual.
- Migrar no DDS as consultas de turnos, histórico e feed; evitar deixar chamadas
  automáticas antigas paralelas. Configurações/escritas podem continuar no backend.
- Pausar polling em aba oculta e não consultar mais rápido que a coleta.
- Mostrar última atualização e indisponibilidade sem apagar a última visão válida.
- Publicar backend de autorização primeiro, depois interface no Hosting.
- Só depois medir custos e decidir por requisição/mínimo zero no Cloud Run.

## Cuidados com o estado local

O raspador tinha exclusões pendentes em `v2/` antes destas alterações.
Não restaurar, sobrescrever ou incluir essas exclusões em commit sem verificar
o contexto do usuário. Os novos arquivos da API estavam não rastreados.

O DDS também tem várias alterações anteriores pendentes (remoção da coleta,
Hosting e API). Não descartá-las. A regra `.agents/AGENTS.md` do DDS limita
commits a dois por dia; consultar as regras próprias do raspador.
Não fazer commit/push/deploy apenas por inferência deste documento.

## Primeiro passo ao abrir a nova pasta

Ler este documento e as instruções do raspador. Inspecionar a composição atual
do índice e planejar a origem dos complementos DDS/cadastro. Implementar e testar
a projeção completa localmente; resolver SSH para instalar a API protegida e
configurar Funnel. A torre atual continua sendo a referência até validar tudo.

## Ajustes locais em 09/10/2026 — contrato e API

- Criados `CONTRATO_TORRE.md` e `torre_contract.py`: validação da estrutura mínima
  da projeção completa e publicação atômica após validação.
- API valida o mapa de equipes e o contrato da torre; gzip truncado/corrompido
  retorna 503. ETag exposto também em 304.
- Testes da API trazidos para o coletor; 36 testes passaram incluindo contrato,
  API, armazenamento, transições e sincronização do índice.
- Composição local completa, exportação do cadastro, sincronização DDS, eventos
  persistentes, instalação e integração do navegador continuam pendentes.
- Instalar também `torre_contract.py`; atualizar as cópias no DDS antes do deploy.
- Nenhum deploy, Funnel, commit ou alteração remota nesta etapa.

## Ajustes locais em 10/10/2026 — entradas da torre

- `coletor/torre_inputs.py`: sincroniza cadastro exportado e últimas 25 projeções
  DDS; valida empresa/schema/datas, mantém revisões GCS e preserva o último
  conjunto válido em falhas. TTL e estado persistem após reinício.
- `scripts/export_torre_catalog.py`: exporta pela leitura oficial do DDS,
  incluindo aliases e equipes inativas. Publicação exige `--publish`; o
  cadastro global atual foi validado apenas para ChicoEletro.
- `main.py`: sincronização opcional antes da coleta, desativada por padrão,
  sem alteração no formato/upload Rotalog. Configurações em `.env.example`.
- `snapshot()` lê somente disco e calcula dados desatualizados pelo relógio.
- Testes cobrem falhas, conjunto vazio válido, reinício, revisão inalterada,
  mudança de bucket, cache corrompido e isolamento por empresa.
- Próximo passo: composição completa dos cards e eventos persistentes usando
  essas entradas. Instalar a exportação periódica no DDS antes de habilitar
  a leitura no Orange. Nenhuma exportação real/publicação/deploy nesta etapa.

## Continuidade em 11/10/2026 — Cloud Run sob demanda e composição local

- Cloud Run `dds-admin-site`: mínimo zero no serviço/revisão e CPU throttling
  ativado. Não desligar o serviço: login, permissões, relatórios e a torre atual
  ainda dependem dele. Instâncias podem continuar ativas por tráfego/polling.
- Configuração aplicada em 10/10/2026 e verificada: revisão `00249-z7h` atende
  100%; Hosting respondeu 200 e endpoint protegido respondeu 401 sem sessão.
- `D:/programas/DDS/dds-webtools/deploy.ps1` agora usa `--min=0`,
  `--min-instances=0` e `--cpu-throttling`.
- `coletor/torre_cards.py`: regras de cards portadas do DDS sem acesso remoto.
- `coletor/torre_composer.py`: composição de cards, aliases e contadores;
  journal persistente de eventos, transições intermediárias e publicação atômica.
- `main.py`: composição opcional após cada ciclo, inclusive sem mudança Rotalog;
  falha da coleta pode recompor com indicação de Rotalog desatualizado.
- `TORRE_COMPOSE_ENABLED=false` por padrão; requer entradas habilitadas para
  ativar. Ainda não instalado no Orange nem validado contra dados reais.
- 75 testes locais passaram. Nenhum commit/push, exportação real de cadastro,
  publicação da torre ou ativação do Funnel nesta etapa.
- Próximo: exportação periódica do cadastro no DDS, validar fontes e composição
  reais, instalar no Orange, configurar chave/Funnel e integrar a interface.
