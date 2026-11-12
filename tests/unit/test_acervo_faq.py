"""FAQ pelo painel e remoção adiada — com um S3 falso que guarda estado.

O que estes testes protegem: (1) um FAQ enviado pelo painel entra **dividido**
por pergunta, sem passo manual; (2) o painel trata o FAQ como um documento só
(lista, reenvio, substituição e remoção); (3) a remoção não falha quando a base
está indexando — termina em segundo plano.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters import acervo_handler
from src.adapters.acervo_handler import handler
from src.adapters.extracao import extrair_texto
from src.domain.acervo import GRUPO_ADMIN
from src.domain.config import PREPARO_PREFIX, REMOVIDOS_PREFIX
from src.domain.faq import e_faq, dividir

pytestmark = pytest.mark.unit

SUB = "sub-alice"
DOCS, PREPARO = "progep-ifs-documents", "progep-ifs-staging"


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    for chave, valor in {
        "KB_BUCKET": DOCS, "STAGING_BUCKET": PREPARO,
        "KNOWLEDGE_BASE_ID": "KB123", "DATA_SOURCE_ID": "DS123",
        "AWS_LAMBDA_FUNCTION_NAME": "progep-ifs-acervo",
    }.items():
        monkeypatch.setenv(chave, valor)


class ErroS3(Exception):
    def __init__(self, codigo):
        super().__init__(codigo)
        self.response = {"Error": {"Code": codigo}}


class S3Falso:
    """O mínimo do S3 que o handler usa, com objetos de verdade em memória."""

    def __init__(self):
        self.objetos: dict[tuple[str, str], bytes] = {}
        self.chamadas: list[str] = []

    def _etag(self, dados):
        return f'"{hashlib.md5(dados).hexdigest()}"'

    def put_object(self, Bucket, Key, Body, **_):
        self.chamadas.append(f"put:{Key}")
        self.objetos[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()

    def copy_object(self, Bucket, Key, CopySource, **_):
        self.chamadas.append(f"copia:{Key}")
        self.objetos[(Bucket, Key)] = self.objetos[(CopySource["Bucket"], CopySource["Key"])]

    def delete_object(self, Bucket, Key):
        self.chamadas.append(f"apaga:{Key}")
        self.objetos.pop((Bucket, Key), None)

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objetos:
            raise ErroS3("404")
        dados = self.objetos[(Bucket, Key)]
        return {"ETag": self._etag(dados), "ContentLength": len(dados)}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objetos[(Bucket, Key)])}

    def get_paginator(self, _nome):
        falso = self

        class Paginador:
            def paginate(self, Bucket, Prefix=""):
                conteudo = [
                    {"Key": k, "ETag": falso._etag(v), "Size": len(v),
                     "LastModified": datetime(2026, 11, 12, tzinfo=timezone.utc)}
                    for (b, k), v in sorted(falso.objetos.items())
                    if b == Bucket and k.startswith(Prefix)
                ]
                return [{"Contents": conteudo}]

        return Paginador()


FAQ = b"""FAQ do Teste
1. Quem avalia o servidor?
Para nomeados a partir de 07/02/2025: chefia, autoavaliacao e pares.
Para nomeados ate 06/02/2025: chefia e dois pares.
2. Quantas avaliacoes existem?
Tres ciclos avaliativos.
3. Onde tirar duvidas?
Na PROGEP, pelo e-mail institucional.
"""


def _evento(metodo, caminho, corpo=None):
    return {
        "httpMethod": metodo, "path": caminho,
        "body": json.dumps(corpo) if corpo is not None else None,
        "requestContext": {"authorizer": {"claims": {
            "sub": SUB, "cognito:groups": GRUPO_ADMIN}}},
    }


def _corpo(resposta):
    return json.loads(resposta["body"])


@pytest.fixture
def s3():
    return S3Falso()


@pytest.fixture
def kb():
    return MagicMock()


def _enviar(s3, nome, dados=FAQ):
    s3.objetos[(PREPARO, f"{PREPARO_PREFIX}{SUB}/{nome}")] = dados


def _indexar(s3, kb, nome, **extra):
    return handler(
        _evento("POST", "/acervo/indexar", {"chave": f"{PREPARO_PREFIX}{SUB}/{nome}", **extra}),
        s3_client=s3, kb_client=kb,
    )


# --- FAQ no envio ---------------------------------------------------------------

def test_faq_enviado_entra_dividido_por_pergunta(s3, kb):
    _enviar(s3, "FAQ Teste.txt")
    resposta = _indexar(s3, kb, "FAQ Teste.txt")

    assert resposta["statusCode"] == 200, resposta["body"]
    corpo = _corpo(resposta)
    assert corpo["indexado"] == "cliente/faq/FAQ Teste/"
    assert corpo["faq"] == {"perguntas": 3}
    no_acervo = sorted(k for (b, k) in s3.objetos if b == DOCS)
    assert no_acervo == [
        "cliente/faq/FAQ Teste/01-quem-avalia-o-servidor.md",
        "cliente/faq/FAQ Teste/02-quantas-avaliacoes-existem.md",
        "cliente/faq/FAQ Teste/03-onde-tirar-duvidas.md",
    ]
    # A resposta com as duas datas fica num arquivo só.
    pergunta1 = s3.objetos[(DOCS, no_acervo[0])].decode()
    assert "07/02/2025" in pergunta1 and "06/02/2025" in pergunta1
    # O original fica guardado fora da base, e o preparo é limpo.
    assert (PREPARO, "faq-originais/cliente/faq/FAQ Teste/FAQ Teste.txt") in s3.objetos
    assert (PREPARO, f"{PREPARO_PREFIX}{SUB}/FAQ Teste.txt") not in s3.objetos


def test_documento_comum_entra_inteiro(s3, kb):
    _enviar(s3, "Resolucao 348.txt", b"Art. 1o Fica aprovado o regulamento.\nArt. 2o Revoga-se.")
    corpo = _corpo(_indexar(s3, kb, "Resolucao 348.txt"))
    assert corpo["indexado"] == "cliente/painel/Resolucao 348.txt"
    assert corpo["faq"] is None


def test_reenvio_do_mesmo_faq_e_copia_identica(s3, kb):
    _enviar(s3, "FAQ Teste.txt")
    _indexar(s3, kb, "FAQ Teste.txt")
    _enviar(s3, "FAQ Teste (1).txt")

    resposta = _indexar(s3, kb, "FAQ Teste (1).txt", acao="manter_ambos")

    assert resposta["statusCode"] == 409
    assert _corpo(resposta)["identicos"] == ["cliente/faq/FAQ Teste/"]


def test_versao_nova_do_faq_substitui_todas_as_perguntas(s3, kb):
    _enviar(s3, "FAQ Teste.txt", FAQ + b"4. Pergunta que vai sumir?\nSim.\n")
    _indexar(s3, kb, "FAQ Teste.txt")
    _enviar(s3, "FAQ Teste.txt", FAQ.replace(b"Tres ciclos", b"Quatro ciclos"))

    pergunta = _indexar(s3, kb, "FAQ Teste.txt")
    assert pergunta["statusCode"] == 409 and _corpo(pergunta)["pode_prosseguir"]
    resposta = _indexar(s3, kb, "FAQ Teste.txt", acao="substituir")

    assert resposta["statusCode"] == 200, resposta["body"]
    no_acervo = sorted(k for (b, k) in s3.objetos if b == DOCS)
    # A pergunta que sumiu do FAQ sai da base; as outras são reescritas.
    assert no_acervo == [
        "cliente/faq/FAQ Teste/01-quem-avalia-o-servidor.md",
        "cliente/faq/FAQ Teste/02-quantas-avaliacoes-existem.md",
        "cliente/faq/FAQ Teste/03-onde-tirar-duvidas.md",
    ]
    assert b"Quatro ciclos" in s3.objetos[(DOCS, no_acervo[1])]
    removidos = kb.delete_knowledge_base_documents.call_args.kwargs["documentIdentifiers"]
    assert [d["s3"]["uri"].rsplit("/", 1)[-1] for d in removidos] == ["04-pergunta-que-vai-sumir.md"]
    # As versões anteriores (perguntas e original) estão na lixeira.
    assert (PREPARO, f"{REMOVIDOS_PREFIX}cliente/faq/FAQ Teste/02-quantas-avaliacoes-existem.md") in s3.objetos
    assert (PREPARO, f"{REMOVIDOS_PREFIX}faq-originais/cliente/faq/FAQ Teste/FAQ Teste.txt") in s3.objetos
    assert s3.objetos[(PREPARO, "faq-originais/cliente/faq/FAQ Teste/FAQ Teste.txt")].count(b"Quatro") == 1


def test_manter_os_dois_com_o_mesmo_nome_de_faq_e_recusado(s3, kb):
    _enviar(s3, "FAQ Teste.txt")
    _indexar(s3, kb, "FAQ Teste.txt")
    _enviar(s3, "FAQ Teste.txt", FAQ + b"4. Mais uma?\nSim.\n")

    resposta = _indexar(s3, kb, "FAQ Teste.txt", acao="manter_ambos")

    assert resposta["statusCode"] == 400
    assert "renomeie" in _corpo(resposta)["erro"]


# --- Lista e remoção do FAQ como um documento -----------------------------------

def test_lista_mostra_o_faq_numa_linha(s3, kb):
    _enviar(s3, "FAQ Teste.txt")
    _indexar(s3, kb, "FAQ Teste.txt")
    s3.objetos[(DOCS, "cliente/DAP/y.pdf")] = b"%PDF"

    itens = _corpo(handler(_evento("GET", "/acervo/documentos"), s3_client=s3, kb_client=kb))["itens"]

    faq = next(i for i in itens if i["chave"] == "cliente/faq/FAQ Teste/")
    assert faq["nome"] == "FAQ Teste" and faq["faq"]["perguntas"] == 3
    assert len(itens) == 2


def test_remover_o_faq_tira_todas_as_perguntas_e_o_original(s3, kb):
    _enviar(s3, "FAQ Teste.txt")
    _indexar(s3, kb, "FAQ Teste.txt")

    resposta = handler(
        _evento("DELETE", "/acervo/documentos", {"chave": "cliente/faq/FAQ Teste/"}),
        s3_client=s3, kb_client=kb,
    )

    assert resposta["statusCode"] == 200, resposta["body"]
    assert not [k for (b, k) in s3.objetos if b == DOCS]
    assert not [k for (b, k) in s3.objetos if k.startswith("faq-originais/")]
    uris = kb.delete_knowledge_base_documents.call_args.kwargs["documentIdentifiers"]
    assert len(uris) == 3


@pytest.mark.parametrize("chave", ["cliente/faq/../DAP/", "cliente/DAP/", "preparo/faq/x/"])
def test_remocao_de_pasta_so_vale_para_faq(s3, kb, chave):
    resposta = handler(_evento("DELETE", "/acervo/documentos", {"chave": chave}),
                       s3_client=s3, kb_client=kb)
    assert resposta["statusCode"] == 400


def test_remocao_em_lotes_de_dez(s3, kb):
    for n in range(23):
        s3.objetos[(DOCS, f"cliente/faq/Grande/{n:02d}-p.md")] = b"x"
    handler(_evento("DELETE", "/acervo/documentos", {"chave": "cliente/faq/Grande/"}),
            s3_client=s3, kb_client=kb)
    tamanhos = [len(c.kwargs["documentIdentifiers"])
                for c in kb.delete_knowledge_base_documents.call_args_list]
    assert tamanhos == [10, 10, 3]


# --- Remoção durante indexação --------------------------------------------------

class IndexacaoRodando(Exception):
    def __str__(self):
        return ("An error occurred (ValidationException): There is at least one "
                "running ingestion job for this data source")


def test_remocao_durante_indexacao_termina_em_segundo_plano(s3, kb, monkeypatch):
    s3.objetos[(DOCS, "cliente/DAP/y.pdf")] = b"%PDF"
    kb.delete_knowledge_base_documents.side_effect = IndexacaoRodando()
    lambda_falso = MagicMock()
    monkeypatch.setattr(acervo_handler, "_lambda", lambda: lambda_falso)

    resposta = handler(_evento("DELETE", "/acervo/documentos", {"chave": "cliente/DAP/y.pdf"}),
                       s3_client=s3, kb_client=kb)

    assert resposta["statusCode"] == 200
    assert _corpo(resposta)["remocao_pendente"] is True
    # Já está na lixeira e fora do bucket; só a retirada do índice ficou para depois.
    assert (DOCS, "cliente/DAP/y.pdf") not in s3.objetos
    chamada = lambda_falso.invoke.call_args.kwargs
    assert chamada["InvocationType"] == "Event"
    assert json.loads(chamada["Payload"]) == {"interno": "remover_da_base",
                                              "chaves": ["cliente/DAP/y.pdf"]}


def test_tarefa_em_segundo_plano_espera_a_indexacao(s3, kb):
    kb.delete_knowledge_base_documents.side_effect = [IndexacaoRodando(), IndexacaoRodando(), None]
    esperas = []

    resultado = acervo_handler._tarefa_interna(
        {"interno": "remover_da_base", "chaves": ["cliente/DAP/y.pdf"]}, None, s3, kb,
        dormir=esperas.append,
    )

    assert resultado == {"removidos": ["cliente/DAP/y.pdf"]}
    assert len(esperas) == 2


def test_tarefa_falha_de_proposito_perto_do_fim_do_tempo(s3, kb):
    """A Lambda reenvia o evento: a espera continua na próxima tentativa."""
    kb.delete_knowledge_base_documents.side_effect = IndexacaoRodando()
    contexto = MagicMock()
    contexto.get_remaining_time_in_millis.return_value = 10_000
    with pytest.raises(RuntimeError):
        acervo_handler._tarefa_interna(
            {"chaves": ["cliente/DAP/y.pdf"]}, contexto, s3, kb, dormir=lambda _s: None)


def test_tarefa_nao_tira_da_base_o_que_voltou_ao_bucket(s3, kb):
    s3.objetos[(DOCS, "cliente/DAP/y.pdf")] = b"reenviado"
    acervo_handler._tarefa_interna({"chaves": ["cliente/DAP/y.pdf"]}, None, s3, kb)
    kb.delete_knowledge_base_documents.assert_not_called()


def test_evento_http_nunca_e_tratado_como_tarefa_interna(s3, kb):
    """O caminho interno pula a autorização: não pode ser alcançado pela API."""
    evento = _evento("DELETE", "/acervo/documentos", {"chave": "cliente/DAP/y.pdf"})
    evento["interno"] = "remover_da_base"
    evento["chaves"] = ["cliente/DAP/y.pdf"]
    evento["requestContext"]["authorizer"]["claims"]["cognito:groups"] = "servidores"
    assert handler(evento, s3_client=s3, kb_client=kb)["statusCode"] == 403


# --- Leitura do texto e reconhecimento --------------------------------------------

def _docx(paragrafos):
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragrafos)
        + "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as pacote:
        pacote.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_texto_de_docx_sem_bibliotecas_externas():
    texto = extrair_texto("FAQ.docx", _docx(["1. Pergunta um?", "Resposta um."]))
    assert texto == "1. Pergunta um?\nResposta um."


def test_arquivo_ilegivel_nao_impede_o_envio():
    assert extrair_texto("x.pdf", b"isto nao e um pdf") is None
    assert extrair_texto("x.docx", b"nem um docx") is None


def test_norma_com_itens_numerados_nao_vira_faq():
    norma = "1. Compete à PROGEP coordenar.\n2. Compete à DAP executar as rotinas.\n" * 3
    assert not e_faq("Resolucao 348.pdf", dividir(norma)[1])


def test_sem_faq_no_nome_precisa_de_perguntas_com_interrogacao():
    texto = "\n".join(
        f"{n}. Pergunta numero {n} sobre o tema?\n"
        f"Resposta {n}: o servidor abre processo no SEI e anexa o formulario."
        for n in range(1, 7)
    )
    assert e_faq("Orientacoes gerais.pdf", dividir(texto)[1])


def test_checklist_numerado_nao_vira_faq():
    """Anexo da Resolução CS 348: perguntas de sim/não, sem resposta escrita."""
    checklist = "\n".join(
        f"{n}. Item {n} do requerimento está preenchido e assinado?\n( ) Sim ( ) Não"
        for n in range(1, 13)
    )
    assert not e_faq("CS_348_Aprova_regulamento.pdf", dividir(checklist)[1])


def test_faq_que_nao_se_divide_avisa(s3, kb):
    _enviar(s3, "FAQ Sem Numeracao.txt", b"Texto corrido de orientacoes, sem perguntas.")
    corpo = _corpo(_indexar(s3, kb, "FAQ Sem Numeracao.txt"))
    assert corpo["faq"] is None
    assert "DNX" in corpo["aviso"]
