"""Canal WhatsApp pelo AWS End User Messaging Social (Req 6.1, 6.2).

```
Meta → End User Messaging → SNS → Lambda → SendWhatsAppMessage → Meta
```

(Até 02/10/2026 existiu também um webhook do número de teste, que falava direto
com a Graph API por uma Function URL; saiu por estar sem uso e aceitar POST sem
assinatura.)

**O corpo da mensagem não muda.** ``SendWhatsAppMessage`` recebe o payload como
*blob* — é o mesmo JSON que se manda à Graph API, apenas embrulhado. Por isso o
indicador de "digitando" continua funcionando: ele é um payload, não um recurso
próprio da Graph.

**O que desaparece:** access token, app secret e validação de assinatura HMAC.
Quem autentica passa a ser a AWS, o que elimina de uma vez o token que expirava
em 24 h e o segredo cacheado por container.

**Duas camadas de JSON-dentro-de-string** separam o evento da Lambda da mensagem
do usuário, e é aí que integrações assim costumam quebrar:

```
evento Lambda → Records[].Sns.Message   (string JSON)
                  └→ whatsAppWebhookEntry (string JSON de novo)
                       └→ {"id", "changes": [{"value", "field"}]}
```

O miolo é o **mesmo ``entry`` do webhook da Meta**, percorrido por
:func:`parse_inbound_messages`.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

from src.adapters.whatsapp_connector import IncomingWhatsAppMessage

logger = logging.getLogger(__name__)

#: Versão da API da Meta usada pelo End User Messaging ao repassar o payload.
#: Fixada para que uma mudança de default do serviço não altere o formato sem
#: aviso.
META_API_VERSION = "v20.0"


def parse_inbound_messages(body: Mapping[str, Any]) -> list[IncomingWhatsAppMessage]:
    """Extrai as mensagens de um payload no formato do webhook da Meta.

    Percorre ``entry[].changes[].value.messages[]``; para cada mensagem coleta o
    remetente, o id, o texto e o ``phone_number_id`` de destino. Mensagens sem
    corpo de texto entram com ``text=""`` e ``part_types`` refletindo o tipo
    recebido (ex.: ``("image",)``), de modo que a classificação de conteúdo
    somente-texto do domínio as descarte (Req 6.3).
    """
    out: list[IncomingWhatsAppMessage] = []
    for entry in body.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            metadata = value.get("metadata", {}) or {}
            phone_number_id = str(metadata.get("phone_number_id", "") or "")
            for msg in value.get("messages", []) or []:
                msg_type = str(msg.get("type", "") or "")
                if msg_type == "text":
                    text = str((msg.get("text", {}) or {}).get("body", "") or "")
                    part_types: tuple[str, ...] = ("text",)
                else:
                    # Mídia não textual: sem texto, tipo preservado p/ descarte.
                    text = ""
                    part_types = (msg_type or "unknown",)
                out.append(
                    IncomingWhatsAppMessage(
                        from_number=str(msg.get("from", "") or ""),
                        message_id=str(msg.get("id", "") or ""),
                        text=text,
                        part_types=part_types,
                        phone_number_id=phone_number_id,
                    )
                )
    return out


def parse_sns_event(evento: Mapping[str, Any]) -> list[IncomingWhatsAppMessage]:
    """Extrai as mensagens de um evento SNS do End User Messaging.

    Tolera registro malformado sem derrubar o lote: um evento com duas
    mensagens, uma delas corrompida, deve entregar a boa em vez de perder as
    duas — o SNS reentregaria o lote inteiro e a mensagem boa seria processada
    de novo, duplicando a resposta ao usuário.
    """
    mensagens: list[IncomingWhatsAppMessage] = []
    for registro in evento.get("Records", []) or []:
        bruto = ((registro.get("Sns") or {}).get("Message")) or ""
        if not bruto:
            continue
        try:
            corpo = json.loads(bruto)
            entrada = corpo.get("whatsAppWebhookEntry")
            if not entrada:
                continue
            # O `entry` vem como string; `parse_inbound_messages` espera a lista
            # do webhook, então o embrulho devolve o formato que ela conhece.
            mensagens += parse_inbound_messages({"entry": [json.loads(entrada)]})
        except (ValueError, TypeError, AttributeError):
            logger.warning("Registro SNS ignorado por formato inesperado", exc_info=True)
    return mensagens


def status_de_entrega(evento: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Status de entrega que a Meta devolve para as mensagens **enviadas**.

    ``SendWhatsAppMessage`` só confirma que a AWS aceitou a mensagem. Se a Meta
    recusar depois (número, janela de 24 h, política), o único sinal é um
    ``status: failed`` que chega pelo mesmo tópico, segundos mais tarde. Sem
    registrá-lo, o envio parece ter dado certo — foi assim que o encerramento
    por inatividade sumiu em 14/09/2026 sem deixar erro em log nenhum.

    Devolve só o necessário para diagnóstico: status, id da mensagem, final do
    número e os erros. O número inteiro não vai para log.
    """
    saida: list[dict[str, Any]] = []
    for registro in evento.get("Records", []) or []:
        bruto = ((registro.get("Sns") or {}).get("Message")) or ""
        try:
            entrada = json.loads((json.loads(bruto) or {}).get("whatsAppWebhookEntry") or "{}")
        except (ValueError, TypeError, AttributeError):
            continue
        for mudanca in (entrada.get("changes") or []) if isinstance(entrada, dict) else []:
            for status in ((mudanca.get("value") or {}).get("statuses") or []):
                destino = str(status.get("recipient_id", "") or "")
                saida.append(
                    {
                        "status": str(status.get("status", "") or ""),
                        "message_id": str(status.get("id", "") or ""),
                        "destino_final": destino[-4:],
                        "erros": [
                            {
                                "code": e.get("code"),
                                "title": e.get("title"),
                                "details": (e.get("error_data") or {}).get("details"),
                            }
                            for e in (status.get("errors") or [])
                        ],
                    }
                )
    return saida


