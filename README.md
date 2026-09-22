# DDS Coletor ROTALOG (`dds-coletor-rtl`)

Agente autônomo e enxuto de borda (*Edge Daemon*) para coleta contínua de dados operacionais do **ROTALOG Copel (RTLWeb)** e sincronização direta no **Firebase Storage (GCS)**.

Projetado especificamente para rodar em hardware de baixo consumo como o **Orange Pi** (ou Raspberry Pi / mini PC em rede local), sem carregar dependências pesadas de servidores web ou bancos de dados relacionais.

---

## 1. Arquitetura da Solução

```text
+-------------------------------------------------------------+
|                      REDE LOCAL (EDGE)                      |
|                                                             |
|  [Portal Copel RTLWeb]                                      |
|          | (HTTP / Sessão Autenticada com Cookies JSF)      |
|          v                                                  |
|  [Orange Pi - dds-coletor-rtl]                              |
|          |                                                  |
|          |-- (1. Gravação local contínua em daily/*.json.gz)|
|          |-- (2. Upload inteligente apenas no fechamento/OS)|
|          v                                                  |
+-------------------------------------------------------------+
           |
           v
+-------------------------------------------------------------+
|                        NUVEM (GCP)                          |
|                                                             |
|  [Firebase Storage - GCS Bucket]                            |
|    ├── dados/{empresa}/rotalog/equipes/current/index.json.gz| (Torre de Controle: ~9 KB)
|    └── dados/{empresa}/rotalog/equipes/daily/AAAA-MM-DD/    | (Histórico diário: ~1.5 KB/eq)
|          ^                                                  |
|          | (Leitura passiva HTTP em gzip)                   |
|  [DDS Monitor de Turnos / Dashboards / Mapas]               |
|                                                             |
+-------------------------------------------------------------+
```

---

## 2. Destaques da Arquitetura

Identidade de equipe: quando o ROTALOG cria grupos adicionais pelo AUTOTRACK, sufixos numéricos como
`E3T01(2)` e `E3T01(3)` são consolidados em `E3T01`. Turno, serviços, intervalos, quilometragem,
arquivos locais, índice e caminhos na nuvem usam sempre a equipe base.
As regras de identidade, validação, migração de snapshots e resolução por tablet ou integrantes ficam
centralizadas em `coletor/equipes.py`.

1. **Compactação Nativa GZIP (`.json.gz`)**:
   - Todo o tráfego de rede e armazenamento em disco utiliza `.json.gz`.
   - O `index.json.gz` consolidado (todas as 100+ equipes) caiu de **785 KB para apenas 9.1 KB** (**~99% de economia**).
   - Cada arquivo de equipe compactado pesa entre **1.2 KB e 2.0 KB**.

2. **Torre de Controle Enxuta (`current/index.json.gz`)**:
   - Voltado para o tempo real: contém o status de conexão, turno, intervalos e **apenas o serviço atual** (`ordensServico.atual`) com protocolo, categoria e coordenadas geográficas (lat/long).
   - Se o turno estiver `FECHADO`, `ordensServico.atual` fica `null`.
   - Contadores de produtividade: `totalConcluidos` e `historicoUpdatedAt`.

3. **Política de Gravação Inteligente (Custo Zero no Firebase)**:
   - **Disco Local (`dados-local`)**: Grava todas as etapas (deslocamento, execução, intervalo, etc.) com fidelidade cirúrgica.
   - **Firebase Storage**:
     - `index.json.gz`: Enviado apenas quando os dados operacionais mudam. Horário de coleta e versão interna não provocam envio.
     - `daily/<data>/<equipe>.json.gz`: Enviado na **abertura do turno**, quando a equipe **conclui um serviço**, quando **fecha o turno** e quando um serviço concluído recebe uma correção posterior.
   - **Economia**: O volume mensal para 115 equipes fica em **~40.000 gravações**, ficando **100% coberto pela cota gratuita de 50.000 gravações do Google Cloud Storage (Custo: R$ 0,00)**.

---

## 3. Estrutura de Arquivos

A confirmação do último índice enviado fica em `current/firebase-sync.json`, vinculada ao bucket e ao caminho remoto. Sem confirmação local, o primeiro ciclo envia o índice; ciclos sem mudanças dispensam o upload, inclusive após reiniciar. Falhas são tentadas novamente nos ciclos seguintes com os dados mais recentes. O índice local continua sendo atualizado em cada coleta. No Firebase, `updatedAtIso` indica a última publicação, não a última coleta. Os logs de auditoria remotos continuam seguindo a política anterior.

