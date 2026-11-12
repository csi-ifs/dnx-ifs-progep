# Assistente Inteligente PROGEP-IFS — Documentação Técnica

Documentação da solução **conforme deployada** na conta AWS `342016537070`
(região `us-east-1`). As especificações de requisitos e o design de referência
estão em `.kiro/specs/`; aqui o foco é o que está de fato provisionado, como
operar, como diagnosticar e o que ainda falta. É o documento para quem for
manter ou evoluir o projeto.

A visão para o cliente (o que é, como funciona, custos, em linguagem não
técnica) está em [visao-geral-assistente-progep.md](visao-geral-assistente-progep.md),
e o detalhamento dos custos, com o acesso à aba "Custos", em
[custos-assistente-progep.md](custos-assistente-progep.md).

Última atualização: 2026-10-08.

---

## 1. O que é o projeto

Assistente de atendimento automatizado para servidores e candidatos do
**Instituto Federal de Sergipe**, na área da **Pró-Reitoria de Gestão de Pessoas
(PROGEP)**.

O usuário pergunta em linguagem natural — *"como solicito licença
capacitação?"* — e recebe uma resposta fundamentada **exclusivamente** nos
documentos oficiais da PROGEP, com as fontes usadas. Quando não há documento que
sustente a resposta, o assistente **não inventa**: ele declara que não encontrou,
indica o contato do setor da PROGEP que costuma tratar o assunto e, no horário
de atendimento, oferece transferir para um atendente humano.

**Princípio de projeto que orienta o resto:** é melhor admitir que não sabe do
que errar com confiança. Num processo de aposentadoria ou licença médica, uma
etapa inventada é pior que uma lacuna assumida, porque ninguém percebe o erro.

**Canais:**

| Canal | Estado |
|---|---|
| WhatsApp institucional **+55 79 3711-1496** (End User Messaging Social) | Em produção desde 22/09/2026 |
| Transbordo humano (Amazon Connect) | Validado em 22/09/2026; atendentes de teste (§6) |
| API para o site institucional (autenticada via Cognito) | Publicada; não integrada ao site — guia em §5.5 |
| WhatsApp de teste +1 555-379-4876 (End User Messaging) | Desenvolvimento; bloqueado pela Meta (erro 131037, §7) |
| WhatsApp de teste +1 555 176-1308 (webhook interino, Graph API) | Removido em 02/10/2026 (§7.5) |

**Estado atual:** ~420 documentos na base, 769 testes automatizados, resposta
em ~5–7 s, continuidade de conversa, contatos dos setores quando não há
resposta, e painel web para a PROGEP manter o acervo (§8) e acompanhar os
custos medidos do assistente (§8.6).

---

## 2. Estrutura do projeto

### 2.1 Diretórios

```
src/
  domain/     # lógica pura, sem I/O — testada por unidade e propriedades
              #   config, models, relevance, session, billing, iam,
              #   input_validation, escalation, ingestion, retry, routing
              #   social.py     — reconhece cortesias ("obrigado", "tchau")
              #   transbordo.py — expediente e leitura do "sim/não" da oferta
              #   contatos.py   — catálogo de contatos e assuntos dos setores
              #   fontes.py     — nome legível das fontes citadas
              #   acervo.py     — regras do painel (nomes, chaves, lixeira)
              #   custos.py     — preços, atribuição e resumo da aba Custos
              #   privacidade.py— mascara o telefone antes de ir para log
  adapters/   # fronteira de I/O
              #   agent.py             — RAG (retrieve + generate) e
              #                          classificação de setor
              #   agentcore_client.py  — invoca o AgentCore Runtime
              #   conversation_store.py— estado da conversa no DynamoDB
              #                          (sessão, atividade, transbordo, dedup)
              #   lambda_entry.py      — wiring das Lambdas e orquestração
              #                          do WhatsApp
              #   whatsapp_social.py   — End User Messaging Social
              #   connect_chat.py      — ponte WhatsApp ↔ chat do Connect
              #   acervo_handler.py    — API do painel (acervo e custos)
              #   custos_handler.py    — coleta de custos (logs e métricas)
              #   custos_store.py      — tabela de custos (DynamoDB)
              #   api_handler.py       — handler da API do site (injetável)
              #   knowledge.py         — ingestion jobs da KB
              #   monitor_adapter.py   — métricas CloudWatch
              #   connect_router.py, whatsapp_connector.py — desenho original
              #                          (Connect em toda mensagem), fora de
              #                          produção, mantidos pelos testes da spec
  flows/      # orquestração: ingestão (em produção); site_flow e
              # whatsapp_flow são do desenho original, só usados pelos testes
  infra/      # IaC Pulumi (provider, resources, connect_flow, __main__)
agentcore/    # entrypoint (app.py) e Dockerfile do AgentCore Runtime
frontend/     # painel do acervo (React + Vite)
scripts/      # ingestão, transcrição, testes, custos e deploy (ver §3.3)
tests/        # property / unit / integration
docs/         # documentação; as transcrições (ocr/, fluxos/, faq/) ficam
              # fora do git — a versão indexada está no S3 da base
```

### 2.2 Arquitetura

```
   Site institucional ── API Gateway (Cognito) → Lambda progep-ifs-api ──┐
                                                                         │
   WhatsApp ── Meta → End User Messaging Social → SNS                    │
               progep-ifs-whatsapp-inbound → Lambda whatsapp-social ─────┤
                              │  ▲                                       │
          (transbordo aceito) │  │ fala do atendente                     │ invoke-
                              v  │                                       │ agent-
               Amazon Connect ── streaming → SNS connect-chat-stream     │ runtime
               (fila humana)      → Lambda connect-chat-stream           │
                                                                         │
   EventBridge (5 min) → Lambda whatsapp-encerramento (inatividade)      │
                                                                         │
   Painel do acervo ── CloudFront → S3 (estático); API /acervo           │
               → Lambda progep-ifs-acervo → S3 documentos + KB           │
                                       └→ tabela progep-ifs-custos (leitura)
   Custos: logs (invocações, WhatsApp, chat) → Lambda custos-logs ──┐    │
           EventBridge (1 h) → Lambda custos-coleta (métricas,      ├→ DynamoDB
                               Cost Explorer a cada 12 h) ──────────┘  progep-ifs-custos
                                                                         v
                        ┌─────────────────────────────────────────────┐
                        │   Bedrock AgentCore Runtime                 │
                        │   (container ARM64, agentcore/app.py)       │
                        │                                             │
                        │   ┌── ConversationStore (DynamoDB) ──┐      │
                        │   │  lê sessão → chama → grava sessão │      │
                        │   └──────────────────────────────────┘      │
                        └────────────────┬────────────────────────────┘
                                         │
                        ┌────────────────┴────────────────┐
                        │  em paralelo (ver §5.2)         │
                        v                                 v
                 Retrieve (scores)          RetrieveAndGenerate
                        │                    (Claude Sonnet 4.5)
                        │                   + Converse, só no not_found:
                        │                     classificação do setor
                        └──────────┬──────────┘
                                   v
                    Knowledge Base INLYXJCFOW (hierárquica)
                                   │
                    ┌──────────────┴───────────────┐
                    v                              v
        S3 Vectors (progep-ifs-vectors)   S3 progep-ifs-documents
                                             (fonte dos documentos)

Ingestão:  S3 (ObjectCreated) → Lambda ingestor → ingestion job da KB
Observabilidade: CloudWatch Logs + Transaction Search (aws/spans) + CloudTrail
```

> **Caminho único:** o fluxo Agentic RAG roda **apenas** no AgentCore Runtime.
> As Lambdas não executam o agente in-process — elas só invocam o runtime via
> `bedrock-agentcore:InvokeAgentRuntime`.

### 2.3 Fluxo de uma pergunta

No WhatsApp (`lambda_entry.processar_mensagens_whatsapp`), nesta ordem:

