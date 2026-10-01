# WhatsApp — número de teste (Meta Cloud API) · runbook

Caminho **interino de teste** para validar o canal WhatsApp usando o número de
teste gratuito da Meta, enquanto não há um número institucional real vinculado
ao AWS End User Messaging Social.

> **Por que este caminho é diferente do de produção**
> O número de teste gratuito da Meta fica preso ao *Cloud API sandbox* e **não
> pode** ser vinculado ao AWS End User Messaging Social (que exige o *embedded
> signup* com uma WABA e número reais). Por isso, para testar com o número de
> teste, falamos com o **Meta Cloud API direto** via um webhook (Lambda), em vez
> do caminho de produção `End User Messaging → Amazon Connect`. Quando houver um
> número real, migra-se para o conector de produção
> (`src/adapters/whatsapp_connector.py`), sem alterar a lógica do agente.

## O que já está pronto (lado AWS)

| Item | Valor |
|------|-------|
| Webhook (Function URL) | `https://op5yx2u5nsjlb4joodddntxfuq0uhaob.lambda-url.us-east-1.on.aws/` |
| Lambda | `progep-ifs-whatsapp-webhook` (handler `src.adapters.lambda_entry.whatsapp_handler`) |
| Segredo | `progep-ifs/whatsapp` no Secrets Manager (`verify_token`, `access_token`, `app_secret`) |
| Verify token | já gravado no segredo (campo `verify_token`) |

Validado ao vivo: verificação GET (200 + echo do challenge; 403 com token
errado) e POST de mensagem de texto (parseia → chama o Agente_IA →
`RetrieveAndGenerate` → resposta). Só falta o `access_token` real da Meta para o
envio da resposta funcionar.

## Passos manuais na Meta (exigem login na Meta — feito por você)

1. **Meta for Developers** (https://developers.facebook.com) → criar um app do
   tipo **Business** e adicionar o produto **WhatsApp**.
2. Em **WhatsApp → API Setup**, a Meta já fornece um **número de teste** gratuito
   e um **Temporary access token** (24 h). Anote o **Phone number ID** e o
   **token**.
3. Em **From**/recipients, **adicione o(s) número(s) de destinatário** que vão
   receber as mensagens de teste (o sandbox só envia para números pré-cadastrados
   e verificados por código).
4. **Configurar o webhook** (WhatsApp → Configuration):
   - **Callback URL**: a Function URL da tabela acima.
   - **Verify token**: o valor do campo `verify_token` do segredo
     `progep-ifs/whatsapp` (peça ao time ou leia no Secrets Manager).
   - Clicar **Verify and save** (a Meta chama o GET; deve validar na hora).
   - **Subscribe** ao campo **messages**.
5. **App secret** (opcional, recomendado): Settings → Basic → **App Secret**.
   Com ele preenchido no segredo, o webhook passa a validar a assinatura
   `X-Hub-Signature-256` de cada POST.

## Preencher o segredo com os valores da Meta

Depois de obter `access_token` (e opcionalmente `app_secret`), atualize o
segredo (mantendo o `verify_token` já existente):

```bash
aws secretsmanager put-secret-value \
  --secret-id progep-ifs/whatsapp \
  --region us-east-1 \
  --secret-string '{"verify_token":"<MANTER O ATUAL>","access_token":"<TOKEN DA META>","app_secret":"<APP SECRET OU VAZIO>"}'
```

> No Windows/PowerShell, grave o JSON em um arquivo e use `--secret-string file://caminho.json`
> para evitar problemas de aspas.

O token temporário da Meta **expira em 24 h** — quando isso acontece, a mensagem
chega e é processada normalmente, mas o envio da resposta falha com
`code 190 / Session has expired` (visível nos logs da Lambda). Para uso contínuo,
gere um **System User token permanente**:

1. **Meta Business Manager** → *Configurações do negócio* → **Usuários → Usuários do sistema**.
2. Crie (ou use) um usuário do sistema com função **Administrador**.
3. **Adicionar ativos** → selecione o **app** e a **conta do WhatsApp (WABA)**, com controle total.
4. **Gerar novo token** → escolha o app → marque as permissões
   `whatsapp_business_messaging` e `whatsapp_business_management` → **defina a
   expiração como "Nunca"**.
5. Substitua o `access_token` no segredo `progep-ifs/whatsapp`.

## Dígito 9 nos números brasileiros

O `wa_id` do WhatsApp para números do Brasil normalmente **não inclui o 9** do
celular (ex.: `(79) 90000-1234` chega como `557900001234`). Ao cadastrar o número
do destinatário no painel da Meta, **cadastre sem o 9** — com o 9 o envio falha
mesmo com o número aparentemente verificado. O webhook já responde para o
`from`/`wa_id` exato recebido, então não há nada a ajustar no código.

## Testar

Envie uma mensagem de texto do número de destinatário cadastrado para o número
de teste da Meta. A resposta do assistente (fundamentada na base da PROGEP) deve
chegar no WhatsApp. Logs em `/aws/lambda/progep-ifs-whatsapp-webhook`.

## Limitações do número de teste

- Só envia para destinatários pré-cadastrados (máx. 5).
- Não passa por Amazon Connect nem AWS End User Messaging (caminho de produção).
- Token temporário de 24 h (salvo se usar System User token).
