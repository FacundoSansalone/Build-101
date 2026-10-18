// Estado de la conexión con el proxy y sondeos sin superposición.
// En el ensayo real el interruptor alternaba entre "Proxy conectado" y
// "Guard: sin proxy" (deshabilitado) por un solo sondeo fallido, y los
// sondeos se podían superponer.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  FALLOS_PARA_DESCONECTAR, interruptorHabilitado, registrarSondeo, unaALaVez,
} from "../../static/lib/conexion.js";

test("un sondeo fallido no desconecta: hacen falta varios seguidos", () => {
  assert.equal(FALLOS_PARA_DESCONECTAR, 3);
  let p = { conectado: true, fallos: 0 };
  p = registrarSondeo(p, false);
  assert.deepEqual(p, { conectado: true, fallos: 1 });
  p = registrarSondeo(p, false);
  assert.deepEqual(p, { conectado: true, fallos: 2 });
  p = registrarSondeo(p, true);
  assert.deepEqual(p, { conectado: true, fallos: 0 });
  for (let i = 0; i < 3; i++) p = registrarSondeo(p, false);
  assert.deepEqual(p, { conectado: false, fallos: 3 });
  p = registrarSondeo(p, true);
  assert.deepEqual(p, { conectado: true, fallos: 0 });
});

test("al arrancar queda 'consultando' hasta el primer OK o varios fallos", () => {
  let p = { conectado: null, fallos: 0 };
  p = registrarSondeo(p, false);
  assert.equal(p.conectado, null);
  p = registrarSondeo(p, false);
  assert.equal(p.conectado, null);
  p = registrarSondeo(p, false);
  assert.equal(p.conectado, false);
});

test("registrarSondeo no muta y conserva el resto del estado", () => {
  const antes = { conectado: true, fallos: 0, guard_activo: true, evaluador: "guard" };
  const despues = registrarSondeo(antes, false);
  assert.deepEqual(antes, { conectado: true, fallos: 0, guard_activo: true, evaluador: "guard" });
  assert.equal(despues.guard_activo, true);
  assert.equal(despues.evaluador, "guard");
});

test("el interruptor solo se deshabilita sin proxy, no mientras cambia", () => {
  assert.equal(interruptorHabilitado({ conectado: true, cambiando: false }), true);
  assert.equal(interruptorHabilitado({ conectado: true, cambiando: true }), true);
  assert.equal(interruptorHabilitado({ conectado: false, cambiando: false }), false);
  assert.equal(interruptorHabilitado({ conectado: null, cambiando: false }), false);
});

test("unaALaVez no superpone llamadas y libera al terminar (también con error)", async () => {
  let llamadas = 0;
  let soltar;
  const sondeo = unaALaVez(() => {
    llamadas += 1;
    return new Promise((ok) => { soltar = ok; });
  });
  const a = sondeo();
  const b = sondeo();
  assert.equal(llamadas, 1);
  assert.equal(await b, undefined); // la segunda no espera ni repite el pedido
  soltar("listo");
  assert.equal(await a, "listo");
  const c = sondeo();
  assert.equal(llamadas, 2);
  soltar("otra");
  await c;

  const falla = unaALaVez(async () => { llamadas += 1; throw new Error("caído"); });
  await assert.rejects(falla(), /caído/);
  await assert.rejects(falla(), /caído/); // no queda trabado
});
