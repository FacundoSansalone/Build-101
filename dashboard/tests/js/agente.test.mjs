// Nuestro agente (etapa 2): compras armadas con los eventos y con
// GET /guard/compras, cuerpos de los POST, formato de entregas y plazos.
// Eventos sintéticos de test (no son una compra real).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ESPERAN_AL_USUARIO, FINALES, MAX_RESPUESTA, TEXTO_FINAL, compraElegida, comprasDesdeEventos,
  cuerpoContexto, cuerpoDecision, cuerpoOmitir, desenlaceContexto, describirRequisito,
  esperaAlUsuario, formatearEntrega, formatearEntregas, formatearEntregasAcumuladas, formatearFecha,
  formatearValor,
  fusionarCompras, mensajeErrorAgente, reemplazarCompra, restante,
} from "../../static/lib/agente.js";
import { agruparCompras, describirEvento, estadoCompra, fusionarEventos } from "../../static/lib/eventos.js";

const T0 = Date.UTC(2026, 9, 18, 12, 0, 0);
const iso = (s) => new Date(T0 + s * 1000).toISOString();

let n = 0;
const ev = (tipo, datos, extra = {}) => ({
  id: `ag-${++n}`,
  ts: iso(n),
  contexto_id: null,
  escenario: null,
  guard_activo: true,
  tipo,
  datos,
  ...extra,
});

const REQUISITOS = [
  { campo: "cantidad", operador: "igual", valor: 20, obligatorio: true },
  { campo: "entregas", operador: "cubre", valor: [{ cantidad: 20, fecha: "2026-10-19" }], obligatorio: true },
];
const CONTEXTO = {
  pedido: "pedido de test",
  pregunta: "pregunta de test",
  respuesta: "respuesta de test",
  necesidad: "necesidad de test",
  necesidad_entregas: [{ cantidad: 10, fecha: "2026-10-19" }, { cantidad: 20, fecha: "2026-10-22" }],
  flexibilidades: [
    { descripcion: "flexibilidad dicha", origen: "dicha" },
    { descripcion: "flexibilidad inferida", origen: "inferida" },
  ],
};
const OPORTUNIDAD = {
  id: "op-test",
  cart_id: "cart_test",
  sku: "SKU-TEST",
  titulo: "título de test",
  precio: 1000,
  moneda: "USD",
  cambios: [{
    campo: "entregas",
    pedido: [{ cantidad: 20, fecha: "2026-10-19" }],
    ofrecido: [{ cantidad: 10, fecha: "2026-10-19" }, { cantidad: 10, fecha: "2026-10-21" }],
  }],
  por_que: "por qué de test",
  requisitos_propuestos: [
    { campo: "entregas", operador: "cubre", valor: [{ cantidad: 10, fecha: "2026-10-19" }, { cantidad: 20, fecha: "2026-10-21" }], obligatorio: true },
  ],
};

function eventosCompra(id = "cmp-a", { decision = null } = {}) {
  const evs = [
    ev("pedido_detectado", { compra_id: id, pedido: "pedido de test", pregunta: "pregunta de test", requisitos: REQUISITOS, vence: iso(300) }),
    ev("contexto_registrado", { compra_id: id, omitido: false, vencido: false, contexto: CONTEXTO }, { contexto_id: "ctx-test" }),
    ev("oportunidad", { compra_id: id, oportunidad: OPORTUNIDAD, vence: iso(900) }, { contexto_id: "ctx-test" }),
  ];
  if (decision) {
    evs.push(ev("oportunidad_decidida", {
      compra_id: id, oportunidad_id: "op-test", decision,
      requisitos_autorizados: decision === "aceptada" ? OPORTUNIDAD.requisitos_propuestos : null,
    }, { contexto_id: "ctx-test" }));
  }
  return evs;
}

// ---------------------------------------------------------------------------
// Compras desde los eventos
// ---------------------------------------------------------------------------

