// /demo: el controlador de la pantalla (DOM). La lógica vive en lib/ y se
// testea con node --test: relato.js (plantillas), diagrama.js (máquina de
// estados), demo.js (línea de tiempo, input y contraste).
//
// Fuentes: eventos del proxy (SSE reenviado por el panel + historial), la
// conversación con el agente de compras (GET /api/demo/estado), las compras
// de nuestro agente (para saber a quién le habla el input) y el estado del
// proxy (interruptor del intermediario). Todo same-origin: el token del
// proxy lo agrega el servidor, nunca el navegador.

import { fusionarEventos } from "./lib/eventos.js";
import { registrarSondeo, unaALaVez } from "./lib/conexion.js";
import { armarRelato } from "./lib/relato.js";
import { estadoInicial, geometria, NODOS, paso } from "./lib/diagrama.js";
import {
  desenlace, guardarResultado, lineaDeTiempo, modoDelInput, resultadoCorrida, resumenComparacion,
} from "./lib/demo.js";

const $ = (sel) => document.querySelector(sel);
const NS = "http://www.w3.org/2000/svg";
const reducido = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const angosto = window.matchMedia("(max-width: 640px)");

// ---------------------------------------------------------------- preferencias

function leerPref(clave, porDefecto = null) {
  try { return localStorage.getItem(`psg.${clave}`) ?? porDefecto; } catch { return porDefecto; }
}
function guardarPref(clave, valor) {
  try { localStorage.setItem(`psg.${clave}`, valor); } catch { /* sin storage */ }
}
function leerComparacion() {
  try { return JSON.parse(leerPref("demo.comparacion", "{}")) || {}; } catch { return {}; }
}

// ---------------------------------------------------------------- estado

const estado = {
  eventos: [],
  demo: { sesion: { id: null, creada: null }, ocupado: false, auto: true, fin: null, mensajes: [], escenario: null },
  compras: [],
  proxy: { conectado: null, fallos: 0, guardActivo: null, filtro: false },
  escenarios: { activo: null, grupos: [] },
  replay: null, // {nombre, todos, eventos, inicio, temporizador}
  enviando: false,
  cambiando: false,
  comparacion: leerComparacion(),
  guardadas: new Set(),
};

async function pedir(url, { metodo = "GET", cuerpo, timeout = 6000 } = {}) {
  const control = new AbortController();
  const corte = setTimeout(() => control.abort(), timeout);
  try {
    const r = await fetch(url, {
      method: metodo,
      signal: control.signal,
      cache: "no-store",
      headers: cuerpo === undefined ? {} : { "Content-Type": "application/json" },
      body: cuerpo === undefined ? undefined : JSON.stringify(cuerpo),
    });
    let datos = null;
    try { datos = await r.json(); } catch { /* sin cuerpo */ }
    return { status: r.status, ok: r.ok, datos };
  } catch {
    return { status: 0, ok: false, datos: null };
  } finally {
    clearTimeout(corte);
  }
}

// ---------------------------------------------------------------- sondeos

const refrescarEstado = unaALaVez(async () => {
  const r = await pedir("/api/demo/estado");
  if (r.ok && r.datos?.sesion) {
    estado.demo = r.datos;
    programarRender();
  }
});

const refrescarCompras = unaALaVez(async () => {
  const r = await pedir("/api/proxy/guard/compras", { timeout: 4000 });
  estado.compras = r.ok && Array.isArray(r.datos?.compras) ? r.datos.compras : [];
  programarRender();
});

const refrescarProxy = unaALaVez(async () => {
  const r = await pedir("/api/proxy/guard/state", { timeout: 4000 });
  const ok = r.ok && typeof r.datos?.guard_activo === "boolean";
  estado.proxy = { ...registrarSondeo(estado.proxy, ok), ...(ok ? {
    guardActivo: r.datos.guard_activo, filtro: r.datos.filtro === true,
  } : {}) };
  programarRender();
});

async function cargarEscenarios() {
  const r = await pedir("/api/demo/escenarios");
  if (r.ok && Array.isArray(r.datos?.grupos)) {
    estado.escenarios = r.datos;
    construirSelector();
    programarRender();
  }
}

