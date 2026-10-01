"""Production Lambda entry points wiring the real AWS adapters.

The pure handlers in :mod:`src.adapters.api_handler` receive their dependencies
by injection so they stay unit-testable. This module is the thin production
wiring used by the deployed Lambdas: it builds the real adapters from
environment configuration (set by the IaC in :mod:`src.infra.resources`) and
delegates to the injectable handlers.

Environment variables (API Lambda):

* ``KNOWLEDGE_BASE_ID`` — the Bedrock Knowledge Base to query (required).
* ``RELEVANCE_THRESHOLD`` — ``limiar_relevancia`` (optional, default ``0.4``).
* ``AWS_REGION`` — provided by the Lambda runtime.

The Bedrock clients created by :class:`~src.adapters.agent.BedrockAgentAdapter`
carry the mandatory ``User_Agent_APN`` billing annotation (Req 8.3, 8.5).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.adapters.agentcore_client import AgentCoreRuntimeAdapter
from src.adapters.api_handler import handler as _api_handler
from src.domain.config import AgentStatus, Channel
from src.domain.fontes import formatar_fontes
from src.domain.input_validation import classify_content, validate_message
from src.domain.social import TipoSocial, classificar_social
from src.domain.models import AgentRequest, AgentResponse
from src.domain.transbordo import (
    EXPEDIENTE_TEXTO,
    RespostaOferta,
    classificar_resposta_oferta,
    dentro_do_expediente,
    nome_para_atendente,
)

logger = logging.getLogger(__name__)

#: Default relevance threshold (``limiar_relevancia``) when not configured.
DEFAULT_RELEVANCE_THRESHOLD: float = 0.4

_adapter: AgentCoreRuntimeAdapter | None = None


def _get_adapter() -> AgentCoreRuntimeAdapter:
    """Build (once) the AgentCore Runtime adapter from the environment.

    Arquitetura única: os handlers invocam o Agente_IA hospedado no Bedrock
    AgentCore Runtime (não mais o fluxo Agentic RAG in-process). O ARN do runtime
    vem de ``AGENT_RUNTIME_ARN``.
    """
    global _adapter
    if _adapter is None:
        runtime_arn = os.environ.get("AGENT_RUNTIME_ARN")
        if not runtime_arn:
            raise RuntimeError("AGENT_RUNTIME_ARN deve estar definido no ambiente")
        _adapter = AgentCoreRuntimeAdapter(
            runtime_arn, region_name=os.environ.get("AWS_REGION")
        )
    return _adapter


def _invoke_agent(request: AgentRequest) -> AgentResponse:
    """AgentInvoker: invoca o Agente_IA no AgentCore Runtime."""
    return _get_adapter().invoke(request)


def api_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry point for the API_de_Integracao (site channel).

    Wires the real Cognito verifier (default of the injectable handler) and the
    Bedrock-backed agent invoker, then delegates to
    :func:`src.adapters.api_handler.handler`.
    """
    return _api_handler(event, context, invoker=_invoke_agent)


# --- Amazon Connect contact flow ------------------------------------------------

#: Texto entregue quando o agente não encontra documento relevante; o contact
#: flow usa o ``status`` para decidir a transferência ao atendente (Req 2.4, 3.1).
CONNECT_NOT_FOUND_MESSAGE: str = (
    "Não encontrei essa informação nos documentos oficiais da PROGEP. "
    "Vou transferir você para um atendente."
)
CONNECT_UNAVAILABLE_MESSAGE: str = (
    "O atendimento automático está temporariamente indisponível. "
    "Vou transferir você para um atendente."
)


