// /demo sin DOM: la línea de tiempo (eventos del proxy + lo que pasó con el
// agente de compras), a quién le habla el input, cómo terminó la corrida y el
// contraste con y sin intermediario. Se testea con node --test.

import { formatearMedida, humanizarAtributo, nombreDeOferta } from "./relato.js";

const ESPERAN = new Set(["esperando_contexto", "esperando_decision"]);
const TOLERANCIA_MS = 2_000;

function tiempo(ts) {
  const t = Date.parse(ts ?? "");
  return Number.isNaN(t) ? Number.POSITIVE_INFINITY : t;
}

/** Eventos del proxy (contrato `Evento`) como items de la línea de tiempo. */
export function itemsDeEventos(eventos) {
  return (eventos || []).filter((e) => e && typeof e === "object" && e.tipo)
    .map((e) => ({ ...e, origen: "proxy", datos: e.datos || {} }));
}

/** Mensajes de la conversación con el agente (GET /api/demo/estado). */
export function itemsDeMensajes(mensajes) {
  return (mensajes || []).filter((m) => m && typeof m === "object" && m.tipo)
    .map((m) => ({ ...m, origen: "demo" }));
}

/** Todo junto, en orden de hora (estable), desde el inicio de la corrida. */
export function lineaDeTiempo(eventos, mensajes, desde) {
  const inicio = desde ? Date.parse(desde) - TOLERANCIA_MS : Number.NEGATIVE_INFINITY;
  return [...itemsDeEventos(eventos), ...itemsDeMensajes(mensajes)]
    .filter((i) => tiempo(i.ts) >= inicio)
    .map((i, n) => [i, n])
    .sort((a, b) => tiempo(a[0].ts) - tiempo(b[0].ts) || a[1] - b[1])
    .map(([i]) => i);
}

// ---------------------------------------------------------------- input único

/** La pregunta pendiente más nueva de esta corrida (no vencida). */
export function compraPendiente(compras, desde, ahora) {
  const inicio = desde ? Date.parse(desde) - TOLERANCIA_MS : Number.NEGATIVE_INFINITY;
  return (compras || [])
    .filter((c) => c && ESPERAN.has(c.estado) && typeof c.compra_id === "string")
    .filter((c) => { const v = Date.parse(c.vence ?? ""); return Number.isNaN(v) || v > ahora; })
    .filter((c) => { const t = Date.parse(c.creada ?? ""); return !Number.isNaN(t) ? t >= inicio : !desde; })
    .sort((a, b) => (Date.parse(b.creada ?? "") || 0) - (Date.parse(a.creada ?? "") || 0))[0] ?? null;
}

/**
 * A quién le va a hablar el input (el servidor decide igual al enviar; esto
 * es para la etiqueta y el placeholder): {modo, etiqueta, placeholder,
 * enviar, compraId}.
 */
export function modoDelInput({ compras, ocupado, desde, ahora, replay = false }) {
  if (replay) {
    return { modo: "replay", etiqueta: "Reproduciendo una grabación", placeholder: "En una grabación no se escribe",
      enviar: false, compraId: null };
  }
  const c = compraPendiente(compras, desde, ahora);
  if (c?.estado === "esperando_decision") {
    return { modo: "decision", etiqueta: "Nuestro agente te consulta una alternativa",
      placeholder: "Respondé sí o no", enviar: true, compraId: c.compra_id };
  }
  if (c && c.intake?.confirmar === true) {
    return { modo: "confirmar", etiqueta: "Nuestro agente te pide confirmar la ficha",
      placeholder: "Escribí «sí» para confirmar, o corregí lo que haga falta", enviar: true, compraId: c.compra_id };
  }
  if (c) {
    return { modo: "contexto", etiqueta: "Nuestro agente te pregunta",
      placeholder: "Tu respuesta (o «omitir»)", enviar: true, compraId: c.compra_id };
  }
  if (ocupado) {
    return { modo: "ocupado", etiqueta: "Tu agente está trabajando…", placeholder: "Esperá su respuesta",
      enviar: false, compraId: null };
  }
  return { modo: "agente", etiqueta: "Pedile algo a tu agente de compras",
    placeholder: "¿Qué querés comprar?", enviar: true, compraId: null };
}

