"""Meta WhatsApp Cloud API webhook adapter — caminho interino de teste.

Este módulo é o caminho **de teste** para validar o canal WhatsApp usando o
*número de teste gratuito da Meta* (dev mode). Esse número fica no sandbox do
Meta Cloud API e **não pode ser vinculado ao AWS End User Messaging Social**
(que exige embedded signup com WABA/número reais), então o conector de produção
:mod:`src.adapters.whatsapp_connector` (End User Messaging + Amazon Connect) não
consegue exercitá-lo. Aqui falamos com o Meta Cloud API diretamente:

``WhatsApp → webhook (esta Lambda) → Agente_IA → resposta via graph.facebook.com``

Quando houver um número real vinculado, o caminho de produção (End User Messaging
+ Connect) volta a ser o usado; este módulo permanece apenas para testes.

Responsabilidades:

* **Verificação do webhook (GET)** — a Meta chama o endpoint com
  ``hub.mode``/``hub.verify_token``/``hub.challenge``; :func:`verify_subscription`
  devolve o ``challenge`` somente quando o ``verify_token`` confere.
* **Parsing de entrada (POST)** — :func:`parse_inbound_messages` extrai as
  mensagens de texto do payload do Cloud API, reaproveitando o dataclass
  :class:`~src.adapters.whatsapp_connector.IncomingWhatsAppMessage`.
* **Envio (outbound)** — :class:`WhatsAppCloudClient` envia texto de volta pelo
  endpoint ``/{phone_number_id}/messages`` do graph, usando o access token do
  app. A validação de assinatura ``X-Hub-Signature-256`` é feita por
  :func:`verify_signature` quando o app secret está disponível.

As partes puras (verificação, parsing, assinatura) não fazem I/O e são testáveis
sem rede; apenas :class:`WhatsAppCloudClient` faz a chamada HTTP (via ``urllib``,
já presente no runtime da Lambda — sem dependência de ``requests``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from src.adapters.whatsapp_connector import IncomingWhatsAppMessage

logger = logging.getLogger(__name__)

#: Versão do Meta Cloud API usada nas chamadas ao graph.
META_API_VERSION: str = "v20.0"
#: Host base do Meta Graph API.
GRAPH_HOST: str = "https://graph.facebook.com"


def verify_subscription(
    params: Mapping[str, Any] | None, expected_verify_token: str
) -> str | None:
    """Valida o handshake de verificação do webhook da Meta (GET).

    A Meta chama o endpoint com ``hub.mode=subscribe``, ``hub.verify_token`` e
    ``hub.challenge``. Retorna o ``challenge`` (string) quando o modo é
    ``subscribe`` e o token confere; caso contrário retorna ``None`` (o handler
    responde 403).

    Args:
        params: Os query string parameters da requisição GET.
        expected_verify_token: O verify token configurado (fonte: Secrets
            Manager), definido por nós e colado no painel da Meta.
    """
    if not params or not expected_verify_token:
        return None
    mode = params.get("hub.mode") or params.get("hub_mode")
    token = params.get("hub.verify_token") or params.get("hub_verify_token")
    challenge = params.get("hub.challenge") or params.get("hub_challenge")
    if mode == "subscribe" and token == expected_verify_token and challenge is not None:
        return str(challenge)
    return None


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Confere o cabeçalho ``X-Hub-Signature-256`` do payload (HMAC-SHA256).

    Retorna ``True`` quando a assinatura confere com o ``app_secret``. Quando não
    há ``app_secret`` configurado a checagem é considerada desabilitada e retorna
    ``True`` (o verify token continua protegendo o endpoint).
    """
    if not app_secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


