# Coleta única RTLWeb

Para usar no Orange Pi com dados no SSD, consulte [ORANGE_PI.md](ORANGE_PI.md).

Ferramenta independente do coletor contínuo. Não importa módulos de `coletor`,
não envia dados ao Firebase e não altera os arquivos do coletor atual.

Execute na raiz do repositório, usando o ambiente Python existente:

```powershell
.\venv\Scripts\python.exe -m coleta_unica
```

No Windows, também pode abrir `coleta_unica/iniciar.cmd` com duplo clique.

Abre uma TUI própria e consulta **cada dia de 01/01/2026 até ontem** no horário
de São Paulo. Ao terminar o período, encerra: não há agendamento nem ciclo contínuo.
Espaço pausa entre requisições; Q encerra. Uma requisição em andamento pode levar
até o timeout de rede para terminar. Execute o mesmo comando para retomar.

A TUI mostra três barras que acompanham a largura do terminal: **Completo**
(dia X de Y e dias concluídos), **Diário** (equipe X de Y e equipes concluídas)
e **Equipe** (verificar composição, consultar/reaproveitar e validar).
A barra da equipe avança por etapas, sem estimar porcentagem do download.
O visual usa azul para o total, verde para o dia e lilás para a equipe, com
previsão em destaque. As barras continuam ocupando a largura do terminal.
Defina `NO_COLOR=1` se preferir a apresentação sem cores.
Durante a leitura das tabelas, a fase e as páginas aparecem abaixo da barra diária.
O total de dias só avança depois da gravação; dias retomados também contam.

A linha de previsão mostra o tempo restante e a data/hora estimada de término
no fuso de São Paulo. A primeira estimativa é provisória, após pelo menos 10%
das equipes do primeiro dia. Depois usa a média dos últimos sete dias coletados.
Dias pulados na retomada não são usados como amostras de velocidade, e pausas
não entram no tempo ativo. A previsão fica suspensa durante a pausa e se ajusta
às mudanças no tempo de resposta e no reaproveitamento do cache.

`dados/tempo-execucao.json` mantém as sessões, o tempo ativo acumulado, o tempo
decorrido e as pausas, além das durações dos últimos dias para a previsão.
O tempo em que o programa ficou fechado não entra na soma. A TUI mostra o tempo
ativo da sessão, o total ativo de todas as sessões e as pausas acumuladas.
O histórico é atualizado durante o progresso (com intervalo mínimo de 15 segundos)
e ao encerrar, inclusive por Q ou erro. Após queda inesperada, retoma a última
medição gravada; o tempo posterior a esse registro pode não ser contabilizado.
Sessões anteriores à inclusão desse recurso não têm duração recuperável.

Para conferir apenas o primeiro dia:

```powershell
.\venv\Scripts\python.exe -m coleta_unica --fim 2026-01-01
```

Também aceita `--inicio AAAA-MM-DD`, `--fim AAAA-MM-DD`, `--saida CAMINHO`,
`--env CAMINHO`, `--intervalo 0.5` e `--sem-tui`.
Para instalar em um ambiente separado: `python -m pip install -r coleta_unica/requirements.txt`.

Usa `ROTALOG_USUARIO` e `ROTALOG_SENHA` do ambiente, de `coleta_unica/.env`,
ou do `.env` da raiz, nessa prioridade. Aceita `ROTALOG_CA_BUNDLE` e
`ROTALOG_VERIFY_TLS`, com verificação TLS habilitada por padrão.
Não selecione contrato nem veículo: a exportação inclui todos os registros
que a conta autenticada tem permissão para consultar.

## Arquivos por dia

Em `coleta_unica/dados/AAAA-MM-DD/`:

- `resumo.csv`: os 15 indicadores da figura 1, incluindo o texto e o percentual de KM final.
- `turnos.csv`: todas as páginas da tabela da figura 2, incluindo colunas ocultas,
  link do diário, nome completo da agência e nome completo/matrícula dos dois eletricistas.
- `eventos.csv`: todas as páginas da tabela da figura 3, incluindo colunas ocultas.
- `dados.json`: as três seções e os totais de paginação; recibo de conclusão do dia.
- `brutos/`: HTML da pesquisa e XML das páginas, para conferência.

CSV usa ponto e vírgula e UTF-8 com BOM. Os valores são preservados como no portal,
inclusive protocolos, vírgulas decimais e tempos negativos. Ações da tabela são
preservadas como texto. A ferramenta abre o link do veículo para consultar os
profissionais e a agência do diário; não abre os ícones de ações dos eventos.

Cada página deve conter a quantidade esperada de linhas. Só grava o recibo
depois de validar as duas tabelas e salvar os CSVs. Se falhar, tenta novamente
até três vezes, autenticando outra sessão; depois encerra com erro.
Na retomada, pula somente dias com recibo válido da versão atual e os três CSVs
presentes. Dias coletados sem os nomes completos e matrículas são consultados
novamente para complementar os dados; os arquivos anteriores permanecem até a
nova coleta terminar com sucesso. Os campos originais de nomes curtos e código
da agência continuam preservados. Matrículas são texto, mantendo zeros iniciais.
O dia interrompido é consultado novamente desde a primeira página.
Não remove linhas iguais: eventos distintos podem ter conteúdo idêntico.

A pasta de saída fica bloqueada durante a execução para impedir duas coletas
simultâneas no mesmo destino. O bloqueio é liberado pelo sistema ao encerrar.
Os arquivos exportados contêm dados operacionais e pessoais do portal; a pasta
`dados/` e o `.env` estão ignorados pelo Git.

## Reaproveitamento de profissionais

O arquivo local `dados/profissionais-cache.json` guarda as identificações já
observadas nos diários. É carregado entre execuções e também recebe os dados
dos dias completos já exportados, sem precisar consultar novamente o portal.

Uma composição é identificada pelo veículo, contrato, agência e os dois nomes
resumidos, em suas respectivas posições. Quando essa combinação já é conhecida,
reaproveita nome completo, matrícula e nome da agência. Quando muda algum desses
campos, consulta o diário. Nomes iguais em outros veículos não são associados.
Se forem observadas matrículas ou nomes completos diferentes para a mesma
combinação, ela fica ambígua e exige consulta. Nomes desconhecidos também exigem
consulta. Uma troca entre homônimos que mantenha todos os campos resumidos
idênticos não é detectável apenas pela listagem.

`Detalhes fonte` informa `diario` ou `cache`; `Detalhes diário consultado` aponta
para o diário que comprovou os nomes e matrículas. `Diário URL` continua sendo
o diário do turno atual. A TUI e o JSON mostram quantos diários foram consultados
e quantos turnos aproveitaram o cache. Exportações já completas continuam válidas.