async function cargarGrabaciones() {
  const r = await pedir("/api/replay");
  const select = $("#grabacion");
  for (const g of Array.isArray(r.datos) ? r.datos : []) {
    if (!g?.nombre) continue;
    const op = document.createElement("option");
    op.value = g.nombre;
    op.textContent = g.nombre;
    select.append(op);
  }
}

// ---------------------------------------------------------------- eventos del proxy (SSE)

let fuente = null;
let reintento = null;

const cargarHistorial = unaALaVez(async () => {
  const r = await pedir("/api/proxy/guard/events/history?limit=200", { timeout: 5000 });
  if (r.ok && Array.isArray(r.datos)) ingerir(r.datos);
});

function ingerir(lista) {
  estado.eventos = fusionarEventos(estado.eventos, lista);
  for (const e of lista) {
    if (["pedido_detectado", "contexto_registrado", "oportunidad", "oportunidad_decidida", "guard_cambiado"].includes(e?.tipo)) {
      refrescarCompras();
      if (e.tipo === "guard_cambiado") refrescarProxy();
    }
  }
  programarRender();
}

function conectarEventos() {
  clearTimeout(reintento);
  if (fuente) fuente.close();
  fuente = new EventSource("/api/proxy/guard/events");
  cargarHistorial();
  fuente.onopen = () => cargarHistorial();
  fuente.addEventListener("evento", (m) => {
    try { ingerir([JSON.parse(m.data)]); } catch { /* línea inválida */ }
  });
  fuente.onerror = () => {
    if (fuente.readyState === EventSource.CLOSED) reintento = setTimeout(conectarEventos, 3000);
  };
}
window.addEventListener("pagehide", () => fuente?.close());

// ---------------------------------------------------------------- diagrama

const svg = $("#diagrama");
const capas = { aristas: null, nodos: null, mensajes: null };
let geo = null;

function el(tag, attrs = {}, padre = null) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  if (padre) padre.append(n);
  return n;
}

function texto(padre, contenido, attrs) {
  const t = el("text", { "text-anchor": "middle", "dominant-baseline": "middle", ...attrs }, padre);
  t.textContent = contenido;
  return t;
}

const NOMBRE_ETAPA = { filtro: "filtro", intencion: "intención", verificacion: "verificación" };

function construirDiagrama() {
  geo = geometria(angosto.matches ? "vertical" : "horizontal");
  svg.setAttribute("viewBox", `0 0 ${geo.ancho} ${geo.alto}`);
  for (const n of [...svg.children]) if (!["title", "desc"].includes(n.tagName)) n.remove();
  capas.aristas = el("g", { class: "capa-aristas" }, svg);
  capas.nodos = el("g", { class: "capa-nodos" }, svg);
  capas.mensajes = el("g", { class: "capa-mensajes" }, svg);
  for (const [clave, d] of Object.entries(geo.aristas)) {
    el("path", { d, class: "arista", "data-clave": clave }, capas.aristas);
  }
  for (const id of NODOS) {
    const n = geo.nodos[id];
    const secundario = id === "fuentes" || id === "ap2";
    const g = el("g", { class: `nodo nodo-${id}${secundario ? " secundario" : ""}`, "data-id": id }, capas.nodos);
    el("rect", { x: n.x - n.w / 2, y: n.y - n.h / 2, width: n.w, height: n.h, rx: id === "guard" ? 18 : 12,
      class: id === "guard" ? "caja" : "" }, g);
    if (id === "guard") {
      texto(g, n.etiqueta, { x: n.x, y: n.y - 24, class: "etiqueta" });
      texto(g, n.sub, { x: n.x, y: n.y - 4, class: "sub" });
      construirEtapas(g, n);
    } else if (n.sub) {
      texto(g, n.etiqueta, { x: n.x, y: n.y - 8, class: "etiqueta" });
      texto(g, n.sub, { x: n.x, y: n.y + 11, class: "sub" });
    } else {
      texto(g, n.etiqueta, { x: n.x, y: n.y + 1, class: "etiqueta" });
    }
  }
  programarRender();
}