def _connect_input_text(event: dict[str, Any]) -> str:
    """Extrai o texto do usuário do evento do contact flow.

    O bloco *Invoke AWS Lambda function* envia os parâmetros em
    ``Details.Parameters``; o texto capturado pelo *Get customer input* costuma
    vir em ``Parameters.text`` ou nos atributos do contato.
    """
    details = event.get("Details", {}) or {}
    params = details.get("Parameters", {}) or {}
    for key in ("text", "Text", "message", "inputText"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    attributes = (details.get("ContactData", {}) or {}).get("Attributes", {}) or {}
    for key in ("text", "message", "lastUserMessage"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def connect_handler(event: dict[str, Any], context: Any = None) -> dict[str, str]:
    """Entry point invocado pelo contact flow do Amazon Connect.

    O Connect exige que a resposta seja um objeto **plano** de pares
    chave/valor string — nada aninhado. Devolvemos ``status``, ``content`` e
    ``confidence``; o contact flow ramifica no ``status`` para responder ao
    usuário ou transferir para a fila de atendimento humano (Req 3.1, 3.2).
    """
    text = _connect_input_text(event)
    contact_data = (event.get("Details", {}) or {}).get("ContactData", {}) or {}
    session_id = str(contact_data.get("ContactId", "") or "connect")

    if not text:
        return {
            "status": AgentStatus.UNAVAILABLE.value,
            "content": "Não recebi sua mensagem. Pode repetir?",
            "confidence": "0.0",
        }

    validation = validate_message(text, Channel.SITE.value)
    if not validation.accepted:
        return {
            "status": AgentStatus.UNAVAILABLE.value,
            "content": validation.reason or "Mensagem inválida.",
            "confidence": "0.0",
        }

    request = AgentRequest(
        session_id=session_id, channel=Channel.WHATSAPP.value, text=text
    )
    try:
        response = _invoke_agent(request)
    except Exception:
        logger.exception("Falha ao invocar o Agente_IA a partir do Connect")
        return {
            "status": AgentStatus.UNAVAILABLE.value,
            "content": CONNECT_UNAVAILABLE_MESSAGE,
            "confidence": "0.0",
        }

    if response.status == AgentStatus.ANSWERED.value and response.content:
        content = response.content
    elif response.status == AgentStatus.NOT_FOUND.value:
        content = CONNECT_NOT_FOUND_MESSAGE
    else:
        content = CONNECT_UNAVAILABLE_MESSAGE

    return {
        "status": response.status,
        "content": content,
        "confidence": f"{response.confidence:.4f}",
    }


# --- WhatsApp Cloud API webhook (caminho interino de teste) --------------------
# Só é usado com o número de teste da Meta; o caminho de produção é End User
# Messaging + Connect (src.adapters.whatsapp_connector). Ver whatsapp_cloud.py.

#: Saudação enviada quando o usuário abre uma conversa nova. Vem **antes** da
#: resposta, para o servidor saber com quem está falando e o que esperar — em
#: especial que as respostas saem de documentos oficiais, e não de opinião.
_APRESENTACAO: str = (
    "Olá! Sou o assistente virtual da PROGEP do IFS. 👋\n\n"
    "Respondo dúvidas sobre gestão de pessoas com base nos documentos oficiais "
    "da Pró-Reitoria. Quando não encontro respaldo nos documentos, eu aviso e "
    "encaminho para o atendimento humano."
)
WHATSAPP_SAUDACAO: str = f"{_APRESENTACAO}\n\nVou consultar sua pergunta."

#: Quem abre a conversa com "Olá" também precisa saber com quem fala. Antes, a
#: cortesia curta respondia e a apresentação nunca chegava — o servidor só
#: descobria o que o assistente faz na segunda conversa (visto em 22/09/2026).
WHATSAPP_APRESENTACAO: str = f"{_APRESENTACAO}\n\nQual é a sua dúvida?"

#: Encerramento por inatividade. Precisa dizer que o **contexto** se perdeu, não
#: só que a conversa acabou: quem voltar depois disso e escrever "e no meu caso?"
#: não será entendido, e é justo avisar.
WHATSAPP_ENCERRAMENTO: str = (
    "Como não tive retorno, vou encerrar nosso atendimento por aqui. 😊\n\n"
    "Se precisar de algo mais, é só mandar uma nova mensagem — começaremos uma "
    "conversa nova, então lembre de incluir o assunto na pergunta."
)

#: Respostas a mensagens de cortesia. Sem isso, um "Obrigado" vai para a busca,
#: não encontra documento sobre agradecimento e volta "não encontrei essa
#: informação nos documentos oficiais" — a pior resposta possível para quem só
#: quis ser educado.
WHATSAPP_SOCIAIS: dict[TipoSocial, str] = {
    TipoSocial.SAUDACAO: (
        "Olá! Como posso ajudar com suas dúvidas sobre gestão de pessoas?"
    ),
    TipoSocial.AGRADECIMENTO: (
        "Por nada! Se precisar de mais alguma coisa, é só perguntar. 😊"
    ),
    TipoSocial.DESPEDIDA: (
        "Até logo! Sempre que precisar, é só mandar uma mensagem. 👋"
    ),
    TipoSocial.CONFIRMACAO: "Certo! Posso ajudar em mais alguma coisa?",
}

# --- Transbordo para atendimento humano ----------------------------------------
# Oferecido quando o assistente não encontra respaldo nos documentos, e só
# depois de o servidor aceitar (src.domain.transbordo explica o porquê).

_NAO_ENCONTRADO = "Não encontrei essa informação nos documentos oficiais da PROGEP."

#: Vai **depois** do contato do setor, só no horário de atendimento: o cliente
#: pediu que os contatos venham sempre e que o atendente seja opcional, porque
#: os departamentos não dão conta de todo "não encontrei" (28/09/2026).
#: A resposta pedida precisa estar no próprio texto: o servidor responde com
#: mensagem livre, e "sim"/"não" é o que o classificador reconhece.
WHATSAPP_OFERTA_ATENDENTE: str = (
    "Se preferir, posso transferir você para um atendente da PROGEP agora. "
    "Responda *sim* ou *não*."
)
#: Para quem aceitou a oferta às 17h55 e respondeu às 18h05.
WHATSAPP_FORA_DO_EXPEDIENTE: str = (
    f"O atendimento humano da PROGEP funciona {EXPEDIENTE_TEXTO}. Até lá, "
    "você pode usar os contatos que enviei."
)
#: Pergunta que nem é de gestão de pessoas: não faz sentido indicar setor nem
#: atendente da PROGEP, mas quem perguntou pode querer os contatos mesmo assim.
WHATSAPP_FORA_DA_PROGEP: str = (
    "Esse assunto não parece ser da área de gestão de pessoas, então não "
    "encontrei nada nos documentos da PROGEP."
)
WHATSAPP_OFERTA_LISTA: str = (
    "Se quiser, envio a lista de contatos dos setores da PROGEP. "
    "Responda *sim* ou *não*."
)
#: Precisa dizer que **as próximas mensagens vão para uma pessoa**: sem isso o
#: servidor continua escrevendo como se falasse com o assistente e estranha a
#: demora.
WHATSAPP_TRANSFERIDO: str = (
    "Certo! Transferi sua conversa para o atendimento da PROGEP. Um atendente "
    "vai responder por aqui assim que estiver disponível.\n\n"
    "A partir de agora, suas mensagens vão para o atendente."
)
WHATSAPP_OFERTA_RECUSADA: str = (
    "Tudo bem! Se tiver outra dúvida, é só perguntar."
)
WHATSAPP_TRANSBORDO_FALHOU: str = (
    "No momento não consegui transferir você para um atendente. Tente "
    "novamente em instantes ou procure a PROGEP diretamente."
)
WHATSAPP_ATENDENTE_ENTROU: str = "👤 {nome}, da PROGEP, entrou na conversa."
WHATSAPP_ATENDIMENTO_ENCERRADO: str = (
    "O atendimento com a PROGEP foi encerrado. Se precisar de algo mais, é só "
    "mandar uma nova mensagem."
)
WHATSAPP_ANEXO_DO_ATENDENTE: str = (
    "O atendente enviou um arquivo, mas arquivos não podem ser entregues por "
    "aqui. Peça a ele que envie o conteúdo como texto."
)

_whatsapp_secret: dict[str, str] | None = None
_conversas: Any | None = None
_ponte: Any | None = None
_ponte_lida = False


def _get_ponte() -> Any | None:
    """Ponte com o chat do Connect; ``None`` se o transbordo não está configurado.

    Sem ela o canal responde como antes da ponte existir: "não encontrei" e a
    recomendação de procurar a PROGEP, sem oferta de atendente.
    """
    global _ponte, _ponte_lida
    if not _ponte_lida:
        from src.adapters.connect_chat import ConnectChatBridge

        _ponte = ConnectChatBridge.do_ambiente()
        _ponte_lida = True
    return _ponte


def _get_conversas() -> Any | None:
    """Store do ciclo de vida da conversa; ``None`` se não configurado.

    Sem ele o canal continua funcionando — perde a saudação e o encerramento,
    não a resposta.
    """
    global _conversas
    if _conversas is None:
        tabela = os.environ.get("SESSIONS_TABLE")
        if not tabela:
            return None
        from src.adapters.conversation_store import ConversationStore

        _conversas = ConversationStore(tabela, region_name=os.environ.get("AWS_REGION"))
    return _conversas


def _get_whatsapp_secret() -> dict[str, str]:
    """Lê (uma vez) o segredo do WhatsApp do Secrets Manager (Req 10.1).

    Espera um JSON com ``verify_token``, ``access_token`` e, opcionalmente,
    ``app_secret``. O nome/ARN do segredo vem de ``WHATSAPP_SECRET_NAME``.
    """
    global _whatsapp_secret
    if _whatsapp_secret is None:
        secret_name = os.environ.get("WHATSAPP_SECRET_NAME")
        if not secret_name:
            raise RuntimeError("WHATSAPP_SECRET_NAME deve estar definido no ambiente")
        import boto3

        client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION"))
        value = client.get_secret_value(SecretId=secret_name)["SecretString"]
        _whatsapp_secret = json.loads(value)
    return _whatsapp_secret


def _whatsapp_reply_text(response: AgentResponse) -> str:
    """Escolhe o texto a devolver ao usuário no WhatsApp para uma resposta.

    A resposta fundamentada leva junto os **documentos consultados**, montados a
    partir dos ``doc_id`` que o agente devolveu — não pedidos ao modelo, que
    inventaria número de portaria com a mesma fluência com que acerta. É o que
    permite ao servidor conferir a orientação contra a norma.
    """
    if response.status == AgentStatus.ANSWERED.value and response.content:
        fontes = formatar_fontes([s.doc_id for s in response.sources])
        return f"{response.content}{fontes}"
    if response.status == AgentStatus.NOT_FOUND.value:
        return _texto_sem_resposta(response.setores)
    return (
        "O atendimento está temporariamente indisponível. Sua mensagem foi "
        "recebida e você pode tentar novamente em instantes."
    )


def _fora_da_progep(setores: tuple[str, ...]) -> bool:
    from src.domain.contatos import FORA

    return tuple(setores) == (FORA,)


def _texto_sem_resposta(setores: tuple[str, ...]) -> str:
    """"Não encontrei" com o contato do setor que costuma tratar o assunto.

    Sem classificação (runtime antigo, falha), ``formatar_contato`` cai no
    contato geral da PROGEP — nunca fica sem nenhum contato.
    """
    from src.domain.contatos import formatar_contato

    if _fora_da_progep(setores):
        return f"{WHATSAPP_FORA_DA_PROGEP}\n\n{WHATSAPP_OFERTA_LISTA}"
    return f"{_NAO_ENCONTRADO}\n\n{formatar_contato(setores)}"


def whatsapp_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry point para o webhook do Meta WhatsApp Cloud API (teste).

    GET → responde ao handshake de verificação da Meta (echo do ``hub.challenge``
    quando o ``verify_token`` confere). POST → valida a assinatura, extrai as
    mensagens de texto, encaminha cada uma ao Agente_IA e responde pelo Cloud
    API. Mídia não textual é descartada com aviso (Req 6.3) e mensagens acima de
    4096 caracteres são rejeitadas (Req 6.4), reutilizando a lógica de domínio.
    """
    from src.adapters.whatsapp_cloud import (
        WhatsAppCloudClient,
        parse_inbound_messages,
        verify_signature,
        verify_subscription,
    )

    method = (
        event.get("httpMethod")
        or (event.get("requestContext", {}) or {}).get("http", {}).get("method")
        or ""
    ).upper()
    secret = _get_whatsapp_secret()

    # -- GET: verificação do webhook -------------------------------------------
    if method == "GET":
        params = event.get("queryStringParameters") or {}
        challenge = verify_subscription(params, secret.get("verify_token", ""))
        if challenge is not None:
            return {"statusCode": 200, "body": challenge}
        return {"statusCode": 403, "body": "forbidden"}

    # -- POST: mensagens recebidas ---------------------------------------------
    raw = event.get("body") or ""
    if isinstance(raw, str):
        if event.get("isBase64Encoded"):
            import base64

            raw_bytes = base64.b64decode(raw)
        else:
            raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = bytes(raw)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not verify_signature(
        raw_bytes, headers.get("x-hub-signature-256"), secret.get("app_secret", "")
    ):
        logger.warning("Assinatura X-Hub-Signature-256 inválida; descartando payload")
        return {"statusCode": 403, "body": "invalid signature"}

    try:
        body = json.loads(raw_bytes.decode("utf-8")) if raw_bytes else {}
    except (ValueError, UnicodeDecodeError):
        # A Meta reenvia em não-2xx; responder 200 evita loop de retry de um
        # payload que nunca vai parsear.
        return {"statusCode": 200, "body": "ignored"}

    client = WhatsAppCloudClient(secret.get("access_token", ""))
    processar_mensagens_whatsapp(parse_inbound_messages(body), client)

    # A Meta exige 200 rápido para não reentregar o evento.
    return {"statusCode": 200, "body": "ok"}


def processar_mensagens_whatsapp(mensagens: list[Any], client: Any) -> int:
    """Atende as mensagens recebidas, **independente do canal de entrada**.

    Existe separada dos handlers porque o WhatsApp tem duas entradas: o webhook
    interino (HTTP da Meta, validado por HMAC) e o End User Messaging Social
    (evento SNS). O atendimento — mídia recusada, limite de tamanho, "digitando",
    cortesias, saudação de abertura, sessão e fontes citadas — é o mesmo nos
    dois; só muda como a mensagem chega e por qual cliente a resposta sai.

    ``client`` precisa oferecer ``send_text`` e ``mark_read_and_typing`` com a
    assinatura de :class:`~src.adapters.whatsapp_cloud.WhatsAppCloudClient`. O
    ``phone_number_id`` de cada mensagem é o **identificador de origem daquele
    cliente** — para o End User Messaging, o ARN da AWS, não o id da Meta — e é
    ele que fica gravado para o encerramento por inatividade responder depois.

    Returns:
        Quantas mensagens foram atendidas (qualquer desfecho, inclusive recusa).
    """
    from src.adapters.conversation_store import (
        TRANSBORDO_LISTA,
        TRANSBORDO_OFERECIDO,
        EstadoTransbordo,
    )
    from src.domain.contatos import lista_completa

    processadas = 0
    for message in mensagens:
        loja_dedup = _get_conversas()
        if loja_dedup is not None and not loja_dedup.marcar_mensagem_processada(
            message.message_id
        ):
            logger.info("[entrada] repetida msg=%s ignorada", message.message_id)
            continue
        # Sem o texto: o identificador basta para reconstituir a ordem dos
        # eventos, e a pergunta do servidor não precisa ficar em log.
        logger.info(
            "[entrada] msg=%s tipos=%s", message.message_id, ",".join(message.part_types)
        )
        processadas += 1
        # Req 6.3: descarta mídia não textual sem consultar a KB.
        classification = classify_content(message.part_types, Channel.WHATSAPP.value)
        if not classification.consult_kb:
            _safe_send(client, message, classification.warning or "Envie apenas texto.")
            continue
        # Req 6.4: limite de 4096 caracteres.
        validation = validate_message(message.text, Channel.WHATSAPP.value)
        if not validation.accepted:
            _safe_send(client, message, validation.reason or "Mensagem muito longa.")
            continue

        usuario = message.from_number or message.message_id
        loja = _get_conversas()
        transbordo = (
            loja.estado_transbordo(Channel.WHATSAPP.value, usuario)
            if loja is not None
            else EstadoTransbordo()
        )

        # Com atendente, a mensagem é dele — nem assistente, nem cortesia, nem
        # "digitando" (quem digita é a pessoa, e o indicador mentiria).
        if transbordo.em_atendimento_humano and _encaminhar_ao_atendente(
            message, loja, usuario, transbordo
        ):
            continue

        # A consulta ao agente leva alguns segundos; sem nenhum sinal a conversa
        # parece ter caído no vácuo. Marca como lida e mostra "digitando…" antes
        # de chamar o agente — o indicador some sozinho quando a resposta chega.
        client.mark_read_and_typing(message.phone_number_id or "", message.message_id)

        # Resposta à oferta de atendente. Vem antes das cortesias porque "sim"
        # e "ok" também são confirmações sociais — e ali significam "quero".
        if transbordo.estado == TRANSBORDO_OFERECIDO:
            resposta = classificar_resposta_oferta(message.text)
            if resposta is RespostaOferta.ACEITOU:
                _abrir_atendimento(client, message, loja, usuario, transbordo.pergunta)
                continue
            loja.limpar_transbordo(Channel.WHATSAPP.value, usuario)
            if resposta is RespostaOferta.RECUSOU:
                _safe_send(client, message, WHATSAPP_OFERTA_RECUSADA)
                loja.registrar_atividade(
                    Channel.WHATSAPP.value, usuario, message.phone_number_id or ""
                )
                continue
            # Nem sim nem não: é pergunta nova e segue para o assistente.
        elif transbordo.estado == TRANSBORDO_LISTA:
            resposta = classificar_resposta_oferta(message.text)
            loja.limpar_transbordo(Channel.WHATSAPP.value, usuario)
            if resposta is not RespostaOferta.OUTRA:
                _safe_send(
                    client,
                    message,
                    lista_completa()
                    if resposta is RespostaOferta.ACEITOU
                    else WHATSAPP_OFERTA_RECUSADA,
                )
                loja.registrar_atividade(
                    Channel.WHATSAPP.value, usuario, message.phone_number_id or ""
                )
                continue

        # Cortesia não vai para a Base_de_Conhecimento: responde na hora e não
        # gasta uma consulta ao agente.
        social = classificar_social(message.text)
        if social is not None:
            resposta_social = WHATSAPP_SOCIAIS[social]
            if (
                social is TipoSocial.SAUDACAO
                and loja is not None
                and not loja.conversa_aberta(Channel.WHATSAPP.value, usuario)
            ):
                resposta_social = WHATSAPP_APRESENTACAO
            _safe_send(client, message, resposta_social)
            if loja is not None:
                if social is TipoSocial.DESPEDIDA:
                    # Quem se despediu já encerrou: não faz sentido mandar o
                    # aviso de inatividade 20 minutos depois.
                    loja.marcar_encerrada(Channel.WHATSAPP.value, usuario)
                else:
                    loja.registrar_atividade(
                        Channel.WHATSAPP.value, usuario, message.phone_number_id or ""
                    )
            continue

        # Saudação só na abertura: quem já está conversando não quer ser
        # cumprimentado a cada pergunta. Vai antes da consulta, então também
        # serve de sinal de vida enquanto o agente pensa.
        if loja is not None and not loja.conversa_aberta(Channel.WHATSAPP.value, usuario):
            _safe_send(client, message, WHATSAPP_SAUDACAO)
        if loja is not None:
            loja.registrar_atividade(
                Channel.WHATSAPP.value, usuario, message.phone_number_id or ""
            )

        request = AgentRequest(
            # Chave da conversa é o remetente, não a mensagem: com o
            # message_id cada turno viraria uma conversa nova e o assistente
            # nunca entenderia um "e quanto tempo antes?".
            session_id=message.from_number or message.message_id,
            channel=Channel.WHATSAPP.value,
            text=message.text,
        )
        try:
            response = _invoke_agent(request)
            reply = _whatsapp_reply_text(response)
        except Exception:
            logger.exception("Falha ao processar mensagem WhatsApp %s", message.message_id)
            response = AgentResponse(status=AgentStatus.UNAVAILABLE.value, content="",
                                     sources=(), confidence=0.0)
            reply = _whatsapp_reply_text(response)

        # Sem respaldo nos documentos: o contato do setor já está no texto.
        # Fora da PROGEP, oferece a lista; dentro, oferece também o atendente —
        # mas só se houver quem atenda agora.
        if response.status == AgentStatus.NOT_FOUND.value:
            if _fora_da_progep(response.setores):
                if loja is not None:
                    loja.oferecer_transbordo(
                        Channel.WHATSAPP.value, usuario, message.text,
                        estado=TRANSBORDO_LISTA,
                    )
                else:
                    reply = f"{WHATSAPP_FORA_DA_PROGEP}\n\n{lista_completa()}"
            elif loja is not None and _get_ponte() is not None and dentro_do_expediente():
                reply = f"{reply}\n\n{WHATSAPP_OFERTA_ATENDENTE}"
                loja.oferecer_transbordo(Channel.WHATSAPP.value, usuario, message.text)
        _safe_send(client, message, reply)

    return processadas


def _encaminhar_ao_atendente(message: Any, loja: Any, usuario: str, transbordo: Any) -> bool:
    """Entrega a mensagem do servidor ao chat do Connect.

    Returns:
        ``False`` quando o chat já não existe (terminou sem que o evento de fim
        chegasse). A conversa volta ao assistente e a mensagem é atendida por
        ele — melhor que mandá-la para um chat que ninguém vai ler.
    """
    from src.adapters.connect_chat import ChatEncerrado

    ponte = _get_ponte()
    if ponte is None:
        loja.limpar_transbordo(Channel.WHATSAPP.value, usuario)
        return False
    try:
        token = ponte.enviar(
            transbordo.participant_token, transbordo.connection_token, message.text
        )
    except ChatEncerrado:
        logger.info("Chat %s já encerrado; conversa volta ao assistente", transbordo.contato_id)
        loja.limpar_transbordo(Channel.WHATSAPP.value, usuario)
        return False
    if token != transbordo.connection_token:
        loja.atualizar_conexao(Channel.WHATSAPP.value, usuario, token)
    loja.registrar_atividade(Channel.WHATSAPP.value, usuario, message.phone_number_id or "")
    return True


def _abrir_atendimento(
    client: Any, message: Any, loja: Any, usuario: str, pergunta: str
) -> None:
    """Abre o chat no Connect depois de o servidor aceitar a oferta.

    O expediente é conferido **de novo**: a oferta pode ter sido feita às
    17h55 e aceita às 18h05.
    """
    canal = Channel.WHATSAPP.value
    origem = message.phone_number_id or ""
    ponte = _get_ponte()
    if ponte is None or not dentro_do_expediente():
        loja.limpar_transbordo(canal, usuario)
        _safe_send(client, message, WHATSAPP_FORA_DO_EXPEDIENTE)
        loja.registrar_atividade(canal, usuario, origem)
        return

    digitos = "".join(c for c in message.from_number if c.isdigit())
    try:
        aberto = ponte.abrir(
            nome=nome_para_atendente(message.from_number),
            atributos={
                "canal": canal,
                "whatsapp": f"+{digitos}" if digitos else "",
                "pergunta": pergunta,
            },
            contexto=(
                "[Transferido pelo assistente virtual — a pergunta abaixo não "
                f"tem resposta nos documentos da PROGEP]\n\n{pergunta}"
            ),
            client_token=message.message_id,
        )
    except Exception:  # noqa: BLE001 - o servidor precisa saber que não foi
        logger.exception("Falha ao abrir o atendimento humano")
        loja.limpar_transbordo(canal, usuario)
        _safe_send(client, message, WHATSAPP_TRANSBORDO_FALHOU)
        loja.registrar_atividade(canal, usuario, origem)
        return

    loja.iniciar_atendimento_humano(
        canal,
        usuario,
        origem,
        contato_id=aberto.contato_id,
        participant_token=aberto.participant_token,
        connection_token=aberto.connection_token,
    )
    logger.info("[transbordo] atendimento aberto contato=%s", aberto.contato_id)
    _safe_send(client, message, WHATSAPP_TRANSFERIDO)


def whatsapp_social_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Entrada do WhatsApp pelo AWS End User Messaging Social (evento SNS).

    Substitui o webhook interino depois do vínculo da WABA (§6.4 da
    documentação técnica). Não há HMAC nem segredo da Meta: quem autentica é a
    AWS, e o tópico só aceita publicação do serviço.

    **O número de origem é trocado antes de atender.** O payload traz o
    ``phone_number_id`` da Meta, mas ``SendWhatsAppMessage`` exige o ARN da
    AWS, publicado no cabeçalho do evento. Com o id da Meta o envio é recusado
    por formato — e o encerramento por inatividade, que responde com a origem
    gravada, falharia 20 minutos depois, longe de qualquer pista.

    Eventos de status (enviada, entregue, lida) chegam pelo mesmo tópico e não
    trazem mensagem de usuário; saem sem atender nada.
    """
    from dataclasses import replace

    from src.adapters.whatsapp_social import (
        WhatsAppSocialClient,
        parse_sns_event,
        phone_number_id_de_origem,
        registrar_status_de_entrega,
    )

    # Aceite da AWS não é entrega: a recusa da Meta só aparece aqui.
    registrar_status_de_entrega(event)

    mensagens = parse_sns_event(event)
    if not mensagens:
        return {"processadas": 0}

    origem = phone_number_id_de_origem(event)
    if not origem:
        logger.error("Evento do End User Messaging sem ARN de origem; nada respondido")
        return {"processadas": 0, "erro": "sem_origem"}

    mensagens = [replace(m, phone_number_id=origem) for m in mensagens]
    return {"processadas": processar_mensagens_whatsapp(mensagens, WhatsAppSocialClient())}


def _safe_send(client: Any, message: Any, text: str) -> None:
    """Envia uma resposta ao WhatsApp, logando (sem propagar) em caso de falha."""
    try:
        client.send_text(message.phone_number_id, message.from_number, text)
    except Exception:  # não deixar uma falha de envio abortar o lote
        logger.exception("Falha ao enviar resposta WhatsApp para %s", message.from_number)


def _cliente_para_origem(origem: str) -> Any:
    """Cliente de envio compatível com a origem gravada na conversa.

    Na virada do webhook para o End User Messaging convivem conversas gravadas
    com o ``phone_number_id`` da Meta (começaram pelo webhook) e com o ARN da
    AWS (começaram pelo SNS). Cada uma só pode ser encerrada pelo canal por onde
    começou: o ARN não existe na Graph API, e o id da Meta é recusado pela AWS.

    O segredo da Meta só é lido quando a origem é da Meta — depois da migração
    ele pode nem existir mais, e o encerramento pelo caminho novo não pode
    depender dele.
    """
    if origem.startswith("arn:"):
        from src.adapters.whatsapp_social import WhatsAppSocialClient

        return WhatsAppSocialClient()

    from src.adapters.whatsapp_cloud import WhatsAppCloudClient

    return WhatsAppCloudClient(_get_whatsapp_secret().get("access_token", ""))


def whatsapp_encerramento_handler(
    event: dict[str, Any], context: Any = None
) -> dict[str, Any]:
    """Encerra conversas paradas há mais que a janela de inatividade.

    Disparado por uma regra agendada do EventBridge (a cada 5 min). Não dá para
    depender do TTL do DynamoDB: ele remove o item em até 48 h depois de
    vencido, e um encerramento com horas de atraso seria pior que nenhum.

    Envia a mensagem e marca a conversa como encerrada, para não repetir na
    varredura seguinte. Como a última mensagem do usuário é recente (dentro da
    janela de 24 h de atendimento da Meta), não é preciso template.
    """
    loja = _get_conversas()
    if loja is None:
        logger.warning("SESSIONS_TABLE ausente; encerramento por inatividade inativo")
        return {"encerradas": 0}

    inativas = loja.listar_inativas(Channel.WHATSAPP.value)
    if not inativas:
        return {"encerradas": 0}

    encerradas = 0
    for conversa in inativas:
        origem = conversa.phone_number_id
        if not origem:
            logger.warning("Sem phone_number_id para encerrar %s", conversa.user_key)
            continue
        try:
            client = _cliente_para_origem(origem)
            client.send_text(origem, conversa.user_key, WHATSAPP_ENCERRAMENTO)
        except Exception:  # noqa: BLE001 - uma falha não pode parar as demais
            logger.exception("Falha ao encerrar conversa com %s", conversa.user_key)
            continue
        # Só marca depois do envio: se falhar, a próxima varredura tenta de novo.
        loja.marcar_encerrada(conversa.channel, conversa.user_key)
        encerradas += 1

    logger.info("Encerramento por inatividade: %d de %d", encerradas, len(inativas))
    return {"encerradas": encerradas, "candidatas": len(inativas)}


def connect_chat_stream_handler(
    event: dict[str, Any], context: Any = None
) -> dict[str, Any]:
    """Leva ao WhatsApp o que acontece no chat do Connect (evento SNS).

    O streaming publica **tudo** do chat, inclusive o eco das mensagens do
    próprio servidor. Só quatro coisas saem daqui: a fala do atendente, a
    entrada dele, um anexo que não pode ser entregue e o fim do atendimento.
    Repassar o eco faria o servidor receber de volta cada mensagem que mandou;
    repassar as do sistema entregaria os avisos padrão do Connect, em inglês.
    """
    from src.adapters.connect_chat import parse_stream_event

    eventos = parse_stream_event(event)
    loja = _get_conversas()
    if not eventos or loja is None:
        return {"entregues": 0}

    entregues = 0
    for ev in eventos:
        if not (
            ev.mensagem_do_atendente
            or ev.atendente_entrou
            or ev.anexo_do_atendente
            or ev.encerra_atendimento
        ):
            continue
        conversa = loja.conversa_do_atendimento(ev.contato_id)
        if conversa is None:
            logger.warning("Evento de chat sem conversa ligada: contato=%s", ev.contato_id)
            continue

        if ev.encerra_atendimento:
            # Chega em dobro (atendente sai, chat termina); só o primeiro avisa.
            if not loja.finalizar_atendimento(
                conversa.channel, conversa.user_key, ev.contato_id
            ):
                continue
            texto = WHATSAPP_ATENDIMENTO_ENCERRADO
        elif ev.atendente_entrou:
            texto = WHATSAPP_ATENDENTE_ENTROU.format(nome=ev.nome or "Um atendente")
        elif ev.anexo_do_atendente:
            texto = WHATSAPP_ANEXO_DO_ATENDENTE
        else:
            texto = ev.conteudo

        try:
            _cliente_para_origem(conversa.phone_number_id).send_text(
                conversa.phone_number_id, conversa.user_key, texto
            )
            entregues += 1
        except Exception:  # noqa: BLE001 - um envio não pode parar os demais
            # Fora da janela de 24 h da Meta a mensagem livre é recusada.
            logger.exception("Falha ao entregar evento do chat %s", ev.contato_id)

    return {"entregues": entregues, "eventos": len(eventos)}
