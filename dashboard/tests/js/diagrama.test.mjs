// Máquina de estados del diagrama de /demo: qué mensaje viaja por qué flecha
// y de qué color se pinta el Guard según los eventos reales. Datos de test.
import { test } from "node:test";
import assert from "node:assert/strict";

import { arista, estadoInicial, geometria, NODOS, paso, recorrer } from "../../static/lib/diagrama.js";

let n = 0;
const ts = () => new Date(Date.UTC(2026, 9, 18, 12, 0, ++n)).toISOString();
const ev = (tipo, datos = {}, guard_activo = true) =>
  ({ id: `e-${++n}`, ts: ts(), origen: "proxy", tipo, datos, guard_activo, contexto_id: "ctx" });
const msg = (tipo, extra = {}) => ({ id: `m-${++n}`, ts: ts(), origen: "demo", tipo, ...extra });
const flujos = (r) => r.flujos.map((f) => `${f.de}>${f.a}`);

test("estado inicial: el Guard en la línea si está activo, atenuado si no", () => {
  const on = estadoInicial(true);
  assert.equal(on.guard, "inactivo");
  assert.deepEqual(on.etapas, { filtro: "inactivo", intencion: "inactivo", verificacion: "inactivo" });
  assert.equal(estadoInicial(false).guard, "apagado");
});

test("con el Guard prendido, la búsqueda pasa por él y el filtro revisa", () => {
  let e = estadoInicial(true);
  let r = paso(e, msg("usuario", { texto: "notebook", destino: "agente" }));
  assert.deepEqual(flujos(r), ["usuario>agente"]);
  r = paso(r.estado, msg("herramienta", { nombre: "find_products", args: {} }));
  assert.deepEqual(flujos(r), ["agente>guard"]);
  r = paso(r.estado, ev("busqueda_enviada", { catalog_search: "x" }));
  assert.deepEqual(flujos(r), ["guard>comercio"]);
  r = paso(r.estado, ev("ofertas_recibidas", { carts: [] }));
  assert.deepEqual(flujos(r), ["comercio>guard"]);
  assert.equal(r.estado.etapas.filtro, "trabajando");
  assert.equal(r.estado.guard, "trabajando");
  r = paso(r.estado, ev("reenviado", { metodo: "find_products" }));
  assert.deepEqual(flujos(r), ["guard>agente"]);
  assert.equal(r.estado.etapas.filtro, "ok");
  assert.equal(r.estado.guard, "ok");
  e = r.estado;
  r = paso(e, msg("agente", { texto: "opciones", oculto: false }));
  assert.deepEqual(flujos(r), ["agente>usuario"]);
});

test("la respuesta del comercio cierra la etapa de intención aunque el «sí» llegue después del evento", () => {
  // Orden real (/demo): el proxy registra la confirmación y el mensaje del
  // usuario queda con una hora apenas posterior. La búsqueda vuelve recién
  // cuando nuestro agente terminó de preguntar: al terminar, no "revisa".
  let r = paso(estadoInicial(true), ev("pedido_detectado", { compra_id: "c", pregunta: "¿Confirmás?" }));
  r = paso(r.estado, ev("contexto_registrado", { compra_id: "c", final: true, confirmada: true }));
  r = paso(r.estado, msg("usuario", { texto: "sí", destino: "confirmar" }));
  assert.equal(r.estado.etapas.intencion, "trabajando");
  r = paso(r.estado, ev("ofertas_recibidas", { carts: [], offer_details: [] }));
  assert.equal(r.estado.etapas.intencion, "ok");
  r = paso(r.estado, ev("reenviado", { metodo: "find_products" }));
  assert.equal(r.estado.guard, "ok");
});

test("sin herramienta previa (replay), la búsqueda dibuja los dos tramos", () => {
  const r = paso(estadoInicial(true), ev("busqueda_enviada", { catalog_search: "x" }));
  assert.deepEqual(flujos(r), ["agente>guard", "guard>comercio"]);
});

test("nuestro agente le pregunta al usuario directo, sin pasar por el agente de compras", () => {
  let r = paso(estadoInicial(true), ev("pedido_detectado", { compra_id: "c", pregunta: "¿para qué?" }));
  assert.deepEqual(flujos(r), ["guard>usuario"]);
  assert.equal(r.estado.etapas.intencion, "trabajando");
  r = paso(r.estado, msg("usuario", { texto: "para programar", destino: "contexto" }));
  assert.deepEqual(flujos(r), ["usuario>guard"]);
  r = paso(r.estado, ev("contexto_registrado", { compra_id: "c", contexto: { necesidad: "x" } }));
  assert.equal(r.estado.etapas.intencion, "ok");
  r = paso(r.estado, ev("oportunidad", { compra_id: "c", oportunidad: {} }));
  assert.deepEqual(flujos(r), ["guard>usuario"]);
  assert.equal(r.estado.etapas.intencion, "aviso");
  assert.equal(r.estado.guard, "aviso");
});

test("corte del filtro: el Guard en rojo y al agente le llega el rechazo", () => {
  const r = paso(estadoInicial(true), ev("bloqueado", { etapa: "seguridad", senales: ["x"] }));
  assert.deepEqual(flujos(r), ["guard>agente"]);
  assert.equal(r.flujos[0].tono, "crit");
  assert.equal(r.estado.etapas.filtro, "bloqueado");
  assert.equal(r.estado.guard, "bloqueado");
});

