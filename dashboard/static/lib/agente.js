// Nuestro agente (etapa 2, docs/FLUJO.md): cada compra con su pedido, el
// porqué que contestó el usuario y la oportunidad que se le consulta.
// Se arma con los eventos del proxy (SSE o replay) y, en vivo, con
// GET /guard/compras. Sin DOM: se testea con node --test.

export const MAX_RESPUESTA = 1000;
const MAX_ID = 200;

/** Tipos de `Evento` de nuestro agente. */
export const TIPOS_AGENTE = new Set([
  "pedido_detectado", "contexto_registrado", "oportunidad", "oportunidad_decidida",
]);

/** Estados en los que la compra espera algo del usuario. */
export const ESPERAN_AL_USUARIO = new Set(["esperando_contexto", "esperando_decision"]);
// `bloqueada` (el filtro de seguridad cortó la conversación) no tiene evento
// propio de nuestro agente: solo la trae GET /guard/compras.
export const FINALES = new Set(["sin_oportunidad", "aceptada", "rechazada", "vencida", "bloqueada"]);

// Cuánto avanzó cada estado. Una compra solo avanza: si los eventos muestran
// un estado más avanzado que el último sondeo, el sondeo quedó viejo.
const AVANCE = {
  esperando_contexto: 0, negociando: 1, esperando_decision: 2,
  sin_oportunidad: 2, aceptada: 3, rechazada: 3, vencida: 3, bloqueada: 3,
};

export const TEXTO_ESTADO = {
  esperando_contexto: "Esperando tu respuesta",
  negociando: "Negociando",
  esperando_decision: "Esperando tu decisión",
  sin_oportunidad: "Sin oportunidad: sigue el pedido original",
  aceptada: "Aceptada",
  rechazada: "Rechazada",
  vencida: "Vencida: no se compra",
  bloqueada: "Bloqueada por seguridad",
};

/** Lo que el usuario ve cuando la compra terminó en cada estado final. */
export const TEXTO_FINAL = {
  aceptada: "Parámetros nuevos enviados al agente del cliente",
  rechazada: "Se mantiene el pedido original",
  vencida: "Sin respuesta: no se compra nada. El silencio no es autorización",
  sin_oportunidad: "La negociación siguió con el pedido original",
  bloqueada: "Bloqueado por seguridad: no se buscan alternativas",
};

export const CAMPOS = {
  precio: "Precio", moneda: "Moneda", categoria: "Categoría", uso: "Uso", talle: "Talle",
  marca: "Marca", comercio: "Comercio", cantidad: "Cantidad", entregas: "Entregas",
};

const CAMPOS_COMPRA = [
  "estado", "creada", "contexto_id", "pedido", "pregunta", "requisitos", "contexto",
  "oportunidad", "vence", "decision", "requisitos_autorizados",
];

const texto = (v) => (typeof v === "string" ? v : "");
const lista = (v) => (Array.isArray(v) ? v : []);
const objeto = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const idValido = (id) => typeof id === "string" && id.length > 0 && id.length <= MAX_ID;

function compraVacia(compraId) {
  return {
    compra_id: compraId, estado: null, creada: null, contexto_id: null, pedido: "", pregunta: "",
    requisitos: [], contexto: null, oportunidad: null, vence: null, decision: null,
    requisitos_autorizados: null,
    // Solo los traen los eventos (contexto_registrado): por qué no hubo respuesta.
    omitido: null, vencidoContexto: null,
    ultimoTs: null,
  };
}

// ---------------------------------------------------------------------------
// Compras
// ---------------------------------------------------------------------------

/**
 * Compras de nuestro agente armadas con los eventos (ya ordenados por `ts`,
 * ver fusionarEventos). Agrupa por `datos.compra_id`: el `contexto_id` puede
 * faltar, porque el proxy pregunta antes de reenviar la búsqueda.
 */
