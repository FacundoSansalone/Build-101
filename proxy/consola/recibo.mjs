// Recibo de KaiAI: verificación en el navegador del JWS compacto ES256 que
// firma el motor (guard/recibo.py) contra `GET /v1/public-key`, con WebCrypto
// (inyectado). Lógica pura, sin DOM: corre en el navegador y en Node.
//
// Es de la consola (`/consola/recibo.mjs`, lo re-exporta logica.mjs) y el
// panel del cliente sirve este mismo archivo (`/panel/recibo.mjs`): una sola
// verificación para las dos páginas.

const decodificador = new TextDecoder("utf-8", { fatal: true });
const codificador = new TextEncoder();

export function b64urlABytes(texto) {
  if (typeof texto !== "string" || !/^[A-Za-z0-9_-]*$/.test(texto) || texto.length % 4 === 1) {
    throw new Error("base64url inválido");
  }
  const b64 = texto.replace(/-/g, "+").replace(/_/g, "/");
  const binario = atob(b64 + "===".slice((b64.length + 3) % 4));
  const bytes = new Uint8Array(binario.length);
  for (let i = 0; i < binario.length; i++) bytes[i] = binario.charCodeAt(i);
  return bytes;
}

/** Un JWS compacto → `{header, payload, entrada, firma}`. Lanza si no lo es. */
export function decodificarJws(jws) {
  if (typeof jws !== "string") throw new Error("el JWS no es texto");
  const partes = jws.split(".");
  if (partes.length !== 3) throw new Error("un JWS compacto tiene 3 partes");
  const [h, p, f] = partes;
  const header = JSON.parse(decodificador.decode(b64urlABytes(h)));
  const payload = JSON.parse(decodificador.decode(b64urlABytes(p)));
  if (!header || typeof header !== "object" || Array.isArray(header)) throw new Error("header inválido");
  return { header, payload, entrada: `${h}.${p}`, firma: b64urlABytes(f) };
}

/** La JWK con ese `kid` de `GET /v1/public-key` (`{kid, jwk, keys}`), o null. */
export function elegirJwk(publica, kid) {
  if (!publica || typeof publica !== "object" || typeof kid !== "string") return null;
  const candidatas = [
    ...(Array.isArray(publica.keys) ? publica.keys : []),
    ...(publica.jwk ? [publica.jwk] : []),
  ];
  return candidatas.find((k) => k && typeof k === "object" && k.kid === kid) || null;
}

/**
 * Verifica la firma ES256 de un JWS con la clave pública del motor.
 * `subtle` es WebCrypto (`crypto.subtle`), inyectado. Nunca lanza:
 * `{ok, motivo, header, payload}`.
 */
export async function verificarJws(jws, publica, subtle = globalThis.crypto?.subtle) {
  let partes;
  try {
    partes = decodificarJws(jws);
  } catch (e) {
    return { ok: false, motivo: `no es un JWS compacto (${e.message})`, header: null, payload: null };
  }
  const { header, payload, entrada, firma } = partes;
  const fallo = (motivo) => ({ ok: false, motivo, header, payload });
  if (header.alg !== "ES256") return fallo(`algoritmo ${String(header.alg)}; se espera ES256`);
  if (firma.length !== 64) return fallo(`la firma tiene ${firma.length} bytes; ES256 usa 64 (r||s)`);
  const jwk = elegirJwk(publica, header.kid);
  if (!jwk) return fallo(`no hay una clave pública con kid ${String(header.kid)}`);
  if (jwk.kty !== "EC" || jwk.crv !== "P-256" || typeof jwk.x !== "string" || typeof jwk.y !== "string") {
    return fallo("la clave pública no es EC P-256");
  }
  if (!subtle) return fallo("este navegador no tiene WebCrypto (hace falta https)");
  try {
    const clave = await subtle.importKey(
      "jwk", { kty: "EC", crv: "P-256", x: jwk.x, y: jwk.y, ext: true },
      { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"],
    );
    const ok = await subtle.verify(
      { name: "ECDSA", hash: "SHA-256" }, clave, firma, codificador.encode(entrada),
    );
    return ok ? { ok: true, motivo: "firma ES256 válida", header, payload } : fallo("la firma no corresponde");
  } catch (e) {
    return fallo(`no se pudo verificar (${e?.name || "error"})`);
  }
}

/**
 * El recibo de un veredicto (`{id, jws, kid}`): la firma vale y además el
 * `kid` del header y el `id` del payload son los del recibo.
 */
export async function verificarRecibo(recibo, publica, subtle = globalThis.crypto?.subtle) {
  if (!recibo || typeof recibo.jws !== "string") {
    return { ok: false, motivo: "el veredicto no trae recibo firmado", header: null, payload: null };
  }
  const resultado = await verificarJws(recibo.jws, publica, subtle);
  if (!resultado.ok) return resultado;
  if (resultado.header.kid !== recibo.kid) {
    return { ...resultado, ok: false, motivo: "el kid del JWS no es el del recibo" };
  }
  if (!resultado.payload || resultado.payload.id !== recibo.id) {
    return { ...resultado, ok: false, motivo: "el id del payload no es el del recibo" };
  }
  return resultado;
}
