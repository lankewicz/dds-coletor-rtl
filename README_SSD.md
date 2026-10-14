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

A TUI e ferramentas que leem arquivos diretamente ainda devem receber o **caminho
físico** explicitamente; não foram redirecionadas. Logs de sistema continuam com a
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
