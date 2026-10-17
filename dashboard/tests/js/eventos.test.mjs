// Eventos sintéticos de test (no son una compra real).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  agruparCompras, cartIdElegido, describirEvento, estadoCompra, fusionarEventos,
  mandatosDe, montoCheckout, parLadoALado, partirTextoLibre, topeDeFicha, aJsonl,
} from "../../static/lib/eventos.js";

let n = 0;
const ev = (tipo, extra = {}) => ({
  id: `test-${++n}`,
  ts: new Date(Date.UTC(2026, 0, 1, 0, 0, n)).toISOString(),
  contexto_id: "ctx-test",
  escenario: "B",
  guard_activo: false,
  tipo,
  datos: {},
  ...extra,
});

test("fusionarEventos deduplica por id y ordena por ts", () => {
  const a = ev("busqueda_enviada");
  const b = ev("ofertas_recibidas");
  const fusion = fusionarEventos([b], [a, b]);
  assert.deepEqual(fusion.map((e) => e.id), [a.id, b.id]);
});

test("agruparCompras separa por contexto y deja aparte los de sistema", () => {
  const eventos = [
    ev("guard_cambiado", { contexto_id: null, datos: { guard_activo: true } }),
    ev("busqueda_enviada", { contexto_id: "ctx-1" }),
    ev("busqueda_enviada", { contexto_id: "ctx-2" }),
  ];
  const { compras, sistema } = agruparCompras(eventos);
  assert.equal(sistema.length, 1);
  assert.deepEqual(compras.map((c) => c.contextoId), ["ctx-2", "ctx-1"]);
});

test("compra bloqueada con Guard: elegido, estado y guard del momento de la decisión", () => {
  const evs = [
    ev("busqueda_enviada", { contexto_id: "ctx-b", guard_activo: true, datos: { catalog_search: "x" } }),
    ev("ofertas_recibidas", { contexto_id: "ctx-b", guard_activo: true, datos: { carts: [{ cart_id: "cart_1" }], offer_details: [] } }),
    ev("checkout_pedido", { contexto_id: "ctx-b", guard_activo: true, datos: { cart_id: "cart_1" } }),
    ev("checkout_recibido", { contexto_id: "ctx-b", guard_activo: true, datos: { cart_id: "cart_1", ap2_violaciones: [], jwt_payload: { totals: [{ type: "total", amount: 14900 }] } } }),
    ev("veredicto", { contexto_id: "ctx-b", guard_activo: true, datos: { veredicto: { status: "bloqueado", motivo: "m" } } }),
    ev("bloqueado", { contexto_id: "ctx-b", guard_activo: true, datos: { cart_id: "cart_1", motivo: "m" } }),
  ];
  const [c] = agruparCompras(evs).compras;
  assert.equal(c.guardActivo, true);
  assert.equal(cartIdElegido(c), "cart_1");
  assert.equal(estadoCompra(c).clave, "bloqueado");
  assert.equal(montoCheckout(c.checkout), 149);
});

test("compra sin Guard: checkout reenviado y después mandatos firmados", () => {
  const evs = [
    ev("checkout_recibido", { contexto_id: "ctx-s", datos: { cart_id: "cart_4", ap2_violaciones: [] } }),
    ev("reenviado", { contexto_id: "ctx-s", datos: { metodo: "create_checkout" } }),
  ];
  let [c] = agruparCompras(evs).compras;
  assert.equal(estadoCompra(c).clave, "para_firmar");
  evs.push(ev("reenviado", { contexto_id: "ctx-s", datos: { metodo: "initiate_payment", mandatos: { "ap2.mandates.CheckoutMandateSdJwt": "a.b.c~", "ap2.mandates.PaymentMandateSdJwt": "d.e.f~" } } }));
  [c] = agruparCompras(evs).compras;
  assert.equal(estadoCompra(c).clave, "firmado");
  assert.deepEqual(mandatosDe(c).map((m) => m.nombre), ["ap2.mandates.CheckoutMandateSdJwt", "ap2.mandates.PaymentMandateSdJwt"]);
});

test("parLadoALado elige la más reciente completa de cada lado", () => {
  const evs = [
    ev("checkout_recibido", { contexto_id: "sin-1", guard_activo: false, datos: { cart_id: "cart_4" } }),
    ev("busqueda_enviada", { contexto_id: "sin-2", guard_activo: false }),
    ev("checkout_recibido", { contexto_id: "con-1", guard_activo: true, datos: { cart_id: "cart_4" } }),
    ev("checkout_recibido", { contexto_id: "otro", escenario: "C", guard_activo: true, datos: {} }),
  ];
  const { compras } = agruparCompras(evs);
  const par = parLadoALado(compras, "B");
  assert.equal(par.sin.contextoId, "sin-1");
  assert.equal(par.con.contextoId, "con-1");
  assert.deepEqual(parLadoALado(compras, "A"), { sin: null, con: null });
});

test("topeDeFicha lee el requisito precio/maximo", () => {
  assert.equal(topeDeFicha({ requisitos: [{ campo: "precio", operador: "maximo", valor: 150 }] }), 150);
  assert.equal(topeDeFicha(null), null);
});

test("partirTextoLibre marca lo que no figura en el catálogo público", () => {
  const publico = ["Zapatilla de test", "Para la calle. No es de básquet."];
  const label = "Zapatilla de test. Para la calle. No es de básquet. [Texto agregado de test.]";
  const tramos = partirTextoLibre(label, publico);
  const extra = tramos.filter((t) => !t.publico).map((t) => t.texto.trim());
  assert.deepEqual(extra, ["[Texto agregado de test.]"]);
  assert.equal(tramos.map((t) => t.texto).join(""), label);
  assert.deepEqual(partirTextoLibre("todo público", ["todo público"]), [{ texto: "todo público", publico: true }]);
});

test("describirEvento resume el checkout con el resultado de AP2", () => {
  const d = describirEvento(ev("checkout_recibido", { datos: { cart_id: "cart_4", jwt_header: { alg: "ES256", kid: "k" }, ap2_violaciones: [] } }));
  assert.match(d.resumen, /AP2: OK/);
  assert.match(describirEvento(ev("checkout_recibido", { datos: { ap2_violaciones: ["tope"] } })).resumen, /AP2 frena: tope/);
});

test("aJsonl: una línea por evento", () => {
  const texto = aJsonl([ev("error"), ev("error")]);
  assert.equal(texto.trim().split("\n").length, 2);
});