// ---------------------------------------------------------------- desenlace

const DESENLACES = {
  pagado: { texto: "Pagado", tono: "ok" },
  frenada: { texto: "Frenada antes de la firma", tono: "crit" },
  cortada: { texto: "Conversación cortada por el filtro", tono: "crit" },
  retenida: { texto: "Retenida: necesita tu aprobación", tono: "aviso" },
  vencida: { texto: "La consulta venció", tono: "aviso" },
};

function claveDe(item) {
  if (item.origen === "demo" && item.tipo === "pago") return "pagado";
  if (item.origen !== "proxy" || item.tipo !== "bloqueado") return null;
  const d = item.datos || {};
  if (d.etapa === "seguridad") return "cortada";
  if (d.etapa === "intencion") return "vencida";
  if (d.requiere_aprobacion) return "retenida";
  return "frenada";
}

/** Cómo terminó la corrida (el último desenlace), o null si sigue. */
export function desenlace(items) {
  for (let i = (items || []).length - 1; i >= 0; i--) {
    const clave = claveDe(items[i]);
    if (clave) return { clave, ...DESENLACES[clave], item: items[i] };
  }
  return null;
}

// ---------------------------------------------------------------- con y sin intermediario

/** Lo que el Guard encontró en contra (el primer obligatorio que no cumple). */
function hallazgoDe(veredicto) {
  const c = (veredicto?.chequeos || []).find((x) => x?.tipo === "cumplimiento" && x.resultado === "bloquea");
  if (!c) return null;
  const campo = String(c.campo ?? "");
  const atributo = campo.startsWith("atributo:") ? humanizarAtributo(campo.slice(9)) : humanizarAtributo(campo);
  const unidad = c.observado && typeof c.observado === "object" ? c.observado.unidad : null;
  const pedido = formatearMedida(c.esperado) + (unidad && typeof c.esperado === "number" ? ` ${unidad}` : "");
  return { atributo, real: formatearMedida(c.observado), pedido };
}

/**
 * El resultado de una corrida terminada, para compararla:
 * {escenario, guardActivo, clave, producto, precio, moneda, hallazgo}, o null.
 */
export function resultadoCorrida(items, { escenario, guardActivo }) {
  const fin = desenlace(items);
  if (!fin) return null;
  const ofertas = new Map();
  let cartId = null;
  let veredicto = null;
  for (const it of items || []) {
    const d = it.datos || {};
    if (it.origen === "proxy" && it.tipo === "ofertas_recibidas") {
      for (const c of d.carts || []) if (c?.cart_id) ofertas.set(c.cart_id, { ...(ofertas.get(c.cart_id) || {}), precio: c.amount, moneda: c.currency });
      for (const o of d.offer_details || []) {
        if (o?.cart_id) {
          const previa = ofertas.get(o.cart_id) || {};
          ofertas.set(o.cart_id, { ...previa, titulo: o.title, sku: o.sku, precio: previa.precio ?? o.price, moneda: previa.moneda ?? o.currency });
        }
      }
    }
    if (it.origen === "proxy" && it.tipo === "checkout_pedido" && d.cart_id) cartId = d.cart_id;
    if (it.origen === "proxy" && it.tipo === "veredicto" && d.veredicto) {
      veredicto = d.veredicto;
      cartId = cartId ?? d.veredicto.cart_id_elegido ?? null;
    }
    if (it.origen === "demo" && it.tipo === "pago" && it.cart_id) cartId = it.cart_id;
  }
  const oferta = cartId ? ofertas.get(cartId) : null;
  return {
    escenario: escenario ?? null,
    guardActivo: Boolean(guardActivo),
    clave: fin.clave,
    producto: cartId && fin.clave !== "cortada" ? nombreDeOferta(ofertas, cartId) : null,
    precio: typeof oferta?.precio === "number" ? oferta.precio : null,
    moneda: oferta?.moneda ?? null,
    hallazgo: hallazgoDe(veredicto),
  };
}

