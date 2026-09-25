# ROTALOG Coletor v2

Versão experimental e isolada para estudar somente:

1. autenticação e raspagem do ROTALOG;
2. extração dos eventos da timeline;
3. enriquecimento determinístico dos dados do Tempo Real;
4. salvamento local atômico para auditoria e testes.

Não possui Firebase, GCS, TUI, frota, relatórios ou serviços do sistema.

## Configuração

Na raiz do projeto, o arquivo `.env` já pode fornecer:

```dotenv
ROTALOG_USUARIO=usuario
ROTALOG_SENHA=senha
DDS_TIMEZONE=America/Sao_Paulo
```

Também é possível criar `v2/.env`. Variáveis do ambiente têm prioridade, seguidas por
`v2/.env` e pelo `.env` da raiz.

## Execução

```powershell
python v2/app.py --once
python v2/app.py --once --output-dir v2/dados-local
python v2/app.py --interval-seconds 180
```

Saídas:

```text
v2/dados-local/rotalog/
├── raw/AAAA-MM-DD/HHMMSS.html.gz
├── snapshots/AAAA-MM-DD/HHMMSS.json.gz
├── current/snapshot.json.gz
├── control-tower/index.json.gz
└── teams/
    ├── current/E3XXX.json.gz
    └── snapshots/AAAA-MM-DD/E3XXX/HHMMSS.json.gz
```

O HTML bruto é preservado para que mudanças futuras no parser possam ser testadas
contra coletas reais, sem repetir acessos ao portal.

O snapshot enriquecido separa, por equipe, marcadores de turno, intervalos,
serviços concluídos, atividade atual, fila pendente e eventos ainda não classificados.
Nenhum dado ausente é inventado: protocolo não reconhecido permanece `null`, e a
origem de cada serviço é registrada como `TIMELINE`.

Serviços concluídos ou em andamento sem protocolo são consultados individualmente pelo
AJAX do PrimeFaces. O terminal informa equipe, evento, conteúdo original, protocolo
encontrado, rejeições de validação e falhas. Quando confirmado pelo popup, a origem
passa a ser `AJAX_POPUP`.

Os detalhes confirmados são mantidos em `rotalog/cache/ajax-details.json.gz`, usando
equipe, início, fim e conteúdo original como identidade estável. O cache sobrevive ao
encerramento do processo; executar `--once` novamente não repete consultas já resolvidas.

Depois do enriquecimento, o snapshot anterior é comparado com o novo. O terminal agrupa
por equipe apenas mudanças operacionais, como serviço novo, deslocamento, execução,
conclusão, conexão e regional. Horários técnicos de coleta e reaplicações do cache não
geram alterações.

Cada código válido recebe um documento próprio contendo seu resumo enriquecido e seus
eventos. `E????` é a identidade estável do veículo e a chave preferencial. Códigos das
cinco regionais (`CA`, `LO`, `MA`, `PG`, `CB`) são mantidos em `regionalCode` e podem
mudar quando o veículo troca de regional. Se o portal mostrar apenas `veiculo?-CA078`,
o arquivo `CA078` é provisório e recebe `identitySource=REGIONAL_CODE_FALLBACK`; ele não
é considerado uma identidade estável até ser associado a um código `E????`.

O snapshot completo e a torre de controle são atualizados em toda coleta. Arquivos
individuais de equipe são gravados somente quando o turno abre, um serviço é concluído
ou o turno fecha. O documento registra o motivo em `writeReasons`.

A torre expõe `shiftServices` no topo, com os números gerais, e em cada equipe, com
`completed` e `active`. A fila aparece de forma compacta apenas como
`queue.emergency` e `queue.commercial`, tanto no resumo geral quanto por equipe. O
resumo geral soma a quantidade de serviços distribuídos às equipes em campo.

O arquivo individual é acumulativo: serviços concluídos anteriormente são preservados,
novos protocolos são acrescentados e campos vazios podem ser enriquecidos depois. A
fila pendente continua sendo apenas uma fotografia atual, para não acumular projeções
voláteis da interface. `writeHistory` registra cada atualização do documento.

O histórico consolidado da fila fica em `queue-history/AAAA-MM-DD.json.gz`. Ele cria
intervalos somente quando a situação muda: `BUSY`, `WAITING_WITH_QUEUE`,
`IDLE_NO_QUEUE`, `BREAK` ou `OFFLINE`. Cada intervalo preserva primeira e última
observação, protocolo atual e composição da fila. Nos eventos de gravação, o histórico
da equipe também é incorporado ao seu documento individual.

## Testes

```powershell
python -m unittest discover -s v2/tests -v
```
