# AgentCore Runtime exige imagem linux/arm64 servindo em 0.0.0.0:8080.
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app

# Dependências primeiro (camada cacheável).
COPY agentcore/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Código: a camada pura + adapters do repositório e o entrypoint do AgentCore.
COPY src ./src
COPY agentcore/app.py ./app.py

# Anotação de billing padrão do time (Req 8.3); o adapter também a injeta.
ENV AWS_SDK_UA_APP_ID="APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$"
ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# opentelemetry-instrument habilita a emissão de traces p/ a observabilidade
# do AgentCore; app.run() sobe o servidor /invocations + /ping na porta 8080.
CMD ["opentelemetry-instrument", "python", "-m", "app"]