Se o índice ou o histórico diário de uma equipe estiver corrompido, o coletor tenta recuperar a cópia correspondente do Firebase. A cópia diária só é aceita quando equipe, data e estrutura são compatíveis. Depois da validação, o arquivo defeituoso recebe o sufixo `.corrupt-<identificador>` e permanece ao lado do arquivo reconstruído para diagnóstico. Se não existir uma cópia remota válida, o ciclo registra erro e preserva o arquivo original; ele não cria um histórico vazio sobre dados corrompidos. Arquivo ausente em um dia novo continua sendo tratado como início normal, sem recuperação remota.

Os uploads dos históricos de equipe passam pela fila persistente `rotalog/sync/<empresa>/pending-daily.json.gz`. Uma pendência é gravada antes da tentativa de envio e removida apenas depois da confirmação do Firebase. Falhas mantêm o número de tentativas, o último erro e o horário da tentativa. O ciclo seguinte retoma a fila, inclusive após reiniciar, e envia a versão local mais recente de cada combinação de data e equipe. Eventos repetidos para a mesma equipe no mesmo dia são consolidados em um único upload pendente. Intervalos continuam disponíveis na torre de controle e não provocam upload do histórico diário.

Todos os destinos remotos são derivados de `ROTALOG_GCS_ROOT_PREFIX`, substituindo `{empresa}` pela chave normalizada de `--empresa`. Índice, equipes, logs e quilometragem permanecem sob essa mesma raiz. A configuração antiga `ROTALOG_GCS_CACHE_BLOB` continua aceita quando aponta para a empresa selecionada; configurações divergentes são bloqueadas antes de qualquer upload. Para executar duas empresas no mesmo equipamento, informe um `--output-dir` diferente para cada uma. O coletor detecta índices e arquivos diários identificados como pertencentes a outra empresa e interrompe o ciclo sem misturar os dados.

```text
dds-coletor-rtl/
├── coletor/
│   ├── __init__.py
│   ├── client.py           # Sessão autenticada no RTLWeb (/paginas/j_security_check)
│   ├── historico.py        # Raspagem do histórico diário de equipes e eventos (Zero Pandas)
│   ├── logs.py             # Rotação diária com compressão gzip e leitor incremental JsonlTailReader
│   ├── parser.py           # Parsing da timeline, agrupamento de turnos e detecção de OS
│   └── storage.py          # load_json/write_json nativos para .json.gz, diffing e GCS
├── dados-local/            # Armazenamento local (ignorado pelo git)
│   └── rotalog/
│       ├── equipes/
│       │   ├── current/
│       │   │   └── index.json.gz              # Torre de controle unificada (~9.1 KB)
│       │   └── daily/
│       │       └── AAAA-MM-DD/
│       │           ├── E3C03.json.gz          # Histórico detalhado da equipe no dia
│       │           └── E3389.json.gz
│       └── logs/
│           ├── execucoes.jsonl                # Log ativo do dia corrente
│           └── execucoes-AAAA-MM-DD.jsonl.gz  # Logs anteriores arquivados em GZIP
├── .env.example            # Modelo de variáveis de ambiente
├── .gitignore              # Proteção de credenciais e dados locais
├── main.py                 # Daemon de coleta contínua
├── tui.py                  # Interface interativa de terminal (Curses) com leitura incremental
├── requirements.txt        # Apenas 4 dependências essenciais
└── rotalog.service         # Unit systemd para execução contínua no Orange Pi
```

---

## Atualização remota por versão Git

O agente atualiza o heartbeat e a saúde no arquivo local a cada 120 segundos. No
Firebase, publica a cada 30 minutos entre 07h e 20h e a cada 2 horas fora desse
horário. Cada heartbeat inclui saúde local: temperatura, espaço em disco,
memória disponível, carga, uptime, necessidade de reboot e o estado recente do
coletor. O recibo local `current/firebase-sync.json`, gravado somente após o upload
confirmado do índice, é enviado como informação operacional (`lastIndexUploadAt`),
mas não substitui o heartbeat. A verificação local não consulta o Firebase.
Configure `FLEET_DATA_DIR` se o coletor usar um `--output-dir` diferente de
`dados-local`. Falhas de upload e ciclos sem mudanças não renovam o recibo.
Resultados de deploy são imediatos.
No Windows, a descoberta consulta também o índice remoto identificado pelo publicador,
com tolerância de 45 minutos no pico e 2h30 fora dele. Isso indica comunicação
recente, não garante saúde do coletor. As consultas de comandos ainda usam Firebase.
No computador Windows, a CLI conta os equipamentos ativos, publica a solicitação de
deploy e acompanha o resultado.

