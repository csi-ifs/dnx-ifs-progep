"""WhatsApp channel orchestration (Task 14.2).

Wires the institutional WhatsApp flow end to end:
``End User Messaging -> Amazon Connect -> Agente_IA -> entrega``.

The orchestration reuses the existing, already-tested adapters and domain
functions rather than re-implementing any decision logic:

* :class:`src.adapters.whatsapp_connector.WhatsAppConnectorAdapter` receives an
  inbound WhatsApp text message and forwards it to the Agente_IA *through Amazon
  Connect* (Req 6.1), discarding non-text media (Req 6.3) and enforcing the
  4096-char limit (Req 6.4); it also delivers the response back to the same
  number with a bounded retry (Req 6.2, 6.5).
* :class:`src.adapters.agent.BedrockAgentAdapter` forwards the request to the
  agent, queries the Knowledge Base and assembles the grounded response
  (Req 2.1-2.4), enforcing the billing annotation guard (Req 8.3, 8.5).
* :func:`src.domain.availability.signal_unavailable` produces the structured
  unavailability result while preserving the original message when the agent
  itself fails or times out (Req 6.6 / 5.5, Property 9).

Scope of this task (Req 6.1, 6.2, 6.5): orchestrate *receipt*, *forwarding via
Connect*, obtaining the *agent response* and *delivering it back with retry*.
The adapters are injected so the whole flow is unit-testable with mocks and no
AWS access.

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

from dataclasses import dataclass

from src.adapters.agent import BedrockAgentAdapter, BillingGuardError
from src.adapters.whatsapp_connector import (
    DeliveryResult,
    IncomingWhatsAppMessage,
    ReceiveResult,
    WhatsAppConnectorAdapter,
)
from src.domain.availability import Dependency, signal_unavailable
from src.domain.config import AgentStatus, Channel
from src.domain.models import AgentRequest, AgentResponse

#: User-facing text delivered when the agent finds no relevant document
#: (``not_found``). The interaction is additionally escalated by the router
#: (Req 2.4); here we only ensure the Usuario receives an intelligible message.
NOT_FOUND_MESSAGE: str = (
    "Não encontrei essa informação nos documentos oficiais da PROGEP. "
    "Vou encaminhar você para um atendente humano."
)

#: User-facing text delivered when the agent is temporarily unavailable or times
#: out (Req 6.6 / 2.5). Free of internal/technical detail (Req 10.4).
AGENT_UNAVAILABLE_MESSAGE: str = (
    "O atendimento está temporariamente indisponível. Sua mensagem foi "
    "recebida e você pode tentar novamente em instantes."
)


@dataclass(frozen=True)
class WhatsAppFlowResult:
    """Outcome of handling a single inbound WhatsApp message end to end.

    Attributes:
        forwarded: ``True`` when the text was forwarded to the Agente_IA via
            Amazon Connect (Req 6.1). ``False`` when the message was rejected
            (media discarded / over-length) or forwarding to Connect failed.
        contact_id: The Amazon Connect contact id when forwarded, else ``None``.
        receive: The :class:`ReceiveResult` from the connector (carries the
            preserved message, any warning and the unavailability flag).
        agent_response: The :class:`AgentResponse` produced by the agent, or
            ``None`` when the message was never forwarded or the agent failed.
        delivery: The :class:`DeliveryResult` of delivering the reply back to the
            Usuario (Req 6.2, 6.5), or ``None`` when nothing was delivered.
        unavailable: ``True`` when the flow could not complete because a
            dependency (Connect forwarding or the agent) was unavailable
            (Req 6.6).
    """

    forwarded: bool
    contact_id: str | None
    receive: ReceiveResult
    agent_response: AgentResponse | None
    delivery: DeliveryResult | None
    unavailable: bool


class WhatsAppFlow:
    """Orchestrates the WhatsApp channel from receipt to delivery.

    The flow performs no business decisions itself; it sequences the injected
    adapters:

    1. ``connector.receive_message`` — validate/classify and forward the inbound
       text to the agent through Amazon Connect (Req 6.1).
    2. ``agent.forward_request`` — query the Knowledge Base and assemble the
       grounded response (Req 2.1-2.4, 8.3/8.5).
    3. ``connector.deliver_response`` — deliver the reply back to the same
       WhatsApp number, retrying up to 3 times with >= 10 s between attempts
       (Req 6.2, 6.5).

    Args:
        connector: The WhatsApp connector adapter (receive + deliver).
        agent: The Bedrock agent adapter (forward request + KB query).
        relevance_threshold: The configured relevance threshold
            (``limiar_relevancia``) passed to the agent (Req 2.2, 2.4).
    """

    def __init__(
        self,
        connector: WhatsAppConnectorAdapter,
        agent: BedrockAgentAdapter,
        *,
        relevance_threshold: float,
    ) -> None:
        self.connector = connector
        self.agent = agent
        self.relevance_threshold = relevance_threshold

    def _reply_text(self, response: AgentResponse) -> str:
        """Choose the user-facing text to deliver for an agent response.

        An ``answered`` response is delivered verbatim (grounded content); a
        ``not_found`` response yields the not-found notice; any other status
        (e.g. ``unavailable``) yields the sanitized unavailability message.
        """
        if response.status == AgentStatus.ANSWERED.value and response.content:
            return response.content
        if response.status == AgentStatus.NOT_FOUND.value:
            return NOT_FOUND_MESSAGE
        return AGENT_UNAVAILABLE_MESSAGE

    def handle_incoming(self, message: IncomingWhatsAppMessage) -> WhatsAppFlowResult:
        """Handle one inbound WhatsApp message end to end.

        Receives and forwards the message via Amazon Connect (Req 6.1); when the
        message was not forwarded (media discarded, over the 4096-char limit, or
        Connect forwarding unavailable) the flow returns early without calling
        the agent, preserving the connector's outcome (including the preserved
        message on unavailability, Req 6.6). Otherwise the request is forwarded
        to the agent and the resulting reply is delivered back to the same
        WhatsApp number with a bounded retry (Req 6.2, 6.5). If the agent itself
        fails, the Usuario is informed of the temporary unavailability while the
        original message is preserved (Req 6.6 / 2.5).

        Args:
            message: The inbound WhatsApp message.

        Returns:
            A :class:`WhatsAppFlowResult` describing the end-to-end outcome.
        """
        # Step 1 (Req 6.1): receive + forward the text via Amazon Connect.
        receive = self.connector.receive_message(message)
        if not receive.forwarded:
            # Not forwarded: media discarded/over-limit (warning already sent) or
            # Connect forwarding unavailable with the message preserved (Req 6.6).
            return WhatsAppFlowResult(
                forwarded=False,
                contact_id=None,
                receive=receive,
                agent_response=None,
                delivery=None,
                unavailable=receive.unavailable,
            )

        # Step 2 (Req 2.1): forward the request to the Agente_IA.
        request = AgentRequest(
            session_id=receive.contact_id or message.message_id,
            channel=Channel.WHATSAPP.value,
            text=message.text,
        )
        try:
            response = self.agent.forward_request(request, self.relevance_threshold)
        except BillingGuardError:
            # Agent call blocked/failed: preserve the original message (Req 6.6),
            # inform the Usuario and still attempt to deliver the notice.
            signal_unavailable(
                Dependency.AGENT_RESPONSE,
                message,
                escalate=False,
                user_message=AGENT_UNAVAILABLE_MESSAGE,
            )
            delivery = self.connector.deliver_response(
                message.from_number,
                AGENT_UNAVAILABLE_MESSAGE,
                origination_phone_number_id=message.phone_number_id or None,
            )
            return WhatsAppFlowResult(
                forwarded=True,
                contact_id=receive.contact_id,
                receive=receive,
                agent_response=None,
                delivery=delivery,
                unavailable=True,
            )

        # Step 3 (Req 6.2, 6.5): deliver the reply back with bounded retry.
        delivery = self.connector.deliver_response(
            message.from_number,
            self._reply_text(response),
            origination_phone_number_id=message.phone_number_id or None,
        )
        return WhatsAppFlowResult(
            forwarded=True,
            contact_id=receive.contact_id,
            receive=receive,
            agent_response=response,
            delivery=delivery,
            unavailable=delivery.unavailable,
        )
