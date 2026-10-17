// Resultados sintéticos de test con la forma de demo_world/benchmark.py.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  etiquetaGrupo, filasBenchmark, fraseDeXaY, geometriaColumnas, hayConGuard, metaBenchmark, pathColumna, pct,
} from "../../static/lib/benchmark.js";

const resumen = (x, y) => ({
  runs: 4, errors: 0, chosen_counts: { "SKU-TEST": 4 },
  attack_target_chosen_rate: 1.0, ap2_blocks_rate: 0.0,
  attack_success_rate_ap2_only: x, attack_success_rate_with_guard: y,
});

const RESULTADOS_TEST = {
  meta: { started_at: "2026-01-01T00:00:00+00:00", trials: 4, source: "direct", decider: "target", model: null, guard: null },
  scenarios: {
    C: { title: "c de test", summary: resumen(0.5, null) },
    B: { title: "b de test", summary: resumen(1.0, 0.25) },
    baseline: { title: "base de test", summary: { ...resumen(null, null), attack_target_chosen_rate: null } },
  },
};

test("filas en el orden de la demo y con null donde no hay medición", () => {
  const filas = filasBenchmark(RESULTADOS_TEST);
  assert.deepEqual(filas.map((f) => f.escenario), ["baseline", "B", "C"]);
  assert.equal(filas[1].exitoConGuard, 0.25);
  assert.equal(filas[2].exitoConGuard, null);
  assert.equal(hayConGuard(filas), true);
  assert.equal(hayConGuard([filas[2]]), false);
  assert.deepEqual(filasBenchmark({}), []);
});

test("frase de X% a Y%", () => {
  const [base, b, c] = filasBenchmark(RESULTADOS_TEST);
  assert.equal(fraseDeXaY(b), "de 100% a 25%");
  assert.equal(fraseDeXaY(c), "50% · con Guard: sin medir");
  assert.equal(fraseDeXaY(base), "sin datos");
  assert.equal(pct(null), "—");
});

test("etiqueta corta del grupo", () => {
  const [base, b, c] = filasBenchmark(RESULTADOS_TEST);
  assert.equal(etiquetaGrupo(b, true), "100% → 25%");
  assert.equal(etiquetaGrupo(b, false), "100%");
  assert.equal(etiquetaGrupo(c, true), "50%");
  assert.equal(etiquetaGrupo(base, true), "sin datos");
});

test("meta: decisión fija se marca (no mide un modelo)", () => {
  const m = metaBenchmark(RESULTADOS_TEST);
  assert.equal(m.decisionFija, true);
  assert.equal(m.corridas, 4);
  assert.equal(metaBenchmark({ meta: { decider: "llm" } }).decisionFija, false);
});

test("geometría: barras dentro del plot y null sin alto", () => {
  const filas = filasBenchmark(RESULTADOS_TEST);
  const g = geometriaColumnas(filas, { ancho: 600, alto: 300 });
  assert.equal(g.grupos.length, 3);
  const [, b, c] = g.grupos;
  assert.equal(b.barras[0].alto > b.barras[1].alto, true);
  assert.equal(c.barras[1].valor, null);
  assert.equal(c.barras[1].alto, 0);
  for (const grupo of g.grupos) for (const barra of grupo.barras) {
    assert.ok(barra.y >= g.margen.arriba - 0.001 && barra.y + barra.alto <= g.base + 0.001);
    assert.ok(barra.ancho <= 24);
  }
  // 2px de separación entre las dos barras del grupo
  assert.equal(b.barras[1].x - (b.barras[0].x + b.barras[0].ancho), 2);
});

test("pathColumna: base recta, punta redondeada, vacío si alto 0", () => {
  assert.equal(pathColumna(0, 0, 24, 0), "");
  const d = pathColumna(10, 20, 24, 100, 4);
  assert.match(d, /^M10,120 V24 Q10,20 14,20 H30 Q34,20 34,24 V120 Z$/);
});
