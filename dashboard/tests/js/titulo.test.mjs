// La oferta de una oportunidad se nombra con el título público (catálogo o
// fabricante) o, si no hay, con el SKU: nunca con el título que declara el
// vendedor sin marcarlo. Datos sintéticos de test.
import { test } from "node:test";
import assert from "node:assert/strict";

import { nombreOferta } from "../../static/lib/agente.js";
import { describirEvento } from "../../static/lib/eventos.js";

test("con título público, se muestra rotulado como público", () => {
  const n = nombreOferta({ titulo: "Orion Notebook 14 (catálogo)", sku: "OR-NB-14" });
  assert.equal(n.texto, "Orion Notebook 14 (catálogo)");
  assert.match(n.nota, /título público/);
});

test("sin título público, el SKU (y se dice que el del vendedor no se muestra)", () => {
  const n = nombreOferta({ titulo: "", sku: "OR-NB-14" });
  assert.equal(n.texto, "OR-NB-14");
  assert.match(n.nota, /vendedor/);
  assert.equal(nombreOferta({ titulo: "   ", sku: "X-1" }).texto, "X-1");
});

test("sin título ni SKU, un guion", () => {
  assert.equal(nombreOferta({}).texto, "—");
  assert.equal(nombreOferta(null).texto, "—");
  assert.equal(nombreOferta({ titulo: 42, sku: ["x"] }).texto, "—");
});

test("la línea de tiempo nombra la oferta igual", () => {
  const evento = {
    id: "t-1", ts: "2026-10-18T12:00:00Z", contexto_id: null, escenario: null, guard_activo: true,
    tipo: "oportunidad",
    datos: { compra_id: "c", oportunidad: { cart_id: "cart_2", sku: "OR-NB-14", titulo: "", cambios: [] } },
  };
  assert.match(describirEvento(evento).resumen, /^OR-NB-14/);
});