export function comprasDesdeEventos(eventos) {
  const porId = new Map();
  for (const e of eventos || []) {
    if (!TIPOS_AGENTE.has(e?.tipo)) continue;
    const d = objeto(e.datos) ?? {};
    if (!idValido(d.compra_id)) continue;
    const c = porId.get(d.compra_id) ?? compraVacia(d.compra_id);
    porId.set(d.compra_id, c);
    c.contexto_id = c.contexto_id ?? e.contexto_id ?? null;
    c.ultimoTs = e.ts ?? c.ultimoTs;
    aplicarEvento(c, e.tipo, d, e.ts);
  }
  return ordenar([...porId.values()]);
}

function aplicarEvento(c, tipo, d, ts) {
  switch (tipo) {
    case "pedido_detectado":
      Object.assign(c, {
        estado: "esperando_contexto", creada: c.creada ?? ts ?? null, pedido: texto(d.pedido),
        pregunta: texto(d.pregunta), requisitos: lista(d.requisitos), vence: d.vence ?? null,
      });
      break;
    case "contexto_registrado":
      Object.assign(c, {
        estado: "negociando", contexto: objeto(d.contexto), vence: null,
        omitido: d.omitido === true, vencidoContexto: d.vencido === true,
      });
      break;
    case "oportunidad":
      Object.assign(c, { estado: "esperando_decision", oportunidad: objeto(d.oportunidad), vence: d.vence ?? null });
      break;
    case "oportunidad_decidida":
      Object.assign(c, {
        estado: FINALES.has(d.decision) ? d.decision : c.estado,
        decision: d.decision ?? null,
        requisitos_autorizados: Array.isArray(d.requisitos_autorizados) ? d.requisitos_autorizados : null,
        vence: null,
      });
      break;
    default:
  }
}

/** Una compra del proxy (GET /guard/compras) con los campos que usa la vista. */
export function normalizarCompra(p) {
  const c = compraVacia(p.compra_id);
  for (const k of CAMPOS_COMPRA) if (p[k] !== undefined) c[k] = p[k];
  c.requisitos = lista(c.requisitos);
  c.pedido = texto(c.pedido);
  c.pregunta = texto(c.pregunta);
  return c;
}

/**
 * Une lo que dicen los eventos con lo que devolvió el proxy. El proxy es la
 * fuente de verdad (trae `sin_oportunidad`, que no tiene evento), salvo que
 * los eventos muestren un estado más avanzado: el sondeo es de hasta 4 s
 * atrás y el SSE llega enseguida. Supone que una compra solo avanza y tiene
 * una oportunidad por vez.
 */
export function fusionarCompras(desdeEventos, delProxy) {
  const porId = new Map((desdeEventos || []).map((c) => [c.compra_id, c]));
  for (const p of delProxy || []) {
    if (!objeto(p) || !idValido(p.compra_id)) continue;
    const proxy = normalizarCompra(p);
    const ev = porId.get(p.compra_id);
    porId.set(p.compra_id, ev ? fusionarUna(ev, proxy) : proxy);
  }
  return ordenar([...porId.values()]);
}

function fusionarUna(ev, proxy) {
  const eventosMasNuevos = (AVANCE[ev.estado] ?? -1) > (AVANCE[proxy.estado] ?? -1);
  const [base, respaldo] = eventosMasNuevos ? [ev, proxy] : [proxy, ev];
  const out = { ...base };
  // Lo que le falte a la base (por ejemplo, el pedido_detectado quedó fuera
  // del historial) se completa con la otra fuente.
  for (const k of ["creada", "contexto_id", "pedido", "pregunta", "contexto", "oportunidad"]) {
    if (out[k] === null || out[k] === "") out[k] = respaldo[k];
  }
  if (!out.requisitos.length) out.requisitos = respaldo.requisitos;
  out.omitido = ev.omitido;
  out.vencidoContexto = ev.vencidoContexto;
  out.ultimoTs = ev.ultimoTs;
  return out;
}

function tiempoCompra(c) {
  const t = Date.parse(c.creada ?? c.ultimoTs);
  return Number.isNaN(t) ? Number.NEGATIVE_INFINITY : t;
}