def registrar_status_de_entrega(evento: Mapping[str, Any]) -> int:
    """Loga os status; falha de entrega sai como ``ERROR``. Devolve quantas falhas."""
    falhas = 0
    for s in status_de_entrega(evento):
        if s["status"] == "failed":
            falhas += 1
            logger.error(
                "[entrega] FALHOU msg=%s destino=…%s erros=%s",
                s["message_id"], s["destino_final"], json.dumps(s["erros"], ensure_ascii=False),
            )
        else:
            logger.warning(
                "[entrega] %s msg=%s destino=…%s", s["status"], s["message_id"], s["destino_final"]
            )
    return falhas


def phone_number_id_de_origem(evento: Mapping[str, Any]) -> str:
    """Id do número **na AWS**, que é o que ``SendWhatsAppMessage`` exige.

    O ``phone_number_id`` que vem dentro do payload da Meta não serve aqui: a
    API da AWS quer o identificador dela (``phone-number-id-...``), publicado no
    cabeçalho do evento. Trocar um pelo outro é recusado por padrão de formato,
    não por permissão — o erro não diz que o identificador é de outro domínio.
    """
    for registro in evento.get("Records", []) or []:
        bruto = ((registro.get("Sns") or {}).get("Message")) or ""
        try:
            contexto = (json.loads(bruto) or {}).get("context") or {}
        except (ValueError, TypeError):
            continue
        for numero in contexto.get("MetaPhoneNumberIds") or []:
            arn = str(numero.get("arn", "") or "")
            if arn:
                return arn
    return ""


class WhatsAppSocialClient:
    """Envia mensagens pelo End User Messaging Social.

    ``send_text`` e ``mark_read_and_typing`` são a interface que a orquestração
    do canal usa. O primeiro parâmetro se chama ``phone_number_id``, como na
    Graph API, mas aqui é o identificador **da AWS** (o ARN do número).
    """

    def __init__(self, client: Any = None, *, region_name: str | None = None) -> None:
        self._client = client
        self._region_name = region_name or os.environ.get("AWS_REGION")

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("socialmessaging", region_name=self._region_name)
        return self._client

    def _enviar(self, phone_number_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.send_whatsapp_message(
            originationPhoneNumberId=phone_number_id,
            message=json.dumps(payload).encode("utf-8"),
            metaApiVersion=META_API_VERSION,
        )

    def mark_read_and_typing(self, phone_number_id: str, message_id: str) -> bool:
        """Marca como lida e mostra "digitando…" — o mesmo payload da Graph API.

        Falha aqui **nunca** derruba o atendimento: é sinal de cortesia, não
        parte da resposta.
        """
        if not message_id:
            return False
        try:
            self._enviar(
                phone_number_id,
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                    "typing_indicator": {"type": "text"},
                },
            )
            return True
        except Exception:  # noqa: BLE001 - cortesia não pode quebrar o fluxo
            logger.warning("Não foi possível sinalizar leitura/digitação", exc_info=True)
            return False

    def send_text(self, phone_number_id: str, to_number: str, text: str) -> str:
        """Envia texto e devolve o id da mensagem, ou ``""`` se não vier."""
        resposta = self._enviar(
            phone_number_id,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                # **A AWS exige E.164 com `+`; a Graph API não.** A Meta entrega o
                # remetente sem o sinal (`557900001234`), e reenviar esse valor
                # como veio é recusado com `Invalid destination phone number or
                # message Id` — mensagem que não fala de formato. Medido em
                # 14/09/2026: sem `+` recusado, com `+` aceito, e o nono dígito
                # do celular brasileiro **não** é necessário. O "digitando"
                # escapava do problema por não ter campo de destinatário.
                "to": to_number if to_number.startswith("+") else f"+{to_number}",
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
        )
        return str((resposta or {}).get("messageId", "") or "")
