// El relato de /demo: una línea en lenguaje natural por cada cosa que pasa,
// armada con plantillas en código (nunca con un LLM). Nunca muestra texto
// libre del vendedor (item_label, description): solo campos tipados, lo que
// decidió el Guard con sus plantillas y lo que dijo el agente de compras.
// Sin DOM: se testea con node --test.

import { ATRIBUTOS, CAMPOS, describirParametro, formatearEntregas, formatearValor } from "./agente.js";

const MAX_LINEA = 240;
const MAX_TITULO = 60;

const NOMBRES_ATRIBUTO = {
  ...ATRIBUTOS,
  tamano_pantalla: "Pantalla",
  autonomia_bateria: "Batería",
  conectividad: "Conectividad",
  weight: "Peso",
  // El chequeo de categoría del Guard (guard/categorias.py: campo del cumplimiento).
  categoria_taxonomia: "Categoría",
};

const NOMBRES_CAMPO = { ...CAMPOS, titulo: "Título", sku: "SKU", gtin: "GTIN" };

/** El comercio terminó la búsqueda sin carts (`ofertas_recibidas` vacío del proxy). */
export const SIN_RESULTADOS = "El comercio no tiene nada que cumpla tu pedido";

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

// Señales del filtro (`heuristica:x` o `modelo:x`) → qué hacía la respuesta.
const RAZONES_FILTRO = {
  instruccion_ia: "le daba órdenes a tu agente",
  anula_instrucciones: "intentaba saltear tus instrucciones",
  marcador_rol: "se hacía pasar por el sistema o por vos",
  manipulacion: "intentaba manipular la elección",
  exfiltracion: "pedía datos que no necesita",
  herramientas: "intentaba que tu agente ejecutara acciones",
  texto_oculto: "escondía texto",
  homoglifos: "disfrazaba letras",
};

function enumerar(partes) {
  return partes.length < 2 ? partes.join("") : `${partes.slice(0, -1).join(", ")} y ${partes.at(-1)}`;
}

/** El corte del filtro en lenguaje natural, sin códigos internos. */
export function textoCorteFiltro(senales) {
  const lista = Array.isArray(senales) ? senales.map(String) : [];
  const razones = [...new Set(lista.map((s) => RAZONES_FILTRO[s.split(":").at(-1)]).filter(Boolean))].slice(0, 3);
  const cierre = "Tu agente no la vio y no se compró nada";
  if (razones.length) return `✕ Cortamos la conversación: la respuesta del comercio ${enumerar(razones)}. ${cierre}`;
  if (lista.some((s) => s.startsWith("filtro:"))) {
    return `✕ Cortamos la conversación: no pudimos revisar la respuesta del comercio y, ante la duda, no pasa. ${cierre}`;
  }
  return `✕ Cortamos la conversación: la respuesta del comercio parecía un ataque. ${cierre}`;
}

function nombreCampo(campo) {
  const c = String(campo ?? "");
  if (c.startsWith("atributo:")) return humanizarAtributo(c.slice("atributo:".length));
  return NOMBRES_CAMPO[c] ?? humanizarAtributo(c);
}

const esMedida = (v) => Boolean(v) && typeof v === "object" && !Array.isArray(v) && "valor" in v;

/** {valor: "8", unidad: "GB"} → "8 GB"; "N"/"Y" y booleanos → "no"/"sí". */
export function formatearMedida(v) {
  if (esMedida(v)) {
    const valor = typeof v.valor === "number" && Number.isFinite(v.valor) ? numeroLegible(v.valor) : formatearMedida(v.valor);
    return v.unidad ? `${valor} ${unidadLegible(v.unidad)}` : valor;
  }
  if (v === true || v === "Y") return "sí";
  if (v === false || v === "N") return "no";
  if (v === null || v === undefined) return "—";
  return formatearValor(null, v);
}

