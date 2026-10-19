// Eventos sintéticos de test (no son una compra real).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  agruparCompras, cartIdElegido, describirEvento, escenarioDelMomento, estadoCompra, fichaVigente,
  fusionarEventos, mandatosDe, montoCheckout, parLadoALado, partirTextoLibre, topeDeFicha, aJsonl,
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

test("busqueda_enviada sin contexto se asocia a la compra que empieza después", () => {
  const t0 = Date.UTC(2026, 0, 2, 0, 0, 0);
  const at = (s) => new Date(t0 + s * 1000).toISOString();
  const evs = [
    ev("busqueda_enviada", { contexto_id: null, escenario: "B", ts: at(0), datos: { catalog_search: "búsqueda test 1" } }),
    ev("ofertas_recibidas", { contexto_id: "ctx-a", escenario: "B", ts: at(1) }),
    ev("busqueda_enviada", { contexto_id: null, escenario: "C", ts: at(10), datos: { catalog_search: "búsqueda test 2" } }),
    ev("ofertas_recibidas", { contexto_id: "ctx-b", escenario: "B", ts: at(11) }),
    ev("busqueda_enviada", { contexto_id: null, escenario: "B", ts: at(500) }),
    ev("guard_cambiado", { contexto_id: null, ts: at(501), datos: { guard_activo: false } }),
  ];
  const { compras, sistema } = agruparCompras(evs);
  const porId = Object.fromEntries(compras.map((c) => [c.contextoId, c]));
  assert.equal(porId["ctx-a"].busqueda, "búsqueda test 1");
  assert.equal(porId["ctx-a"].eventos[0].tipo, "busqueda_enviada");
  assert.equal(porId["ctx-b"].busqueda, null); // la de C no es del mismo escenario
  assert.deepEqual(sistema.map((e) => e.tipo), ["busqueda_enviada", "busqueda_enviada", "guard_cambiado"]);
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

test("la ficha que se muestra: el recibo, la del intake o, solo con ficha_demo, la del escenario", () => {
  const recibo = { pedido_original: "p", requisitos: [{ campo: "precio" }], confirmada: true };
  const escenario = { pedido_original: "zapatillas", requisitos: [{ campo: "talle" }], confirmada: true };
  const tope = { campo: "precio", operador: "maximo", valor: 400, obligatorio: true };
  const delIntake = { compra_id: "cmp-1", pedido: "un monitor de 27", requisitos: [tope],
    intake: { final: true, confirmada: true } };
  const conRecibo = { veredicto: { recibo: { ficha: recibo } } };

  // El recibo manda siempre: es la que usó el Guard.
  assert.deepEqual(fichaVigente({ compra: conRecibo, compraAgente: delIntake, fichaDemo: true, fichaEscenario: escenario }),
    { ficha: recibo, fuente: "recibo" });
  // Con PROXY_FICHA_DEMO=0 (por defecto) la del escenario no se muestra: el proxy no la usa.
  const intake = fichaVigente({ compra: {}, compraAgente: delIntake, fichaDemo: false, fichaEscenario: escenario });
  assert.equal(intake.fuente, "intake");
  assert.deepEqual(intake.ficha, { pedido_original: "un monitor de 27", requisitos: [tope],
    criterio: "lowest_price_among_compliant", confirmada: true });
  assert.deepEqual(fichaVigente({ compra: {}, fichaDemo: false, fichaEscenario: escenario }), { ficha: null, fuente: null });
  assert.deepEqual(fichaVigente({ fichaEscenario: escenario }), { ficha: null, fuente: null }); // sin dato del proxy
  // Con PROXY_FICHA_DEMO=1, la del escenario manda sobre la del intake (como en el proxy).
  assert.deepEqual(fichaVigente({ compra: {}, compraAgente: delIntake, fichaDemo: true, fichaEscenario: escenario }),
    { ficha: escenario, fuente: "escenario" });
  // Sin confirmar en el intake: se muestra como sin confirmar.
  const sinConfirmar = fichaVigente({ compraAgente: { ...delIntake, intake: { final: false } } });
  assert.equal(sinConfirmar.ficha.confirmada, false);
});

test("el momento clave sigue a la compra más nueva salvo que se elija un escenario", () => {
  const evs = [
    ev("busqueda_enviada", { contexto_id: "ctx-viejo", escenario: "B" }),
    ev("busqueda_enviada", { contexto_id: "ctx-nuevo", escenario: "general_dato_falso" }),
  ];
  const { compras } = agruparCompras(evs);
  assert.equal(escenarioDelMomento({ compras, porDefecto: "baseline" }), "general_dato_falso");
  assert.equal(escenarioDelMomento({ elegido: "B", compras, porDefecto: "baseline" }), "B");
  assert.equal(escenarioDelMomento({ compras: [], porDefecto: "baseline" }), "baseline");
  assert.equal(escenarioDelMomento({ compras: [{ escenario: null }], porDefecto: "baseline" }), "baseline");
});

test("una búsqueda sin resultados termina la compra (no queda en curso)", () => {
  const vacio = { carts: [], offer_details: [] };
  const evs = [
    ev("busqueda_enviada", { contexto_id: "ctx-v", datos: { catalog_search: "un monitor" } }),
    ev("ofertas_recibidas", { contexto_id: "ctx-v", datos: vacio }),
    ev("reenviado", { contexto_id: "ctx-v", datos: { metodo: "find_products" } }),
  ];
  let [c] = agruparCompras(evs).compras;
  assert.deepEqual(estadoCompra(c), { clave: "sin_resultados", texto: "Sin resultados: el comercio no tiene nada que cumpla el pedido" });
  assert.equal(describirEvento(evs[1]).resumen, "Sin resultados: 0 carritos (ap2.cart)");
  // Si después pide el checkout de una oferta de antes, sigue.
  evs.push(ev("checkout_pedido", { contexto_id: "ctx-v", datos: { cart_id: "cart_1" } }));
  [c] = agruparCompras(evs).compras;
  assert.equal(estadoCompra(c).clave, "en_curso");
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
