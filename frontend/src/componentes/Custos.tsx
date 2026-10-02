/**
 * Aba de custos: quanto o assistente gastou, medido minutos depois do uso.
 *
 * O número é **medido**, não estimado: o backend multiplica cada consumo real
 * (tokens de uma chamada, uma mensagem do WhatsApp, uma hora de CPU) pelo preço
 * público do serviço. A fatura da AWS chega com até um dia de atraso e aparece
 * só como conferência, no rodapé.
 *
 * O aviso de "só PROGEP" fica no topo, sempre visível: a conta da AWS é
 * compartilhada com outros projetos do IFS, e quem compara este painel com a
 * fatura da conta precisa saber por que os totais não batem.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type Categoria,
  type Periodo,
  type ResumoDeCustos,
  buscarCustos,
  formatarData,
  formatarUsd,
} from "../api";

const PERIODOS: { valor: Periodo; rotulo: string }[] = [
  { valor: "hoje", rotulo: "Hoje" },
  { valor: "7d", rotulo: "7 dias" },
  { valor: "30d", rotulo: "30 dias" },
  { valor: "mes", rotulo: "Este mês" },
  { valor: "mes_anterior", rotulo: "Mês anterior" },
];

/** Ordem fixa: a cor segue a categoria, nunca a posição no período. */
const CATEGORIAS: Categoria[] = ["assistente", "documentos", "infraestrutura"];

/** Atualização automática enquanto o período inclui hoje. */
const RECARGA_MS = 60_000;

