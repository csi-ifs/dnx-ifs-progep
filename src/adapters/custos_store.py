"""Tabela de custos do assistente (DynamoDB).

Chaves:

* ``pk = custo#AAAA-MM``, ``sk = AAAA-MM-DD#categoria#item`` — o custo de um
  item num dia (fuso do IFS). Um mês inteiro sai numa única ``Query``.
* ``pk = fatura#AAAA-MM``, ``sk = AAAA-MM-DD#item`` — o valor da fatura (Cost
  Explorer, dia UTC), para conferir o medido.
* ``pk = contador#AAAA-MM``, ``sk = <nome>`` — contadores do mês (cota da Meta).
* ``pk = marca#<id>`` — "já contei este evento", com TTL.

**Duas formas de gravar, e não se misturam no mesmo item.** O que chega evento a
evento (tokens, mensagens) **soma** (``ADD``, atômico entre execuções
simultâneas) e é protegido contra repetição por :meth:`marcar`. O que o coletor
recalcula a partir de métricas (AgentCore, Lambda, fixos) **substitui**
(``PUT``), então rodar o coletor de novo nunca conta duas vezes.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Por quanto tempo uma marca de evento já contado é guardada. A reentrega de
#: log ou de SNS acontece em minutos; três dias cobrem com folga um
#: reprocessamento manual (scripts/custos_retroativo.py) logo após o deploy.
TTL_MARCA_S = 3 * 86400


def _n(valor: float) -> str:
    # Decimal via str: float direto no DynamoDB perde precisão e dá erro com
    # valores como 1e-07.
    return format(Decimal(repr(float(valor))), "f")


class CustosStore:
    def __init__(self, table_name: str, *, client: Any = None,
                 region_name: str | None = None) -> None:
        self._table_name = table_name
        self._client = client
        self._region_name = region_name

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self._region_name)
        return self._client

    # --- escrita ----------------------------------------------------------------

    def marcar(self, marca: str, agora: float | None = None) -> bool:
        """``True`` se o evento ainda não tinha sido contado (e agora está).

        Falha de I/O devolve ``True``: contar um evento duas vezes erra o painel
        por uma fração de centavo; deixar de contar esconderia o gasto.
        """
        expira = int((agora or time.time()) + TTL_MARCA_S)
        try:
            self.client.put_item(
                TableName=self._table_name,
                Item={"pk": {"S": f"marca#{marca}"}, "sk": {"S": "-"},
                      "expira_em": {"N": str(expira)}},
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            codigo = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if codigo == "ConditionalCheckFailedException":
                return False
            logger.warning("Falha ao marcar evento de custo; contando assim mesmo", exc_info=True)
            return True

    def somar(self, dia: str, item: str, usd: float, quantidade: float = 0) -> None:
        self.client.update_item(
            TableName=self._table_name,
            Key={"pk": {"S": f"custo#{dia[:7]}"}, "sk": {"S": f"{dia}#{item}"}},
            UpdateExpression="ADD usd :u, quantidade :q SET atualizado_em = :t, origem = :o",
            ExpressionAttributeValues={
                ":u": {"N": _n(usd)},
                ":q": {"N": _n(quantidade)},
                ":t": {"S": _agora_iso()},
                ":o": {"S": "medido"},
            },
        )

    def definir(self, dia: str, item: str, usd: float, quantidade: float = 0,
                origem: str = "medido") -> None:
        self.client.put_item(
            TableName=self._table_name,
            Item={
                "pk": {"S": f"custo#{dia[:7]}"},
                "sk": {"S": f"{dia}#{item}"},
                "usd": {"N": _n(usd)},
                "quantidade": {"N": _n(quantidade)},
                "atualizado_em": {"S": _agora_iso()},
                "origem": {"S": origem},
            },
        )

    def definir_fatura(self, dia: str, item: str, usd: float) -> None:
        self.client.put_item(
            TableName=self._table_name,
            Item={
                "pk": {"S": f"fatura#{dia[:7]}"},
                "sk": {"S": f"{dia}#{item}"},
                "usd": {"N": _n(usd)},
                "atualizado_em": {"S": _agora_iso()},
            },
        )

    def contar_mes(self, mes: str, nome: str, n: int = 1) -> int:
        """Soma ``n`` ao contador do mês e devolve o valor novo."""
        r = self.client.update_item(
            TableName=self._table_name,
            Key={"pk": {"S": f"contador#{mes}"}, "sk": {"S": nome}},
            UpdateExpression="ADD valor :n",
            ExpressionAttributeValues={":n": {"N": str(n)}},
            ReturnValues="UPDATED_NEW",
        )
        return int(r["Attributes"]["valor"]["N"])

    def salvar_estado(self, nome: str, valor: str) -> None:
        self.client.put_item(
            TableName=self._table_name,
            Item={"pk": {"S": "estado"}, "sk": {"S": nome}, "valor": {"S": valor}},
        )

    # --- leitura ----------------------------------------------------------------

    def ler_contador(self, mes: str, nome: str) -> int:
        r = self.client.get_item(
            TableName=self._table_name,
            Key={"pk": {"S": f"contador#{mes}"}, "sk": {"S": nome}},
        )
        return int(((r.get("Item") or {}).get("valor") or {}).get("N") or 0)

    def ler_estado(self, nome: str) -> str:
        r = self.client.get_item(
            TableName=self._table_name, Key={"pk": {"S": "estado"}, "sk": {"S": nome}}
        )
        return str(((r.get("Item") or {}).get("valor") or {}).get("S") or "")

    def custos(self, inicio: date, fim: date) -> list[dict[str, Any]]:
        """Linhas ``{"dia", "item", "usd", "quantidade", "atualizado_em"}``."""
        return [
            {
                "dia": sk[:10],
                "item": sk[11:],
                "usd": float(item.get("usd", {}).get("N", 0)),
                "quantidade": float(item.get("quantidade", {}).get("N", 0)),
                "atualizado_em": item.get("atualizado_em", {}).get("S", ""),
            }
            for item, sk in self._consultar("custo", inicio, fim)
        ]

    def faturas(self, inicio: date, fim: date) -> list[dict[str, Any]]:
        return [
            {"dia": sk[:10], "item": sk[11:], "usd": float(item.get("usd", {}).get("N", 0))}
            for item, sk in self._consultar("fatura", inicio, fim)
        ]

    def _consultar(self, prefixo: str, inicio: date, fim: date) -> Iterable[tuple[dict, str]]:
        for mes in _meses(inicio, fim):
            parametros: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "pk = :pk AND sk BETWEEN :a AND :b",
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"{prefixo}#{mes}"},
                    ":a": {"S": inicio.isoformat()},
                    # "~" ordena depois de qualquer "#categoria#item" do dia final.
                    ":b": {"S": f"{fim.isoformat()}~"},
                },
            }
            while True:
                r = self.client.query(**parametros)
                for item in r.get("Items", []):
                    yield item, item["sk"]["S"]
                if "LastEvaluatedKey" not in r:
                    break
                parametros["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _meses(inicio: date, fim: date) -> list[str]:
    meses: list[str] = []
    a, m = inicio.year, inicio.month
    while (a, m) <= (fim.year, fim.month):
        meses.append(f"{a:04d}-{m:02d}")
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return meses


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
