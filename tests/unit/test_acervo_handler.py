"""Testes do handler do painel de acervo, com clientes AWS injetados.

O que estes testes protegem, em ordem de gravidade: (1) ninguém fora do grupo
altera o acervo; (2) um administrador não mexe no preparo de outro; (3) a
remoção é reversível e nessa ordem; (4) o envio não entra na base sem
confirmação.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.acervo_handler import grupos_das_claims, handler
from src.domain.acervo import GRUPO_ADMIN
from src.domain.config import PREPARO_PREFIX, REMOVIDOS_PREFIX

pytestmark = pytest.mark.unit

SUB = "sub-alice"
AMBIENTE = {
    "KB_BUCKET": "progep-ifs-documents",
    "STAGING_BUCKET": "progep-ifs-staging",
    "KNOWLEDGE_BASE_ID": "KB123",
    "DATA_SOURCE_ID": "DS123",
}


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    for chave, valor in AMBIENTE.items():
        monkeypatch.setenv(chave, valor)


def _evento(metodo, caminho, corpo=None, grupos=(GRUPO_ADMIN,), sub=SUB):
    return {
        "httpMethod": metodo,
        "path": caminho,
        "body": json.dumps(corpo) if corpo is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {"sub": sub, "cognito:groups": ", ".join(grupos)}
            }
        },
    }


def _corpo(resposta):
    return json.loads(resposta["body"])


#: O S3 é consultado para dois fins com prefixos diferentes (o preparo do
#: usuário e o acervo inteiro); o fake responde conforme o `Prefix` pedido.
_PREPARO = [
    {
        "Contents": [
            {
                "Key": f"{PREPARO_PREFIX}{SUB}/CS 348.pdf",
                "Size": 4096,
                "LastModified": datetime(2026, 8, 31, tzinfo=timezone.utc),
            }
        ]
    }
]
_ACERVO = [
    {
        "Contents": [
            {
                "Key": "site/x.html",
                "Size": 100,
                "LastModified": datetime(2026, 1, 5, tzinfo=timezone.utc),
            },
            {
                "Key": "cliente/DAP/y.pdf",
                "Size": 200,
                "LastModified": datetime(2026, 8, 1, tzinfo=timezone.utc),
            },
            # Está no bucket e **não** na base: é o caso que a listagem pela
            # Knowledge Base escondia.
            {
                "Key": "cliente/DAP/nao_indexou.pdf",
                "Size": 300,
                "LastModified": datetime(2026, 9, 1, tzinfo=timezone.utc),
            },
            {"Key": "cliente/", "Size": 0, "LastModified": None},  # marcador
        ]
    }
]


@pytest.fixture
def s3():
    cliente = MagicMock()
    cliente.generate_presigned_post.return_value = {
        "url": "https://s3/preparo",
        "fields": {"key": "x"},
    }
    cliente.get_paginator.return_value.paginate.side_effect = (
        lambda **kw: _PREPARO if kw.get("Prefix") else _ACERVO
    )
    return cliente


@pytest.fixture
def kb():
    cliente = MagicMock()
    cliente.get_paginator.return_value.paginate.return_value = [
        {
            "documentDetails": [
                {
                    "identifier": {"s3": {"uri": "s3://progep-ifs-documents/site/x.html"}},
                    "status": "INDEXED",
                    "updatedAt": datetime(2026, 8, 31, tzinfo=timezone.utc),
                },
                {
                    "identifier": {
                        "s3": {"uri": "s3://progep-ifs-documents/cliente/DAP/y.pdf"}
                    },
                    "status": "INDEXED",
                    "updatedAt": None,
                },
            ]
        }
    ]
    return cliente


# --- Autorização ---------------------------------------------------------------

@pytest.mark.parametrize(
    "rota",
    [
        ("POST", "/acervo/uploads"),
        ("GET", "/acervo/preparo"),
        ("DELETE", "/acervo/preparo"),
        ("POST", "/acervo/indexar"),
        ("GET", "/acervo/documentos"),
        ("DELETE", "/acervo/documentos"),
    ],
)
def test_toda_rota_exige_o_grupo(rota, s3, kb):
    """Nenhuma rota do painel pode ser alcançada por usuário comum autenticado."""
    metodo, caminho = rota
    resposta = handler(
        _evento(metodo, caminho, {}, grupos=("servidores",)), s3_client=s3, kb_client=kb
    )
    assert resposta["statusCode"] == 403
    s3.copy_object.assert_not_called()
    s3.delete_object.assert_not_called()
    kb.delete_knowledge_base_documents.assert_not_called()


@pytest.mark.parametrize(
    "bruto,esperado",
    [
        ("administradores", ["administradores"]),
        ("[administradores]", ["administradores"]),
        ("[administradores, servidores]", ["administradores", "servidores"]),
        (["administradores"], ["administradores"]),
        ("", []),
        (None, []),
    ],
)
def test_grupos_das_claims(bruto, esperado):
    """A claim chega achatada em string, e com colchetes quando há mais de um.

    Tratar só o formato de lista faria a autorização falhar exatamente para
    quem pertence a mais de um grupo.
    """
    assert grupos_das_claims({"cognito:groups": bruto}) == esperado


# --- Envio e preparo -----------------------------------------------------------

def test_upload_vai_para_a_pasta_do_usuario(s3, kb):
    resposta = handler(
        _evento("POST", "/acervo/uploads", {"nome": "CS 348.pdf", "tamanho": 4096}),
        s3_client=s3, kb_client=kb,
    )
    assert resposta["statusCode"] == 200
    assert _corpo(resposta)["chave"] == f"{PREPARO_PREFIX}{SUB}/CS 348.pdf"
    argumentos = s3.generate_presigned_post.call_args.kwargs
    assert argumentos["Bucket"] == "progep-ifs-staging"
    # content-length-range é o que impede enviar mais do que foi declarado.
    assert any("content-length-range" in str(c) for c in argumentos["Conditions"])


def test_upload_nao_toca_o_bucket_de_documentos(s3, kb):
    """O envio não pode alcançar a base: é o que sustenta a revisão."""
    handler(
        _evento("POST", "/acervo/uploads", {"nome": "x.pdf", "tamanho": 10}),
        s3_client=s3, kb_client=kb,
    )
    assert s3.generate_presigned_post.call_args.kwargs["Bucket"] != "progep-ifs-documents"


def test_lista_o_proprio_preparo(s3, kb):
    resposta = handler(_evento("GET", "/acervo/preparo"), s3_client=s3, kb_client=kb)
    corpo = _corpo(resposta)
    assert corpo["total"] == 1
    assert corpo["itens"][0]["nome"] == "CS 348.pdf"
    prefixo = s3.get_paginator.return_value.paginate.call_args.kwargs["Prefix"]
    assert prefixo == f"{PREPARO_PREFIX}{SUB}/"


@pytest.mark.parametrize("rota", ["/acervo/preparo", "/acervo/indexar"])
def test_nao_mexe_no_preparo_de_outro_administrador(rota, s3, kb):
    """Chave de outro usuário é recusada mesmo vindo de um administrador.

    Sem isso, alguém confirmaria o envio de outro — arquivo que não viu nem
    revisou, entrando na base em seu nome.
    """
    metodo = "DELETE" if rota.endswith("preparo") else "POST"
    resposta = handler(
        _evento(metodo, rota, {"chave": f"{PREPARO_PREFIX}sub-bob/segredo.pdf"}),
        s3_client=s3, kb_client=kb,
    )
    assert resposta["statusCode"] == 400
    s3.copy_object.assert_not_called()
    s3.delete_object.assert_not_called()


def test_indexar_copia_para_o_acervo_e_limpa_o_preparo(s3, kb):
    chave = f"{PREPARO_PREFIX}{SUB}/CS 348.pdf"
    resposta = handler(
        _evento("POST", "/acervo/indexar", {"chave": chave, "titulo": "Resolução CS 348"}),
        s3_client=s3, kb_client=kb,
    )

    assert resposta["statusCode"] == 200
    copia = s3.copy_object.call_args.kwargs
    assert copia["Bucket"] == "progep-ifs-documents"
    assert copia["Key"] == "cliente/painel/CS 348.pdf"
    assert copia["CopySource"] == {"Bucket": "progep-ifs-staging", "Key": chave}
    # Título acentuado viaja percent-encoded: metadado de objeto é cabeçalho HTTP.
    assert copia["Metadata"]["titulo"] == "Resolu%C3%A7%C3%A3o%20CS%20348"
    s3.delete_object.assert_called_once_with(Bucket="progep-ifs-staging", Key=chave)


def test_preparo_so_e_apagado_depois_da_copia(s3, kb):
    """Se a cópia falhar, o envio continua na lista para nova tentativa."""
    s3.copy_object.side_effect = RuntimeError("falha na cópia")
    resposta = handler(
        _evento("POST", "/acervo/indexar",
                {"chave": f"{PREPARO_PREFIX}{SUB}/CS 348.pdf"}),
        s3_client=s3, kb_client=kb,
    )
    assert resposta["statusCode"] == 500
    s3.delete_object.assert_not_called()


# --- Documentos na base --------------------------------------------------------

def test_lista_documentos_com_procedencia(s3, kb):
    corpo = _corpo(handler(_evento("GET", "/acervo/documentos"), s3_client=s3, kb_client=kb))
    por_chave = {i["chave"]: i for i in corpo["itens"]}

    assert por_chave["site/x.html"]["regenerave"] is True
    assert por_chave["site/x.html"]["aviso"]
    assert por_chave["cliente/DAP/y.pdf"]["regenerave"] is False
    assert por_chave["cliente/DAP/y.pdf"]["origem"] == "repositório da PROGEP"


def test_listagem_mostra_documento_que_nao_indexou(s3, kb):
    """A razão de listar pelo S3 e não pela base.

    Um PDF que falha ao indexar some da Knowledge Base sem erro visível: o job
    conta a falha mas não a nomeia. Listando pela base, ele desapareceria da
    tela — está no acervo, o assistente age como se não estivesse, e ninguém
    tem como descobrir.
    """
    corpo = _corpo(
        handler(_evento("GET", "/acervo/documentos"), s3_client=s3, kb_client=kb)
    )
    chaves = {i["chave"] for i in corpo["itens"]}

    assert "cliente/DAP/nao_indexou.pdf" in chaves
    # A listagem não consulta a Knowledge Base — é daí que vem a rapidez.
    kb.get_paginator.assert_not_called()


def test_listagem_ignora_marcador_de_pasta(s3, kb):
    corpo = _corpo(
        handler(_evento("GET", "/acervo/documentos"), s3_client=s3, kb_client=kb)
    )
    assert all(not i["chave"].endswith("/") for i in corpo["itens"])


def test_documento_recem_atualizado_vem_primeiro(s3, kb):
    """Regressão: com 400 documentos, o recém-enviado caía na posição 389 e
    sumia da tela — que é exatamente o que o administrador foi procurar."""
    itens = _corpo(
        handler(_evento("GET", "/acervo/documentos"), s3_client=s3, kb_client=kb)
    )["itens"]

    assert [i["nome"] for i in itens] == ["nao_indexou.pdf", "y.pdf", "x.html"]


# --- Conferência ---------------------------------------------------------------

def test_conferencia_nomeia_os_ausentes(s3, kb):
    """O job de ingestão conta as falhas e não diz quais. Esta é a única forma
    de saber **quais** documentos estão no acervo sem responder."""
    corpo = _corpo(
        handler(_evento("GET", "/acervo/conferencia"), s3_client=s3, kb_client=kb)
    )

    assert corpo["ausentes"] == ["cliente/DAP/nao_indexou.pdf"]
    assert corpo["orfaos"] == []
    assert corpo["no_s3"] == 3  # o marcador de pasta não conta
    assert corpo["por_situacao"] == {"INDEXED": 2}


def test_conferencia_aponta_orfao_na_base(s3, kb):
    """Sobra de remoção que não completou: fora do S3, ainda respondendo."""
    kb.get_paginator.return_value.paginate.return_value = [
        {
            "documentDetails": [
                {
                    "identifier": {"s3": {"uri": "s3://b/cliente/sumiu-do-s3.pdf"}},
                    "status": "INDEXED",
                    "updatedAt": None,
                }
            ]
        }
    ]

    corpo = _corpo(
        handler(_evento("GET", "/acervo/conferencia"), s3_client=s3, kb_client=kb)
    )

    assert corpo["orfaos"] == ["cliente/sumiu-do-s3.pdf"]


def test_remocao_guarda_copia_antes_de_tirar_da_base(s3, kb):
    """A ordem é a garantia: tirar da base antes de copiar deixaria uma janela
    em que o documento não responde e ainda não é recuperável."""
    ordem: list[str] = []
    s3.copy_object.side_effect = lambda **kw: ordem.append("copia")
    s3.delete_object.side_effect = lambda **kw: ordem.append("apaga_s3")
    kb.delete_knowledge_base_documents.side_effect = lambda **kw: ordem.append("apaga_kb")

    resposta = handler(
        _evento("DELETE", "/acervo/documentos", {"chave": "cliente/DAP/y.pdf"}),
        s3_client=s3, kb_client=kb,
    )

    assert resposta["statusCode"] == 200
    assert ordem == ["copia", "apaga_s3", "apaga_kb"]
    assert s3.copy_object.call_args.kwargs["Key"] == (
        f"{REMOVIDOS_PREFIX}cliente/DAP/y.pdf"
    )
    assert _corpo(resposta)["recuperavel_em"].startswith(REMOVIDOS_PREFIX)


def test_remocao_usa_a_api_por_documento(s3, kb):
    """Sem job de sincronização: a URI sai da base na hora."""
    handler(
        _evento("DELETE", "/acervo/documentos", {"chave": "cliente/DAP/y.pdf"}),
        s3_client=s3, kb_client=kb,
    )
    argumentos = kb.delete_knowledge_base_documents.call_args.kwargs
    assert argumentos["documentIdentifiers"] == [
        {
            "dataSourceType": "S3",
            "s3": {"uri": "s3://progep-ifs-documents/cliente/DAP/y.pdf"},
        }
    ]


@pytest.mark.parametrize(
    "chave",
    [
        f"{PREPARO_PREFIX}sub-bob/x.pdf",   # preparo não é acervo
        "cliente/../preparo/x.pdf",
        "",
        "outro-bucket-qualquer/x.pdf",
    ],
)
def test_remocao_recusa_chave_fora_do_acervo(chave, s3, kb):
    """O papel da Lambda alcança o bucket inteiro: a validação é o controle."""
    resposta = handler(
        _evento("DELETE", "/acervo/documentos", {"chave": chave}),
        s3_client=s3, kb_client=kb,
    )
    assert resposta["statusCode"] == 400
    s3.delete_object.assert_not_called()
    kb.delete_knowledge_base_documents.assert_not_called()


# --- Roteamento ----------------------------------------------------------------

def test_preflight_nao_exige_autenticacao():
    """Preflight de CORS não carrega credencial, por especificação."""
    resposta = handler(_evento("OPTIONS", "/acervo/documentos", grupos=()))
    assert resposta["statusCode"] == 204


def test_caminho_com_stage_casa_com_a_rota(s3, kb):
    """O API Gateway entrega `/prod/acervo/...` quando o stage está no caminho."""
    resposta = handler(_evento("GET", "/prod/acervo/preparo"), s3_client=s3, kb_client=kb)
    assert resposta["statusCode"] == 200


def test_rota_desconhecida(s3, kb):
    resposta = handler(_evento("GET", "/acervo/inexistente"), s3_client=s3, kb_client=kb)
    assert resposta["statusCode"] == 404
