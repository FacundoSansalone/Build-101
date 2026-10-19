// Eventos del proxy (contrato `Evento`) → compras agrupadas por contexto_id.
// Sin DOM: se testea con node --test.

import { CAMPOS, PARAMETROS, TEXTO_FINAL, formatearValor, nombreOferta } from "./agente.js";

const TS_INVALIDO = Number.POSITIVE_INFINITY;

function tiempo(evento) {
  const t = Date.parse(evento?.ts);
  return Number.isNaN(t) ? TS_INVALIDO : t;
}

/** Une eventos sin duplicar por `id` y los ordena por `ts` (estable). */
export function fusionarEventos(existentes, nuevos) {
  const vistos = new Set();
  const todos = [];
  for (const e of [...(existentes || []), ...(nuevos || [])]) {
    if (!e || typeof e !== "object") continue;
    const clave = e.id ?? `${e.ts}|${e.tipo}|${e.contexto_id}`;
    if (vistos.has(clave)) continue;
    vistos.add(clave);
    todos.push(e);
  }
  return todos
    .map((e, i) => [e, i])
    .sort((a, b) => tiempo(a[0]) - tiempo(b[0]) || a[1] - b[1])
    .map(([e]) => e);
}

// Una búsqueda sin contexto se asocia a la compra que empieza después, si
// empieza dentro de esta ventana.
export const VENTANA_BUSQUEDA_MS = 60_000;

/**
 * Agrupa por `contexto_id` (una compra). Devuelve las compras de la más
 * reciente a la más vieja, y aparte los eventos sin contexto (interruptor,
 * errores del proxy).
 *
 * El proxy actual manda `busqueda_enviada` con su `contexto_id` (el del pedido
 * o el de la respuesta del comercio). Solo para registros viejos que llegan
 * con `contexto_id: null` queda el respaldo: se asocia a la primera compra
 * del mismo escenario que empieza después, dentro de VENTANA_BUSQUEDA_MS, y
 * que todavía no tiene su búsqueda. La vista lo marca como asociado por tiempo.
 */
export function agruparCompras(eventos) {
  const porContexto = new Map();
  const sueltos = [];
  for (const e of eventos || []) {
    if (!e.contexto_id) {
      sueltos.push(e);
      continue;
    }
    if (!porContexto.has(e.contexto_id)) porContexto.set(e.contexto_id, []);
    porContexto.get(e.contexto_id).push(e);
  }

  const inicios = [...porContexto.entries()]
    .map(([id, evs]) => ({ id, inicio: tiempo(evs[0]), escenario: evs.find((x) => x.escenario)?.escenario ?? null }))
    .sort((a, b) => a.inicio - b.inicio);
  // Las compras que ya trajeron su búsqueda con contexto no entran al respaldo.
  const conBusqueda = new Set([...porContexto.entries()]
    .filter(([, evs]) => evs.some((x) => x.tipo === "busqueda_enviada"))
    .map(([id]) => id));
  const sistema = [];
  for (const e of sueltos) {
    if (e.tipo !== "busqueda_enviada") {
      sistema.push(e);
      continue;
    }
    const t = tiempo(e);
    const destino = inicios.find((c) => !conBusqueda.has(c.id) && c.inicio >= t &&
      c.inicio - t <= VENTANA_BUSQUEDA_MS && (!e.escenario || !c.escenario || c.escenario === e.escenario));
    if (!destino) {
      sistema.push(e);
      continue;
    }
    conBusqueda.add(destino.id);
    porContexto.get(destino.id).unshift(e);
  }

  const compras = [...porContexto.entries()].map(([id, evs]) => armarCompra(id, evs));
  compras.sort((a, b) => b.ultimoTs - a.ultimoTs);
  return { compras, sistema };
}

function ultimoDeTipo(eventos, tipo) {
  for (let i = eventos.length - 1; i >= 0; i--) if (eventos[i].tipo === tipo) return eventos[i];
  return null;
}

