# Assistente Virtual da PROGEP — Custos

**Instituto Federal de Sergipe · Pró-Reitoria de Gestão de Pessoas**
Documento de custos · outubro de 2026 · DNX Brasil

Este documento explica quanto o assistente custa, de onde vem cada valor e
como acompanhar o gasto real pela aba **"Custos"** do painel. A apresentação
geral do assistente está em
[visao-geral-assistente-progep.md](visao-geral-assistente-progep.md).

---

## 1. Resumo

- **O assistente custou US$ 8,17 em setembro de 2026**, fase de testes, com 98
  perguntas. Desse total, US$ 5,64 foram infraestrutura fixa, quase toda de um
  registro de auditoria duplicado que já foi desligado (seção 6).
- **Hoje a parte fixa fica em cerca de US$ 2 a 3 por mês.** O resto acompanha o
  uso: de US$ 0,03 a 0,05 por pergunta.
- **Com 1.000 perguntas por mês, o custo previsto fica entre US$ 35 e 60**,
  incluindo a tarifa da Meta acima da cota grátis.
- **O gasto real pode ser acompanhado a qualquer momento** na aba "Custos" do
  painel, atualizada poucos minutos depois de cada pergunta.

Todos os valores estão em **dólar americano**, sem impostos. A AWS acrescenta
na fatura cerca de 13% de impostos nesta conta.

---

## 2. Onde acompanhar: a aba "Custos"

**Endereço:** https://d2ush4hjiv6t7m.cloudfront.net → aba **"Custos"**

**É preciso login.** O painel é restrito às pessoas autorizadas pela PROGEP, com
usuário e senha. Para ganhar acesso, peça à **DNX** informando o nome e o e-mail
institucional de quem vai acessar. A pessoa recebe por e-mail uma senha
temporária e define a própria senha no primeiro login. O mesmo acesso abre as
abas do acervo (envio e consulta de documentos).

### O que a aba mostra

| Parte da tela | O que é |
|---|---|
| Aviso no topo | Lembra que os valores são **só do assistente da PROGEP** |
| **Gasto de hoje** | Total do dia, com o número de perguntas e a hora da última atualização |
| Cota grátis da Meta | Quantas das 1.000 mensagens grátis do mês já foram usadas (seção 4) |
| Períodos | Hoje, 7 dias, 30 dias, este mês e mês anterior |
| Totais do período | Gasto total, perguntas e custo do assistente por pergunta |
| Projeção para o mês | Gasto do mês até hoje + a média diária dos últimos 30 dias × os dias que faltam |
| Gasto por dia | Gráfico com as três partes do custo; passar o mouse sobre um dia mostra os valores |
| Detalhamento | Cada item de custo, com a quantidade (chamadas, mensagens, execuções) e o valor |
| Como os valores são calculados | Notas e a conferência com a fatura da AWS |

A tela se atualiza sozinha a cada minuto.

### Como ler os números

- **Os valores são medidos, não estimados.** Cada uso real (as palavras
  processadas pelo modelo, cada mensagem do WhatsApp, o tempo de execução do
  assistente) é multiplicado pelo preço público da AWS e da Meta. É o mesmo
  cálculo da fatura, feito minutos depois do uso em vez de até um dia depois.
- **Conferido com a fatura.** Em setembro, o custo do modelo de linguagem
  medido pelo painel bateu com a fatura da AWS **ao centavo, dia a dia**. O
  rodapé da aba mostra essa conferência para o período escolhido.
- **Quando cada valor aparece:**

  | Item | Aparece na aba |
  |---|---|
  | Modelo de linguagem, busca, mensagens do WhatsApp e do atendimento humano | segundos a 5 minutos depois do uso |
  | Execução do assistente, número de perguntas, serviços de apoio | de hora em hora |
  | Infraestrutura fixa | valor mensal dividido pelos dias do mês |
  | Registro de auditoria | pela fatura, com até um dia de atraso |

  Por isso, logo depois de uma pergunta, o número de perguntas de "hoje" pode
  continuar o mesmo por até uma hora, enquanto o custo do modelo já subiu.

- **Só a PROGEP.** A conta da AWS do IFS também hospeda outros projetos. O
  painel separa o que é do assistente pelos recursos que ele próprio usa, então
  **o total da aba é menor que o total da fatura da conta**, e isso é esperado.

