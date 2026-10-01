/**
 * Tela de entrada, incluindo a troca de senha provisória.
 *
 * Usuário criado pelo administrador chega em `FORCE_CHANGE_PASSWORD` e não
 * consegue entrar sem trocar a senha. Mostrar "falha no login" nesse caso seria
 * mentir sobre a causa — e é o primeiro contato de todo administrador novo.
 */

import { useState } from "react";
import { definirNovaSenha, entrar, type Usuario } from "../autenticacao";
import type { Config } from "../config";

export function Login({
  config,
  aoEntrar,
}: {
  config: Config;
  aoEntrar: (usuario: Usuario) => void;
}) {
  const [email, setEmail] = useState("");
  const [senha, setSenha] = useState("");
  const [novaSenha, setNovaSenha] = useState("");
  const [trocarSenha, setTrocarSenha] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [ocupado, setOcupado] = useState(false);

  async function enviar(evento: React.FormEvent) {
    evento.preventDefault();
    setErro(null);
    setOcupado(true);
    try {
      if (trocarSenha) {
        const r = await definirNovaSenha(
          config.poolId,
          config.clienteId,
          email,
          senha,
          novaSenha,
        );
        aoEntrar(r.usuario);
      } else {
        const r = await entrar(config.poolId, config.clienteId, email, senha);
        if (r.novaSenhaExigida) {
          setTrocarSenha(true);
          setErro("Sua senha é provisória. Defina uma nova para continuar.");
        } else {
          aoEntrar(r.usuario);
        }
      }
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Não foi possível entrar.");
    } finally {
      setOcupado(false);
    }
  }

  return (
    <main className="login">
      <form onSubmit={enviar} className="cartao">
        <h1>Acervo do Assistente</h1>
        <p className="ajuda">Pró-Reitoria de Gestão de Pessoas — IFS</p>

        <label>
          E-mail
          <input
            type="email"
            value={email}
            autoComplete="username"
            required
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>

        <label>
          {trocarSenha ? "Senha provisória" : "Senha"}
          <input
            type="password"
            value={senha}
            autoComplete="current-password"
            required
            onChange={(e) => setSenha(e.target.value)}
          />
        </label>

        {trocarSenha && (
          <label>
            Nova senha
            <input
              type="password"
              value={novaSenha}
              autoComplete="new-password"
              required
              onChange={(e) => setNovaSenha(e.target.value)}
            />
          </label>
        )}

        {erro && (
          <p className={trocarSenha ? "aviso" : "erro"} role="alert">
            {erro}
          </p>
        )}

        <button className="primario" type="submit" disabled={ocupado}>
          {ocupado ? "Entrando…" : trocarSenha ? "Definir senha e entrar" : "Entrar"}
        </button>
      </form>
    </main>
  );
}