def parse_inbound_messages(body: Mapping[str, Any]) -> list[IncomingWhatsAppMessage]:
    """Extrai as mensagens de texto de um payload de webhook do Cloud API.

    Percorre ``entry[].changes[].value.messages[]``; para cada mensagem coleta o
    remetente, o id, o texto e o ``phone_number_id`` de destino (usado como
    origem da resposta). Mensagens sem corpo de texto entram com ``text=""`` e
    ``part_types`` refletindo o tipo recebido (ex.: ``("image",)``), de modo que
    a classificação de conteúdo somente-texto do domínio as descarte (Req 6.3).

    Args:
        body: O JSON do webhook já desserializado.

    Returns:
        Lista de :class:`IncomingWhatsAppMessage`, uma por mensagem recebida.
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


class WhatsAppSendError(RuntimeError):
    """Falha ao enviar mensagem pelo Cloud API, com o erro da Meta preservado.

    O Graph API devolve o motivo real (token expirado, destinatário fora da
    allow-list, restrição de país…) no **corpo** da resposta de erro; sem
    capturá-lo, o log mostraria apenas ``HTTP Error 400``, que não diz nada.

    Attributes:
        status: Código HTTP retornado.
        code: Código de erro da Meta (ex.: ``190`` token inválido/expirado).
        detail: Mensagem de erro da Meta.
    """

    def __init__(self, status: int, code: int | None, detail: str) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"WhatsApp Cloud API HTTP {status} (code={code}): {detail}")


def _parse_graph_error(status: int, body: bytes) -> WhatsAppSendError:
    """Converte o corpo de erro do Graph API em :class:`WhatsAppSendError`."""
    detail, code = body.decode("utf-8", errors="replace")[:500], None
    try:
        error = (json.loads(body.decode("utf-8")) or {}).get("error") or {}
        detail = str(error.get("message", detail))
        raw_code = error.get("code")
        code = int(raw_code) if raw_code is not None else None
    except (ValueError, TypeError, UnicodeDecodeError):
        pass
    return WhatsAppSendError(status, code, detail)


class WhatsAppCloudClient:
    """Cliente mínimo do Meta Cloud API para enviar texto de volta ao usuário.

    Usa ``urllib`` (presente no runtime da Lambda) para não depender de
    ``requests``. O ``access_token`` e o ``phone_number_id`` vêm do app da Meta
    (armazenados no Secrets Manager, Req 10.1).
    """

    def __init__(
        self,
        access_token: str,
        *,
        api_version: str = META_API_VERSION,
        opener: Any | None = None,
    ) -> None:
        """Inicializa o cliente.

        Args:
            access_token: Access token do app WhatsApp (Cloud API).
            api_version: Versão do Graph API.
            opener: Injetável para testes (callable estilo ``urlopen``); quando
                ``None`` usa ``urllib.request.urlopen``.
        """
        self._access_token = access_token
        self._api_version = api_version
        self._opener = opener or urllib.request.urlopen

    def mark_read_and_typing(self, phone_number_id: str, message_id: str) -> bool:
        """Marca a mensagem como lida e mostra "digitando…" para o usuário.

        O agente leva alguns segundos para responder; sem nenhum sinal, a
        conversa parece ter caído no vácuo. A Meta aceita os dois avisos na
        mesma requisição, e o indicador some sozinho quando a resposta chega
        (ou em 25 s, o que vier primeiro).

        Falha aqui **nunca** derruba o atendimento: é um sinal de cortesia, não
        parte da resposta. Retorna ``True`` quando o aviso foi aceito.
        """
        if not message_id:
            return False
        url = f"{GRAPH_HOST}/{self._api_version}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(req, timeout=5) as resp:
                resp.read()
            return True
        except Exception:  # noqa: BLE001 - cortesia não pode quebrar o fluxo
            logger.warning("Não foi possível sinalizar leitura/digitação", exc_info=True)
            return False

    def send_text(self, phone_number_id: str, to_number: str, text: str) -> str:
        """Envia uma mensagem de texto e retorna o id do provedor.

        Args:
            phone_number_id: Id do número de origem (destino recebido no webhook).
            to_number: Número do usuário (E.164), o mesmo que enviou (Req 6.2).
            text: Texto da resposta.

        Returns:
            O ``messages[0].id`` retornado pela Meta, ou ``""`` se ausente.
        """
        url = f"{GRAPH_HOST}/{self._api_version}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Preserva o motivo real da Meta (token expirado, allow-list, país…).
            raise _parse_graph_error(exc.code, exc.read()) from exc
        messages = body.get("messages", []) or []
        return str(messages[0].get("id", "")) if messages else ""
