/**
 * Login no Cognito.
 *
 * **O token enviado à API é o ID token.** O authorizer do API Gateway aceita os
 * dois, mas foi com o ID token que o fluxo ponta a ponta foi validado, e é dele
 * que o backend lê `cognito:groups` para autorizar. Trocar por access token sem
 * refazer aquele teste é convite a um 403 sem explicação.
 */

import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserPool,
  CognitoUserSession,
} from "amazon-cognito-identity-js";

export type Usuario = {
  email: string;
  /** Grupos do Cognito. `administradores` libera o painel. */
  grupos: string[];
  administrador: boolean;
};

let poolCache: CognitoUserPool | null = null;

function pool(idDoPool: string, idDoCliente: string): CognitoUserPool {
  if (!poolCache) {
    poolCache = new CognitoUserPool({ UserPoolId: idDoPool, ClientId: idDoCliente });
  }
  return poolCache;
}

function lerSessao(sessao: CognitoUserSession): { usuario: Usuario; token: string } {
  const carga = sessao.getIdToken().decodePayload();
  const grupos: string[] = carga["cognito:groups"] ?? [];
  return {
    usuario: {
      email: carga.email ?? "",
      grupos,
      administrador: grupos.includes("administradores"),
    },
    token: sessao.getIdToken().getJwtToken(),
  };
}

export async function entrar(
  idDoPool: string,
  idDoCliente: string,
  email: string,
  senha: string,
): Promise<{ usuario: Usuario; token: string; novaSenhaExigida?: true }> {
  const usuarioCognito = new CognitoUser({
    Username: email,
    Pool: pool(idDoPool, idDoCliente),
  });

  return new Promise((resolver, rejeitar) => {
    usuarioCognito.authenticateUser(
      new AuthenticationDetails({ Username: email, Password: senha }),
      {
        onSuccess: (sessao) => resolver(lerSessao(sessao)),
        onFailure: (erro) =>
          rejeitar(
            new Error(
              erro.code === "NotAuthorizedException"
                ? "E-mail ou senha incorretos."
                : erro.message || "Não foi possível entrar.",
            ),
          ),
        // Usuário criado pelo administrador chega em `FORCE_CHANGE_PASSWORD` e
        // não entra sem trocar a senha. Mostrar "falha no login" aqui seria
        // mentir sobre a causa — daí o retorno próprio.
        newPasswordRequired: () =>
          resolver({
            usuario: { email, grupos: [], administrador: false },
            token: "",
            novaSenhaExigida: true,
          }),
      },
    );
  });
}

export async function definirNovaSenha(
  idDoPool: string,
  idDoCliente: string,
  email: string,
  senhaProvisoria: string,
  novaSenha: string,
): Promise<{ usuario: Usuario; token: string }> {
  const usuarioCognito = new CognitoUser({
    Username: email,
    Pool: pool(idDoPool, idDoCliente),
  });

  return new Promise((resolver, rejeitar) => {
    usuarioCognito.authenticateUser(
      new AuthenticationDetails({ Username: email, Password: senhaProvisoria }),
      {
        onSuccess: (sessao) => resolver(lerSessao(sessao)),
        onFailure: (erro) => rejeitar(new Error(erro.message)),
        newPasswordRequired: () =>
          usuarioCognito.completeNewPasswordChallenge(
            novaSenha,
            {},
            {
              onSuccess: (sessao) => resolver(lerSessao(sessao)),
              onFailure: (erro) => rejeitar(new Error(erro.message)),
            },
          ),
      },
    );
  });
}

/**
 * Sessão guardada pelo SDK, renovando o token se preciso.
 *
 * Evita pedir senha a cada recarga: o SDK guarda os tokens no `localStorage` e
 * renova pelo refresh token quando o ID token expira.
 */
export async function sessaoAtual(
  idDoPool: string,
  idDoCliente: string,
): Promise<{ usuario: Usuario; token: string } | null> {
  const usuarioCognito = pool(idDoPool, idDoCliente).getCurrentUser();
  if (!usuarioCognito) return null;

  return new Promise((resolver) => {
    usuarioCognito.getSession((erro: Error | null, sessao: CognitoUserSession | null) => {
      if (erro || !sessao?.isValid()) return resolver(null);
      resolver(lerSessao(sessao));
    });
  });
}

/**
 * Token válido **agora**.
 *
 * Deve ser chamada imediatamente antes de cada requisição, nunca guardada no
 * estado da aplicação. O token do Cognito vale uma hora: capturá-lo no login e
 * reutilizá-lo faria quem deixa a aba aberta receber **401 em tudo** depois
 * desse prazo, sem nenhum sinal de que a causa é a sessão.
 *
 * `getSession` é barato — só vai à rede quando o token expirou.
 */
export async function tokenValido(
  idDoPool: string,
  idDoCliente: string,
): Promise<string> {
  const sessao = await sessaoAtual(idDoPool, idDoCliente);
  if (!sessao) throw new Error("Sua sessão expirou. Entre novamente.");
  return sessao.token;
}

export function sair(idDoPool: string, idDoCliente: string): void {
  pool(idDoPool, idDoCliente).getCurrentUser()?.signOut();
}
