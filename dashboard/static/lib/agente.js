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
  atributo: "Atributo",
};

/** Atributos genéricos (Requisito.atributo) con nombre legible. */
export const ATRIBUTOS = {
  memoria_ram: "Memoria RAM", almacenamiento: "Almacenamiento", pantalla: "Pantalla",
  color: "Color", inalambrico: "Inalámbrico", cancelacion_ruido: "Cancelación de ruido",
};

/** Parámetros necesarios del intake (y "mencion:…" para los atributos). */
export const PARAMETROS = {
  categoria: "Producto", cantidad: "Cantidad", precio: "Tope", moneda: "Moneda", plazo: "Plazo",
};
const ESTADO_PARAMETRO = { resuelto: "resuelto", a_confirmar: "a confirmar", faltante: "falta" };
const ORIGEN_PARAMETRO = { dicho: "lo dijiste", inferido: "lo deduje" };

const CAMPOS_COMPRA = [
  "estado", "creada", "contexto_id", "pedido", "pregunta", "requisitos", "contexto",
  "oportunidad", "vence", "decision", "requisitos_autorizados", "intake",
];
// Lo que trae cada turno del intake en pedido_detectado.
const CAMPOS_TURNO = ["faltan", "a_confirmar", "ficha_parcial", "cortado", "mensaje", "fuera_de_catalogo"];

const texto = (v) => (typeof v === "string" ? v : "");
const lista = (v) => (Array.isArray(v) ? v : []);
const objeto = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const idValido = (id) => typeof id === "string" && id.length > 0 && id.length <= MAX_ID;

function compraVacia(compraId) {
  return {
    compra_id: compraId, estado: null, creada: null, contexto_id: null, pedido: "", pregunta: "",
    requisitos: [], contexto: null, oportunidad: null, vence: null, decision: null,
    requisitos_autorizados: null,
    // Intake (proxy con un agente que repregunta): el turno actual, qué falta,
    // la ficha parcial y, al final, la ficha autorizada. null sin intake.
    intake: null,
    // Solo los traen los eventos: un turno por pregunta (turno 0 = el porqué)
    // con su respuesta, y por qué no hubo respuesta al porqué.
    turnos: [],
    omitido: null, vencidoContexto: null,
    ultimoTs: null,
  };
}

/** El turno del intake en que está la compra (0 = el porqué). */
export function turnoActual(compra) {
  const t = compra?.intake?.turno;
  return Number.isInteger(t) && t >= 0 ? t : 0;
}

function turno(c, n) {
  let t = c.turnos.find((x) => x.turno === n);
  if (!t) {
    t = {
      turno: n, pregunta: "", vence: null, confirmar: false, faltan: [], a_confirmar: [],
      ficha_parcial: [], cortado: false, mensaje: null, fuera_de_catalogo: false,
      respuesta: null, omitido: null, vencido: null, confirmada: null,
    };
    c.turnos.push(t);
    c.turnos.sort((a, b) => a.turno - b.turno);
  }
  return t;
}

const numeroTurno = (d) => (Number.isInteger(d.turno) && d.turno >= 0 ? d.turno : 0);

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
    case "pedido_detectado": {
      const n = numeroTurno(d);
      Object.assign(c, {
        estado: "esperando_contexto", creada: c.creada ?? ts ?? null,
        pedido: texto(d.pedido) || c.pedido,
        pregunta: texto(d.pregunta), requisitos: lista(d.requisitos), vence: d.vence ?? null,
      });
      const t = turno(c, n);
      Object.assign(t, { pregunta: texto(d.pregunta), vence: d.vence ?? null, confirmar: d.confirmar === true });
      for (const k of CAMPOS_TURNO) if (d[k] !== undefined) t[k] = d[k];
      t.faltan = lista(t.faltan);
      t.a_confirmar = lista(t.a_confirmar);
      t.ficha_parcial = lista(t.ficha_parcial);
      if (d.turno !== undefined || d.faltan !== undefined) {
        c.intake = { ...(c.intake ?? {}), turno: n, confirmar: t.confirmar, faltan: t.faltan,
          a_confirmar: t.a_confirmar, ficha_parcial: t.ficha_parcial, cortado: t.cortado === true,
          mensaje: t.mensaje ?? null, fuera_de_catalogo: t.fuera_de_catalogo === true };
      }
      break;
    }
    case "contexto_registrado": {
      const n = numeroTurno(d);
      const t = turno(c, n);
      Object.assign(t, {
        respuesta: typeof d.respuesta === "string" ? d.respuesta : null,
        omitido: d.omitido === true, vencido: d.vencido === true, confirmada: d.confirmada === true,
      });
      // Un turno sin contexto (el modelo falló) no borra el de antes.
      const contexto = objeto(d.contexto) ?? (n > 0 ? c.contexto : null);
      Object.assign(c, { estado: "negociando", contexto, vence: null });
      // Cómo terminó el porqué: lo dice el primer turno.
      if (n === 0) Object.assign(c, { omitido: d.omitido === true, vencidoContexto: d.vencido === true });
      if (d.final === true) {
        c.intake = { ...(c.intake ?? {}), turno: n, confirmar: false, final: true,
          confirmada: d.confirmada === true, sin_tiempo: d.sin_tiempo === true,
          ficha_autorizada: lista(d.ficha_autorizada), sin_verificar_guard: lista(d.sin_verificar_guard) };
        const r = objeto(d.intake);
        if (r) Object.assign(c.intake, { faltan: lista(r.faltan), cortado: r.cortado === true, mensaje: r.mensaje ?? null });
      }
      break;
    }
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
  c.intake = objeto(c.intake);
  return c;
}

