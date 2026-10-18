// Intake en la vista Agente: varias repreguntas, lo que falta, la ficha con
// lo inferido "a confirmar" y la confirmación al final. Eventos sintéticos.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  claveEnvio, comprasDesdeEventos, cuerpoConfirmar, describirParametro, describirRequisito,
  fusionarCompras, mismosRequisitos, turnoActual,
} from "../../static/lib/agente.js";
import { describirEvento } from "../../static/lib/eventos.js";

const T0 = Date.UTC(2026, 9, 18, 12, 0, 0);
const iso = (s) => new Date(T0 + s * 1000).toISOString();
let n = 0;
const ev = (tipo, datos) => ({
  id: `in-${++n}`, ts: iso(n), contexto_id: null, escenario: null, guard_activo: true, tipo, datos,
});

const ID = "cmp-intake";
const PARCIAL = [
  { parametro: "categoria", estado: "resuelto", valor: "basketball_shoes", origen: "dicho", probabilidad: 0.99, texto: "basketball_shoes", necesario: true, requisito: { campo: "categoria", operador: "igual", valor: "basketball_shoes", obligatorio: true, origen: "dicho" } },
  { parametro: "cantidad", estado: "a_confirmar", valor: 1, origen: "inferido", probabilidad: 0.93, texto: "1", necesario: true, requisito: null },
  { parametro: "precio", estado: "faltante", valor: null, origen: null, probabilidad: 0.9, texto: "", necesario: true, requisito: null },
  { parametro: "mencion:marca:nike", estado: "resuelto", valor: "Nike", origen: "dicho", probabilidad: 1, texto: "marca Nike", necesario: false, requisito: { campo: "marca", operador: "igual", valor: "Nike", obligatorio: true, origen: "dicho" } },
];

function conversacion() {
  return [
    ev("pedido_detectado", {
      compra_id: ID, pedido: "zapatillas Nike", pregunta: "¿Para qué? Me falta el tope.", requisitos: [],
      vence: iso(300), turno: 0, confirmar: false, faltan: ["precio", "moneda", "plazo"], a_confirmar: ["cantidad"],
      ficha_parcial: PARCIAL,
    }),
    ev("contexto_registrado", {
      compra_id: ID, turno: 0, respuesta: "Para jugar.", omitido: false, vencido: false,
      contexto: { pedido: "zapatillas Nike", pregunta: "¿Para qué?", respuesta: "Para jugar.", historial: [], necesidad: "Para jugar.", necesidad_entregas: [], flexibilidades: [] },
      intake: { faltan: ["precio", "moneda", "plazo"], siguiente: "repreguntar" },
    }),
    ev("pedido_detectado", {
      compra_id: ID, pedido: "zapatillas Nike", pregunta: "Me falta saber el tope de precio y la moneda.",
      requisitos: [], vence: iso(400), turno: 1, confirmar: false, faltan: ["precio", "moneda"], a_confirmar: ["cantidad"],
      ficha_parcial: PARCIAL,
    }),
    ev("contexto_registrado", {
      compra_id: ID, turno: 1, respuesta: "Hasta 150 dólares.", omitido: false, vencido: false, contexto: null,
    }),
    ev("pedido_detectado", {
      compra_id: ID, pedido: "zapatillas Nike", pregunta: "Armé la ficha. ¿La confirmás?",
      requisitos: [{ campo: "precio", operador: "maximo", valor: 150, obligatorio: true, origen: "dicho" }],
      vence: iso(500), turno: 2, confirmar: true, faltan: [], a_confirmar: ["cantidad"], ficha_parcial: PARCIAL,
    }),
  ];
}

test("varias repreguntas: cada turno con su pregunta, lo que falta y la respuesta", () => {
  const [c] = comprasDesdeEventos(conversacion());
  assert.equal(c.compra_id, ID);
  assert.equal(c.estado, "esperando_contexto");
  assert.equal(c.pedido, "zapatillas Nike");
  assert.deepEqual(c.turnos.map((t) => t.turno), [0, 1, 2]);
  assert.deepEqual(c.turnos[0].faltan, ["precio", "moneda", "plazo"]);
  assert.equal(c.turnos[0].respuesta, "Para jugar.");
  assert.equal(c.turnos[1].respuesta, "Hasta 150 dólares.");
  assert.equal(c.turnos[2].confirmar, true);
  assert.equal(c.turnos[2].respuesta, null);
  assert.equal(c.pregunta, "Armé la ficha. ¿La confirmás?");
  assert.equal(turnoActual(c), 2);
  assert.equal(c.intake.confirmar, true);
  // El contexto del primer turno no se pierde con un turno sin contexto.
  assert.equal(c.contexto.respuesta, "Para jugar.");
  assert.equal(c.requisitos[0].campo, "precio");
});

test("confirmar cierra la ficha: autorizada, sin verificar y confirmada", () => {
  const eventos = [...conversacion(), ev("contexto_registrado", {
    compra_id: ID, turno: 2, respuesta: null, omitido: false, vencido: false, contexto: null,
    final: true, confirmada: true, sin_tiempo: false,
    ficha_autorizada: [{ campo: "precio", operador: "maximo", valor: 150, obligatorio: true, origen: "dicho" }],
    sin_verificar_guard: [{ campo: "atributo", atributo: "color", operador: "igual", valor: "negro", obligatorio: true, origen: "dicho" }],
  })];
  const [c] = comprasDesdeEventos(eventos);
  assert.equal(c.estado, "negociando");
  assert.equal(c.turnos[2].confirmada, true);
  assert.equal(c.intake.final, true);
  assert.equal(c.intake.confirmada, true);
  assert.equal(c.intake.ficha_autorizada[0].valor, 150);
  assert.equal(c.intake.sin_verificar_guard[0].atributo, "color");
  assert.equal(c.intake.confirmar, false);
});

