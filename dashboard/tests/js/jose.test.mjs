// node --test dashboard/tests/js/
// Datos sintéticos de test: claves generadas acá, recibos inventados.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  b64urlABytes, bytesAB64url, decodificarJwt, decodificarSdJwt, jsonCanonico,
  payloadCanonicoRecibo, resolverSdJwt, sha256B64url, verificarRecibo,
} from "../../static/lib/jose.js";

const subtle = globalThis.crypto.subtle;
const enc = new TextEncoder();
const b64json = (o) => bytesAB64url(enc.encode(JSON.stringify(o)));
const b64texto = (t) => bytesAB64url(enc.encode(t));

const RECIBO_TEST = {
  id: "recibo-test-1",
  emitido: "2026-01-01T00:00:00Z",
  ficha: {
    pedido_original: "pedido de test ñandú",
    requisitos: [{ campo: "precio", operador: "maximo", valor: 150, obligatorio: true }],
    criterio: "lowest_price_among_compliant",
    confirmada: true,
  },
  consideradas: ["cart_1", "cart_2"],
  filtradas: { cart_2: ["categoria: x ≠ y"] },
  verificaciones: [],
  ranking: ["cart_1", "cart_2"],
  elegido: "cart_2",
  por_que_gano: "test",
  sin_verificar: ["calidad"],
  status: "bloqueado",
  checkout_hash: null,
  firma: null,
  kid: "kid-test",
};

async function claveTest() {
  const par = await subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const jwk = await subtle.exportKey("jwk", par.publicKey);
  return { par, publica: { kid: "kid-test", jwk: { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y } } };
}

async function firmar(par, header, payloadTexto) {
  const h = b64json(header);
  const p = b64texto(payloadTexto);
  const firma = new Uint8Array(await subtle.sign({ name: "ECDSA", hash: "SHA-256" }, par.privateKey, enc.encode(`${h}.${p}`)));
  return `${h}.${p}.${bytesAB64url(firma)}`;
}

test("base64url ida y vuelta, sin relleno", () => {
  const bytes = new Uint8Array([0, 255, 62, 63, 1]);
  const t = bytesAB64url(bytes);
  assert.ok(!t.includes("=") && !t.includes("+") && !t.includes("/"));
  assert.deepEqual(b64urlABytes(t), bytes);
  assert.throws(() => b64urlABytes("a+b"));
});

test("decodificarJwt separa header, payload y firma", () => {
  const jwt = `${b64json({ alg: "ES256", kid: "k" })}.${b64json({ a: 1 })}.${bytesAB64url(new Uint8Array(64))}`;
  const d = decodificarJwt(jwt);
  assert.equal(d.header.alg, "ES256");
  assert.deepEqual(d.payload, { a: 1 });
  assert.equal(d.firmaBytes.length, 64);
  assert.throws(() => decodificarJwt("solo.dos"));
});

test("jsonCanonico ordena claves, sin espacios y sin escapar lo no ASCII", () => {
  assert.equal(jsonCanonico({ b: 1, a: [true, null, "ñ"], c: { z: 1, y: 2 } }),
    '{"a":[true,null,"ñ"],"b":1,"c":{"y":2,"z":1}}');
  assert.equal(payloadCanonicoRecibo({ kid: "k", firma: "f", b: 2, a: 1 }), '{"a":1,"b":2}');
});

test("verificarRecibo: firma válida y contenido igual", async () => {
  const { par, publica } = await claveTest();
  const payload = payloadCanonicoRecibo(RECIBO_TEST);
  const firma = await firmar(par, { alg: "ES256", kid: "kid-test" }, payload);
  const r = await verificarRecibo({ ...RECIBO_TEST, firma }, publica, subtle);
  assert.equal(r.ok, true, JSON.stringify(r.pasos));
  assert.deepEqual(r.pasos.map((p) => p.id), ["formato", "kid", "firma", "contenido"]);
  assert.equal(r.exacto, true);
});