```powershell
# Envia a branch atual ao Git e instala o commit enviado nos Orange Pis ativos
python fleet --update

# Instala exatamente uma versão que já existe no Git remoto
python fleet --version 8f93d814e5

# Apenas consulta equipamentos e versões ativas
python fleet --status
```

`--update` não cria commits automaticamente e recusa um repositório com alterações
locais. Isso impede que arquivos ainda não revisados sejam enviados por engano. As
duas formas resolvem o hash curto para o SHA completo e só aceitam commits disponíveis
em uma branch do remoto configurado.

Para limitar a operação e controlar a espera:

```powershell
python fleet --version 8f93d814e5 --node orange-01
python fleet --update --timeout 1200
python fleet --update --no-wait
```

### Instalação inicial no Orange Pi

Adicione ao `.env` de cada equipamento uma identidade exclusiva:

```dotenv
FLEET_NODE_ID=orange-01
```

Instale as unidades fornecidas e a regra restrita de `sudo`:

```bash
sudo install -m 0644 fleet-agent.service /etc/systemd/system/fleet-agent.service
sudo install -m 0644 fleet-agent.timer /etc/systemd/system/fleet-agent.timer
sudo install -m 0440 fleet-agent.sudoers /etc/sudoers.d/dds-fleet-agent
sudo visudo -cf /etc/sudoers.d/dds-fleet-agent
sudo systemctl daemon-reload
sudo systemctl enable --now fleet-agent.timer
```

O agente prepara o commit em uma worktree temporária, compila o Python, executa os
testes disponíveis e instala dependências antes de interromper o coletor. A troca usa
um checkout destacado do SHA exato. Se o serviço não permanecer ativo, o agente volta
automaticamente ao commit anterior.

O usuário `orangepi` recebe permissão apenas para iniciar, parar, reiniciar e consultar
`rotalog.service`; o agente não aceita comandos de shell vindos do Firebase. Para uma
separação completa, use credenciais IAM distintas: o Windows cria comandos; o Orange
apenas lê comandos, publica heartbeat e grava resultados.

---

## 4. Dependências Mínimas

O projeto requer apenas **Python 3.9+** e 4 pacotes essenciais:
* `requests`
* `beautifulsoup4`
* `google-cloud-storage`
* `python-dotenv`

*(Zero Django, Zero Pandas, Zero dependências de servidor web pesado. O ambiente virtual consome menos de 35 MB).*

---

## 5. Configuração do Ambiente (.env)

Crie o arquivo `.env` baseado no `.env.example`:

```bash
cp .env.example .env
```

Preencha os valores:
```env
ROTALOG_USUARIO=seu_usuario_copel
ROTALOG_SENHA=sua_senha_copel
DDS_BUCKET_NAME=dds-treinamentos.firebasestorage.app
ROTALOG_GCS_ROOT_PREFIX=dados/{empresa}/rotalog
DDS_EMPRESA_PADRAO=ChicoEletro
DDS_TIMEZONE=America/Sao_Paulo
ROTALOG_UPLOAD_FIREBASE=true
GOOGLE_APPLICATION_CREDENTIALS=serviceAccountKey.json
```

Coloque o arquivo de chave da conta de serviço do Google Cloud (`serviceAccountKey.json`) na raiz do diretório.

---

## 6. Como Executar

### Teste Rápido (Executa um único ciclo)
```bash
python main.py --once
```

### Execução Contínua no Terminal
```bash
# Execução com grade horária adaptativa (3 min no pico 07h-20h / 10 min no noturno 20h-07h):
python main.py --firebase

# Ou forçando um intervalo fixo em segundos (ex: 120 segundos):
python main.py --firebase --interval-seconds 120
```

### Painel Interativo de Terminal (TUI)
```bash
# Modo ativo (executa a raspagem com tela gráfica no terminal e grade adaptativa):
python tui.py --firebase

# Modo passivo (apenas visualiza o serviço systemd sem interferir no ciclo):
python tui.py --view
```
### Coleta de Quilometragem Diária e Mensal (Arquivo Único / Custo Mínimo)

Em vez de atualizar cada equipe individualmente (o que geraria centenas de acessos ao Firebase Storage), o sistema consolida todos os serviços e quilometragens em um **ÚNICO arquivo diário** compactado em GZIP filtrado pelos contratos monitorados (`4600026988` e `4600025149`):

* **Estrutura do Arquivo Diário**:
  * `dados/{empresa}/rotalog/quilometragem/diario/AAAA-MM-DD.json.gz` (~9 KB)
  * Indexado por **`protocolos`**: `protocolo -> { "equipe": "E3K95", "kmInformado": 14.0, "kmAutorizadoFinal": 14.0 }`.
  * Indexado por **`totaisPorEquipe`**: `equipe -> { "kmInformado": 247.0, "kmAutorizadoFinal": 204.24, "contrato": "4600026988" }`.
  * **Consumo no Firebase**: Apenas **1 gravação por dia** (30 gravações no mês inteiro!).

