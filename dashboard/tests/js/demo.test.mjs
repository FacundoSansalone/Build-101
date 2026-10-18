// /demo: a quién le habla el input, cómo terminó la corrida y el contraste
// con y sin intermediario. Datos sintéticos de test.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  desenlace, guardarResultado, lineaDeTiempo, modoDelInput, resultadoCorrida, resumenComparacion,
} from "../../static/lib/demo.js";

const AHORA = Date.UTC(2026, 9, 18, 12, 0, 0);
const iso = (ms) => new Date(ms).toISOString();
const compra = (estado, extra = {}) => ({
  compra_id: "cmp-test", estado, creada: iso(AHORA - 5_000), vence: iso(AHORA + 60_000),
  intake: { turno: 0, confirmar: false }, ...extra,
});

let n = 0;
const ev = (tipo, datos = {}, guard_activo = true) =>
  ({ id: `e-${++n}`, ts: iso(AHORA + n * 1000), contexto_id: "ctx", escenario: "notebook_dato_falso", guard_activo, tipo, datos });
const msg = (tipo, extra = {}) => ({ id: `m-${++n}`, ts: iso(AHORA + n * 1000), tipo, ...extra });

// ---------------------------------------------------------------- ruteo del input

test("sin pregunta pendiente, el input le habla al agente de compras", () => {
  const m = modoDelInput({ compras: [], ocupado: false, desde: iso(AHORA - 60_000), ahora: AHORA });
  assert.equal(m.modo, "agente");
  assert.equal(m.enviar, true);
  assert.match(m.etiqueta, /agente/i);
});

test("con el porqué pendiente, el input le contesta a nuestro agente", () => {
  const m = modoDelInput({ compras: [compra("esperando_contexto", { pregunta: "¿Para qué?" })], ocupado: true,
    desde: iso(AHORA - 60_000), ahora: AHORA });
  assert.equal(m.modo, "contexto");
  assert.equal(m.enviar, true); // aunque el agente de compras esté esperando
  assert.equal(m.compraId, "cmp-test");
});

test("confirmar la ficha y decidir una alternativa", () => {
  const base = { ocupado: true, desde: iso(AHORA - 60_000), ahora: AHORA };
  assert.equal(modoDelInput({ ...base, compras: [compra("esperando_contexto", { intake: { turno: 2, confirmar: true } })] }).modo, "confirmar");
  const d = modoDelInput({ ...base, compras: [compra("esperando_decision")] });
  assert.equal(d.modo, "decision");
  assert.match(d.placeholder, /sí|no/i);
});

test("ocupado sin pregunta: se puede escribir pero no enviar", () => {
  const m = modoDelInput({ compras: [compra("negociando")], ocupado: true, desde: null, ahora: AHORA });
  assert.equal(m.modo, "ocupado");
  assert.equal(m.enviar, false);
});

test("no cuentan las preguntas vencidas ni las de una corrida anterior", () => {
  const vencida = compra("esperando_contexto", { vence: iso(AHORA - 1) });
  const vieja = compra("esperando_contexto", { creada: iso(AHORA - 120_000) });
  const base = { ocupado: false, desde: iso(AHORA - 60_000), ahora: AHORA };
  assert.equal(modoDelInput({ ...base, compras: [vencida] }).modo, "agente");
  assert.equal(modoDelInput({ ...base, compras: [vieja] }).modo, "agente");
});

test("en replay el input no envía nada", () => {
  const m = modoDelInput({ compras: [], ocupado: false, desde: null, ahora: AHORA, replay: true });
  assert.equal(m.modo, "replay");
  assert.equal(m.enviar, false);
});

// ---------------------------------------------------------------- línea de tiempo

test("la línea de tiempo arranca en la corrida actual", () => {
  const viejo = { ...ev("ofertas_recibidas"), ts: iso(AHORA - 120_000) };
  const nuevo = ev("ofertas_recibidas");
  const m = msg("usuario", { texto: "hola" });
  const items = lineaDeTiempo([viejo, nuevo], [m], iso(AHORA - 60_000));
  assert.deepEqual(items.map((i) => i.id), [nuevo.id, m.id]);
  assert.equal(lineaDeTiempo([viejo, nuevo], [], null).length, 2);
});

// ---------------------------------------------------------------- desenlace

const OFERTAS = {
  carts: [{ cart_id: "cart_4", amount: 899, currency: "USD" }],
  offer_details: [{ cart_id: "cart_4", sku: "ZT-NB-08", title: "Zento Slim 14", price: 899, currency: "USD",
    atributos: { memoria_ram: { valor: 16, unidad: "GB" } } }],
};
const VEREDICTO = {
  status: "bloqueado", cart_id_elegido: "cart_4", motivo: "Bloqueado: memoria_ram 8 GB no cumple el obligatorio (mínimo 16 GB).",
  chequeos: [{ tipo: "cumplimiento", resultado: "bloquea", campo: "atributo:memoria_ram", esperado: 16,
    observado: { valor: "8", unidad: "GB" }, origen: "fabricante", detalle: "memoria_ram 8 GB no cumple el obligatorio (mínimo 16 GB)." }],
};

