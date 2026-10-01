import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `amazon-cognito-identity-js` referencia `global`, que só existe no Node. Sem
// mapeá-lo, a página abre **em branco** com `ReferenceError: global is not
// defined` no console — e nada aponta para a biblioteca de login como causa.
//
// Precisa dos **dois** lugares: `define` cobre o código da aplicação, e
// `optimizeDeps.esbuildOptions` cobre as dependências que o Vite pré-empacota
// em desenvolvimento, que é justamente onde o Cognito mora.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  define: { global: "globalThis" },
  optimizeDeps: { esbuildOptions: { define: { global: "globalThis" } } },
});