export function Custos({
  api,
  obterToken,
}: {
  api: string;
  obterToken: () => Promise<string>;
}) {
  const [periodo, setPeriodo] = useState<Periodo>("mes");
  const [resumo, setResumo] = useState<ResumoDeCustos | null>(null);
  const [hoje, setHoje] = useState<ResumoDeCustos | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [carregando, setCarregando] = useState(true);

  const carregar = useCallback(async () => {
    try {
      const token = await obterToken();
      const [r, h] = await Promise.all([
        buscarCustos(api, token, periodo),
        periodo === "hoje" ? Promise.resolve(null) : buscarCustos(api, token, "hoje"),
      ]);
      setResumo(r);
      setHoje(h ?? r);
      setErro(null);
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Falha ao carregar os custos.");
    } finally {
      setCarregando(false);
    }
  }, [api, obterToken, periodo]);

  useEffect(() => {
    setCarregando(true);
    void carregar();
    if (periodo === "mes_anterior") return;
    const id = window.setInterval(() => void carregar(), RECARGA_MS);
    return () => window.clearInterval(id);
  }, [carregar, periodo]);

  return (
    <main className="painel custos">
      <p className="alerta" role="note">
        Estes valores são <strong>apenas do Assistente Virtual da PROGEP</strong>. A conta
        da AWS do IFS também hospeda outros projetos, que não entram aqui.
      </p>

      {erro && (
        <p className="erro" role="alert">
          {erro}
        </p>
      )}

      {hoje && (
        <section className="cartao destaque">
          <div>
            <span className="rotulo-figura">Gasto de hoje</span>
            <span className="figura">{formatarUsd(hoje.total_usd)}</span>
            <span className="ajuda">
              {hoje.perguntas} {hoje.perguntas === 1 ? "pergunta" : "perguntas"}
              {hoje.atualizado_em && <> · atualizado em {formatarData(hoje.atualizado_em)}</>}
            </span>
          </div>
          <CotaMeta meta={hoje.meta} />
        </section>
      )}

      <nav className="periodos" aria-label="Período">
        {PERIODOS.map((p) => (
          <button
            key={p.valor}
            className={periodo === p.valor ? "aba ativa" : "aba"}
            aria-pressed={periodo === p.valor}
            onClick={() => setPeriodo(p.valor)}
          >
            {p.rotulo}
          </button>
        ))}
      </nav>

      {carregando && !resumo && <p className="vazio">Carregando…</p>}

      {resumo && (
        <>
          <section className="figuras">
            <Figura rotulo={rotuloDoPeriodo(resumo)} valor={formatarUsd(resumo.total_usd)} />
            <Figura rotulo="Perguntas" valor={resumo.perguntas.toLocaleString("pt-BR")} />
            <Figura
              rotulo="Custo do assistente por pergunta"
              valor={
                resumo.custo_por_pergunta_usd === null
                  ? "—"
                  : formatarUsd(resumo.custo_por_pergunta_usd)
              }
            />
            {resumo.projecao_mes_usd !== null && (
              <Figura
                rotulo="Projeção para o mês"
                valor={formatarUsd(resumo.projecao_mes_usd)}
                nota={
                  resumo.media_diaria_usd !== null
                    ? `média de ${formatarUsd(resumo.media_diaria_usd)}/dia nos últimos 30 dias`
                    : undefined
                }
              />
            )}
          </section>

          {resumo.dias.length > 1 && (
            <section className="cartao">
              <h2>Gasto por dia</h2>
              <Legenda resumo={resumo} />
              <GraficoDiario resumo={resumo} />
            </section>
          )}

          <section className="cartao">
            <h2>Detalhamento</h2>
            <TabelaDeItens resumo={resumo} />
          </section>

          <section className="cartao notas">
            <h2>Como estes valores são calculados</h2>
            <ul>
              <li>
                Cada uso real (tokens do modelo, mensagens, tempo de execução) é multiplicado
                pelo preço público da AWS e da Meta. O gasto aparece aqui de 1 a 5 minutos
                depois do uso; a execução do assistente e os serviços de apoio, de hora em
                hora.
              </li>
              <li>
                A infraestrutura fixa (credenciais, armazenamento) entra como o valor mensal
                dividido pelos dias do mês. O registro de auditoria vem da fatura, com até um
                dia de atraso.
              </li>
              <li>
                Valores em dólar, sem os impostos que a AWS acrescenta na fatura (cerca de
                13%).
              </li>
              {resumo.conferencia && (
                <li>
                  Conferência com a fatura da AWS, modelo Claude até{" "}
                  {formatarDia(resumo.conferencia.ate)}: medido{" "}
                  {formatarUsd(resumo.conferencia.medido_usd)}, faturado{" "}
                  {formatarUsd(resumo.conferencia.faturado_usd)}.
                </li>
              )}
            </ul>
          </section>
        </>
      )}
    </main>
  );
}

function Figura({ rotulo, valor, nota }: { rotulo: string; valor: string; nota?: string }) {
  return (
    <div className="cartao figura-pequena">
      <span className="rotulo-figura">{rotulo}</span>
      <span className="valor">{valor}</span>
      {nota && <span className="ajuda nota-figura">{nota}</span>}
    </div>
  );
}

function CotaMeta({ meta }: { meta: ResumoDeCustos["meta"] }) {
  if (meta.isento) {
    return (
      <div className="cota">
        <span className="rotulo-figura">Mensagens do WhatsApp (Meta)</span>
        <span className="ajuda">Isenção de órgão público: sem tarifa da Meta.</span>
      </div>
    );
  }
  const usadas = Math.min(meta.entregues_no_mes, meta.cota);
  const fracao = meta.cota ? usadas / meta.cota : 0;
  return (
    <div className="cota">
      <span className="rotulo-figura">Cota grátis da Meta neste mês</span>
      <div
        className="medidor"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={meta.cota}
        aria-valuenow={usadas}
        aria-label="Mensagens grátis usadas"
      >
        <div className={fracao >= 0.9 ? "medidor-uso alto" : "medidor-uso"}
             style={{ width: `${fracao * 100}%` }} />
      </div>
      <span className="ajuda">
        {meta.entregues_no_mes.toLocaleString("pt-BR")} de {meta.cota.toLocaleString("pt-BR")}{" "}
        mensagens entregues
        {meta.entregues_no_mes > meta.cota && " — as excedentes são cobradas"}
      </span>
    </div>
  );
}

