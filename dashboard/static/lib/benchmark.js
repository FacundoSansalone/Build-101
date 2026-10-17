// Resultados de demo_world/benchmark.py → datos del gráfico "de X% a Y%".
// Formato de entrada: { meta, scenarios: { <nombre>: { title, attack, trials, summary } } }

export const ORDEN_ESCENARIOS = ["baseline", "A", "B", "B2", "C"];

function tasa(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function ordenEscenario(nombre) {
  const i = ORDEN_ESCENARIOS.indexOf(nombre);
  return i < 0 ? ORDEN_ESCENARIOS.length : i;
}

/** Una fila por escenario, en el orden de la demo. */
export function filasBenchmark(resultados) {
  const escenarios = resultados?.scenarios;
  if (!escenarios || typeof escenarios !== "object") return [];
  return Object.entries(escenarios)
    .map(([nombre, datos]) => {
      const s = datos?.summary || {};
      return {
        escenario: nombre,
        titulo: datos?.title ?? nombre,
        ataque: datos?.attack ?? null,
        eligeObjetivo: tasa(s.attack_target_chosen_rate),
        ap2Frena: tasa(s.ap2_blocks_rate),
        exitoSoloAp2: tasa(s.attack_success_rate_ap2_only),
        exitoConGuard: tasa(s.attack_success_rate_with_guard),
        corridas: typeof s.runs === "number" ? s.runs : null,
        errores: typeof s.errors === "number" ? s.errors : 0,
        elegidos: s.chosen_counts && typeof s.chosen_counts === "object" ? s.chosen_counts : {},
      };
    })
    .sort((a, b) => ordenEscenario(a.escenario) - ordenEscenario(b.escenario) ||
      a.escenario.localeCompare(b.escenario));
}

/** Datos del archivo para el pie del gráfico. */
export function metaBenchmark(resultados) {
  const m = resultados?.meta || {};
  const a = resultados?._archivo || {};
  const decider = m.decider ?? null;
  return {
    archivo: a.nombre ?? null,
    carpeta: a.carpeta ?? null,
    fecha: m.started_at ?? a.fecha ?? null,
    fechaFin: m.finished_at ?? null,
    modelo: m.model ?? null,
    decider,
    // 'best' y 'target' no usan LLM: prueban el circuito, no miden un modelo.
    decisionFija: decider !== null && decider !== "llm",
    corridas: typeof m.trials === "number" ? m.trials : null,
    fuente: m.source ?? null,
    guard: m.guard ?? null,
  };
}

/** ¿Hay al menos una tasa "con Guard" medida? */
export function hayConGuard(filas) {
  return filas.some((f) => f.exitoConGuard !== null);
}

export function pct(v, { decimales = 0 } = {}) {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(decimales)}%`;
}

/** "de 100% a 0%", o lo que se puede decir si falta la medición con Guard. */
export function fraseDeXaY(fila) {
  if (fila.exitoSoloAp2 === null) return "sin datos";
  if (fila.exitoConGuard === null) return `${pct(fila.exitoSoloAp2)} · con Guard: sin medir`;
  return `de ${pct(fila.exitoSoloAp2)} a ${pct(fila.exitoConGuard)}`;
}

/** Columna con la punta redondeada (radio r) y la base recta. */
export function pathColumna(x, y, ancho, alto, r = 4) {
  if (alto <= 0) return "";
  const rr = Math.min(r, ancho / 2, alto);
  const base = y + alto;
  return [
    `M${x},${base}`,
    `V${y + rr}`,
    `Q${x},${y} ${x + rr},${y}`,
    `H${x + ancho - rr}`,
    `Q${x + ancho},${y} ${x + ancho},${y + rr}`,
    `V${base}`,
    "Z",
  ].join(" ");
}

/**
 * Geometría del gráfico de columnas agrupadas (dos series por escenario).
 * Pura: el dibujo SVG lo hace la vista. Valores en [0, 1].
 */
export function geometriaColumnas(filas, {
  ancho = 960, alto = 360,
  margen = { arriba: 56, derecha: 16, abajo: 64, izquierda: 56 },
  anchoBarra = 24, separacion = 2,
} = {}) {
  const plotAncho = Math.max(1, ancho - margen.izquierda - margen.derecha);
  const plotAlto = Math.max(1, alto - margen.arriba - margen.abajo);
  const banda = filas.length > 0 ? plotAncho / filas.length : plotAncho;
  const y = (v) => margen.arriba + plotAlto * (1 - Math.max(0, Math.min(1, v)));
  const base = margen.arriba + plotAlto;
  const series = [
    { clave: "exitoSoloAp2", nombre: "Éxito del ataque · solo AP2" },
    { clave: "exitoConGuard", nombre: "Éxito del ataque · con Guard" },
  ];
  const grupo = series.length * anchoBarra + (series.length - 1) * separacion;
  const grupos = filas.map((f, i) => {
    const centro = margen.izquierda + banda * (i + 0.5);
    const x0 = centro - grupo / 2;
    const barras = series.map((s, j) => {
      const v = f[s.clave];
      const x = x0 + j * (anchoBarra + separacion);
      if (v === null) return { serie: j, clave: s.clave, nombre: s.nombre, valor: null, x, y: base, ancho: anchoBarra, alto: 0 };
      const yTop = y(v);
      return { serie: j, clave: s.clave, nombre: s.nombre, valor: v, x, y: yTop, ancho: anchoBarra, alto: base - yTop };
    });
    const topes = barras.filter((b) => b.valor !== null).map((b) => b.y);
    return {
      escenario: f.escenario, titulo: f.titulo, centro,
      etiquetaY: (topes.length ? Math.min(...topes) : base) - 10,
      barras,
    };
  });
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((v) => ({ valor: v, y: y(v) }));
  return { ancho, alto, margen, base, banda, grupos, ticks, series };
}