export function armarCompra(contextoId, eventos) {
  const datos = (tipo) => ultimoDeTipo(eventos, tipo)?.datos ?? null;
  const checkoutEvento = ultimoDeTipo(eventos, "checkout_recibido");
  const veredictoEvento = ultimoDeTipo(eventos, "veredicto");
  // El estado del interruptor que importa es el del momento de la decisión.
  const decisivo = veredictoEvento || checkoutEvento || eventos[eventos.length - 1];
  const escenario = [...eventos].reverse().find((e) => e.escenario)?.escenario ?? null;
  const ofertas = datos("ofertas_recibidas");
  const datosVeredicto = veredictoEvento?.datos ?? null;
  return {
    contextoId,
    eventos,
    escenario,
    guardActivo: typeof decisivo?.guard_activo === "boolean" ? decisivo.guard_activo : null,
    inicioTs: tiempo(eventos[0]),
    ultimoTs: tiempo(eventos[eventos.length - 1]),
    busqueda: datos("busqueda_enviada")?.catalog_search ?? null,
    ofertas: ofertas
      ? { carts: ofertas.carts || [], offer_details: ofertas.offer_details || [] }
      : null,
    checkoutPedido: datos("checkout_pedido")?.cart_id ?? null,
    checkout: checkoutEvento?.datos ?? null,
    veredicto: datos("veredicto")?.veredicto ?? null,
    // Fuente del catálogo que usó el Guard ("http" = :8011, "fixture" = copia
    // local de la demo, "ninguno" = sin catálogo público: solo datos
    // declarados) y si el recibo quedó firmado. Solo si el proxy los manda.
    fuenteCatalogo: typeof datosVeredicto?.fuente === "string" ? datosVeredicto.fuente : null,
    reciboFirmado: typeof datosVeredicto?.recibo_firmado === "boolean" ? datosVeredicto.recibo_firmado : null,
    // Comercio que firmó el checkout y, si hubo, por qué no se usó el catálogo.
    comercioFirmado: typeof datosVeredicto?.comercio_id === "string" ? datosVeredicto.comercio_id : null,
    catalogoDescartado: typeof datosVeredicto?.catalogo_descartado === "string"
      ? datosVeredicto.catalogo_descartado : null,
    bloqueo: datos("bloqueado"),
    desenlace: desenlaceCheckout(eventos),
    checkoutTs: checkoutEvento ? tiempo(checkoutEvento) : null,
    reenviados: eventos.filter((e) => e.tipo === "reenviado"),
    errores: eventos.filter((e) => e.tipo === "error").map((e) => e.datos?.mensaje ?? ""),
  };
}

/** cart_id elegido por el agente: el del checkout (paso 6 del flujo AP2). */
export function cartIdElegido(compra) {
  return compra?.checkout?.cart_id ?? compra?.checkoutPedido ??
    compra?.veredicto?.cart_id_elegido ?? compra?.bloqueo?.cart_id ?? null;
}

export function cartDe(compra, cartId) {
  if (!compra?.ofertas || !cartId) return { cart: null, detalle: null };
  return {
    cart: compra.ofertas.carts.find((c) => c.cart_id === cartId) ?? null,
    detalle: compra.ofertas.offer_details.find((d) => d.cart_id === cartId) ?? null,
  };
}

/** Mandatos que vio el proxy al reenviar el pago (si los registró). */
export function mandatosDe(compra) {
  const out = [];
  for (const r of compra?.reenviados || []) {
    const m = r.datos?.mandatos;
    const metodo = r.datos?.metodo ?? null;
    if (!m) continue;
    if (typeof m === "string") out.push({ nombre: "mandato", token: m, metodo });
    else if (Array.isArray(m)) m.forEach((t, i) => out.push({ nombre: `mandato ${i + 1}`, token: t, metodo }));
    else if (typeof m === "object") {
      for (const [nombre, token] of Object.entries(m)) out.push({ nombre, token, metodo });
    }
  }
  return out;
}