---

## 3. Do que é feito o custo

A aba agrupa os itens em três partes.

### Assistente: varia com o número de perguntas

| Item | O que é | Preço |
|---|---|---|
| Modelo Claude (respostas e classificação) | O modelo de linguagem que lê os trechos e escreve a resposta; também escolhe o setor quando não há resposta | US$ 3 por milhão de palavras-unidade (*tokens*) lidas e US$ 15 por milhão escritas |
| Busca nos documentos | Transforma a pergunta em vetor para achar os trechos | US$ 0,02 por milhão de *tokens* (frações de centavo) |
| Execução do assistente (AgentCore) | O processamento do assistente enquanto responde | US$ 0,0895 por hora de processador e US$ 0,00945 por GB-hora de memória |
| WhatsApp: mensagens enviadas (AWS) | Cada mensagem que o assistente envia | US$ 0,005 por mensagem |
| WhatsApp: mensagens recebidas (AWS) | Cada mensagem que o servidor envia | US$ 0,001 por mensagem |
| WhatsApp: tarifa da Meta | Só acima de 1.000 mensagens entregues no mês (seção 4) | US$ 0,0068 por mensagem |
| Atendimento humano (Connect) | Cada mensagem trocada no chat com o atendente, nos dois sentidos | US$ 0,01 por mensagem |

**Custo típico por pergunta** (medido em setembro de 2026):

| Item | Por pergunta |
|---|---|
| Modelo Claude | US$ 0,017 |
| Execução do assistente | até US$ 0,02 |
| Mensagens do WhatsApp (AWS) | US$ 0,011 |
| Busca nos documentos | menos de US$ 0,001 |
| **Total** | **US$ 0,03 a 0,05** |

Perguntas que geram respostas longas custam mais que a média; cortesias como
"bom dia" e "obrigado" não passam pelo modelo e custam só a mensagem.

### Documentos: novos documentos e manutenção da base

| Item | O que é |
|---|---|
| Preparação de documentos para a busca | Cada documento novo é dividido em trechos e indexado; custa frações de centavo |
| Equipe técnica: transcrições e testes | Uso do modelo pela DNX, por exemplo para transcrever documentos escaneados ou testar o assistente |

**Quanto custa acrescentar documentos:**

| Tipo de documento | Custo |
|---|---|
| Documento com texto (PDF digital, Word, HTML, texto) | praticamente zero: menos de US$ 0,001 cada. Cem documentos de 20 páginas custam menos de US$ 0,05 |
| PDF escaneado (precisa ser transcrito) | cerca de US$ 0,02 por página; um documento de 20 páginas sai por cerca de US$ 0,40 |
| Fluxograma em imagem | cerca de US$ 0,06 por fluxograma (medido nos 94 do Mapeamento de Processos) |

Enviar documentos pelo painel, portanto, não pesa no custo. Só a transcrição de
material em imagem tem custo perceptível, e é feita uma única vez por
documento.

**Montagem da base atual** (cerca de 420 documentos, julho a setembro de 2026):
cerca de **US$ 6 a 7, uma única vez**, quase todo na transcrição dos 94
fluxogramas (US$ 5,40). Manter a base guardada custa menos de US$ 0,02 por mês.

### Infraestrutura: custo fixo

| Item | Custo por mês |
|---|---|
| Guarda de credenciais (Secrets Manager) | US$ 0,80 |
| Logs e métricas (CloudWatch) | US$ 0,20 a 1, conforme o uso |
| Armazenamento (documentos, índice de busca e imagem do assistente) | menos de US$ 0,10 |
| Automações (Lambda), banco de conversas (DynamoDB) e API | centavos |
| Este controle de custos (consultas à fatura e às métricas) | cerca de US$ 0,80 |
| Registro de auditoria (CloudTrail) | sem custo desde 02/10/2026 (seção 6) |
| **Total** | **cerca de US$ 2 a 3** |

---

## 4. Tarifa da Meta (WhatsApp)

Desde **01/10/2026**, a Meta cobra as mensagens de resposta acima de uma cota
grátis:

- cada número tem **1.000 mensagens grátis por mês**;
- a partir da 1.001ª, cerca de **US$ 0,0068 por mensagem entregue**.