test("cada evento avanza la compra: pregunta → negociando → decisión", () => {
  const evs = eventosCompra("cmp-a");
  let [c] = comprasDesdeEventos(evs.slice(0, 1));
  assert.equal(c.compra_id, "cmp-a");
  assert.equal(c.estado, "esperando_contexto");
  assert.equal(c.pedido, "pedido de test");
  assert.equal(c.pregunta, "pregunta de test");
  assert.deepEqual(c.requisitos, REQUISITOS);
  assert.equal(c.vence, iso(300));
  assert.equal(c.creada, evs[0].ts);

  [c] = comprasDesdeEventos(evs.slice(0, 2));
  assert.equal(c.estado, "negociando");
  assert.equal(c.vence, null);
  assert.equal(c.contexto.necesidad, "necesidad de test");
  assert.equal(c.contexto_id, "ctx-test");
  assert.equal(desenlaceContexto(c), "respondido");

  [c] = comprasDesdeEventos(evs);
  assert.equal(c.estado, "esperando_decision");
  assert.equal(c.oportunidad.id, "op-test");
  assert.equal(c.vence, iso(900));
});

test("oportunidad_decidida: aceptada, rechazada o vencida, sin plazo", () => {
  for (const decision of ["aceptada", "rechazada", "vencida"]) {
    const [c] = comprasDesdeEventos(eventosCompra(`cmp-${decision}`, { decision }));
    assert.equal(c.estado, decision);
    assert.equal(c.decision, decision);
    assert.equal(c.vence, null);
    assert.ok(TEXTO_FINAL[decision]);
  }
  const [aceptada] = comprasDesdeEventos(eventosCompra("cmp-x", { decision: "aceptada" }));
  assert.deepEqual(aceptada.requisitos_autorizados, OPORTUNIDAD.requisitos_propuestos);
  assert.match(TEXTO_FINAL.vencida, /El silencio no es autorización/);
});

test("contexto omitido o vencido: sigue el pedido literal", () => {
  const base = ev("pedido_detectado", { compra_id: "cmp-o", pedido: "p", pregunta: "q", requisitos: [], vence: iso(60) });
  const omitido = ev("contexto_registrado", { compra_id: "cmp-o", omitido: true, vencido: false, contexto: null });
  const [o] = comprasDesdeEventos([base, omitido]);
  assert.equal(o.estado, "negociando");
  assert.equal(desenlaceContexto(o), "omitido");

  const vencido = ev("contexto_registrado", { compra_id: "cmp-o", omitido: false, vencido: true, contexto: { ...CONTEXTO, respuesta: null } });
  const [v] = comprasDesdeEventos([base, vencido]);
  assert.equal(desenlaceContexto(v), "vencido");
  assert.equal(desenlaceContexto(comprasDesdeEventos([base])[0]), null);
});

test("el proxy ya tomó la respuesta pero el contexto no llegó: 'armando', no 'sin respuesta'", () => {
  // POST /guard/compras/contexto pasa la compra a negociando enseguida; el
  // contexto_registrado llega cuando nuestro agente termina de armarlo.
  const pedido = ev("pedido_detectado", { compra_id: "cmp-r", pedido: "p", pregunta: "q", requisitos: [], vence: iso(60) });
  const [c] = fusionarCompras(comprasDesdeEventos([pedido]), [compraProxy({ compra_id: "cmp-r", contexto: null })]);
  assert.equal(c.estado, "negociando");
  assert.equal(desenlaceContexto(c), "armando");
  // Llegó contexto_registrado sin contexto (el agente falló): sigue el pedido literal.
  const fallo = ev("contexto_registrado", { compra_id: "cmp-r", omitido: false, vencido: false, contexto: null });
  const [d] = comprasDesdeEventos([pedido, fallo]);
  assert.equal(desenlaceContexto(d), "sin_contexto");
  assert.match(describirEvento(fallo).resumen, /no pudo armar el contexto/);
  // Solo el sondeo, ya en otro estado y sin datos: no se inventa.
  const [s] = fusionarCompras([], [compraProxy({ compra_id: "cmp-s", estado: "sin_oportunidad", contexto: null })]);
  assert.equal(desenlaceContexto(s), "sin_respuesta");
});