/** Nombre legible de la clave A2A de un mandato. */
export function nombreMandato(clave) {
  const c = String(clave).toLowerCase();
  if (c.includes("checkout")) return "CheckoutMandate";
  if (c.includes("payment")) return "PaymentMandate";
  return String(clave);
}

/**
 * El último desenlace de un pedido de checkout en la compra: `bloqueado`
 * (bloqueado o retenido) o `reenviado` de create_checkout. Una compra puede
 * tener varios intentos (retenido → aprobado por humano → reintento).
 */
export function desenlaceCheckout(eventos) {
  for (let i = (eventos || []).length - 1; i >= 0; i--) {
    const e = eventos[i];
    if (e.tipo === "bloqueado") return e;
    if (e.tipo === "reenviado" && e.datos?.metodo === "create_checkout") return e;
  }
  return null;
}

const esRetencion = (e) => e?.tipo === "bloqueado" && e.datos?.requiere_aprobacion === true;
const esAprobacionHumana = (e) => e?.tipo === "reenviado" && e.datos?.aprobacion_humana === true;

/**
 * En qué terminó la compra, desde el punto de vista de la firma:
 * - corte_seguridad: el filtro cortó la conversación (bloqueado, etapa seguridad).
 * - sin_autorizacion: venció la consulta de nuestro agente (bloqueado, etapa intencion).
 * - retenido: el Guard escaló y el proxy retuvo el checkout; decide un humano.
 * - bloqueado: el proxy cortó antes de que el usuario firme.
 * - firmado: el pago se reenvió con mandatos firmados por el usuario.
 * - aprobado_humano: un humano aprobó el escalado y el reintento pasó intacto.
 * - para_firmar: el checkout firmado llegó intacto al agente.
 */
export function estadoCompra(compra) {
  const fin = compra.desenlace ?? desenlaceCheckout(compra.eventos);
  if (esRetencion(fin)) return { clave: "retenido", texto: "Retenido — esperando decisión humana" };
  // Etapa 2: cortes antes del checkout (el filtro, o la consulta que venció).
  if (fin?.tipo === "bloqueado" && fin.datos?.etapa === "seguridad") {
    return { clave: "corte_seguridad", texto: "Bloqueado por seguridad: conversación cortada" };
  }
  if (fin?.tipo === "bloqueado" && fin.datos?.etapa === "intencion") {
    return { clave: "sin_autorizacion", texto: "La consulta venció: no se compra nada" };
  }
  if (fin?.tipo === "bloqueado") return { clave: "bloqueado", texto: "Bloqueado antes de la firma" };
  if (mandatosDe(compra).length > 0) return { clave: "firmado", texto: "El usuario firmó los mandatos" };
  const metodo = (r) => String(r.datos?.metodo ?? "");
  if (compra.reenviados.some((r) => /payment|pago/i.test(metodo(r)))) {
    return { clave: "firmado", texto: "Pago reenviado al comercio" };
  }
  if (esAprobacionHumana(fin)) {
    return { clave: "aprobado_humano", texto: "Aprobado por humano: checkout presentado para firmar" };
  }
  if (compra.veredicto?.status === "escalar") return { clave: "escalar", texto: "Escalado: decide el humano" };
  const checkoutReenviado = compra.reenviados.some((r) =>
    /checkout/i.test(metodo(r)) || (compra.checkoutTs !== null && tiempo(r) >= compra.checkoutTs));
  if (compra.checkout && checkoutReenviado) {
    return { clave: "para_firmar", texto: "Checkout presentado para firmar" };
  }
  if (compra.errores.length > 0) return { clave: "error", texto: "Error en el proxy" };
  if (terminoSinResultados(compra.eventos)) {
    return { clave: "sin_resultados", texto: "Sin resultados: el comercio no tiene nada que cumpla el pedido" };
  }
  return { clave: "en_curso", texto: "En curso" };
}