// Unidades por dimensión, con su factor a la de base: las mismas que
// guard/atributos.py (_DIMENSIONES). Acá solo sirven para mostrar; la
// comparación la hace el Guard con los números sin redondear.
const DIMENSIONES = {
  datos: {
    b: 1, byte: 1, bytes: 1, ad: 1, kb: 1e3, "2p": 1e3, mb: 1e6, "4l": 1e6, gb: 1e9, e34: 1e9, tb: 1e12, e35: 1e12,
    kib: 2 ** 10, mib: 2 ** 20, gib: 2 ** 30, tib: 2 ** 40,
  },
  longitud: {
    mm: 1e-3, mmt: 1e-3, cm: 1e-2, cmt: 1e-2, m: 1, mtr: 1, in: 0.0254, inh: 0.0254, inch: 0.0254, inches: 0.0254,
    '"': 0.0254, "″": 0.0254, pulgada: 0.0254, pulgadas: 0.0254, pulg: 0.0254,
  },
  masa: {
    mg: 1e-6, mgm: 1e-6, g: 1e-3, gr: 1e-3, grm: 1e-3, kg: 1, kgm: 1,
    lb: 0.45359237, lbs: 0.45359237, lbr: 0.45359237, oz: 0.028349523125, onz: 0.028349523125,
  },
  tiempo: {
    s: 1, sec: 1, seg: 1, min: 60, h: 3600, hr: 3600, hrs: 3600, hur: 3600, hora: 3600, horas: 3600, hour: 3600, hours: 3600,
  },
};
const UNIDADES = new Map(Object.entries(DIMENSIONES).flatMap(([dim, us]) =>
  Object.entries(us).map(([u, factor]) => [u, [dim, factor]])));
// Códigos UN/CEFACT (unitCode de schema.org) y signos, como se leen.
const SIMBOLOS = {
  ad: "B", "2p": "kB", "4l": "MB", e34: "GB", e35: "TB", mmt: "mm", cmt: "cm", mtr: "m", inh: "in", '"': "in", "″": "in",
  mgm: "mg", grm: "g", kgm: "kg", lbr: "lb", onz: "oz", hur: "h",
};
const claveUnidad = (u) => String(u).trim().toLowerCase().replace(/\.+$/, "");
const NUMERO_CON_UNIDAD = /^\s*(-?\d+(?:[.,]\d+)?)\s*([^\d\s][^\d]{0,15})?\s*$/;
const CIFRAS_CONVERTIDO = 3;

/** "E34" → "GB", "CMT" → "cm", '"' → "in"; una que no es código, tal cual. */
export function unidadLegible(u) {
  return SIMBOLOS[claveUnidad(u)] ?? String(u).trim();
}

function numeroLegible(n) {
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(6)));
}

/** Tres cifras significativas (enteros desde 100), como el Guard: 24,0157 → "24". */
function aproximado(n) {
  if (n === 0) return "0";
  const decimales = Math.max(0, CIFRAS_CONVERTIDO - 1 - Math.floor(Math.log10(Math.abs(n))));
  return numeroLegible(Number(n.toFixed(decimales)));
}

/** [número, unidad] de una medida ({valor: 61, unidad: "cm"} o {valor: "61 cm"}), o null. */
function cantidadDe(v) {
  if (typeof v.valor === "number" && Number.isFinite(v.valor)) return [v.valor, v.unidad ?? null];
  if (typeof v.valor !== "string") return null;
  if (v.unidad && /^\s*-?\d+(?:\.\d+)?\s*$/.test(v.valor)) return [Number(v.valor), v.unidad];
  const m = v.unidad ? null : NUMERO_CON_UNIDAD.exec(v.valor);
  return m ? [Number(m[1].replace(",", ".")), m[2] ?? null] : null;
}

/**
 * Una medida en la unidad de lo pedido y, si hubo que convertir, la original
 * entre paréntesis: ({valor: 61, unidad: "cm"}, "in") → "24 in (61 cm)". La
 * misma unidad con otro nombre ("E34", "pulgadas") va con la de lo pedido. Si
 * no se puede convertir (sin unidad, otra dimensión, unidad desconocida),
 * igual que formatearMedida.
 */
