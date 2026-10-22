# Coleta única no Orange Pi e SSD

Requer Linux, Bash, Python 3.10 ou superior e SSD já montado em `/mnt/ssd`.
Funciona em ARM sem bibliotecas de interface gráfica. A TUI usa o terminal Linux,
inclusive via SSH; sem terminal interativo, o progresso é mostrado em texto.
Não altera nem reinicia o coletor contínuo.

## Instalação

Leve a pasta `coleta_unica` atualizada ao projeto no Orange Pi, mantendo o `.env`
que já existe no equipamento. O caminho usado nesta instalação é
`/home/orangepi/dds-coletor-rtl`. Não copie o `venv` do Windows para o Linux.

O iniciador procura `.venv-3.14`, `.venv` e `venv`, nessa ordem. Para criar um
ambiente exclusivo da coleta única, se necessário:

```bash
cd /home/orangepi/dds-coletor-rtl
python3 -m venv coleta_unica/.venv
coleta_unica/.venv/bin/python -m pip install -r coleta_unica/requirements.txt
export COLETA_UNICA_PYTHON="$PWD/coleta_unica/.venv/bin/python"
```

## Pasta do SSD

Confira a montagem e crie somente a pasta da coleta única. Os comandos abaixo
não montam nem formatam discos e não mudam a propriedade de outras pastas:

```bash
mountpoint -q /mnt/ssd && findmnt /mnt/ssd
mountpoint -q /mnt/ssd && sudo install -d -o orangepi -g orangepi -m 750 /mnt/ssd/dds-coletor-rtl/coleta-unica/dados
```

Execute o Python como `orangepi`, sem sudo. Dados, CSV, JSON, respostas brutas,
cache de profissionais, bloqueio e histórico de tempo ficam exclusivamente em:

```text
/mnt/ssd/dds-coletor-rtl/coleta-unica/dados/
```

Código, ambiente Python e `.env` permanecem no cartão.

## Executar com TUI

```bash
cd /home/orangepi/dds-coletor-rtl
bash coleta_unica/iniciar-orange.sh
```

Usa 01/01/2026 até ontem em São Paulo. Aceita os mesmos argumentos da versão
Windows, por exemplo `--fim 2026-01-01` ou `--sem-tui`. Espaço pausa e Q encerra.
O iniciador pode ser executado com `bash`, sem precisar alterar permissões.

Para outro ponto de montagem ou destino:

```bash
COLETA_UNICA_SSD_MOUNT=/mnt/ssd COLETA_UNICA_SAIDA=/mnt/ssd/minha-coleta/dados bash coleta_unica/iniciar-orange.sh
```

A saída deve estar dentro do SSD informado. O programa verifica a montagem antes
de criar a pasta e antes de gravar arquivos. Ausência, perda ou troca da montagem
interrompem a coleta; não há fallback para o cartão. Reconecte o SSD correto e
reinicie para retomar. Nenhum programa consegue garantir o salvamento de dados
que estavam em gravação durante uma desconexão física.

Para manter a coleta ao desconectar o SSH, execute dentro de uma sessão `tmux`,
se ele já estiver instalado no equipamento:

```bash
tmux new -s coleta-unica
bash coleta_unica/iniciar-orange.sh
```

Use Ctrl+B, depois D, para sair do terminal sem encerrar. Retorne com
`tmux attach -t coleta-unica`. Não há serviço ou agendamento instalado automaticamente.

## Continuar os dados coletados no Windows

Encerre a coleta no Windows com Q e confirme que terminou. Com a coleta única
também parada no Orange, copie **o conteúdo inteiro** de `coleta_unica/dados/`
para a pasta de dados no SSD, usando um destino novo/vazio para não mesclar
históricos divergentes. Não é necessário parar o coletor contínuo.

Transfira os dias, `profissionais-cache.json` e `tempo-execucao.json`, quando
existirem. Não transfira `.execucao.lock` nem arquivos `.tmp`. Preserve a origem
até conferir os CSVs e os totais no Orange. Os recibos dos dias concluídos serão
reconhecidos; o dia incompleto é refeito, aproveitando os profissionais já salvos.
O histórico de tempo também continua; o intervalo entre computadores não é somado.

## Conferência

```bash
cd /home/orangepi/dds-coletor-rtl
.venv-3.14/bin/python -m unittest discover -s coleta_unica -p 'test_*.py' -v
du -sh /mnt/ssd/dds-coletor-rtl/coleta-unica/dados
```

Se estiver usando o ambiente exclusivo, substitua `.venv-3.14/bin/python` por
`coleta_unica/.venv/bin/python`. As instruções preparam a execução; a validação
no hardware real precisa ser feita no próprio Orange Pi.