/**
 * ¿Lo último de la compra es una búsqueda que el comercio terminó sin nada?
 * (`ofertas_recibidas` con `carts: []`, sin ofertas ni checkout después).
 */
function terminoSinResultados(eventos) {
  for (let i = (eventos || []).length - 1; i >= 0; i--) {
    const e = eventos[i];
    if (e.tipo === "checkout_pedido") return false;
    if (e.tipo === "ofertas_recibidas") return Array.isArray(e.datos?.carts) && e.datos.carts.length === 0;
  }
  return false;
}

/**
 * Lo que hay que mandar a POST /guard/aprobaciones para "Aprobar igual":
 * el contexto de la compra y el cart retenido. null si no está retenida.
 */
export function cuerpoAprobacion(compra) {
  const fin = compra?.desenlace ?? desenlaceCheckout(compra?.eventos);
  if (!esRetencion(fin)) return null;
  const cartId = fin.datos?.cart_id ?? cartIdElegido(compra);
  if (!compra.contextoId || !cartId) return null;
  return { contexto_id: compra.contextoId, cart_id: cartId };
}

/**
 * Estado de la decisión humana de una compra, con la lista de
 * GET /guard/aprobaciones ({pendientes, usadas}) del proxy.
 * - retenido: esperando que alguien decida.
 * - aprobada: hay una aprobación pendiente; falta que el agente reintente.
 * - aprobado_humano: el reintento pasó con la aprobación (evento reenviado).
 */
export function estadoAprobacion(compra, aprobaciones) {
  const fin = compra?.desenlace ?? desenlaceCheckout(compra?.eventos);
  if (esAprobacionHumana(fin)) {
    return { clave: "aprobado_humano", texto: "Aprobado por humano: el reintento pasó con el checkout intacto.", aprobacion: null };
  }
  const cuerpo = cuerpoAprobacion(compra);
  if (!cuerpo) return { clave: null, texto: "", aprobacion: null };
  const pendiente = (aprobaciones?.pendientes || []).find((a) =>
    a?.contexto_id === cuerpo.contexto_id && a?.cart_id === cuerpo.cart_id) ?? null;
  if (pendiente) {
    return {
      clave: "aprobada",
      texto: "Aprobado por humano: pedile al agente que reintente el checkout. El Guard lo vuelve a evaluar y, si sigue en escalar con el mismo monto, pasa intacto.",
      aprobacion: pendiente,
    };
  }
  return { clave: "retenido", texto: "Retenido — esperando decisión humana", aprobacion: null };
}

/** Chequeos que atan el cart evaluado al checkout que firmó el comercio. */
export function chequeosCheckoutFirmado(veredicto) {
  return (veredicto?.chequeos || []).filter((c) => c?.origen === "checkout_firmado");
}

/**
 * Para el momento clave: la compra más reciente del escenario con el Guard
 * apagado y la más reciente con el Guard prendido. Se prefieren las que ya
 * tienen checkout firmado (las completas).
 */
export function parLadoALado(compras, escenario) {
  const delEscenario = (compras || []).filter((c) => c.escenario === escenario);
  const elegir = (activo) => {
    const candidatas = delEscenario.filter((c) => c.guardActivo === activo);
    return candidatas.find((c) => c.checkout) ?? candidatas[0] ?? null;
  };
  return { sin: elegir(false), con: elegir(true) };
}

/** Tope del pedido (requisito precio/máximo de la ficha). */
export function topeDeFicha(ficha) {
  const r = (ficha?.requisitos || []).find((q) => q.campo === "precio" && q.operador === "maximo");
  return typeof r?.valor === "number" ? r.valor : null;
}

const esObjeto = (x) => Boolean(x) && typeof x === "object" && !Array.isArray(x);

