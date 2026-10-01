"""Ciclo de vida da conversa: sessão do Bedrock, saudação e encerramento.

Guarda, por usuário e canal, o estado da conversa em curso:

* ``provider_session_id`` — o identificador que o **Bedrock** gera (ele recusa um
  id escolhido por nós: ``ValidationException: Session with Id ... is not
  valid``) e espera receber de volta nos turnos seguintes. É o que faz "e quanto
  tempo antes preciso pedir?" ser entendido em vez de virar ``not_found``.
* ``last_activity`` — quando o usuário falou pela última vez. Decide duas
  coisas: se ainda há contexto a retomar e se a conversa deve ser encerrada por
  inatividade.
* ``phone_number_id`` — de qual número responder no encerramento, já que ali não
  há mensagem de entrada para consultar.
* ``closed`` — evita reenviar o encerramento a cada varredura.

**Por que a expiração não usa o TTL do DynamoDB:** o TTL não é pontual — a AWS
remove o item em até 48 h depois de vencido. Um "conversa encerrada" chegando
horas atrasado seria pior que não existir. O TTL fica só como coleta de lixo
(24 h) e quem decide o fim é a varredura agendada, comparando ``last_activity``.

Escritas usam ``UpdateItem`` com atributos disjuntos: o runtime grava a sessão,
a Lambda do WhatsApp grava a atividade, e nenhum sobrescreve o outro.

Nenhuma falha aqui pode derrubar o atendimento: perder o vínculo custa
continuidade, não a resposta.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Prefixo que isola estes itens das sessões do domínio na mesma tabela.
KEY_PREFIX = "conv"

#: Janela de continuidade **e** de inatividade. Passado isso, o contexto não é
#: retomado e a conversa é encerrada — retomar assunto de horas atrás confunde
#: mais do que ajuda, e histórico ligado a um telefone não deve durar além do
#: necessário (LGPD).
JANELA_MIN = 20

#: Coleta de lixo do item. Bem maior que a janela, para a varredura ainda
#: encontrar a conversa a encerrar.
TTL_HORAS = 24


#: Prefixo do índice reverso contato do Connect → conversa. As mensagens do
#: atendente chegam identificadas só pelo ``ContactId``; sem este item não há
#: como saber para qual telefone mandar.
ATENDIMENTO_PREFIX = "atend"

#: Prefixo das marcas de mensagem já atendida (contra entrega repetida).
MENSAGEM_PREFIX = "msg"

#: Estados do transbordo gravados na conversa.
TRANSBORDO_OFERECIDO = "oferecido"
TRANSBORDO_HUMANO = "humano"
#: Pergunta fora da PROGEP: o assistente ofereceu a lista completa de contatos.
TRANSBORDO_LISTA = "lista"

#: Estados que são uma pergunta de sim/não em aberto e vencem com a janela.
_OFERTAS = frozenset({TRANSBORDO_OFERECIDO, TRANSBORDO_LISTA})

#: Vida do índice reverso. Cobre a duração máxima do chat no Connect
#: (:data:`src.adapters.connect_chat.DURACAO_CHAT_MIN`, 24 h) com folga para o
#: evento de encerramento chegar.
TTL_ATENDIMENTO_HORAS = 48


@dataclass(frozen=True)
class ConversaInativa:
    """Conversa que passou da janela sem resposta e ainda não foi encerrada."""

    channel: str
    user_key: str
    phone_number_id: str


@dataclass(frozen=True)
class EstadoTransbordo:
    """Onde a conversa está no transbordo para o atendimento humano.

    ``estado`` vazio é o caso normal: quem responde é o assistente.
    """

    estado: str = ""
    #: Quando a oferta foi feita (epoch). A oferta vale só dentro da janela.
    oferecido_em: int = 0
    #: A pergunta que o assistente não respondeu — vai ao atendente como contexto.
    pergunta: str = ""
    contato_id: str = ""
    participant_token: str = ""
    connection_token: str = ""

    @property
    def em_atendimento_humano(self) -> bool:
        return self.estado == TRANSBORDO_HUMANO and bool(self.contato_id)


@dataclass(frozen=True)
class ConversaDoAtendimento:
    """Para onde mandar o que o atendente escreveu."""

    channel: str
    user_key: str
    phone_number_id: str


def conversation_key(channel: str, user_key: str) -> str:
    """Chave estável da conversa, por canal e usuário."""
    return f"{KEY_PREFIX}#{channel}#{user_key}"


def atendimento_key(contato_id: str) -> str:
    """Chave do índice reverso de um contato do Connect."""
    return f"{ATENDIMENTO_PREFIX}#{contato_id}"


def _s(item: dict[str, Any], nome: str) -> str:
    return str((item.get(nome) or {}).get("S", "") or "")


class ConversationStore:
    """Lê e grava o estado da conversa de um usuário."""

    def __init__(
        self,
        table_name: str,
        *,
        region_name: str | None = None,
        janela_min: int = JANELA_MIN,
        client: Any | None = None,
    ) -> None:
        self._table_name = table_name
        self._janela_min = janela_min
        self._client = client
        self._region_name = region_name

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self._region_name)
        return self._client

    # -- leitura -----------------------------------------------------------------

    def get(self, channel: str, user_key: str) -> str:
        """Sessão do Bedrock a retomar, ou ``""``.

        Devolve vazio quando a última atividade passou da janela: o item ainda
        existe (o TTL é longo), mas o contexto já não deve ser retomado.
        """
        item = self._item(channel, user_key)
        if not item:
            return ""
        # **A janela conta de quando a sessão foi salva, não da última
        # atividade.** A Lambda do WhatsApp grava a atividade *antes* de chamar
        # o runtime; medir por ela fazia toda sessão parecer recente, e uma
        # sessão de dois dias atrás era reenviada ao Bedrock, que a recusa
        # (17/09/2026). Item sem a marca — gravado antes da correção — não é
        # retomado: perder o contexto uma vez é melhor que arriscar a recusa.
        salva_em = int((item.get("session_saved_at") or {}).get("N", "0") or 0)
        if not salva_em or (time.time() - salva_em) > self._janela_min * 60:
            return ""
        return str((item.get("provider_session_id") or {}).get("S", "") or "")

    def conversa_aberta(self, channel: str, user_key: str) -> bool:
        """``True`` se há conversa em andamento — usado para não repetir a saudação."""
        item = self._item(channel, user_key)
        if not item:
            return False
        if str((item.get("closed") or {}).get("BOOL", False)) == "True":
            return False
        return not self._passou_da_janela(item)

    def _item(self, channel: str, user_key: str) -> dict[str, Any]:
        if not user_key:
            return {}
        try:
            return self.client.get_item(
                TableName=self._table_name,
                Key={"session_id": {"S": conversation_key(channel, user_key)}},
            ).get("Item") or {}
        except Exception:  # noqa: BLE001 - sem contexto ainda é melhor que sem resposta
            logger.warning("Falha ao ler a conversa", exc_info=True)
            return {}

    def _passou_da_janela(self, item: dict[str, Any]) -> bool:
        ultima = int((item.get("last_activity") or {}).get("N", "0") or 0)
        return (time.time() - ultima) > self._janela_min * 60

    # -- escrita -----------------------------------------------------------------

    def registrar_atividade(
        self, channel: str, user_key: str, phone_number_id: str = ""
    ) -> bool:
        """Marca que o usuário falou agora e reabre a conversa."""
        if not user_key:
            return False
        agora = int(time.time())
        valores: dict[str, Any] = {
            ":agora": {"N": str(agora)},
            ":aberta": {"BOOL": False},
            ":ttl": {"N": str(agora + TTL_HORAS * 3600)},
            ":canal": {"S": channel},
        }
        expr = "SET last_activity = :agora, closed = :aberta, #t = :ttl, channel = :canal"
        if phone_number_id:
            expr += ", phone_number_id = :pnid"
            valores[":pnid"] = {"S": phone_number_id}
        return self._atualizar(channel, user_key, expr, valores)

    def salvar_sessao(
        self, channel: str, user_key: str, provider_session_id: str
    ) -> bool:
        """Guarda a alça de conversa devolvida pelo Bedrock."""
        if not (user_key and provider_session_id):
            return False
        agora = int(time.time())
        return self._atualizar(
            channel,
            user_key,
            "SET provider_session_id = :sid, session_saved_at = :agora, #t = :ttl",
            {
                ":sid": {"S": provider_session_id},
                ":agora": {"N": str(agora)},
                ":ttl": {"N": str(agora + TTL_HORAS * 3600)},
            },
        )

    def marcar_encerrada(self, channel: str, user_key: str) -> bool:
        """Registra que o encerramento já foi enviado."""
        return self._atualizar(
            channel, user_key, "SET closed = :fechada", {":fechada": {"BOOL": True}}
        )

    # -- transbordo para atendimento humano --------------------------------------

    def estado_transbordo(self, channel: str, user_key: str) -> EstadoTransbordo:
        """Estado do transbordo da conversa; vazio se o assistente atende.

        A oferta vence com a janela: quem volta uma hora depois e escreve "sim"
        está respondendo a outra coisa.
        """
        item = self._item(channel, user_key)
        estado = _s(item, "transbordo")
        if not estado:
            return EstadoTransbordo()
        oferecido_em = int((item.get("transbordo_em") or {}).get("N", "0") or 0)
        if estado in _OFERTAS and (
            time.time() - oferecido_em > self._janela_min * 60
        ):
            return EstadoTransbordo()
        return EstadoTransbordo(
            estado=estado,
            oferecido_em=oferecido_em,
            pergunta=_s(item, "pergunta_pendente"),
            contato_id=_s(item, "contato_id"),
            participant_token=_s(item, "participant_token"),
            connection_token=_s(item, "connection_token"),
        )

    def oferecer_transbordo(
        self,
        channel: str,
        user_key: str,
        pergunta: str,
        estado: str = TRANSBORDO_OFERECIDO,
    ) -> bool:
        """Registra a oferta em aberto: atendente (padrão) ou lista de contatos."""
        if not user_key:
            return False
        return self._atualizar(
            channel,
            user_key,
            "SET transbordo = :estado, transbordo_em = :agora, pergunta_pendente = :p",
            {
                ":estado": {"S": estado},
                ":agora": {"N": str(int(time.time()))},
                # Atributo de contato do Connect aceita até 32 KB; a pergunta
                # já foi limitada a 4096 pelo canal.
                ":p": {"S": pergunta or "-"},
            },
        )

    def limpar_transbordo(self, channel: str, user_key: str) -> bool:
        """Volta a conversa para o assistente, sem oferta nem atendimento."""
        if not user_key:
            return False
        return self._atualizar(
            channel,
            user_key,
            "REMOVE transbordo, transbordo_em, pergunta_pendente, contato_id, "
            "participant_token, connection_token",
            None,
        )

    def iniciar_atendimento_humano(
        self,
        channel: str,
        user_key: str,
        phone_number_id: str,
        *,
        contato_id: str,
        participant_token: str,
        connection_token: str,
    ) -> bool:
        """Grava o atendimento aberto e o índice reverso do contato.

        O índice é gravado **primeiro**: se a segunda escrita falhar, o
        atendente ainda consegue falar com o servidor, e o que se perde é só o
        caminho de volta — que se recupera quando o chat terminar.
        """
        if not (user_key and contato_id):
            return False
        agora = int(time.time())
        try:
            self.client.put_item(
                TableName=self._table_name,
                Item={
                    "session_id": {"S": atendimento_key(contato_id)},
                    "user_key": {"S": user_key},
                    "channel": {"S": channel},
                    "phone_number_id": {"S": phone_number_id or ""},
                    "ttl": {"N": str(agora + TTL_ATENDIMENTO_HORAS * 3600)},
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao gravar o índice do atendimento", exc_info=True)
            return False
        return self._atualizar(
            channel,
            user_key,
            "SET transbordo = :estado, transbordo_em = :agora, contato_id = :cid, "
            "participant_token = :pt, connection_token = :ct, last_activity = :agora, "
            "closed = :aberta, #t = :ttl",
            {
                ":estado": {"S": TRANSBORDO_HUMANO},
                ":agora": {"N": str(agora)},
                ":cid": {"S": contato_id},
                ":pt": {"S": participant_token},
                ":ct": {"S": connection_token},
                ":aberta": {"BOOL": False},
                ":ttl": {"N": str(agora + TTL_ATENDIMENTO_HORAS * 3600)},
            },
        )

    def atualizar_conexao(self, channel: str, user_key: str, connection_token: str) -> bool:
        """Troca o connection token vencido pelo renovado."""
        if not (user_key and connection_token):
            return False
        return self._atualizar(
            channel,
            user_key,
            "SET connection_token = :ct",
            {":ct": {"S": connection_token}},
        )

    def marcar_mensagem_processada(self, message_id: str) -> bool:
        """``True`` na primeira vez que esta mensagem aparece; ``False`` depois.

        **A entrega é "pelo menos uma vez".** Em 25/09/2026 duas mensagens
        enviadas durante um atendimento humano foram atendidas de novo pelo
        assistente dois minutos depois — ele respondeu "Por nada!" e ofereceu um
        atendente do nada, já com o atendimento encerrado. Sem esta marca não há
        como distinguir uma mensagem nova de uma repetida.

        A gravação é **condicional**: se duas execuções simultâneas tentarem, só
        a primeira vence. Falha de I/O devolve ``True`` — responder duas vezes é
        ruim, não responder é pior.
        """
        if not message_id:
            return True
        agora = int(time.time())
        try:
            self.client.put_item(
                TableName=self._table_name,
                Item={
                    "session_id": {"S": f"{MENSAGEM_PREFIX}#{message_id}"},
                    "ttl": {"N": str(agora + TTL_HORAS * 3600)},
                },
                ConditionExpression="attribute_not_exists(session_id)",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            codigo = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if codigo == "ConditionalCheckFailedException":
                return False
            logger.warning("Falha ao marcar a mensagem; seguindo", exc_info=True)
            return True

    def conversa_do_atendimento(self, contato_id: str) -> ConversaDoAtendimento | None:
        """Conversa ligada a um contato do Connect, pelo índice reverso."""
        if not contato_id:
            return None
        try:
            item = self.client.get_item(
                TableName=self._table_name,
                Key={"session_id": {"S": atendimento_key(contato_id)}},
            ).get("Item") or {}
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao ler o índice do atendimento", exc_info=True)
            return None
        user_key = _s(item, "user_key")
        if not user_key:
            return None
        return ConversaDoAtendimento(
            channel=_s(item, "channel") or "whatsapp",
            user_key=user_key,
            phone_number_id=_s(item, "phone_number_id"),
        )

    def finalizar_atendimento(self, channel: str, user_key: str, contato_id: str) -> bool:
        """Devolve a conversa ao assistente **uma única vez** por atendimento.

        O fim chega em dois eventos (o atendente sai, depois o chat termina) e
        o aviso ao servidor não pode sair em dobro. A condição sobre
        ``contato_id`` faz só a primeira chamada vencer; a segunda devolve
        ``False``. Também não mexe numa conversa que já abriu **outro**
        atendimento.

        Marca a conversa como encerrada: o servidor recebeu o aviso de fim e
        não deve receber, 20 minutos depois, o de inatividade.
        """
        if not (user_key and contato_id):
            return False
        parametros: dict[str, Any] = {
            "TableName": self._table_name,
            "Key": {"session_id": {"S": conversation_key(channel, user_key)}},
            "UpdateExpression": (
                "SET closed = :fechada REMOVE transbordo, transbordo_em, "
                "pergunta_pendente, contato_id, participant_token, connection_token"
            ),
            "ConditionExpression": "contato_id = :cid",
            "ExpressionAttributeValues": {
                ":fechada": {"BOOL": True},
                ":cid": {"S": contato_id},
            },
        }
        try:
            self.client.update_item(**parametros)
            return True
        except Exception as exc:  # noqa: BLE001
            codigo = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if codigo != "ConditionalCheckFailedException":
                logger.warning("Falha ao finalizar o atendimento", exc_info=True)
            return False

    def _atualizar(
        self,
        channel: str,
        user_key: str,
        expressao: str,
        valores: dict[str, Any] | None,
    ) -> bool:
        parametros: dict[str, Any] = {
            "TableName": self._table_name,
            "Key": {"session_id": {"S": conversation_key(channel, user_key)}},
            "UpdateExpression": expressao,
        }
        if valores:
            parametros["ExpressionAttributeValues"] = valores
        if "#t" in expressao:
            # ``ttl`` é palavra reservada no DynamoDB e precisa de alias.
            parametros["ExpressionAttributeNames"] = {"#t": "ttl"}
        try:
            self.client.update_item(**parametros)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao gravar a conversa", exc_info=True)
            return False

    # -- varredura de encerramento -----------------------------------------------

    def listar_inativas(self, channel: str = "whatsapp") -> list[ConversaInativa]:
        """Conversas paradas há mais que a janela e ainda não encerradas.

        **Conversa com atendente não entra.** O silêncio ali é do atendente
        pensando ou da fila andando, não do servidor que foi embora; encerrar
        mandaria "como não tive retorno" a quem está esperando a PROGEP.

        A tabela guarda uma linha por conversa ativa, então o ``Scan`` é barato.
        Se o volume crescer, o caminho é um índice por ``closed``/``last_activity``.
        """
        corte = int(time.time()) - self._janela_min * 60
        try:
            paginas = self.client.get_paginator("scan").paginate(
                TableName=self._table_name,
                FilterExpression=(
                    "begins_with(session_id, :pref) AND channel = :canal "
                    "AND closed = :aberta AND last_activity < :corte "
                    "AND (attribute_not_exists(transbordo) OR transbordo <> :humano)"
                ),
                ExpressionAttributeValues={
                    ":pref": {"S": f"{KEY_PREFIX}#{channel}#"},
                    ":canal": {"S": channel},
                    ":aberta": {"BOOL": False},
                    ":corte": {"N": str(corte)},
                    ":humano": {"S": TRANSBORDO_HUMANO},
                },
            )
            itens = [item for pagina in paginas for item in pagina.get("Items", [])]
        except Exception:  # noqa: BLE001 - a varredura não pode derrubar nada
            logger.warning("Falha ao varrer conversas inativas", exc_info=True)
            return []

        inativas: list[ConversaInativa] = []
        for item in itens:
            chave = str((item.get("session_id") or {}).get("S", ""))
            partes = chave.split("#", 2)
            if len(partes) != 3:
                continue
            inativas.append(
                ConversaInativa(
                    channel=partes[1],
                    user_key=partes[2],
                    phone_number_id=str(
                        (item.get("phone_number_id") or {}).get("S", "") or ""
                    ),
                )
            )
        return inativas