function construirEtapas(g, n) {
  const fila = el("g", { class: "etapas" }, g);
  const items = Object.entries(NOMBRE_ETAPA).map(([clave, nombre]) => {
    const e = el("g", { class: "etapa", "data-etapa": clave }, fila);
    el("circle", { r: 4, cx: 0, cy: 0 }, e);
    const t = el("text", { x: 8, y: 0.5, "dominant-baseline": "middle" }, e);
    t.textContent = nombre;
    return { e, ancho: 8 + (t.getComputedTextLength?.() || nombre.length * 6.2) };
  });
  const separacion = 12;
  const total = items.reduce((s, i) => s + i.ancho, 0) + separacion * (items.length - 1);
  let x = n.x - total / 2 + 4;
  for (const i of items) {
    i.e.setAttribute("transform", `translate(${x.toFixed(1)} ${(n.y + 24).toFixed(1)})`);
    x += i.ancho + separacion;
  }
}

const diag = { clave: null, vistos: new Set(), estado: estadoInicial(true) };

// Lo que ya estaba al abrir la página (el historial) no se anima.
const ARRANQUE = performance.now();
const enArranque = () => performance.now() - ARRANQUE < 1500;

function guardActivoAhora(e) {
  if (estado.replay) return e.guardActivo;
  return typeof estado.proxy.guardActivo === "boolean" ? estado.proxy.guardActivo : e.guardActivo;
}

function avanzarDiagrama(items) {
  const clave = estado.replay ? `replay:${estado.replay.nombre}:${estado.replay.inicio}` : estado.demo.sesion?.creada;
  if (clave !== diag.clave) {
    diag.clave = clave;
    diag.vistos = new Set();
  }
  let e = estadoInicial(estado.replay ? true : (estado.proxy.guardActivo ?? true));
  const animar = [];
  for (const it of items) {
    const r = paso(e, it);
    e = r.estado;
    if (!diag.vistos.has(it.id)) {
      diag.vistos.add(it.id);
      if (!enArranque()) animar.push(...r.flujos);
    }
  }
  diag.estado = e;
  encolar(animar);
  pintarDiagrama(e);
}

const DESCRIPCION = {
  inactivo: "espera el próximo mensaje",
  trabajando: "está revisando",
  ok: "dejó pasar lo que revisó",
  aviso: "necesita tu atención",
  bloqueado: "frenó la conversación o la compra",
};

function pintarDiagrama(e) {
  if (!geo) return;
  const activo = guardActivoAhora(e);
  const color = activo ? (e.guard === "apagado" ? "inactivo" : e.guard) : "apagado";
  const guard = capas.nodos.querySelector(".nodo-guard");
  guard.dataset.estado = color;
  const { dx, dy } = activo ? { dx: 0, dy: 0 } : geo.apagado;
  guard.style.transform = `translate(${dx}px, ${dy}px)`;
  for (const [etapa, valor] of Object.entries(e.etapas)) {
    const nodo = guard.querySelector(`.etapa[data-etapa="${etapa}"]`);
    if (nodo) nodo.dataset.estado = activo ? valor : "inactivo";
  }
  for (const id of ["fuentes", "ap2"]) {
    capas.nodos.querySelector(`.nodo-${id}`).dataset.estado = e.nodos[id] ?? "inactivo";
  }
  const visibles = {
    "agente-comercio": !activo, "agente-guard": activo, "guard-comercio": activo,
    "guard-usuario": activo, "guard-fuentes": activo,
  };
  for (const path of capas.aristas.children) {
    const v = visibles[path.dataset.clave];
    path.dataset.visible = v === undefined ? "true" : String(v);
  }
  $("#diagrama-desc").textContent = activo
    ? `Intermediario activado: ${DESCRIPCION[color] ?? DESCRIPCION.inactivo}.`
    : "Intermediario apagado: tu agente y el comercio se hablan directo.";
}

// Mensajes que viajan por las flechas, de a uno.
const cola = [];
let animando = false;

function encolar(flujos) {
  if (!flujos.length) return;
  cola.push(...flujos);
  if (cola.length > 12) cola.splice(0, cola.length - 12);
  if (!animando) siguienteFlujo();
}

function siguienteFlujo() {
  const f = cola.shift();
  if (!f) { animando = false; return; }
  animando = true;
  animarFlujo(f, cola.length > 3 ? 280 : 560).then(siguienteFlujo);
}