test("bloqueada (filtro de seguridad) es final y solo la trae el proxy", () => {
  const evs = eventosCompra("cmp-a");
  const [c] = fusionarCompras(comprasDesdeEventos(evs.slice(0, 2)), [compraProxy({ estado: "bloqueada" })]);
  assert.equal(c.estado, "bloqueada");
  assert.ok(FINALES.has("bloqueada"));
  assert.match(TEXTO_FINAL.bloqueada, /^Bloqueado por seguridad: no se buscan alternativas/);
  assert.equal(esperaAlUsuario(c, T0), false);
});

test("agrupa por compra_id (no por contexto_id) y la más nueva va primero", () => {
  const evs = [
    ev("pedido_detectado", { compra_id: "cmp-vieja", pedido: "a", pregunta: "q", requisitos: [], vence: null }),
    ev("pedido_detectado", { compra_id: "cmp-nueva", pedido: "b", pregunta: "q", requisitos: [], vence: null }),
    ev("contexto_registrado", { compra_id: "cmp-vieja", omitido: true, vencido: false, contexto: null }),
    // Eventos de otros tipos o sin compra_id no arman compras.
    ev("busqueda_enviada", { compra_id: "cmp-otra" }),
    ev("oportunidad", { oportunidad: OPORTUNIDAD }),
  ];
  const compras = comprasDesdeEventos(evs);
  assert.deepEqual(compras.map((c) => c.compra_id), ["cmp-nueva", "cmp-vieja"]);
  assert.equal(compras[1].estado, "negociando");
});

// ---------------------------------------------------------------------------
// Eventos + GET /guard/compras
// ---------------------------------------------------------------------------

const compraProxy = (extra) => ({
  compra_id: "cmp-a", estado: "negociando", creada: iso(1), contexto_id: "ctx-test", pedido: "pedido de test",
  pregunta: "pregunta de test", requisitos: REQUISITOS, contexto: CONTEXTO, oportunidad: null, vence: null,
  decision: null, requisitos_autorizados: null, ...extra,
});

test("el proxy manda, salvo que los eventos estén más avanzados", () => {
  const evs = eventosCompra("cmp-a");
  // El SSE ya trajo la oportunidad; el sondeo de hace 4 s todavía dice negociando.
  let [c] = fusionarCompras(comprasDesdeEventos(evs), [compraProxy()]);
  assert.equal(c.estado, "esperando_decision");
  assert.equal(c.oportunidad.id, "op-test");
  // El proxy ya la cerró (sin evento todavía): gana el proxy.
  [c] = fusionarCompras(comprasDesdeEventos(evs), [compraProxy({ estado: "rechazada", oportunidad: OPORTUNIDAD, decision: "rechazada" })]);
  assert.equal(c.estado, "rechazada");
  // Lo que solo traen los eventos (omitido/vencido) se conserva.
  const omitida = [
    ev("pedido_detectado", { compra_id: "cmp-b", pedido: "p", pregunta: "q", requisitos: [], vence: null }),
    ev("contexto_registrado", { compra_id: "cmp-b", omitido: true, vencido: false, contexto: null }),
  ];
  [c] = fusionarCompras(comprasDesdeEventos(omitida), [compraProxy({ compra_id: "cmp-b", estado: "sin_oportunidad", contexto: null })]);
  assert.equal(c.estado, "sin_oportunidad");
  assert.equal(desenlaceContexto(c), "omitido");
});

test("sin_oportunidad solo lo trae el proxy; una compra solo del proxy también se muestra", () => {
  const [c] = fusionarCompras([], [compraProxy({ estado: "sin_oportunidad" })]);
  assert.equal(c.estado, "sin_oportunidad");
  assert.equal(c.pedido, "pedido de test");
  assert.equal(desenlaceContexto(c), "respondido");
  // Basura del proxy no rompe nada.
  assert.deepEqual(fusionarCompras([], [null, { compra_id: "" }, { compra_id: 3 }, "x"]), []);
});