/** De la más nueva a la más vieja (estable). */
function ordenar(compras) {
  return compras
    .map((c, i) => [c, i])
    .sort((a, b) => tiempoCompra(b[0]) - tiempoCompra(a[0]) || a[1] - b[1])
    .map(([c]) => c);
}

/** Reemplaza (o agrega adelante) la compra que devolvió un POST. No muta. */
export function reemplazarCompra(compras, compra) {
  const actuales = compras || [];
  if (!objeto(compra) || !idValido(compra.compra_id)) return actuales;
  if (!actuales.some((c) => c?.compra_id === compra.compra_id)) return [compra, ...actuales];
  return actuales.map((c) => (c?.compra_id === compra.compra_id ? compra : c));
}

/** La compra elegida, o la más nueva si no hay elegida (o ya no está). */
export function compraElegida(compras, compraId) {
  return (compraId && (compras || []).find((c) => c.compra_id === compraId)) || (compras || [])[0] || null;
}

/** ¿La compra espera una respuesta del usuario y todavía no venció el plazo? */
export function esperaAlUsuario(compra, ahora) {
  if (!ESPERAN_AL_USUARIO.has(compra?.estado)) return false;
  const r = restante(compra.vence, ahora);
  return !r || !r.vencido;
}

/**
 * Cómo terminó el porqué (null mientras se espera la respuesta):
 * - respondido: hay contexto armado con la respuesta del usuario;
 * - armando: el proxy ya tomó la respuesta (negociando) y todavía no llegó
 *   contexto_registrado (nuestro agente lo arma con un LLM);
 * - sin_contexto: el usuario contestó pero nuestro agente no pudo armarlo;
 * - omitido, vencido;
 * - sin_respuesta: no hay más datos (por ejemplo, solo el sondeo del proxy).
 */
export function desenlaceContexto(compra) {
  if (!compra || compra.estado === "esperando_contexto" || compra.estado === null) return null;
  if (typeof compra.contexto?.respuesta === "string" && compra.contexto.respuesta) return "respondido";
  if (compra.omitido) return "omitido";
  if (compra.vencidoContexto) return "vencido";
  // omitido/vencido en false: llegó contexto_registrado sin contexto.
  if (compra.omitido === false && compra.vencidoContexto === false) return "sin_contexto";
  if (compra.estado === "negociando" && !compra.contexto && compra.omitido === null) return "armando";
  return "sin_respuesta";
}

// ---------------------------------------------------------------------------
// Cuerpos de los POST (mismo formato que valida el servidor del panel)
// ---------------------------------------------------------------------------

/** `{compra_id, respuesta}` con la respuesta sin espacios en los bordes, o null. */
export function cuerpoContexto(compraId, respuesta) {
  const r = typeof respuesta === "string" ? respuesta.trim() : "";
  if (!idValido(compraId) || r.length === 0 || r.length > MAX_RESPUESTA) return null;
  return { compra_id: compraId, respuesta: r };
}

export function cuerpoOmitir(compraId) {
  return idValido(compraId) ? { compra_id: compraId, omitir: true } : null;
}

export function cuerpoDecision(compraId, decision) {
  if (!idValido(compraId) || (decision !== "aceptar" && decision !== "rechazar")) return null;
  return { compra_id: compraId, decision };
}

/** Texto para el usuario cuando un POST de nuestro agente falla. */
export function mensajeErrorAgente(status, datos, mensaje = "") {
  if (status === 409) {
    const est = TEXTO_ESTADO[datos?.estado] ?? datos?.estado ?? "?";
    return `La compra ya no está esperando esto (estado actual: ${est}).`;
  }
  if (status === 404) return "El proxy no conoce esta compra (¿se reinició?).";
  if (status === 400) return "El proxy rechazó el formato del pedido.";
  if (status === 503 && datos?.error === "sin_token_del_proxy") {
    return "No se encontró el token del proxy (.temp-db/guard_api_token): ¿está levantado?";
  }
  if (status === 502 && datos?.detalle === "ReadTimeout") {
    return "El proxy no contestó a tiempo. En unos segundos se ve el estado actualizado.";
  }
  if (status === 502 || !status) return "No se pudo hablar con el proxy: ¿está levantado?";
  return `No se pudo enviar: ${datos?.error || mensaje || `HTTP ${status}`}`;
}

