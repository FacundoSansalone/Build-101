// Piezas de DOM compartidas por las vistas. Todo dato externo entra como
// texto (textContent / createTextNode): nunca innerHTML con datos.

import { partirTextoLibre } from "../lib/eventos.js";

const SVG = "http://www.w3.org/2000/svg";

/** h("div", {class: "x", onclick: fn, aria-label: "..."}, hijos...) */
export function h(tag, props = {}, ...hijos) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = Array.isArray(v) ? v.filter(Boolean).join(" ") : v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  agregar(el, hijos);
  return el;
}

export function agregar(el, hijos) {
  for (const hijo of hijos.flat(Infinity)) {
    if (hijo === null || hijo === undefined || hijo === false) continue;
    el.append(hijo instanceof Node ? hijo : document.createTextNode(String(hijo)));
  }
  return el;
}

export function icono(nombre, { etiqueta = null, clase = "" } = {}) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", `icono ${clase}`.trim());
  svg.setAttribute("viewBox", "0 0 24 24");
  if (etiqueta) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", etiqueta);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  const use = document.createElementNS(SVG, "use");
  use.setAttribute("href", `#i-${nombre}`);
  svg.append(use);
  return svg;
}

// ---------------------------------------------------------------------------
// Estados (status del veredicto y resultado de cada chequeo)
// ---------------------------------------------------------------------------

export const STATUS = {
  aprobado: { tono: "ok", icono: "check", texto: "Aprobado" },
  con_avisos: { tono: "warn", icono: "alert", texto: "Con avisos" },
  escalar: { tono: "serious", icono: "up", texto: "Escalar" },
  bloqueado: { tono: "crit", icono: "block", texto: "Bloqueado" },
};

export const RESULTADO = {
  ok: { tono: "ok", icono: "check", texto: "OK" },
  aviso: { tono: "warn", icono: "alert", texto: "Aviso" },
  escalar: { tono: "serious", icono: "up", texto: "Escalar" },
  bloquea: { tono: "crit", icono: "block", texto: "Bloquea" },
};

export const EVIDENCIA = {
  verificado: { tono: "ok", icono: "check", texto: "Verificado" },
  solo_declarado: { tono: "warn", icono: "alert", texto: "Solo declarado" },
  contradicho: { tono: "crit", icono: "x", texto: "Contradicho" },
  no_verificable: { tono: "neutro", icono: "dash", texto: "No verificable" },
};

export function insignia(mapa, clave, { grande = false } = {}) {
  const d = mapa[clave] ?? { tono: "neutro", icono: "dash", texto: String(clave ?? "—") };
  return h("span", { class: ["insignia", `t-${d.tono}`, grande && "insignia-grande"] },
    icono(d.icono), h("span", { text: d.texto }));
}

export function chip(texto, { tono = "neutro", icon = null, titulo = null } = {}) {
  return h("span", { class: ["chip", `t-${tono}`], title: titulo }, icon && icono(icon), h("span", { text: texto }));
}

export function chipGuard(activo) {
  if (activo === true) return chip("Guard activo", { tono: "ok", icon: "shield" });
  if (activo === false) return chip("Guard apagado", { tono: "neutro", icon: "shield-off" });
  return chip("Guard: sin dato", { tono: "neutro" });
}

export function vacio(titulo, ...detalle) {
  return h("div", { class: "vacio" },
    h("p", { class: "vacio-titulo", text: titulo }),
    detalle.length ? h("div", { class: "vacio-detalle" }, ...detalle) : null);
}

// ---------------------------------------------------------------------------
// Formato
// ---------------------------------------------------------------------------

export function dinero(monto, moneda = "USD") {
  if (typeof monto !== "number" || !Number.isFinite(monto)) return "—";
  return `${moneda ?? ""} ${monto.toFixed(2)}`.trim();
}

export function hora(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) +
    "." + String(d.getMilliseconds()).padStart(3, "0");
}

