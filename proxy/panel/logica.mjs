// Panel del cliente de KaiAI: la lógica pura, sin DOM. Corre en el navegador y
// en Node (`node --test proxy/tests/js/panel.test.mjs`).
//
// Arma las query de /v1 (resumen, listado con filtros y paginación), valida lo
// que va a una URL (el compra_id del hash, el cursor, la referencia) y pasa lo
// que devuelve la API a textos, íconos y tonos. Nada de acá decide nada: el
// panel solo lee el historial con la clave de la empresa.
//
// Todo dato de la API se trata como no confiable: acá solo se eligen textos
// fijos según valores conocidos; lo demás lo pinta vista.mjs con textContent.

// Lo que valida la API (proxy/tipos_gateway.py y proxy/api_v1.py): si no
// cumple, el panel no lo manda.
const ID_COMPRA_RE = /^cmp-[0-9a-f]{16}$/;
const CLAVE_RE = /^psg_[A-Za-z0-9_-]{8,200}$/;
const MONEDA_RE = /^[A-Z]{3}$/;
const CURSOR_RE = /^[A-Za-z0-9_-]{1,200}$/;
// Fecha y hora ISO 8601 en UTC, como las arma isoUtc (sin `+`: no hay que
// codificarlo en la query).
const ISO_UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const CONTROL_RE = /[\u0000-\u001f\u007f-\u009f]/;

export const LARGO_REFERENCIA = 200;
export const LIMITE_PAGINA = 50;
export const LIMITE_MAXIMO = 200;
export const PERIODOS = Object.freeze([7, 30, 90]);
export const PERIODO_POR_DEFECTO = 30;
export const ESTADOS_COMPRA = Object.freeze([
  "creada", "esperando_contexto", "negociando", "esperando_decision", "aceptada", "rechazada",
  "sin_oportunidad", "bloqueada", "vencida",
]);

// ---------------------------------------------------------------------------
// Validaciones y rutas
// ---------------------------------------------------------------------------

export function esIdCompra(valor) {
  return typeof valor === "string" && ID_COMPRA_RE.test(valor);
}

/** ¿Tiene forma de clave `psg_…`? (no dice si vale: eso lo dice la API). */
export function claveValida(clave) {
  return typeof clave === "string" && CLAVE_RE.test(clave);
}

/**
 * La vista del hash: `#/compra/<id>` con un id válido es el detalle; con
 * cualquier otro id, `compra_invalida` (no se le pide nada a la API); sin hash
 * o con otro `#/…`, la lista. Un ancla de la página (`#principal`) es `ancla`:
 * no cambia la vista.
 */
export function rutaDe(hash) {
  const texto = typeof hash === "string" ? hash : "";
  if (texto && texto !== "#" && !texto.startsWith("#/")) return { vista: "ancla" };
  const m = /^#\/compra\/(.*)$/.exec(texto);
  if (!m) return { vista: "lista" };
  return esIdCompra(m[1]) ? { vista: "compra", id: m[1] } : { vista: "compra_invalida" };
}

export function hashDeCompra(id) {
  return esIdCompra(id) ? `#/compra/${id}` : null;
}

/** La referencia del filtro: recortada; "" si no hay; null si no sirve. */
export function referenciaValida(texto) {
  if (typeof texto !== "string") return null;
  const limpia = texto.trim();
  if (limpia.length > LARGO_REFERENCIA || CONTROL_RE.test(limpia)) return null;
  return limpia;
}

/** El `siguiente` de la API, si tiene forma de cursor (si no, no hay más). */
export function siguienteValido(cursor) {
  return typeof cursor === "string" && CURSOR_RE.test(cursor) ? cursor : null;
}

// ---------------------------------------------------------------------------
// Fechas (hora local del navegador, sin Intl: igual en todos lados)
// ---------------------------------------------------------------------------

function dosDigitos(n) {
  return String(n).padStart(2, "0");
}

/** Una fecha en ISO 8601 UTC, sin milisegundos: "2026-10-21T03:04:05Z". */
export function isoUtc(fecha) {
  return fecha.toISOString().replace(/\.\d{3}Z$/, "Z");
}

function medianocheLocal(fecha, dias = 0) {
  return new Date(fecha.getFullYear(), fecha.getMonth(), fecha.getDate() + dias);
}

