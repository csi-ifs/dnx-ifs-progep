"""I/O adapter for the Conector_WhatsApp (AWS End User Messaging + Connect).

Implements Task 12.6 of the Assistente PROGEP-IFS spec. This is the thin I/O
boundary for the WhatsApp institutional channel. All decisions are delegated to
the pure-logic layer and reused here rather than re-implemented:

* :func:`src.domain.input_validation.classify_content` decides whether the
  incoming message is text-only. Non-text media (image/document/audio) is
  discarded and the Usuario is informed that only text is supported, without
  forwarding anything to the Agente_IA (Req 6.3).
* :func:`src.domain.input_validation.validate_message` enforces the 1..4096
  character limit for the WhatsApp channel; out-of-range messages are rejected
  with a limit warning and are not forwarded (Req 6.4).
* :func:`src.domain.retry.plan_retries` / :func:`src.domain.retry.decide_retry`
  bound the delivery attempts to 3 tries with a minimum interval of 10 s between
  attempts (Req 6.5, Property 10), using
  :class:`~src.domain.retry.RetryOperation.WHATSAPP_DELIVERY`.
* :func:`src.domain.availability.signal_unavailable` produces the structured
  unavailability result while preserving the received message unchanged, used
  when forwarding to Amazon Connect fails (Req 6.6, Property 9), keyed on
  :class:`~src.domain.availability.Dependency.CONNECT_FORWARDING`.

Responsibilities mapped to acceptance criteria:

* **Req 6.1** — a text message received on the official WhatsApp number is
  forwarded to the Agente_IA *through Amazon Connect* (a Connect chat contact).
* **Req 6.2** — a response generated for a WhatsApp-originated message is
  delivered back to the same WhatsApp number via AWS End User Messaging Social.
* **Req 6.3** — non-text media is discarded with an "apenas texto" warning and
  is not forwarded.
* **Req 6.4** — messages longer than 4096 characters are rejected and the limit
  is reported to the Usuario.
* **Req 6.5** — delivery is retried up to 3 times, honouring a minimum interval
  of 10 s between attempts.
* **Req 6.6** — when forwarding to Amazon Connect fails, the Usuario is informed
  of the temporary unavailability and the received message is preserved.

The ``boto3`` import is guarded so this module imports even when ``boto3`` is not
installed (e.g. unit-test/CI runners without the AWS SDK). The Amazon Connect
client and the AWS End User Messaging Social client may be injected for testing;
otherwise real clients are lazily built. The inter-attempt wait is performed via
an injectable ``sleeper`` so tests do not have to wait the real 10 s interval.

.. note::
   **Arquitetura superada (decisão de 11/09/2026).** O Req 6.1 deixou de exigir
   que *toda* mensagem passe pelo Amazon Connect: o desenho novo é
   ``WhatsApp -> End User Messaging Social -> SNS -> Agente_IA``, com o Connect
   acionado **só no transbordo**. Pagar um contato de chat por pergunta que o
   assistente responde sozinho é custo sem contrapartida.

   Este módulo ainda implementa o caminho antigo e será reescrito. Ele **não**
   está no caminho em produção hoje — o canal ativo é o webhook interino
   (``lambda_entry.whatsapp_handler``), que não toca no Connect.
   Ver ``docs/documentacao-tecnica.md`` §6.4.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from src.domain.availability import Dependency, signal_unavailable
from src.domain.config import MAX_RETRIES, Channel
from src.domain.input_validation import (
    TEXT_ONLY_WARNING,
    classify_content,
    validate_message,
)
from src.domain.retry import RetryOperation, decide_retry, plan_retries

# --- Guarded boto3 import -------------------------------------------------------
# The module must import even when boto3/botocore are absent, so the adapter can
# be unit-tested via mocking. When present we catch the SDK's transient errors.
try:  # pragma: no cover - exercised indirectly depending on environment
    import boto3
    from botocore.config import Config as _BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError

    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    boto3 = None  # type: ignore[assignment]
    _BotoConfig = None  # type: ignore[assignment]

    class BotoCoreError(Exception):  # type: ignore[no-redef]
        """Fallback when botocore is unavailable."""

    class ClientError(Exception):  # type: ignore[no-redef]
        """Fallback when botocore is unavailable."""

    _BOTO3_AVAILABLE = False


#: Exceptions treated as a *failed* (retryable) attempt for delivery/forwarding.
#: Network and SDK transient errors count as failures against the attempt budget.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    BotoCoreError,
    ClientError,
    ConnectionError,
    TimeoutError,
)

#: boto3 service name for AWS End User Messaging Social (WhatsApp).
SOCIAL_MESSAGING_SERVICE: str = "socialmessaging"
#: boto3 service name for Amazon Connect.
CONNECT_SERVICE: str = "connect"
#: Meta WhatsApp Cloud API version used for outbound message payloads.
META_API_VERSION: str = "v20.0"

#: Sanitized, user-facing message when the WhatsApp message cannot be forwarded
#: to the Agente_IA via Amazon Connect (Req 6.6). Free of internal detail (Req 10.4).
FORWARDING_UNAVAILABLE_MESSAGE: str = (
    "O atendimento está temporariamente indisponível. Sua mensagem foi "
    "recebida e você pode tentar novamente em instantes."
)


@dataclass(frozen=True)
class IncomingWhatsAppMessage:
    """A message received on the institutional WhatsApp number.

    Attributes:
        from_number: The Usuario's WhatsApp phone number (E.164) — the same
            number the response must be delivered to (Req 6.2).
        message_id: The provider message identifier of the inbound message.
        text: The text body of the message (empty when the message carries only
            non-text media).
        part_types: The content part types of the message (e.g. ``("text",)`` or
            ``("text", "image")``), used to classify text-only vs. media.
        phone_number_id: The origination phone-number id (WABA number) the
            message arrived on, used as the sender for replies.
    """

    from_number: str
    message_id: str
    text: str
    part_types: tuple[str, ...] = ("text",)
    phone_number_id: str = ""


@dataclass(frozen=True)
class ReceiveResult:
    """Outcome of receiving and forwarding an inbound WhatsApp message.

    Attributes:
        forwarded: ``True`` when the text message was enqueued to the Agente_IA
            via Amazon Connect (Req 6.1).
        contact_id: The Amazon Connect contact identifier when forwarded, else
            ``None``.
        warning: User-facing warning shown when the message is rejected — the
            text-only warning for media (Req 6.3) or the limit warning for
            over-length messages (Req 6.4); ``None`` when accepted.
        discarded_media: The non-text part types that were discarded (Req 6.3);
            empty when the message was text-only.
        unavailable: ``True`` when forwarding to Amazon Connect failed (Req 6.6).
        user_message: Sanitized message to show the Usuario on unavailability
            (Req 6.6); ``None`` otherwise.
        preserved_message: The received message, preserved unchanged so it is
            never lost on a forwarding failure (Req 6.6).
    """

    forwarded: bool
    contact_id: str | None
    warning: str | None
    discarded_media: tuple[str, ...]
    unavailable: bool
    user_message: str | None
    preserved_message: IncomingWhatsAppMessage


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of delivering an agent response to the Usuario over WhatsApp.

    Attributes:
        delivered: ``True`` when the response was accepted by End User Messaging.
        message_id: The provider message identifier when delivered, else ``None``.
        attempts_made: Number of delivery attempts actually performed
            (``1..MAX_RETRIES``).
        unavailable: ``True`` when all attempts were exhausted without delivery
            (Req 6.5).
        user_message: Sanitized message on exhaustion (Req 6.5); ``None`` on
            success.
        waits_s: The inter-attempt waits actually applied, in seconds; each is
            ``>= 10`` (Req 6.5).
    """

    delivered: bool
    message_id: str | None
    attempts_made: int
    unavailable: bool
    user_message: str | None
    waits_s: tuple[int, ...] = field(default_factory=tuple)