export function fechaHora(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("es-AR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function corto(texto, n = 10) {
  const t = String(texto ?? "");
  return t.length > n * 2 + 1 ? `${t.slice(0, n)}…${t.slice(-n)}` : t;
}

export function valorTexto(v) {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.map(valorTexto).join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// ---------------------------------------------------------------------------
// Texto libre del vendedor
// ---------------------------------------------------------------------------

/**
 * El texto libre del vendedor, marcado como tal. Lo que no figura en el
 * catálogo público del comercio va además subrayado: es una comparación con
 * lo que el comercio publica, no un detector de inyecciones.
 */
export function textoLibre(texto, textosPublicos = []) {
  const tramos = textosPublicos.length ? partirTextoLibre(texto, textosPublicos) : [{ texto, publico: true }];
  return h("span", { class: "libre" },
    tramos.map((t) => t.publico ? t.texto
      : h("mark", { class: "libre-extra", title: "No figura en el catálogo público del comercio" }, t.texto)));
}

export function leyendaTextoLibre({ conExtra = true } = {}) {
  return h("p", { class: "leyenda" },
    h("span", { class: "muestra muestra-libre", "aria-hidden": "true" }), " texto libre del vendedor",
    conExtra ? [h("span", { class: "muestra muestra-extra", "aria-hidden": "true" }), " no figura en su catálogo público (:8011)"] : null);
}

// ---------------------------------------------------------------------------
// JSON con resaltado de rutas
// ---------------------------------------------------------------------------

/**
 * <pre> con el JSON indentado y coloreado. `resaltar` es un objeto
 * { "ruta.0.campo": (texto) => Node } para reemplazar el contenido de un string.
 * `acortar` corta strings largos (tokens) a n caracteres por lado.
 */
export function vistaJson(valor, { resaltar = {}, acortar = 0, clase = "" } = {}) {
  const pre = h("pre", { class: ["json", clase], tabindex: "0" });
  const code = h("code");
  pre.append(code);
  const p = (t, c) => code.append(c ? h("span", { class: c, text: t }) : document.createTextNode(t));

  const escribir = (v, ruta, nivel) => {
    const sangria = "  ".repeat(nivel);
    const clave = ruta.join(".");
    if (typeof v === "string") {
      if (resaltar[clave]) {
        p('"', "j-str");
        code.append(resaltar[clave](v));
        p('"', "j-str");
      } else {
        const s = acortar && v.length > acortar * 2 + 20
          ? `${v.slice(0, acortar)}… (${v.length} caracteres) …${v.slice(-acortar)}` : v;
        p(JSON.stringify(s), "j-str");
      }
    } else if (typeof v === "number") p(String(v), "j-num");
    else if (v === null || typeof v === "boolean") p(String(v), "j-lit");
    else if (Array.isArray(v)) {
      if (v.length === 0) return p("[]", "j-pun");
      p("[\n", "j-pun");
      v.forEach((item, i) => {
        p(sangria + "  ");
        escribir(item, [...ruta, i], nivel + 1);
        p(i < v.length - 1 ? ",\n" : "\n", "j-pun");
      });
      p(sangria + "]", "j-pun");
    } else if (typeof v === "object") {
      const claves = Object.keys(v);
      if (claves.length === 0) return p("{}", "j-pun");
      p("{\n", "j-pun");
      claves.forEach((k, i) => {
        p(sangria + "  ");
        p(JSON.stringify(k), "j-key");
        p(": ", "j-pun");
        escribir(v[k], [...ruta, k], nivel + 1);
        p(i < claves.length - 1 ? ",\n" : "\n", "j-pun");
      });
      p(sangria + "}", "j-pun");
    } else p(String(v));
  };
  escribir(valor, [], 0);
  return pre;
}

/** JWS compacto en tres segmentos con su nombre (header . payload . firma). */
export function vistaJws(compacto, { etiquetaFirma = "firma", payloadMax = 28 } = {}) {
  const [hd = "", pl = "", fi = ""] = String(compacto ?? "").split(".");
  const seg = (texto, clase, nombre) => h("span", { class: ["seg", clase] },
    h("span", { class: "seg-nombre", text: nombre }), h("span", { class: "seg-valor", text: texto }));
  return h("p", { class: "jws", "aria-label": "JWS compacto: header, payload y firma" },
    seg(hd, "seg-h", "header"), h("span", { class: "seg-punto", text: "." }),
    seg(corto(pl, payloadMax), "seg-p", "payload"), h("span", { class: "seg-punto", text: "." }),
    seg(fi, "seg-f", etiquetaFirma));
}

/** Tarjeta con encabezado de paso. */
export function tarjeta({ paso = null, titulo, subtitulo = null, clase = "", acciones = null }, ...cuerpo) {
  return h("article", { class: ["tarjeta", clase] },
    h("header", { class: "tarjeta-cab" },
      paso !== null ? h("span", { class: "paso", "aria-hidden": "true", text: paso }) : null,
      h("div", { class: "tarjeta-titulos" },
        h("h3", { text: titulo }),
        subtitulo ? h("p", { class: "tarjeta-sub", text: subtitulo }) : null),
      acciones),
    h("div", { class: "tarjeta-cuerpo" }, ...cuerpo));
}

export function filaDato(nombre, ...valor) {
  return [h("dt", { text: nombre }), h("dd", {}, ...valor)];
}
