/**
 * Aba de acervo: envio de documentos e confirmação da indexação.
 *
 * O arquivo enviado fica numa **área de preparo** até a confirmação. Isso não é
 * um aviso na tela: o preparo é um bucket separado, que nenhum data source
 * varre. Enquanto o arquivo está ali ele existe no S3 e **não existe para o
 * assistente** — descartar é apagar algo que nunca entrou na base, o que
 * resolve o arquivo errado sem precisar de remoção depois.
 */

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import {
  type ArquivoEmPreparo,
  type ConflitoDeEnvio,
  conflitoDoErro,
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
  // Conflito com o acervo por arquivo em preparo (cópia idêntica ou mesmo
  // nome). Fica na linha do arquivo, e não no aviso geral, porque pede uma
  // decisão sobre **aquele** arquivo.
  const [conflitos, setConflitos] = useState<Record<string, ConflitoDeEnvio>>({});
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

  function esquecerConflito(chave: string) {
    setConflitos(({ [chave]: _, ...resto }) => resto);
  }

  async function indexar(item: ArquivoEmPreparo, acao?: "substituir" | "manter_ambos") {
    setErro(null);
    setAviso(null);
    setOcupado(true);
    try {
      const r = await confirmarIndexacao(
        api,
        await obterToken(),
        item.chave,
        titulos[item.chave] ?? "",
        acao,
      );
      esquecerConflito(item.chave);
      setAviso(
        r.substituidos?.length
          ? `"${item.nome}" entrou na fila de indexação. A versão anterior foi para a lixeira (recuperável por 30 dias).`
          : `"${item.nome}" entrou na fila de indexação.`,
      );
      await recarregar();
    } catch (e) {
      const conflito = conflitoDoErro(e);
      if (conflito) {
        setConflitos((c) => ({ ...c, [item.chave]: conflito }));
      } else {
        setErro(e instanceof Error ? e.message : "Não foi possível concluir.");
      }
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
                <Fragment key={item.chave}>
                <tr>
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
                      disabled={ocupado || item.chave in conflitos}
                      onClick={() => void indexar(item)}
                    >
                      Indexar
                    </button>
                    <button
                      className="discreto"
                      disabled={ocupado}
                      onClick={() =>
                        void comOcupado(async () => {
                          await descartarPreparo(api, await obterToken(), item.chave);
                          esquecerConflito(item.chave);
                        }, `"${item.nome}" descartado.`)
                      }
                    >
                      Descartar
                    </button>
                  </td>
                </tr>
                {conflitos[item.chave] && (
                  <tr className="linha-conflito">
                    <td colSpan={4}>
                      <AvisoDeConflito
                        conflito={conflitos[item.chave]}
                        ocupado={ocupado}
                        aoSubstituir={() => void indexar(item, "substituir")}
                        aoManterAmbos={() => void indexar(item, "manter_ambos")}
                        aoCancelar={() => esquecerConflito(item.chave)}
                      />
                    </td>
                  </tr>
                )}
                </Fragment>
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

/**
 * Cópia idêntica não tem escolha: a mesma informação duas vezes ocupa o dobro
 * das vagas na busca e empurra para fora o trecho de outro documento. Mesmo
 * nome pode ser versão nova ou documento diferente — quem sabe é quem enviou.
 */
function AvisoDeConflito({
  conflito,
  ocupado,
  aoSubstituir,
  aoManterAmbos,
  aoCancelar,
}: {
  conflito: ConflitoDeEnvio;
  ocupado: boolean;
  aoSubstituir: () => void;
  aoManterAmbos: () => void;
  aoCancelar: () => void;
}) {
  const lista = conflito.pode_prosseguir ? conflito.mesmo_nome : conflito.identicos;
  return (
    <div className="confirmacao">
      <p className="alerta">
        {conflito.pode_prosseguir
          ? "Já existe no acervo documento com o mesmo nome:"
          : "Este arquivo já está no acervo, com o mesmo conteúdo:"}
      </p>
      <ul className="lista-conflito">
        {lista.map((chave) => (
          <li key={chave} title={chave}>
            {chave}
          </li>
        ))}
      </ul>
      {conflito.pode_prosseguir ? (
        <>
          <p>
            Se for uma versão nova, substitua: a anterior sai da base e fica
            recuperável por 30 dias. Se for outro documento, mantenha os dois.
          </p>
          <button className="primario" disabled={ocupado} onClick={aoSubstituir}>
            Substituir
          </button>
          <button className="discreto" disabled={ocupado} onClick={aoManterAmbos}>
            Manter os dois
          </button>
        </>
      ) : (
        <p>Não é preciso enviar de novo. Descarte este envio.</p>
      )}
      <button className="discreto" disabled={ocupado} onClick={aoCancelar}>
        Fechar
      </button>
    </div>
  );
}