/**
 * El escenario del "Momento clave": el que eligió el usuario o, si no eligió,
 * el de la compra más nueva (con el comercio general, el comportamiento de
 * esa corrida); sin compras, el de por defecto (un fixture). `compras`, de
 * la más nueva a la más vieja (agruparCompras).
 */
export function escenarioDelMomento({ elegido = null, compras = [], porDefecto = null } = {}) {
  if (elegido) return elegido;
  return (compras || []).find((c) => typeof c?.escenario === "string" && c.escenario)?.escenario ?? porDefecto;
}

/**
 * La ficha de una compra que muestra el panel, y de dónde sale: {ficha, fuente}.
 * Sigue el orden del proxy (`proxy.ficha_de`):
 * - "recibo": la que usó el Guard (`veredicto.recibo.ficha`). Manda siempre.
 * - "escenario": la del escenario de la demo, solo si el proxy la usa
 *   (`GET /guard/state` → `ficha_demo`, PROXY_FICHA_DEMO=1).
 * - "intake": la que armó nuestro agente con el usuario (los requisitos
 *   vigentes de la compra en GET /guard/compras), confirmada solo si el
 *   usuario la confirmó.
 * - null: ninguna todavía. Con `ficha_demo` apagado (el default) o sin dato
 *   del proxy, la del escenario nunca se muestra como la de la compra.
 */
export function fichaVigente({ compra = null, compraAgente = null, fichaDemo = false, fichaEscenario = null } = {}) {
  const recibo = compra?.veredicto?.recibo?.ficha;
  if (esObjeto(recibo)) return { ficha: recibo, fuente: "recibo" };
  if (fichaDemo === true && esObjeto(fichaEscenario)) return { ficha: fichaEscenario, fuente: "escenario" };
  const requisitos = Array.isArray(compraAgente?.requisitos) ? compraAgente.requisitos : [];
  if (requisitos.length) {
    return {
      ficha: {
        pedido_original: typeof compraAgente.pedido === "string" ? compraAgente.pedido : "",
        requisitos,
        criterio: "lowest_price_among_compliant",
        confirmada: compraAgente.intake?.confirmada === true,
      },
      fuente: "intake",
    };
  }
  return { ficha: null, fuente: null };
}

/** Monto del checkout firmado (centavos del JWT → unidades), o del evento. */
export function montoCheckout(checkout) {
  const totales = checkout?.jwt_payload?.totals;
  if (Array.isArray(totales)) {
    const total = totales.find((t) => t.type === "total");
    if (typeof total?.amount === "number") return total.amount / 100;
  }
  if (typeof checkout?.amount === "number") return checkout.amount;
  return null;
}

export function monedaCheckout(checkout) {
  return checkout?.jwt_payload?.currency ?? checkout?.currency ?? null;
}

/**
 * Parte el texto libre del vendedor en tramos: los que figuran en el
 * catálogo público del comercio y los que no. No detecta inyecciones:
 * solo compara contra lo que el comercio publica.
 */
export function partirTextoLibre(texto, textosPublicos) {
  if (typeof texto !== "string" || texto.length === 0) return [];
  const cubierto = new Array(texto.length).fill(false);
  const frases = new Set();
  for (const t of textosPublicos || []) {
    if (typeof t !== "string" || !t.trim()) continue;
    frases.add(t.trim());
    for (const f of t.split(/(?<=[.!?])\s+/)) if (f.trim().length > 3) frases.add(f.trim());
  }
  for (const f of frases) {
    let desde = 0;
    for (;;) {
      const i = texto.indexOf(f, desde);
      if (i < 0) break;
      cubierto.fill(true, i, i + f.length);
      desde = i + 1;
    }
  }
  const tramos = [];
  let i = 0;
  while (i < texto.length) {
    const publico = cubierto[i];
    let j = i;
    while (j < texto.length && cubierto[j] === publico) j++;
    tramos.push({ texto: texto.slice(i, j), publico });
    i = j;
  }
  // Puntuación o espacios sueltos no cuentan como "texto que no está".
  const normalizados = tramos.map((t) =>
    !t.publico && !/[\p{L}\p{N}]/u.test(t.texto) ? { ...t, publico: true } : t);
  const fusion = [];
  for (const t of normalizados) {
    const prev = fusion[fusion.length - 1];
    if (prev && prev.publico === t.publico) prev.texto += t.texto;
    else fusion.push({ ...t });
  }
  return fusion;
}