test("verificación antes de la firma: consulta las fuentes y decide", () => {
  let r = paso(estadoInicial(true), msg("herramienta", { nombre: "create_checkout", args: {} }));
  assert.deepEqual(flujos(r), ["agente>guard"]);
  r = paso(r.estado, ev("checkout_pedido", { cart_id: "cart_4" }));
  assert.deepEqual(flujos(r), ["guard>comercio"]);
  r = paso(r.estado, ev("checkout_recibido", { cart_id: "cart_4" }));
  assert.deepEqual(flujos(r), ["comercio>guard"]);
  assert.equal(r.estado.etapas.verificacion, "trabajando");
  r = paso(r.estado, ev("veredicto", { veredicto: { status: "bloqueado" } }));
  assert.deepEqual(flujos(r), ["guard>fuentes", "fuentes>guard"]);
  assert.equal(r.estado.etapas.verificacion, "bloqueado");
  assert.equal(r.estado.nodos.fuentes, "trabajando");
  r = paso(r.estado, ev("bloqueado", { cart_id: "cart_4", status: "bloqueado" }));
  assert.deepEqual(flujos(r), ["guard>agente"]);
  assert.equal(r.estado.guard, "bloqueado");

  const ok = recorrer([ev("veredicto", { veredicto: { status: "aprobado" } }), ev("reenviado", { metodo: "create_checkout" })], true);
  assert.equal(ok.estado.etapas.verificacion, "ok");
  assert.equal(ok.estado.guard, "ok");
  const escala = recorrer([ev("veredicto", { veredicto: { status: "escalar" } })], true);
  assert.equal(escala.estado.guard, "aviso");
});

test("con el Guard apagado los mensajes van directo agente ↔ comercio", () => {
  const items = [
    ev("guard_cambiado", { guard_activo: false }, false),
    msg("herramienta", { nombre: "find_products", args: {} }),
    ev("busqueda_enviada", { catalog_search: "x" }, false),
    ev("ofertas_recibidas", { carts: [] }, false),
    ev("reenviado", { metodo: "find_products" }, false),
    ev("checkout_pedido", { cart_id: "c" }, false),
    ev("checkout_recibido", { cart_id: "c" }, false),
    ev("reenviado", { metodo: "create_checkout" }, false),
  ];
  const r = recorrer(items, true);
  assert.equal(r.estado.guard, "apagado");
  assert.equal(r.estado.guardActivo, false);
  assert.deepEqual(r.flujos.map((f) => `${f.de}>${f.a}`), [
    "agente>comercio", "comercio>agente", "agente>comercio", "comercio>agente",
  ]);
  assert.ok(r.flujos.every((f) => f.de !== "guard" && f.a !== "guard"));
});

test("el pago va por el comercio a AP2 y el recibo vuelve por el mismo camino", () => {
  const r = recorrer([
    ev("reenviado", { metodo: "otro", mandatos: { a: "x" } }),
    msg("pago", { cart_id: "cart_1" }),
  ], true);
  assert.deepEqual(r.flujos.map((f) => `${f.de}>${f.a}`), [
    "agente>guard", "guard>comercio", "comercio>ap2", "ap2>comercio", "comercio>guard", "guard>agente",
  ]);
  assert.equal(r.estado.nodos.ap2, "ok");
  const sin = recorrer([ev("reenviado", { metodo: "otro", mandatos: { a: "x" } }, false)], false);
  assert.deepEqual(sin.flujos.map((f) => `${f.de}>${f.a}`), ["agente>comercio", "comercio>ap2"]);
});

test("los pasos automáticos viajan tenues; lo oculto no viaja", () => {
  const r = recorrer([msg("auto", { paso: "tarjeta" }), msg("agente", { oculto: true })], true);
  assert.deepEqual(r.flujos.map((f) => `${f.de}>${f.a}:${f.tono}`), ["usuario>agente:tenue"]);
});

test("cada flujo usa una arista dibujada (en un sentido o en el otro)", () => {
  for (const orientacion of ["horizontal", "vertical", "franja"]) {
    const g = geometria(orientacion);
    assert.deepEqual(Object.keys(g.nodos).sort(), [...NODOS].sort());
    for (const id of NODOS) {
      const nodo = g.nodos[id];
      assert.ok(nodo.x >= 0 && nodo.y >= 0 && nodo.x <= g.ancho && nodo.y <= g.alto, `${orientacion} ${id}`);
      assert.ok(typeof nodo.etiqueta === "string" && nodo.etiqueta.length > 0);
    }
    for (const [de, a] of [["usuario", "agente"], ["agente", "guard"], ["guard", "comercio"], ["agente", "comercio"],
      ["guard", "usuario"], ["guard", "fuentes"], ["comercio", "ap2"]]) {
      const ida = arista(de, a);
      const vuelta = arista(a, de);
      assert.ok(g.aristas[ida.clave], `${orientacion} ${de}-${a}`);
      assert.equal(ida.clave, vuelta.clave);
      assert.notEqual(ida.invertida, vuelta.invertida);
    }
    assert.ok(g.apagado.dx !== 0 || g.apagado.dy !== 0, "el Guard apagado se corre de la línea");
    // Corrido, no tapa el atajo agente ↔ comercio.
    const gd = g.nodos.guard;
    const corrido = { x: gd.x + g.apagado.dx, y: gd.y + g.apagado.dy };
    const ag = g.nodos.agente;
    const co = g.nodos.comercio;
    if (orientacion !== "vertical") assert.ok(corrido.y - gd.h / 2 > Math.max(ag.y, co.y), "queda debajo de la línea");
    else assert.ok(corrido.x + gd.w / 2 < Math.min(ag.x, co.x), "queda al costado de la línea");
  }
});
