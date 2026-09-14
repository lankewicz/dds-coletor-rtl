# Revisão inicial — 14/09/2026

Escopo: leitura do fluxo principal, armazenamento, cliente HTTP e interface de terminal. Validação local com dados sintéticos; sem coleta no portal ou gravação no Firebase.

## Correções implementadas

- A compactação preserva os contadores e a data do último serviço quando recebe um índice já compacto. Isso evita zerar os dados de equipes que não aparecem na coleta seguinte.
- A decisão de salvar o histórico local compara o documento diário anterior completo com o novo. O índice resumido não contém os detalhes necessários para essa comparação.
- A comparação detecta correções no início do turno, no conteúdo dos intervalos e em serviços já concluídos, mesmo quando as quantidades permanecem iguais.
- A comparação do formato antigo voltou a executar; estava depois de um `return` em outra função.
- A leitura local trata GZIP truncado e conteúdo com codificação inválida como cache indisponível.
- Índices e históricos diários corrompidos são recuperados do Firebase após validação. A cópia defeituosa fica preservada; sem uma fonte remota válida, o ciclo falha sem sobrescrever o histórico.
- Os históricos de equipe usam uma fila local persistente por data/equipe. Abertura, conclusão, fechamento e correções posteriores entram na fila antes do upload; falhas permanecem para ciclos e reinicializações seguintes, sempre usando o arquivo diário local mais recente.
- Índice, históricos de equipe, logs e quilometragem usam uma única raiz GCS derivada e validada pela empresa. Configurações remotas cruzadas são bloqueadas, as filas são separadas por empresa e o coletor recusa reutilizar dados locais identificados como pertencentes a outra empresa.

Validação: `python -m unittest discover -s tests -v` (oito testes).

## Próximas prioridades

1. **Fechamento mensal recuperável.** `last_monthly_sweep_day` existe apenas em memória. Reiniciar no dia 1 repete a varredura; ficar desligado nesse dia impede a execução automática. Persistir o mês concluído e distinguir coleta local de sincronização confirmada.
2. **Autenticação e TLS.** O cliente desativa a validação de certificados e oculta avisos. Validar a cadeia de certificados do portal e adotar configuração explícita antes de alterar a operação em produção.
3. **Monitoramento e consumo local.** O visualizador relê todo o arquivo de logs e mantém todas as entradas temporariamente a cada alteração. Adotar leitura incremental e rotação. Também distinguir serviço ativo de log antigo.
4. **Estimativas de custo.** O README promete gratuidade, mas o código também grava índice e auditoria a cada ciclo. Recalcular com todas as operações e condições reais do bucket; a revisão não verificou preços ou cotas externas.

O Python local emitiu um aviso de compatibilidade entre dependências do `requests`. Os testes passaram, mas convém validar a instalação em ambiente virtual isolado antes da implantação.