const TITULOS = {
  busqueda_enviada: "Búsqueda enviada al comercio",
  ofertas_recibidas: "Ofertas recibidas",
  checkout_pedido: "El agente pide el checkout",
  checkout_recibido: "Checkout firmado por el comercio",
  veredicto: "Veredicto del Guard",
  reenviado: "El proxy reenvía",
  bloqueado: "Bloqueado antes de la firma",
  guard_cambiado: "Interruptor del Guard",
  error: "Error",
  pedido_detectado: "Nuestro agente pregunta el porqué",
  contexto_registrado: "Contexto del pedido",
  oportunidad: "Posible punto de encuentro: consulta al usuario",
  oportunidad_decidida: "Decisión del usuario",
};

const DECISION = {
  aceptada: `Aceptada: ${TEXTO_FINAL.aceptada.toLowerCase()}`,
  rechazada: `Rechazada: ${TEXTO_FINAL.rechazada.toLowerCase()}`,
  vencida: `Vencida: ${TEXTO_FINAL.vencida.replace(/^Sin respuesta: /, "")}`,
};

/** pedido_detectado: el pedido y la pregunta; en una repregunta, el turno y lo que falta. */
function resumenPregunta(d) {
  const n = Number.isInteger(d.turno) && d.turno > 0 ? d.turno : 0;
  const partes = [];
  if (n === 0 && d.pedido) partes.push(`«${d.pedido}»`);
  if (n > 0) partes.push(d.confirmar ? `turno ${n}: confirmar la ficha` : `turno ${n}`);
  if (d.pregunta) partes.push(d.pregunta);
  const faltan = Array.isArray(d.faltan) ? d.faltan : [];
  if (faltan.length) {
    partes.push(`falta: ${faltan.map((p) => (PARAMETROS[p] ?? String(p)).toLowerCase()).join(", ")}`);
  }
  return partes.join(" · ");
}

function resumenContexto(d) {
  const n = Number.isInteger(d.turno) && d.turno > 0 ? d.turno : 0;
  if (d.confirmada) return "El usuario confirmó la ficha: con eso sigue la negociación";
  if (n > 0 && d.omitido) return "El usuario omitió la repregunta: sigue con lo que ya dijo";
  if (n > 0 && d.vencido) return "Sin respuesta a la repregunta: sigue con lo que ya dijo";
  if (n > 0 && typeof d.respuesta === "string") return `Respuesta del turno ${n}`;
  if (d.omitido) return "El usuario omitió la pregunta: la negociación sigue con el pedido literal";
  if (d.vencido) return "Sin respuesta a tiempo: la negociación sigue con el pedido literal";
  if (!d.contexto) return "Nuestro agente no pudo armar el contexto: la negociación sigue con el pedido literal";
  const c = d.contexto;
  const partes = [`Necesidad: ${c.necesidad || "—"}`];
  const flex = Array.isArray(c.flexibilidades) ? c.flexibilidades : [];
  if (flex.length) {
    const inferidas = flex.filter((f) => f?.origen === "inferida").length;
    partes.push(`${flex.length} ${flex.length === 1 ? "flexibilidad" : "flexibilidades"}${inferidas ? ` (${inferidas} inferida${inferidas === 1 ? "" : "s"})` : ""}`);
  }
  return partes.join(" · ");
}

