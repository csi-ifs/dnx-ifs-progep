# Assistente Virtual da PROGEP — Visão Geral

**Instituto Federal de Sergipe · Pró-Reitoria de Gestão de Pessoas**
Documento de apresentação · outubro de 2026 · DNX Brasil

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
| **Site do IFS** | A porta de entrada (API) está pronta, mas não foi integrada ao site: o foco do projeto foi o WhatsApp. A equipe do site pode fazer a integração seguindo o guia da documentação técnica (seção 5.5) |

O nome **"IFS - Progep"** foi aprovado pela Meta para o número. Ele passa a
aparecer no WhatsApp depois de um registro do número, feito pelo administrador
do portfólio do IFS na Meta (passo a passo na documentação técnica, seção 7.3).

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
| Painel do acervo | Amazon CloudFront, S3 e Amazon Cognito | Página com login para a PROGEP gerenciar os documentos da base e acompanhar os custos |
| Controle de custos | AWS Lambda, Amazon DynamoDB e AWS Cost Explorer | Mede o gasto do assistente poucos minutos depois de cada uso e confere com a fatura |
| Conexão com o site | Amazon API Gateway e Amazon Cognito | Porta de entrada para o site do IFS consultar o assistente |
| Segurança e registros | AWS Secrets Manager, CloudWatch e CloudTrail | Guardam as credenciais com segurança, registram o funcionamento e auditam as alterações |

---

## 4. Painel do acervo

**Endereço:** https://d2ush4hjiv6t7m.cloudfront.net

O painel permite que a própria PROGEP mantenha a base de documentos
atualizada, sem depender da DNX, e acompanhe quanto o assistente custa.

**Acesso:** o painel exige login (usuário e senha), e só as pessoas
autorizadas pela PROGEP entram. Para ganhar acesso, peça à DNX informando o
nome e o e-mail institucional da pessoa; ela recebe por e-mail uma senha
temporária, trocada no primeiro login.

### O que dá para fazer

- **Enviar um documento novo.** Formatos aceitos: PDF, Word (.docx), texto
  (.txt), HTML e Markdown, com até 50 MB. O documento passa por uma etapa de
  revisão antes de entrar na base; depois de confirmado, o assistente passa a
  usá-lo em poucos minutos. Se o arquivo **já estiver na base** com o mesmo
  conteúdo, o painel avisa onde ele está e não deixa enviar de novo. Se houver
  documento **com o mesmo nome**, o painel pergunta se é uma versão nova (a
  anterior vai para a lixeira) ou outro documento (os dois ficam).
- **Consultar os documentos da base**, com busca por nome.
- **Remover um documento.** O arquivo vai para uma lixeira e pode ser
  recuperado pela DNX em até 30 dias.
- **Conferir a base.** Uma verificação mostra documentos que estão no acervo
  mas não puderam ser lidos pelo assistente. No topo da lista aparecem também
  as **possíveis cópias** (mesmo conteúdo, ou mesmo nome em formatos
  diferentes), para decidir qual manter.
- **Acompanhar os custos** (aba "Custos"). Mostra o gasto de hoje, dos últimos
  7 e 30 dias e do mês, com projeção para o fechamento do mês, custo por
  pergunta e quanto da cota grátis de mensagens da Meta já foi usado. O valor
  é **medido a partir do uso real**, não estimado, e aparece poucos minutos
  depois de cada pergunta. Conta **só o assistente da PROGEP**: a conta da AWS
  do IFS também tem outros projetos, que ficam de fora. Como ler cada número:
  [custos-assistente-progep.md](custos-assistente-progep.md).

### Boas práticas

- **Manter só a versão atual de cada norma.** Quando o mesmo conteúdo está na
  base em versões diferentes, o assistente pode usar a desatualizada.
- **FAQs: manter "FAQ" ou "Perguntas frequentes" no nome do arquivo e as
  perguntas numeradas.** O assistente lê os documentos em blocos de tamanho
  fixo, e num FAQ longo a resposta de uma pergunta poderia ficar partida entre
  dois blocos. Por isso o painel reconhece o FAQ no envio e o separa sozinho em
  uma parte por pergunta — na lista ele aparece como um documento só, com o
  número de perguntas. Se o painel avisar que não reconheceu as perguntas,
  peça à DNX para fazer a divisão.
- **Evitar cópias do mesmo documento.** Cópias repetidas ocupam o espaço de
  outros documentos na busca e pioram as respostas.
- **Preferir documentos com texto.** PDFs escaneados (fotos de páginas) não têm
  texto legível pelo assistente e aparecem na conferência como não lidos. Nesses
  casos, a DNX pode transcrever o conteúdo.

---

## 5. Custos

O detalhamento completo, item a item, está em
[custos-assistente-progep.md](custos-assistente-progep.md). Em resumo:

| | Valor |
|---|---|
| Custo por pergunta | US$ 0,04 a 0,06 |
| Custo fixo mensal (existe mesmo sem uso) | cerca de US$ 2 a 3 |
| Setembro/2026, fase de testes (98 perguntas), medido | US$ 8,17 |
| Previsão para 1.000 perguntas por mês | US$ 45 a 70 |
| Montagem da base de documentos (uma única vez) | cerca de US$ 6 a 7 |
| Acrescentar um documento com texto | menos de US$ 0,001 |

Os valores estão em **dólar americano**, sem os impostos da fatura da AWS
(cerca de 13% nesta conta). Desde 01/10/2026 a Meta cobra as mensagens do
WhatsApp acima de 1.000 por mês; a previsão acima já inclui essa tarifa. O
IFS pode ser isento por ser órgão público, o que ainda precisa ser confirmado
com a Meta.

O **gasto real**, dia a dia, aparece na aba **"Custos" do painel** (seção 4),
com acesso por login.

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