function animarFlujo(f, duracion) {
  return new Promise((listo) => {
    const path = capas.aristas?.querySelector(`.arista[data-clave="${f.clave}"]`);
    if (!path || document.hidden) { listo(); return; }
    path.classList.add("activa");
    setTimeout(() => path.classList.remove("activa"), duracion + 380);
    if (reducido()) { setTimeout(listo, 180); return; }
    const largo = path.getTotalLength();
    const punto = el("circle", { r: 5.5, class: "mensaje", "data-tono": f.tono }, capas.mensajes);
    const inicio = performance.now();
    const curva = (p) => (p < 0.5 ? 4 * p * p * p : 1 - (-2 * p + 2) ** 3 / 2);
    const cuadro = (t) => {
      const p = Math.min(1, (t - inicio) / duracion);
      const q = curva(p);
      const pt = path.getPointAtLength((f.invertida ? 1 - q : q) * largo);
      punto.setAttribute("cx", pt.x.toFixed(1));
      punto.setAttribute("cy", pt.y.toFixed(1));
      if (p < 1) requestAnimationFrame(cuadro);
      else { punto.remove(); listo(); }
    };
    requestAnimationFrame(cuadro);
  });
}

angosto.addEventListener("change", construirDiagrama);

// ---------------------------------------------------------------- relato

const relato = $("#relato");

function crearLinea(l, animar) {
  const li = document.createElement("li");
  li.className = `linea${animar ? " entra" : ""}`;
  li.dataset.id = l.id;
  const t = document.createElement("time");
  t.dateTime = l.ts;
  const marca = document.createElement("span");
  marca.className = "marca-actor";
  marca.setAttribute("aria-hidden", "true");
  const tx = document.createElement("span");
  tx.className = "texto";
  li.append(t, marca, tx);
  actualizarLinea(li, l);
  return li;
}

function actualizarLinea(li, l) {
  const [t, , tx] = li.children;
  if (t.textContent !== l.hora) t.textContent = l.hora;
  if (tx.textContent !== l.texto) tx.textContent = l.texto;
  li.dataset.tono = l.tono;
  li.dataset.actor = l.actor;
}

// El relato queda pegado al final mientras el usuario no suba a leer.
let pegado = true;
const esScrollable = () => relato.scrollHeight > relato.clientHeight + 4;
relato.addEventListener("scroll", () => {
  pegado = relato.scrollHeight - relato.scrollTop - relato.clientHeight < 40;
}, { passive: true });
new ResizeObserver(() => { if (pegado && esScrollable()) relato.scrollTop = relato.scrollHeight; }).observe(relato);

function renderRelato(items) {
  // En una grabación no se sabe si el proxy tenía filtro: no se afirma.
  const lineas = armarRelato(items, { filtro: !estado.replay && estado.proxy.filtro });
  const fin = desenlace(items);
  const cercaDelFinal = pegado || !esScrollable();
  const ids = new Set(lineas.map((l) => l.id));
  for (const li of [...relato.children]) if (!ids.has(li.dataset.id)) li.remove();
  let ultimaNueva = null;
  lineas.forEach((l, i) => {
    let li = relato.children[i];
    if (li?.dataset.id !== l.id) {
      const existente = [...relato.children].find((x) => x.dataset.id === l.id);
      if (existente) relato.insertBefore(existente, li ?? null);
      else {
        const nueva = crearLinea(l, !enArranque());
        relato.insertBefore(nueva, li ?? null);
        ultimaNueva = nueva;
      }
      li = relato.children[i];
    }
    actualizarLinea(li, l);
    li.dataset.final = String(Boolean(fin && fin.item.id === l.id));
  });
  $("#relato-vacio").hidden = lineas.length > 0;
  if (ultimaNueva && cercaDelFinal) {
    const suave = reducido() || enArranque() ? "auto" : "smooth";
    if (esScrollable()) {
      relato.scrollTo({ top: relato.scrollHeight, behavior: suave });
      pegado = true;
    } else {
      ultimaNueva.scrollIntoView({ block: "nearest", behavior: suave });
    }
  }
}

// ---------------------------------------------------------------- contraste con y sin intermediario

function escenarioActivo() {
  return estado.demo.escenario ?? estado.escenarios.activo ?? null;
}

