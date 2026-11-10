/**
 * Aba de documentos: o que está na base hoje, e remoção.
 *
 * **A procedência fica visível porque a remoção não se comporta igual em todos
 * os casos.** Documento do repositório da PROGEP sai em definitivo; página
 * vinda do site é regenerada pelo crawler e voltaria na próxima varredura. Uma
 * tela que escondesse essa diferença prometeria um controle que não tem.
 *
 * A confirmação é em dois passos, e não um `confirm()` do navegador: o aviso
 * sobre o conteúdo voltar precisa ser lido, e diálogo nativo não comporta isso.
 */

import { useCallback, useEffect, useState } from "react";
import {
  type Conferencia,
  type DocumentoNaBase,
  type GrupoDeDuplicatas,
  conferirIndexacao,
  formatarBytes,
  formatarData,
  listarDocumentos,
  listarDuplicatas,
  removerDocumento,
} from "../api";

export function Documentos({
  api,
  obterToken,
}: {
  api: string;
  obterToken: () => Promise<string>;
}) {
  const [itens, setItens] = useState<DocumentoNaBase[]>([]);
  const [truncado, setTruncado] = useState(false);
  const [filtro, setFiltro] = useState("");
  const [confirmando, setConfirmando] = useState<string | null>(null);
  const [ocupado, setOcupado] = useState(false);
  const [carregando, setCarregando] = useState(true);
  // Conferência sob demanda: custa ~6 s (a Knowledge Base devolve ~25
  // documentos por página e ignora `maxResults`), contra menos de 1 s da
  // listagem. Cobrar isso em toda abertura da aba tornaria a tela lenta para
  // responder uma pergunta que raramente se faz.
  const [conferencia, setConferencia] = useState<Conferencia | null>(null);
  const [conferindo, setConferindo] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  // Cópias já no acervo. Vem junto com a listagem (as duas leem só o S3, menos
  // de 1 s): cópia esquecida só é corrigida se alguém a vir sem procurar.
  const [duplicatas, setDuplicatas] = useState<GrupoDeDuplicatas[]>([]);

  const recarregar = useCallback(async () => {
    setCarregando(true);
    try {
      const token = await obterToken();
      const [r, d] = await Promise.all([
        listarDocumentos(api, token),
        listarDuplicatas(api, token),
      ]);
      setItens(r.itens);
      setTruncado(r.truncado);
      setDuplicatas(d.grupos);
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Falha ao carregar os documentos.");
    } finally {
      setCarregando(false);
    }
  }, [api, obterToken]);

  useEffect(() => {
    void recarregar();
  }, [recarregar]);

  async function remover(doc: DocumentoNaBase) {
    setErro(null);
    setAviso(null);
    setOcupado(true);
    try {
      const r = await removerDocumento(api, await obterToken(), doc.chave);
      setAviso(
        `"${doc.nome}" saiu da base. Recuperável por 30 dias em ${r.recuperavel_em}.`,
      );
      setConfirmando(null);
      // Tira a linha **na hora**, sem esperar a releitura. A remoção na Knowledge
      // Base é assíncrona: por alguns segundos o documento ainda volta na
      // listagem, e vê-lo reaparecer logo após a mensagem de sucesso dá a
      // entender que nada aconteceu. O estado da tela passa a refletir o que o
      // servidor já confirmou, não o que a listagem ainda não alcançou.
      setItens((atuais) => atuais.filter((i) => i.chave !== doc.chave));
      setDuplicatas((grupos) =>
        grupos
          .map((g) => ({
            ...g,
            chaves: g.chaves.filter((c) => c !== doc.chave),
            documentos: g.documentos.filter((d) => d.chave !== doc.chave),
          }))
          .filter((g) => g.chaves.length > 1),
      );
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Não foi possível remover.");
    } finally {
      setOcupado(false);
    }
  }

  async function conferir() {
    setErro(null);
    setConferindo(true);
    try {
      setConferencia(await conferirIndexacao(api, await obterToken()));
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Falha ao conferir a indexação.");
    } finally {
      setConferindo(false);
    }
  }

  const ausentes = new Set(conferencia?.ausentes ?? []);
  const termo = filtro.trim().toLowerCase();
  const visiveis = termo
    ? itens.filter(
        (i) =>
          i.nome.toLowerCase().includes(termo) || i.origem.toLowerCase().includes(termo),
      )
    : itens;

  return (
    <section className="painel">
      <div className="cartao">
        <h2>Documentos no acervo ({itens.length})</h2>
        <p className="ajuda">
          {truncado
            ? `Mostrando ${itens.length} documentos — o acervo tem mais que isso.`
            : "Os mais recentes aparecem primeiro."}
        </p>
        <input
          type="search"
          className="busca"
          placeholder="Filtrar por nome ou origem…"
          value={filtro}
          onChange={(e) => setFiltro(e.target.value)}
        />
        <div className="conferencia-barra">
          <button className="discreto" disabled={conferindo} onClick={() => void conferir()}>
            {conferindo ? "Conferindo…" : "Conferir indexação"}
          </button>
          {conferencia && (
            <span className={ausentes.size ? "alerta" : "ok"}>
              {ausentes.size === 0
                ? `Todos os ${conferencia.no_s3} documentos respondem.`
                : `${ausentes.size} de ${conferencia.no_s3} estão no acervo e não respondem.`}
              {conferencia.orfaos.length > 0 &&
                ` ${conferencia.orfaos.length} sobra(s) na base sem arquivo.`}
            </span>
          )}
        </div>
        {conferencia && ausentes.size > 0 && (
          <p className="ajuda">
            Documento que não indexou costuma ser PDF digitalizado sem camada de
            texto: ele entra vazio, sem erro, e o assistente age como se não
            existisse. Marcados na lista abaixo.
          </p>
        )}
      </div>

      {erro && (
        <p className="erro" role="alert">
          {erro}
        </p>
      )}
      {aviso && <p className="aviso">{aviso}</p>}

      {duplicatas.length > 0 && (
        <div className="cartao">
          <h2>Possíveis cópias ({duplicatas.length})</h2>
          <p className="ajuda">
            Cópia do mesmo documento atrapalha a busca: as duas ocupam as vagas
            de trecho que outro documento usaria para completar a resposta.
            Confira e remova a que sobra na lista abaixo (filtre pelo nome).
          </p>
          <ul className="duplicatas">
            {duplicatas.map((g) => (
              <li key={g.chaves.join("|")}>
                <span className={g.tipo === "identico" ? "etiqueta etiqueta-erro" : "etiqueta"}>
                  {g.tipo === "identico" ? "conteúdo idêntico" : "mesmo nome, conteúdo diferente"}
                </span>
                <ul>
                  {g.documentos.map((d) => (
                    <li key={d.chave} title={d.chave}>
                      <button className="link" onClick={() => setFiltro(d.nome)}>
                        {d.chave}
                      </button>{" "}
                      <span className="ajuda">({d.origem})</span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="cartao">
        {carregando ? (
          // Cinco segundos de tela imóvel parecem defeito. A listagem percorre
          // a base inteira; dizer isso é mais honesto que um vazio silencioso.
          <p className="vazio">Carregando os documentos da base…</p>
        ) : visiveis.length === 0 ? (
          <p className="vazio">
            {itens.length === 0
              ? "Nenhum documento na base."
              : `Nenhum documento corresponde a "${filtro}".`}
          </p>
        ) : (
          <table className="tabela">
            <thead>
              <tr>
                <th>Documento</th>
                <th>Origem</th>
                <th>Tamanho</th>
                <th>Atualizado</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visiveis.map((doc) => (
                <tr key={doc.chave}>
                  <td className="nome" title={doc.chave}>
                    {doc.nome}
                    {ausentes.has(doc.chave) && (
                      <span className="etiqueta etiqueta-erro">não responde</span>
                    )}
                  </td>
                  <td>
                    {doc.origem}
                    {doc.regenerave && <span className="etiqueta">regenerado</span>}
                  </td>
                  <td>{formatarBytes(doc.tamanho)}</td>
                  <td>{formatarData(doc.atualizado_em)}</td>
                  <td className="acoes">
                    {confirmando === doc.chave ? (
                      <div className="confirmacao">
                        {doc.aviso && <p className="alerta">{doc.aviso}</p>}
                        <p>
                          Remover <strong>{doc.nome}</strong> da base? O arquivo
                          fica recuperável por 30 dias.
                        </p>
                        <button
                          className="perigo"
                          disabled={ocupado}
                          onClick={() => void remover(doc)}
                        >
                          Confirmar remoção
                        </button>
                        <button
                          className="discreto"
                          disabled={ocupado}
                          onClick={() => setConfirmando(null)}
                        >
                          Cancelar
                        </button>
                      </div>
                    ) : (
                      <button
                        className="discreto"
                        disabled={ocupado}
                        onClick={() => setConfirmando(doc.chave)}
                      >
                        Remover
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