O assistente envia de 1,5 a 2 mensagens por pergunta (resposta, apresentação,
aviso de encerramento), então a cota cobre de **500 a 650 perguntas por mês**.
Acima disso, o custo por pergunta sobe cerca de US$ 0,01. A aba "Custos" mostra
quanto da cota já foi usado no mês.

A Meta indica que **órgãos públicos elegíveis podem continuar isentos**. A
elegibilidade do IFS precisa ser confirmada com a Meta, pelo suporte do
portfólio "IFS - DTI". Se confirmada, a DNX ajusta o painel para não cobrar
essa tarifa.

---

## 5. Planejamento por volume

Estimativas para volumes que ainda não aconteceram, a partir do custo medido
por pergunta. O valor real aparece na aba "Custos".

| Perguntas por mês | Uso | Fixo | Tarifa da Meta (sem isenção) | Total |
|---|---|---|---|---|
| 500 | US$ 15 a 25 | US$ 2 a 3 | dentro da cota | US$ 17 a 28 |
| 1.000 | US$ 30 a 50 | US$ 2 a 3 | US$ 3 a 7 | US$ 35 a 60 |
| 5.000 | US$ 150 a 250 | US$ 2 a 3 | US$ 45 a 60 | US$ 197 a 313 |
| 10.000 | US$ 300 a 500 | US$ 2 a 3 | US$ 95 a 130 | US$ 397 a 633 |

**Atendimento humano:** cobrado só quando usado. Um atendimento típico, de 10 a
20 mensagens, custa de US$ 0,10 a 0,20, mais as mensagens do WhatsApp. Não há
mensalidade por atendente.

---

## 6. Histórico e economias aplicadas

| Mês | Perguntas | Assistente | Documentos | Infraestrutura | Total |
|---|---|---|---|---|---|
| Setembro/2026 (testes) | 98 | US$ 2,13 | US$ 0,40 | US$ 5,64 | **US$ 8,17** |

**Economias de 02/10/2026:**

- **Registro de auditoria duplicado desligado.** A conta já tem um registro de
  auditoria geral, mantido pela organização. O do projeto duplicava esse
  trabalho, e a AWS cobrava a segunda cópia: cerca de US$ 4,30 por mês, dois
  terços do custo de setembro. Não houve perda de auditoria. Se a conta do IFS
  deixar a organização da DNX, o registro próprio precisa ser religado (é uma
  configuração já preparada).
- **Versões antigas da imagem do assistente** passaram a ser apagadas depois de
  7 dias: eram 2,9 GB guardados sem necessidade.
- **Métricas de monitoramento** deixaram de ser criadas uma por conversa, o que
  faria o custo de monitoramento crescer com o número de conversas.

Com isso, a parte fixa caiu de US$ 6 a 7 para cerca de US$ 2 a 3 por mês (já
incluído o próprio controle de custos, de cerca de US$ 0,80).

---

## 7. O que não aparece na aba

- **Impostos** da fatura da AWS (cerca de 13% nesta conta).
- **Outros projetos** da mesma conta da AWS.
- **Serviços dentro da faixa gratuita** ou abaixo de um centavo por mês:
  login (Cognito), entrega do painel (CloudFront) e filas de mensagens (SNS).
- **Mensagens recebidas no WhatsApp antes de 02/10/2026**: o registro delas
  começou nessa data. São US$ 0,001 cada, algo como US$ 0,10 em setembro.

---

## 8. Perguntas frequentes

**Por que o total da aba é diferente da fatura da AWS?**
A fatura é da conta inteira, que inclui outros projetos do IFS e os impostos. A
aba mostra só o assistente da PROGEP, sem impostos.

**Por que "hoje" mostra zero perguntas logo depois de eu testar?**
O número de perguntas é atualizado de hora em hora. O custo do modelo e das
mensagens aparece antes, em poucos minutos.

**A projeção do mês é confiável?**
Ela supõe que os próximos dias terão o mesmo gasto médio dos últimos 30. Um
aumento de uso (divulgação do assistente, por exemplo) aparece na projeção à
medida que acontece.

**Quem pode ver a aba?**
Só as pessoas com acesso ao painel do acervo, autorizadas pela PROGEP. Para
incluir alguém, peça à DNX (seção 2).