/**
 * Los últimos `dias` días (7, 30 o 90): desde la medianoche local de hace
 * `dias - 1` días hasta la de mañana (`hasta` no se incluye, como en la API).
 * null si `dias` no es uno de los períodos.
 */
export function ventanaPeriodo(dias, ahora = new Date()) {
  if (!PERIODOS.includes(dias)) return null;
  return {
    desde: isoUtc(medianocheLocal(ahora, -(dias - 1))),
    hasta: isoUtc(medianocheLocal(ahora, 1)),
  };
}

function fechaDe(iso) {
  if (typeof iso !== "string" || !iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? new Date(ms) : null;
}

function dia(fecha) {
  return `${dosDigitos(fecha.getDate())}/${dosDigitos(fecha.getMonth() + 1)}/${fecha.getFullYear()}`;
}

function horaMinutos(fecha) {
  return `${dosDigitos(fecha.getHours())}:${dosDigitos(fecha.getMinutes())}`;
}

/** "21/10/2026 14:05" en la hora local, o "—". */
export function fechaHoraLocal(iso) {
  const fecha = fechaDe(iso);
  return fecha ? `${dia(fecha)} ${horaMinutos(fecha)}` : "—";
}

/** "21/10 14:05" (la línea de tiempo), o "—". */
export function fechaCorta(iso) {
  const fecha = fechaDe(iso);
  return fecha
    ? `${dosDigitos(fecha.getDate())}/${dosDigitos(fecha.getMonth() + 1)} ${horaMinutos(fecha)}`
    : "—";
}

/** "Del 15/10/2026 al 21/10/2026" (`hasta` no se incluye), o "". */
export function textoPeriodo(ventana) {
  const desde = fechaDe(ventana?.desde);
  const hasta = fechaDe(ventana?.hasta);
  if (!desde || !hasta || hasta <= desde) return "";
  return `Del ${dia(desde)} al ${dia(new Date(hasta.getTime() - 1))}`;
}

// ---------------------------------------------------------------------------
// Montos
// ---------------------------------------------------------------------------

/** 2148.5 → "2.148,50" (siempre dos decimales, como los textos de la API). */
export function formatearNumero(valor) {
  if (typeof valor !== "number" || !Number.isFinite(valor)) return "—";
  const negativo = valor < 0;
  const centavos = Math.round(Math.abs(valor) * 100);
  const entero = Math.floor(centavos / 100);
  const miles = String(entero).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return `${negativo && centavos ? "-" : ""}${miles},${dosDigitos(centavos % 100)}`;
}

/** "USD 2.148,50"; una moneda que no es un código ISO no se muestra. */
export function formatearMonto(valor, moneda) {
  const numero = formatearNumero(valor);
  if (numero === "—") return numero;
  return typeof moneda === "string" && MONEDA_RE.test(moneda) ? `${moneda} ${numero}` : numero;
}

function esMonto(monto, moneda) {
  return typeof monto === "number" && Number.isFinite(monto) && typeof moneda === "string"
    && MONEDA_RE.test(moneda);
}

/** El tope que autorizaron (`precio` `maximo` y `moneda` `igual`), o null. */
export function topeDeCompra(compra) {
  const requisitos = Array.isArray(compra?.ficha?.requisitos) ? compra.ficha.requisitos : [];
  const precio = requisitos.find((r) => r?.campo === "precio" && r?.operador === "maximo");
  const moneda = requisitos.find((r) => r?.campo === "moneda" && r?.operador === "igual");
  return esMonto(precio?.valor, moneda?.valor) ? { tope: precio.valor, moneda: moneda.valor } : null;
}

/**
 * Lo que va en la columna Monto: el de la compra (`monto` y `moneda` del
 * listado, con la misma regla que la explicación) o, si no hubo, el tope.
 */
export function montoMostrado(compra) {
  if (esMonto(compra?.monto, compra?.moneda)) {
    return { texto: formatearMonto(compra.monto, compra.moneda), tope: false };
  }
  const tope = topeDeCompra(compra);
  if (tope) return { texto: `Tope ${formatearMonto(tope.tope, tope.moneda)}`, tope: true };
  return { texto: "—", tope: false };
}

// ---------------------------------------------------------------------------
// Resultado, alertas, chequeos y etapas: textos fijos, ícono y tono
// ---------------------------------------------------------------------------

// tono: ok | aviso | crit | info | neutro (los colores de demo.css). Cada uno
// tiene además su ícono y su texto: nada se dice solo con el color.
export const RESULTADOS = Object.freeze({
  aprobada: { texto: "Aprobada", tono: "ok", icono: "check", descripcion: "Llegó a la firma." },
  frenada: { texto: "Frenada", tono: "crit", icono: "freno", descripcion: "KaiAI la frenó antes de la firma." },
  retenida: { texto: "Retenida", tono: "aviso", icono: "pausa", descripcion: "Espera que una persona la apruebe." },
  cortada_por_seguridad: {
    texto: "Cortada por seguridad", tono: "crit", icono: "escudo",
    descripcion: "El filtro de seguridad cortó la conversación con el comercio.",
  },
  vencida: { texto: "Vencida", tono: "neutro", icono: "reloj", descripcion: "Nadie decidió a tiempo una propuesta." },
  en_curso: { texto: "En curso", tono: "info", icono: "curso", descripcion: "Todavía no hubo un checkout." },
  sin_compra: { texto: "Sin compra", tono: "neutro", icono: "guion", descripcion: "Pasaron 24 h sin ningún checkout." },
});

export function infoResultado(resultado) {
  return Object.hasOwn(RESULTADOS, resultado)
    ? RESULTADOS[resultado]
    : { texto: "Sin resultado", tono: "neutro", icono: "guion", descripcion: "" };
}

// El orden es el de la API (proxy/alertas.py: ALERTAS).
export const ALERTAS = Object.freeze({
  manipulacion: { texto: "Intentaron manipular a tu agente", corto: "Manipulación", estafa: true },
  dato_falso: { texto: "Datos falsos del comercio", corto: "Dato falso", estafa: true },
  precio_distinto: { texto: "Precio distinto al publicado", corto: "Precio distinto", estafa: true },
  checkout_alterado: { texto: "Checkout alterado", corto: "Checkout alterado", estafa: true },
  no_cumple: { texto: "No cumple lo que pediste", corto: "No cumple", estafa: false },
  sin_verificar: { texto: "Hay datos que no pudimos verificar", corto: "Sin verificar", estafa: false },
});

/** `{id, texto, corto, estafa, tono, icono}` o null si la API manda otra cosa. */
export function infoAlerta(alerta) {
  if (!Object.hasOwn(ALERTAS, alerta)) return null;
  const info = ALERTAS[alerta];
  return { id: alerta, ...info, tono: info.estafa ? "crit" : "neutro", icono: info.estafa ? "alerta" : "info" };
}

/** Las alertas conocidas de una lista, sin repetir y en el orden de la API. */
export function alertasDe(lista) {
  const presentes = new Set(Array.isArray(lista) ? lista : []);
  return Object.keys(ALERTAS).filter((a) => presentes.has(a)).map(infoAlerta);
}

/** `{estafa, avisos}`: las cuatro de estafa y los avisos neutros. */
export function separarAlertas(lista) {
  const todas = alertasDe(lista);
  return { estafa: todas.filter((a) => a.estafa), avisos: todas.filter((a) => !a.estafa) };
}

export const CHEQUEOS = Object.freeze({
  ok: { texto: "Bien", tono: "ok", icono: "check" },
  aviso: { texto: "Aviso", tono: "aviso", icono: "info" },
  escalar: { texto: "A revisar", tono: "aviso", icono: "pregunta" },
  bloquea: { texto: "Frena", tono: "crit", icono: "freno" },
});

export function infoChequeo(resultado) {
  return Object.hasOwn(CHEQUEOS, resultado)
    ? CHEQUEOS[resultado]
    : { texto: "Sin dato", tono: "neutro", icono: "guion" };
}

/** Los chequeos de la explicación que traen texto: `{resultado, texto, info}`. */
export function chequeosDe(lista) {
  return (Array.isArray(lista) ? lista : [])
    .filter((c) => c && typeof c === "object" && typeof c.texto === "string" && c.texto)
    .map((c) => ({ resultado: c.resultado, texto: c.texto, info: infoChequeo(c.resultado) }));
}

const ETAPAS = Object.freeze({
  pedido: "Pedido", contexto: "Para qué", busqueda: "Búsqueda", ofertas: "Ofertas",
  seguridad: "Seguridad", intencion: "Intención", checkout: "Checkout", verificacion: "Verificación",
  aprobacion: "Aprobación", resultado: "Resultado", omitido: "Omitido",
});

export function nombreEtapa(etapa) {
  return Object.hasOwn(ETAPAS, etapa) ? ETAPAS[etapa] : "Paso";
}

/** La línea de tiempo de la explicación: `{ts, cuando, etapa, nombre, texto, omitido}`. */
export function lineaDeTiempo(linea) {
  return (Array.isArray(linea) ? linea : [])
    .filter((e) => e && typeof e === "object" && typeof e.texto === "string" && e.texto)
    .map((e) => {
      const fecha = fechaDe(e.ts);
      return {
        ts: fecha ? fecha.toISOString() : null,
        cuando: fecha ? fechaCorta(e.ts) : "—",
        etapa: typeof e.etapa === "string" ? e.etapa : "",
        nombre: nombreEtapa(e.etapa),
        texto: e.texto,
        omitido: e.etapa === "omitido",
      };
    });
}

// ---------------------------------------------------------------------------
// Query de /v1
// ---------------------------------------------------------------------------

function query(pares) {
  const params = new URLSearchParams();
  for (const [nombre, valor] of pares) if (valor !== null && valor !== undefined && valor !== "") params.append(nombre, valor);
  const texto = params.toString();
  return texto ? `?${texto}` : "";
}

function isoValido(valor) {
  return typeof valor === "string" && ISO_UTC_RE.test(valor) ? valor : null;
}

/**
 * `GET /v1/compras`: `limite`, `desde`, `hasta`, `estado`, `referencia`,
 * `alerta`, `resultado`, `intento_de_estafa` y `cursor`. Lo que no tiene la
 * forma que acepta la API no viaja.
 */
export function queryListado({
  desde, hasta, referencia, alerta, estado, cursor, limite, resultado, intento_de_estafa,
} = {}) {
  const ref = referenciaValida(referencia ?? "");
  const tope = Number.isInteger(limite) && limite >= 1 && limite <= LIMITE_MAXIMO ? limite : LIMITE_PAGINA;
  return query([
    ["limite", String(tope)],
    ["desde", isoValido(desde)],
    ["hasta", isoValido(hasta)],
    ["estado", ESTADOS_COMPRA.includes(estado) ? estado : null],
    ["referencia", ref || null],
    ["alerta", Object.hasOwn(ALERTAS, alerta) ? alerta : null],
    ["resultado", Object.hasOwn(RESULTADOS, resultado) ? resultado : null],
    ["intento_de_estafa", typeof intento_de_estafa === "boolean" ? String(intento_de_estafa) : null],
    ["cursor", siguienteValido(cursor)],
  ]);
}

/** `GET /v1/resumen`: solo `desde` y `hasta`. */
export function queryResumen({ desde, hasta } = {}) {
  return query([["desde", isoValido(desde)], ["hasta", isoValido(hasta)]]);
}

// El filtro "Alerta" tiene además "Cualquier intento de estafa", que en la API
// no es una alerta sino `intento_de_estafa=true`.
export const FILTRO_ESTAFA = "estafa";

/**
 * Lo que eligió el usuario en los filtros (`{resultado, alerta, referencia}`,
 * los valores de los select y del campo), en parámetros de `queryListado`.
 */
export function filtrosDeLaApi(filtros) {
  const f = filtros && typeof filtros === "object" ? filtros : {};
  const estafa = f.alerta === FILTRO_ESTAFA;
  return {
    resultado: Object.hasOwn(RESULTADOS, f.resultado) ? f.resultado : null,
    alerta: !estafa && Object.hasOwn(ALERTAS, f.alerta) ? f.alerta : null,
    intento_de_estafa: estafa ? true : null,
    referencia: referenciaValida(f.referencia ?? "") ?? "",
  };
}

// ---------------------------------------------------------------------------
// Paginación
// ---------------------------------------------------------------------------

/** Las compras de antes y las nuevas, sin repetir y solo con un id válido. */
export function combinarCompras(actuales, nuevas) {
  const salida = [];
  const vistos = new Set();
  for (const lista of [actuales, nuevas]) {
    for (const c of Array.isArray(lista) ? lista : []) {
      if (!c || typeof c !== "object" || !esIdCompra(c.compra_id) || vistos.has(c.compra_id)) continue;
      vistos.add(c.compra_id);
      salida.push(c);
    }
  }
  return salida;
}

// ---------------------------------------------------------------------------
// Resumen
// ---------------------------------------------------------------------------

function cuenta(valor) {
  return Number.isInteger(valor) && valor >= 0 ? valor : null;
}

function textoCuenta(valor) {
  const n = cuenta(valor);
  return n === null ? "—" : String(n);
}

/** Las tarjetas de arriba: `{clave, titulo, valor, tono, icono, detalle?, lineas?}`. */
export function tarjetasResumen(resumen) {
  const r = resumen && typeof resumen === "object" ? resumen : {};
  const por = r.por_resultado && typeof r.por_resultado === "object" ? r.por_resultado : {};
  const frenadas = cuenta(por.frenada ?? 0);
  const retenidas = cuenta(por.retenida ?? 0);
  const sumadas = frenadas === null || retenidas === null ? null : frenadas + retenidas;
  const estafas = cuenta(r.intentos_de_estafa);
  const lineas = (Array.isArray(r.monto_frenado) ? r.monto_frenado : [])
    .filter((m) => m && esMonto(m.total, m.moneda))
    .map((m) => formatearMonto(m.total, m.moneda));
  return [
    { clave: "compras", titulo: "Compras", valor: textoCuenta(r.compras), tono: "neutro", icono: "carro" },
    { clave: "aprobadas", titulo: "Aprobadas", valor: textoCuenta(por.aprobada ?? 0), tono: "ok", icono: "check" },
    {
      clave: "frenadas", titulo: "Frenadas o retenidas", valor: textoCuenta(sumadas), tono: "aviso", icono: "freno",
      detalle: frenadas === null || retenidas === null ? ""
        : `${frenadas} ${frenadas === 1 ? "frenada" : "frenadas"} · ${retenidas} ${retenidas === 1 ? "retenida" : "retenidas"}`,
    },
    {
      clave: "cortadas", titulo: "Cortadas por seguridad", valor: textoCuenta(por.cortada_por_seguridad ?? 0),
      tono: "crit", icono: "escudo",
    },
    {
      clave: "estafas", titulo: "Intentos de estafa", valor: textoCuenta(estafas),
      tono: estafas === 0 ? "ok" : "crit", icono: "alerta",
    },
    {
      clave: "monto", titulo: "Monto frenado", valor: lineas[0] ?? "Nada", lineas, tono: "neutro", icono: "moneda",
      detalle: lineas.length > 1 ? `y ${lineas.length - 1} ${lineas.length === 2 ? "moneda más" : "monedas más"}` : "",
    },
  ];
}

/** Un aviso si el período tiene más compras de las que cuenta la API. */
export function notaResumen(resumen) {
  return resumen?.completo === false
    ? "El período tiene más de 5000 compras: contamos las más nuevas. Achicá el período para ver todo."
    : "";
}

// ---------------------------------------------------------------------------
// Clave, errores y recibo
// ---------------------------------------------------------------------------

/**
 * Qué clave es, por `GET /v1/clave` (`{integrador, kid, permisos}`): con el
 * permiso `lectura`, de solo lectura; sin permisos, completa. Cualquier otra
 * cosa: no se sabe (el panel no dice nada).
 */
export function tipoDeClave(clave) {
  const permisos = clave && typeof clave === "object" ? clave.permisos : null;
  if (!Array.isArray(permisos) || !permisos.every((x) => typeof x === "string")) return "desconocida";
  if (permisos.length === 0) return "completa";
  return permisos.length === 1 && permisos[0] === "lectura" ? "lectura" : "desconocida";
}

/** Un error de /v1 (`{"error": {codigo, mensaje, detalle}}`) en una línea. */
export function mensajeErrorApi(status, cuerpo) {
  const error = cuerpo && typeof cuerpo === "object" ? cuerpo.error : null;
  if (error && typeof error === "object") {
    const codigo = typeof error.codigo === "string" ? error.codigo : "error";
    const mensaje = typeof error.mensaje === "string" ? error.mensaje : "";
    const campos = Array.isArray(error.detalle?.campos) ? error.detalle.campos : [];
    const detalle = campos
      .filter((c) => c && typeof c === "object")
      .slice(0, 5)
      .map((c) => `${c.campo}: ${c.problema}`)
      .join("; ");
    return `${mensaje || "Error"} (${codigo}, HTTP ${status})${detalle ? `: ${detalle}` : ""}`;
  }
  if (typeof error === "string") return `${error} (HTTP ${status})`;
  return `HTTP ${status}`;
}

/** El mensaje de la entrada (y de una clave que deja de valer). */
export function mensajeEntrada(status, cuerpo) {
  if (status === 401) return "La clave no es válida o está dada de baja.";
  if (status === 0) return "No se pudo hablar con KaiAI. Revisá la conexión y probá de nuevo.";
  if (status === 429) return "Demasiados pedidos: esperá un momento y probá de nuevo.";
  if (status === 503) return `KaiAI no está disponible ahora (${mensajeErrorApi(status, cuerpo)}).`;
  return `No se pudo entrar: ${mensajeErrorApi(status, cuerpo)}.`;
}

/**
 * El recibo a verificar (`{id, jws, kid}`): el del veredicto de la compra
 * (`GET /v1/compras/{id}`) cuyo id es el de la explicación (que no trae el JWS).
 */
export function reciboParaVerificar(compra, recibo) {
  const id = recibo && typeof recibo === "object" ? recibo.id : null;
  if (typeof id !== "string" || !id) return null;
  const veredictos = Array.isArray(compra?.veredictos) ? compra.veredictos : [];
  const v = veredictos.find((x) => x?.recibo && typeof x.recibo === "object" && x.recibo.id === id);
  if (!v || typeof v.recibo.jws !== "string" || !v.recibo.jws) return null;
  return { id, jws: v.recibo.jws, kid: v.recibo.kid };
}

/** El resultado de `verificarRecibo` (recibo.mjs) en una frase. */
export function textoVerificacion(resultado) {
  if (resultado?.ok === true) {
    return { tono: "ok", texto: "Firma válida: este recibo lo firmó KaiAI y nadie lo cambió (ES256, verificado en tu navegador)." };
  }
  const motivo = typeof resultado?.motivo === "string" && resultado.motivo ? resultado.motivo : "no se pudo verificar";
  return { tono: "crit", texto: `Firma inválida: ${motivo}.` };
}

// ---------------------------------------------------------------------------
// El detalle
// ---------------------------------------------------------------------------

/**
 * Los datos de la compra para el detalle, todos de la explicación (`GET
 * /v1/compras/{id}/explicacion`): `[{nombre, valor, mono?}]`. Son de la
 * empresa (pedido, referencia, comercio) o de KaiAI (fecha, monto, tope, id);
 * nada del comercio. Sin monto (no hubo checkout), solo el tope.
 */
export function datosDeCompra(explicacion) {
  const e = explicacion && typeof explicacion === "object" ? explicacion : {};
  const datos = [];
  const texto = (v) => (typeof v === "string" && v.trim() ? v : null);
  if (texto(e.pedido)) datos.push({ nombre: "Pedido", valor: e.pedido });
  if (texto(e.referencia)) datos.push({ nombre: "Referencia", valor: e.referencia });
  if (texto(e.comercio)) datos.push({ nombre: "Comercio", valor: e.comercio });
  if (fechaDe(e.creada)) datos.push({ nombre: "Fecha", valor: fechaHoraLocal(e.creada) });
  if (esMonto(e.monto, e.moneda)) datos.push({ nombre: "Monto", valor: formatearMonto(e.monto, e.moneda) });
  if (esMonto(e.tope?.precio, e.tope?.moneda)) {
    datos.push({ nombre: "Tope autorizado", valor: formatearMonto(e.tope.precio, e.tope.moneda) });
  }
  if (esIdCompra(e.compra_id)) datos.push({ nombre: "Compra", valor: e.compra_id, mono: true });
  return datos;
}

// ---------------------------------------------------------------------------
// Tema
// ---------------------------------------------------------------------------

export const TEMAS = Object.freeze(["auto", "light", "dark"]);
export const NOMBRE_TEMA = Object.freeze({
  auto: "Tema: automático", light: "Tema: claro", dark: "Tema: oscuro",
});

export function siguienteTema(tema) {
  const i = TEMAS.indexOf(tema);
  return i < 0 ? "light" : TEMAS[(i + 1) % TEMAS.length];
}
