# Assistente Virtual da PROGEP — Visão Geral

**Instituto Federal de Sergipe · Pró-Reitoria de Gestão de Pessoas**
Documento de apresentação · setembro de 2026 · DNX Brasil

---

## 1. O que é

O Assistente Virtual da PROGEP responde, a qualquer hora, dúvidas dos
servidores do IFS sobre gestão de pessoas: férias, licenças, aposentadoria,
progressões, adicionais, remoção, auxílios e demais assuntos da Pró-Reitoria.

Três características definem o assistente:

- **Responde somente com base nos documentos oficiais da PROGEP.** Leis,
  decretos, portarias, instruções normativas, fluxos de processos, cartilhas e
  perguntas frequentes. Ele não usa informações da internet nem opinião.
- **Mostra as fontes.** Cada resposta termina com a lista dos documentos
  consultados, para que o servidor possa conferir a norma.
- **Não inventa quando não sabe.** Se os documentos não trazem a resposta, ele
  avisa e indica o setor da PROGEP que costuma tratar o assunto, com e-mail e
  telefone.

### Canais

| Canal | Situação |
|---|---|
| **WhatsApp** — número +55 79 3711-1496 ([abrir conversa](https://wa.me/557937111496)) | Em funcionamento |
| **Site do IFS** | Pronto do lado do assistente; aguarda decisão do IFS sobre a forma de acesso e a integração pela equipe do site |

O nome exibido no WhatsApp será **"IFS - Progep"** (aprovado pela Meta; falta
apenas ativá-lo no número).

---

## 2. Como funciona

### O caminho de uma pergunta

1. O servidor envia a pergunta pelo WhatsApp. Na primeira mensagem, o
   assistente se apresenta e explica o que faz.
2. Enquanto processa, o WhatsApp mostra "digitando…".
3. O assistente procura, entre os cerca de 420 documentos da base, os trechos
   que tratam do assunto.
4. Com base **apenas** nesses trechos, escreve a resposta e envia com a lista
   de fontes. Em média, a resposta chega em poucos segundos.

### Conversa contínua

O assistente entende perguntas de acompanhamento, como *"e para docentes?"* ou
*"quanto tempo antes preciso pedir?"*, desde que a conversa não fique parada
por mais de 20 minutos. Mensagens de cortesia ("bom dia", "obrigado", "tchau")
são respondidas na hora, sem consultar os documentos.

### Quando não há resposta nos documentos

| Situação | O que o servidor recebe |
|---|---|
| Assunto de um setor da PROGEP | O contato do setor que costuma tratar o assunto (e-mail, telefone e ramal) |
| Assunto de saúde (perícia, licença para tratamento) | Os contatos do SIASS e da DAP |
| Assunto da PROGEP sem setor claro | O contato geral da PROGEP |
| Assunto fora da gestão de pessoas (ex.: cardápio do restaurante) | O aviso de que o tema não é da PROGEP e a oferta da lista completa de contatos |

O setor é escolhido a partir do **mapeamento de processos da própria PROGEP**,
que indica o setor responsável por cada processo. Na dúvida, o assistente
indica o contato geral, para nunca mandar o servidor ao setor errado.

### Atendimento humano

De **segunda a sexta, das 9h às 18h**, junto com o contato do setor, o
assistente pergunta se o servidor prefere falar com um atendente. Se ele
responder "sim":

1. A conversa é transferida para a fila de atendimento da PROGEP.
2. O atendente recebe a conversa num painel web, já com a pergunta que ficou
   sem resposta.
3. O servidor continua no mesmo WhatsApp: as mensagens passam a ir para o
   atendente, e as respostas do atendente chegam por lá.
4. Quando o atendente encerra, o servidor é avisado e o assistente volta a
   responder.

Fora do horário, o assistente envia apenas o contato do setor. O uso do
atendimento humano é opcional e pode ser desligado se a PROGEP preferir
trabalhar só com os contatos dos setores.

### Encerramento da conversa

Se o servidor ficar 20 minutos sem responder, o assistente envia uma mensagem
de encerramento. Uma nova mensagem depois disso começa uma conversa nova.

### Privacidade

- O assistente não pede nome, CPF nem matrícula.
- O histórico da conversa é guardado apenas pelo tempo necessário: o contexto
  vale por 20 minutos, e o registro da conversa é apagado automaticamente em
  até 24 horas (48 horas quando houve atendimento humano).
- Os registros técnicos de uso do modelo, acessíveis apenas à equipe técnica,
  contêm o texto das buscas feitas a partir das perguntas. Eles servem para
  acompanhar custos e investigar respostas incorretas.
- Todos os registros técnicos são apagados automaticamente depois de 90 dias. O
  telefone de quem conversou aparece neles só com os 4 últimos dígitos.

---

## 3. Do que o projeto é feito

Todo o assistente roda na **conta AWS do IFS**, na região de Virgínia (EUA).
Nenhum componente fica em servidor próprio da DNX.

| Parte | Serviço | Para que serve |
|---|---|---|
| Inteligência das respostas | Amazon Bedrock — modelo Claude Sonnet 4.5 | Lê os trechos encontrados e escreve a resposta em português |
| Base de conhecimento | Amazon Bedrock Knowledge Bases, com Amazon S3 e S3 Vectors | Guarda os documentos da PROGEP e permite encontrar rapidamente os trechos de cada assunto |
| Motor do assistente | Amazon Bedrock AgentCore | Executa o assistente sob demanda, só quando chega uma pergunta |
| WhatsApp | AWS End User Messaging Social, conectado à Meta | Recebe e envia as mensagens do número oficial |
| Atendimento humano | Amazon Connect | Fila da PROGEP e painel dos atendentes |
| Memória da conversa | Amazon DynamoDB | Guarda por pouco tempo o andamento de cada conversa |
| Automações | AWS Lambda | Pequenos programas que ligam as partes: recebem a mensagem, chamam o assistente, encerram conversas paradas |
| Painel do acervo | Amazon CloudFront, S3 e Amazon Cognito | Página com login para a PROGEP gerenciar os documentos da base |
| Conexão com o site | Amazon API Gateway e Amazon Cognito | Porta de entrada para o site do IFS consultar o assistente |
| Segurança e registros | AWS Secrets Manager, CloudWatch e CloudTrail | Guardam as credenciais com segurança, registram o funcionamento e auditam as alterações |

---

## 4. Painel do acervo

**Endereço:** https://d2ush4hjiv6t7m.cloudfront.net

O painel permite que a própria PROGEP mantenha a base de documentos
atualizada, sem depender da DNX. O acesso é feito com usuário e senha, e só as
pessoas autorizadas pela PROGEP podem alterar a base.

### O que dá para fazer

- **Enviar um documento novo.** Formatos aceitos: PDF, Word (.docx), texto
  (.txt), HTML e Markdown, com até 50 MB. O documento passa por uma etapa de
  revisão antes de entrar na base; depois de confirmado, o assistente passa a
  usá-lo em poucos minutos.
- **Consultar os documentos da base**, com busca por nome.
- **Remover um documento.** O arquivo vai para uma lixeira e pode ser
  recuperado pela DNX em até 30 dias.
- **Conferir a base.** Uma verificação mostra documentos que estão no acervo
  mas não puderam ser lidos pelo assistente.
- **Acompanhar os custos** (aba "Custos"). Mostra o gasto de hoje, dos últimos
  7 e 30 dias e do mês, com projeção para o fechamento do mês, custo por
  pergunta e quanto da cota grátis de mensagens da Meta já foi usado. O valor
  é **medido a partir do uso real**, não estimado, e aparece poucos minutos
  depois de cada pergunta. Conta **só o assistente da PROGEP**: a conta da AWS
  do IFS também tem outros projetos, que ficam de fora.

### Boas práticas

- **Manter só a versão atual de cada norma.** Quando o mesmo conteúdo está na
  base em versões diferentes, o assistente pode usar a desatualizada.
- **Evitar cópias do mesmo documento.** Cópias repetidas ocupam o espaço de
  outros documentos na busca e pioram as respostas.
- **Preferir documentos com texto.** PDFs escaneados (fotos de páginas) não têm
  texto legível pelo assistente e aparecem na conferência como não lidos. Nesses
  casos, a DNX pode transcrever o conteúdo.

---

## 5. Custos

Os valores estão em **dólar americano**, moeda em que a AWS cobra. Em reais,
variam com o câmbio. Sobre eles incidem os impostos cobrados pela AWS na
fatura: nesta conta, cerca de 13%.

Os custos se dividem em duas partes: uma **fixa**, que existe mesmo sem uso, e
uma **por uso**, proporcional à quantidade de perguntas.

Os valores reais, dia a dia, ficam na aba **"Custos" do painel do acervo**
(seção 4). As tabelas abaixo servem para planejar volumes que ainda não
aconteceram.

### Custo por pergunta

Medido em setembro de 2026, com 78 perguntas reais ao assistente.

| Item | Custo por pergunta |
|---|---|
| Inteligência das respostas (modelo Claude) | US$ 0,017 |
| Execução do assistente (AgentCore) | até US$ 0,02 |
| Mensagens do WhatsApp (tarifa da AWS) | US$ 0,011 |
| Busca nos documentos | menos de US$ 0,001 |
| **Total por pergunta** | **US$ 0,03 a 0,05** |

**Tarifa da Meta (desde 01/10/2026).** Cada número tem **1.000 mensagens de
resposta grátis por mês**. A partir da 1.001ª, a Meta cobra cerca de
**US$ 0,0068 por mensagem** entregue. Com 1,5 a 2 mensagens enviadas por
pergunta, a cota cobre de 500 a 650 perguntas por mês. Acima disso, o custo
por pergunta sobe cerca de US$ 0,01. A Meta indica que órgãos públicos
elegíveis podem continuar isentos; a elegibilidade do IFS ainda precisa ser
confirmada com a Meta.

### Estimativa mensal por volume de uso

| Perguntas por mês | Custo de uso estimado | Tarifa da Meta (sem isenção) |
|---|---|---|
| 1.000 | US$ 30 a 50 | + US$ 3 a 7 |
| 5.000 | US$ 150 a 250 | + US$ 45 a 60 |
| 10.000 | US$ 300 a 500 | + US$ 95 a 130 |

### Atendimento humano

Cobrado só quando usado: **US$ 0,01 por mensagem** trocada no chat do
atendimento, além da tarifa normal do WhatsApp. Um atendimento típico, de 10 a
20 mensagens, custa de US$ 0,10 a 0,20. Não há mensalidade por atendente.

### Custo fixo mensal

| Item | Custo por mês |
|---|---|
| Guarda segura de credenciais (Secrets Manager) | US$ 0,80 |
| Monitoramento (CloudWatch) | US$ 0,20 a 1 |
| Armazenamento (documentos, índice de busca e imagem do assistente) | menos de US$ 0,10 |
| Registro de auditoria (CloudTrail) | sem custo (ver abaixo) |
| Banco de conversas, automações, painel e API | centavos |
| **Total fixo** | **cerca de US$ 1 a 2** |

**Economia aplicada em 02/10/2026.** O custo fixo era de US$ 6 a 7 por mês. A
maior parte vinha do registro de auditoria do projeto, que duplicava o
registro geral que a organização já mantém para a conta; a AWS cobra a
segunda cópia, e ela foi desligada sem perda de auditoria. As versões antigas
da imagem do assistente passaram a ser apagadas depois de 7 dias. Se a conta
do IFS deixar a organização da DNX, o registro próprio deve ser religado
(é uma configuração, já preparada).

### Base de conhecimento

**Quanto custou montar a base atual** (cerca de 420 documentos, medido na
fatura e no uso do modelo de julho a setembro de 2026):

| Item | Custo |
|---|---|
| Transcrição dos 94 fluxogramas do Mapeamento de Processos (imagens lidas pelo modelo Claude) | cerca de US$ 5,40 |
| Transcrição de documentos escaneados (PDFs sem texto) | menos de US$ 1 |
| Preparação dos documentos para a busca (todas as indexações) | US$ 0,09 |
| Gravação no índice de busca | US$ 0,03 |
| **Total** | **cerca de US$ 6 a 7, uma única vez** |

Manter a base guardada custa **menos de US$ 0,02 por mês**: são cerca de
170 MB de documentos e 12.600 trechos no índice de busca.

**Quanto custa acrescentar documentos novos:**

| Tipo de documento | Custo estimado |
|---|---|
| Documento com texto (PDF digital, Word, HTML, texto) | praticamente zero: menos de US$ 0,001 por documento. Cem documentos de 20 páginas custam menos de US$ 0,05 |
| PDF escaneado (precisa ser transcrito) | cerca de US$ 0,02 por página. Um documento de 20 páginas sai por cerca de US$ 0,40 |
| Fluxograma em imagem | cerca de US$ 0,06 por fluxograma (valor medido) |

Ou seja: enviar documentos pelo painel do acervo não pesa no custo. Só a
transcrição de material em imagem tem custo perceptível, e ela é feita uma
única vez por documento.

### Visão geral

| Cenário | Custo mensal aproximado |
|---|---|
| Fase de testes (setembro/2026, 98 perguntas), medido | US$ 8,17 |
| 1.000 perguntas por mês | US$ 30 a 55 |
| 5.000 perguntas por mês | US$ 150 a 255 |

Os cenários de volume não incluem a tarifa da Meta acima da cota grátis (ver
"Tarifa da Meta"). A conta AWS é compartilhada com outros projetos do IFS; os
valores acima consideram apenas o que é do assistente da PROGEP, medido pelos
recursos do próprio assistente.

---

## 6. Limites e cuidados

- **O assistente é tão atual quanto a base de documentos.** Se um documento
  está desatualizado, a resposta também estará. Por isso é importante manter o
  acervo revisado pelo painel.
- **Ele orienta, não decide.** As respostas não substituem o atendimento formal
  nem a análise dos setores. Em caso de dúvida, o servidor deve conferir a
  norma citada ou procurar o setor indicado.
- **Ele não consulta dados individuais.** O assistente não acessa SIGEPE,
  SouGov, SEI ou dados funcionais do servidor, e não abre processos.
- **Pode errar.** Como todo sistema baseado em inteligência artificial, pode
  interpretar mal uma pergunta ou juntar informações de forma imprecisa. As
  fontes ao pé de cada resposta permitem conferir. Os retornos da PROGEP sobre
  respostas incorretas são usados para corrigir o assistente e a base.
- **Documentos em imagem não são lidos.** PDFs escaneados precisam ser
  transcritos para que o assistente consiga usá-los.
