// Ronda de revisión: escalar retenido, aprobación humana, atadura cart ↔
// checkout firmado, fuente del catálogo y busqueda_enviada con contexto.
// Eventos sintéticos de test (no son una compra real).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  agruparCompras, chequeosCheckoutFirmado, cuerpoAprobacion, describirEvento,
  estadoAprobacion, estadoCompra,
} from "../../static/lib/eventos.js";

let n = 0;
const ev = (tipo, extra = {}) => ({
  id: `apr-${++n}`,
  ts: new Date(Date.UTC(2026, 0, 3, 0, 0, n)).toISOString(),
  contexto_id: "ctx-c",
  escenario: "C",
  guard_activo: true,
  tipo,
  datos: {},
  ...extra,
});

const VEREDICTO_ESCALAR = {
  status: "escalar",
  motivo: "precio contradicho",
  cart_id_elegido: "cart_1",
  chequeos: [
    { tipo: "evidencia", resultado: "escalar", detalle: "precio ≠ catálogo", origen: "catalogo_publico" },
    { tipo: "mandato", resultado: "ok", detalle: "hash del checkout = el declarado", origen: "checkout_firmado" },
    { tipo: "mandato", resultado: "ok", detalle: "cart del JWT = el evaluado", origen: "checkout_firmado" },
  ],
};

function compraRetenida() {
  return [
    ev("checkout_pedido", { datos: { cart_id: "cart_1" } }),
    ev("checkout_recibido", { datos: { cart_id: "cart_1", ap2_violaciones: [] } }),
    ev("veredicto", { datos: { veredicto: VEREDICTO_ESCALAR, fuente: "http", recibo_firmado: true } }),
    ev("bloqueado", { datos: { cart_id: "cart_1", motivo: "precio contradicho", status: "escalar", requiere_aprobacion: true } }),
  ];
}

test("escalar retenido: estado 'retenido' y no 'bloqueado'", () => {
  const [c] = agruparCompras(compraRetenida()).compras;
  const est = estadoCompra(c);
  assert.equal(est.clave, "retenido");
  assert.match(est.texto, /retenido/i);
  assert.match(est.texto, /decisión humana/i);
  assert.equal(estadoAprobacion(c, { pendientes: [], usadas: [] }).clave, "retenido");
});

test("bloqueado sin requiere_aprobacion sigue siendo 'bloqueado'", () => {
  const evs = compraRetenida();
  evs[3] = ev("bloqueado", { datos: { cart_id: "cart_1", motivo: "m", status: "bloqueado", requiere_aprobacion: false } });
  const [c] = agruparCompras(evs).compras;
  assert.equal(estadoCompra(c).clave, "bloqueado");
  assert.equal(estadoAprobacion(c, { pendientes: [], usadas: [] }).clave, null);
});

test("aprobación pendiente: 'pedile al agente que reintente'", () => {
  const [c] = agruparCompras(compraRetenida()).compras;
  const aprobaciones = { pendientes: [{ id: "apr-x", contexto_id: "ctx-c", cart_id: "cart_1", estado: "pendiente" }], usadas: [] };
  const est = estadoAprobacion(c, aprobaciones);
  assert.equal(est.clave, "aprobada");
  assert.equal(est.aprobacion.id, "apr-x");
  assert.match(est.texto, /pedile al agente que reintente el checkout/);
  // Una aprobación de otro contexto o cart no cuenta.
  const otra = { pendientes: [{ id: "y", contexto_id: "ctx-otro", cart_id: "cart_1" }], usadas: [] };
  assert.equal(estadoAprobacion(c, otra).clave, "retenido");
});

test("reintento aprobado por humano: el último desenlace manda", () => {
  const evs = [
    ...compraRetenida(),
    ev("checkout_pedido", { datos: { cart_id: "cart_1" } }),
    ev("checkout_recibido", { datos: { cart_id: "cart_1", ap2_violaciones: [] } }),
    ev("veredicto", { datos: { veredicto: VEREDICTO_ESCALAR } }),
    ev("reenviado", { datos: { metodo: "create_checkout", aprobacion_humana: true, aprobacion_id: "apr-x" } }),
  ];
  const [c] = agruparCompras(evs).compras;
  assert.equal(estadoCompra(c).clave, "aprobado_humano");
  assert.match(estadoCompra(c).texto, /aprobado por humano/i);
  assert.equal(estadoAprobacion(c, { pendientes: [], usadas: [] }).clave, "aprobado_humano");
  // Y si después firma, gana "firmado".
  evs.push(ev("reenviado", { datos: { metodo: "otro", mandatos: { "ap2.mandates.CheckoutMandateSdJwt": "a.b.c~" } } }));
  assert.equal(estadoCompra(agruparCompras(evs).compras[0]).clave, "firmado");
});

