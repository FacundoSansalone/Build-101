// El relato de /demo: una línea en lenguaje natural por cada cosa que pasa,
// armada con plantillas en código (nunca con un LLM). Nunca muestra texto
// libre del vendedor (item_label, description): solo campos tipados, lo que
// decidió el Guard con sus plantillas y lo que dijo el agente de compras.
// Sin DOM: se testea con node --test.

import { ATRIBUTOS, CAMPOS, formatearEntregas, formatearValor } from "./agente.js";

const MAX_LINEA = 240;
const MAX_TITULO = 60;

const NOMBRES_ATRIBUTO = {
  ...ATRIBUTOS,
  tamano_pantalla: "Pantalla",
  autonomia_bateria: "Batería",
  conectividad: "Conectividad",
  weight: "Peso",
};

const NOMBRES_CAMPO = { ...CAMPOS, titulo: "Título", sku: "SKU", gtin: "GTIN" };

const FUENTES = {
  fabricante: "el fabricante",
  catalogo_publico: "el catálogo público",
  pagina_publica: "la página pública",
};

/** "memoria_ram" → "Memoria RAM"; uno desconocido, legible igual. */
export function humanizarAtributo(nombre) {
  const n = String(nombre ?? "");
  if (NOMBRES_ATRIBUTO[n]) return NOMBRES_ATRIBUTO[n];
  const texto = n.replace(/_/g, " ").trim();
  return texto ? texto[0].toUpperCase() + texto.slice(1) : "—";
}

function nombreCampo(campo) {
  const c = String(campo ?? "");
  if (c.startsWith("atributo:")) return humanizarAtributo(c.slice("atributo:".length));
  return NOMBRES_CAMPO[c] ?? humanizarAtributo(c);
}

/** {valor: "8", unidad: "GB"} → "8 GB"; "N"/"Y" y booleanos → "no"/"sí". */
export function formatearMedida(v) {
  if (v && typeof v === "object" && !Array.isArray(v) && "valor" in v) {
    const valor = formatearMedida(v.valor);
    return v.unidad ? `${valor} ${v.unidad}` : valor;
  }
  if (v === true || v === "Y") return "sí";
  if (v === false || v === "N") return "no";
  if (v === null || v === undefined) return "—";
  return formatearValor(null, v);
}