function minuscula(texto) {
  return /^[A-ZÁÉÍÓÚÑ][a-záéíóúñ]/.test(texto) ? texto[0].toLowerCase() + texto.slice(1) : texto;
}

function frase(r, otro) {
  if (!r) return null;
  switch (r.clave) {
    case "pagado": {
      // Lo que no se vio sin intermediario lo dice la corrida con intermediario
      // del mismo producto.
      const h = r.hallazgo ?? (otro?.producto && otro.producto === r.producto ? otro.hallazgo : null);
      if (h) {
        return /\d/.test(h.real)
          ? `Se compró ${r.producto}, con ${h.real} de ${minuscula(h.atributo)} (pediste ${h.pedido})`
          : `Se compró ${r.producto}: ${minuscula(h.atributo)} ${h.real} (pediste ${h.pedido})`;
      }
      const precio = typeof r.precio === "number" ? ` por ${r.moneda ? `${r.moneda} ` : ""}${r.precio}` : "";
      return `Se compró ${r.producto ?? "el producto"}${precio}`;
    }
    case "frenada": return "Frenada antes de la firma: no se pagó nada";
    case "cortada": return "El filtro cortó la conversación: no se compró nada";
    case "retenida": return "Retenida antes de la firma: decidís vos";
    case "vencida": return "La consulta venció: no se compró nada";
    default: return null;
  }
}

/** Una línea por lado: {sin, con}. */
export function resumenComparacion(sin, con) {
  return { sin: frase(sin, con), con: frase(con, sin) };
}

function tono(r, otro) {
  if (!r) return null;
  if (r.clave === "pagado") {
    const h = r.hallazgo ?? (otro?.producto && otro.producto === r.producto ? otro.hallazgo : null);
    return h ? "crit" : "ok";
  }
  if (r.clave === "frenada" || r.clave === "cortada") return "freno";
  return "aviso";
}

/**
 * El tono de cada lado: "crit" (se pagó algo que no cumple), "ok" (se pagó
 * sin hallazgos), "freno" (no se pagó nada), "aviso" (quedó esperándote).
 */
export function tonosComparacion(sin, con) {
  return { sin: tono(sin, con), con: tono(con, sin) };
}

/**
 * Guarda la última corrida por lado ({sin, con}) bajo `clave` (por defecto,
 * el escenario). Con `maximo`, quedan las claves más nuevas: la que se guarda
 * pasa al final. No muta.
 */
export function guardarResultado(mapa, resultado, { clave = resultado?.escenario, maximo = Number.POSITIVE_INFINITY } = {}) {
  const base = mapa && typeof mapa === "object" ? mapa : {};
  if (!resultado?.escenario || !clave) return base;
  const lado = resultado.guardActivo ? "con" : "sin";
  const { [clave]: previo, ...resto } = base;
  const entradas = Object.entries({ ...resto, [clave]: { ...(previo || {}), [lado]: resultado } });
  return Object.fromEntries(entradas.slice(Math.max(0, entradas.length - maximo)));
}

const MAX_PEDIDO_CLAVE = 200;

/** El pedido en minúsculas, sin tildes ni puntuación: "Comprame 20…, hasta" ≈ "comprame 20… hasta". */
function normalizarPedido(pedido) {
  if (typeof pedido !== "string") return "";
  return pedido.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^a-z0-9ñ]+/g, " ").trim().slice(0, MAX_PEDIDO_CLAVE);
}

/**
 * La clave del contraste: el mismo pedido al mismo comportamiento del
 * comercio, con y sin intermediario (con pedidos libres, el escenario solo no
 * alcanza). null sin escenario o sin pedido.
 */