test("cuerpoAprobacion usa el contexto de la compra y el cart retenido", () => {
  const [c] = agruparCompras(compraRetenida()).compras;
  assert.deepEqual(cuerpoAprobacion(c), { contexto_id: "ctx-c", cart_id: "cart_1" });
  const [b] = agruparCompras([ev("busqueda_enviada", { contexto_id: "ctx-z" })]).compras;
  assert.equal(cuerpoAprobacion(b), null);
});

test("chequeos con origen checkout_firmado (atadura cart ↔ checkout)", () => {
  const ligados = chequeosCheckoutFirmado(VEREDICTO_ESCALAR);
  assert.equal(ligados.length, 2);
  assert.ok(ligados.every((q) => q.origen === "checkout_firmado"));
  assert.deepEqual(chequeosCheckoutFirmado(null), []);
});

test("la compra trae la fuente del catálogo y si el recibo quedó firmado", () => {
  const [c] = agruparCompras(compraRetenida()).compras;
  assert.equal(c.fuenteCatalogo, "http");
  assert.equal(c.reciboFirmado, true);
  const [s] = agruparCompras([ev("veredicto", { contexto_id: "otra", datos: { veredicto: VEREDICTO_ESCALAR } })]).compras;
  assert.equal(s.fuenteCatalogo, null);
  assert.equal(s.reciboFirmado, null);
});

test("sin catálogo público: fuente 'ninguno', comercio firmado y catálogo descartado", () => {
  const datos = {
    veredicto: VEREDICTO_ESCALAR, fuente: "ninguno", comercio_id: "merchant_1",
    catalogo_descartado: "el catálogo es del comercio 'otro'",
  };
  const [c] = agruparCompras([ev("veredicto", { contexto_id: "sin-cat", datos })]).compras;
  assert.equal(c.fuenteCatalogo, "ninguno");
  assert.equal(c.comercioFirmado, "merchant_1");
  assert.match(c.catalogoDescartado, /otro/);
  const [s] = agruparCompras([ev("veredicto", { contexto_id: "x", datos: { veredicto: VEREDICTO_ESCALAR } })]).compras;
  assert.equal(s.comercioFirmado, null);
  assert.equal(s.catalogoDescartado, null);
});

test("describirEvento distingue retenido y aprobado por humano", () => {
  const retenido = describirEvento(ev("bloqueado", { datos: { motivo: "m", status: "escalar", requiere_aprobacion: true } }));
  assert.match(retenido.titulo, /retenido/i);
  const aprobado = describirEvento(ev("reenviado", { datos: { metodo: "create_checkout", aprobacion_humana: true } }));
  assert.match(aprobado.resumen, /aprobado por humano/i);
  const veredicto = describirEvento(ev("veredicto", { datos: { veredicto: VEREDICTO_ESCALAR, fuente: "fixture", recibo_firmado: false } }));
  assert.match(veredicto.resumen, /fixture/);
});

test("busqueda_enviada con contexto_id no usa la ventana de 60 s", () => {
  const t0 = Date.UTC(2026, 0, 4, 0, 0, 0);
  const at = (s) => new Date(t0 + s * 1000).toISOString();
  const evs = [
    // Un registro viejo sin contexto: sigue asociándose por tiempo, pero no
    // a una compra que ya tiene su búsqueda.
    ev("busqueda_enviada", { contexto_id: null, escenario: "B", ts: at(0), datos: { catalog_search: "sin contexto" } }),
    // La del proxy nuevo: ya trae su contexto.
    ev("busqueda_enviada", { contexto_id: "ctx-a", escenario: "B", ts: at(1), datos: { catalog_search: "con contexto" } }),
    ev("ofertas_recibidas", { contexto_id: "ctx-a", escenario: "B", ts: at(2) }),
    ev("ofertas_recibidas", { contexto_id: "ctx-b", escenario: "B", ts: at(3) }),
  ];
  const { compras } = agruparCompras(evs);
  const porId = Object.fromEntries(compras.map((c) => [c.contextoId, c]));
  assert.equal(porId["ctx-a"].busqueda, "con contexto");
  assert.equal(porId["ctx-a"].eventos.filter((e) => e.tipo === "busqueda_enviada").length, 1);
  assert.equal(porId["ctx-b"].busqueda, "sin contexto");
});
