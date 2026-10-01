"""Ponte entre o WhatsApp e o chat do Amazon Connect (transbordo humano).

O End User Messaging entrega o WhatsApp à Lambda, não ao Connect. Para um
atendente conversar com o servidor, a Lambda abre um **chat do Connect em nome
do servidor** e passa a carregar as mensagens nos dois sentidos:

```
servidor → WhatsApp → SNS → Lambda ─ SendMessage ──────────→ chat do Connect
atendente → chat do Connect ─ streaming → SNS → Lambda → SendWhatsAppMessage
```

Abrir o atendimento são três chamadas, **nesta ordem**:

1. ``StartChatContact`` — cria o contato e o põe no fluxo que só transfere para
   a fila da PROGEP. Devolve o *participant token* do servidor.
2. ``StartContactStreaming`` — manda o Connect publicar as mensagens do chat no
   tópico SNS. Tem que vir antes de o atendente aceitar, senão a primeira fala
   dele se perde.
3. ``CreateParticipantConnection`` — troca o participant token por um
   *connection token*, que é o que autoriza ``SendMessage``.

**A API de participante não usa IAM.** Quem autoriza é o token; por isso ele é
guardado na conversa e nunca vai para log.

O connection token expira antes do chat (horas, não o dia inteiro). Quando o
envio é recusado, a ponte pede um novo com o participant token; se esse também
for recusado, o chat acabou e :class:`ChatEncerrado` avisa quem chamou.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: Duração máxima do chat no Connect. 24 h acompanha a janela de atendimento da
#: Meta: depois dela o WhatsApp só aceita template, então a fala do atendente não
#: chegaria mesmo.
DURACAO_CHAT_MIN = 24 * 60

#: Papéis de participante no streaming.
PAPEL_ATENDENTE = "AGENT"
PAPEL_SERVIDOR = "CUSTOMER"

#: Tipos de evento do chat que a ponte trata.
EVENTO_ENTROU = "application/vnd.amazonaws.connect.event.participant.joined"
EVENTO_SAIU = "application/vnd.amazonaws.connect.event.participant.left"
EVENTO_FIM = "application/vnd.amazonaws.connect.event.chat.ended"


class ChatEncerrado(Exception):
    """O chat do Connect não aceita mais mensagens do servidor."""


@dataclass(frozen=True)
class AtendimentoAberto:
    contato_id: str
    participant_token: str
    connection_token: str


@dataclass(frozen=True)
class EventoDoChat:
    """Uma entrada do streaming do Connect, já sem as camadas de JSON."""

    contato_id: str
    #: ``MESSAGE``, ``EVENT`` ou ``ATTACHMENT``.
    tipo: str
    content_type: str
    papel: str
    conteudo: str = ""
    nome: str = ""

    @property
    def mensagem_do_atendente(self) -> bool:
        return (
            self.tipo == "MESSAGE"
            and self.papel == PAPEL_ATENDENTE
            and bool(self.conteudo.strip())
        )

    @property
    def atendente_entrou(self) -> bool:
        return self.tipo == "EVENT" and self.content_type == EVENTO_ENTROU and (
            self.papel == PAPEL_ATENDENTE
        )

    @property
    def anexo_do_atendente(self) -> bool:
        return self.tipo == "ATTACHMENT" and self.papel == PAPEL_ATENDENTE

    @property
    def encerra_atendimento(self) -> bool:
        """Fim do chat, ou o atendente saiu dele.

        Os dois contam: quando o atendente clica em "encerrar", o Connect manda
        ``participant.left`` e só depois — às vezes minutos depois, com o
        trabalho pós-atendimento — o ``chat.ended``. Esperar o segundo deixaria
        o servidor falando sozinho nesse intervalo.
        """
        if self.tipo != "EVENT":
            return False
        if self.content_type == EVENTO_FIM:
            return True
        return self.content_type == EVENTO_SAIU and self.papel == PAPEL_ATENDENTE


def parse_stream_event(evento: Mapping[str, Any]) -> list[EventoDoChat]:
    """Extrai as entradas do chat de um evento SNS do streaming do Connect.

    Registro malformado é ignorado sem derrubar o lote, pelo mesmo motivo do
    lado do WhatsApp: a reentrega duplicaria as mensagens boas.
    """
    eventos: list[EventoDoChat] = []
    for registro in evento.get("Records", []) or []:
        bruto = ((registro.get("Sns") or {}).get("Message")) or ""
        if not bruto:
            continue
        try:
            corpo = json.loads(bruto)
        except (ValueError, TypeError):
            logger.warning("Registro do streaming ignorado por formato inesperado")
            continue
        if not isinstance(corpo, dict):
            continue
        # ``ContactId`` muda numa transferência entre filas; o ``InitialContactId``
        # é o que foi gravado ao abrir o atendimento e não muda.
        contato = str(corpo.get("InitialContactId") or corpo.get("ContactId") or "")
        if not contato:
            continue
        eventos.append(
            EventoDoChat(
                contato_id=contato,
                tipo=str(corpo.get("Type", "") or ""),
                content_type=str(corpo.get("ContentType", "") or ""),
                papel=str(corpo.get("ParticipantRole", "") or ""),
                conteudo=str(corpo.get("Content", "") or ""),
                nome=str(corpo.get("DisplayName", "") or ""),
            )
        )
    return eventos


class ConnectChatBridge:
    """Abre o chat do servidor no Connect e envia as mensagens dele."""

    def __init__(
        self,
        instance_id: str,
        contact_flow_id: str,
        stream_topic_arn: str,
        *,
        connect_client: Any = None,
        participant_client: Any = None,
        region_name: str | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.contact_flow_id = contact_flow_id
        self.stream_topic_arn = stream_topic_arn
        self._connect = connect_client
        self._participant = participant_client
        self._region_name = region_name or os.environ.get("AWS_REGION")

    @classmethod
    def do_ambiente(cls) -> "ConnectChatBridge | None":
        """Ponte configurada pelo IaC, ou ``None`` se faltar alguma peça.

        Sem ela o canal continua respondendo; só não oferece atendente.
        """
        instancia = os.environ.get("CONNECT_INSTANCE_ID", "")
        fluxo = os.environ.get("CONNECT_TRANSFER_FLOW_ID", "")
        topico = os.environ.get("CONNECT_STREAM_TOPIC_ARN", "")
        if not (instancia and fluxo and topico):
            return None
        return cls(instancia, fluxo, topico)

    @property
    def connect(self) -> Any:
        if self._connect is None:
            import boto3

            self._connect = boto3.client("connect", region_name=self._region_name)
        return self._connect

    @property
    def participant(self) -> Any:
        if self._participant is None:
            import boto3

            self._participant = boto3.client(
                "connectparticipant", region_name=self._region_name
            )
        return self._participant

    # -- abertura ----------------------------------------------------------------

    def abrir(
        self,
        *,
        nome: str,
        atributos: Mapping[str, str],
        contexto: str,
        client_token: str = "",
    ) -> AtendimentoAberto:
        """Abre o chat, liga o streaming e entrega o contexto ao atendente.

        Falha em qualquer das três chamadas sobe: um atendimento aberto pela
        metade (contato na fila sem streaming) faria o atendente responder para
        ninguém, e o servidor precisa saber que a transferência não aconteceu.
        """
        parametros: dict[str, Any] = {
            "InstanceId": self.instance_id,
            "ContactFlowId": self.contact_flow_id,
            "ParticipantDetails": {"DisplayName": nome[:256]},
            # Atributos do contato aceitam só string, até 32 KB no total.
            "Attributes": {k: str(v)[:1024] for k, v in atributos.items()},
            "ChatDurationInMinutes": DURACAO_CHAT_MIN,
        }
        if client_token:
            parametros["ClientToken"] = client_token[:500]
        inicio = self.connect.start_chat_contact(**parametros)
        contato_id = inicio["ContactId"]
        participant_token = inicio["ParticipantToken"]

        self.connect.start_contact_streaming(
            InstanceId=self.instance_id,
            ContactId=contato_id,
            ChatStreamingConfiguration={"StreamingEndpointArn": self.stream_topic_arn},
            ClientToken=f"stream-{contato_id}"[:500],
        )
        connection_token = self._conectar(participant_token)

        if contexto.strip():
            try:
                self._enviar(connection_token, contexto)
            except Exception:  # noqa: BLE001 - o atendimento já existe
                # O contato já está na fila e os atributos levam a pergunta;
                # perder a mensagem de contexto não justifica desfazer tudo.
                logger.warning("Contexto não entregue ao chat %s", contato_id, exc_info=True)

        return AtendimentoAberto(contato_id, participant_token, connection_token)

    def _conectar(self, participant_token: str) -> str:
        resposta = self.participant.create_participant_connection(
            Type=["CONNECTION_CREDENTIALS"],
            ParticipantToken=participant_token,
            ConnectParticipant=True,
        )
        return resposta["ConnectionCredentials"]["ConnectionToken"]

    def _enviar(self, connection_token: str, texto: str) -> None:
        self.participant.send_message(
            ContentType="text/plain",
            Content=texto,
            ConnectionToken=connection_token,
        )

    # -- conversa em andamento ---------------------------------------------------

    def enviar(self, participant_token: str, connection_token: str, texto: str) -> str:
        """Entrega a mensagem do servidor ao atendente.

        Returns:
            O connection token em uso — o mesmo, ou um novo se o anterior tinha
            expirado. Quem chama deve gravar o devolvido.

        Raises:
            ChatEncerrado: se nem um token novo é aceito — o chat acabou.
        """
        if connection_token:
            try:
                self._enviar(connection_token, texto)
                return connection_token
            except Exception:  # noqa: BLE001 - token vencido ou chat encerrado
                logger.info("Envio recusado com o token atual; renovando conexão")

        if not participant_token:
            raise ChatEncerrado("sem participant token")
        try:
            novo = self._conectar(participant_token)
            self._enviar(novo, texto)
        except Exception as exc:  # noqa: BLE001
            raise ChatEncerrado(str(exc)) from exc
        return novo
