// El ensayo real del escenario B (dashboard/demo/ensayo_B.jsonl, 17 eventos
// del proxy, sin secretos) arma el lado a lado del momento clave en Replay.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  agruparCompras, estadoCompra, fusionarEventos, mandatosDe, parLadoALado,
} from "../../static/lib/eventos.js";

const ARCHIVO = new URL("../../demo/ensayo_B.jsonl", import.meta.url);

function cargar() {
  const lineas = readFileSync(ARCHIVO, "utf8").split("\n").filter((l) => l.trim());
  return lineas.map((l) => JSON.parse(l));
}

test("el ensayo trae las dos compras de B", () => {
  const eventos = cargar();
  assert.equal(eventos.length, 17);
  const { compras } = agruparCompras(fusionarEventos([], eventos));
  const ids = compras.map((c) => c.contextoId.slice(0, 8)).sort();
  assert.deepEqual(ids, ["036a26e7", "05d515eb"]);
});

test("lado a lado: sin Guard la compra pagada con mandatos, con Guard la bloqueada", () => {
  const { compras } = agruparCompras(fusionarEventos([], cargar()));
  const { sin, con } = parLadoALado(compras, "B");

  assert.ok(sin, "falta la compra sin Guard");
  assert.ok(sin.contextoId.startsWith("036a26e7"));
  assert.equal(sin.guardActivo, false);
  assert.ok(sin.checkout, "sin Guard: el checkout firmado llegó");
  assert.ok(mandatosDe(sin).length >= 2, "sin Guard: los mandatos SD-JWT del usuario");
  assert.deepEqual(
    [...new Set(mandatosDe(sin).map((m) => m.nombre))].sort(),
    ["ap2.mandates.CheckoutMandateSdJwt", "ap2.mandates.PaymentMandateSdJwt"],
  );
  assert.equal(estadoCompra(sin).clave, "firmado");

  assert.ok(con, "falta la compra con Guard");
  assert.ok(con.contextoId.startsWith("05d515eb"));
  assert.equal(con.guardActivo, true);
  assert.equal(con.veredicto?.status, "bloqueado");
  assert.equal(estadoCompra(con).clave, "bloqueado");
  assert.equal(mandatosDe(con).length, 0, "con Guard nada llegó a la firma");
});
