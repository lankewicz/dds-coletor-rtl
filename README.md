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
│   ├── parser.py           # Parsing da timeline, agrupamento de turnos e detecção de OS
│   └── storage.py          # load_json/write_json nativos para .json.gz, diffing e GCS
├── dados-local/            # Armazenamento local (ignorado pelo git)
│   └── rotalog/
│       └── equipes/
│           ├── current/
│           │   └── index.json.gz              # Torre de controle unificada (~9.1 KB)
│           └── daily/
│               └── AAAA-MM-DD/
│                   ├── E3C03.json.gz          # Histórico detalhado da equipe no dia
│                   └── E3389.json.gz
├── .env.example            # Modelo de variáveis de ambiente
├── .gitignore              # Proteção de credenciais e dados locais
├── main.py                 # Daemon de coleta contínua
├── tui.py                  # Interface interativa de terminal (Curses)
├── requirements.txt        # Apenas 4 dependências essenciais
└── rotalog.service         # Unit systemd para execução contínua no Orange Pi
```

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
python main.py --firebase --interval-seconds 120
```

### Painel Interativo de Terminal (TUI)
```bash
# Modo ativo (executa a raspagem com tela gráfica no terminal)
python tui.py --firebase --interval-seconds 120

# Modo passivo (apenas visualiza o serviço systemd sem interferir no ciclo)
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