function registrarCorrida(items) {
  if (estado.replay) return;
  const fin = desenlace(items);
  if (!fin) return;
  const clave = `${estado.demo.sesion?.creada}|${fin.item.id}`;
  if (estado.guardadas.has(clave)) return;
  estado.guardadas.add(clave);
  const delProxy = [...items].reverse().find((i) => i.origen === "proxy" && typeof i.guard_activo === "boolean");
  const escenario = [...items].reverse().find((i) => i.escenario)?.escenario ?? escenarioActivo();
  const resultado = resultadoCorrida(items, {
    escenario, guardActivo: delProxy ? delProxy.guard_activo : estado.proxy.guardActivo !== false,
  });
  estado.comparacion = guardarResultado(estado.comparacion, resultado);
  guardarPref("demo.comparacion", JSON.stringify(estado.comparacion));
}

function renderContraste() {
  const par = estado.comparacion[escenarioActivo()] ?? {};
  const { sin, con } = resumenComparacion(par.sin ?? null, par.con ?? null);
  $("#contraste").hidden = !(sin || con) || Boolean(estado.replay);
  for (const [id, valor] of [["#contraste-sin", sin], ["#contraste-con", con]]) {
    const p = $(id);
    const textoNuevo = valor ?? "Todavía no la corriste así";
    if (p.textContent !== textoNuevo) p.textContent = textoNuevo;
    p.dataset.vacio = String(!valor);
  }
}

// ---------------------------------------------------------------- input único

const formulario = $("#formulario");
const entrada = $("#entrada");

function pedidoDeEjemplo() {
  const activo = escenarioActivo();
  for (const g of estado.escenarios.grupos || []) {
    for (const o of g.opciones || []) if (o.id === activo) return o.pedido ?? null;
  }
  return null;
}

function modoActual() {
  const m = modoDelInput({
    compras: estado.compras, ocupado: estado.demo.ocupado || estado.enviando,
    desde: estado.demo.sesion?.creada, ahora: Date.now(), replay: Boolean(estado.replay),
  });
  if (m.modo === "agente" && pedidoDeEjemplo()) m.placeholder = "¿Qué querés comprar?";
  return m;
}

function renderInput(lineasVisibles) {
  const m = modoActual();
  formulario.dataset.modo = m.modo;
  const etiqueta = $("#etiqueta");
  if (etiqueta.textContent !== m.etiqueta) etiqueta.textContent = m.etiqueta;
  if (entrada.placeholder !== m.placeholder) entrada.placeholder = m.placeholder;
  entrada.readOnly = m.modo === "replay";
  $("#enviar").setAttribute("aria-disabled", String(!m.enviar));
  const ejemplo = $("#ejemplo");
  ejemplo.hidden = !(m.modo === "agente" && pedidoDeEjemplo() && !entrada.value && !lineasVisibles);
}

function avisar(textoAviso, tono = "") {
  const p = $("#aviso-entrada");
  p.textContent = textoAviso;
  p.dataset.tono = tono;
}

async function enviar(ev) {
  ev.preventDefault();
  const valor = entrada.value.trim();
  if (!valor) return;
  let m = modoActual();
  if (m.modo === "ocupado") {
    // Puede que nuestro agente acabe de preguntar y el sondeo todavía no llegó.
    await refrescarCompras();
    m = modoActual();
  }
  if (!m.enviar) { avisar(m.modo === "ocupado" ? "Tu agente todavía está trabajando: esperá su respuesta." : m.etiqueta); return; }
  estado.enviando = true;
  avisar("");
  programarRender();
  const r = await pedir("/api/demo/mensaje", { metodo: "POST", cuerpo: { texto: valor }, timeout: 15000 });
  estado.enviando = false;
  if (r.status === 200 || r.status === 202) {
    entrada.value = "";
    refrescarEstado();
    refrescarCompras();
  } else if (r.status === 422) {
    avisar("Nuestro agente espera un sí o un no.", "error");
  } else if (r.status === 409 && r.datos?.error === "agente_ocupado") {
    avisar("Tu agente todavía está trabajando: esperá su respuesta.", "error");
  } else if (r.status === 409 || r.status === 404) {
    avisar("Esa pregunta ya no está pendiente.", "error");
    refrescarCompras();
  } else if (r.status === 503) {
    avisar("No se encontró el token del proxy: ¿está levantado?", "error");
  } else {
    avisar("No se pudo enviar. ¿Está levantado el panel?", "error");
  }
  programarRender();
}

