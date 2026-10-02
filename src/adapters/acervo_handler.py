"""Adaptador de I/O do painel de acervo (API Gateway + Lambda).

| Rota                        | Quem  | O que faz                                  |
|-----------------------------|-------|--------------------------------------------|
| ``POST /acervo/uploads``    | admin | URL pré-assinada para o bucket de preparo   |
| ``GET /acervo/preparo``     | admin | o que está aguardando confirmação           |
| ``DELETE /acervo/preparo``  | admin | descarta antes de indexar                   |
| ``POST /acervo/indexar``    | admin | copia preparo → documentos e enfileira      |
| ``GET /acervo/documentos``  | admin | o acervo (lido do S3), com procedência       |
| ``GET /acervo/conferencia`` | admin | compara o bucket com a base                  |
| ``DELETE /acervo/documentos``| admin | remoção reversível (lixeira + KB)          |
| ``GET /acervo/custos``      | admin | custos do assistente (aba "Custos")         |

**Por que ``POST /acervo/indexar`` é o único caminho para a base.** O envio cai
no bucket de preparo, que nenhum data source varre e nenhuma notificação
observa. Confirmar é copiar para o bucket de documentos — e é a cópia que
dispara o Ingestor já existente. Enquanto o arquivo estiver no preparo,
descartá-lo é apagar algo que nunca entrou na base.

**A remoção não apaga.** O objeto vai para ``removidos/`` no bucket de preparo
(fora de qualquer data source) e o documento sai da base por
``DeleteKnowledgeBaseDocuments``, que aceita a URI do S3 e dispensa job de
sincronização. O arquivo continua recuperável pelo prazo do lifecycle: um
clique que apaga norma institucional em definitivo é perigoso demais.

Todo I/O entra por injeção, então o módulo é testável sem AWS.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import quote, unquote

from src.domain.acervo import (
    NaoAutorizado,
    PedidoInvalido,
    chave_de_preparo,
    chave_publicada,
    chave_removida,
    exige_admin,
    procedencia,
    validar_chave_do_acervo,
    validar_envio,
    validar_titulo,
)
from src.domain.config import MAX_DOC_SIZE_BYTES, PREPARO_PREFIX

logger = logging.getLogger(__name__)

#: Validade da URL pré-assinada. Curta: ela autoriza escrita no bucket, e um
#: link vazado tem valor enquanto durar.
EXPIRACAO_UPLOAD_S = 900

#: Teto de documentos devolvidos numa listagem da base.
#:
#: Era 200, e cortava: com 400 documentos na base, o recém-adicionado ficava na
#: posição 389 e **sumia da tela** — justamente o que alguém acabou de enviar é
#: o que ele quer encontrar. Um teto alto o bastante para caber o acervo inteiro
#: (a resposta com 400 itens não chega a 100 KB, contra o limite de 10 MB do API
#: Gateway) evita o problema sem inventar paginação por cursor antes de haver
#: volume que a justifique.
MAX_DOCUMENTOS = 2000


def _cors() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": os.environ.get("CORS_ALLOW_ORIGIN", "*"),
        "Access-Control-Allow-Headers": "Content-Type,Authorization,x-access-token",
        "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
        "Access-Control-Max-Age": "3600",
    }


def _resposta(codigo: int, corpo: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": codigo,
        "headers": {"Content-Type": "application/json", **_cors()},
        "body": json.dumps(corpo, ensure_ascii=False),
    }


def claims_do_evento(evento: Mapping[str, Any]) -> Mapping[str, Any]:
    """Claims que o authorizer do Cognito anexou à requisição."""
    ctx = evento.get("requestContext") or {}
    autorizador = ctx.get("authorizer") or {}
    claims = autorizador.get("claims")
    return claims if isinstance(claims, Mapping) else {}


def grupos_das_claims(claims: Mapping[str, Any]) -> list[str]:
    """Lê ``cognito:groups``, que chega em formatos diferentes.

    No authorizer de REST API a claim vem **achatada em string**: um grupo vira
    ``"administradores"`` e vários viram ``"[a, b]"`` — com colchetes, sem
    aspas, e portanto não é JSON. Tratar só a lista faria a autorização falhar
    exatamente para quem tem mais de um grupo.
    """
    bruto = claims.get("cognito:groups")
    if isinstance(bruto, (list, tuple)):
        return [str(g).strip() for g in bruto if str(g).strip()]
    if not isinstance(bruto, str) or not bruto.strip():
        return []
    texto = bruto.strip().strip("[]")
    return [g.strip() for g in texto.split(",") if g.strip()]


def _corpo(evento: Mapping[str, Any]) -> dict[str, Any]:
    bruto = evento.get("body")
    if isinstance(bruto, Mapping):
        return dict(bruto)
    if not isinstance(bruto, str) or not bruto.strip():
        return {}
    try:
        analisado = json.loads(bruto)
    except (ValueError, TypeError):
        raise PedidoInvalido("Corpo do pedido não é JSON válido.")
    return analisado if isinstance(analisado, dict) else {}


def _rota(evento: Mapping[str, Any]) -> str:
    metodo = str(evento.get("httpMethod") or "").upper()
    caminho = str(evento.get("path") or "").rstrip("/")
    # `/acervo/uploads` e `/prod/acervo/uploads` (com o stage no caminho) têm de
    # casar com a mesma rota.
    if "/acervo" in caminho:
        caminho = "/acervo" + caminho.split("/acervo", 1)[1]
    return f"{metodo} {caminho}"


# --- Operações ------------------------------------------------------------------

def _pedir_upload(corpo: dict, sub: str, s3: Any, bucket_preparo: str) -> dict:
    """URL pré-assinada de escrita no bucket de preparo.

    POST pré-assinado, e não PUT: só ele aceita ``content-length-range``, que é
    o que impede um cliente de enviar um arquivo maior do que declarou.
    """
    nome, tamanho = validar_envio(corpo)
    chave = chave_de_preparo(sub, nome)
    assinado = s3.generate_presigned_post(
        Bucket=bucket_preparo,
        Key=chave,
        Fields={},
        Conditions=[["content-length-range", 1, MAX_DOC_SIZE_BYTES]],
        ExpiresIn=EXPIRACAO_UPLOAD_S,
    )
    return {"url": assinado["url"], "campos": assinado["fields"], "chave": chave,
            "nome": nome, "tamanho": tamanho}


def _listar_preparo(sub: str, s3: Any, bucket_preparo: str) -> dict:
    """O que este administrador enviou e ainda não confirmou."""
    prefixo = f"{PREPARO_PREFIX}{sub}/"
    itens: list[dict] = []
    paginas = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket_preparo, Prefix=prefixo
    )
    for pagina in paginas:
        for obj in pagina.get("Contents", []) or []:
            chave = obj["Key"]
            itens.append(
                {
                    "chave": chave,
                    "nome": chave.rsplit("/", 1)[-1],
                    "tamanho": obj.get("Size", 0),
                    "enviado_em": obj.get("LastModified").isoformat()
                    if obj.get("LastModified")
                    else None,
                }
            )
    return {"itens": itens, "total": len(itens)}


def _garantir_do_usuario(chave: str, sub: str) -> str:
    """Um administrador só mexe no próprio preparo.

    Sem esta conferência, a chave vinda do cliente permitiria confirmar ou
    descartar o envio de outro administrador — arquivo que ele não viu e não
    revisou, entrando na base com o nome dele.
    """
    esperado = f"{PREPARO_PREFIX}{sub}/"
    chave = str(chave or "").strip().lstrip("/")
    if ".." in chave or not chave.startswith(esperado) or chave == esperado:
        raise PedidoInvalido("Documento não encontrado no seu preparo.")
    return chave


def _descartar_preparo(corpo: dict, sub: str, s3: Any, bucket_preparo: str) -> dict:
    chave = _garantir_do_usuario(corpo.get("chave", ""), sub)
    s3.delete_object(Bucket=bucket_preparo, Key=chave)
    return {"descartado": chave}


def _indexar(
    corpo: dict, sub: str, s3: Any, bucket_preparo: str, bucket_documentos: str
) -> dict:
    """Move do preparo para o acervo. É aqui que o documento entra na base.

    A cópia para o bucket de documentos dispara a notificação que já existe →
    Ingestor → ingestion job. Não é preciso chamar a KB daqui.
    """
    chave = _garantir_do_usuario(corpo.get("chave", ""), sub)
    titulo = validar_titulo(corpo.get("titulo"))
    nome = chave.rsplit("/", 1)[-1]
    destino = chave_publicada(nome)

    argumentos: dict[str, Any] = {
        "Bucket": bucket_documentos,
        "Key": destino,
        "CopySource": {"Bucket": bucket_preparo, "Key": chave},
    }
    if titulo:
        # Metadado de objeto no S3 só trafega em ASCII (é cabeçalho HTTP), então
        # o título acentuado vai percent-encoded e é decodificado na leitura.
        argumentos["Metadata"] = {"titulo": quote(titulo)}
        argumentos["MetadataDirective"] = "REPLACE"
    s3.copy_object(**argumentos)
    # Só apaga o preparo depois da cópia: se a cópia falhar, o envio continua
    # na lista para nova tentativa em vez de sumir sem ter entrado na base.
    s3.delete_object(Bucket=bucket_preparo, Key=chave)
    return {"indexado": destino, "nome": nome, "titulo": titulo,
            "situacao": "na fila de indexação"}


def _listar_documentos(s3: Any, bucket_documentos: str) -> dict:
    """O acervo, lido do S3 — a fonte da verdade do que existe.

    **Não vem da Knowledge Base, e a diferença importa.** Medido no acervo
    atual: `list_objects_v2` devolve 404 objetos em 0,89 s numa única página,
    enquanto `ListKnowledgeBaseDocuments` devolve 399 documentos em ~6 s e 16
    páginas (o `maxResults` do modelo é ignorado pelo serviço, então não há como
    pedir menos viagens).

    Os 5 de diferença são o motivo real da troca: são documentos que estão no
    bucket e **falharam ao indexar**. Listar pela base os esconderia justamente
    quando alguém precisa saber que existem e não respondem. A situação de cada
    um vem da conferência (:func:`_conferir`), sob demanda.
    """
    itens: list[dict] = []
    paginas = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket_documentos)
    for pagina in paginas:
        for obj in pagina.get("Contents", []) or []:
            chave = obj["Key"]
            if chave.endswith("/"):  # marcador de "pasta", não é documento
                continue
            proc = procedencia(chave)
            itens.append(
                {
                    "chave": chave,
                    "nome": unquote(chave.rsplit("/", 1)[-1]),
                    "tamanho": obj.get("Size", 0),
                    "atualizado_em": obj.get("LastModified").isoformat()
                    if obj.get("LastModified")
                    else None,
                    "origem": proc.origem,
                    "regenerave": proc.regenerave,
                    "aviso": proc.aviso,
                }
            )
            if len(itens) >= MAX_DOCUMENTOS:
                return {
                    "itens": _mais_recentes_primeiro(itens),
                    "total": len(itens),
                    "truncado": True,
                }
    return {
        "itens": _mais_recentes_primeiro(itens),
        "total": len(itens),
        "truncado": False,
    }


def _conferir(s3: Any, kb: Any, bucket_documentos: str, kb_id: str, ds_id: str) -> dict:
    """Compara o bucket com a base, nome a nome.

    É a única verificação confiável de que um documento **responde**: o
    ingestion job diz `COMPLETE` com falhas contadas mas não nomeadas, então um
    PDF que não indexou desaparece em silêncio — está no acervo, e o assistente
    age como se não estivesse.

    ``ausentes`` está no S3 e não na base (o caso a investigar). ``orfaos`` está
    na base e não no S3 — sobra de remoção que não completou.
    """
    no_s3 = {
        obj["Key"]
        for pagina in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket_documentos
        )
        for obj in pagina.get("Contents", []) or []
        if not obj["Key"].endswith("/")
    }

    indexados: set[str] = set()
    por_situacao: dict[str, int] = {}
    for pagina in kb.get_paginator("list_knowledge_base_documents").paginate(
        knowledgeBaseId=kb_id, dataSourceId=ds_id
    ):
        for doc in pagina.get("documentDetails", []) or []:
            uri = ((doc.get("identifier") or {}).get("s3") or {}).get("uri", "")
            chave = uri.split("/", 3)[-1] if uri.startswith("s3://") else uri
            indexados.add(chave)
            situacao = str(doc.get("status", "") or "")
            por_situacao[situacao] = por_situacao.get(situacao, 0) + 1

    return {
        "no_s3": len(no_s3),
        "indexados": sorted(indexados),
        "ausentes": sorted(no_s3 - indexados),
        "orfaos": sorted(indexados - no_s3),
        "por_situacao": por_situacao,
    }


def _mais_recentes_primeiro(itens: list[dict]) -> list[dict]:
    """Ordena por atualização, do mais novo para o mais antigo.

    A ordem da API é a do data source, que não diz nada a quem está na tela.
    Quem abre esta aba quase sempre quer o documento que acabou de enviar ou o
    que acabou de mudar — pô-lo no topo é o que torna a lista útil sem busca.
    Documento sem data vai para o fim, não para o começo.
    """
    return sorted(itens, key=lambda i: i.get("atualizado_em") or "", reverse=True)


def _remover_documento(
    corpo: dict,
    s3: Any,
    kb: Any,
    *,
    bucket_documentos: str,
    bucket_preparo: str,
    kb_id: str,
    data_source_id: str,
) -> dict:
    """Remoção reversível: lixeira primeiro, base depois.

    A ordem importa. Tirar da base antes de guardar a cópia deixaria uma janela
    em que o documento não responde e ainda não está recuperável.
    """
    chave = validar_chave_do_acervo(corpo.get("chave", ""))
    s3.copy_object(
        Bucket=bucket_preparo,
        Key=chave_removida(chave),
        CopySource={"Bucket": bucket_documentos, "Key": chave},
    )
    s3.delete_object(Bucket=bucket_documentos, Key=chave)
    kb.delete_knowledge_base_documents(
        knowledgeBaseId=kb_id,
        dataSourceId=data_source_id,
        documentIdentifiers=[
            {
                "dataSourceType": "S3",
                "s3": {"uri": f"s3://{bucket_documentos}/{chave}"},
            }
        ],
    )
    proc = procedencia(chave)
    return {
        "removido": chave,
        "recuperavel_em": chave_removida(chave),
        "aviso": proc.aviso,
    }


#: Períodos da aba de custos → (início, fim) a partir de hoje (fuso do IFS).
PERIODOS_DE_CUSTO = ("hoje", "7d", "30d", "mes", "mes_anterior")


def intervalo_do_periodo(periodo: str, hoje: date) -> tuple[date, date]:
    if periodo == "hoje":
        return hoje, hoje
    if periodo == "7d":
        return hoje - timedelta(days=6), hoje
    if periodo == "30d":
        return hoje - timedelta(days=29), hoje
    if periodo == "mes_anterior":
        fim = hoje.replace(day=1) - timedelta(days=1)
        return fim.replace(day=1), fim
    if periodo == "mes":
        return hoje.replace(day=1), hoje
    raise PedidoInvalido(f"Período inválido; use um de: {', '.join(PERIODOS_DE_CUSTO)}.")


def _custos(evento: Mapping[str, Any], loja: Any, hoje: date | None = None) -> dict:
    """Custos medidos do período, a conferência com a fatura e a cota da Meta."""
    from src.domain import custos as dom

    parametros = evento.get("queryStringParameters") or {}
    hoje = hoje or dom.hoje_local()
    periodo = str(parametros.get("periodo") or "mes")
    inicio, fim = intervalo_do_periodo(periodo, hoje)
    linhas = loja.custos(inicio, fim)
    faturas = loja.faturas(inicio, fim)
    # Projeção: média dos últimos 30 dias fechados (hoje fica de fora, incompleto).
    media = None
    if periodo == "mes":
        media = dom.media_diaria(loja.custos(hoje - timedelta(days=30), hoje - timedelta(days=1)))
    agora = datetime.now(timezone.utc)
    resumo = dom.resumir(
        linhas, inicio=inicio, fim=fim, hoje=hoje, faturas=faturas,
        entregues_meta_mes=loja.ler_contador(agora.strftime("%Y-%m"), "meta_entregues"),
        meta_isento=os.environ.get("CUSTOS_META_ISENTO", "").lower() in ("1", "true", "sim"),
        media_diaria=media,
    )
    resumo["periodo"] = periodo
    resumo["conferencia"] = dom.conferir_modelo(linhas, faturas)
    return resumo


# --- Roteamento -----------------------------------------------------------------

def handler(
    evento: Mapping[str, Any],
    contexto: Any = None,
    *,
    s3_client: Any = None,
    kb_client: Any = None,
    custos_store: Any = None,
) -> dict[str, Any]:
    """Ponto de entrada do painel de acervo."""
    rota = _rota(evento)
    if rota.startswith("OPTIONS "):
        return {"statusCode": 204, "headers": _cors(), "body": ""}

    claims = claims_do_evento(evento)
    sub = str(claims.get("sub") or "")

    try:
        # Toda rota do painel altera ou expõe o acervo institucional: a
        # conferência de grupo vem antes de qualquer leitura de parâmetro.
        exige_admin(grupos_das_claims(claims))
        corpo = _corpo(evento)

        # Antes das variáveis do acervo: a aba de custos não depende delas.
        if rota == "GET /acervo/custos":
            loja = custos_store if custos_store is not None else _custos_store()
            return _resposta(200, _custos(evento, loja))

        bucket_documentos = os.environ["KB_BUCKET"]
        bucket_preparo = os.environ["STAGING_BUCKET"]
        kb_id = os.environ["KNOWLEDGE_BASE_ID"]
        data_source_id = os.environ["DATA_SOURCE_ID"]

        s3 = s3_client if s3_client is not None else _s3()
        kb = kb_client if kb_client is not None else _kb()

        if rota == "POST /acervo/uploads":
            return _resposta(200, _pedir_upload(corpo, sub, s3, bucket_preparo))
        if rota == "GET /acervo/preparo":
            return _resposta(200, _listar_preparo(sub, s3, bucket_preparo))
        if rota == "DELETE /acervo/preparo":
            return _resposta(200, _descartar_preparo(corpo, sub, s3, bucket_preparo))
        if rota == "POST /acervo/indexar":
            return _resposta(
                200, _indexar(corpo, sub, s3, bucket_preparo, bucket_documentos)
            )
        if rota == "GET /acervo/documentos":
            return _resposta(200, _listar_documentos(s3, bucket_documentos))
        if rota == "GET /acervo/conferencia":
            return _resposta(
                200, _conferir(s3, kb, bucket_documentos, kb_id, data_source_id)
            )
        if rota == "DELETE /acervo/documentos":
            return _resposta(
                200,
                _remover_documento(
                    corpo, s3, kb,
                    bucket_documentos=bucket_documentos,
                    bucket_preparo=bucket_preparo,
                    kb_id=kb_id,
                    data_source_id=data_source_id,
                ),
            )
        return _resposta(404, {"erro": f"Rota desconhecida: {rota}"})

    except NaoAutorizado as erro:
        return _resposta(403, {"erro": str(erro)})
    except PedidoInvalido as erro:
        return _resposta(400, {"erro": str(erro)})
    except KeyError as erro:
        logger.exception("Configuração ausente no painel de acervo")
        return _resposta(500, {"erro": f"Configuração ausente: {erro}"})
    except Exception:
        # Mensagem sanitizada: o detalhe fica no log, não na tela (Req 10.4).
        logger.exception("Falha no painel de acervo (rota %s)", rota)
        return _resposta(500, {"erro": "Falha ao processar a solicitação."})


def _s3() -> Any:
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION"))


def _custos_store() -> Any:
    from src.adapters.custos_store import CustosStore

    return CustosStore(os.environ["CUSTOS_TABLE"], region_name=os.environ.get("AWS_REGION"))


def _kb() -> Any:
    import boto3

    return boto3.client("bedrock-agent", region_name=os.environ.get("AWS_REGION"))
