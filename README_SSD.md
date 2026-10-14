# Armazenamento preferencial no SSD

Implementação local preparada em 14/10/2026. Não instala serviços, monta discos,
formata unidades nem muda a frota automaticamente. Não modifica as regras de turno.

## O que muda

- O cartão mantém Linux, código, ambiente Python e credenciais.
- Um SSD **preparado**, montado em `/mnt/ssd`, recebe os dados operacionais em
  `/mnt/ssd/dds-coletor-rtl/chicoeletro/dados-local`.
- `.storage.env`, local e ignorado no Git, ativa o modo `auto`. Sem essa configuração,
  o comportamento atual permanece.
- Coleta, histórico, fechamento mensal, heartbeat e API resolvem o mesmo destino.
  Os serviços podem manter o `--output-dir` atual: ele é o caminho lógico do cartão.
  Diretórios explícitos de teste diferentes desse caminho não são redirecionados.
- O heartbeat inclui `health.storage`: modo, UUID, dispositivo, montagem, diretório
  e motivo da escolha, separado do `nodeId`. Esse dado acompanha os heartbeats
  existentes; não cria uma coleção nova nem altera quem está autorizado a coletar.
- A preparação confirma a empresa, copia os arquivos e compara SHA-256. Não copia
  `.tmp` ou locks. Não aceita links simbólicos nem sobrescreve um SSD já preparado.
- SSDs preparados podem circular entre Oranges da mesma empresa. O UUID é verificado
  contra o marcador no próprio disco, não fica preso ao `nodeId`. Opcionalmente,
  `DDS_STORAGE_UUIDS=uuid-a,uuid-b` limita quais discos esta instalação aceita.

## Limites importantes

Esta etapa implementa escolha do destino e migração inicial. O **backup completo e
a restauração automática pelo Firebase discutidos ainda não estão implementados**.
O fallback usa a pasta existente no cartão; ela pode estar antiga depois da migração.
Não apagar a origem e não tratar os índices/diários já publicados como backup completo.

No retorno do SSD, se o índice do cartão for mais recente, a seleção permanece no
cartão com `card-newer:reconcile-required`. Isso evita usar automaticamente dados
antigos do SSD. Reconciliar/restaurar os dados antes da troca; não mesclar diretórios
às cegas ou sobrescrever journals/pendências. Esta rotina de reconciliação é pendente.

A escolha ocorre ao iniciar cada processo. Para inserir/trocar um SSD, parar os
processos envolvidos e reiniciá-los juntos após conferir os dados. Não há troca a
quente durante um ciclo. As gravações JSON atômicas verificam o UUID antes de escrever
e publicar; perda do SSD gera erro. O coletor reavalia na próxima inicialização
(o systemd atual já reinicia em falha). A API responde 503 se perder seu disco e
precisa ser reiniciada para selecionar o cartão. Não se garante recuperação automática
do estado completo nem funcionamento contínuo da API nesta etapa.

O TUI seleciona o armazenamento na abertura. Outras ferramentas que leem arquivos
diretamente ainda devem receber o **caminho físico**. Logs de sistema continuam com a
política existente do Linux. Um filesystem montado por UUID não elimina a necessidade
de validação da montagem e da empresa.

## Preparação no Orange

Executar somente após levar esta versão ao equipamento e validar os testes. A cópia
precisa ocorrer sem coleta, histórico, heartbeat, composição manual ou outros gravadores.
Os comandos abaixo são referências para instalação, não foram executados remotamente.

1. Conferir sem modificar o disco:

   ```bash
   cd /home/orangepi/dds-coletor-rtl
   .venv-3.14/bin/python scripts/prepare_ssd.py
   ```

2. Registrar quais serviços/timers estão ativos e parar apenas os instalados.
   Incluir `rotalog.service`, `rotalog-history.service`, `rotalog-monthly.service`,
   `fleet-agent.service`, `torre-api.service` e os timers correspondentes. Fechar
   ferramentas que gravem nos dados. Restaurar depois os timers que estavam ativos.

3. Criar **apenas a pasta da aplicação** com permissão para `orangepi`; não alterar
   a propriedade de outros arquivos do SSD. Confirmar antes que `/mnt/ssd` está montado:

   ```bash
   mountpoint -q /mnt/ssd && sudo install -d -o orangepi -g orangepi -m 750 /mnt/ssd/dds-coletor-rtl
   .venv-3.14/bin/python scripts/prepare_ssd.py --apply
   ```

   Rodar o Python como `orangepi`, sem `sudo`. Ele grava `.storage.env` somente após
   verificar a cópia e criar o marcador do disco. Se houver falha, preservar a pasta
   `.dds-migration-*` para diagnóstico; não repetir removendo dados automaticamente.

4. Reiniciar coleta e os demais serviços/timers que estavam ativos. Conferir o log
   inicial `Armazenamento`, que deve mostrar `mode: ssd`, UUID e o caminho físico.
   Conferir também o próximo heartbeat. Não reativar serviços que estavam desativados.