formulario.addEventListener("submit", enviar);
entrada.addEventListener("input", () => programarRender());
$("#ejemplo").addEventListener("click", () => {
  const p = pedidoDeEjemplo();
  if (!p) return;
  entrada.value = p;
  entrada.focus();
  programarRender();
});

// ---------------------------------------------------------------- controles

function construirSelector() {
  const contenedor = $("#escenarios");
  contenedor.replaceChildren();
  for (const g of estado.escenarios.grupos || []) {
    const grupo = document.createElement("div");
    grupo.className = "grupo";
    grupo.setAttribute("role", "group");
    grupo.setAttribute("aria-label", g.categoria);
    const nombre = document.createElement("span");
    nombre.className = "grupo-nombre";
    nombre.textContent = g.categoria;
    nombre.setAttribute("aria-hidden", "true");
    grupo.append(nombre);
    for (const o of g.opciones || []) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "opcion";
      b.dataset.escenario = o.id;
      b.textContent = o.etiqueta;
      b.setAttribute("aria-pressed", "false");
      b.setAttribute("aria-label", `${g.categoria}: ${o.etiqueta}`);
      b.addEventListener("click", () => elegirEscenario(o.id));
      grupo.append(b);
    }
    contenedor.append(grupo);
  }
}

async function elegirEscenario(id) {
  if (estado.replay) detenerReplay();
  const r = await pedir("/api/demo/escenario", { metodo: "POST", cuerpo: { escenario: id } });
  if (r.ok) {
    estado.escenarios.activo = r.datos.activo;
    avisar("");
    await refrescarEstado();
    entrada.focus();
  } else {
    avisar("No se pudo cambiar el comercio.", "error");
  }
  programarRender();
}

const interruptor = $("#intermediario");
interruptor.addEventListener("click", async () => {
  if (estado.cambiando || estado.replay || estado.proxy.conectado !== true) return;
  const nuevo = !(estado.proxy.guardActivo === true);
  estado.cambiando = true;
  interruptor.setAttribute("aria-busy", "true");
  // Primero la compra nueva, así el cambio queda en el relato de esta corrida.
  await pedir("/api/demo/nueva", { metodo: "POST", cuerpo: {} });
  const r = await pedir("/api/proxy/guard/state", { metodo: "POST", cuerpo: { guard_activo: nuevo } });
  if (r.ok && typeof r.datos?.guard_activo === "boolean") estado.proxy.guardActivo = r.datos.guard_activo;
  else avisar("No se pudo cambiar el intermediario.", "error");
  estado.cambiando = false;
  interruptor.removeAttribute("aria-busy");
  await refrescarEstado();
  programarRender();
});

$("#nueva").addEventListener("click", async () => {
  if (estado.replay) detenerReplay();
  await pedir("/api/demo/nueva", { metodo: "POST", cuerpo: {} });
  avisar("");
  await refrescarEstado();
  entrada.focus();
});

$("#auto").addEventListener("click", async () => {
  const r = await pedir("/api/demo/auto", { metodo: "POST", cuerpo: { auto: !estado.demo.auto } });
  if (r.ok) estado.demo.auto = r.datos.auto;
  programarRender();
});