test("si el pedido_detectado quedó fuera del historial, el proxy completa el pedido", () => {
  const soloOportunidad = [ev("oportunidad", { compra_id: "cmp-a", oportunidad: OPORTUNIDAD, vence: iso(900) })];
  const [c] = fusionarCompras(comprasDesdeEventos(soloOportunidad), [compraProxy()]);
  assert.equal(c.estado, "esperando_decision");
  assert.equal(c.pedido, "pedido de test");
  assert.deepEqual(c.requisitos, REQUISITOS);
});

test("reemplazarCompra pone la que devolvió el POST sin mutar la lista", () => {
  const lista = [compraProxy({ compra_id: "cmp-1" }), compraProxy({ compra_id: "cmp-2" })];
  const nueva = compraProxy({ compra_id: "cmp-2", estado: "aceptada" });
  const out = reemplazarCompra(lista, nueva);
  assert.deepEqual(out.map((c) => [c.compra_id, c.estado]), [["cmp-1", "negociando"], ["cmp-2", "aceptada"]]);
  assert.equal(lista[1].estado, "negociando");
  assert.equal(reemplazarCompra(lista, compraProxy({ compra_id: "cmp-3" }))[0].compra_id, "cmp-3");
  assert.equal(reemplazarCompra(lista, { error: "x" }), lista);
});

test("compraElegida: la elegida o la más nueva", () => {
  const compras = comprasDesdeEventos([...eventosCompra("cmp-1"), ...eventosCompra("cmp-2")]);
  assert.equal(compraElegida(compras, null).compra_id, "cmp-2");
  assert.equal(compraElegida(compras, "cmp-1").compra_id, "cmp-1");
  assert.equal(compraElegida(compras, "cmp-borrada").compra_id, "cmp-2");
  assert.equal(compraElegida([], null), null);
});

test("esperaAlUsuario: solo si espera algo y el plazo no venció", () => {
  const [c] = comprasDesdeEventos(eventosCompra("cmp-a"));
  assert.ok(ESPERAN_AL_USUARIO.has(c.estado));
  assert.equal(esperaAlUsuario(c, T0), true);
  assert.equal(esperaAlUsuario(c, T0 + 901_000), false);
  assert.equal(esperaAlUsuario({ ...c, vence: null }, T0), true);
  assert.equal(esperaAlUsuario({ ...c, estado: "negociando" }, T0), false);
});

// ---------------------------------------------------------------------------
// Cuerpos de los POST (los mismos que valida el servidor)
// ---------------------------------------------------------------------------

test("cuerpoContexto recorta espacios y respeta 1..1000", () => {
  assert.deepEqual(cuerpoContexto("cmp-1", "  hola \n"), { compra_id: "cmp-1", respuesta: "hola" });
  assert.equal(cuerpoContexto("cmp-1", "   "), null);
  assert.equal(cuerpoContexto("cmp-1", ""), null);
  assert.equal(cuerpoContexto("cmp-1", undefined), null);
  assert.equal(cuerpoContexto("cmp-1", "x".repeat(MAX_RESPUESTA + 1)), null);
  assert.equal(cuerpoContexto("cmp-1", ` ${"x".repeat(MAX_RESPUESTA)} `).respuesta.length, MAX_RESPUESTA);
  assert.equal(cuerpoContexto("", "hola"), null);
  assert.equal(cuerpoContexto("c".repeat(201), "hola"), null);
});

test("cuerpoOmitir y cuerpoDecision tienen exactamente las claves del contrato", () => {
  assert.deepEqual(cuerpoOmitir("cmp-1"), { compra_id: "cmp-1", omitir: true });
  assert.equal(cuerpoOmitir(null), null);
  assert.deepEqual(cuerpoDecision("cmp-1", "aceptar"), { compra_id: "cmp-1", decision: "aceptar" });
  assert.deepEqual(cuerpoDecision("cmp-1", "rechazar"), { compra_id: "cmp-1", decision: "rechazar" });
  assert.equal(cuerpoDecision("cmp-1", "aceptada"), null);
  assert.equal(cuerpoDecision(undefined, "aceptar"), null);
});

