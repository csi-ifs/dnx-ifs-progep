/**
 * Cliente da API do painel de acervo.
 *
 * O envio tem **duas etapas de propósito**: a API devolve uma URL pré-assinada
 * e o navegador envia o arquivo direto ao S3. O arquivo nunca passa pela
 * Lambda — que tem teto de 6 MB de payload, enquanto o acervo aceita 50 MB.
 */

export type ArquivoEmPreparo = {
  chave: string;
  nome: string;
  tamanho: number;
  enviado_em: string | null;
};

export type DocumentoNaBase = {
  chave: string;
  nome: string;
  tamanho: number;
  atualizado_em: string | null;
  origem: string;
  /** O conteúdo volta se a origem for reprocessada (crawler do site). */
  regenerave: boolean;
  aviso: string;
};

/** Resultado da comparação entre o bucket e a Knowledge Base. */
export type Conferencia = {
  no_s3: number;
  indexados: string[];
  /** No acervo e **não** na base: está lá e não responde. */
  ausentes: string[];
  /** Na base e não no acervo: sobra de remoção incompleta. */
  orfaos: string[];
  por_situacao: Record<string, number>;
};

type Envio = {
  url: string;
  campos: Record<string, string>;
  chave: string;
  nome: string;
};

async function chamar<T>(
  api: string,
  token: string,
  metodo: string,
  caminho: string,
  corpo?: unknown,
): Promise<T> {
  const resposta = await fetch(`${api}${caminho}`, {
    method: metodo,
    headers: { Authorization: token, "Content-Type": "application/json" },
    body: corpo === undefined ? undefined : JSON.stringify(corpo),
  });
  const texto = await resposta.text();
  const dados = texto ? JSON.parse(texto) : {};
  if (!resposta.ok) {
    // A mensagem do backend é escrita para quem está na tela ("Apenas
    // administradores podem..."); repassá-la é melhor que um código HTTP.
    throw new Error(dados?.erro || `Falha na requisição (HTTP ${resposta.status}).`);
  }
  return dados as T;
}

export function listarPreparo(
  api: string,
  token: string,
): Promise<{ itens: ArquivoEmPreparo[]; total: number }> {
  return chamar(api, token, "GET", "/acervo/preparo");
}

export function listarDocumentos(
  api: string,
  token: string,
): Promise<{ itens: DocumentoNaBase[]; total: number; truncado: boolean }> {
  return chamar(api, token, "GET", "/acervo/documentos");
}

export function conferirIndexacao(
  api: string,
  token: string,
): Promise<Conferencia> {
  return chamar(api, token, "GET", "/acervo/conferencia");
}

export async function enviarParaPreparo(
  api: string,
  token: string,
  arquivo: File,
): Promise<void> {
  const envio = await chamar<Envio>(api, token, "POST", "/acervo/uploads", {
    nome: arquivo.name,
    tamanho: arquivo.size,
  });

  const formulario = new FormData();
  for (const [chave, valor] of Object.entries(envio.campos)) {
    formulario.append(chave, valor);
  }
  // O arquivo vai **por último**: o S3 ignora qualquer campo que venha depois
  // dele no multipart, então uma ordem trocada descarta a política assinada.
  formulario.append("file", arquivo);

  let resposta: Response;
  try {
    resposta = await fetch(envio.url, { method: "POST", body: formulario });
  } catch {
    // `fetch` rejeita sem status nem corpo quando o navegador barra a
    // requisição antes de enviá-la — na prática, CORS ausente no bucket. O
    // "Failed to fetch" cru não diz nada a quem está na tela, e manda procurar
    // o problema no arquivo em vez de na configuração.
    throw new Error(
      "O navegador não conseguiu falar com o armazenamento. " +
        "Se acabou de ser implantado, confira a configuração de CORS do bucket de preparo.",
    );
  }
  if (!resposta.ok) {
    throw new Error(
      resposta.status === 403
        ? "O envio foi recusado pelo S3 (arquivo maior que o declarado ou link expirado)."
        : `Falha ao enviar o arquivo (HTTP ${resposta.status}).`,
    );
  }
}

export function descartarPreparo(
  api: string,
  token: string,
  chave: string,
): Promise<{ descartado: string }> {
  return chamar(api, token, "DELETE", "/acervo/preparo", { chave });
}

export function confirmarIndexacao(
  api: string,
  token: string,
  chave: string,
  titulo: string,
): Promise<{ indexado: string; situacao: string }> {
  return chamar(api, token, "POST", "/acervo/indexar", {
    chave,
    titulo: titulo.trim() || undefined,
  });
}

export function removerDocumento(
  api: string,
  token: string,
  chave: string,
): Promise<{ removido: string; recuperavel_em: string; aviso: string }> {
  return chamar(api, token, "DELETE", "/acervo/documentos", { chave });
}

export function formatarBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatarData(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("pt-BR");
}