5. Medir o destino real:

   ```bash
   du -sh /mnt/ssd/dds-coletor-rtl/chicoeletro/dados-local
   ```

O código e as credenciais não são copiados ao SSD. A fonte `dados-local` no cartão
permanece intacta. Não há espelhamento periódico para o cartão. Planejar o backup
completo na nuvem antes de remover essa cópia ou depender da contingência.

## TUI e saúde física do SSD

O TUI resolve a configuração `.storage.env` na abertura, assim como o coletor.
Reabra o visualizador depois da atualização:

```bash
.venv-3.14/bin/python tui.py --view
```

O painel mostra SSD/cartão, pasta efetiva, espaço livre/capacidade, modelo, dispositivo
e UUID. A capacidade refere-se ao volume de dados, não ao cartão que contém o sistema.
Na tela principal, o resumo ocupa três linhas: coleta/sincronização, saúde/SMART
e alertas. O painel de eventos começa na linha 10, deixando a maior parte da altura
para as equipes. Pressione `d` para consultar os detalhes de execução, modelo, UUID,
pastas e contadores do SSD. `d` ou Esc retorna aos eventos; setas e PgUp/PgDn rolam
os detalhes em terminais menores. Falhas, pendências e alertas continuam no resumo.
Heartbeat e último envio são exibidos em horário de Brasília. Heartbeats antigos
recebem alerta; reabrir o TUI após trocar o armazenamento. O TUI não troca de disco
durante a execução e avisa se o SSD selecionado desaparecer.

O agente de frota consulta SMART junto da atualização do heartbeat local (normalmente
a cada minuto); o TUI somente lê essa cópia, sem novas consultas ao disco/Firebase.
Depois de atualizar o agente, é possível solicitar a próxima amostra com
`sudo systemctl start fleet-agent.service` se ele estiver inativo.

SMART exige `smartctl` (pacote `smartmontools`), suporte do adaptador USB e permissão
do usuário do agente para consultar o disco físico. Não se alteram permissões;
sudo só é usado com a opção explícita descrita abaixo. Não se executam testes
ou comandos de escrita no SSD.
Sem essas condições, o painel informa `INDISPONIVEL` e a causa, mantendo a coleta.

Quando disponíveis, são mostrados status SMART, temperatura do SSD (separada da CPU),
horas ligadas, ciclos de energia, setores realocados/pendentes/incorrigíveis e erros
CRC. Desgaste percentual é exibido para NVMe que forneça esse campo; não se inventa
estimativa de vida útil para atributos ATA específicos do fabricante. Erros históricos
geram atenção; falha SMART gera alerta crítico. SMART `OK` não garante ausência de falhas.

Para diagnóstico inicial, confira `command -v smartctl` e execute uma leitura manual
com `sudo smartctl -j -a /dev/sda`, substituindo pelo **disco físico atual**, não pela
partição `/dev/sda1`. Essa consulta como root não concede acesso ao agente `orangepi`;
se funcionar somente com sudo, falta configurar acesso restrito para o serviço.
Alguns adaptadores precisam de `-d sat`; nesse caso, após confirmar o funcionamento,
adicione `DDS_SMART_DEVICE_TYPE=sat` ao `.env` do agente. A identificação do disco é
derivada da montagem atual e não fixa `/dev/sda`, permitindo intercambiar SSDs.

Referência: [manual oficial do smartctl](https://github.com/smartmontools/smartmontools/blob/main/src/smartctl.8.in).

### Consulta SMART com permissão restrita

Se a consulta funcionar somente como root, não incluir o usuário no grupo `disk`.
É possível habilitar `DDS_SMART_USE_SUDO=true` no `.env`. O agente então executa
`sudo -n smartctl ...`, que nunca solicita senha. Sem a autorização exata, SMART
permanece indisponível e o coletor continua funcionando.

Conferir `command -v smartctl` e o disco físico atual. Para `/usr/sbin/smartctl`
e `/dev/sda`, a regra em `/etc/sudoers.d/90-dds-smart`, propriedade de root,
permissão 0440, deve conter apenas:

```sudoers
orangepi ALL=(root) NOPASSWD: /usr/sbin/smartctl -j -a /dev/sda
```

Validar o arquivo antes de instalá-lo com `sudo visudo -cf <arquivo>`.
Validar a leitura como orangepi com `sudo -n /usr/sbin/smartctl -j -a /dev/sda`.
Não usar curingas nos argumentos nem liberar todo o executável. Se o dispositivo
mudar para `/dev/sdb`, ou se for necessário `-d sat`, atualizar a regra para o novo
comando exato. Esta regra restringe o comando/dispositivo; o UUID da seleção de
armazenamento continua sendo validado pelo agente antes da consulta.

Após atualizar o código/configuração, iniciar `fleet-agent.service` se estiver
inativo e aguardar a leitura do heartbeat pelo TUI (até dois minutos).

Referência: [manual do sudoers](https://www.sudo.ws/docs/man/1.9.14/sudoers.man.pdf).