function resumenOportunidad(d) {
  const o = d.oportunidad || {};
  const cambios = (Array.isArray(o.cambios) ? o.cambios : []).map((c) =>
    `${CAMPOS[c?.campo] ?? c?.campo}: ${formatearValor(c?.campo, c?.pedido)} → ${formatearValor(c?.campo, c?.ofrecido)}`);
  const nombre = nombreOferta(o).texto;
  return [nombre === "—" ? null : nombre, ...cambios].filter(Boolean).join(" · ");
}

/** Título y resumen de una línea para la línea de tiempo. */
export function describirEvento(e) {
  const d = e?.datos || {};
  const titulo = TITULOS[e?.tipo] ?? String(e?.tipo ?? "evento");
  let resumen = "";
  switch (e?.tipo) {
    case "busqueda_enviada": resumen = d.catalog_search ?? ""; break;
    case "ofertas_recibidas": {
      const n = (d.carts || []).length;
      // Vacío: el proxy lo emite cuando el comercio terminó la búsqueda sin nada.
      resumen = Array.isArray(d.carts) && n === 0
        ? "Sin resultados: 0 carritos (ap2.cart)"
        : `${n} ${n === 1 ? "carrito" : "carritos"} (ap2.cart) y ${(d.offer_details || []).length} fichas declaradas`;
      break;
    }
    case "checkout_pedido": resumen = `cart_id ${d.cart_id ?? "—"}`; break;
    case "checkout_recibido": {
      const v = d.ap2_violaciones;
      const ap2 = Array.isArray(v) ? (v.length === 0 ? "AP2: OK" : `AP2 frena: ${v.join("; ")}`) : "AP2: sin dato";
      resumen = `${d.cart_id ?? "—"} · ${d.jwt_header?.alg ?? "?"} kid ${d.jwt_header?.kid ?? "?"} · ${ap2}`;
      break;
    }
    case "veredicto": {
      const extra = [];
      if (typeof d.fuente === "string") extra.push(`catálogo: ${d.fuente}`);
      if (typeof d.recibo_firmado === "boolean") extra.push(d.recibo_firmado ? "recibo firmado" : "recibo sin firmar");
      resumen = d.veredicto ? `${d.veredicto.status} · ${d.veredicto.motivo ?? ""}` : "";
      if (extra.length) resumen += ` (${extra.join(" · ")})`;
      break;
    }
    case "reenviado":
      resumen = `${d.metodo ?? "respuesta"}${d.aprobacion_humana ? " · aprobado por humano" : ""}${d.mandatos ? " · con mandatos firmados" : ""}`;
      break;
    case "bloqueado":
      resumen = d.motivo ?? "";
      if (d.requiere_aprobacion) return { titulo: "Retenido: requiere aprobación humana", resumen };
      // Etapa 2: el corte puede venir de antes del checkout (`etapa`).
      if (d.etapa === "seguridad") {
        const senales = Array.isArray(d.senales) && d.senales.length ? ` (señales: ${d.senales.join(", ")})` : "";
        return { titulo: "Corte de seguridad: conversación bloqueada", resumen: resumen + senales };
      }
      if (d.etapa === "intencion") return { titulo: "La consulta venció: no se compra nada", resumen };
      break;
    case "guard_cambiado": resumen = d.guard_activo ? "Guard activado" : "Guard apagado"; break;
    case "error": resumen = d.mensaje ?? ""; break;
    case "pedido_detectado": resumen = resumenPregunta(d); break;
    case "contexto_registrado": resumen = resumenContexto(d); break;
    case "oportunidad": resumen = resumenOportunidad(d); break;
    case "oportunidad_decidida": resumen = DECISION[d.decision] ?? String(d.decision ?? ""); break;
    default: resumen = "";
  }
  return { titulo, resumen };
}

/** Eventos como JSONL (una línea por evento), para guardar un replay. */
export function aJsonl(eventos) {
  return (eventos || []).map((e) => JSON.stringify(e)).join("\n") + "\n";
}