test("mensajeErrorAgente explica cada respuesta del proxy", () => {
  assert.match(mensajeErrorAgente(409, { error: "estado", estado: "vencida" }), /ya no está esperando.*Vencida/);
  assert.match(mensajeErrorAgente(404, { error: "compra_desconocida" }), /no conoce esta compra/);
  assert.match(mensajeErrorAgente(503, { error: "sin_token_del_proxy" }), /token del proxy/);
  assert.match(mensajeErrorAgente(502, { error: "proxy_desconectado", detalle: "ReadTimeout" }), /no contestó a tiempo/);
  assert.match(mensajeErrorAgente(502, { error: "proxy_desconectado" }), /proxy/);
  assert.match(mensajeErrorAgente(undefined, null, "Failed to fetch"), /proxy/);
  assert.match(mensajeErrorAgente(403, { error: "origen_no_permitido" }), /origen_no_permitido/);
});

// ---------------------------------------------------------------------------
// Formato
// ---------------------------------------------------------------------------

test("entregas: '10 el lun 19/10' sin depender de la zona horaria", () => {
  assert.equal(formatearFecha("2026-10-19"), "lun 19/10");
  assert.equal(formatearFecha("2026-10-21"), "mié 21/10");
  assert.equal(formatearFecha("2026-10-22"), "jue 22/10");
  assert.equal(formatearFecha("2026-02-30"), "2026-02-30");
  assert.equal(formatearFecha("mañana"), "mañana");
  assert.equal(formatearEntrega({ cantidad: 10, fecha: "2026-10-19" }), "10 el lun 19/10");
  assert.equal(formatearEntregas([{ cantidad: 10, fecha: "2026-10-19" }, { cantidad: 10, fecha: "2026-10-21" }]),
    "10 el lun 19/10 y 10 el mié 21/10");
  assert.equal(formatearEntregas([
    { cantidad: 5, fecha: "2026-10-19" }, { cantidad: 5, fecha: "2026-10-20" }, { cantidad: 10, fecha: "2026-10-21" },
  ]), "5 el lun 19/10, 5 el mar 20/10 y 10 el mié 21/10");
  assert.equal(formatearEntregas([]), "—");
});

test("formatearValor según el campo", () => {
  assert.equal(formatearValor("entregas", OPORTUNIDAD.cambios[0].ofrecido), "10 el lun 19/10 y 10 el mié 21/10");
  assert.equal(formatearValor("cantidad", 20), "20");
  assert.equal(formatearValor("precio", 149.5), "149.50");
  assert.equal(formatearValor("talle", [42, 43]), "42, 43");
  assert.equal(formatearValor("marca", null), "—");
  assert.equal(formatearValor("comercio", { id: "m" }), '{"id":"m"}');
});

test("describirRequisito: cubre es acumulado", () => {
  assert.deepEqual(describirRequisito(REQUISITOS[1]), {
    campo: "Entregas", condicion: "20 para el lun 19/10", obligatorio: true,
  });
  // 10 + 10 se escribe "10 … · 20 en total …", no "10 … y 20 …" (que parecen 30).
  assert.equal(describirRequisito(OPORTUNIDAD.requisitos_propuestos[0]).condicion,
    "10 para el lun 19/10 · 20 en total para el mié 21/10");
  assert.equal(formatearEntregasAcumuladas([{ x: 1 }]), '{"x":1}');
  assert.deepEqual(describirRequisito({ campo: "precio", operador: "maximo", valor: 1500, obligatorio: false }), {
    campo: "Precio", condicion: "≤ 1500", obligatorio: false,
  });
  assert.equal(describirRequisito(REQUISITOS[0]).condicion, "20");
});

test("restante: m:ss, h:mm:ss y vencido", () => {
  assert.deepEqual(restante(iso(90), T0), { ms: 90_000, vencido: false, texto: "1:30" });
  assert.equal(restante(iso(90), T0 + 100).texto, "1:30"); // redondea para arriba
  assert.equal(restante(iso(3725), T0).texto, "1:02:05");
  assert.deepEqual(restante(iso(0), T0), { ms: 0, vencido: true, texto: "0:00" });
  assert.equal(restante(null, T0), null);
  assert.equal(restante("no es fecha", T0), null);
});

