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

/** Guarda la última corrida por escenario y lado ({sin, con}). No muta. */
export function guardarResultado(mapa, resultado) {
  const base = mapa && typeof mapa === "object" ? mapa : {};
  if (!resultado?.escenario) return base;
  const lado = resultado.guardActivo ? "con" : "sin";
  return { ...base, [resultado.escenario]: { ...(base[resultado.escenario] || {}), [lado]: resultado } };
}
