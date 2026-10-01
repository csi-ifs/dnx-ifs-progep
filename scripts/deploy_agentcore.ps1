<#
Deploy do Agente_IA no Amazon Bedrock AgentCore Runtime.

Pré-requisitos:
  - `pulumi up` já criou o repositório ECR e a role de execução do AgentCore
    (outputs: agentcore_ecr_url, agentcore_exec_role_arn).
  - Docker Desktop rodando (build linux/arm64 via buildx).
  - `aws login` válido (a criação do runtime usa a AWS CLI, pois o boto3/pulumi
    instalados ainda não conhecem bedrock-agentcore-control).

Uso:
  pwsh scripts/deploy_agentcore.ps1 [-RuntimeName progep-ifs-agent] [-Region us-east-1]

O script:
  1. Lê os outputs do Pulumi (ECR url, role arn) e o KB id / MODEL_ARN.
  2. Faz login no ECR, build da imagem linux/arm64 e push.
  3. Cria (ou atualiza) o AgentCore Runtime apontando para a imagem, com as
     variáveis de ambiente do agente e a role de execução.
  4. Habilita a observabilidade (CloudWatch Transaction Search) da conta.
#>
param(
  [string]$RuntimeName = "progep_ifs_agent",
  [string]$Region = "us-east-1"
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:PULUMI_CONFIG_PASSPHRASE = "progep-ifs-local"

Write-Host "== Lendo outputs do Pulumi =="
$ecrUrl   = pulumi stack output agentcore_ecr_url --cwd $repoRoot
$roleArn  = pulumi stack output agentcore_exec_role_arn --cwd $repoRoot
$kbId     = pulumi stack output knowledge_base_id --cwd $repoRoot
$acct     = (aws sts get-caller-identity --query Account --output text)
$modelArn = "arn:aws:bedrock:${Region}:${acct}:inference-profile/global.anthropic.claude-sonnet-4-5-20250929-v1:0"
$tag      = "$ecrUrl`:latest"
Write-Host "  ECR:   $ecrUrl"
Write-Host "  Role:  $roleArn"
Write-Host "  KB:    $kbId"

Write-Host "== Login no ECR =="
# O pipe nativo do Windows PowerShell 5.1 corrompe a stdin do docker login (o
# token do ECR chega malformado e o registry devolve 400 Bad Request). Passar
# por arquivo temporário sem BOM, redirecionado pelo cmd, evita o pipe do PS —
# e mantém o token fora da linha de comando (que fica visível na lista de
# processos).
$registry = $ecrUrl.Split("/")[0]
$ecrPass  = aws ecr get-login-password --region $Region
$passFile = Join-Path $env:TEMP "ecr-login-$PID.txt"
try {
    [System.IO.File]::WriteAllText(
        $passFile, $ecrPass, (New-Object System.Text.UTF8Encoding($false))
    )
    cmd /c "docker login --username AWS --password-stdin $registry < `"$passFile`""
    if ($LASTEXITCODE -ne 0) { throw "docker login no ECR falhou" }
} finally {
    Remove-Item $passFile -Force -ErrorAction SilentlyContinue
}

Write-Host "== Build linux/arm64 + push =="
docker buildx build --platform linux/arm64 `
  -f "$repoRoot/agentcore/Dockerfile" `
  -t $tag --push "$repoRoot"

Write-Host "== Habilitando observabilidade (CloudWatch Transaction Search) =="
# Idempotente; ignora se já habilitado.
try {
  aws xray update-trace-segment-destination --destination CloudWatchLogs --region $Region | Out-Null
} catch { Write-Host "  (trace destination já configurado ou indisponível)" }

Write-Host "== Criando/atualizando o AgentCore Runtime =="
# SESSIONS_TABLE liga a continuidade de conversa: sem ela o runtime
# responde cada pergunta isoladamente (sem entender follow-up).
$envJson = @{ KNOWLEDGE_BASE_ID = $kbId; MODEL_ARN = $modelArn;
              RELEVANCE_THRESHOLD = "0.4";
              SESSIONS_TABLE = "progep-ifs-sessions" } | ConvertTo-Json -Compress
$artifact = @{ containerConfiguration = @{ containerUri = $tag } } | ConvertTo-Json -Compress -Depth 5

# Existe um runtime com esse nome?
$existing = aws bedrock-agentcore-control list-agent-runtimes --region $Region `
  --query "agentRuntimes[?agentRuntimeName=='$RuntimeName'].agentRuntimeId | [0]" --output text 2>$null

# `Set-Content -Encoding utf8` grava **com BOM** no PS 5.1, e a AWS CLI não
# tolera o BOM ao ler `file://` — falha com "Expected: '=', received: '﻿'".
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$envFile = New-TemporaryFile
[System.IO.File]::WriteAllText($envFile, $envJson, $utf8NoBom)
$artFile = New-TemporaryFile
[System.IO.File]::WriteAllText($artFile, $artifact, $utf8NoBom)
# Idem para a network config: passada inline, o PS 5.1 remove as aspas do
# JSON antes de entregá-lo ao exe nativo ("{networkMode:PUBLIC}").
$netFile = New-TemporaryFile
[System.IO.File]::WriteAllText($netFile, '{"networkMode":"PUBLIC"}', $utf8NoBom)

if ($existing -and $existing -ne "None") {
  Write-Host "  Atualizando runtime $existing"
  aws bedrock-agentcore-control update-agent-runtime `
    --agent-runtime-id $existing `
    --agent-runtime-artifact "file://$artFile" `
    --role-arn $roleArn `
    --network-configuration "file://$netFile" `
    --environment-variables "file://$envFile" `
    --region $Region
} else {
  Write-Host "  Criando runtime $RuntimeName"
  aws bedrock-agentcore-control create-agent-runtime `
    --agent-runtime-name $RuntimeName `
    --agent-runtime-artifact "file://$artFile" `
    --role-arn $roleArn `
    --network-configuration "file://$netFile" `
    --environment-variables "file://$envFile" `
    --region $Region
}
Remove-Item $envFile, $artFile, $netFile -ErrorAction SilentlyContinue
Write-Host "== Concluído =="