1. **Mensagem repetida?** Marca condicional `msg#<wamid>` no DynamoDB; a
   segunda entrega da mesma mensagem é ignorada (a entrega é "pelo menos uma
   vez").
2. **Validação**: só texto (mídia é recusada) e até 4096 caracteres.
3. **Em atendimento humano?** A mensagem vai para o chat do Connect (§6) e o
   processamento termina aqui.
4. Marca como lida e mostra **"digitando…"**.
5. **Oferta em aberto?** "sim"/"não" responde à oferta de atendente ou de lista
   de contatos; qualquer outra coisa descarta a oferta e segue como pergunta.
6. **Cortesia?** ("olá", "obrigado", "tchau") responde na hora, sem consultar a
   base. "Olá" em conversa nova recebe a apresentação completa.
7. **Saudação** na primeira mensagem de uma conversa nova.
8. Invoca o **AgentCore Runtime** com `{text, session_id, channel}`. No runtime:
   - busca a sessão do Bedrock da conversa em curso (janela de 20 min);
   - em paralelo, `Retrieve` (scores) e `RetrieveAndGenerate` (resposta);
   - decisão: resposta com citações e sem a sentinela → `answered`; sem
     citação ou com `NAO_ENCONTRADO` → `not_found`;
   - no `not_found`, classifica o setor responsável (`classificar_setor`);
   - grava a sessão devolvida pelo Bedrock, para entender o próximo turno.
9. Monta a resposta: `answered` leva as fontes; `not_found` leva o contato do
   setor e, no expediente, a oferta de atendente (§6.2).

---

## 3. Serviços e ferramentas

### 3.1 Serviços AWS

| Serviço | Recurso | Papel |
|---------|---------|-------|
| Amazon Bedrock | KB ativa `INLYXJCFOW` (data source `I50FTDPEQ6`, divisão hierárquica); reserva `1AACEWPJNC` (`ACPN3VUCDG`, divisão fixa) — §4.2; Claude Sonnet 4.5 (perfil `global.`), Titan Embed v2 | Recuperação, geração e classificação de setor |
| Bedrock AgentCore | Runtime `progep_ifs_agent-07SkZL6Kh5` | Hospedagem do agente (executa sob demanda) |
| Amazon S3 Vectors | `progep-ifs-vectors` / índices `progep-ifs-index-hier` (ativa) e `progep-ifs-index` (reserva) | Vector store da KB |
| Amazon S3 | `progep-ifs-documents` | Documentos da base (fonte da KB) |
| Amazon S3 | `progep-ifs-staging` | Preparo de uploads do painel (`preparo/`) e lixeira (`removidos/`, 30 dias) |
| Amazon S3 + CloudFront | `progep-ifs-painel` / distribuição `progep-ifs-painel-cf` | Site estático do painel do acervo |
| AWS Lambda | `-api`, `-acervo`, `-ingestor`, `-whatsapp-social`, `-whatsapp-encerramento`, `-connect-chat-stream`, `-custos-logs`, `-custos-coleta` | Handlers (§3.4) |
| Amazon SNS | `progep-ifs-whatsapp-inbound`, `progep-ifs-connect-chat-stream` | Entrada do WhatsApp; espelho do chat do Connect |
| AWS End User Messaging Social | WABA `IFS - PROGEP` (+55 79 3711-1496); WABA `Projeto dev` (teste) | Canal WhatsApp (§7) |
| Amazon EventBridge | regras `progep-ifs-whatsapp-encerramento` (5 min) e `progep-ifs-custos-coleta` (1 h) | Varredura de inatividade; coleta de custos |
| Amazon API Gateway | `57hy9et9yh` (stage `prod`: `/message`, `/acervo/*`) | API do site e do painel |
| Amazon Cognito | pool `us-east-1_3YNaD3UOP` (grupo `administradores`) | Login do painel e da API |
| Amazon ECR | `progep-ifs-agentcore` (imagens sem tag expiram em 7 dias) | Imagem do runtime |
| DynamoDB | `progep-ifs-sessions` (TTL) | Estado das conversas, índice de atendimentos, marcas de mensagem |
| DynamoDB | `progep-ifs-custos` (TTL só nas marcas) | Custos medidos por dia e item (§8.6) |
| AWS Cost Explorer | — (leitura a cada 12 h) | Fatura do CloudTrail e conferência do modelo (§8.6) |
| Amazon Connect | instância `progep-ifs-progep` | Fila e painel do atendimento humano (§6) |
| Secrets Manager | `progep-ifs/whatsapp`, `progep-ifs-secrets` | Token da Graph API, só para diagnóstico manual (§7.6) |
| CloudWatch / X-Ray | log groups (90 dias) + `aws/spans` | Observabilidade |
| CloudTrail | trilha `progep-ifs-trail`, **desligada** | A trilha `dnx` da organização cobre a conta; religar: §12 |

Todo recurso que suporta tags recebe `aws-apn-id: pc:7b0pljcsg0gii1a5gn5vfca0v`.
Os que não suportam são registrados no output `tagging_exceptions` do Pulumi.

### 3.2 Ferramentas

- **Pulumi (Python)** — IaC, backend local, stack `prod`
- **Docker** — build ARM64 do container do AgentCore
- **pytest + Hypothesis** — 769 testes (unidade, propriedades, integração)
- **React + Vite** — painel do acervo (`frontend/`)
- **PyMuPDF (fitz) + Pillow** — detecção de PDF sem texto e OCR

### 3.3 Scripts

| Script | Para quê |
|---|---|
| `ingest_site.py` | Crawl e ingestão do site da PROGEP |
| `ingest_repositorio.py` | Ingestão do pacote `.zip` enviado pelo cliente |
| `transcribe_flows.py` | Transcreve os 94 fluxogramas BPMN (imagens) |
| `ocr_documentos.py` | OCR de PDFs digitalizados |
| `dividir_faq.py` | Divide um FAQ do acervo em um arquivo por pergunta e tira o original da base (§4.5) |
| `check_flows.py` | Varredura de qualidade das transcrições |
| `run_qa_suite.py` | Bateria de perguntas contra o runtime |
| `testa_conversa.py` | Testa continuidade de conversa (follow-ups) |
| `compare_models.py` | Compara modelos de geração (qualidade × latência) |
| `deploy_agentcore.ps1` | Build, push e atualização do runtime |
| `publicar_painel.py` | Build do painel do acervo, `config.json` a partir dos outputs do Pulumi, upload e invalidação do CloudFront |
| `enable_bedrock_logging.ps1` | Liga o model invocation logging |
| `custos_retroativo.py` | Preenche a aba "Custos" com o uso anterior ao deploy (idempotente; `--simular` só imprime) |
| `retencao_logs.py` | Aplica 90 dias de retenção aos log groups da PROGEP (os das Lambdas nascem sem prazo); rodar depois de criar Lambda nova |

### 3.4 Lambdas: para que serve cada uma

| Lambda | Handler | Disparo | Papel |
|---|---|---|---|
| `progep-ifs-whatsapp-social` | `lambda_entry.whatsapp_social_handler` | SNS `whatsapp-inbound` | Atende o WhatsApp: dedup, cortesias, oferta, agente, transbordo; loga status de entrega da Meta |
| `progep-ifs-whatsapp-encerramento` | `lambda_entry.whatsapp_encerramento_handler` | EventBridge, 5 min | Envia o aviso de inatividade (20 min), exceto em atendimento humano |
| `progep-ifs-connect-chat-stream` | `lambda_entry.connect_chat_stream_handler` | SNS `connect-chat-stream` | Leva ao WhatsApp a fala do atendente, a entrada dele e o fim do atendimento |
| `progep-ifs-api` | `lambda_entry.api_handler` | API Gateway `/message` | API do site |
| `progep-ifs-acervo` | `acervo_handler` | API Gateway `/acervo/*` | Painel do acervo (§8) |
| `progep-ifs-ingestor` | ingestor | S3 `ObjectCreated` | Dispara a indexação dos documentos novos |
| `progep-ifs-custos-logs` | `custos_handler.logs_handler` | assinatura de 3 log groups | Custo em tempo real: modelo, WhatsApp, chat do Connect (§8.6) |
| `progep-ifs-custos-coleta` | `custos_handler.coleta_handler` | EventBridge, 1 h | Custo de métricas (AgentCore, Lambda, DynamoDB…), fixos e fatura (§8.6) |

---

## 4. Base de conhecimento

### 4.1 Composição

**~420 documentos** (419 na sincronização de 29/09/2026, 5 deles sem texto
legível), de duas origens. A tabela abaixo é da carga inicial; o acervo agora
também muda pelo painel (§8):

| Prefixo no S3 | Qtd. | Conteúdo |
|---|---|---|
| `site/` | 275 | Páginas e editais do site da PROGEP |
| `site/mapeamento-processos/` | 94 | Transcrições dos fluxogramas BPMN |
| `cliente/` | 123 | Repositório enviado pela PROGEP |
| `cliente/ocr/` | 2 | PDFs digitalizados, via OCR |
| `cliente/faq/`, `site/faq/` | 121 | FAQs divididos por pergunta (§4.5) |

O repositório do cliente vem organizado por departamento — DAP (73), DSDPES
(26), DNAGS (11), SIASS (11), PROGEP (2) — e a sigla é preservada no caminho do
S3, então a origem aparece na fonte citada pelo assistente.

### 4.2 Vector store e divisão em trechos

**S3 Vectors** (não OpenSearch Serverless, como no design original) — a troca foi
por custo: ~zero contra ~US$ 350/mês. Titan Embed v2, 1024 dimensões.

> **Pegadinha:** o índice precisa de `AMAZON_BEDROCK_TEXT` **e**
> `AMAZON_BEDROCK_METADATA` como não-filtráveis. Sem isso os documentos estouram
> o limite de 2048 bytes de metadado filtrável e a indexação falha.

**Duas bases, uma ativa.** Desde 10/11/2026 o assistente usa a base com
**divisão hierárquica** (`INLYXJCFOW`, índice `progep-ifs-index-hier`): a busca
compara trechos pequenos (filhos, ~300 tokens) e entrega ao modelo o trecho
grande que os contém (pai, ~1.500 tokens; sobreposição de 60). A base original,
com **divisão fixa** (`1AACEWPJNC`, índice `progep-ifs-index`), fica de reserva
com o mesmo acervo.

*Por que a troca.* Feedback do cliente: "quantas avaliações de estágio
probatório serão realizadas?" não era respondida, embora o FAQ estivesse
indexado. Na divisão fixa, a pergunta e a resposta do FAQ caíam em trechos
diferentes — a busca achava a pergunta, sem a resposta, e o modelo dizia que
não havia a informação. Com o pai de 1.500 tokens, pergunta e resposta chegam
juntas ao modelo. Medido nas baterias: sem perda nas demais perguntas.

*Efeito no custo.* O contexto enviado ao modelo passou de ~1.100 para ~4.700
tokens por pergunta: o custo por pergunta foi de US$ 0,03–0,05 para
**US$ 0,04–0,06**. A latência não mudou de forma perceptível.

*Qual base está ativa* sai da configuração `progep-ifs:baseAtiva`
(`hierarquica`, padrão, ou `fixa`). Ela define a base das Lambdas `acervo` e
`ingestor`; o runtime recebe o id pelo output `knowledge_base_id` no
`deploy_agentcore.ps1`. Para voltar à base de reserva:

```bash
pulumi config set progep-ifs:baseAtiva fixa
pulumi up
.\scripts\deploy_agentcore.ps1
```

Antes da volta, sincronize a base de reserva (o painel só altera a ativa):
`aws bedrock-agent start-ingestion-job` com os outputs `knowledge_base_fixa_id`
e `kb_fixa_data_source_id`. Documento **removido** pelo painel enquanto a outra
base estava ativa precisa sair também da reserva — a sincronização cuida disso,
porque o arquivo não está mais no bucket.

Quando a base hierárquica tiver se mostrado estável, a reserva pode ser
apagada (sai de `_definir_base_hierarquica`/`BASES_DISPONIVEIS` em
`src/infra/resources.py` e o índice `progep-ifs-index` é removido).

### 4.3 Formatos aceitos

`pdf`, `docx`, `txt`, `html`, `md`. Outros são **rejeitados pelo Ingestor sem
erro visível** (é o comportamento correto, Req 1.3) — se um lote não indexar,
confira o formato antes de procurar bug.

> **Armadilha conhecida:** PDF digitalizado sem camada de texto é indexado
> **vazio**, sem nenhum erro. Sempre rode a detecção antes de subir um lote:
> `ingest_repositorio.py` faz isso automaticamente e lista os suspeitos.

### 4.4 Qualidade e limites conhecidos

- **Descasamento de vocabulário.** A norma diz "ausentar-se do serviço"; o
  servidor pergunta "licença por falecimento". A busca não liga as duas, e a
  pergunta cai em `not_found` mesmo com a Lei 8.112 indexada. **Aumentar o
  número de trechos não resolve** (testado com 10 e 20). A correção é o FAQ dos
  setores, com o campo "como o servidor costuma perguntar isso".
- **O threshold de relevância é decorativo.** Com corte em 0,4, nada é barrado:
  pergunta fora de escopo pontua ~0,65. Todo `not_found` vem da etapa de
  geração, nunca do threshold.
- **Valores mudam por portaria.** O assistente responde o valor que está no
  corpus. Quando sair portaria nova, o corpus precisa ser atualizado — senão ele
  afirma o valor velho com toda a confiança.
- **Escopo além da PROGEP.** O repositório inclui a Lei 14.133 (licitações),
  então o assistente responde sobre compras. Não é bug, é o corpus.
- **Cópias do mesmo documento roubam vagas.** A geração usa só os 5 trechos
  mais parecidos. Em 29/09/2026 o Manual de Perícia de 2017 estava 4 vezes na
  base (dois pares de arquivos idênticos), e as 4 cópias do mesmo trecho
  ocupavam as 5 vagas — a Cartilha SIASS de 2024, atual, ficava de fora e o
  assistente respondia com a regra antiga. Três cópias foram removidas em
  setembro e mais sete em 10/11/2026. Desde então o painel **recusa** cópia
  idêntica e pergunta antes de indexar documento com o mesmo nome (§8.2), e o
  `ingest_site.py` pula arquivo cujo conteúdo já está no bucket. Para lotes
  subidos por outro script, compare os hashes (`ETag`) antes.
- **Diagramação de PDF atrapalha a busca.** PDFs de folheto quebram palavras
  com hífen ("servi - dor"). A transcrição limpa ajuda pouco no ranking, mas
  permite marcar cada seção com fonte e ano (indexada como
  `cliente/SIASS/Cartilha_SIASS_IFS_2024.md`; cópia local, fora do git, em
  `docs/ocr/`).
- **Em aberto: menos trechos que o pedido.** Nas perguntas de saúde, o
  `Retrieve` devolve 2 trechos quando se pedem 5 (13–15 quando se pedem 30); em
  outros assuntos vêm os 5. **Não** são vetores órfãos: o índice foi auditado
  (todos os 939 vetores do manual são da cópia mantida). Causa desconhecida;
  medir com `retrieve` antes de mexer em `DEFAULT_MAX_RESULTS`.
- **O modelo só vê o texto dos trechos**, não o nome nem a data do arquivo. A
  regra de "preferir o mais recente" do prompt só funciona quando a data está
  no próprio trecho.

### 4.5 FAQs divididos por pergunta

**O problema.** A base corta cada arquivo em trechos de tamanho fixo (§4.2),
sem saber onde uma pergunta começa. Num FAQ longo, a resposta de uma pergunta
pode ficar partida entre dois trechos. Caso real (12/11/2026): no FAQ de
estágio probatório (28 mil caracteres), "Quem é responsável por avaliar o
servidor?" ficou com a parte dos nomeados a partir de 07/02/2025 num trecho e a
dos nomeados até 06/02/2025 no seguinte — o assistente respondia só a metade
que a busca trazia. Na pergunta sobre o docente, nenhum trecho do FAQ chegou
entre os primeiros: a resposta cortada não dizia mais de que assunto tratava.

**A correção.** `scripts/dividir_faq.py` grava **um arquivo por pergunta** em
`<origem>/faq/<nome do FAQ>/NN-pergunta.md`, cada um com o título do FAQ e a
pergunta no topo, e tira o original da base (guardado em `faq-originais/`). A lógica fica
em `src/domain/faq.py`:

- reconhece os formatos do acervo: "9. …?", "2 - …?", "N. Tema" seguido de
  "Pergunta: …?", pergunta quebrada em várias linhas pelo PDF, título
  afirmativo sem "?" ("9 - Atestado devolvido sem ação do servidor") e sumário
  no início (descartado — ele casa com a busca e não tem resposta);
- a numeração precisa seguir em sequência (aceita pular um número), o que
  impede um inciso "2. sessenta dias" de virar pergunta;
- resposta maior que ~4.000 caracteres é dividida em partes nos parágrafos (ou
  nas frases), cada parte repetindo o título e a pergunta;
- se não reconhecer nenhuma pergunta, não envia nada.

O rótulo da fonte continua sendo o nome do FAQ (`fontes.rotulo_da_fonte` usa a
pasta), e o painel trata a pasta como **um** documento na conferência de
cópias. A página de FAQ do site é enviada já dividida pelo `ingest_site.py`
(`FAQ_PAGES`).

Divididos em 12/11/2026: estágio probatório (40 perguntas), Perícia Oficial
(31), FAQ NAPA `.docx` e `.pdf` (19 cada) e o FAQ do site (10). O FAQ de
adicionais ocupacionais (`.md`, 2,8 mil caracteres) já cabe num trecho.

**FAQ enviado pelo painel é dividido sozinho** (desde 12/11/2026). Ao
confirmar a indexação, a Lambda `progep-ifs-acervo` lê o texto
(`src/adapters/extracao.py`: PDF pela camada `progep-ifs-pypdf`, DOCX e HTML
pela biblioteca padrão) e decide se é FAQ (`faq.e_faq`):

- **com "FAQ" ou "Perguntas frequentes" no nome**: basta reconhecer 3
  perguntas;
- **sem isso no nome**: 5 perguntas ou mais, 70% terminando em "?" e 80% com
  resposta de pelo menos 40 caracteres. A última condição existe por causa de
  um falso positivo real: o checklist anexo à Resolução CS 348 ("Está no
  efetivo exercício? ( ) sim ( ) não"). Conferido no acervo inteiro (427
  documentos): nenhum outro falso positivo.

Sendo FAQ, entram na base os arquivos por pergunta em `cliente/faq/<nome>/`, e
o original fica guardado em `progep-ifs-staging/faq-originais/<pasta>/` (fora
de qualquer data source e da expiração da lixeira). Para o painel, o FAQ é
**um** documento: aparece numa linha ("FAQ · 40 perguntas"), e remover ou
substituir afeta todas as perguntas e o original. Reenviar o mesmo arquivo é
recusado como cópia idêntica (o `ETag` do original guardado). "Manter os dois"
com o mesmo nome de FAQ é recusado: as perguntas se misturariam na pasta.

A leitura tem prazo (12 s para nome de FAQ, 5 s para os demais; arquivo acima
de 10 MB ou de 2 MB, respectivamente, nem é lido), porque o API Gateway corta
em 29 s e o `pypdf` leva dezenas de segundos em alguns PDFs grandes. Se o nome
indica FAQ e as perguntas não foram reconhecidas, o documento entra inteiro e
a tela avisa para pedir a divisão à DNX.

**O script** serve para FAQ que entrou por outro caminho (repositório, site) ou
que o painel não conseguiu dividir:

```bash
python scripts/dividir_faq.py --chave "cliente/painel/<FAQ>.pdf" --dry-run --saida docs/faq
python scripts/dividir_faq.py --chave "cliente/painel/<FAQ>.pdf" --remover-original --kb-id INLYXJCFOW --ds-id I50FTDPEQ6
```

O `--dry-run` mostra quantas perguntas foram reconhecidas e grava os arquivos
para conferir. `DeleteKnowledgeBaseDocuments` é recusado enquanto houver
indexação em andamento; o script espera (até ~10 min) e tenta de novo.

---

## 5. Agente e API

### 5.1 Geração

`RetrieveAndGenerate` com **Claude Sonnet 4.5** (inference profile global) e um
`promptTemplate` próprio (`GENERATION_PROMPT_TEMPLATE`, em `src/adapters/agent.py`):
persona da PROGEP, proibição de comentar o processo de busca, e a sentinela
`NAO_ENCONTRADO` quando os trechos não respondem.

> **Cobertura parcial não é ausência de resposta.** A sentinela vale só quando
> os trechos não trazem nada sobre o assunto; havendo resposta pela metade, o
> modelo apresenta o que há e encerra com "Para os demais casos, recomendo
> confirmar com a PROGEP". Sem esse desempate a mesma pergunta oscilava entre
> `answered` e `not_found` entre execuções (ver seção 11).

> **Divergência entre documentos.** Quando os trechos discordam, o modelo usa a
> norma de data mais recente e prefere os documentos do IFS (cartilhas, INs,
> fluxos) aos manuais nacionais antigos, sem misturar as versões. Se o servidor
> contesta uma resposta, ele reconfirma só com os trechos, sem acrescentar
> regra. Isso não substitui a curadoria: versão desatualizada na base continua
> competindo na busca.

> O template **precisa** conter `$search_results$` e
> `$output_format_instructions$`. Sem o segundo a resposta pode vir sem
> citações — e é a citação que sustenta o grounding.

`is_no_answer()` normaliza a negativa para `not_found` mesmo quando ela vem
*com* citações.

**Por que Sonnet e não Haiku:** medido em 87 perguntas. Haiku é 1,2 s mais
rápido e empata em acertos (69 × 70), **mas** produziu 6 respostas comentando o
retrieval contra 0 do Sonnet, e escalou perguntas que o Sonnet respondeu com
fontes. O ganho de latência não compensa a regressão de qualidade.

### 5.2 Busca dupla e paralelismo

`forward_turn` dispara `Retrieve` e `RetrieveAndGenerate` **em paralelo**. Os
dois partem da mesma pergunta e não dependem um do outro: o primeiro traz os
scores (threshold e confiança), o segundo busca por conta própria por dentro.
Em série custavam ~0,7 s + ~5,2 s.

> **Trade-off assumido:** perdeu-se a garantia "nenhum LLM é invocado sem
> documento relevante". Como o threshold nunca corta, o custo medido é zero.

### 5.3 Continuidade de conversa

O assistente entende follow-ups: *"e quanto tempo antes preciso pedir?"* depois
de uma pergunta sobre licença capacitação.

**Como funciona:** o Bedrock mantém o histórico do lado dele, identificado por um
`sessionId`. Ele **recusa um id escolhido por nós** (`ValidationException`):
gera o próprio no primeiro turno e espera recebê-lo de volta. Por isso o
`ConversationStore` grava `conv#<canal>#<usuário> → sessionId` na tabela
`progep-ifs-sessions`.

**Janela de 20 minutos, contada de `session_saved_at`** (quando a sessão foi
salva) — **não** da última atividade, que a Lambda do WhatsApp grava *antes* de
chamar o runtime e faria toda sessão parecer recente. O TTL do item é de 24 h
(48 h quando houve atendimento humano) e serve só de coleta de lixo; quem decide
se o contexto é retomado é a janela. Se o Bedrock recusar a sessão
(`ValidationException ... Session ... is not valid`), a pergunta é refeita sem
contexto, em vez de falhar.

O mesmo item guarda o resto do estado da conversa: `last_activity` e `closed`
(encerramento por inatividade), `phone_number_id` (de qual número responder) e
os campos do transbordo (§6).

**Chave da conversa:** o número do remetente no WhatsApp (não o id da
mensagem — com ele cada turno viraria uma conversa nova).

**Custo:** ~0,6 s por pergunta, todo do lado do Bedrock (ele reformula a
pergunta com o histórico antes de buscar). O `ConversationStore` custa 0,08 s.

### 5.4 API do site (referência)

A API está publicada e funcionando, mas **não foi integrada ao site do IFS**: o
foco do projeto foi o WhatsApp. O guia para fazer a integração está em §5.5.

`POST https://57hy9et9yh.execute-api.us-east-1.amazonaws.com/prod/message`

| Header | Conteúdo |
|---|---|
| `Authorization` | **ID token** do Cognito (validado pelo authorizer do API Gateway) |
| `x-access-token` | **Access token** do mesmo login (validado pela Lambda via `get_user`) |
| `Content-Type` | `application/json` |

Corpo:

```json
{"message": "Como solicito licença capacitação?", "sessionId": "3f6c1e9a-…"}
```

- `message`: texto de 1 a 4.000 caracteres (só texto);
- `sessionId`: identificador da conversa, escolhido por quem chama. Repetido
  nas perguntas seguintes, dá ao assistente o contexto da conversa por 20
  minutos ("e para docentes?"). Sem ele, cada pergunta é independente.

| Situação | HTTP | Corpo |
|---|---|---|
| Resposta | 200 | `{"status": "ok", "content": "texto da resposta"}` |
| Sem resposta nos documentos | 200 | `{"status": "ok", "content": ""}` (ver §5.5, passo 5) |
| Pedido inválido (sem `message`, vazio, longo demais) | 400 | `{"status": "error", "error": {"code": "invalid_request", "message": "…"}}` |
| Token ausente, inválido ou vencido | 401 | `{"status": "error", "error": {"code": "unauthenticated", …}}` |
| Assistente indisponível ou sem resposta em 30 s | 503 | `{"status": "error", "error": {"code": "agent_unavailable", …}}` |
| O API Gateway desistiu antes da Lambda (29 s) | 504 | corpo do API Gateway |

**CORS:** o `POST` usa integração Lambda-proxy, então os cabeçalhos vêm da
função (`CORS_ALLOW_ORIGIN`). Há ainda um método `OPTIONS` (MOCK) para o
preflight e **gateway responses** para `UNAUTHORIZED`/`ACCESS_DENIED` — sem
elas o navegador veria falha de CORS em vez do 401 legítimo. Hoje a origem
permitida é `*`; restrinja ao domínio do site antes de abrir ao público:

```bash
pulumi config set progep-ifs:corsAllowOrigin https://www.ifs.edu.br
```

### 5.5 Guia de integração com o site

Passos para a equipe do site do IFS colocar o assistente numa página. O
assistente que responde é o mesmo do WhatsApp: mesma base, mesmo modelo, mesma
regra de não inventar.

**1. Escolher o modelo de acesso.**

| Modelo | Como funciona | Quando usar |
|---|---|---|
| **A. Pelo servidor do site** (recomendado) | O navegador conversa só com o servidor do site; o servidor do site faz login no Cognito com um **usuário técnico** e chama a API | Chat aberto a qualquer visitante da página |
| B. Direto do navegador | Cada pessoa tem usuário no Cognito, faz login na página e o navegador chama a API com os próprios tokens | Área restrita, com login dos servidores |

No modelo A a senha do usuário técnico fica só no servidor do site, nunca no
navegador. No modelo B cada servidor precisaria de uma conta no Cognito; o
caminho natural seria ligar o Cognito ao login institucional do IFS (federação
SAML ou OIDC), que o projeto não configurou.

**2. Pedir o usuário técnico** (modelo A). A DNX — ou quem administrar a conta
— cria o usuário **fora** do grupo `administradores`, então ele chama a API
mas não entra no painel do acervo:

```bash
aws cognito-idp admin-create-user --user-pool-id us-east-1_3YNaD3UOP --username site-ifs --message-action SUPPRESS --profile ifs
aws cognito-idp admin-set-user-password --user-pool-id us-east-1_3YNaD3UOP --username site-ifs --password '<senha forte>' --permanent --profile ifs
```

A senha vai para o cofre de segredos do servidor do site, nunca para o
código-fonte nem para o navegador.

**3. Obter os tokens.** Login com usuário e senha no app client
`4c21pvac1ma1a932m8fdqh7uum` (sem segredo de cliente), região `us-east-1`:

```bash
aws cognito-idp initiate-auth --client-id 4c21pvac1ma1a932m8fdqh7uum --auth-flow USER_PASSWORD_AUTH --auth-parameters USERNAME=site-ifs,PASSWORD='<senha>' --region us-east-1
```

A resposta traz `IdToken`, `AccessToken` (válidos por **1 hora**) e
`RefreshToken` (válido por **30 dias**). O servidor do site guarda os três em
memória e, perto de vencer, renova sem a senha:

```bash
aws cognito-idp initiate-auth --client-id 4c21pvac1ma1a932m8fdqh7uum --auth-flow REFRESH_TOKEN_AUTH --auth-parameters REFRESH_TOKEN='<refresh token>' --region us-east-1
```

Qualquer SDK da AWS faz o mesmo (`InitiateAuth` no Cognito Identity Provider);
em JavaScript também há a biblioteca `amazon-cognito-identity-js`, a mesma que
o painel do acervo usa (`frontend/src/autenticacao.ts`).

**4. Chamar a API.** Exemplo do servidor do site, em Node.js:

```javascript
const resposta = await fetch(
  "https://57hy9et9yh.execute-api.us-east-1.amazonaws.com/prod/message",
  {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: tokens.IdToken,
      "x-access-token": tokens.AccessToken,
    },
    body: JSON.stringify({ message: pergunta, sessionId: conversaId }),
    signal: AbortSignal.timeout(35_000),
  },
);
const corpo = await resposta.json();
```

O `sessionId` deve ser **um identificador aleatório por conversa** (um UUID
gerado quando o visitante abre o chat), repetido nas perguntas seguintes.
Nunca use CPF, e-mail ou outro dado pessoal como `sessionId`: ele fica
gravado nos registros técnicos.

**5. Tratar a resposta.**

- `200` com `content` preenchido: mostrar o texto. Ele vem em português, em
  parágrafos curtos, às vezes com listas.
- `200` com `content` vazio: o assistente **não encontrou** a resposta nos
  documentos. Mostrar uma mensagem própria, por exemplo: "Não encontrei essa
  informação nos documentos da PROGEP. Fale com a PROGEP: progep@ifs.edu.br,
  (79) 3711-1443." (contatos completos em `src/domain/contatos.py`).
- `401`: renovar os tokens (passo 3) e tentar **uma** vez de novo.
- `400`: mensagem vazia ou acima de 4.000 caracteres; validar antes de enviar.
- `503` ou `504`: "O assistente está indisponível no momento, tente de novo em
  instantes."

**6. Cuidados antes de abrir ao público.**

- **Limitar a frequência por visitante** no servidor do site (por exemplo, 10
  perguntas por minuto por IP): cada pergunta custa de US$ 0,02 a 0,04 sem as
  tarifas do WhatsApp (§8.6). Para uma proteção a mais na AWS, um *usage plan*
  no API Gateway ou uma regra de taxa no AWS WAF.
- **Restringir o CORS** ao domínio do site (§5.4); no modelo A, o navegador nem
  precisa chamar a API.
- **Aviso de privacidade** no chat: pedir que o visitante não informe CPF,
  matrícula ou dados de saúde, como no WhatsApp.
- **Só texto**, até 4.000 caracteres.

**7. Melhorias recomendadas na API** (para o site oferecer o mesmo que o
WhatsApp). Hoje a API devolve só o texto; o resultado do agente já traz o
necessário, falta repassar. Em `src/adapters/api_handler.py`, a última linha de
`handler` (`build_response(content=agent_response.content)`) recebe um
`AgentResponse` com `status` (`answered`/`not_found`), `sources` (documentos
usados) e `setores` (setor indicado quando não há resposta). O WhatsApp monta
isso em `lambda_entry._whatsapp_reply_text`, com `formatar_fontes`
(`src/domain/fontes.py`) e `formatar_contato` (`src/domain/contatos.py`).
Sugestão de corpo:

```json
{"status": "ok", "resultado": "not_found", "content": "",
 "fontes": [], "contato": "DAP — dap.progep@ifs.edu.br, (79) 3711-…"}
```

Mudar o envelope exige atualizar `ApiResponse` (`src/domain/api.py`) e os
testes de propriedade do envelope (`tests/property/test_p16_response_envelope.py`).

**8. Testar.** Com os tokens do passo 3:

```bash
curl -X POST "https://57hy9et9yh.execute-api.us-east-1.amazonaws.com/prod/message" -H "Content-Type: application/json" -H "Authorization: <IdToken>" -H "x-access-token: <AccessToken>" -d '{"message": "Quantos dias de férias o servidor tem?", "sessionId": "teste-1"}'
```

O uso pelo site aparece na aba "Custos" do painel junto com o do WhatsApp
(itens do assistente e "API do site e do painel").

### 5.6 Monitoramento

- **Tokens:** o `RetrieveAndGenerate` não devolve contagem. A fonte é o *model
  invocation logging* do Bedrock, no log group
  `/progep-ifs/bedrock/model-invocations`; dois metric filters extraem
  `InputTokens` e `OutputTokens` para o namespace `PROGEP-IFS/Bedrock`.
- **Respostas e falhas:** `MonitorAdapter` emite `Responses`/`ResponseLength`
  e `LlmCallFailures` em falha, com retry ≤3. Desde 03/10/2026 as métricas saem
  **sem dimensão** (antes havia uma série por sessão, cobrada por hora); a
  sessão fica na linha de log, com o telefone mascarado.
- **Custos:** aba "Custos" do painel (§8.6).
- **Latência:** cada invocação do runtime grava uma linha `[tempo]` com a
  decomposição (adapter / agente / monitor / total) e se o container foi
  reaproveitado.

---

## 6. Transbordo para atendimento humano

> **Ponte WhatsApp ↔ Connect validada em 22/09/2026** no número do IFS
> (+55 79 3711-1496): oferta, "sim", chat na fila, entrada do atendente,
> mensagens nos dois sentidos e aviso de fim enviado uma única vez.

### 6.1 O que existe

| Recurso | Nome |
|---|---|
| Instância Connect | `progep-ifs-progep` |
| Fila humana | `progep-ifs-progep-humano` |
| Perfil de roteamento | `progep-ifs-atendimento-progep` (canal **CHAT**, 2 simultâneos) |
| Horário | `progep-ifs-24x7` (enfileira 24×7, Req 3.4) |
| Contact flow do transbordo | `progep-ifs-transbordo-whatsapp` (só enfileira) |
| Tópico do streaming do chat | `progep-ifs-connect-chat-stream` |
| Lambda do streaming | `progep-ifs-connect-chat-stream` (`connect_chat_stream_handler`) |

> O perfil de roteamento declara **só o canal CHAT**. O transbordo do WhatsApp
> chega como contato de chat; declarar VOICE prometeria um atendimento por
> telefone que não existe, já que a instância não tem número de voz.

### 6.2 Quando o transbordo dispara

O assistente sinaliza escalonamento quando devolve `not_found` — ou seja,
quando **nenhum documento sustenta a resposta**, seja porque a pergunta está
fora do escopo da PROGEP, seja porque o corpus não cobre o assunto.

Também escala em `unavailable`, quando a chamada ao agente falha.

**No WhatsApp o transbordo é com confirmação** (decidido em 14/09/2026). Só o
`not_found` dispara, e só **oferece**: "Quer falar com um atendente da PROGEP?
Responda sim ou não". Transferir automaticamente encheria a fila com pergunta
fora do escopo ("cardápio do restaurante"), que também volta `not_found`. O
`unavailable` não oferece atendente: é falha nossa, não falta de documento.

**Contatos dos setores (pedido do cliente em 28/09/2026).** Os departamentos
não dão conta de atender todo "não encontrei", então o assistente passou a
**indicar sempre o contato do setor que costuma tratar o assunto**, e a oferta
de atendente virou opcional, anexada ao contato só no horário de atendimento:

| Classificação | O que o servidor recebe |
|---|---|
| Setor da PROGEP (DAP, DSDPES, DNAGS, NPG, CPSPP) | "Não encontrei…" + e-mail e telefone do setor (+ oferta de atendente no horário) |
| Saúde (perícia, licença para tratamento) | contatos do **SIASS e da DAP** |
| PROGEP sem setor claro, ou classificação falhou | contato geral da PROGEP |
| Fora da gestão de pessoas (cardápio, matrícula…) | aviso + "quer a lista de contatos?" (sem oferta de atendente) |

A classificação é uma chamada curta ao mesmo modelo da geração
(`BedrockAgentAdapter.classificar_setor`, `bedrock-runtime` Converse com a
anotação de billing), **só nos `not_found`**, feita no runtime e devolvida no
campo `setores`. Os assuntos de cada setor vêm do "Setor responsável" dos
fluxogramas (`docs/fluxos`): DAP 33 processos, DNAGS 31, DSDPES 25, PGD 4. Os
contatos ficam em `src/domain/contatos.py` — trocar um ramal é editar só ali.
A CPSPP não aparece em nenhum fluxo; os assuntos dela no classificador vêm
do nome do setor. Se as coordenadorias ganharem contato próprio, basta
acrescentá-las ao catálogo.

Regras (`src/domain/transbordo.py`):

- **expediente:** segunda a sexta, 9h–18h (UTC−3 fixo; feriados não contam).
  Fora dele, o servidor recebe o horário em vez da oferta. O expediente é
  conferido de novo no "sim" — oferta às 17h55, aceite às 18h05 não abre;
- **a resposta precisa ser inteira sim ou não.** "Não, mas como peço férias?"
  descarta a oferta e vai ao assistente como pergunta nova;
- **"ok" e "sim" são aceite**, não cortesia, enquanto há oferta pendente;
- a oferta vence com a janela da conversa (20 min).

### 6.3 Desenho do fluxo

O contact flow `progep-ifs-transbordo-whatsapp` **só enfileira**: define a fila
humana da PROGEP e transfere. Não chama o assistente (quem pediu uma pessoa já
conversou com ele) e não manda mensagem — o que o fluxo escreve chega pelo
streaming com papel `SYSTEM`, em inglês, e a ponte descarta; os avisos ao
servidor saem da Lambda.

O JSON do fluxo é **gerado por código** (`src/infra/connect_flow.py`), não
desenhado no console — reprodutível e auditável (Req 9.1). Até 02/10/2026
existia também o fluxo `assistente-progep` do desenho original (§7.5).

> Pegadinhas do schema do Connect já tratadas: todo `Transitions` precisa de
> `Errors` **e** `Conditions`; `TransferContactToQueue` não aceita
> `Parameters`. Num fluxo com captura de entrada, `GetParticipantInput` exige
> `InputValidation` e rejeita o ErrorType `InputTimeLimitExceeded`. O erro do
> provider vem vazio — use `aws connect create-contact-flow
> --cli-error-format json` para ver o campo `problems`.

### 6.4 Mudança de arquitetura do canal WhatsApp (decidida em 11/09/2026)

O **Req 6.1** dizia que a mensagem recebida no número oficial é encaminhada ao
Agente_IA **através do Amazon Connect** — ou seja, todo turno abria um contato
de chat, mesmo os que o assistente resolve sozinho. **Isso foi alterado.**

Desenho novo:

```
WhatsApp → AWS End User Messaging Social → SNS → Agente_IA
                                                    └→ Connect só no transbordo
```

**Por que mudou.** O Connect cobra por contato de chat, e a esmagadora maioria
das conversas nunca precisa de humano — pagar contato para "quantos dias de
férias tenho" é custo sem contrapartida, além da latência do salto extra. O
usuário recebe exatamente a mesma coisa nos dois desenhos.

**O que o Req 6.1 passa a exigir:** a mensagem recebida no número oficial é
encaminhada ao Agente_IA, e o Amazon Connect é acionado **quando há
escalonamento** (§6.2). Os demais requisitos do grupo 6 seguem inalterados.

> **Estado do código:** `src/adapters/whatsapp_connector.py` e
> `src/flows/whatsapp_flow.py` são do desenho original (Connect em toda
> mensagem) e **não estão em produção**; só os testes da especificação os usam.
> O canal em produção é `lambda_entry.whatsapp_social_handler`.

**Número de teste da Meta: depende de onde ele foi criado (corrigido em
14/09/2026).** Alguns números de teste **podem** ser vinculados ao End User
Messaging Social — há um vinculado nesta conta AWS, que é compartilhada com
outros dois projetos do IFS (`den` e `diae`). Mas **o nosso não pode**: no
*embedded signup*, o `+1 555 176-1308` aparece como **"Não qualificado"**, com o
motivo *"Este número não pode ser compartilhado com este app"*. Ele está preso
ao app `projeto-dev`, onde foi gerado, e desconectá-lo não o libera.

**Como obter um número elegível:** no *embedded signup*, escolher **"Usar um
nome de exibição com um número virtual"**. A Meta gera um número novo
("Número gerado pelo WhatsApp") que já nasce compartilhável com a AWS, numa
**WABA nova**. Foi assim que o `+1 555-379-4876` entrou (vinculado em
14/09/2026; WABA AWS `waba-d67d85459cd845d195a78da19a4a2d2a`, Meta ID
`1718007719797905`). Por estar em WABA própria, separada da do `176-1308`, não
há risco de as duas entradas responderem ao mesmo número.

Três armadilhas do fluxo, todas vividas na primeira tentativa:

1. **País do número.** Ao reaproveitar o número virtual, o pop-up pode
   prefixar **+55** e pedir código por SMS para um número inexistente
   (`+5515553794876`). Corrija o país para **+1**. Número virtual não recebe
   SMS nem ligação — se a tela pedir código, o caminho está errado.
2. **O clique final é na AWS, não na Meta.** Quando o pop-up fecha, a Meta já
   mostra "Conta conectada a Amazon Social Messaging", mas **a AWS só registra
   a WABA** depois de **Add phone number** na página do console. Sem ele, cada
   lado fica esperando o outro.
3. **O campo "Phone number verification" é um PIN, não código SMS.** É o PIN
   de verificação em duas etapas do número: você cria 6 dígitos e guarda num
   gerenciador de senhas — ele é exigido para re-registrar ou migrar o número.

Na mesma página dá para marcar **Message and event publishing** e colar o ARN
do tópico, o que dispensa o passo separado de *Event destination*.

> **O destino precisa de `+`.** A Graph API aceita o número sem o sinal, e é
> assim que a Meta entrega o remetente (`557900001234`). O `SendWhatsAppMessage`
> **recusa** esse formato com `Invalid destination phone number or message Id`,
> e aceita `+557900001234` — sem precisar do nono dígito do celular. O sintoma é
> enganoso: o "digitando…" aparece (ele não tem campo de destinatário) e a
> resposta nunca chega. `WhatsAppSocialClient.send_text` normaliza para E.164.
>
> Segundos após o vínculo, a conta oficial do WhatsApp envia uma notificação de
> "continue a configuração". O assistente a trata como pergunta e tenta
> responder; a falha de envio que aparece no log nessa hora é o mesmo defeito
> do `+`, e não uma recusa da conta da Meta.

> No *embedded signup* a lista de números mostra as WABAs de **todos** os
> projetos do portfólio. Selecionar o número de outro projeto é possível e
> desviaria as mensagens dele para o nosso tópico e o nosso assistente — o
> critério seguro é o número, nunca só o rótulo "Test Number".

`SendWhatsAppMessage` recebe o corpo como **blob** — o mesmo JSON que hoje
mandamos à Graph API. Consequências: o indicador de "digitando" continua
funcionando (é o mesmo payload), e o access token, o app secret e a validação
de assinatura HMAC **deixam de existir**, porque a AWS passa a autenticar.

### 6.5 Como funciona a ponte WhatsApp ↔ Connect

```
servidor → WhatsApp → SNS → Lambda whatsapp-social ─ SendMessage → chat do Connect
atendente → chat do Connect ─ streaming → SNS → Lambda connect-chat-stream → WhatsApp
```

Componentes: `lambda_entry.whatsapp_social_handler` (entrada),
`lambda_entry.processar_mensagens_whatsapp` (atendimento e oferta),
`src/adapters/connect_chat.py` (abertura do chat e envio ao atendente) e
`lambda_entry.connect_chat_stream_handler` (volta do atendente).

Estados da conversa (item `conv#whatsapp#<telefone>` da tabela de sessões,
atributo `transbordo`):

| Estado | Quem recebe a mensagem do servidor |
|---|---|
| vazio | assistente |
| `oferecido` | classificador sim/não da oferta de atendente; outra coisa vai ao assistente |
| `lista` | classificador sim/não da oferta da lista de contatos (pergunta fora da PROGEP) |
| `humano` | chat do Connect (sem "digitando", sem cortesias) |

Ao aceitar, a Lambda chama, **nesta ordem**, `StartChatContact` (fluxo
`progep-ifs-transbordo-whatsapp`, chat de 24 h), `StartContactStreaming`
(tópico `progep-ifs-connect-chat-stream`) e `CreateParticipantConnection`;
depois manda ao chat a pergunta que o assistente não respondeu. O atendente vê
o servidor como "Servidor WhatsApp (…1234)"; o número completo e a pergunta
ficam nos atributos do contato. Um índice reverso `atend#<ContactId>` liga o
contato ao telefone.

Da volta, só quatro coisas chegam ao WhatsApp: fala do atendente, entrada dele
("👤 Nome, da PROGEP, entrou na conversa"), aviso de anexo que não pode ser
entregue e fim do atendimento. O eco das mensagens do servidor e as do sistema
(papel `SYSTEM`, em inglês) são descartados.

Comportamentos tratados:
- o fim chega em **dois** eventos (`participant.left` do atendente, depois
  `chat.ended`); a gravação condicional em `contato_id` faz o aviso sair uma
  vez;
- o connection token expira antes do chat e é renovado com o participant
  token. Se nem o renovado é aceito, o chat acabou sem aviso e a mensagem volta
  ao assistente;
- a varredura de inatividade **ignora** conversas em estado `humano`;
- a Participant API não usa IAM — os tokens autorizam, então ficam só no
  DynamoDB (TTL 48 h) e nunca em log.

**Desligar o transbordo** (ficar só com os contatos dos setores): remova as
variáveis `CONNECT_INSTANCE_ID`, `CONNECT_TRANSFER_FLOW_ID` e
`CONNECT_STREAM_TOPIC_ARN` das Lambdas do WhatsApp (`transbordo_env` em
`src/infra/resources.py`) e rode `pulumi up`. Sem elas a ponte não é criada e o
"não encontrei" leva só o contato do setor.

**Como testar** (dentro do expediente, com o painel do atendente aberto e o
status *Available*):
1. mandar uma pergunta da PROGEP que a base não responde ("meu pedido de
   remoção para o campus Lagarto já foi analisado?") → deve vir o contato do
   setor e a oferta de atendente;
2. responder "sim" → "Transferi sua conversa…"; o chat aparece no painel com a
   pergunta;
3. aceitar → "👤 Nome, da PROGEP, entrou na conversa" no WhatsApp;
4. conversar nos dois sentidos;
5. encerrar → "O atendimento com a PROGEP foi encerrado" **uma vez**;
6. mandar uma pergunta nova → o assistente volta a responder.

Variações: responder "não" (recusa sem chamar o assistente); responder com
outra pergunta (a oferta é descartada); testar fora do expediente (só o
contato); perguntar algo fora da gestão de pessoas ("qual o cardápio do
restaurante?") → oferta da lista de contatos, sem atendente.

### 6.6 Criar atendentes

O atendente **não precisa de acesso à conta AWS**: só de um usuário do próprio
Connect (a instância é `CONNECT_MANAGED`) e de um navegador suportado (Chrome,
Firefox ou Edge atualizados).

1. Entre no Connect como administrador: https://progep-ifs-progep.my.connect.aws
   (ou, sem senha, console AWS → Amazon Connect → `progep-ifs-progep` →
   Overview → *Log in for emergency access*).
2. **Users → User management → Add new users.**
3. Login `nome.sobrenome`, senha inicial (troca obrigatória no primeiro acesso),
   **routing profile `progep-ifs-atendimento-progep`** (sem ele os chats da
   PROGEP não chegam) e **security profile `Agent`**.
4. Envie ao atendente o login, a senha e o painel:
   https://progep-ifs-progep.my.connect.aws/agent-app-v2/

Pela CLI: `aws connect create-user` com routing profile
`4551b88d-dd0d-4f28-a485-12bdca1b2bfe` e security profile
`5d49d2e1-5401-47fc-abb4-024449ad1911`. Gere a senha no próprio script e
grave-a só em arquivo local — nunca em log ou chat.

No painel: status **Available** para receber chats; cada atendente recebe até 2
simultâneos; depois de encerrar, o contato fica em *After chat work* até
**Close contact**. Senha esquecida: redefinir em *User management* pelo acesso
de emergência. Não há custo por atendente, só por mensagem.

---

## 7. WhatsApp e Meta — guia operacional

O canal passa pelo **AWS End User Messaging Social** (a AWS é o provedor
oficial — BSP — perante a Meta). Não há token da Meta no caminho de produção:
quem autentica é a AWS. O histórico da decisão está em §6.4.

### 7.1 Números e contas

| Número | Uso | Conta Meta (WABA) | IDs |
|---|---|---|---|
| **+55 79 3711-1496** | Produção | "IFS - PROGEP" (Meta `1383291770109148`), portfólio **IFS - DTI** (empresa verificada, limite 2.000) | AWS WABA `waba-8e94ffc153c4419d8e4d5aa76f53a315`; AWS número `phone-number-id-1090405d46ea49eeac373888a23ade70`; Meta número `1277914255409615` |
| +1 555-379-4876 | Teste (número virtual 555) | "Projeto dev" (Meta `1718007719797905`), portfólio da DNX (não verificado) | AWS WABA `waba-d67d85459cd845d195a78da19a4a2d2a`; Meta número `1319875041208726` |
| +1 555 176-1308 | Desativado (webhook removido em 02/10/2026, §7.5) | "Test WhatsApp Business Account" | — |

A conta AWS é **compartilhada** com os projetos `den` e `diae` do IFS. No
embedded signup aparecem números de outros projetos (ex.: "DNX lab" +1
555-452-5306, "Test Number" do Licenciamento ambiental). **Selecionar um deles
desviaria as mensagens para o nosso assistente** — o critério é o número, nunca
o rótulo. Os números 3711-1497 e 3711-1498 do IFS não estão vinculados a nós.

### 7.2 Vincular um número

1. Console AWS → **End User Messaging Social** → *Add WhatsApp phone number*.
2. No pop-up da Meta, escolha o **portfólio** dono do número e a conta do
   WhatsApp (ou crie uma).
3. Número:
   - **próprio:** informe o número e escolha SMS ou **ligação** (fixo não
     recebe SMS; URA/atendimento automático derruba a ligação antes do código).
     O número **não pode estar em uso** no WhatsApp comum/Business do celular;
   - **virtual 555 (teste):** "Usar um nome de exibição com um número
     virtual". Confira o país **+1** — o pop-up às vezes prefixa +55 e pede um
     SMS que nunca chega.
4. Crie o **PIN de verificação em duas etapas** (6 dígitos) e guarde num
   gerenciador de senhas: ele é exigido para registrar o número de novo.
5. **Volte à página da AWS e clique em *Add phone number*.** A Meta já mostra
   "conectado", mas a AWS só registra a conta depois desse clique.
6. Em *Message and event publishing*, informe o tópico
   `arn:aws:sns:us-east-1:342016537070:progep-ifs-whatsapp-inbound`.
7. Confira: `aws socialmessaging get-linked-whatsapp-business-account --id <waba>`
   deve mostrar `eventDestinations` com o tópico. Mande uma mensagem e veja o
   log da `progep-ifs-whatsapp-social`.

### 7.3 Nome de exibição

Trocar: WhatsApp Manager → Números de telefone → o número → **Perfil** → Nome
de exibição → **Editar**. O nome precisa ter relação clara com a empresa do
portfólio e grafia normal (a Meta recusa tudo em maiúsculas). Número próprio
com empresa verificada **continua enviando** durante a análise.

**Depois da aprovação** (notificação "O nome de exibição … já pode ser usado"),
o nome **só entra em uso após registrar o número de novo**:

1. [Graph API Explorer](https://developers.facebook.com/tools/explorer/),
   app `projeto-dev`, *User Token*, permissões `whatsapp_business_management` e
   `whatsapp_business_messaging`. *Generate Access Token* e, na janela, marque
   a conta do WhatsApp do número.
2. Conferência: `GET {meta-número}?fields=verified_name,new_display_name`.
3. Registro: `POST {meta-número}/register` com `messaging_product=whatsapp` e
   `pin=<PIN>` → `{"success": true}`.
4. Limite: **10 registros por número a cada 72 h** (erro 133016 bloqueia por 3
   dias). Nunca use `deregister`.

> **Número de produção.** O nome "IFS - Progep" foi aprovado pela Meta em
> 29/09/2026 para o +55 79 3711-1496. O número pertence ao portfólio do IFS, e
> o app do Explorer é da DNX: nesse caso o token pode enxergar a conta
> `1383291770109148` mas **não** os números dela (`phone_numbers` vazio; `GET`
> no número dá erro 100/33), mesmo com acesso total ao portfólio. Para fazer o
> registro, nesta ordem: incluir a permissão `business_management` no token;
> atribuir a pessoa ao ativo "IFS - PROGEP" com controle total; ou pedir ao
> administrador do portfólio do IFS (`dti@ifs.edu.br`) que faça o `register`.

**Números 555 (virtuais):** só enviam com nome **aprovado**; sem isso, toda
mensagem é recusada com **131037**, mesmo com `name_status:
AVAILABLE_WITHOUT_REVIEW`. O suporte da Meta não atende WABA gerenciada por BSP
e manda abrir chamado pela AWS, o que exige plano de suporte pago. Para testes,
prefira número próprio.

### 7.4 Envio, entrega e cobrança

- `SendWhatsAppMessage` exige o destino em **E.164 com `+`**; a Meta entrega o
  remetente sem o `+` (`WhatsAppSocialClient.send_text` normaliza).
- **Aceite da AWS não é entrega.** A recusa da Meta chega segundos depois como
  status `failed` no mesmo tópico; a Lambda loga `[entrega] FALHOU … erros=[…]`
  (código e detalhe da Meta) e `[entrega] sent|delivered|read`.
- **Entrega "pelo menos uma vez":** cada mensagem recebida é marcada
  (`msg#<wamid>`, condicional, TTL 24 h) e a repetição é ignorada; entrada
  logada como `[entrada] msg=<wamid>` (sem texto).
- **Janela de 24 h:** respostas a quem escreveu nas últimas 24 h são mensagens
  de serviço. Desde 01/10/2026 a Meta cobra cerca de US$ 0,0068 por mensagem de
  serviço entregue acima de **1.000 por número por mês** (órgão público
  elegível pode ser isento; não confirmado para o IFS). A AWS cobra US$ 0,005
  por mensagem enviada e US$ 0,001 por recebida. Iniciar conversa fora da
  janela exige template (pago). O consumo real aparece na aba "Custos" (§8.6).

### 7.5 Legado removido (02/10/2026)

- **Webhook interino** do número de teste antigo: Lambda
  `progep-ifs-whatsapp-webhook` + Function URL pública. Com `app_secret` vazio
  no segredo, aceitava POST sem assinatura (qualquer um com o link disparava o
  agente). Saiu junto com `src/adapters/whatsapp_cloud.py`; o leitor do payload
  da Meta, que o End User Messaging repassa, foi para `whatsapp_social.py`.
- **Fluxo antigo do Connect** do desenho original: Lambda `progep-ifs-connect`
  (`connect_handler`) e contact flow `progep-ifs-assistente-progep`, que
  chamava o assistente a cada mensagem pelo Connect. Nada o usava; o transbordo
  usa `progep-ifs-transbordo-whatsapp`.

O segredo `progep-ifs/whatsapp` **fica**, só para o diagnóstico manual (§7.6);
nenhuma Lambda o lê mais.

### 7.6 Diagnóstico pela Graph API

O `access_token` do segredo `progep-ifs/whatsapp` (usuário de sistema "admin"
da DNX) alcança as contas do portfólio da DNX, **não** as do IFS. Útil:

- `GET {meta-número}?fields=verified_name,name_status,new_name_status,new_display_name,health_status`
  — `health_status.can_send_message` e o motivo do bloqueio;
- `GET {meta-waba}?fields=analytics.start(<epoch>).end(<epoch>).granularity(DAY)`
  — enviadas e entregues por dia.

A Graph API não guarda histórico de erros de entrega: esses só existem nos logs
`[entrega]`.

---

## 8. Painel do acervo

**Endereço:** https://d2ush4hjiv6t7m.cloudfront.net — React (`frontend/`) servido
pelo CloudFront a partir do bucket privado `progep-ifs-painel` (OAC). A API é o
mesmo API Gateway do site, rotas `/acervo/*`, com authorizer Cognito e a Lambda
`progep-ifs-acervo`.

### 8.1 Acesso

Só usuários do grupo Cognito **`administradores`** alteram o acervo e veem a
aba "Custos". A inclusão no grupo é manual, de propósito: quem mexe no acervo é
decisão da PROGEP. Os pedidos de acesso chegam à DNX, que cria o usuário com os
comandos abaixo.

```bash
aws cognito-idp admin-create-user --user-pool-id us-east-1_3YNaD3UOP --username <email> --user-attributes Name=email,Value=<email> Name=email_verified,Value=true --profile ifs
aws cognito-idp admin-add-user-to-group --user-pool-id us-east-1_3YNaD3UOP --username <email> --group-name administradores --profile ifs
```

O Cognito envia a senha temporária por e-mail; o primeiro login pede a troca.
Se o usuário não conseguir entrar, confira no CloudTrail se o erro é de senha
(e reenvie o convite com `--message-action RESEND`). Antes da produção, remova o
usuário `teste-acervo`.

### 8.2 Fluxo de um documento

1. **Envio:** o navegador pede uma URL assinada (`POST /acervo/uploads`) e sobe
   o arquivo direto para `progep-ifs-staging/preparo/`. Formatos: pdf, docx,
   txt, html, md; até 50 MB.
2. **Revisão:** o documento fica em preparo (`GET/DELETE /acervo/preparo`),
   com título editável.
3. **Indexação:** `POST /acervo/indexar` copia para
   `progep-ifs-documents/cliente/painel/…`; o `ObjectCreated` aciona o Ingestor
   e a KB indexa em alguns minutos. **Antes da cópia, a conferência de cópias**
   (`acervo.conflitos_do_envio` / `decidir_conflito`), comparando o `ETag`
   (MD5) e o nome-base (sem pasta, extensão, acento e sufixo "(1)") com todo o
   acervo:
   - **conteúdo idêntico** a algo do acervo → HTTP 409, não entra (não há
     opção de forçar: a cópia só ocuparia vagas na busca);
   - **mesmo nome**, conteúdo diferente → HTTP 409 com a lista; a tela
     pergunta e reenvia com `acao: "substituir"` (as antigas vão para a
     lixeira **depois** que a nova entrou) ou `acao: "manter_ambos"`. Só
     podem ser substituídas chaves que estão na lista do conflito.
4. **Remoção** (`DELETE /acervo/documentos`) vale para **qualquer** documento da
   base, não só os do painel: copia para `progep-ifs-staging/removidos/<chave
   original>` (30 dias), apaga do bucket e chama `DeleteKnowledgeBaseDocuments`
   (em lotes de 10). Auditado em 29/09/2026: os vetores saem do índice por
   completo. A chave pode ser a pasta de um FAQ dividido (§4.5).

   **Remoção durante indexação.** `DeleteKnowledgeBaseDocuments` é recusado
   ("running ingestion job") enquanto houver indexação no data source — comum
   logo depois de um envio e certo numa substituição. O painel não espera: o
   arquivo já está na lixeira, a resposta sai com `remocao_pendente: true`, e
   a Lambda invoca a si mesma de forma assíncrona (`_tarefa_interna`) para
   tirar do índice quando a indexação acabar. Ela espera até o fim do próprio
   tempo (300 s) e, se não deu, falha de propósito para a Lambda tentar de
   novo (2 vezes, `FunctionEventInvokeConfig`). Documento que voltou ao bucket
   nesse meio-tempo não é tirado da base. O evento interno só é aceito sem
   `httpMethod`, então não é alcançável pela API.
5. **Restaurar:** copiar de `removidos/<chave>` de volta para a chave original
   no bucket de documentos (o Ingestor reindexa).

> Documentos em `site/` voltam se `ingest_site.py` for rodado de novo — o
> painel mostra a procedência para deixar isso claro.

### 8.3 Conferência

`GET /acervo/conferencia` compara o bucket com a KB: **ausentes** (no bucket,
não indexados — em geral PDF escaneado; ver `ocr_documentos.py`) e **órfãos**.
A listagem da KB é lenta (~25 itens por página, 5–7 s para 400 documentos); a
lista de documentos do painel vem do S3.

`GET /acervo/duplicatas` lista as cópias que **já estão** no acervo, no topo
da aba "Documentos na base": grupos de conteúdo idêntico (qualquer origem) e
grupos de mesmo nome entre documentos curados (repositório e painel). Ficam
fora da comparação por nome as páginas do site (repetem nome por natureza, ex.:
`concursos/2024.html` e `remocao-interna/2024.html`) e as transcrições em
`cliente/ocr/`, que têm o nome do PDF de propósito.

> Os 22 fluxogramas em PDF enviados pela PROGEP em 30/09/2026
> (`cliente/painel/PDF - …`) são imagens e aparecem como "não responde" na
> conferência. **Não é falha**: todos já estão transcritos em
> `site/mapeamento-processos/` (a partir dos PNGs do site, mesmo conteúdo).

### 8.4 Publicar o painel

```bash
python scripts/publicar_painel.py
```

Faz o build, escreve o `config.json` com os outputs do Pulumi, sobe com
cabeçalhos de cache e invalida o CloudFront. O upload direto exige CORS no
bucket de preparo (POST das origens CloudFront e `localhost:5173`); sem ele, o
arquivo sobe mas o navegador acusa "Failed to fetch".

### 8.5 Armadilhas já vividas

- O authorizer do Cognito entrega `cognito:groups` como texto (`"a"` ou
  `"[a, b]"`), não lista — `acervo.exige_admin` trata os dois.
- A API usa o **ID token** do Cognito.
- Ao criar rotas no API Gateway, mude o `triggers` do deployment no Pulumi, ou
  a rota nova não é publicada no stage.
- Nomes de arquivo do portal vêm com acento codificado como `-C3-A7`
  (`fontes.decodificar_nome` corrige).

### 8.6 Aba "Custos" (controle de custos)

Mostra quanto o assistente gastou hoje, nos últimos 7 e 30 dias e no mês,
com projeção de fechamento, custo por pergunta, cota grátis da Meta e
detalhamento por item. Só o grupo `administradores` vê (rota
`GET /acervo/custos?periodo=hoje|7d|30d|mes|mes_anterior`, mesma Lambda do
acervo, que só **lê** a tabela). A tela se atualiza sozinha a cada minuto. A
explicação para o cliente, item a item, está em
[custos-assistente-progep.md](custos-assistente-progep.md).

**Projeção do mês:** o gasto até hoje + a média diária dos últimos 30 dias
fechados × os dias que faltam. Usar só os dias do mês corrente fazia um único
dia atípico (bateria de testes no dia 1º) virar a projeção do mês inteiro.

**O valor é medido, não estimado:** cada consumo real × preço público
(`src/domain/custos.py:PRECOS`, conferidos em 02/10/2026). Conferido contra a
fatura de setembro: o modelo Claude bateu **ao centavo, dia a dia**
(US$ 1,4586 medido = faturado).

**Só a PROGEP.** A conta é compartilhada com `den` e `diae`, então a atribuição
é pelo recurso que consumiu, nunca pelo total do serviço na fatura:

| Item | Fonte | Atraso |
|---|---|---|
| Modelo, busca, indexação, equipe técnica | log de invocações do Bedrock, pela identidade (`custos.item_da_invocacao`): role `progep-ifs-agentcore-exec` = assistente; role `progep-ifs-kb-*` com sessão `BKB-Retrieve` = busca, outra sessão = indexação; `DNXAccess/<e-mail>` listado em `custosEquipeTecnica` = transcrições e testes | 1–5 min |
| WhatsApp (AWS e Meta) | linhas `[entrada]` e `[entrega] sent/delivered` da Lambda `whatsapp-social`; a tarifa da Meta só acima de 1.000 entregues no mês (contador por mês UTC) | segundos |
| Atendimento humano | linha `[chat] mensagem` (Connect cobra cada mensagem do chat) | segundos |
| Execução (AgentCore) e perguntas | métricas `CPUUsed-vCPUHours`, `MemoryUsed-GBHours` e `Invocations` do nosso runtime | 1 h |
| Lambda, DynamoDB, API Gateway, logs | métricas por função/tabela/API/log group com prefixo `progep-ifs` | 1 h |
| Armazenamento, segredos | tamanho dos buckets, imagens do ECR, vetores (6 KB cada) e 2 segredos, rateados por dia | 1 h |
| CloudTrail | fatura (Cost Explorer); a única trilha paga da conta é a `progep-ifs-trail` | até 24 h |

Peças: tabela `progep-ifs-custos` (`pk = custo#AAAA-MM`, `sk = dia#categoria#item`);
Lambda `progep-ifs-custos-logs`, assinada nos três log groups
(`CUSTOS_ASSINATURAS` em `resources.py`), que **soma** com marca por evento
contra reentrega; Lambda `progep-ifs-custos-coleta`, de hora em hora, que
**substitui** os itens de métrica (idempotente) e lê a fatura a cada 12 h. O
controle custa centavos por mês, e ele mesmo aparece na tabela ("Este controle
de custos").

Configuração:

```bash
pulumi config set progep-ifs:custosEquipeTecnica <e-mail1>,<e-mail2>
# quando a Meta confirmar a isenção de órgão público:
pulumi config set progep-ifs:custosMetaIsento true
```

Histórico anterior ao deploy (reprocessa logs e métricas com as mesmas funções;
pode rodar de novo sem duplicar):

```bash
python scripts/custos_retroativo.py --inicio 2026-09-01 --simular
python scripts/custos_retroativo.py --inicio 2026-09-01
```

Limites conhecidos:
- Mensagens **recebidas** antes de 02/10/2026 não entram: a linha `[entrada]`
  era INFO, e o runtime da Lambda deixa a raiz em WARNING, então ela **nunca
  chegou ao CloudWatch** (corrigido com `logger.setLevel(INFO)` em
  `lambda_entry` e `connect_chat`). São US$ 0,001 cada.
- Métrica personalizada é cobrada por hora com dado. Até 02/10/2026 o Monitor
  de Consumo publicava 2 séries **por sessão** (dimensão `SessionId`: 186
  séries em duas semanas), e o controle conta 1 hora por pergunta nesses dias
  (o teto). A partir de 03/10 o Monitor publica sem dimensão (a sessão fica na
  linha de log) e o custo passa a depender das horas com uso, não do número de
  conversas (`MONITOR_SEM_SESSAO_DESDE` em `custos_handler.py`).
- Serviços que ficam na faixa gratuita ou abaixo de um centavo por mês
  (Cognito, CloudFront, SNS) não aparecem.

---

## 9. Testes

**769 testes** automatizados: `python -m pytest`.

| Camada | O que cobre |
|---|---|
| `tests/property/` | Invariantes do domínio (Hypothesis) |
| `tests/unit/` | Adapters e flows com fakes injetados |
| `tests/integration/` | Adapters contra AWS simulada (moto) |

### 9.1 Baterias de qualidade (contra o ambiente real)

Três conjuntos de perguntas rotuladas com o status esperado
(`answered`, `not_found` ou `any` para inspeção manual):

| Arquivo | Foco | Resultado |
|---|---|---|
| `perguntas_teste.json` | Temas gerais de RH | 23–24/24 |
| `perguntas_processos.json` | Trâmite dos processos | 19/20 |
| `perguntas_repositorio.json` | Legislação federal | 30/32 |

```bash
python scripts/run_qa_suite.py --runtime-arn <arn> --questions scripts/perguntas_repositorio.json --out relatorio.md
```

As baterias incluem **armadilhas de precisão** de propósito: pedir um percentual
que depende de laudo individual, pedir orientação jurídica, pedir valor que muda
por portaria. Nesses casos o critério não é acertar — é **não inventar**.

### 9.2 Continuidade de conversa

```bash
python scripts/testa_conversa.py --out conversa.md
```

Roda diálogos com follow-ups dependentes de contexto. Último resultado: **5/5**.

### 9.3 Falha conhecida na suíte

`TestMonitorAdapterIntegration::test_token_usage_and_response_metrics_are_emitted`
falha com `KeyError: 'Metrics'`. É incompatibilidade entre `moto 5.0.24` e
`botocore 1.43.69` (reproduzível fora do projeto), **não** regressão do código.
Conserto provável: subir o moto.

---

## 10. Passo a passo de execução

### 10.1 Pré-requisitos

- Python ≥ 3.10, Pulumi, AWS CLI, Docker Desktop (só para o AgentCore)
- `pip install -e ".[dev]"`

### 10.2 Credenciais

```bash
aws login --profile ifs
```

**Sempre confirme a conta antes de qualquer operação em lote** — existem outros
perfis na máquina, incluindo um `ifpa` que é outra instituição:

```bash
aws sts get-caller-identity --profile ifs
```

Deve responder `342016537070`. Para scripts Python:

```bash
aws configure export-credentials --profile ifs --format powershell | Invoke-Expression
```

> As credenciais duram ~15–20 min. Operações longas (transcrição dos 94
> fluxogramas, baterias completas) **vão** expirar no meio — por isso os
> scripts são retomáveis.

### 10.3 Infraestrutura

```bash
pulumi up
```
(exige `PULUMI_CONFIG_PASSPHRASE=progep-ifs-local` e o **backend local**: se o
`pulumi login` estiver apontando para a nuvem por causa de outro projeto, a
stack `prod` "some" — rode `pulumi login --local`.)

O `pulumi up` publica o código de **todas as Lambdas** (WhatsApp, API, painel,
Ingestor, custos). Mudanças em `agentcore/` ou no agente (`src/adapters/agent.py`,
prompt, classificação, `monitor_adapter.py`) exigem também o deploy do runtime
(§10.5). Rode a suíte antes: `python -m pytest tests/unit tests/property`.

Se o `up` **criar uma Lambda nova**, o log group dela nasce sem prazo de
retenção. Depois do primeiro uso, rode:

```bash
python scripts/retencao_logs.py
```

Mudanças em `frontend/` não vão pelo Pulumi: publique o painel (§8.4).

### 10.4 Ingestão

```bash
python scripts/ingest_site.py --bucket progep-ifs-documents
```

```bash
python scripts/ingest_repositorio.py --zip "pacote.zip" --bucket progep-ifs-documents
```

O upload no S3 aciona o Ingestor, que faz debounce e coalesce o lote num único
job. Se precisar disparar manualmente:

```bash
aws bedrock-agent start-ingestion-job --knowledge-base-id INLYXJCFOW --data-source-id I50FTDPEQ6 --region us-east-1
```

### 10.5 Runtime do agente

Docker Desktop rodando, e **sem `2>&1` nem `*>`** (ver §11):

```bash
& .\scripts\deploy_agentcore.ps1
```

Confira a versão nova: `aws bedrock-agentcore-control get-agent-runtime
--agent-runtime-id progep_ifs_agent-07SkZL6Kh5` deve mostrar `status: READY` e
`agentRuntimeVersion` incrementada. O aviso
`UpdateTraceSegmentDestination ... already set to CloudWatchLogs` é esperado.

### 10.6 Controle de custos

Configuração (uma vez; já aplicada em produção):

```bash
pulumi config set progep-ifs:custosEquipeTecnica <e-mail1>,<e-mail2>
```

Ao mudar um preço (AWS ou Meta), edite `PRECOS`/`PRECO_MODELOS` em
`src/domain/custos.py` e rode `pulumi up`. Para recalcular o histórico com o
preço novo: os itens de métrica são recalculados pelo
`custos_retroativo.py`; os de evento (tokens, mensagens) não, porque somam —
antes de reprocessar, apague os itens do mês (`pk = custo#AAAA-MM`) **e** as
marcas de evento (`pk` começando com `marca#`, que vivem 3 dias); sem apagar as
marcas, os eventos desses 3 dias são pulados como "já contados".

### 10.7 Antes de mexer no prompt

O prompt (`GENERATION_PROMPT_TEMPLATE`) já oscilou com mudanças pequenas.
Sempre: rode as três baterias (§9.1) **antes e depois**, compare, e teste
localmente o caso que motivou a mudança (os scripts de comparação montam o
`BedrockAgentAdapter` com clientes reais e trocam só o template).

---

## 11. Troubleshooting

### Ambiente (Windows / PowerShell 5.1)

| Sintoma | Causa e correção |
|---|---|
| `pwsh não é reconhecido` | Só existe Windows PowerShell 5.1. Use `& .\scripts\x.ps1` |
| `deploy_agentcore.ps1` aborta no build | **Não use `2>&1`**: o PS 5.1 embrulha a saída do `docker buildx` em ErrorRecord e o `$ErrorActionPreference="Stop"` mata o script |
| `docker login` no ECR devolve 400 | O pipe nativo do PS 5.1 corrompe a stdin. Já contornado no script (arquivo temporário + `cmd /c`) |
| AWS CLI: `Expected: '=', received: '﻿'` | BOM no JSON. Use `[System.IO.File]::WriteAllText(f, json, UTF8Encoding($false))`, nunca `Set-Content -Encoding utf8` |
| JSON inline vira `{networkMode:PUBLIC}` | O PS 5.1 come as aspas. Passe por arquivo (`file://`) |
| Saída de script Python não aparece | stdout com buffer ao redirecionar. Use `line_buffering=True` |
| `UnicodeEncodeError` no meio de um crawl | Console cp1252. Use `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` |
| `/aws/lambda/...` vira caminho estranho | MSYS do git-bash converte paths. Use PowerShell ou `MSYS_NO_PATHCONV=1` |

### AWS

| Sintoma | Causa e correção |
|---|---|
| `Your session has expired` | `aws login --profile ifs`. Scripts longos são retomáveis |
| Conta errada em `get-caller-identity` | O perfil `default` já apontou para outro cliente. Use `--profile ifs` |
| `error: no stack named 'prod' found` | O Pulumi está logado na nuvem (outro projeto). `pulumi login --local` |
| `pulumi up` avisa "resources are in an unknown state because the Pulumi CLI was interrupted" | `pending_operations` antigas na stack (`connect-hours`; a outra era da Lambda `progep-ifs-connect`, removida em 02/10/2026), não do deploy atual. Não bloqueiam; limpar com `pulumi stack export/import` quando houver janela |
| `docker login` no ECR: `500 Internal Server Error ... dockerDesktopLinuxEngine` | O motor do Docker Desktop travou, mesmo com o programa aberto. Reinicie o Docker Desktop (Quit e abrir de novo) |
| `deploy_agentcore.ps1` falha logo no build com `NativeCommandError` | Rodado com `*>` ou `2>&1`. Rode sem redirecionamento |
| `failed to load checkpoint: EOF` no Pulumi | `prod.json.attrs` zerou após interrupção. Copie `prod.json.bak.attrs` por cima (valide o md5) e apague o `.tmp` órfão |
| Recurso existe na AWS mas não no estado | `pulumi import` + `pulumi state unprotect` |
| `pulumi preview` sempre quer remover `responseTemplates` | Já corrigido: os gateway responses declaram o template explicitamente |

### Ingestão

| Sintoma | Causa e correção |
|---|---|
| Documento no S3 mas não na KB | Formato não suportado (rejeitado em silêncio, §4.3), ou **PDF sem camada de texto** — rode `ocr_documentos.py` |
| Nenhum ingestion job dispara | Era o bug do `clientToken` (corrigido). Confira os logs do `progep-ifs-ingestor` |
| `ThrottlingException` no StartIngestionJob | Rate limit baixo. O Ingestor faz debounce; em lote grande, dispare 1 job manual |
| Contagem de arquivos ≠ contagem de itens | **Sempre confira**. Já houve colisão de nome silenciosa (dois fluxos com o mesmo título) |
| Assistente responde com a versão antiga de uma norma | Cópias do documento antigo ocupando as vagas da busca, ou versão nova ausente (§4.4). Rode `retrieve` com a pergunta e veja de onde vêm os 5 trechos |
| Filtro por `x-amz-bedrock-kb-source-uri` no `retrieve` volta vazio | Com S3 Vectors a origem fica dentro de `AMAZON_BEDROCK_METADATA` (não filtrável). Para inspecionar um documento, liste o índice (`s3vectors list-vectors --return-metadata`) ou leia o PDF |

### Agente

| Sintoma | Causa e correção |
|---|---|
| `AccessDenied` no RetrieveAndGenerate | O inference profile exige três permissões: `InvokeModel` no ARN do profile, `InvokeModel` no foundation-model com região `*`, e `bedrock:GetInferenceProfile` |
| Resposta comenta o retrieval | Regressão do prompt. Verifique se o `promptTemplate` está sendo enviado |
| Follow-up não entendido | `SESSIONS_TABLE` ausente no runtime, ou a role sem `dynamodb:GetItem/PutItem` |
| Pergunta respondível cai em `not_found` | Provável descasamento de vocabulário (§4.4). Teste a mesma pergunta na linguagem da norma |
| A **mesma** pergunta ora responde ora não | Cobertura parcial no limite da decisão (§5.1). Meça antes de mexer: rode a pergunta 6–8× direto no `retrieve_and_generate` e conte `answered`; um único teste não distingue regressão de oscilação |
| "Temporariamente indisponível" em ~1 s para quem volta a conversar no dia seguinte | Sessão do Bedrock vencida: `ValidationException: Session with Id ... is not valid`. Até 17/09/2026 a janela de retomada era medida pela última atividade, que a Lambda grava **antes** de chamar o runtime — toda sessão parecia recente. Corrigido: a janela conta de `session_saved_at`, e uma sessão recusada é repetida sem contexto. Procure `[falha]` no log do runtime (antes a exceção não era logada) |

### WhatsApp

| Sintoma | Causa e correção |
|---|---|
| `code 190 Session has expired` na Graph API (diagnóstico, §7.6) | Token temporário (24 h). Use o token de System User do segredo `progep-ifs/whatsapp` |
| Encerramento por inatividade não chega, sem erro na Lambda de encerramento | A AWS **aceitou** o envio (`closed=True`), mas a Meta recusou depois. Procure `[entrega] FALHOU` no log da `progep-ifs-whatsapp-social`. Em 15/09/2026 foi `131037 — number needs display name approval`. **Regra dos números 555 gerados pela Meta:** exigem nome de exibição **aprovado** para enviar, mesmo com `name_status: AVAILABLE_WITHOUT_REVIEW` (o status que a Graph API mostra, e que para número próprio bastaria). "Projeto dev" nunca foi enviado para análise. Nas primeiras horas as respostas saíram; no dia seguinte, tudo passou a ser recusado. Corrige-se no WhatsApp Manager → Números de telefone → Perfil → Nome de exibição → Editar; o andamento aparece em `new_name_status` (Graph API, `GET /{meta-phone-number-id}?fields=name_status,new_name_status`) |
| Assistente responde a uma mensagem antiga, fora de hora (ex.: cortesia e oferta de atendente depois do atendimento encerrado) | Entrega repetida: o caminho Meta → End User Messaging → SNS entrega **pelo menos uma vez**. Em 25/09/2026 duas mensagens do atendimento humano voltaram ~2 min depois (sem erro, sem throttle e sem reentrega do SNS — publicações = entregas = execuções). Corrigido com marca por `message_id` (`msg#<wamid>` na tabela de sessões, gravação condicional, TTL 24 h); a entrada agora é logada como `[entrada] msg=…` |
| Procurou o status no log e não achou | O status da Meta chega **segundos depois do envio**, antes de o comando que disparou o envio retornar. Comece a janela do filtro antes do disparo |
| "Digitando…" aparece e a resposta nunca chega | Destino sem `+` (recusado pela AWS) ou recusa da Meta (131037 etc.). Veja `[entrega]` no log |
| Resposta "temporariamente indisponível" | Falha na chamada ao runtime. Procure `[falha]` no log do runtime (`/aws/bedrock-agentcore/runtimes/progep_ifs_agent-07SkZL6Kh5-DEFAULT`) |
| Graph API: `Object with ID ... does not exist ... (100/33)` | O token não tem acesso ao objeto. Gere de novo marcando a conta do WhatsApp; para conta de outra empresa, ver §7.3. Não conta nas 10 tentativas de registro |
| Setor indicado errado no "não encontrei" | Classificação do runtime: log `[setor] setores=…`. Ajuste os assuntos em `src/domain/contatos.py` e redeploy do runtime |
| Oferta de atendente não aparece | Fora do expediente (seg–sex 9h–18h, UTC−3), pergunta classificada como fora da PROGEP, ou Lambda sem as variáveis `CONNECT_*` |

### Aba "Custos"

| Sintoma | Causa e correção |
|---|---|
| "Hoje" mostra perguntas = 0 logo depois de uma pergunta | Esperado: perguntas e execução do AgentCore vêm de métricas horárias. O modelo e o WhatsApp aparecem em segundos |
| Custo do modelo parado | A assinatura do log de invocações caiu ou a Lambda `progep-ifs-custos-logs` falhou. Veja o log dela (`[custos] grupo=… {…}`) e `aws logs describe-subscription-filters --log-group-name /progep-ifs/bedrock/model-invocations` |
| Modelo novo com custo zero | Falta o preço em `PRECO_MODELOS`; o log da `custos-logs` avisa `Modelo sem preço cadastrado` |
| Medido ≠ faturado na conferência | Último dia da fatura incompleto (fica de fora) ou virada de dia UTC × UTC−3. Diferença persistente em vários dias = preço desatualizado em `PRECOS` |
| Uso de outro projeto aparecendo | A atribuição é pela identidade do log de invocações (`custos.item_da_invocacao`). Confira se a role ou o e-mail em `custosEquipeTecnica` não são compartilhados |

---

## 12. Evoluções possíveis

O projeto foi entregue com o canal WhatsApp em produção. As sugestões abaixo
são para quem continuar a evoluí-lo.

### Ajustes já preparados (uma configuração)

- **Religar a trilha própria do CloudTrail** se a conta do IFS sair da
  organização da DNX, que leva a trilha `dnx` junto:
  `pulumi config set progep-ifs:trilhaPropria true` e `pulumi up`.
- **Isenção da Meta para órgão público** (mensagens de serviço acima de 1.000
  por mês): se a Meta confirmar a isenção do IFS, ligue
  `progep-ifs:custosMetaIsento` (§8.6) para a aba "Custos" deixar de cobrar essa
  tarifa.

### Qualidade das respostas

1. **FAQ dos setores** — é o que resolve o descasamento de vocabulário (§4.4) e
   cobre o "onde protocolo e que documento anexo", que a legislação não traz.
2. **Validação das transcrições** dos fluxogramas pela PROGEP, começando pelos
   10 fluxos mais consultados.
3. **Escopo** — o corpus inclui a Lei 14.133 (licitações), e o assistente
   responde sobre compras; retirar o documento se isso não for desejado.
4. **Atualização do acervo** — ao publicar norma nova pelo painel (§8), remover
   a versão antiga, para as duas não competirem na busca.
5. **Perguntas de saúde com menos trechos** — o `Retrieve` devolve 2 de 5
   (§4.4); vale investigar antes de mexer em `DEFAULT_MAX_RESULTS`.

### Técnicas

6. **Integração com o site** — a API está pronta, mas não foi integrada ao site
   (o foco foi o WhatsApp). O guia está em §5.5.
7. **Encurtar respostas** — a cauda lenta (p90 ~9 s) são as respostas longas.
8. **Divisão por estrutura para todo o acervo** — hoje só os FAQs são
   divididos pelo conteúdo (§4.5), por script. A forma geral é uma Lambda de
   *custom transformation* no data source, que recebe cada documento na
   indexação e devolve os trechos cortados nos títulos, artigos ou perguntas
   (tamanho variável), em vez do corte por tamanho. Exige data source novo (a
   estratégia de divisão não muda num data source existente) e reindexar tudo.
9. **Reescrita de pergunta** para o vocabulário normativo antes da busca —
   alternativa técnica ao FAQ; custa uma chamada de LLM.
10. **OCR no pipeline de ingestão** — hoje é passo manual (`ocr_documentos.py`).
11. **Limpar as `pending_operations`** antigas da stack Pulumi (§11).
12. **Subir o moto** para destravar o teste de integração (§9.3).
13. **Observabilidade das conversas (Langfuse)** — avaliado em 05/10/2026 e
    não adotado. O guia v2 está sem correções de segurança desde o 1º
    trimestre de 2025; a versão atual pede Postgres, ClickHouse, Redis e S3 (no
    mínimo 4 vCPU e 16 GB, ~US$ 130–150/mês self-hosted). Ele calcula só o
    custo do modelo (não lê a fatura, então não substitui a aba "Custos"), e a
    chamada principal (`RetrieveAndGenerate`) não devolve tokens. Se voltar à
    pauta, testar antes o Langfuse Cloud (plano grátis).

---

## 13. Changelog

### 2026-11-12
- **FAQs divididos por pergunta** (§4.5), após o 2º feedback sobre o FAQ de
  estágio probatório: respostas sobre quem avalia e quem participa vinham pela
  metade (só os nomeados a partir de 07/02/2025) ou da Lei 8.112. Cinco FAQs
  divididos (121 arquivos), originais guardados em `faq-originais/`. As
  perguntas dos prints passaram a vir completas, com os dois regimes.
- **FAQ dividido automaticamente pelo painel** (§4.5): sem passo manual. O FAQ
  aparece como um documento só na lista, na remoção e na substituição. Nova
  camada `progep-ifs-pypdf`.
- **Remoção durante indexação** deixou de dar erro no painel (§8.2): termina
  em segundo plano.

### 2026-11-10
- **Base com divisão hierárquica** (§4.2), após o feedback do FAQ de estágio
  probatório não reconhecido: a pergunta e a resposta do FAQ caíam em trechos
  diferentes. Runtime v19; a base de divisão fixa fica de reserva
  (`progep-ifs:baseAtiva`). Custo por pergunta: US$ 0,04–0,06.
- **Sete cópias idênticas removidas** do acervo (para a lixeira).
- **Proteção contra cópias no painel** (§8.2, §8.3): cópia idêntica é
  recusada, mesmo nome pergunta se substitui ou mantém os dois, e a aba
  "Documentos na base" lista as cópias que já existem. `ingest_site.py` pula
  conteúdo que já está no bucket.

### 2026-10-08
- **Documentação revisada** (técnica e visão geral) e novo documento de custos
  para o cliente ([custos-assistente-progep.md](custos-assistente-progep.md)),
  com o acesso à aba "Custos".
- Langfuse avaliado e não adotado (§12, item 13).

### 2026-10-02
- **Aba "Custos" no painel** (§8.6): custo medido em tempo real, só da
  PROGEP, conferido contra a fatura de setembro ao centavo.
- Linhas INFO das Lambdas (`[entrada]`, `[transbordo]`) nunca chegavam ao
  CloudWatch; agora chegam.
- **Otimizações de custo:** trilha própria do CloudTrail desligada (a da
  organização cobre a conta; era ~US$ 4,30/mês); ciclo de vida no ECR (imagens
  sem tag expiram em 7 dias; eram 55 imagens, 2,9 GB); Monitor de Consumo sem a
  dimensão `SessionId`. Custo fixo: de US$ 6–7 para cerca de US$ 2–3 por mês (incluindo ~US$ 0,80
  do próprio controle de custos).
- **Limpeza e privacidade:** removidos o webhook do número de teste e o fluxo
  antigo do Connect (§7.5) e funções sem uso; o telefone de quem conversa vai
  para os logs só com os 4 últimos dígitos (`domain/privacidade.py`); os log
  groups da PROGEP passaram a vencer em 90 dias (`scripts/retencao_logs.py`).

### 2026-10-01
- **Contatos dos setores não chegavam em produção:** a imagem da v13 não
  continha o bloco de classificação (o log tinha `[decisao]`, nunca `[setor]`),
  então `setores` vinha vazio. Republicado; conferido no runtime com 18
  perguntas (9 `not_found`, todas com o setor esperado). Runtime v14.
- **Regras de divergência e contestação no prompt publicadas** (preferir a
  norma mais recente e os documentos do IFS aos manuais nacionais antigos; ao
  ser contestado, reconfirmar só com os trechos). Comparadas antes nas
  perguntas do SIASS: sem regressão, e a resposta deixou de citar o
  procedimento antigo do envelope lacrado.
- `is_no_answer()` passou a reconhecer "não trazem", "não mencionam" etc., os
  vazamentos "documentos apresentados/disponíveis" e qualquer "os documentos
  [adjetivo] não trazem/contêm/mencionam…": "Os documentos fornecidos não
  trazem informações sobre…" saía como resposta, sem contato do setor.
  Runtime v15/v16.

### 2026-09-29 / 30
- **Retorno do SIASS:** 3 cópias duplicadas do Manual de Perícia 2017
  removidas (lixeira); transcrição limpa da Cartilha SIASS/IFS 2024 indexada;
  sincronização completa da KB; auditoria do índice S3 Vectors (sem vetores
  órfãos). Regras de divergência e contestação no prompt (publicadas em 01/10).
- **Documentação:** visão geral para o cliente
  ([visao-geral-assistente-progep.md](visao-geral-assistente-progep.md)) e esta
  revisão técnica (seções 7 e 8 novas).

### 2026-09-28
- **Contatos dos setores** quando não há resposta: classificação no runtime
  (`classificar_setor`, 11/11 no teste real), SIASS+DAP para saúde, lista
  completa para assuntos fora da PROGEP; oferta de atendente só no expediente.
  Runtime v13.
- Atendente `jonatha.alves` criado no Connect.

### 2026-09-25
- **Entrega repetida** do WhatsApp neutralizada: marca `msg#<wamid>`
  condicional; entrada logada. "Não tenho mais dúvidas" virou despedida.

### 2026-09-22
- **Número do IFS em produção** (+55 79 3711-1496), portfólio IFS - DTI.
  Transbordo e encerramento por inatividade validados nele.
- Apresentação completa para "Olá" em conversa nova; rótulo "Cartilha IFS".

### 2026-09-17
- **Sessão vencida do Bedrock** não vira mais "indisponível": janela por
  `session_saved_at` e repetição sem sessão. Runtime v12.

### 2026-09-14 / 15
- **Canal migrado para o End User Messaging Social** (SNS → Lambda
  `whatsapp-social`); destino em E.164.
- **Ponte WhatsApp ↔ Connect** (transbordo com confirmação, expediente,
  streaming do chat, estado na conversa).
- Log `[entrega]` com os status da Meta (achado do erro 131037 no número 555).

### Início de setembro
- **Painel do acervo** (CloudFront + Cognito + Lambda `acervo`): envio,
  revisão, indexação, remoção com lixeira e conferência S3 × KB.
- FAQ do NAPA; OCR de 3 documentos; logs `[decisao]` com o motivo de cada
  `not_found`.

### 2026-08-24
- **Continuidade de conversa**: `AgentTurn` + `forward_turn`, `ConversationStore`
  (TTL 30 min), `dynamodb:GetItem/PutItem` na role do runtime. 5/5 follow-ups.
- **Repositório do cliente ingerido**: 123 documentos por departamento; KB de
  275 → 398. OCR de 2 PDFs digitalizados.
- **Markdown aceito na ingestão** (`ContentType.MD`) — sem isso o Ingestor
  rejeitava todo `.md` em silêncio.
- **Desempate de nomes na transcrição** — dois fluxos com o mesmo título
  deixavam um deles sem transcrever, sem erro em log nenhum.
- **Latência**: busca dupla paralelizada, indicador de digitação no WhatsApp,
  instrumentação `[tempo]` por invocação. Baterias: ~11,5 s → ~7,7 s de média.
- **Sonnet 4.5 mantido** após comparação com Haiku 4.5 em 87 perguntas.

### 2026-08-18
- **94 fluxogramas do Mapeamento de Processos transcritos** com visão
  computacional (fatiamento + faixa de raias rotacionada).
- `check_flows.py` para varredura de qualidade das transcrições.

### 2026-07-28
- **Corpus do site expandido**: `SERVIDOR_PAGES` (o crawl só alcançava editais).
  KB de 71 → 161 documentos.
- **Prompt de geração próprio** com persona e sentinela `NAO_ENCONTRADO`;
  `is_no_answer()` para normalizar negativas.
- **Bug do Ingestor corrigido**: o `clientToken` era `bucket/key`, formato que a
  API rejeita — nenhum job automático jamais havia disparado.
- Bateria de QA (`run_qa_suite.py`) criada.

### 2026-07-23
- Contact flow do Connect gerado por código; CORS; monitoramento de tokens.
- WhatsApp E2E validado com token permanente.

### 2026-07-22
- AgentCore Runtime ativado; migração para caminho único.
- Geração LLM via RetrieveAndGenerate; E2E da API validado.

### 2026-07-21
- Deploy inicial: 40 recursos, Knowledge Base com S3 Vectors.
