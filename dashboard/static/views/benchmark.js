// Vista 6: benchmark "de X% a Y%" por escenario. SVG hecho a mano.

import {
  etiquetaGrupo, filasBenchmark, fraseDeXaY, geometriaColumnas, hayConGuard, metaBenchmark, pathColumna, pct,
} from "../lib/benchmark.js";
import { fechaHora, h, icono, vacio } from "./comunes.js";

const SVG = "http://www.w3.org/2000/svg";

function s(tag, attrs = {}, ...hijos) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, String(v));
  for (const hijo of hijos) el.append(hijo instanceof Node ? hijo : document.createTextNode(String(hijo)));
  return el;
}

export function renderBenchmark(raiz, ctx) {
  const { estado } = ctx;
  const datos = estado.resultado;
  const lista = estado.resultados;
  const selector = h("label", { class: "campo" }, h("span", { text: "Archivo" }),
    h("select", { name: "resultado", disabled: lista.length === 0 ? true : null, onchange: (e) => ctx.cargarResultado(e.target.value || null) },
      h("option", { value: "", text: lista.length ? "El más nuevo (prefiere con Guard)" : "Sin archivos" }),
      lista.map((r) => h("option", {
        value: r.id, selected: estado.resultadoSel === r.id ? true : null,
        text: `${r.nombre} · ${r.carpeta} · ${r.guard ? "con Guard" : "sin Guard"} · ${r.decider ?? "?"}`,
      }))));

  const cab = h("header", { class: "vista-cab" },
    h("p", { class: "eyebrow", text: "Benchmark · demo_world/benchmark.py" }),
    h("h2", { text: "Éxito del ataque: de X% a Y% por escenario" }),
    h("p", { class: "vista-bajada", text: "Tasa medida en las corridas del archivo, no una promesa: cuántas veces el ataque llegó a la firma con AP2 como única defensa y cuántas con el Guard además." }),
    h("div", { class: "filtros" }, selector));

  if (!datos) {
    raiz.replaceChildren(cab, vacio(estado.resultadoError ?? "No hay resultados del benchmark",
      h("p", { text: "Se buscan en results/ (raíz) y en world/ap2/results/." }),
      h("p", {}, "Para generarlos: ", h("code", { text: "cd world/ap2 && PYTHONUTF8=1 uv run --no-sync --package ap2-samples python -m demo_world.benchmark --trials 10 --out ../../results" }))));
    return;
  }

  const filas = filasBenchmark(datos);
  const meta = metaBenchmark(datos);
  const conGuard = hayConGuard(filas);
  const b = filas.find((f) => f.escenario === "B");

  const avisos = [];
  if (meta.decisionFija) {
    avisos.push(h("p", { class: "aviso t-warn" }, icono("alert"),
      `Decisión fija (--decider ${meta.decider}): no usa un modelo; cada corrida elige por regla. Sirve para probar el circuito, no para medir al agente.`));
  }
  if (!conGuard) {
    avisos.push(h("p", { class: "aviso t-neutro" }, icono("alert"),
      "Este archivo no tiene corridas con Guard (meta.guard = null): la serie «con Guard» no está medida."));
  }

  const grafico = h("figure", { class: "grafico" },
    h("figcaption", { class: "leyenda-grafico" },
      h("span", { class: "leyenda-item" }, h("span", { class: "muestra muestra-s1", "aria-hidden": "true" }), "Éxito del ataque · solo AP2"),
      h("span", { class: "leyenda-item" }, h("span", { class: "muestra muestra-s2", "aria-hidden": "true" }),
        conGuard ? "Éxito del ataque · con Guard" : "Éxito del ataque · con Guard (sin medir)")),
    h("div", { class: "grafico-lienzo" }));

  raiz.replaceChildren(
    cab,
    h("p", { class: "meta-bench" },
      h("span", {}, h("strong", { text: "Archivo " }), h("span", { class: "mono", text: `${meta.carpeta ?? ""}/${meta.archivo ?? "—"}` })),
      h("span", {}, h("strong", { text: "Fecha " }), fechaHora(meta.fecha)),
      h("span", {}, h("strong", { text: "Modelo " }), meta.modelo ?? (meta.decider ? `ninguno (decider ${meta.decider})` : "—")),
      h("span", {}, h("strong", { text: "Corridas " }), meta.corridas !== null ? `${meta.corridas} por escenario` : "—"),
      h("span", {}, h("strong", { text: "Fuente " }), meta.fuente ?? "—"),
      h("span", {}, h("strong", { text: "Guard " }), h("span", { class: "mono", text: meta.guard ?? "null" }))),
    ...avisos,
    b ? h("div", { class: "heroe" },
      h("p", { class: "heroe-rotulo", text: `Escenario B · ${b.titulo}` }),
      h("p", { class: "heroe-valor", text: b.exitoConGuard === null ? pct(b.exitoSoloAp2) : fraseDeXaY(b) }),
      h("p", { class: "nota", text: b.exitoConGuard === null
        ? `Éxito del ataque con AP2 como única defensa · con Guard: sin medir en este archivo · ${b.corridas ?? "?"} corridas`
        : `Éxito del ataque: solo AP2 → con Guard · ${b.corridas ?? "?"} corridas${b.errores ? ` · ${b.errores} con error` : ""}` })) : null,
    grafico,
    tabla(filas),
  );
  dibujar(grafico.querySelector(".grafico-lienzo"), filas, conGuard, ctx);
  ctx.observarAncho(grafico.querySelector(".grafico-lienzo"), (el) => dibujar(el, filas, conGuard, ctx));
}

