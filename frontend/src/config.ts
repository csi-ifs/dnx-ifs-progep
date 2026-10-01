/**
 * Configuração vinda do deploy, **fora do bundle**.
 *
 * `config.json` é publicado ao lado do build (ver `scripts/publicar_painel.py`).
 * Ficar fora do bundle permite apontar a mesma build para outra conta sem
 * recompilar — e evita que um identificador de pool errado fique congelado num
 * artefato já publicado.
 */

export type Config = {
  regiao: string;
  poolId: string;
  clienteId: string;
  /** Base da API_de_Integracao, incluindo o stage (`.../prod`). */
  api: string;
};

let cache: Config | null = null;

export async function carregarConfig(): Promise<Config> {
  if (cache) return cache;
  const resposta = await fetch("/config.json", { cache: "no-store" });
  if (!resposta.ok) {
    throw new Error(
      "config.json não encontrado — a aplicação não sabe a qual ambiente se conectar.",
    );
  }
  cache = (await resposta.json()) as Config;
  return cache;
}
