/**
 * Aba de acervo: envio de documentos e confirmação da indexação.
 *
 * O arquivo enviado fica numa **área de preparo** até a confirmação. Isso não é
 * um aviso na tela: o preparo é um bucket separado, que nenhum data source
 * varre. Enquanto o arquivo está ali ele existe no S3 e **não existe para o
 * assistente** — descartar é apagar algo que nunca entrou na base, o que
 * resolve o arquivo errado sem precisar de remoção depois.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type ArquivoEmPreparo,
  confirmarIndexacao,
  descartarPreparo,
  enviarParaPreparo,
  formatarBytes,
  listarPreparo,
} from "../api";

export function Acervo({
  api,
  obterToken,
}: {
  api: string;
  obterToken: () => Promise<string>;
}) {
  const [itens, setItens] = useState<ArquivoEmPreparo[]>([]);
  const [ocupado, setOcupado] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  // Título por arquivo, preenchido **aqui** e não no envio: é neste momento que
  // o administrador confere o que subiu. Perguntar antes obrigaria a nomear às
  // cegas; depois seria tarde, o objeto já teria sido copiado com o metadado.
  const [titulos, setTitulos] = useState<Record<string, string>>({});
  const entrada = useRef<HTMLInputElement>(null);

  const recarregar = useCallback(async () => {
    try {
      const token = await obterToken();
      const r = await listarPreparo(api, token);
      setItens(r.itens);
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Falha ao carregar o preparo.");
    }
  }, [api, obterToken]);

  useEffect(() => {
    void recarregar();
  }, [recarregar]);

  async function comOcupado(acao: () => Promise<void>, sucesso?: string) {
    setErro(null);
    setAviso(null);
    setOcupado(true);
    try {
      await acao();
      if (sucesso) setAviso(sucesso);
      await recarregar();
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Não foi possível concluir.");
    } finally {
      setOcupado(false);
    }
  }

  async function aoEscolherArquivos(lista: FileList | null) {
    if (!lista?.length) return;
    const arquivos = Array.from(lista);
    await comOcupado(async () => {
      const token = await obterToken();
      // Em série, e não em paralelo: cada envio gera uma URL assinada própria e
      // uma falha no meio precisa dizer **qual** arquivo falhou.
      for (const arquivo of arquivos) {
        await enviarParaPreparo(api, token, arquivo);
      }
    }, `${arquivos.length} arquivo(s) enviado(s). Confira e confirme abaixo.`);
    if (entrada.current) entrada.current.value = "";
  }

  return (
    <section className="painel">
      <div className="cartao">
        <h2>Enviar documentos</h2>
        <p className="ajuda">
          O arquivo fica em preparo e <strong>não entra na base</strong> até você
          confirmar. Formatos aceitos: PDF, DOCX, TXT, HTML e Markdown, até 50 MB.
        </p>
        <input
          ref={entrada}
          type="file"
          multiple
          disabled={ocupado}
          onChange={(e) => void aoEscolherArquivos(e.target.files)}
        />
      </div>

      {erro && (
        <p className="erro" role="alert">
          {erro}
        </p>
      )}
      {aviso && <p className="aviso">{aviso}</p>}

      <div className="cartao">
        <h2>Aguardando confirmação ({itens.length})</h2>
        {itens.length === 0 ? (
          <p className="vazio">Nada em preparo.</p>
        ) : (
          <table className="tabela">
            <thead>
              <tr>
                <th>Arquivo</th>
                <th>Tamanho</th>
                <th>Título exibido nas fontes</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {itens.map((item) => (
                <tr key={item.chave}>
                  <td className="nome">{item.nome}</td>
                  <td>{formatarBytes(item.tamanho)}</td>
                  <td>
                    <input
                      type="text"
                      placeholder="Ex.: Resolução CS/IFS nº 348/2025"
                      value={titulos[item.chave] ?? ""}
                      disabled={ocupado}
                      onChange={(e) =>
                        setTitulos((t) => ({ ...t, [item.chave]: e.target.value }))
                      }
                    />
                  </td>
                  <td className="acoes">
                    <button
                      className="primario"
                      disabled={ocupado}
                      onClick={() =>
                        void comOcupado(
                          async () =>
                            void (await confirmarIndexacao(
                              api,
                              await obterToken(),
                              item.chave,
                              titulos[item.chave] ?? "",
                            )),
                          `"${item.nome}" entrou na fila de indexação.`,
                        )
                      }
                    >
                      Indexar
                    </button>
                    <button
                      className="discreto"
                      disabled={ocupado}
                      onClick={() =>
                        void comOcupado(
                          async () =>
                            void (await descartarPreparo(
                              api,
                              await obterToken(),
                              item.chave,
                            )),
                          `"${item.nome}" descartado.`,
                        )
                      }
                    >
                      Descartar
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {itens.length > 0 && (
          <p className="ajuda">
            O título é opcional, mas é ele que aparece na lista de fontes ao pé
            das respostas. Sem ele, o assistente cita o nome do arquivo.
          </p>
        )}
      </div>
    </section>
  );
}