def _build_social_client(region_name: str | None) -> Any:
    """Build a real AWS End User Messaging Social client.

    Raises:
        RuntimeError: If ``boto3`` is not available in the environment.
    """
    if not _BOTO3_AVAILABLE:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "boto3 is not available; inject a social messaging client for "
            "testing or install boto3 to use the real End User Messaging adapter."
        )
    return boto3.client(SOCIAL_MESSAGING_SERVICE, region_name=region_name)


def _build_connect_client(region_name: str | None) -> Any:
    """Build a real Amazon Connect client.

    Raises:
        RuntimeError: If ``boto3`` is not available in the environment.
    """
    if not _BOTO3_AVAILABLE:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "boto3 is not available; inject a Connect client for testing or "
            "install boto3 to use the real Amazon Connect adapter."
        )
    return boto3.client(CONNECT_SERVICE, region_name=region_name)


class WhatsAppConnectorAdapter:
    """I/O adapter integrating the institutional WhatsApp channel.

    The pure validation/classification/retry/unavailability decisions are
    delegated to the domain layer; this class only performs the Amazon Connect
    and End User Messaging Social calls, bounds delivery to 3 attempts spaced by
    at least 10 s, discards non-text media and enforces the 4096-char limit.

    Args:
        instance_id: The Amazon Connect instance identifier.
        contact_flow_id: The contact flow that routes the WhatsApp chat to the
            agent.
        origination_phone_number_id: The AWS End User Messaging phone-number id
            (WABA number) used to send replies when the inbound message does not
            carry one.
        connect_client: Optional pre-built Amazon Connect client (enables unit
            testing via mocking); a real client is built lazily otherwise.
        social_client: Optional pre-built End User Messaging Social client
            (enables unit testing via mocking); a real client is built lazily
            otherwise.
        region_name: AWS region used only when building real clients.
        sleeper: Callable used to wait between delivery attempts. Defaults to
            :func:`time.sleep`; inject a no-op in tests to avoid real waiting.
    """

    def __init__(
        self,
        instance_id: str,
        contact_flow_id: str,
        origination_phone_number_id: str,
        *,
        connect_client: Any | None = None,
        social_client: Any | None = None,
        region_name: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.instance_id = instance_id
        self.contact_flow_id = contact_flow_id
        self.origination_phone_number_id = origination_phone_number_id
        self._region_name = region_name
        self._connect_client = connect_client
        self._social_client = social_client
        self._sleeper = sleeper

    # -- lazy clients ------------------------------------------------------------

    @property
    def connect_client(self) -> Any:
        """The Amazon Connect client, built lazily on first use if not injected."""
        if self._connect_client is None:
            self._connect_client = _build_connect_client(self._region_name)
        return self._connect_client

    @property
    def social_client(self) -> Any:
        """The End User Messaging Social client, built lazily if not injected."""
        if self._social_client is None:
            self._social_client = _build_social_client(self._region_name)
        return self._social_client

    # -- sending helpers ---------------------------------------------------------

    def _origination_id(self, message: IncomingWhatsAppMessage) -> str:
        """Resolve the origination phone-number id to send a reply from."""
        return message.phone_number_id or self.origination_phone_number_id

    def _send_text(self, to_number: str, origination_id: str, text: str) -> str:
        """Send a single WhatsApp text message via End User Messaging Social.

        Returns:
            The provider message identifier of the sent message.
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": text},
        }
        resp = self.social_client.send_whatsapp_message(
            originationPhoneNumberId=origination_id,
            metaApiVersion=META_API_VERSION,
            message=json.dumps(payload).encode("utf-8"),
        )
        return resp.get("messageId", "")

    def _send_warning(self, message: IncomingWhatsAppMessage, warning: str) -> None:
        """Best-effort delivery of a user-facing warning back over WhatsApp.

        Warnings (media not supported, limit exceeded) are informational; a
        failure to deliver the warning must not raise from the receive path.
        """
        try:
            self._send_text(message.from_number, self._origination_id(message), warning)
        except RETRYABLE_EXCEPTIONS:  # pragma: no cover - best effort
            pass

    # -- forwarding to Amazon Connect (Req 6.1) ----------------------------------

    def _forward_to_connect(self, message: IncomingWhatsAppMessage) -> str:
        """Enqueue the inbound text as an Amazon Connect chat contact (Req 6.1).

        Returns:
            The Amazon Connect contact identifier.
        """
        resp = self.connect_client.start_chat_contact(
            InstanceId=self.instance_id,
            ContactFlowId=self.contact_flow_id,
            Attributes={
                "channel": Channel.WHATSAPP.value,
                "whatsapp_from": message.from_number,
                "whatsapp_message_id": message.message_id,
            },
            ParticipantDetails={"DisplayName": message.from_number},
            InitialMessage={
                "ContentType": "text/plain",
                "Content": message.text,
            },
        )
        return resp["ContactId"]

    # -- receive (Req 6.1, 6.3, 6.4, 6.6) ----------------------------------------

    def receive_message(self, message: IncomingWhatsAppMessage) -> ReceiveResult:
        """Receive an inbound WhatsApp message and forward text to the agent.

        The message is first classified as text-only or media-bearing
        (Req 6.3); non-text media is discarded with an "apenas texto" warning and
        nothing is forwarded. Text-only messages are then size-validated against
        the 4096-char WhatsApp limit (Req 6.4); over-length messages are rejected
        with a limit warning. A valid text message is forwarded to the Agente_IA
        via an Amazon Connect chat contact (Req 6.1). If forwarding fails, the
        Usuario is informed of the temporary unavailability and the received
        message is preserved unchanged (Req 6.6).

        Args:
            message: The inbound WhatsApp message.

        Returns:
            A :class:`ReceiveResult` describing the outcome.
        """
        # Req 6.3: discard non-text media without forwarding.
        classification = classify_content(message.part_types, Channel.WHATSAPP.value)
        if not classification.consult_kb:
            warning = classification.warning or TEXT_ONLY_WARNING
            self._send_warning(message, warning)
            return ReceiveResult(
                forwarded=False,
                contact_id=None,
                warning=warning,
                discarded_media=classification.discarded_media,
                unavailable=False,
                user_message=None,
                preserved_message=message,
            )

        # Req 6.4: enforce the 1..4096 character limit before forwarding.
        validation = validate_message(message.text, Channel.WHATSAPP.value)
        if not validation.accepted:
            warning = validation.reason or ""
            self._send_warning(message, warning)
            return ReceiveResult(
                forwarded=False,
                contact_id=None,
                warning=warning,
                discarded_media=(),
                unavailable=False,
                user_message=None,
                preserved_message=message,
            )

        # Req 6.1: forward the text to the Agente_IA via Amazon Connect.
        try:
            contact_id = self._forward_to_connect(message)
        except RETRYABLE_EXCEPTIONS:
            # Req 6.6: forwarding failed -> inform unavailability, preserve input.
            result = signal_unavailable(
                Dependency.CONNECT_FORWARDING,
                message,
                escalate=False,
                user_message=FORWARDING_UNAVAILABLE_MESSAGE,
            )
            self._send_warning(message, result.user_message)
            return ReceiveResult(
                forwarded=False,
                contact_id=None,
                warning=None,
                discarded_media=(),
                unavailable=True,
                user_message=result.user_message,
                preserved_message=message,
            )

        return ReceiveResult(
            forwarded=True,
            contact_id=contact_id,
            warning=None,
            discarded_media=(),
            unavailable=False,
            user_message=None,
            preserved_message=message,
        )

    # -- deliver (Req 6.2, 6.5) --------------------------------------------------

    def deliver_response(
        self,
        to_number: str,
        text: str,
        *,
        origination_phone_number_id: str | None = None,
        max_attempts: int = MAX_RETRIES,
    ) -> DeliveryResult:
        """Deliver an agent response to the Usuario over WhatsApp (Req 6.2, 6.5).

        Delivery is attempted up to 3 times; between consecutive attempts a
        minimum interval of 10 s is honoured (Req 6.5, Property 10). On success
        the provider message id is returned; on exhaustion a structured
        unavailability result is returned.

        Args:
            to_number: The Usuario's WhatsApp number (same as the inbound one,
                Req 6.2).
            text: The response text to deliver.
            origination_phone_number_id: Optional origination number id to send
                from; defaults to the adapter's configured number.
            max_attempts: Maximum delivery attempts (``1..MAX_RETRIES``).

        Returns:
            A :class:`DeliveryResult` describing the outcome.
        """
        origination_id = origination_phone_number_id or self.origination_phone_number_id
        # Validate/normalize the bound up front (raises on invalid input).
        plan = plan_retries(RetryOperation.WHATSAPP_DELIVERY, max_attempts=max_attempts)
        bound = plan.max_attempts

        attempts_made = 0
        waits: list[int] = []
        while True:
            try:
                message_id = self._send_text(to_number, origination_id, text)
            except RETRYABLE_EXCEPTIONS:
                attempts_made += 1
                decision = decide_retry(
                    RetryOperation.WHATSAPP_DELIVERY,
                    attempts_made,
                    max_attempts=bound,
                )
                if decision.should_retry:
                    # Req 6.5: honour the >= 10 s minimum interval between tries.
                    wait_s = decision.wait_s or 0
                    if wait_s > 0:
                        self._sleeper(wait_s)
                    waits.append(wait_s)
                    continue
                # Req 6.5: all attempts exhausted -> signal unavailability.
                result = signal_unavailable(
                    Dependency.CONNECT_FORWARDING,
                    text,
                    escalate=False,
                    user_message=FORWARDING_UNAVAILABLE_MESSAGE,
                )
                return DeliveryResult(
                    delivered=False,
                    message_id=None,
                    attempts_made=attempts_made,
                    unavailable=True,
                    user_message=result.user_message,
                    waits_s=tuple(waits),
                )

            attempts_made += 1
            return DeliveryResult(
                delivered=True,
                message_id=message_id,
                attempts_made=attempts_made,
                unavailable=False,
                user_message=None,
                waits_s=tuple(waits),
            )
