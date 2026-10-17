// JWT / JWS / SD-JWT: decodificar y verificar. Sin DOM: corre en el navegador
// y en Node (node --test). La criptografía es WebCrypto (crypto.subtle).

const enc = new TextEncoder();
const dec = new TextDecoder("utf-8", { fatal: true });

export function b64urlABytes(texto) {
  if (typeof texto !== "string" || !/^[A-Za-z0-9_-]*$/.test(texto)) {
    throw new Error("base64url inválido");
  }
  const b64 = texto.replace(/-/g, "+").replace(/_/g, "/");
  const relleno = b64 + "===".slice((b64.length + 3) % 4);
  const bin = atob(relleno);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

export function bytesAB64url(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64urlATexto(texto) {
  return dec.decode(b64urlABytes(texto));
}

/** JWS/JWT compacto → partes decodificadas. Lanza si no tiene 3 partes JSON. */
export function decodificarJwt(compacto) {
  if (typeof compacto !== "string") throw new Error("El JWT no es texto");
  const partes = compacto.trim().split(".");
  if (partes.length !== 3) throw new Error("Un JWS compacto tiene 3 partes");
  const [h, p, f] = partes;
  const payloadTexto = b64urlATexto(p);
  return {
    header: JSON.parse(b64urlATexto(h)),
    payload: JSON.parse(payloadTexto),
    payloadTexto,
    segmentos: { header: h, payload: p, firma: f },
    firmaBytes: b64urlABytes(f),
  };
}

/**
 * SD-JWT (RFC 9901): `<jwt>~<divulgación>~...~[<kb-jwt>]`. Los mandatos de AP2
 * pueden encadenarse con `~~`; se devuelve un elemento por eslabón.
 */
export function decodificarSdJwt(token) {
  if (typeof token !== "string" || !token.includes(".")) {
    throw new Error("No parece un SD-JWT");
  }
  return token
    .split("~~")
    .filter((t) => t.length > 0)
    .map((eslabon) => {
      const partes = eslabon.split("~");
      const emisor = decodificarJwt(partes[0]);
      const resto = partes.slice(1);
      // El último elemento es el KB-JWT si tiene forma de JWT; si no, vacío.
      let kb = null;
      const ultimo = resto[resto.length - 1];
      if (ultimo && ultimo.split(".").length === 3) {
        kb = decodificarJwt(ultimo);
        resto.pop();
      }
      const divulgaciones = resto
        .filter((d) => d.length > 0)
        .map((b64) => {
          const arr = JSON.parse(b64urlATexto(b64));
          if (!Array.isArray(arr) || (arr.length !== 2 && arr.length !== 3)) {
            throw new Error("Divulgación con formato inesperado");
          }
          return arr.length === 3
            ? { b64, salt: arr[0], nombre: arr[1], valor: arr[2] }
            : { b64, salt: arr[0], nombre: null, valor: arr[1] };
        });
      return { emisor, divulgaciones, kb };
    });
}

export async function sha256B64url(texto, subtle = globalThis.crypto?.subtle) {
  if (!subtle) throw new Error("WebCrypto no disponible");
  const digest = await subtle.digest("SHA-256", enc.encode(texto));
  return bytesAB64url(new Uint8Array(digest));
}

/** Aplica las divulgaciones a un payload SD-JWT (`_sd` y `{"...": digest}`). */
export async function resolverSdJwt(payload, divulgaciones, subtle = globalThis.crypto?.subtle) {
  const porDigest = new Map();
  for (const d of divulgaciones) porDigest.set(await sha256B64url(d.b64, subtle), d);
  const usadas = new Set();

  const resolver = (valor) => {
    if (Array.isArray(valor)) {
      const out = [];
      for (const item of valor) {
        if (item && typeof item === "object" && !Array.isArray(item) &&
            Object.keys(item).length === 1 && typeof item["..."] === "string") {
          const d = porDigest.get(item["..."]);
          if (d) {
            usadas.add(d.b64);
            out.push(resolver(d.valor));
          }
          continue; // digest sin divulgar: no se muestra
        }
        out.push(resolver(item));
      }
      return out;
    }
    if (valor && typeof valor === "object") {
      const out = {};
      for (const [k, v] of Object.entries(valor)) {
        if (k === "_sd" || k === "_sd_alg") continue;
        out[k] = resolver(v);
      }
      for (const digest of Array.isArray(valor._sd) ? valor._sd : []) {
        const d = porDigest.get(digest);
        if (d && d.nombre !== null) {
          usadas.add(d.b64);
          out[d.nombre] = resolver(d.valor);
        }
      }
      return out;
    }
    return valor;
  };

  const resuelto = resolver(payload);
  return { resuelto, sinUsar: divulgaciones.filter((d) => !usadas.has(d.b64)).length };
}

// ---------------------------------------------------------------------------
// JSON canónico (mismo criterio que Recibo.payload_canonico() en Python:
// claves ordenadas, separadores "," y ":", sin escapar lo no ASCII).
// ---------------------------------------------------------------------------

export function jsonCanonico(valor) {
  if (valor === null || typeof valor !== "object") return JSON.stringify(valor);
  if (Array.isArray(valor)) return "[" + valor.map(jsonCanonico).join(",") + "]";
  const claves = Object.keys(valor).filter((k) => valor[k] !== undefined).sort(compararClaves);
  return "{" + claves.map((k) => JSON.stringify(k) + ":" + jsonCanonico(valor[k])).join(",") + "}";
}

// Python ordena por punto de código; sort() de JS por unidades UTF-16. Para
// claves BMP es lo mismo; comparamos por punto de código para no depender de eso.
function compararClaves(a, b) {
  const ca = Array.from(a, (c) => c.codePointAt(0));
  const cb = Array.from(b, (c) => c.codePointAt(0));
  for (let i = 0; i < Math.min(ca.length, cb.length); i++) {
    if (ca[i] !== cb[i]) return ca[i] - cb[i];
  }
  return ca.length - cb.length;
}

/** Lo que se firma de un Recibo: todo menos `firma` y `kid`. */
export function payloadCanonicoRecibo(recibo) {
  const { firma, kid, ...resto } = recibo || {};
  return jsonCanonico(resto);
}

// null y ausente significan lo mismo para comparar contenido (un emisor puede
// serializar con exclude_none). 150 y 150.0 también: JSON.parse no los distingue.
function sinNulos(valor) {
  if (Array.isArray(valor)) return valor.map(sinNulos);
  if (valor && typeof valor === "object") {
    const out = {};
    for (const [k, v] of Object.entries(valor)) if (v !== null) out[k] = sinNulos(v);
    return out;
  }
  return valor;
}

/**
 * Verifica el recibo firmado del Guard en el navegador.
 * - firma: JWS compacto ES256 (r||s crudo, 64 bytes), como decide CLAUDE.md.
 * - jwk:   clave pública EC P-256 del Guard.
 * Devuelve los pasos con su resultado; `ok` solo si pasan todos.
 */
export async function verificarRecibo(recibo, clave, subtle = globalThis.crypto?.subtle) {
  const pasos = [];
  const paso = (id, ok, detalle) => { pasos.push({ id, ok, detalle }); return ok; };
  const fin = (extra = {}) => ({ ok: pasos.every((p) => p.ok), pasos, ...extra });

  if (!recibo?.firma) {
    paso("formato", false, "El recibo no trae firma.");
    return fin();
  }
  let jws;
  try {
    jws = decodificarJwt(recibo.firma);
  } catch (e) {
    paso("formato", false, `La firma no es un JWS compacto: ${e.message}`);
    return fin();
  }
  if (!paso("formato", jws.header.alg === "ES256" && jws.firmaBytes.length === 64,
    jws.header.alg === "ES256"
      ? (jws.firmaBytes.length === 64 ? "JWS compacto ES256 (r||s, 64 bytes)."
        : `La firma tiene ${jws.firmaBytes.length} bytes; ES256 usa 64.`)
      : `Algoritmo ${jws.header.alg}; se espera ES256.`)) {
    return fin({ header: jws.header });
  }

  const kidClave = clave?.kid ?? null;
  const kids = [jws.header.kid, recibo.kid, kidClave].filter((k) => k != null);
  paso("kid", kids.length > 0 && kids.every((k) => k === kids[0]),
    `kid del JWS: ${jws.header.kid ?? "—"} · del recibo: ${recibo.kid ?? "—"} · de la clave: ${kidClave ?? "—"}`);

  if (!subtle) {
    paso("firma", false, "WebCrypto no está disponible (abrí el dashboard en localhost).");
    return fin({ header: jws.header });
  }
  if (!clave?.jwk) {
    paso("firma", false, "No hay clave pública del Guard para verificar.");
    return fin({ header: jws.header });
  }
  let valida = false;
  try {
    const { kty, crv, x, y } = clave.jwk;
    const k = await subtle.importKey("jwk", { kty, crv, x, y, ext: true },
      { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    valida = await subtle.verify({ name: "ECDSA", hash: "SHA-256" }, k, jws.firmaBytes,
      enc.encode(`${jws.segmentos.header}.${jws.segmentos.payload}`));
  } catch (e) {
    paso("firma", false, `No se pudo verificar: ${e.message}`);
    return fin({ header: jws.header });
  }
  paso("firma", valida, valida
    ? "Firma ECDSA P-256 / SHA-256 válida con la clave pública del Guard."
    : "La firma NO corresponde a esta clave y este contenido.");

  const esperado = payloadCanonicoRecibo(recibo);
  let firmado;
  try {
    firmado = JSON.parse(jws.payloadTexto);
  } catch {
    paso("contenido", false, "El payload firmado no es JSON.");
    return fin({ header: jws.header });
  }
  const exacto = jws.payloadTexto === esperado;
  const igual = exacto ||
    jsonCanonico(sinNulos(firmado)) === jsonCanonico(sinNulos(JSON.parse(esperado)));
  paso("contenido", igual, igual
    ? (exacto ? "El contenido firmado es byte a byte el recibo mostrado."
      : "El contenido firmado coincide con el recibo mostrado.")
    : "El contenido firmado NO coincide con el recibo mostrado.");
  return fin({ header: jws.header, payloadFirmado: firmado, exacto });
}
