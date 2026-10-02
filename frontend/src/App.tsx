/**
 * Casca do painel: configuração, sessão e as duas abas.
 *
 * A aba só aparece para quem está no grupo `administradores` — mas isso é
 * cortesia, não segurança. **O controle é o backend** (`acervo.exige_admin`):
 * esconder botão nunca protegeu endpoint, e o painel inteiro é alcançável por
 * qualquer um com um token válido e um cliente HTTP.
 */

import { useCallback, useEffect, useState } from "react";
import { Acervo } from "./componentes/Acervo";
import { Custos } from "./componentes/Custos";
import { Documentos } from "./componentes/Documentos";
import { Login } from "./componentes/Login";
import { carregarConfig, type Config } from "./config";
import { sair, sessaoAtual, tokenValido, type Usuario } from "./autenticacao";

type Aba = "acervo" | "documentos" | "custos";

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [usuario, setUsuario] = useState<Usuario | null>(null);
  const [aba, setAba] = useState<Aba>("acervo");
  const [carregando, setCarregando] = useState(true);
  const [falha, setFalha] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const c = await carregarConfig();
        setConfig(c);
        // Sessão guardada pelo SDK: evita pedir senha a cada recarga da página.
        const s = await sessaoAtual(c.poolId, c.clienteId);
        if (s) setUsuario(s.usuario);
      } catch (e) {
        setFalha(e instanceof Error ? e.message : "Falha ao iniciar o painel.");
      } finally {
        setCarregando(false);
      }
    })();
  }, []);

  // Buscado a cada requisição, nunca guardado no estado: o token vale uma hora,
  // e uma aba deixada aberta passaria a receber 401 em tudo sem explicação.
  const obterToken = useCallback(async () => {
    if (!config) throw new Error("Painel ainda não configurado.");
    return tokenValido(config.poolId, config.clienteId);
  }, [config]);

  if (carregando) return <main className="carregando">Carregando…</main>;
  if (falha)
    return (
      <main className="carregando">
        <p className="erro" role="alert">
          {falha}
        </p>
      </main>
    );
  if (!config) return null;
  if (!usuario) return <Login config={config} aoEntrar={setUsuario} />;

  if (!usuario.administrador) {
    return (
      <main className="carregando">
        <div className="cartao">
          <h1>Sem acesso ao acervo</h1>
          <p>
            Sua conta ({usuario.email}) não está no grupo de administradores do
            acervo. Peça a inclusão a quem administra o assistente.
          </p>
          <button
            className="discreto"
            onClick={() => {
              sair(config.poolId, config.clienteId);
              setUsuario(null);
            }}
          >
            Sair
          </button>
        </div>
      </main>
    );
  }

  return (
    <>
      <header className="topo">
        <div>
          <strong>Acervo do Assistente</strong>
          <span className="sub">PROGEP / IFS</span>
        </div>
        <nav className="abas">
          <button
            className={aba === "acervo" ? "aba ativa" : "aba"}
            onClick={() => setAba("acervo")}
          >
            Enviar
          </button>
          <button
            className={aba === "documentos" ? "aba ativa" : "aba"}
            onClick={() => setAba("documentos")}
          >
            Documentos na base
          </button>
          <button
            className={aba === "custos" ? "aba ativa" : "aba"}
            onClick={() => setAba("custos")}
          >
            Custos
          </button>
        </nav>
        <div className="usuario">
          <span>{usuario.email}</span>
          <button
            className="discreto"
            onClick={() => {
              sair(config.poolId, config.clienteId);
              setUsuario(null);
            }}
          >
            Sair
          </button>
        </div>
      </header>

      {aba === "acervo" && <Acervo api={config.api} obterToken={obterToken} />}
      {aba === "documentos" && <Documentos api={config.api} obterToken={obterToken} />}
      {aba === "custos" && <Custos api={config.api} obterToken={obterToken} />}
    </>
  );
}