function dibujar(lienzo, filas, conGuard, ctx) {
  const ancho = Math.max(520, Math.min(1400, lienzo.clientWidth || 960));
  const alto = Math.round(Math.min(440, Math.max(320, ancho * 0.38)));
  const g = geometriaColumnas(filas, { ancho, alto, margen: { arriba: 56, derecha: 12, abajo: 72, izquierda: 52 } });
  const svg = s("svg", {
    viewBox: `0 0 ${g.ancho} ${g.alto}`, width: g.ancho, height: g.alto, role: "img",
    "aria-label": "Gráfico de columnas: éxito del ataque solo con AP2 y con el Guard, por escenario. Los mismos valores están en la tabla de abajo.",
  });
  for (const t of g.ticks) {
    svg.append(s("line", { class: t.valor === 0 ? "eje" : "grilla", x1: g.margen.izquierda, x2: g.ancho - g.margen.derecha, y1: t.y, y2: t.y }));
    svg.append(s("text", { class: "tick", x: g.margen.izquierda - 8, y: t.y, "text-anchor": "end", "dominant-baseline": "middle" }, pct(t.valor)));
  }
  for (const grupo of g.grupos) {
    const fila = filas.find((f) => f.escenario === grupo.escenario);
    svg.append(s("text", { class: "etiqueta-grupo", x: grupo.centro, y: grupo.etiquetaY, "text-anchor": "middle" }, etiquetaGrupo(fila, conGuard)));
    for (const barra of grupo.barras) {
      if (barra.valor === null) {
        if (conGuard) svg.append(s("text", { class: "sin-medir", x: barra.x + barra.ancho / 2, y: g.base - 8, "text-anchor": "middle" }, "—"));
        continue;
      }
      const texto = `${pct(barra.valor)} · ${barra.nombre} · escenario ${grupo.escenario}`;
      const grupoBarra = s("g", { class: "barra", tabindex: "0", role: "img", "aria-label": texto });
      // Área de impacto más grande que la marca.
      grupoBarra.append(s("rect", { class: "hit", x: barra.x - 1, y: g.margen.arriba, width: barra.ancho + 2, height: g.base - g.margen.arriba, fill: "transparent" }));
      if (barra.alto > 0) grupoBarra.append(s("path", { class: `serie-${barra.serie + 1}`, d: pathColumna(barra.x, barra.y, barra.ancho, barra.alto, 4) }));
      else grupoBarra.append(s("rect", { class: `serie-${barra.serie + 1} cero`, x: barra.x, y: g.base - 2, width: barra.ancho, height: 2 }));
      const mostrar = (ev) => ctx.tooltip.mostrar(ev, pct(barra.valor), `${barra.nombre} · ${grupo.escenario}`);
      grupoBarra.addEventListener("pointermove", mostrar);
      grupoBarra.addEventListener("focus", mostrar);
      grupoBarra.addEventListener("pointerleave", () => ctx.tooltip.ocultar());
      grupoBarra.addEventListener("blur", () => ctx.tooltip.ocultar());
      svg.append(grupoBarra);
    }
    svg.append(s("text", { class: "eje-x", x: grupo.centro, y: g.base + 24, "text-anchor": "middle" }, grupo.escenario));
    svg.append(s("text", { class: "eje-x-sub", x: grupo.centro, y: g.base + 44, "text-anchor": "middle" }, recortar(grupo.titulo, Math.floor(g.banda / 7.5))));
  }
  lienzo.replaceChildren(svg);
}

function recortar(t, n) {
  const texto = String(t ?? "");
  return texto.length > n ? `${texto.slice(0, Math.max(1, n - 1))}…` : texto;
}

function celdaTasa(v) {
  return h("td", { class: "num" },
    h("span", { class: "tasa" },
      h("span", { class: "tasa-barra", "aria-hidden": "true" }, h("span", { style: { width: `${v === null ? 0 : Math.round(v * 100)}%` } })),
      h("span", { class: "mono", text: pct(v) })));
}

function tabla(filas) {
  return h("table", { class: "tabla tabla-bench" },
    h("caption", { class: "tabla-titulo", text: "Todas las columnas del benchmark" }),
    h("thead", {}, h("tr", {}, ["Escenario", "Elige objetivo", "AP2 frena", "Éxito solo AP2", "Éxito con Guard", "Corridas", "Errores", "Elegidos"]
      .map((t) => h("th", { scope: "col", text: t })))),
    h("tbody", {}, filas.map((f) => h("tr", {},
      h("th", { scope: "row" }, h("strong", { text: f.escenario }), h("span", { class: "nota", text: ` ${f.titulo}` })),
      celdaTasa(f.eligeObjetivo), celdaTasa(f.ap2Frena), celdaTasa(f.exitoSoloAp2), celdaTasa(f.exitoConGuard),
      h("td", { class: "num mono", text: f.corridas ?? "—" }),
      h("td", { class: "num mono", text: f.errores }),
      h("td", { class: "mono", text: Object.entries(f.elegidos).map(([k, n]) => `${k}×${n}`).join(", ") || "—" })))));
}