// ---------------------------------------------------------------------------
// Línea de tiempo (vista Flujo)
// ---------------------------------------------------------------------------

test("describirEvento resume los cuatro eventos de nuestro agente", () => {
  const [pedido, contexto, oportunidad] = eventosCompra("cmp-d");
  const p = describirEvento(pedido);
  assert.match(p.titulo, /pregunta el porqué/);
  assert.match(p.resumen, /«pedido de test» · pregunta de test/);

  const c = describirEvento(contexto);
  assert.match(c.resumen, /Necesidad: necesidad de test/);
  assert.match(c.resumen, /2 flexibilidades \(1 inferida\)/);
  assert.match(describirEvento(ev("contexto_registrado", { compra_id: "x", omitido: true })).resumen, /omitió.*pedido literal/);
  assert.match(describirEvento(ev("contexto_registrado", { compra_id: "x", vencido: true })).resumen, /Sin respuesta a tiempo/);

  const o = describirEvento(oportunidad);
  assert.match(o.titulo, /Posible punto de encuentro/);
  assert.match(o.resumen, /Entregas: 20 el lun 19\/10 → 10 el lun 19\/10 y 10 el mié 21\/10/);

  const decidida = (decision) => describirEvento(ev("oportunidad_decidida", { compra_id: "x", decision })).resumen;
  assert.match(decidida("aceptada"), /^Aceptada: parámetros nuevos/);
  assert.match(decidida("rechazada"), /^Rechazada: se mantiene el pedido original/);
  assert.match(decidida("vencida"), /^Vencida: no se compra nada\. El silencio no es autorización/);
});

test("los eventos de nuestro agente conviven con las compras de la etapa 1", () => {
  const evs = fusionarEventos([], [
    ...eventosCompra("cmp-e"),
    ev("busqueda_enviada", { catalog_search: "x" }, { contexto_id: "ctx-test" }),
  ]);
  const { compras, sistema } = agruparCompras(evs);
  // El pedido_detectado llega antes del context_id (el proxy retiene la búsqueda).
  assert.deepEqual(sistema.map((e) => e.tipo), ["pedido_detectado"]);
  assert.equal(compras.length, 1);
  assert.equal(compras[0].busqueda, "x");
  assert.equal(comprasDesdeEventos(evs).length, 1);
});

test("bloqueado con etapa: corte de seguridad o consulta vencida, antes del checkout", () => {
  const corte = ev("bloqueado", {
    cart_id: null, motivo: "motivo fijo de test", status: "bloqueado", requiere_aprobacion: false,
    etapa: "seguridad", senales: ["señal a", "señal b"],
  }, { contexto_id: "ctx-corte" });
  const d = describirEvento(corte);
  assert.match(d.titulo, /Corte de seguridad/);
  assert.equal(d.resumen, "motivo fijo de test (señales: señal a, señal b)");
  const [c] = agruparCompras([corte]).compras;
  assert.equal(estadoCompra(c).clave, "corte_seguridad");

  const vencida = ev("bloqueado", { motivo: "m", status: "bloqueado", requiere_aprobacion: false, etapa: "intencion" },
    { contexto_id: "ctx-vencida" });
  assert.match(describirEvento(vencida).titulo, /La consulta venció/);
  assert.equal(estadoCompra(agruparCompras([vencida]).compras[0]).clave, "sin_autorizacion");

  // Los bloqueados de siempre (veredicto del checkout) no traen etapa.
  const viejo = ev("bloqueado", { cart_id: "cart_1", motivo: "m", status: "bloqueado" }, { contexto_id: "ctx-viejo" });
  assert.equal(describirEvento(viejo).titulo, "Bloqueado antes de la firma");
  assert.equal(estadoCompra(agruparCompras([viejo]).compras[0]).clave, "bloqueado");
});