export function claveComparacion(escenario, pedido) {
  const p = normalizarPedido(pedido);
  return escenario && p ? `${escenario}|${p}` : null;
}

/** El pedido de la corrida: el primer mensaje que el usuario le mandó al agente de compras. */
export function pedidoDeCorrida(items) {
  const m = (items || []).find((i) => i?.origen === "demo" && i.tipo === "usuario" && i.destino === "agente"
    && typeof i.texto === "string" && i.texto.trim());
  return m ? m.texto.trim() : null;
}

// ---------------------------------------------------------------- comportamientos del comercio

const valida = (s) => s && typeof s.etiqueta === "string" && s.etiqueta.trim()
  && typeof s.texto === "string" && s.texto.trim();

/**
 * Pedidos para empezar (GET /api/demo/escenarios → sugerencias). El comercio
 * general responde a cualquiera; un escenario fijo responde siempre lo mismo,
 * así que sugiere solo su pedido (o nada).
 */
export function sugerenciasPara(datos, activo) {
  const info = datos?.nombres?.[activo];
  if (info?.tipo === "fijo") {
    return typeof info.pedido === "string" && info.pedido.trim()
      ? [{ etiqueta: "Pedido del escenario", texto: info.pedido.trim() }] : [];
  }
  return (Array.isArray(datos?.sugerencias) ? datos.sugerencias : []).filter(valida)
    .map(({ etiqueta, texto }) => ({ etiqueta, texto }));
}

function infoDe(datos, id) {
  if (!id) return null;
  const nombre = datos?.nombres?.[id];
  if (nombre?.etiqueta) return nombre;
  for (const g of datos?.grupos || []) {
    for (const o of g?.opciones || []) if (o?.id === id && o.etiqueta) return { etiqueta: o.etiqueta, tipo: g.tipo ?? null };
  }
  return null;
}

/** "Inyección en la oferta", "Zapatillas · inyección" o el id de un escenario que no conocemos. */
export function nombreDeEscenario(datos, id) {
  return infoDe(datos, id)?.etiqueta ?? (id ? String(id) : "");
}

const COLA_COMERCIO = "Tu agente negocia; KaiAI revisa antes de que firmes.";

/** La bajada de la bienvenida: con qué comercio se está comprando. */
export function textoDelComercio(datos, activo) {
  if (!activo) return "Tu agente negocia con el comercio; KaiAI revisa antes de que firmes.";
  const info = infoDe(datos, activo);
  if (info?.tipo === "general") return `Comportamiento del comercio: ${minuscula(info.etiqueta)}. ${COLA_COMERCIO}`;
  if (info?.tipo === "fijo") return `Escenario de prueba: ${info.etiqueta}. ${COLA_COMERCIO}`;
  return `Escenario del comercio: ${info?.etiqueta ?? activo}. ${COLA_COMERCIO}`;
}

// ---------------------------------------------------------------- ritmo del relato en vivo

/**
 * Las líneas que se ven ya y la primera que espera su pausa. Una línea del
 * Guard con `pausa` que todavía no se mostró se retiene, y con ella todas las
 * que vienen después, para que el orden no cambie. `mostradas` es un Set de
 * ids; se le suman las que pasan.
 */
export function retenerLineas(lineas, mostradas) {
  const visibles = [];
  for (const l of lineas) {
    if (!mostradas.has(l.id) && l.actor === "guard" && l.pausa) return { visibles, retenida: l };
    mostradas.add(l.id);
    visibles.push(l);
  }
  return { visibles, retenida: null };
}

/** Al terminar la pausa: la retenida y las del Guard que la siguen con la misma pausa. */
export function lineasASoltar(lineas, id) {
  const i = lineas.findIndex((l) => l.id === id);
  if (i < 0) return [id];
  const texto = lineas[i].pausa?.texto;
  const ids = [id];
  for (let k = i + 1; k < lineas.length && lineas[k].actor === "guard" && lineas[k].pausa?.texto === texto; k++) {
    ids.push(lineas[k].id);
  }
  return ids;
}
