"""Contatos dos departamentos da PROGEP, para quando o assistente não responde.

Pedido do cliente em 28/09/2026: o atendimento humano pelo Connect fica
disponível, mas os departamentos têm muita demanda para atender todo "não
encontrei". Quando não há resposta nos documentos, o assistente indica **o
contato do departamento que costuma tratar o assunto**; quando o assunto nem é
de gestão de pessoas, diz isso e oferece a lista completa.

**De onde vêm os assuntos de cada departamento:** do campo "Setor responsável"
dos fluxogramas de processos da PROGEP (``docs/fluxos``), não de suposição —
DAP tem 33 processos, DNAGS 31, DSDPES 25 e o Programa de Gestão 4. A CPSPP não
é responsável por nenhum fluxo mapeado, e a descrição dela vem do nome.

**Saúde vai para dois setores.** Perícia médica e licença para tratamento de
saúde aparecem sob mais de um setor nos fluxos, e o cliente pediu citar SIASS e
DAP juntos.

**Na dúvida, o contato geral.** Indicar o setor errado faz o servidor ligar
para o lugar errado; o contato geral da PROGEP encaminha internamente.

Os contatos ficam aqui, separados da lógica, para que trocar um ramal ou e-mail
não exija mexer em mais nada.

Lógica pura.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Departamento:
    codigo: str
    nome: str
    email: str
    telefone: str
    #: Assuntos que o departamento trata — usados para classificar a pergunta.
    assuntos: str


GERAL = "PROGEP"
#: Pergunta que não é de gestão de pessoas (cardápio, matrícula de aluno…).
FORA = "FORA"
#: Código da classificação para saúde do servidor; vira SIASS + DAP.
SAUDE = "SAUDE"

DEPARTAMENTOS: dict[str, Departamento] = {
    d.codigo: d
    for d in (
        Departamento(
            GERAL,
            "Pró-Reitoria de Gestão de Pessoas (PROGEP)",
            "progep@ifs.edu.br",
            "(79) 3711-1443",
            "assuntos gerais de gestão de pessoas que não se encaixam nos demais; "
            "programa de acolhimento aos servidores ingressantes (PASIFS)",
        ),
        Departamento(
            "CPSPP",
            "Coordenadoria de Planejamento e Sistematização de Programas e "
            "Projetos em Gestão de Pessoas (CPSPP)",
            "cpspp.progep@ifs.edu.br",
            "(79) 3711-1443, ramal 1435",
            "planejamento e sistematização de programas e projetos de gestão de pessoas",
        ),
        Departamento(
            "NPG",
            "Núcleo do Programa de Gestão (NPG)",
            "programadegestao@ifs.edu.br",
            "(79) 3711-1443, ramal 1435",
            "Programa de Gestão e Desempenho (PGD), teletrabalho, inscrição no "
            "programa, cadastro e execução do plano de trabalho",
        ),
        Departamento(
            "DAP",
            "Diretoria de Administração de Pessoal (DAP)",
            "dap.progep@ifs.edu.br",
            "(79) 3711-1443, ramal 1401 ou 1445",
            "aposentadorias, abono de permanência, pensão civil, folha de "
            "pagamento, auxílio transporte, auxílio saúde, auxílio natalidade e "
            "pré-escolar, auxílio funeral, plano de saúde GEAP, admissão, cadastro "
            "e alteração de dados pessoais e bancários, emissão de declarações, "
            "certidão e contagem de tempo de serviço, desaverbação, licença "
            "casamento, falecimento, maternidade, paternidade e prêmio, "
            "substituição remunerada, GECC, isenção de imposto de renda",
        ),
        Departamento(
            "DSDPES",
            "Departamento de Seleção e Desenvolvimento de Pessoas (DSDPES)",
            "dsdpes.progep@ifs.edu.br",
            "(79) 3711-1443, ramal 1447 ou 1425",
            "concursos e processos seletivos, professor substituto e voluntário, "
            "contratos temporários, capacitação, licença para capacitação, "
            "progressão por capacitação e por mérito, incentivo à qualificação, "
            "retribuição por titulação, reconhecimento de título, aceleração da "
            "promoção, estágio probatório, afastamento para pós-graduação, "
            "horário especial para servidor estudante, redistribuição docente",
        ),
        Departamento(
            "DNAGS",
            "Departamento de Normas e Apoio à Gestão e Saúde (DNAGS)",
            "dnags.progep@ifs.edu.br",
            "(79) 3711-1443",
            "legislação e normas de pessoal, cessão, requisição, colaboração "
            "técnica, exercício provisório, remoção, recondução, licenças sem "
            "remuneração (interesses particulares, acompanhamento de cônjuge, "
            "atividade política, mandato classista, serviço militar), afastamento "
            "para a justiça eleitoral, alteração de regime de trabalho docente, "
            "horário especial para pessoa com deficiência, redução de jornada, "
            "adicionais ocupacionais (insalubridade, periculosidade), decisões "
            "judiciais, acúmulo de cargos, reposição ao erário",
        ),
        Departamento(
            "SIASS",
            "Subsistema Integrado de Atenção à Saúde do Servidor (SIASS)",
            "siass@ifs.edu.br",
            "(79) 3711-1457",
            "perícia médica, licença para tratamento de saúde, junta médica, "
            "saúde e segurança do servidor",
        ),
    )
}

#: Classificação de saúde → os dois setores, SIASS primeiro.
_SETORES_SAUDE = ("SIASS", "DAP")

_CODIGOS_VALIDOS = frozenset(DEPARTAMENTOS) | {FORA, SAUDE}


def prompt_classificacao() -> str:
    """Instrução do classificador: o catálogo de assuntos e o formato da saída."""
    linhas = [
        f"- {d.codigo}: {d.assuntos}"
        for d in DEPARTAMENTOS.values()
        if d.codigo != "SIASS"
    ]
    return (
        "Você classifica perguntas de servidores do Instituto Federal de Sergipe "
        "(IFS) para indicar qual setor da Pró-Reitoria de Gestão de Pessoas "
        "(PROGEP) procurar. A pergunta não foi respondida pelos documentos; "
        "você só escolhe o setor.\n\n"
        "Setores e assuntos:\n"
        + "\n".join(linhas)
        + f"\n- {SAUDE}: perícia médica, licença para tratamento de saúde, junta "
        "médica, saúde e segurança do servidor\n"
        f"- {FORA}: não é assunto de gestão de pessoas do IFS (por exemplo "
        "restaurante, matrícula de aluno, biblioteca, assuntos pessoais)\n\n"
        f"Se a pergunta for de gestão de pessoas mas o setor não estiver claro, "
        f"responda {GERAL}. Responda apenas com o código, sem mais nenhum texto."
    )


def interpretar_classificacao(texto: str) -> tuple[str, ...]:
    """Converte a saída do classificador nos setores a indicar.

    Aceita ruído em volta do código (pontuação, frase curta). Qualquer coisa
    irreconhecível vira o contato geral — errar o setor é pior que ser genérico.
    """
    for palavra in re.findall(r"[A-Za-z]+", (texto or "").upper()):
        if palavra in _CODIGOS_VALIDOS:
            if palavra == SAUDE:
                return _SETORES_SAUDE
            return (palavra,)
    return (GERAL,)


def formatar_contato(codigos: tuple[str, ...] | list[str]) -> str:
    """Bloco de contato dos setores indicados; o geral quando não há nenhum."""
    departamentos = [DEPARTAMENTOS[c] for c in codigos if c in DEPARTAMENTOS]
    if not departamentos:
        departamentos = [DEPARTAMENTOS[GERAL]]
    if len(departamentos) == 1:
        cabecalho = "Esse assunto costuma ser tratado por este setor:"
    else:
        cabecalho = "Esse assunto costuma ser tratado por estes setores:"
    blocos = [_contato(d) for d in departamentos]
    return cabecalho + "\n\n" + "\n\n".join(blocos)


def lista_completa() -> str:
    """Todos os contatos da PROGEP, para quem pediu a lista."""
    blocos = [_contato(d) for d in DEPARTAMENTOS.values()]
    return "Contatos da PROGEP:\n\n" + "\n\n".join(blocos)


def _contato(d: Departamento) -> str:
    return f"*{d.nome}*\n📧 {d.email}\n📞 {d.telefone}"