// ---------------------------------------------------------------------------
// Formato
// ---------------------------------------------------------------------------

const DIAS = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
const dos = (n) => String(n).padStart(2, "0");

/** "2026-10-19" → "lun 19/10" (el día de la semana de esa fecha, sin zona horaria). */
export function formatearFecha(fecha) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(fecha ?? ""));
  if (!m) return String(fecha ?? "—");
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  if (Number.isNaN(d.getTime()) || d.getUTCDate() !== Number(m[3])) return String(fecha);
  return `${DIAS[d.getUTCDay()]} ${m[3]}/${m[2]}`;
}

const esEntrega = (e) => objeto(e) && "cantidad" in e && "fecha" in e;

/** {cantidad: 10, fecha: "2026-10-19"} → "10 el lun 19/10". */
export function formatearEntrega(e) {
  if (!esEntrega(e)) return JSON.stringify(e);
  return `${e.cantidad} el ${formatearFecha(e.fecha)}`;
}

/** "10 el lun 19/10 y 10 el mié 21/10". */
export function formatearEntregas(entregas) {
  const partes = lista(entregas).map(formatearEntrega);
  if (partes.length <= 1) return partes[0] ?? "—";
  return `${partes.slice(0, -1).join(", ")} y ${partes[partes.length - 1]}`;
}

function numero(n) {
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

/** Un valor de un requisito o de un cambio, según su campo. */
export function formatearValor(campo, valor) {
  if (valor === null || valor === undefined) return "—";
  if (campo === "entregas" && Array.isArray(valor) && valor.every(esEntrega)) return formatearEntregas(valor);
  if (typeof valor === "number" && Number.isFinite(valor)) return numero(valor);
  if (typeof valor === "boolean") return valor ? "sí" : "no";
  if (Array.isArray(valor)) return valor.map((v) => formatearValor(campo, v)).join(", ");
  if (typeof valor === "object") return JSON.stringify(valor);
  return String(valor);
}

/**
 * Entregas de un requisito `cubre`, que son acumuladas (al menos esa
 * cantidad entregada a más tardar en cada fecha):
 * "10 para el lun 19/10 · 20 en total para el mié 21/10".
 */
export function formatearEntregasAcumuladas(entregas) {
  const items = lista(entregas);
  if (!items.length || !items.every(esEntrega)) return formatearValor("entregas", entregas);
  return items.map((e, i) =>
    `${e.cantidad}${i > 0 ? " en total" : ""} para el ${formatearFecha(e.fecha)}`).join(" · ");
}

/**
 * Un requisito en palabras: {campo: "Entregas", condicion: "20 para el lun
 * 19/10", obligatorio}.
 */
export function describirRequisito(q) {
  const campo = CAMPOS[q?.campo] ?? String(q?.campo ?? "—");
  const valor = formatearValor(q?.campo, q?.valor);
  let condicion;
  if (q?.operador === "maximo") condicion = `≤ ${valor}`;
  else if (q?.operador === "incluye") condicion = `incluye ${valor}`;
  else if (q?.operador === "cubre") condicion = formatearEntregasAcumuladas(q?.valor);
  else condicion = valor;
  return { campo, condicion, obligatorio: q?.obligatorio !== false };
}

/**
 * Cuánto falta para `vence` (ISO): {ms, vencido, texto: "m:ss" o "h:mm:ss"}.
 * null si no hay plazo.
 */
export function restante(vence, ahora) {
  const fin = Date.parse(vence ?? "");
  if (Number.isNaN(fin)) return null;
  const ms = fin - ahora;
  if (ms <= 0) return { ms: 0, vencido: true, texto: "0:00" };
  const s = Math.ceil(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return { ms, vencido: false, texto: h ? `${h}:${dos(m)}:${dos(s % 60)}` : `${m}:${dos(s % 60)}` };
}
