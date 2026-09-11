# DDS Coletor ROTALOG (`dds-coletor-rtl`)

Agente autônomo e enxuto de borda (*Edge Daemon*) para coleta contínua de dados operacionais do **ROTALOG Copel (RTLWeb)** e sincronização direta no **Firebase Storage (GCS)**.

Projetado especificamente para rodar em hardware de recursos modestos como o **Orange Pi** (ou Raspberry Pi / mini PC em rede local), sem carregar dependências pesadas de aplicações web ou bancos de dados.

---

## 1. Arquitetura da Solução

```
+-------------------------------------------------------------+
|                      REDE LOCAL (EDGE)                      |
|                                                             |
|  [Portal Copel RTLWeb]                                      |
|          | (HTTP/Sessão Autenticada)                        |
|          v                                                  |
|  [Orange Pi - dds-coletor-rtl]                              |
|          | (Gzip / Upload atômico)                          |
+----------|--------------------------------------------------+
           |
           v
+-------------------------------------------------------------+
|                        NUVEM (GCP)                          |
|                                                             |
|  [Firebase Storage - GCS Bucket]                            |
|    └── dados/{empresa}/rotalog/equipes/current/index.json.gz|
|          ^                                                  |
|          | (Leitura passiva HTTP)                           |
|  [DDS Monitor de Turnos (Cloud Run)]                        |
|                                                             |
+-------------------------------------------------------------+
```

---

## 2. Estrutura do Projeto

```text
dds-coletor-rtl/
├── coletor/
│   ├── __init__.py
│   ├── client.py           # Autenticação e sessão no RTLWeb (/paginas/j_security_check)
│   ├── parser.py           # Parsing da timeline PrimeFaces e extração de turnos/SS
│   └── storage.py          # Gravação local, diffing incremental e upload no Firebase Storage
├── dados-local/            # Armazenamento local (ignorado pelo git)
├── .env.example            # Modelo de configuração
├── .gitignore              # Proteção de credenciais e dados locais
├── main.py                 # Daemon de coleta contínua
├── tui.py                  # Interface interativa de terminal (Curses)
├── requirements.txt        # Apenas 4 dependências essenciais
└── rotalog.service         # Unit systemd para execução contínua no Orange Pi
```

---

## 3. Dependências Mínimas

O projeto requer apenas **Python 3.9+** e 4 pacotes:
* `requests`
* `beautifulsoup4`
* `google-cloud-storage`
* `python-dotenv`

*(Zero Django, Zero Pandas, Zero dependências de servidor web. O ambiente virtual consome menos de 35 MB).*

---

## 4. Configuração do Ambiente (.env)

Crie o arquivo `.env` baseado no `.env.example`:

```bash
cp .env.example .env
```

Preencha os valores:
```env
ROTALOG_USUARIO=seu_usuario_copel
ROTALOG_SENHA=sua_senha_copel
DDS_BUCKET_NAME=dds-treinamentos.firebasestorage.app
ROTALOG_GCS_CACHE_BLOB=dados/chicoeletro/rotalog/equipes/current/index.json.gz
DDS_EMPRESA_PADRAO=ChicoEletro
DDS_TIMEZONE=America/Sao_Paulo
ROTALOG_UPLOAD_FIREBASE=true
GOOGLE_APPLICATION_CREDENTIALS=serviceAccountKey.json
```

Coloque a chave da conta de serviço do Google Cloud (`serviceAccountKey.json`) na raiz do diretório.

---

## 5. Como Executar

### Teste Rápido (Executa uma única vez)
```bash
python main.py --once
```

### Execução Contínua (Daemon no terminal)
```bash
python main.py --firebase --interval-seconds 120
```

### Painel Interativo de Terminal (TUI)
```bash
# Modo ativo (executa a raspagem com tela gráfica no terminal)
python tui.py --firebase --interval-seconds 120

# Modo passivo (visualiza o serviço systemd sem interferir no ciclo)
python tui.py --view
```

---

## 6. Instalação e Implantação no Orange Pi

Siga este passo a passo para configurar o serviço autônomo no Orange Pi:

### Passo 1: Parar o serviço antigo (se estiver rodando)
```bash
sudo systemctl stop rotalog
```

### Passo 2: Criar a pasta e o ambiente virtual limpo
```bash
mkdir -p /home/orangepi/dds-coletor-rtl
cd /home/orangepi/dds-coletor-rtl

# Copie os arquivos do projeto para cá (via scp, git ou rsync)

# Criação do virtualenv isolado
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Passo 3: Configurar credenciais
```bash
cp .env.example .env
nano .env   # insira seu usuario e senha da Copel
# Copie o arquivo serviceAccountKey.json para /home/orangepi/dds-coletor-rtl/
```

### Passo 4: Testar a execução manual
```bash
.venv/bin/python main.py --once --firebase
```
Se o terminal exibir `{"status": "success", ...}`, a coleta e o envio para a nuvem funcionaram perfeitamente!

### Passo 5: Instalar e iniciar o serviço systemd
```bash
sudo cp rotalog.service /etc/systemd/system/rotalog.service
sudo systemctl daemon-reload
sudo systemctl enable rotalog
sudo systemctl start rotalog
```

### Passo 6: Monitorar o serviço
```bash
# Acompanhar logs em tempo real
journalctl -u rotalog -f

# Ou abrir o painel gráfico interativo via SSH
cd /home/orangepi/dds-coletor-rtl
.venv/bin/python tui.py --view
```