```bash
# 1. Coleta diária (ontem D-1 por padrão) gerando o arquivo único do dia:
python main.py --historico --firebase

# 2. Coleta de uma data específica:
python main.py --historico 2026-09-12 --firebase
```

### Fechamento Mensal e Varredura do Mês Anterior (Notas de Cobrança)

Ao longo do mês, a fiscalização da Copel homologa pareceres de glosas. No **1º dia de cada mês**, o coletor executa automaticamente uma varredura do mês anterior e consolida um **ÚNICO arquivo mensal de fechamento**:

* `dados/{empresa}/rotalog/quilometragem/mensal/AAAA-MM.json.gz`
* Contém todos os protocolos homologados e a medição final consolidada por equipe para geração das notas de cobrança.

```bash
# Execução manual sob demanda:
python main.py --mes-anterior --firebase
python main.py --mes 2026-08 --firebase
```

### 6. Relatórios Diário e Mensal (Fontes Distintas e Paginação Completa)

O coletor possui duas fontes oficiais distintas no portal RTLWeb da Copel, cada uma com paginação PrimeFaces em múltiplas páginas tratada automaticamente:

#### Relatório Diário (`/paginas/listagemEventos`)
* **Fonte**: Tela **Listagem de Eventos** (`https://www.copel.com/rtlweb/paginas/listagemEventos`).
* **Conteúdo**: Detalhamento operacional e auditoria de cada serviço executado no dia (protocolo, equipe, horários de início/retorno do deslocamento, início/fim da execução, KM informado com limitador e KM autorizado final).
* **Paginação**: Varre todas as páginas da tabela PrimeFaces (`form:tbListagemEventos`) até obter 100% dos eventos.
* **Saída local**: 
  - `dados-local/rotalog/quilometragem/diario/AAAA-MM-DD.json.gz` (dados estruturados)
  - `dados-local/rotalog/quilometragem/diario/AAAA-MM-DD-relatorio.html` (relatório imprimível / PDF)

```bash
# Execução diária manual para uma data específica:
python main.py --historico 2026-09-14 --no-firebase
```

#### Relatório Mensal (`/paginas/equipes`)
* **Fonte**: Tela **Equipes** (`https://www.copel.com/rtlweb/paginas/equipes`).
* **Conteúdo**: Fechamento oficial e homologado das equipes para faturamento e notas de cobrança. Apresenta por contrato e equipe: serviços executados, KM informada, glosada crítica, glosada por parecer, autorizada inicial, recuperados por parecer, autorizada final, aguardando justificativa, em análise, alertas e diferença percentual.
* **Paginação**: Consulta o período do mês e navega por todas as páginas PrimeFaces (`form:tbEquipes`) via AJAX, consolidando todos os registros de fechamento.
* **Saída local**:
  - `dados-local/rotalog/quilometragem/mensal/AAAA-MM.json.gz`
  - `dados-local/rotalog/quilometragem/mensal/AAAA-MM-relatorio.html`

```bash
# Execução mensal manual:
python main.py --mes 08/2026 --no-firebase
python main.py --mes 2026-08 --firebase
python main.py --mes-anterior --firebase
```

Abra o arquivo HTML gerado no navegador. Ambos os relatórios contam com visual limpo, subtotais por contrato (`4600026988` e `4600025149`), total geral e botão **Imprimir / Salvar PDF**.

---

## 7. Instalação e Implantação no Orange Pi

### Passo 1: Parar o serviço antigo
```bash
sudo systemctl stop rotalog
```

### Passo 2: Clonar ou atualizar o projeto
```bash
cd /home/orangepi/dds-coletor-rtl
git pull
```

### Passo 3: Ativar o virtualenv e atualizar dependências
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Passo 4: Testar a execução manual (1 ciclo)
```bash
.venv/bin/python main.py --once --firebase
```
Se o terminal exibir `{"status": "success", ...}`, a coleta e o envio funcionaram perfeitamente!

### Passo 5: Atualizar e reiniciar o serviço systemd
```bash
sudo cp rotalog.service /etc/systemd/system/rotalog.service
sudo systemctl daemon-reload
sudo systemctl enable rotalog
sudo systemctl restart rotalog
```

### Passo 6: Monitorar o serviço
```bash
# Acompanhar logs em tempo real
journalctl -u rotalog -f

# Ou abrir o painel gráfico interativo via SSH
cd /home/orangepi/dds-coletor-rtl
.venv/bin/python tui.py --view
```