test("desenlace de cada corrida", () => {
  // Los eventos del proxy traen contexto_id; los mensajes de la demo, no.
  const items = (...xs) => lineaDeTiempo(xs.filter((x) => "contexto_id" in x), xs.filter((x) => !("contexto_id" in x)), null);
  assert.equal(desenlace(items(ev("ofertas_recibidas", OFERTAS))), null);
  assert.equal(desenlace(items(msg("pago", { cart_id: "cart_4" }))).clave, "pagado");
  assert.equal(desenlace(items(ev("bloqueado", { status: "bloqueado" }))).clave, "frenada");
  assert.equal(desenlace(items(ev("bloqueado", { etapa: "seguridad" }))).clave, "cortada");
  assert.equal(desenlace(items(ev("bloqueado", { requiere_aprobacion: true }))).clave, "retenida");
  assert.equal(desenlace(items(ev("bloqueado", { etapa: "intencion" }))).clave, "vencida");
});

test("resultado de una corrida pagada sin intermediario y de una frenada con intermediario", () => {
  const sinItems = lineaDeTiempo(
    [ev("ofertas_recibidas", OFERTAS, false), ev("checkout_pedido", { cart_id: "cart_4" }, false)],
    [msg("pago", { cart_id: "cart_4" })], null);
  const sin = resultadoCorrida(sinItems, { escenario: "notebook_dato_falso", guardActivo: false });
  assert.deepEqual(sin, {
    escenario: "notebook_dato_falso", guardActivo: false, clave: "pagado",
    producto: "Zento Slim 14", precio: 899, moneda: "USD", hallazgo: null,
  });
  const conItems = lineaDeTiempo(
    [ev("ofertas_recibidas", OFERTAS), ev("checkout_pedido", { cart_id: "cart_4" }),
      ev("veredicto", { veredicto: VEREDICTO }), ev("bloqueado", { status: "bloqueado", cart_id: "cart_4" })],
    [], null);
  const con = resultadoCorrida(conItems, { escenario: "notebook_dato_falso", guardActivo: true });
  assert.equal(con.clave, "frenada");
  assert.equal(con.producto, "Zento Slim 14");
  assert.deepEqual(con.hallazgo, { atributo: "Memoria RAM", real: "8 GB", pedido: "16 GB" });
  assert.equal(resultadoCorrida(lineaDeTiempo([ev("ofertas_recibidas", OFERTAS)], [], null), { escenario: "x", guardActivo: true }), null);
});

test("el contraste lado a lado, en una línea por lado", () => {
  const sin = { escenario: "notebook_dato_falso", guardActivo: false, clave: "pagado", producto: "Zento Slim 14",
    precio: 899, moneda: "USD", hallazgo: null };
  const con = { escenario: "notebook_dato_falso", guardActivo: true, clave: "frenada", producto: "Zento Slim 14",
    precio: 899, moneda: "USD", hallazgo: { atributo: "Memoria RAM", real: "8 GB", pedido: "16 GB" } };
  assert.deepEqual(resumenComparacion(sin, con), {
    sin: "Se compró Zento Slim 14, con 8 GB de memoria RAM (pediste 16 GB)",
    con: "Frenada antes de la firma: no se pagó nada",
  });
  assert.deepEqual(resumenComparacion(sin, null), { sin: "Se compró Zento Slim 14 por USD 899", con: null });
  const cortada = { ...con, clave: "cortada", hallazgo: null };
  assert.equal(resumenComparacion(null, cortada).con, "El filtro cortó la conversación: no se compró nada");
  assert.equal(resumenComparacion({ ...sin, clave: "frenada" }, null).sin, "Frenada antes de la firma: no se pagó nada");
});

test("guardarResultado guarda la última corrida por escenario y lado", () => {
  const r1 = { escenario: "B", guardActivo: false, clave: "pagado" };
  const r2 = { escenario: "B", guardActivo: true, clave: "frenada" };
  const r3 = { escenario: "B", guardActivo: false, clave: "frenada" };
  let mapa = guardarResultado({}, r1);
  mapa = guardarResultado(mapa, r2);
  assert.deepEqual(mapa.B, { sin: r1, con: r2 });
  const nuevo = guardarResultado(mapa, r3);
  assert.deepEqual(nuevo.B, { sin: r3, con: r2 });
  assert.deepEqual(mapa.B.sin, r1); // no muta
  assert.deepEqual(guardarResultado(mapa, null), mapa);
});