test("verificarRecibo: 150.0 de Python coincide con 150 de JS (no exacto, igual)", async () => {
  const { par, publica } = await claveTest();
  const payload = payloadCanonicoRecibo(RECIBO_TEST).replace('"valor":150', '"valor":150.0');
  const firma = await firmar(par, { alg: "ES256", kid: "kid-test" }, payload);
  const r = await verificarRecibo({ ...RECIBO_TEST, firma }, publica, subtle);
  assert.equal(r.ok, true);
  assert.equal(r.exacto, false);
});

test("verificarRecibo: recibo alterado después de firmar → contenido no coincide", async () => {
  const { par, publica } = await claveTest();
  const firma = await firmar(par, { alg: "ES256", kid: "kid-test" }, payloadCanonicoRecibo(RECIBO_TEST));
  const r = await verificarRecibo({ ...RECIBO_TEST, status: "aprobado", firma }, publica, subtle);
  assert.equal(r.ok, false);
  assert.equal(r.pasos.find((p) => p.id === "firma").ok, true);
  assert.equal(r.pasos.find((p) => p.id === "contenido").ok, false);
});

test("verificarRecibo: otra clave → firma inválida", async () => {
  const { par } = await claveTest();
  const otra = await claveTest();
  const firma = await firmar(par, { alg: "ES256", kid: "kid-test" }, payloadCanonicoRecibo(RECIBO_TEST));
  const r = await verificarRecibo({ ...RECIBO_TEST, firma }, otra.publica, subtle);
  assert.equal(r.ok, false);
  assert.equal(r.pasos.find((p) => p.id === "firma").ok, false);
});

test("verificarRecibo: sin firma, alg distinto o kid distinto", async () => {
  const { par, publica } = await claveTest();
  assert.equal((await verificarRecibo({ ...RECIBO_TEST }, publica, subtle)).ok, false);
  const hs = await firmar(par, { alg: "HS256", kid: "kid-test" }, payloadCanonicoRecibo(RECIBO_TEST));
  assert.equal((await verificarRecibo({ ...RECIBO_TEST, firma: hs }, publica, subtle)).pasos[0].ok, false);
  const otroKid = await firmar(par, { alg: "ES256", kid: "otro" }, payloadCanonicoRecibo(RECIBO_TEST));
  const r = await verificarRecibo({ ...RECIBO_TEST, firma: otroKid }, publica, subtle);
  assert.equal(r.pasos.find((p) => p.id === "kid").ok, false);
  assert.equal(r.ok, false);
});

test("SD-JWT: decodifica divulgaciones y las resuelve por digest", async () => {
  const d1 = b64json(["sal1", "checkout_hash", "hash-test"]);
  const d2 = b64json(["sal2", "elemento"]);
  const payload = {
    _sd: [await sha256B64url(d1, subtle)],
    _sd_alg: "sha-256",
    lista: [{ "...": await sha256B64url(d2, subtle) }, "fijo"],
    iss: "test",
  };
  const jwt = `${b64json({ alg: "ES256" })}.${b64json(payload)}.${bytesAB64url(new Uint8Array(64))}`;
  const [eslabon] = decodificarSdJwt(`${jwt}~${d1}~${d2}~`);
  assert.equal(eslabon.divulgaciones.length, 2);
  assert.equal(eslabon.kb, null);
  const { resuelto, sinUsar } = await resolverSdJwt(eslabon.emisor.payload, eslabon.divulgaciones, subtle);
  assert.deepEqual(resuelto, { lista: ["elemento", "fijo"], iss: "test", checkout_hash: "hash-test" });
  assert.equal(sinUsar, 0);
});

test("SD-JWT: cadena con ~~ y KB-JWT al final", () => {
  const jwt = `${b64json({ alg: "ES256" })}.${b64json({ a: 1 })}.${bytesAB64url(new Uint8Array(64))}`;
  const kb = `${b64json({ alg: "ES256", typ: "kb+jwt" })}.${b64json({ nonce: "n" })}.${bytesAB64url(new Uint8Array(64))}`;
  const cadena = decodificarSdJwt(`${jwt}~~${jwt}~${kb}`);
  assert.equal(cadena.length, 2);
  assert.equal(cadena[1].kb.payload.nonce, "n");
});