test("un sondeo de un turno anterior no pisa el turno nuevo de los eventos", () => {
  const eventos = comprasDesdeEventos(conversacion()); // turno 2, esperando
  const viejo = { compra_id: ID, estado: "negociando", pedido: "zapatillas Nike", pregunta: "x", requisitos: [], intake: { turno: 1, confirmar: false } };
  assert.equal(fusionarCompras(eventos, [viejo])[0].estado, "esperando_contexto");
  // El proxy en el mismo turno y más avanzado (ya contestó): manda el proxy.
  const nuevo = { ...viejo, intake: { turno: 2, confirmar: false } };
  const [c] = fusionarCompras(eventos, [nuevo]);
  assert.equal(c.estado, "negociando");
  assert.equal(c.turnos.length, 3); // la historia de turnos sale de los eventos
});

test("una compra solo del proxy trae el intake del sondeo", () => {
  const [c] = fusionarCompras([], [{
    compra_id: ID, estado: "esperando_contexto", pedido: "p", pregunta: "¿La confirmás?",
    requisitos: [], intake: { turno: 3, confirmar: true, faltan: [], ficha_parcial: PARCIAL },
  }]);
  assert.equal(turnoActual(c), 3);
  assert.equal(c.intake.confirmar, true);
});

test("claveEnvio distingue el turno: contestar el turno 0 no traba el 1", () => {
  const t0 = { compra_id: ID, estado: "esperando_contexto", intake: { turno: 0 } };
  const t1 = { compra_id: ID, estado: "esperando_contexto", intake: { turno: 1 } };
  assert.notEqual(claveEnvio(t0), claveEnvio(t1));
  assert.equal(claveEnvio({ compra_id: ID, estado: "esperando_decision" }), "esperando_decision:0");
});

test("cuerpoConfirmar tiene exactamente las claves del contrato", () => {
  assert.deepEqual(cuerpoConfirmar(ID), { compra_id: ID, confirmar: true });
  assert.equal(cuerpoConfirmar(""), null);
  assert.equal(cuerpoConfirmar("x".repeat(201)), null);
});

test("describirParametro: lo dicho, lo inferido a confirmar y lo que falta", () => {
  const [categoria, cantidad, precio, marca] = PARCIAL.map(describirParametro);
  assert.deepEqual(
    [categoria.nombre, categoria.valor, categoria.estado, categoria.origen],
    ["Producto", "basketball_shoes", "resuelto", "lo dijiste"],
  );
  assert.deepEqual([cantidad.nombre, cantidad.estado, cantidad.origen], ["Cantidad", "a confirmar", "lo deduje"]);
  assert.deepEqual([precio.nombre, precio.valor, precio.estado, precio.origen], ["Tope", "—", "falta", null]);
  // Las menciones se nombran por su requisito: "Marca · Nike", no "Atributo · marca Nike".
  assert.deepEqual([marca.nombre, marca.valor, marca.obligatorio], ["Marca", "Nike", true]);
  const color = describirParametro({ parametro: "mencion:atributo:color", estado: "a_confirmar", origen: "dicho", texto: "color negro", necesario: false, requisito: null });
  assert.deepEqual([color.nombre, color.valor], ["Atributo", "color negro"]);
});

test("mismosRequisitos: la ficha vigente puede no ser la del intake (escenario de la demo)", () => {
  const a = [{ campo: "precio", operador: "maximo", valor: 150, obligatorio: true, origen: "dicho" }];
  const b = [{ campo: "precio", operador: "maximo", valor: 150, obligatorio: true }];
  assert.equal(mismosRequisitos(a, b), true); // el origen no cambia lo que se verifica
  assert.equal(mismosRequisitos(a, [...b, { campo: "uso", operador: "igual", valor: "outdoor", obligatorio: true }]), false);
  assert.equal(mismosRequisitos(a, [{ ...b[0], valor: 200 }]), false);
});

test("describirRequisito: atributos genéricos y mínimo con unidad", () => {
  const ram = describirRequisito({ campo: "atributo", atributo: "memoria_ram", operador: "minimo", valor: 16, unidad: "GB", obligatorio: true });
  assert.deepEqual(ram, { campo: "Memoria RAM", condicion: "≥ 16 GB", obligatorio: true });
  const color = describirRequisito({ campo: "atributo", atributo: "color", operador: "igual", valor: "negro", obligatorio: false, origen: "inferido" });
  assert.deepEqual(color, { campo: "Color", condicion: "negro", obligatorio: false, inferido: true });
});

test("describirEvento: repregunta, lo que falta y la confirmación", () => {
  const [, , repregunta] = conversacion();
  assert.match(describirEvento(repregunta).resumen, /turno 1.*falta: tope, moneda/);
  const confirmada = ev("contexto_registrado", { compra_id: ID, turno: 2, confirmada: true, final: true, omitido: false, vencido: false });
  assert.match(describirEvento(confirmada).resumen, /confirmó la ficha/);
});
