# Assistente Inteligente PROGEP-IFS (Contact Center PROGEP)

Solução de atendimento automatizado (Agentic RAG) para servidores do IFS na
área da PROGEP. Núcleo em Amazon Bedrock AgentCore sobre uma Knowledge Base,
canal WhatsApp (AWS End User Messaging Social), transbordo para atendente
humano no Amazon Connect e infraestrutura como código em Pulumi (Python).

> **Documentação**
> - [docs/visao-geral-assistente-progep.md](docs/visao-geral-assistente-progep.md):
>   apresentação para o cliente (o que é, como funciona, serviços, custos, painel do acervo).
> - [docs/documentacao-tecnica.md](docs/documentacao-tecnica.md): arquitetura
>   deployada, serviços, passo a passo de deploy, Meta/WhatsApp, acervo e troubleshooting.
>
> O vector store deployado é **Amazon S3 Vectors** (não OpenSearch Serverless, que
> era o design de referência) — escolha por custo. O agente roda no **Bedrock
> AgentCore Runtime** e também in-process na Lambda da API.

## Estrutura do projeto

```
src/
  domain/       # camada de lógica pura (modelos, validação, sessão, contatos, transbordo)
  adapters/     # adaptadores de I/O para serviços AWS (agente, WhatsApp, Connect, DynamoDB)
  flows/        # orquestração dos canais e da ingestão
  infra/        # IaC em Pulumi (Python) com tagueamento global APN
agentcore/      # imagem do agente no AgentCore Runtime (app.py + Dockerfile)
frontend/       # painel do acervo (React + Vite, publicado no CloudFront)
scripts/        # deploy do agente, publicação do painel, ingestão, OCR e baterias de QA
tests/
  property/     # testes de propriedade (Hypothesis, >=100 iterações)
  unit/         # testes unitários por exemplo
  integration/  # testes de integração com serviços AWS
docs/           # documentação (os documentos da base — FAQ, fluxos, OCR — ficam fora do git)
```

O estado do Pulumi é **local** (`pulumi login --local`, stack `prod`), assim
como `Pulumi.prod.yaml`; ver a documentação técnica para o deploy.

## Requisitos

- Python >= 3.10

## Configuração do ambiente de desenvolvimento

```bash
python -m pip install -e ".[dev]"
# ou
python -m pip install -r requirements-dev.txt
```

## Executando os testes

```bash
python -m pytest              # todos os testes
python -m pytest -m unit      # apenas unitários
python -m pytest -m property  # apenas testes de propriedade
```

Os testes de propriedade usam [Hypothesis](https://hypothesis.readthedocs.io/)
com um mínimo de 100 iterações por propriedade.