// Tema: el mismo que el panel (psg.tema).
const TEMAS = ["auto", "light", "dark"];
const NOMBRE_TEMA = { auto: "Tema: automático", light: "Tema: claro", dark: "Tema: oscuro" };
function aplicarTema(tema) {
  if (tema === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = tema;
  const b = $("#tema");
  b.dataset.tema = tema;
  b.setAttribute("aria-label", NOMBRE_TEMA[tema]);
  b.title = NOMBRE_TEMA[tema];
}
$("#tema").addEventListener("click", () => {
  const actual = $("#tema").dataset.tema || "auto";
  const siguiente = TEMAS[(TEMAS.indexOf(actual) + 1) % TEMAS.length];
  guardarPref("tema", siguiente);
  aplicarTema(siguiente);
});

// ---------------------------------------------------------------- replay (plan B)

const grabacion = $("#grabacion");
grabacion.addEventListener("change", () => programarRender());
$("#reproducir").addEventListener("click", () => (estado.replay ? detenerReplay() : reproducir(grabacion.value)));

async function reproducir(nombre) {
  if (!nombre) return;
  const r = await pedir(`/api/replay/${encodeURIComponent(nombre)}`);
  if (!r.ok || !Array.isArray(r.datos)) { avisar("No se pudo leer la grabación.", "error"); return; }
  estado.replay = { nombre, todos: fusionarEventos([], r.datos), eventos: [], inicio: Date.now(), temporizador: null };
  avisar(`Reproduciendo ${nombre}`);
  pasoReplay();
}

function pasoReplay() {
  const rep = estado.replay;
  if (!rep) return;
  const i = rep.eventos.length;
  if (i >= rep.todos.length) { avisar("Fin de la grabación."); programarRender(); return; }
  rep.eventos = rep.todos.slice(0, i + 1);
  programarRender();
  const actual = Date.parse(rep.todos[i].ts);
  const proximo = rep.todos[i + 1] ? Date.parse(rep.todos[i + 1].ts) : actual;
  const espera = Math.min(1400, Math.max(300, (proximo - actual) || 300));
  rep.temporizador = setTimeout(pasoReplay, espera);
}

function detenerReplay() {
  if (!estado.replay) return;
  clearTimeout(estado.replay.temporizador);
  estado.replay = null;
  avisar("");
  programarRender();
}

// ---------------------------------------------------------------- render

let pendiente = false;
function programarRender() {
  if (pendiente) return;
  pendiente = true;
  requestAnimationFrame(() => { pendiente = false; render(); });
}

function itemsActuales() {
  if (estado.replay) return lineaDeTiempo(estado.replay.eventos, [], null);
  return lineaDeTiempo(estado.eventos, estado.demo.mensajes, estado.demo.sesion?.creada);
}

function renderControles() {
  const activo = escenarioActivo();
  for (const b of document.querySelectorAll(".opcion")) {
    const pressed = String(b.dataset.escenario === activo);
    if (b.getAttribute("aria-pressed") !== pressed) b.setAttribute("aria-pressed", pressed);
  }
  const conectado = estado.proxy.conectado === true;
  const on = estado.proxy.guardActivo === true;
  interruptor.disabled = !conectado || Boolean(estado.replay);
  interruptor.setAttribute("aria-checked", String(on));
  const textoInterruptor = estado.proxy.conectado === false ? "Intermediario: sin conexión"
    : estado.proxy.guardActivo === null ? "Intermediario: …"
      : on ? "Intermediario: activado" : "Intermediario: desactivado";
  const nodoTexto = interruptor.querySelector(".interruptor-texto");
  if (nodoTexto.textContent !== textoInterruptor) nodoTexto.textContent = textoInterruptor;
  const auto = $("#auto");
  auto.setAttribute("aria-pressed", String(estado.demo.auto));
  auto.textContent = `Respuestas automáticas: ${estado.demo.auto ? "sí" : "no"}`;
  const rep = $("#reproducir");
  rep.disabled = !estado.replay && !grabacion.value;
  rep.textContent = estado.replay ? "Detener" : "Reproducir";
}

function render() {
  const items = itemsActuales();
  registrarCorrida(items);
  renderRelato(items);
  avanzarDiagrama(items);
  renderInput(relato.children.length > 0);
  renderControles();
  renderContraste();
}

// ---------------------------------------------------------------- arranque

aplicarTema(TEMAS.includes(leerPref("tema", "auto")) ? leerPref("tema", "auto") : "auto");
construirDiagrama();
cargarEscenarios();
cargarGrabaciones();
refrescarEstado();
refrescarCompras();
refrescarProxy();
conectarEventos();
setInterval(() => { if (!document.hidden) refrescarEstado(); }, 900);
setInterval(() => { if (!document.hidden) refrescarCompras(); }, 1500);
setInterval(() => { if (!document.hidden) refrescarProxy(); }, 4000);
setInterval(() => programarRender(), 5000); // los plazos vencen solos