export function medidaEnUnidad(v, unidad) {
  const original = formatearMedida(v);
  if (!esMedida(v) || typeof unidad !== "string" || !unidad.trim()) return original;
  const cantidad = cantidadDe(v);
  if (!cantidad || !cantidad[1]) return original;
  const [n, desde] = cantidad;
  const origen = UNIDADES.get(claveUnidad(desde)) ?? [`unidad:${claveUnidad(desde)}`, 1];
  const destino = UNIDADES.get(claveUnidad(unidad)) ?? [`unidad:${claveUnidad(unidad)}`, 1];
  if (origen[0] !== destino[0]) return original;
  if (origen[1] === destino[1]) return `${numeroLegible(n)} ${unidadLegible(unidad)}`;
  return `${aproximado((n * origen[1]) / destino[1])} ${unidadLegible(unidad)} (${original})`;
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

const RECOMIENDA = /recomiend|recomendaci[oó]n|recommend|mejor opci[oó]n|best (option|match|choice)/i;

/**
 * Lo que dijo el agente, en una línea: si es largo, su recomendación (o la
 * primera frase) y su última pregunta. Es el momento que importa en la
 * demo: qué recomienda el agente y qué le pregunta al usuario.
 */
export function resumirAgente(texto, max = 260) {
  const completo = limpiarTexto(texto, Number.POSITIVE_INFINITY);
  if (completo.length <= max) return completo;
  const sinFin = Number.POSITIVE_INFINITY;
  const lineas = String(texto ?? "").split(/\n/);
  // Las opciones de una lista numerada: el primer renglón de cada una.
  const opciones = lineas.map((l) => /^\s*\d+[.)]\s+(.+)$/.exec(l)?.[1])
    .filter(Boolean).map((o) => recortar(limpiarTexto(o, sinFin), 40)).slice(0, 4);
  const parrafos = lineas.map((l) => limpiarTexto(l, sinFin)).filter(Boolean);
  const esTitulo = (p) => p.endsWith(":") && p.length < 40; // "Mi recomendación:"
  const frases = parrafos.filter((p) => !esTitulo(p)).flatMap((p) => p.split(/(?<=[.!?…])\s+/));
  let recomendacion = null;
  const i = parrafos.findIndex((p) => RECOMIENDA.test(p));
  if (i >= 0) {
    recomendacion = esTitulo(parrafos[i]) ? parrafos.slice(i + 1).find((p) => !esTitulo(p)) ?? null
      : parrafos[i].split(/(?<=[.!?…])\s+/).find((f) => RECOMIENDA.test(f)) ?? parrafos[i];
  }
  const pregunta = [...frases].reverse().find((f) => f.includes("?"));
  const partes = [];
  if (opciones.length > 1) partes.push(`Opciones: ${opciones.join(", ")}.`);
  const principal = recomendacion ?? (opciones.length > 1 ? null : frases[0] ?? completo);
  if (principal && principal !== pregunta) partes.push(recortar(principal, 170));
  if (pregunta) partes.push(recortar(pregunta, 110));
  const resumen = partes.join(" ");
  return partes.length === 1 && !resumen.endsWith("…") && !pregunta ? `${resumen} …` : resumen;
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

const linea = (item, texto, tono, actor, extra = {}) => ({
  id: `${item.id}${extra.sufijo ?? ""}`, ts: item.ts, hora: hora(item.ts), texto: recortar(texto, extra.max ?? 320), tono, actor,
  pausa: pausaDe(item),
});

// Qué hizo el intermediario antes de esta línea. La vista en vivo lo muestra
// un momento ("Revisando la respuesta del comercio…") antes de la línea: las
// heurísticas y el comercio de la demo responden en milisegundos.
const REVISAR = { texto: "Revisando la respuesta del comercio", ms: 1400 };
const VERIFICAR = { texto: "Verificando la compra antes de la firma", ms: 1600 };
const ANALIZAR = { texto: "Analizando tu respuesta", ms: 900 };
const COMPARAR = { texto: "Comparando la oferta con lo que necesitás", ms: 1400 };

function pausaDe(item) {
  if (item?.origen !== "proxy") return null;
  const d = item.datos || {};
  switch (item.tipo) {
    case "reenviado":
      if (d.metodo === "find_products") return REVISAR;
      return d.metodo === "create_checkout" ? VERIFICAR : null;
    case "bloqueado":
      if (d.etapa === "seguridad") return REVISAR;
      return d.etapa === "intencion" ? null : VERIFICAR;
    case "veredicto": return VERIFICAR;
    case "pedido_detectado": case "contexto_registrado": return ANALIZAR;
    case "oportunidad": return COMPARAR;
    default: return null;
  }
}

// ---------------------------------------------------------------- contexto

function nuevoContexto(opciones) {
  return {
    filtro: opciones?.filtro !== false,
    ofertas: new Map(),
    busquedas: new Set(),
    entendido: new Map(), // compra_id → lo último que se dijo que entendió nuestro agente
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
    // La fuente, en la unidad que declaró el comercio: "dice 24 in (61 cm); declaró 27 in".
    const unidad = esMedida(c.observado) ? c.observado.unidad : null;
    out.push(linea(item, `${nombreCampo(c.campo)}: ${FUENTES[c.origen]} dice ${medidaEnUnidad(c.esperado, unidad)}; el comercio declaró ${formatearMedida(c.observado)}`,
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

// Categorías del catálogo público en palabras. Una desconocida se muestra
// como slug (el proxy solo acepta letras, números, espacios y guiones).
const CATEGORIAS = {
  basketball_shoes: "zapatillas de básquet", lifestyle_sneakers: "zapatillas urbanas",
  laptops: "notebooks", headphones: "auriculares",
  fuera_de_catalogo: "el que pediste (no figura en el catálogo público)", dicha: "el que pediste",
};

// Cómo quedó cada parámetro: lo dijiste, lo dedujo (y hay que confirmarlo),
// lo dijiste pero no quedó claro, o falta.
function marcaParametro(p) {
  if (p.estado === "resuelto") return ["lo dijiste", "ok"];
  if (p.estado === "a_confirmar") return p.origen === "inferido" ? ["lo deduje: confirmalo", "aviso"] : ["a confirmar", "aviso"];
  return ["falta", "crit"];
}

const MAX_NIVELES_RUTA = 6;
const MAX_NIVEL_RUTA = 40;
const MAX_RUTA = 200;

/**
 * La ruta de la categoría en la taxonomía de Google (nuestro árbol, nunca
 * texto del comercio) en una línea: ["Electrónica", "Laptops"] o
 * "Electrónica > Laptops" → "Electrónica › Laptops". null si no hay o no se
 * entiende.
 */
export function rutaCategoria(valor) {
  const partes = Array.isArray(valor) ? valor : typeof valor === "string" ? valor.split(">") : [];
  const niveles = partes.filter((p) => typeof p === "string")
    .map((p) => recortar(p.replace(/[\u0000-\u001f\u007f]/g, " "), MAX_NIVEL_RUTA)).filter(Boolean);
  if (!niveles.length) return null;
  return recortar(niveles.slice(-MAX_NIVELES_RUTA).join(" › "), MAX_RUTA);
}

const MARCA_CATEGORIA = "KaiAI la verifica en cada oferta";

/**
 * La ficha parcial del intake (`ficha_parcial` de pedido_detectado, como la
 * arma el proxy) en filas legibles: [{nombre, valor, marca, tono, preferido}].
 * Los valores son plantillas del intake (números, fechas, monedas) o
 * requisitos tipados: nada del comercio salvo el nombre de una categoría de
 * su catálogo público. Con `categoriaRuta` (la ruta de la taxonomía que trae
 * el evento), suma la fila "Categoría" después del producto.
 */
export function filasFicha(fichaParcial, { categoriaRuta = null } = {}) {
  const filas = (Array.isArray(fichaParcial) ? fichaParcial : [])
    .filter((p) => p && typeof p === "object" && typeof p.parametro === "string")
    .map((p) => {
      const d = describirParametro(p);
      let valor = d.valor;
      if (p.parametro === "categoria" && p.estado !== "faltante" && typeof p.valor === "string") {
        valor = CATEGORIAS[p.valor] ?? titulo(p.valor.replace(/_/g, " "));
      }
      const [marca, tono] = marcaParametro(p);
      return { nombre: d.nombre, valor, marca, tono, preferido: !d.obligatorio, parametro: p.parametro };
    });
  const ruta = rutaCategoria(categoriaRuta);
  if (ruta) {
    const producto = filas.findIndex((f) => f.parametro === "categoria");
    filas.splice(producto + 1, 0, { nombre: "Categoría", valor: ruta, marca: MARCA_CATEGORIA, tono: "neutro", preferido: false });
  }
  return filas.map(({ parametro, ...f }) => f);
}

const NOTA_FICHA = "Escribí «sí» para confirmarla o corregí lo que haga falta. "
  + "Si no confirmás, queda autorizado solo lo que dijiste con claridad: lo que deduje, no.";

// Las preguntas de nuestro agente son plantillas del intake (nada del
// comercio): la de confirmar, con lo que dedujo y lo que falta, es larga.
const MAX_PREGUNTA = 640;

function lineaPedido(item, d) {
  const n = Number.isInteger(d.turno) ? d.turno : 0;
  const pregunta = String(d.pregunta ?? "").trim();
  if (d.confirmar) {
    const l = linea(item, `Nuestro agente te pide confirmar: ${pregunta}`, "aviso", "guard", { max: MAX_PREGUNTA });
    const ficha = filasFicha(d.ficha_parcial, { categoriaRuta: d.categoria_ruta });
    return ficha.length ? { ...l, ficha, nota: NOTA_FICHA } : l;
  }
  if (n > 0) return linea(item, `Nuestro agente repregunta: ${pregunta}`, "aviso", "guard", { max: MAX_PREGUNTA });
  return linea(item, `Nuestro agente pregunta: ${pregunta}`, "aviso", "guard", { max: MAX_PREGUNTA });
}

// Lo que el usuario dijo flexible, en palabras de la plantilla. Las
// descripciones del agente de decisiones son texto fijo
// (intencion/agente_decisiones.py → FLEXIBILIDADES); las de Gemini llevan la
// etiqueta del campo ("Entregas: …", intencion/agente.py) y su texto libre no
// se muestra. Una "Otro: …" o una sin etiqueta conocida, tampoco.
const FLEXIBLES_FIJAS = {
  "Acepta recibir la compra en partes (entregas escalonadas).": "aceptás recibir en partes",
  "Acepta otra fecha de entrega.": "aceptás otra fecha de entrega",
  "Acepta otra marca u otro modelo.": "aceptás otra marca u otro modelo",
  "Acepta otra cantidad.": "aceptás otra cantidad",
};
const FLEXIBLES_POR_CAMPO = {
  Cantidad: "puede variar la cantidad", Entregas: "pueden variar las entregas", Marca: "puede variar la marca",
  Categoría: "puede variar el producto", Uso: "puede variar el uso", Talle: "puede variar el talle",
};
const MAX_ENTREGAS_ENTENDIDO = 6;

function fraseFlexibilidad(f) {
  const descripcion = String(f?.descripcion ?? "");
  return FLEXIBLES_FIJAS[descripcion] ?? FLEXIBLES_POR_CAMPO[descripcion.split(": ")[0]] ?? null;
}

const entregaValida = (e) => Number.isInteger(e?.cantidad) && e.cantidad > 0
  && /^\d{4}-\d{2}-\d{2}$/.test(String(e?.fecha ?? ""));

/**
 * Lo que entendió nuestro agente, armado con el contexto estructurado: el
 * cronograma de la necesidad y las flexibilidades que el usuario dijo
 * ("necesitás 10 el lun 19/10 y 10 el jue 22/10; aceptás recibir en partes").
 * null si no hay nada de eso. Nunca usa `necesidad` (texto de un modelo o el
 * eco de la primera respuesta).
 */
export function textoEntendido(contexto) {
  const partes = [];
  const entregas = (Array.isArray(contexto?.necesidad_entregas) ? contexto.necesidad_entregas : []).filter(entregaValida);
  if (entregas.length) {
    const resto = entregas.length - MAX_ENTREGAS_ENTENDIDO;
    partes.push(`necesitás ${formatearEntregas(entregas.slice(0, MAX_ENTREGAS_ENTENDIDO))}${resto > 0 ? ` y ${resto} entregas más` : ""}`);
  }
  const dichas = (Array.isArray(contexto?.flexibilidades) ? contexto.flexibilidades : [])
    .filter((f) => f?.origen === "dicha").map(fraseFlexibilidad).filter(Boolean);
  partes.push(...new Set(dichas));
  return partes.length ? partes.join("; ") : null;
}

function lineaContexto(item, d, ctx) {
  const ruta = d.final ? rutaCategoria(d.categoria_ruta) : null;
  const categoria = ruta ? ` (categoría: ${ruta})` : "";
  if (d.final && d.confirmada) return linea(item, `Confirmaste la ficha: con eso negocia tu agente${categoria}`, "ok", "guard");
  if (d.omitido || d.vencido) {
    return linea(item, Number(d.turno) > 0 ? "Sin más respuestas: sigue con lo que ya dijiste"
      : "Sin el porqué: tu agente sigue con el pedido literal", "tenue", "guard");
  }
  const entendido = d.contexto ? textoEntendido(d.contexto) : null;
  const compra = String(d.compra_id ?? "");
  if (entendido && ctx.entendido.get(compra) !== entendido) {
    ctx.entendido.set(compra, entendido);
    return linea(item, `Nuestro agente entendió: ${entendido}`, "neutro", "guard");
  }
  // Sin cronograma ni flexibilidades: la respuesta de este turno (la última).
  const respuesta = typeof d.respuesta === "string" ? d.respuesta.trim() : "";
  if (!entendido && d.contexto && respuesta) {
    return linea(item, `Nuestro agente entendió: «${recortar(respuesta, 160)}»`, "neutro", "guard");
  }
  if (d.final) return linea(item, `Ficha lista: con eso negocia tu agente${categoria}`, "neutro", "guard");
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
      // El proxy lo emite vacío cuando el comercio terminó la búsqueda sin nada.
      if (n === 0) return [linea(item, SIN_RESULTADOS, "aviso", "comercio")];
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
      if (d.etapa === "seguridad") return [linea(item, textoCorteFiltro(d.senales), "crit", "guard")];
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
      return [lineaContexto(item, d, ctx)].filter(Boolean);
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
      return [linea(item, `Tu agente: ${resumirAgente(item.texto)}`, "agente", "agente")];
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
