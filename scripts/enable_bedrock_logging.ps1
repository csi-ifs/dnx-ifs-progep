<#
Habilita o *model invocation logging* do Amazon Bedrock (Req 7.1).

Por que fora do Pulumi: o provider `pulumi_aws` (7.38) não expõe o recurso
`ModelInvocationLoggingConfiguration`. O log group e a role de logging **são**
criados pela IaC (`src/infra/resources.py`); este script apenas liga a
configuração da conta a eles, e é idempotente.

Sem isso, o Req 7.1 não é atendível: o `RetrieveAndGenerate` não devolve
contagem de tokens na resposta — a fonte confiável é este log, de onde os
metric filters extraem `InputTokens`/`OutputTokens` para o CloudWatch.

Uso:
  pwsh scripts/enable_bedrock_logging.ps1 [-Region us-east-1]
#>
param([string]$Region = "us-east-1")
$ErrorActionPreference = "Stop"

$logGroup = "/progep-ifs/bedrock/model-invocations"
$roleName = "progep-ifs-bedrock-logging"

Write-Host "== Resolvendo a role de logging =="
$roleArn = aws iam get-role --role-name $roleName --query "Role.Arn" --output text
if (-not $roleArn) { throw "Role $roleName não encontrada — rode 'pulumi up' antes." }
Write-Host "  $roleArn"

$cfg = [ordered]@{
    cloudWatchConfig = [ordered]@{ logGroupName = $logGroup; roleArn = $roleArn }
    textDataDeliveryEnabled      = $true
    imageDataDeliveryEnabled     = $false
    embeddingDataDeliveryEnabled = $false
}
$file = New-TemporaryFile
[System.IO.File]::WriteAllText($file, ($cfg | ConvertTo-Json -Compress -Depth 5))

Write-Host "== Aplicando a configuração de logging =="
aws bedrock put-model-invocation-logging-configuration `
    --logging-config "file://$file" --region $Region
Remove-Item $file -ErrorAction SilentlyContinue

Write-Host "== Configuração ativa =="
aws bedrock get-model-invocation-logging-configuration --region $Region --output json