function Legenda({ resumo }: { resumo: ResumoDeCustos }) {
  return (
    <ul className="legenda">
      {CATEGORIAS.map((c) => (
        <li key={c}>
          <span className={`amostra serie-${c}`} aria-hidden="true" />
          {resumo.categorias[c]}
          <span className="ajuda"> {formatarUsd(resumo.por_categoria[c])}</span>
        </li>
      ))}
    </ul>
  );
}

// --- Gráfico -----------------------------------------------------------------

const ALTURA = 220;
const MARGEM = { topo: 12, direita: 8, base: 26, esquerda: 64 };
const LARGURA_MAX_COLUNA = 24;
const VAO = 2;
const RAIO = 4;

function GraficoDiario({ resumo }: { resumo: ResumoDeCustos }) {
  const [largura, setLargura] = useState(900);
  const [foco, setFoco] = useState<number | null>(null);
  const caixa = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!caixa.current) return;
    const observador = new ResizeObserver(([e]) => setLargura(e.contentRect.width));
    observador.observe(caixa.current);
    return () => observador.disconnect();
  }, []);

  const totais = resumo.dias.map((d) => CATEGORIAS.reduce((s, c) => s + d[c], 0));
  const { topo, marcas } = escala(Math.max(...totais, 0));
  const util = Math.max(largura - MARGEM.esquerda - MARGEM.direita, 50);
  const passo = util / resumo.dias.length;
  const coluna = Math.min(LARGURA_MAX_COLUNA, Math.max(passo * 0.6, 3));
  const alturaUtil = ALTURA - MARGEM.topo - MARGEM.base;
  const y = (v: number) => MARGEM.topo + alturaUtil - (topo ? (v / topo) * alturaUtil : 0);
  const rotular = resumo.dias.length <= 10 ? 1 : Math.ceil(resumo.dias.length / 8);

  return (
    <div className="grafico" ref={caixa}>
      <svg width={largura} height={ALTURA} role="img"
           aria-label="Gasto por dia, separado em assistente, documentos e infraestrutura">
        {marcas.map((m) => (
          <g key={m}>
            <line x1={MARGEM.esquerda} x2={largura - MARGEM.direita} y1={y(m)} y2={y(m)}
                  className={m === 0 ? "eixo" : "grade"} />
            <text x={MARGEM.esquerda - 8} y={y(m)} dy="0.32em" textAnchor="end"
                  className="marca">
              {formatarUsd(m)}
            </text>
          </g>
        ))}
        {resumo.dias.map((d, i) => {
          const x = MARGEM.esquerda + passo * i + (passo - coluna) / 2;
          let base = 0;
          const segmentos = CATEGORIAS.filter((c) => d[c] > 0);
          return (
            <g key={d.dia}>
              {segmentos.map((c, j) => {
                const y0 = y(base);
                base += d[c];
                const y1 = y(base);
                const ultimo = j === segmentos.length - 1;
                // Vão de 2 px entre segmentos; o arredondamento só no topo da pilha.
                const h = Math.max(y0 - y1 - (j > 0 ? VAO : 0), 0.5);
                const yTopo = y0 - (j > 0 ? VAO : 0) - h;
                return (
                  <path key={c} className={`serie-${c}`}
                        d={retangulo(x, yTopo, coluna, h, ultimo ? RAIO : 0)} />
                );
              })}
              {i % rotular === 0 && (
                <text x={x + coluna / 2} y={ALTURA - 8} textAnchor="middle" className="marca">
                  {formatarDia(d.dia)}
                </text>
              )}
              {/* Alvo de hover mais largo que a coluna. */}
              <rect x={MARGEM.esquerda + passo * i} y={MARGEM.topo} width={passo}
                    height={alturaUtil} fill="transparent"
                    onMouseEnter={() => setFoco(i)} onMouseLeave={() => setFoco(null)} />
            </g>
          );
        })}
      </svg>
      {foco !== null && (
        <div className="dica" role="tooltip"
             style={{ left: Math.min(MARGEM.esquerda + passo * (foco + 0.5) + 12, largura - 220) }}>
          <strong>{formatarDia(resumo.dias[foco].dia)}</strong>
          <span>Total {formatarUsd(totais[foco])}</span>
          {CATEGORIAS.map((c) => (
            <span key={c}>
              <span className={`amostra serie-${c}`} aria-hidden="true" />
              {resumo.categorias[c]}: {formatarUsd(resumo.dias[foco][c])}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function TabelaDeItens({ resumo }: { resumo: ResumoDeCustos }) {
  if (!resumo.itens.length) return <p className="vazio">Nenhum gasto registrado no período.</p>;
  return (
    <table className="tabela">
      <thead>
        <tr>
          <th>Item</th>
          <th className="numero">Quantidade</th>
          <th className="numero">Custo</th>
        </tr>
      </thead>
      <tbody>
        {CATEGORIAS.map((c) => {
          const itens = resumo.itens.filter((i) => i.categoria === c);
          if (!itens.length) return null;
          return [
            <tr key={c} className="grupo">
              <td>
                <span className={`amostra serie-${c}`} aria-hidden="true" />
                {resumo.categorias[c]}
              </td>
              <td />
              <td className="numero">{formatarUsd(resumo.por_categoria[c])}</td>
            </tr>,
            ...itens.map((i) => (
              <tr key={i.item}>
                <td className="recuo">{i.rotulo}</td>
                <td className="numero">
                  {i.unidade && i.quantidade
                    ? `${Math.round(i.quantidade).toLocaleString("pt-BR")} ${i.unidade}`
                    : "—"}
                </td>
                <td className="numero">{formatarUsd(i.usd)}</td>
              </tr>
            )),
          ];
        })}
        <tr className="total">
          <td>Total</td>
          <td />
          <td className="numero">{formatarUsd(resumo.total_usd)}</td>
        </tr>
      </tbody>
    </table>
  );
}

// --- utilidades -----------------------------------------------------------------

/** Topo arredondado e marcas "limpas" (1, 2 ou 5 × 10ⁿ) para o eixo. */
function escala(maximo: number): { topo: number; marcas: number[] } {
  if (maximo <= 0) return { topo: 0.01, marcas: [0, 0.01] };
  const bruto = maximo / 4;
  const potencia = 10 ** Math.floor(Math.log10(bruto));
  const passo = [1, 2, 5, 10].map((m) => m * potencia).find((p) => p >= bruto) ?? bruto;
  const topo = Math.ceil(maximo / passo) * passo;
  const marcas: number[] = [];
  for (let v = 0; v <= topo + passo / 2; v += passo) marcas.push(Number(v.toFixed(6)));
  return { topo, marcas };
}

function retangulo(x: number, y: number, w: number, h: number, r: number): string {
  const raio = Math.min(r, w / 2, h);
  if (!raio) return `M${x},${y}h${w}v${h}h${-w}Z`;
  return (
    `M${x},${y + h}V${y + raio}Q${x},${y} ${x + raio},${y}` +
    `H${x + w - raio}Q${x + w},${y} ${x + w},${y + raio}V${y + h}Z`
  );
}

function formatarDia(iso: string): string {
  const [, mes, dia] = iso.split("-");
  return `${dia}/${mes}`;
}

function rotuloDoPeriodo(r: ResumoDeCustos): string {
  const nomes: Record<Periodo, string> = {
    hoje: "Total de hoje",
    "7d": "Total em 7 dias",
    "30d": "Total em 30 dias",
    mes: "Total do mês até agora",
    mes_anterior: "Total do mês anterior",
  };
  return nomes[r.periodo] ?? "Total do período";
}