/** Cuánto avanzó: primero el turno del intake, después el estado. */
function avance(c) {
  return [turnoActual(c), AVANCE[c.estado] ?? -1];
}

function masAvanzada(a, b) {
  const [ta, ea] = avance(a);
  const [tb, eb] = avance(b);
  return ta !== tb ? ta > tb : ea > eb;
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
  const eventosMasNuevos = masAvanzada(ev, proxy);
  const [base, respaldo] = eventosMasNuevos ? [ev, proxy] : [proxy, ev];
  const out = { ...base };
  // Lo que le falte a la base (por ejemplo, el pedido_detectado quedó fuera
  // del historial) se completa con la otra fuente.
  for (const k of ["creada", "contexto_id", "pedido", "pregunta", "contexto", "oportunidad", "intake"]) {
    if (out[k] === null || out[k] === "") out[k] = respaldo[k];
  }
  if (!out.requisitos.length) out.requisitos = respaldo.requisitos;
  // La historia de turnos solo la tienen los eventos.
  out.turnos = ev.turnos;
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

/** "Confirmar la ficha" en el último turno del intake. */
export function cuerpoConfirmar(compraId) {
  return idValido(compraId) ? { compra_id: compraId, confirmar: true } : null;
}

/**
 * Lo que identifica "a qué se contestó": el estado y el turno del intake.
 * Contestar el turno 0 no traba el turno 1 (los dos están esperando_contexto).
 */
export function claveEnvio(compra) {
  return `${compra?.estado ?? ""}:${turnoActual(compra)}`;
}

/** ¿La compra viene de un agente con intake (repregunta y confirma la ficha)? */
export function tieneIntake(compra) {
  const i = compra?.intake;
  return Boolean(i && (lista(i.ficha_parcial).length || i.final === true));
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
  const campo = q?.campo === "atributo"
    ? ATRIBUTOS[q?.atributo] ?? String(q?.atributo ?? "Atributo")
    : CAMPOS[q?.campo] ?? String(q?.campo ?? "—");
  const unidad = typeof q?.unidad === "string" && q.unidad ? ` ${q.unidad}` : "";
  const valor = formatearValor(q?.campo, q?.valor) + unidad;
  let condicion;
  if (q?.operador === "maximo") condicion = `≤ ${valor}`;
  else if (q?.operador === "minimo") condicion = `≥ ${valor}`;
  else if (q?.operador === "incluye") condicion = `incluye ${valor}`;
  else if (q?.operador === "cubre") condicion = formatearEntregasAcumuladas(q?.valor);
  else condicion = valor;
  const salida = { campo, condicion, obligatorio: q?.obligatorio !== false };
  // Solo cuando lo es: el resto de las fichas (scenarios.json) no trae origen.
  if (q?.origen === "inferido") salida.inferido = true;
  return salida;
}

/**
 * ¿Dos listas de requisitos piden lo mismo? (campo, atributo, operador, valor,
 * unidad y obligatorio; el origen no cambia lo que se verifica).
 */
export function mismosRequisitos(a, b) {
  const clave = (q) => JSON.stringify([q?.campo, q?.atributo ?? null, q?.operador, q?.valor,
    q?.unidad ?? null, q?.obligatorio !== false]);
  const ca = lista(a).map(clave).sort();
  const cb = lista(b).map(clave).sort();
  return ca.length === cb.length && ca.every((x, i) => x === cb[i]);
}

/**
 * Un parámetro de la ficha parcial del intake en palabras:
 * {nombre: "Tope", valor: "150", estado: "resuelto" | "a confirmar" | "falta",
 *  origen: "lo dijiste" | "lo deduje" | null, obligatorio}.
 */
export function describirParametro(p) {
  const necesario = p?.necesario !== false;
  const falta = p?.estado === "faltante";
  // Una mención con requisito se nombra por él: "Marca · Nike".
  const req = !necesario && objeto(p?.requisito) ? describirRequisito(p.requisito) : null;
  const nombre = req?.campo ?? (necesario ? PARAMETROS[p?.parametro] ?? String(p?.parametro ?? "—") : "Atributo");
  let valor = falta || !texto(p?.texto) ? "—" : texto(p.texto);
  if (req && !falta) valor = req.condicion;
  return {
    nombre,
    valor,
    estado: ESTADO_PARAMETRO[p?.estado] ?? String(p?.estado ?? "—"),
    origen: falta ? null : ORIGEN_PARAMETRO[p?.origen] ?? null,
    obligatorio: p?.requisito ? p.requisito.obligatorio !== false : true,
  };
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