/** Texto del agente sin markdown, en una línea y recortado. */
export function limpiarTexto(texto, max = MAX_LINEA) {
  if (typeof texto !== "string") return "";
  const limpio = texto
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s*(?:[*+-]|\d+[.)])\s+/gm, "")
    .replace(/(\*\*|__|`|~~)/g, "")
    .replace(/\*/g, "")
    // "_énfasis_" sí, "cart_1" no.
    .replace(/(^|[\s(["'¿¡])_+/g, "$1")
    .replace(/_+(?=[\s)\]"'.,;:!?]|$)/g, "");
  return recortar(limpio, max);
}

/** En una línea y recortado (sin tocar el contenido). */
function recortar(texto, max = MAX_LINEA) {
  const limpio = String(texto ?? "").replace(/\s+/g, " ").trim();
  if (limpio.length <= max) return limpio;
  const corte = limpio.slice(0, max);
  const espacio = corte.lastIndexOf(" ");
  return `${(espacio > max * 0.6 ? corte.slice(0, espacio) : corte).trimEnd()}…`;
}

function titulo(texto) {
  return recortar(texto, MAX_TITULO);
}

function monto(precio, moneda) {
  if (typeof precio !== "number" || !Number.isFinite(precio)) return null;
  const n = Number.isInteger(precio) ? String(precio) : precio.toFixed(2);
  return moneda ? `${moneda} ${n}` : n;
}

function hora(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((x) => String(x).padStart(2, "0")).join(":");
}

/** Los motivos del Guard ("Bloqueado: …") sin el prefijo y con nombres legibles. */
function humanizarDetalle(detalle) {
  return String(detalle ?? "")
    .replace(/^(Bloqueado|Escalar a una persona|Escalar|Aprobado con avisos|Aprobado|Con avisos):\s*/i, "")
    .replace(/\b([a-z]+(?:_[a-z]+)+)\b/g, (m) => (NOMBRES_ATRIBUTO[m] ? NOMBRES_ATRIBUTO[m] : m))
    .replace(/no cumple el obligatorio/g, "no cumple lo que pediste")
    .replace(/no cumple el preferido/g, "no cumple lo que preferías")
    .trim()
    .replace(/\.$/, "");
}

const linea = (item, texto, tono, actor, extra = {}) =>
  ({ id: `${item.id}${extra.sufijo ?? ""}`, ts: item.ts, hora: hora(item.ts), texto: recortar(texto, 320), tono, actor });

// ---------------------------------------------------------------- contexto

function nuevoContexto(opciones) {
  return {
    filtro: opciones?.filtro !== false,
    ofertas: new Map(),
    busquedas: new Set(),
    apagadoDicho: false, // "Intermediario apagado…" una vez por corrida
  };
}

function registrarOfertas(ctx, datos) {
  for (const c of datos?.carts || []) {
    if (c?.cart_id) ctx.ofertas.set(c.cart_id, { ...(ctx.ofertas.get(c.cart_id) || {}), precio: c.amount, moneda: c.currency });
  }
  for (const d of datos?.offer_details || []) {
    if (!d?.cart_id) continue;
    const previa = ctx.ofertas.get(d.cart_id) || {};
    ctx.ofertas.set(d.cart_id, {
      ...previa, titulo: d.title, sku: d.sku,
      precio: previa.precio ?? d.price, moneda: previa.moneda ?? d.currency, atributos: d.atributos,
    });
  }
}

/** Cómo nombrar una oferta: el título declarado (corto), el SKU o el cart_id. */
export function nombreDeOferta(ofertas, cartId) {
  const o = ofertas?.get?.(cartId);
  if (o?.titulo) return titulo(o.titulo);
  if (o?.sku) return titulo(o.sku);
  return cartId ? titulo(cartId) : "la oferta elegida";
}

// ---------------------------------------------------------------- plantillas

function lineasVeredicto(item, d) {
  const v = d?.veredicto;
  if (!v || typeof v !== "object") return [];
  const out = [];
  // Lo que dice una fuente que el comercio no controla contra lo que declaró.
  const contrastes = (v.chequeos || []).filter((c) =>
    c?.tipo === "evidencia" && c.resultado !== "ok" && FUENTES[c.origen] && c.campo !== "titulo" &&
    c.esperado !== null && c.esperado !== undefined && c.observado !== null && c.observado !== undefined);
  contrastes.slice(0, 2).forEach((c, i) => {
    out.push(linea(item, `${nombreCampo(c.campo)}: ${FUENTES[c.origen]} dice ${formatearMedida(c.esperado)}; el comercio declaró ${formatearMedida(c.observado)}`,
      "aviso", "guard", { sufijo: `-f${i}` }));
  });
  const bloquea = (v.chequeos || []).find((c) => c?.resultado === "bloquea");
  switch (v.status) {
    case "aprobado":
      out.push(linea(item, "Nuestra verificación: coincide con lo que autorizaste", "ok", "guard"));
      break;
    case "con_avisos":
      out.push(linea(item, "Nuestra verificación: coincide con lo que autorizaste (con avisos)", "ok", "guard"));
      break;
    case "escalar":
      out.push(linea(item, `⚠ Nuestra verificación pide tu revisión: ${humanizarDetalle(v.motivo)}`, "aviso", "guard"));
      break;
    case "bloqueado":
      out.push(linea(item, `Nuestra verificación: ${humanizarDetalle(bloquea?.detalle ?? v.motivo)}`, "crit", "guard"));
      break;
    default:
  }
  return out;
}

function lineaPedido(item, d) {
  const n = Number.isInteger(d.turno) ? d.turno : 0;
  const pregunta = String(d.pregunta ?? "").trim();
  if (d.confirmar) return linea(item, `Nuestro agente te pide confirmar: ${pregunta}`, "aviso", "guard");
  if (n > 0) return linea(item, `Nuestro agente repregunta: ${pregunta}`, "aviso", "guard");
  return linea(item, `Nuestro agente pregunta: ${pregunta}`, "aviso", "guard");
}

function lineaContexto(item, d) {
  if (d.final && d.confirmada) return linea(item, "Confirmaste la ficha: con eso negocia tu agente", "ok", "guard");
  if (d.omitido || d.vencido) {
    return linea(item, Number(d.turno) > 0 ? "Sin más respuestas: sigue con lo que ya dijiste"
      : "Sin el porqué: tu agente sigue con el pedido literal", "tenue", "guard");
  }
  const necesidad = d.contexto?.necesidad;
  if (necesidad) return linea(item, `Nuestro agente entendió: ${necesidad}`, "neutro", "guard");
  if (d.final) return linea(item, "Ficha lista: con eso negocia tu agente", "neutro", "guard");
  return null;
}

function lineaOportunidad(item, d) {
  const o = d.oportunidad || {};
  const cambios = (o.cambios || []).map((c) => {
    const ofrecido = c?.campo === "entregas" && Array.isArray(c.ofrecido) ? formatearEntregas(c.ofrecido) : formatearMedida(c?.ofrecido);
    return `${nombreCampo(c?.campo).toLowerCase()}: ${ofrecido}`;
  });
  const nombre = o.titulo || o.sku || "una oferta";
  return linea(item, `Nuestro agente encontró una alternativa: ${titulo(nombre)}, ${cambios.join("; ") || "con cambios"}. ¿La aceptás? (sí / no)`,
    "aviso", "guard");
}

const DECISIONES = {
  aceptada: ["Aceptaste: tu agente recibe los parámetros nuevos", "ok"],
  rechazada: ["Rechazaste la alternativa: sigue el pedido original", "neutro"],
  vencida: ["Sin respuesta: no se compra nada (el silencio no es autorización)", "aviso"],
};

const APAGADO = "Intermediario apagado: la respuesta del comercio llega directo a tu agente";

function lineasDeEvento(item, ctx) {
  const lineas = lineasDeEventoSolo(item, ctx);
  // Una corrida con el intermediario apagado lo dice una vez, aunque el
  // interruptor se haya movido antes de empezar.
  if (item.tipo !== "guard_cambiado" && item.guard_activo === false && !ctx.apagadoDicho) {
    ctx.apagadoDicho = true;
    return [linea(item, APAGADO, "tenue", "sistema", { sufijo: "-apagado" }), ...lineas];
  }
  return lineas;
}

function lineasDeEventoSolo(item, ctx) {
  const d = item.datos || {};
  const activo = item.guard_activo !== false;
  switch (item.tipo) {
    case "guard_cambiado":
      ctx.apagadoDicho = !d.guard_activo;
      return [d.guard_activo
        ? linea(item, "Intermediario activado: revisa cada respuesta y verifica antes de la firma", "ok", "guard")
        : linea(item, APAGADO, "tenue", "sistema")];
    case "busqueda_enviada": {
      const q = String(d.catalog_search ?? "").trim();
      if (!q || ctx.busquedas.has(q)) return [];
      ctx.busquedas.add(q);
      return [linea(item, `Tu agente le pidió al comercio: «${q}»`, "neutro", "agente")];
    }
    case "ofertas_recibidas": {
      registrarOfertas(ctx, d);
      const n = (d.carts || []).length;
      return [linea(item, `El comercio respondió con ${n} ${n === 1 ? "oferta" : "ofertas"}`, "neutro", "comercio")];
    }
    case "reenviado": {
      if (d.mandatos) return [linea(item, "Tu agente firmó los mandatos de AP2 y pidió el pago", "neutro", "agente")];
      if (!activo) return [];
      if (d.metodo === "find_products") {
        return [ctx.filtro
          ? linea(item, "Nuestro filtro revisó la respuesta: sin señales de manipulación", "ok", "guard")
          : linea(item, "La respuesta del comercio pasó a tu agente", "neutro", "guard")];
      }
      if (d.metodo === "create_checkout") {
        return [linea(item, d.aprobacion_humana ? "✓ Aprobado por vos: el checkout pasa a la firma, intacto"
          : "✓ El checkout pasa a la firma, intacto", "ok", "guard")];
      }
      return [];
    }
    case "bloqueado": {
      if (d.etapa === "seguridad") {
        const senales = Array.isArray(d.senales) && d.senales.length ? ` (${d.senales.slice(0, 3).join(", ")})` : "";
        return [linea(item, `✕ Nuestro filtro cortó la conversación${senales}: nada del comercio llegó a tu agente`, "crit", "guard")];
      }
      if (d.etapa === "intencion") return [linea(item, "✕ La consulta venció: no se compra nada", "aviso", "guard")];
      if (d.requiere_aprobacion) {
        return [linea(item, "⚠ Retenida antes de la firma: necesita tu aprobación (en el detalle técnico)", "aviso", "guard")];
      }
      return [linea(item, "✕ Compra frenada antes de la firma", "crit", "guard")];
    }
    case "checkout_pedido":
      return [linea(item, `Tu agente pidió el checkout de ${nombreDeOferta(ctx.ofertas, d.cart_id)}`, "neutro", "agente")];
    case "checkout_recibido": {
      const totales = d.jwt_payload?.totals;
      const total = Array.isArray(totales) ? totales.find((x) => x?.type === "total") : null;
      const precio = typeof total?.amount === "number" ? total.amount / 100 : d.amount;
      const moneda = d.jwt_payload?.currency ?? d.currency ?? ctx.ofertas.get(d.cart_id)?.moneda;
      const v = d.ap2_violaciones;
      const ap2 = Array.isArray(v) ? (v.length ? `AP2 lo frena: ${v.join("; ")}` : "AP2: dentro del tope") : null;
      const partes = [`El comercio firmó el checkout${monto(precio, moneda) ? ` por ${monto(precio, moneda)}` : ""}`, ap2].filter(Boolean);
      return [linea(item, partes.join("; "), "neutro", "comercio")];
    }
    case "veredicto":
      return lineasVeredicto(item, d);
    case "pedido_detectado":
      return [lineaPedido(item, d)];
    case "contexto_registrado":
      return [lineaContexto(item, d)].filter(Boolean);
    case "oportunidad":
      return [lineaOportunidad(item, d)];
    case "oportunidad_decidida": {
      const [texto, tono] = DECISIONES[d.decision] ?? [null, null];
      return texto ? [linea(item, texto, tono, "usuario")] : [];
    }
    case "error":
      return [linea(item, "El intermediario registró un error (ver el detalle técnico)", "aviso", "sistema")];
    default:
      return [];
  }
}

// Pasos automáticos que se juntan en una línea (por grupo).
const GRUPO_AUTO = { billetera: "billetera", continuar: "billetera", tarjeta: "billetera", confirmar: "confirmar", otp: "otp" };

function textoAutos(pasos, detalle) {
  const grupo = GRUPO_AUTO[pasos[0]];
  if (grupo === "billetera") {
    const direccion = pasos.includes("billetera");
    const tarjeta = pasos.includes("tarjeta");
    const final = detalle ? ` (terminada en ${detalle})` : "";
    if (direccion && tarjeta) return `Dirección y tarjeta de la billetera${final} ✓`;
    if (tarjeta) return `Tarjeta de la billetera${final} ✓`;
    if (direccion) return "Dirección de la billetera ✓";
    return "Seguimos con la compra ✓";
  }
  if (grupo === "confirmar") return "Confirmaste la compra ✓";
  return "Código de verificación ✓";
}

const ERRORES_DEMO = {
  adk_no_responde: "No se pudo hablar con tu agente de compras: ¿está levantado en :8000?",
  adk_error: "Tu agente de compras devolvió un error",
};

function lineasDeMensaje(item, ctx) {
  switch (item.tipo) {
    case "usuario":
      return [linea(item, `Vos: ${item.texto ?? ""}`, "usuario", "usuario")];
    case "agente":
      if (item.oculto) return [];
      return [linea(item, `Tu agente: ${limpiarTexto(item.texto)}`, "agente", "agente")];
    case "herramienta": {
      const args = item.args || {};
      if (item.nombre === "find_products" && args.catalog_search) {
        const q = String(args.catalog_search).trim();
        if (ctx.busquedas.has(q)) return [];
        ctx.busquedas.add(q);
        return [linea(item, `Tu agente le pidió al comercio: «${q}»`, "neutro", "agente")];
      }
      if (item.nombre === "select_cart" && args.cart_id) {
        return [linea(item, `Tu agente eligió ${nombreDeOferta(ctx.ofertas, args.cart_id)}`, "neutro", "agente")];
      }
      return [];
    }
    case "pago": {
      const o = ctx.ofertas.get(item.cart_id);
      const precio = monto(o?.precio, o?.moneda);
      const nombre = item.cart_id ? nombreDeOferta(ctx.ofertas, item.cart_id) : null;
      return [linea(item, `✓ Pagado${nombre ? `: ${nombre}` : ""}${precio ? `, ${precio}` : ""}`, "ok", "ap2")];
    }
    case "error_agente":
      if (item.guard) return []; // el bloqueo ya lo cuenta el Guard
      return [linea(item, "Tu agente tuvo un problema y reintenta", "tenue", "agente")];
    case "error":
      return [linea(item, ERRORES_DEMO[item.codigo] ?? "Algo falló al hablar con tu agente de compras", "aviso", "sistema")];
    default:
      return [];
  }
}

/**
 * El relato completo: items de la línea de tiempo (ver demo.js) → líneas.
 * Opciones: {filtro} (si el proxy tiene filtro cargado).
 */
export function armarRelato(items, opciones = {}) {
  const ctx = nuevoContexto(opciones);
  const out = [];
  let autos = null; // {linea, pasos, detalle}
  for (const item of items || []) {
    if (item?.origen === "demo" && item.tipo === "auto") {
      const grupo = GRUPO_AUTO[item.paso] ?? "otro";
      if (autos && autos.grupo === grupo) {
        autos.pasos.push(item.paso);
        autos.detalle = item.detalle ?? autos.detalle;
        autos.linea.texto = textoAutos(autos.pasos, autos.detalle);
        continue;
      }
      const l = linea(item, textoAutos([item.paso], item.detalle), "tenue", "usuario");
      l.texto = textoAutos([item.paso], item.detalle);
      autos = { grupo, pasos: [item.paso], detalle: item.detalle ?? null, linea: l };
      out.push(l);
      continue;
    }
    const lineas = item?.origen === "demo" ? lineasDeMensaje(item, ctx) : lineasDeEvento(item, ctx);
    // Un mensaje oculto o una herramienta sin línea no corta el grupo de pasos automáticos.
    if (lineas.length) autos = null;
    for (const l of lineas) {
      if (l.texto === "Tu agente tuvo un problema y reintenta" && out.at(-1)?.texto === l.texto) continue;
      out.push(l);
    }
  }
  return out;
}
