// Dashboard de la demo del Pre-Sign Guard. JS vanilla, sin build ni CDN.
// Fuentes de eventos: "En vivo (proxy)" por SSE o "Replay (archivo)".

import {
  aJsonl, agruparCompras, cuerpoAprobacion, estadoAprobacion, fusionarEventos,
} from "./lib/eventos.js";
import { decodificarSdJwt, resolverSdJwt, sha256B64url, verificarRecibo } from "./lib/jose.js";
import { renderBenchmark } from "./views/benchmark.js";
import { renderFicha } from "./views/ficha.js";
import { renderFlujo } from "./views/flujo.js";
import { renderMomento } from "./views/momento.js";
import { renderRecibo } from "./views/recibo.js";
import { renderVeredicto } from "./views/veredicto.js";
import { h, icono } from "./views/comunes.js";

const VISTAS = {
  momento: renderMomento,
  flujo: renderFlujo,
  veredicto: renderVeredicto,
  recibo: renderRecibo,
  ficha: renderFicha,
  benchmark: renderBenchmark,
};
const ORDEN_VISTAS = Object.keys(VISTAS);

const estado = {
  vista: "momento",
  config: { proxy_url: "http://127.0.0.1:8010" },
  fuente: "vivo",
  replays: null, // null = todavía cargando
  replaySel: null,
  replayTodos: [],
  replayInvalidas: 0,
  reproduciendo: false,
  eventos: [],
  compraSel: null,
  seguirUltima: true,
  escenarioMomento: "B",
  momentoSin: null,
  momentoCon: null,
  escenarioFicha: null,
  proxy: { conectado: null, guard_activo: null, evaluador: null, merchant_url: null, cambiando: false },
  // GET /guard/aprobaciones del proxy (solo en vivo).
  aprobaciones: { pendientes: [], usadas: [] },
  sse: "cerrado",
  fixtures: [],
  catalogo: null,
  resultados: [],
  resultadoSel: null,
  resultado: null,
  resultadoError: null,
};

const cache = {
  fixtures: new Map(),
  fichas: new Map(),
  hashes: new Map(),
  mandatos: new Map(),
  verificaciones: new Map(),
};
const detallesAbiertos = new Set();
const nuevos = new Set();
const aprobando = new Set(); // contextos con un POST de aprobación en curso
const mensajesAprobacion = new Map(); // contexto → {ok, texto}

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

function leerPref(clave, porDefecto) {
  try { return localStorage.getItem(`psg.${clave}`) ?? porDefecto; } catch { return porDefecto; }
}
function guardarPref(clave, valor) {
  try { valor === null ? localStorage.removeItem(`psg.${clave}`) : localStorage.setItem(`psg.${clave}`, valor); } catch { /* sin storage */ }
}

async function pedirJson(url, opciones = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), opciones.timeout ?? 6000);
  try {
    const r = await fetch(url, { ...opciones, signal: ctrl.signal, headers: { Accept: "application/json", ...(opciones.headers || {}) } });
    let datos = null;
    try { datos = await r.json(); } catch { datos = null; }
    if (!r.ok) {
      const e = new Error(datos?.error ?? `HTTP ${r.status}`);
      e.status = r.status;
      e.datos = datos;
      throw e;
    }
    return { datos, respuesta: r };
  } finally {
    clearTimeout(t);
  }
}

function anunciar(texto) {
  const region = $("#anuncios");
  region.textContent = "";
  setTimeout(() => { region.textContent = texto; }, 30);
}

let renderPendiente = false;
function programarRender() {
  if (renderPendiente) return;
  renderPendiente = true;
  requestAnimationFrame(() => { renderPendiente = false; render(); });
}

// ---------------------------------------------------------------------------
// Contexto que reciben las vistas
// ---------------------------------------------------------------------------

let derivados = { compras: [], sistema: [] };
let observador = null;

const tooltip = {
  mostrar(ev, valor, rotulo) {
    const el = $("#tooltip");
    el.replaceChildren(h("strong", { text: valor }), h("span", { text: rotulo }));
    el.hidden = false;
    let x; let y;
    if (ev.type === "focus") {
      const r = ev.currentTarget.getBoundingClientRect();
      x = r.left + r.width / 2; y = r.top;
    } else { x = ev.clientX; y = ev.clientY; }
    const ancho = el.offsetWidth;
    el.style.left = `${Math.min(window.innerWidth - ancho - 8, Math.max(8, x - ancho / 2))}px`;
    el.style.top = `${Math.max(8, y - el.offsetHeight - 12)}px`;
  },
  ocultar() { $("#tooltip").hidden = true; },
};

function ctx() {
  const { compras, sistema } = derivados;
  return {
    estado,
    eventos: estado.eventos,
    compras,
    sistema,
    fixtures: estado.fixtures,
    catalogo: estado.catalogo,
    detallesAbiertos,
    tooltip,
    set(patch) {
      Object.assign(estado, patch);
      render();
    },
    esNuevo: (id) => nuevos.has(id),
    fixture: (esc) => cargarPerezoso(cache.fixtures, esc, `/api/fixtures/${encodeURIComponent(esc)}`),
    ficha: (esc) => cargarPerezoso(cache.fichas, esc, `/api/fixtures/${encodeURIComponent(esc)}/ficha`),
    compraActual() {
      if (!estado.seguirUltima && estado.compraSel) {
        const c = compras.find((x) => x.contextoId === estado.compraSel);
        if (c) return c;
      }
      return compras[0] ?? null;
    },
    compraConVeredicto() {
      if (!estado.seguirUltima && estado.compraSel) {
        const c = compras.find((x) => x.contextoId === estado.compraSel);
        if (c?.veredicto) return c;
      }
      return compras.find((c) => c.veredicto) ?? null;
    },
    hashCoincide,
    mandato,
    estadoAprobacion: (compra) => estadoAprobacion(compra, estado.aprobaciones),
    aprobar,
    aprobando: (contextoId) => aprobando.has(contextoId),
    mensajeAprobacion: (contextoId) => mensajesAprobacion.get(contextoId) ?? null,
    verificacion: (recibo) => cache.verificaciones.get(claveRecibo(recibo)) ?? null,
    verificar,
    irARecibo(compra) {
      Object.assign(estado, { compraSel: compra.contextoId, seguirUltima: false });
      irAVista("recibo");
    },
    descargarJsonl,
    cargarResultado,
    observarAncho(el, fn) {
      if (!("ResizeObserver" in window)) return;
      let ultimo = el.clientWidth;
      observador = new ResizeObserver(() => {
        if (Math.abs(el.clientWidth - ultimo) > 24) { ultimo = el.clientWidth; fn(el); }
      });
      observador.observe(el);
    },
  };
}

function cargarPerezoso(mapa, clave, url) {
  if (!clave) return null;
  if (mapa.has(clave)) return mapa.get(clave);
  mapa.set(clave, null);
  pedirJson(url).then(({ datos }) => { mapa.set(clave, datos); programarRender(); }).catch(() => { /* queda null */ });
  return null;
}

function hashCoincide(jwt, hash) {
  const clave = `${hash}|${jwt}`;
  if (cache.hashes.has(clave)) return cache.hashes.get(clave);
  cache.hashes.set(clave, null);
  if (globalThis.crypto?.subtle) {
    sha256B64url(jwt).then((calc) => { cache.hashes.set(clave, calc === hash); programarRender(); }).catch(() => {});
  }
  return null;
}

function mandato(token) {
  const clave = typeof token === "string" ? token : JSON.stringify(token);
  if (cache.mandatos.has(clave)) return cache.mandatos.get(clave);
  if (typeof token !== "string") {
    const dec = { jwt: null, header: null, claims: token };
    cache.mandatos.set(clave, dec);
    return dec;
  }
  cache.mandatos.set(clave, null);
  (async () => {
    try {
      const cadena = decodificarSdJwt(token);
      const ultimo = cadena[cadena.length - 1];
      const { resuelto } = globalThis.crypto?.subtle
        ? await resolverSdJwt(ultimo.emisor.payload, ultimo.divulgaciones)
        : { resuelto: ultimo.emisor.payload };
      const s = ultimo.emisor.segmentos;
      cache.mandatos.set(clave, { jwt: `${s.header}.${s.payload}.${s.firma}`, header: ultimo.emisor.header, claims: resuelto });
    } catch (e) {
      cache.mandatos.set(clave, { error: e.message });
    }
    programarRender();
  })();
  return null;
}

const claveRecibo = (r) => `${r?.id}|${r?.firma}`;

async function verificar(recibo) {
  const clave = claveRecibo(recibo);
  cache.verificaciones.set(clave, { estado: "verificando" });
  render();
  try {
    if (!globalThis.crypto?.subtle) {
      throw new Error("WebCrypto no está disponible en esta página: abrila en http://localhost (no por IP de red).");
    }
    let pk;
    try {
      ({ datos: pk } = await pedirJson("/api/public-key"));
    } catch (e) {
      throw new Error(e.status === 404
        ? "No hay clave pública del Guard: el proxy no la sirve y no está .temp-db/guard_signing_key.pem."
        : `No se pudo pedir la clave pública: ${e.message}`);
    }
    const resultado = await verificarRecibo(recibo, pk);
    cache.verificaciones.set(clave, { estado: "listo", resultado, clave: pk });
    anunciar(resultado.ok ? "Recibo verificado: lo firmó el Guard y no cambió." : "La verificación del recibo falló.");
  } catch (e) {
    cache.verificaciones.set(clave, { estado: "error", mensaje: e.message });
    anunciar(e.message);
  }
  render();
}

function descargarJsonl() {
  const blob = new Blob([aJsonl(estado.eventos)], { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob);
  const sello = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
  const a = h("a", { href: url, download: `ensayo-${sello}.jsonl` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------------------
// Eventos: en vivo (SSE) y replay
// ---------------------------------------------------------------------------

let fuenteSse = null;
let reintentoSse = null;
let buffer = [];
let primerLote = true;

function ingerir(lista, { marcarNuevos = true } = {}) {
  buffer.push(...lista);
  if (buffer.length === 0) return;
  requestAnimationFrame(() => {
    if (buffer.length === 0) return;
    const antes = new Set(estado.eventos.map((e) => e.id));
    const lote = buffer;
    buffer = [];
    estado.eventos = fusionarEventos(estado.eventos, lote);
    if (marcarNuevos && !primerLote) {
      for (const e of lote) {
        if (antes.has(e.id)) continue;
        nuevos.add(e.id);
        setTimeout(() => nuevos.delete(e.id), 1600);
        if (e.tipo === "bloqueado" && e.datos?.requiere_aprobacion) {
          anunciar(`Retenido: requiere aprobación humana. ${e.datos?.motivo ?? ""}`);
          refrescarAprobaciones();
        } else if (e.tipo === "bloqueado") anunciar(`Bloqueado antes de la firma: ${e.datos?.motivo ?? ""}`);
        else if (e.tipo === "reenviado" && e.datos?.aprobacion_humana) {
          anunciar("Aprobado por humano: el checkout pasó intacto");
          refrescarAprobaciones();
        }
        else if (e.tipo === "veredicto") anunciar(`Veredicto del Guard: ${e.datos?.veredicto?.status ?? ""}`);
      }
    }
    primerLote = false;
    render();
  });
}

function cerrarSse() {
  clearTimeout(reintentoSse);
  if (fuenteSse) { fuenteSse.close(); fuenteSse = null; }
  estado.sse = "cerrado";
}

function conectarSse() {
  cerrarSse();
  if (estado.fuente !== "vivo") return;
  primerLote = true;
  estado.sse = "conectando";
  const es = new EventSource("/api/proxy/guard/events");
  fuenteSse = es;
  // Ventana corta para tratar como "carga inicial" los 200 que reenvía el proxy.
  setTimeout(() => { primerLote = false; }, 1200);
  es.onopen = () => { estado.sse = "abierto"; renderBarra(); };
  es.addEventListener("evento", (m) => {
    try { ingerir([JSON.parse(m.data)]); } catch { /* línea inválida */ }
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      estado.sse = "error";
      renderBarra();
      reintentoSse = setTimeout(conectarSse, 3000);
    } else {
      estado.sse = "conectando";
      renderBarra();
    }
  };
}

let temporizadorReplay = null;

async function cargarReplay(nombre) {
  detenerReplay();
  estado.replaySel = nombre;
  guardarPref("replay", nombre);
  estado.eventos = [];
  estado.replayTodos = [];
  if (!nombre) { render(); return; }
  try {
    const { datos, respuesta } = await pedirJson(`/api/replay/${encodeURIComponent(nombre)}`);
    estado.replayTodos = fusionarEventos([], datos || []);
    estado.replayInvalidas = Number(respuesta.headers.get("X-Lineas-Invalidas") || 0);
    estado.eventos = estado.replayTodos;
  } catch (e) {
    estado.replayInvalidas = 0;
    anunciar(`No se pudo leer el replay: ${e.message}`);
  }
  render();
}

function detenerReplay() {
  clearTimeout(temporizadorReplay);
  temporizadorReplay = null;
  if (estado.reproduciendo) { estado.reproduciendo = false; estado.eventos = estado.replayTodos; }
}

function reproducirReplay() {
  if (estado.reproduciendo) { detenerReplay(); render(); return; }
  const todos = estado.replayTodos;
  if (!todos.length) return;
  estado.reproduciendo = true;
  estado.eventos = [];
  estado.seguirUltima = true;
  primerLote = false;
  let i = 0;
  const paso = () => {
    if (!estado.reproduciendo) return;
    ingerir([todos[i]]);
    i += 1;
    if (i >= todos.length) { estado.reproduciendo = false; programarRender(); return; }
    const gap = Date.parse(todos[i].ts) - Date.parse(todos[i - 1].ts);
    temporizadorReplay = setTimeout(paso, Math.max(300, Math.min(1400, Number.isFinite(gap) ? gap : 700)));
  };
  paso();
  render();
}

async function cambiarFuente(fuente) {
  if (estado.fuente === fuente) return;
  estado.fuente = fuente;
  guardarPref("fuente", fuente);
  detenerReplay();
  estado.eventos = [];
  nuevos.clear();
  mensajesAprobacion.clear();
  Object.assign(estado, {
    compraSel: null, seguirUltima: true, momentoSin: null, momentoCon: null,
    aprobaciones: { pendientes: [], usadas: [] },
  });
  if (fuente === "vivo") {
    conectarSse();
    render();
  } else {
    cerrarSse();
    await refrescarReplays();
    const nombre = estado.replays.some((r) => r.nombre === estado.replaySel) ? estado.replaySel : (estado.replays[0]?.nombre ?? null);
    await cargarReplay(nombre);
  }
}


async function refrescarReplays() {
  try { estado.replays = (await pedirJson("/api/replay")).datos || []; } catch { estado.replays = []; }
}

// ---------------------------------------------------------------------------
// Proxy: estado e interruptor
// ---------------------------------------------------------------------------

async function refrescarProxy() {
  try {
    const { datos } = await pedirJson("/api/proxy/guard/state", { timeout: 3500 });
    estado.proxy = { ...estado.proxy, conectado: true, guard_activo: datos.guard_activo, evaluador: datos.evaluador, merchant_url: datos.merchant_url };
  } catch {
    estado.proxy = { ...estado.proxy, conectado: false };
  }
  renderBarra();
  if (estado.proxy.conectado) refrescarAprobaciones();
}

// ---------------------------------------------------------------------------
// Aprobación humana de un checkout retenido (escalar)
// ---------------------------------------------------------------------------

async function refrescarAprobaciones() {
  if (estado.fuente !== "vivo") return;
  try {
    const { datos } = await pedirJson("/api/proxy/guard/aprobaciones", { timeout: 3500 });
    if (!datos || !Array.isArray(datos.pendientes)) return;
    const nuevas = { pendientes: datos.pendientes, usadas: Array.isArray(datos.usadas) ? datos.usadas : [] };
    if (JSON.stringify(nuevas) !== JSON.stringify(estado.aprobaciones)) {
      estado.aprobaciones = nuevas;
      programarRender();
    }
  } catch { /* proxy caído: queda lo último que se vio */ }
}

/** "Aprobar igual": POST /guard/aprobaciones (vía el servidor del panel). */
async function aprobar(compra) {
  const cuerpo = cuerpoAprobacion(compra);
  if (!cuerpo || aprobando.has(compra.contextoId)) return;
  aprobando.add(compra.contextoId);
  render();
  try {
    await pedirJson("/api/proxy/guard/aprobaciones", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cuerpo),
    });
    mensajesAprobacion.set(compra.contextoId, { ok: true, texto: "Aprobación registrada: pedile al agente que reintente el checkout." });
    anunciar("Aprobado por humano: pedile al agente que reintente el checkout");
  } catch (e) {
    const texto = e.status === 409
      ? "El proxy no tiene un escalar vigente para esta compra (puede que ya haya otro intento)."
      : `No se pudo registrar la aprobación: ${e.message}`;
    mensajesAprobacion.set(compra.contextoId, { ok: false, texto });
    anunciar(texto);
  } finally {
    aprobando.delete(compra.contextoId);
    await refrescarAprobaciones();
    render();
  }
}

async function cambiarGuard() {
  if (!estado.proxy.conectado || estado.proxy.cambiando) return;
  const nuevo = !estado.proxy.guard_activo;
  estado.proxy.cambiando = true;
  renderBarra();
  try {
    const { datos } = await pedirJson("/api/proxy/guard/state", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ guard_activo: nuevo }),
    });
    estado.proxy.guard_activo = typeof datos?.guard_activo === "boolean" ? datos.guard_activo : nuevo;
    anunciar(estado.proxy.guard_activo ? "Guard activado en el proxy" : "Guard apagado en el proxy");
  } catch {
    estado.proxy.conectado = false;
    anunciar("Proxy desconectado: no se pudo cambiar el Guard");
  } finally {
    estado.proxy.cambiando = false;
    await refrescarProxy();
  }
}

// ---------------------------------------------------------------------------
// Benchmark
// ---------------------------------------------------------------------------

async function cargarResultado(id) {
  estado.resultadoSel = id;
  const url = id ? `/api/results/${id.split("/").map(encodeURIComponent).join("/")}` : "/api/results/latest";
  try {
    estado.resultado = (await pedirJson(url)).datos;
    estado.resultadoError = null;
  } catch (e) {
    estado.resultado = null;
    estado.resultadoError = e.status === 404 ? "No hay resultados del benchmark" : `No se pudieron leer los resultados: ${e.message}`;
  }
  render();
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderBarra() {
  // Fuente
  for (const f of ["vivo", "replay"]) {
    $(`#fuente-${f}`).setAttribute("aria-pressed", String(estado.fuente === f));
  }
  const replayCtl = $("#replay-ctl");
  replayCtl.hidden = estado.fuente !== "replay";
  if (estado.fuente === "replay") {
    const sel = $("#replay-archivo");
    const lista = estado.replays ?? [];
    const opciones = lista.length
      ? lista.map((r) => h("option", { value: r.nombre, selected: r.nombre === estado.replaySel ? true : null,
        text: `${r.nombre} · ${r.eventos} eventos · ${r.origen}` }))
      : [h("option", { value: "", text: estado.replays === null ? "Cargando archivos…" : "No hay archivos .jsonl" })];
    sel.replaceChildren(...opciones);
    sel.disabled = lista.length === 0;
    const play = $("#replay-play");
    play.disabled = estado.replayTodos.length === 0;
    play.replaceChildren(icono(estado.reproduciendo ? "stop" : "play"), estado.reproduciendo ? "Detener" : "Reproducir");
    $("#replay-info").textContent = estado.replayInvalidas ? `${estado.replayInvalidas} líneas inválidas descartadas` : "";
  }

  // Proxy
  const p = estado.proxy;
  const chipProxy = $("#proxy-estado");
  chipProxy.className = `estado-proxy t-${p.conectado === true ? "ok" : p.conectado === false ? "crit" : "neutro"}`;
  const sse = estado.fuente === "vivo"
    ? ({ abierto: " · eventos en vivo", conectando: " · conectando eventos…", error: " · sin eventos" }[estado.sse] ?? "") : "";
  chipProxy.replaceChildren(
    h("span", { class: "punto", "aria-hidden": "true" }),
    h("span", { text: p.conectado === true ? `Proxy conectado${p.evaluador ? ` · evaluador ${p.evaluador}` : ""}${sse}`
      : p.conectado === false ? `proxy desconectado (${estado.config.proxy_url.replace(/^https?:\/\//, "")})` : "consultando proxy…" }));

  const sw = $("#guard-switch");
  const activo = p.guard_activo === true;
  sw.setAttribute("aria-checked", String(activo));
  sw.disabled = p.conectado !== true || p.cambiando;
  sw.querySelector(".switch-texto").textContent = p.conectado !== true ? "Guard: sin proxy"
    : p.cambiando ? "Guard: cambiando…" : activo ? "Guard activo" : "Guard apagado";
  sw.title = p.conectado === true ? `POST ${estado.config.proxy_url}/guard/state` : "El proxy no responde";

  const avisoStub = $("#aviso-stub");
  avisoStub.hidden = !(p.conectado === true && p.evaluador === "stub");
}

function claveFoco(panel) {
  const activo = document.activeElement;
  if (!activo || !panel.contains(activo)) return null;
  const focusables = [...panel.querySelectorAll("button, select, summary, [tabindex], a[href], input")];
  return focusables.indexOf(activo);
}

function restaurarFoco(panel, indice) {
  if (indice === null || indice < 0) return;
  const focusables = [...panel.querySelectorAll("button, select, summary, [tabindex], a[href], input")];
  const el = focusables[Math.min(indice, focusables.length - 1)];
  if (el) el.focus({ preventScroll: true });
}

function render() {
  derivados = agruparCompras(estado.eventos);
  renderBarra();
  for (const id of ORDEN_VISTAS) {
    const activa = id === estado.vista;
    const tab = $(`#tab-${id}`);
    tab.setAttribute("aria-selected", String(activa));
    tab.tabIndex = activa ? 0 : -1;
    $(`#panel-${id}`).hidden = !activa;
  }
  const panel = $(`#panel-${estado.vista}`);
  const foco = claveFoco(panel);
  if (observador) { observador.disconnect(); observador = null; }
  tooltip.ocultar();
  try {
    VISTAS[estado.vista](panel, ctx());
  } catch (e) {
    console.error(e);
    panel.replaceChildren(h("div", { class: "vacio" }, h("p", { class: "vacio-titulo", text: "Error al dibujar esta vista" }), h("pre", { class: "json", text: String(e?.stack ?? e) })));
  }
  restaurarFoco(panel, foco);
}

function irAVista(id, { foco = false } = {}) {
  if (!VISTAS[id]) return;
  estado.vista = id;
  if (location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);
  render();
  if (foco) $(`#tab-${id}`).focus();
}

// ---------------------------------------------------------------------------
// Tema
// ---------------------------------------------------------------------------

const TEMAS = ["auto", "light", "dark"];
const NOMBRE_TEMA = { auto: "Tema: automático", light: "Tema: claro", dark: "Tema: oscuro" };
const ICONO_TEMA = { auto: "contrast", light: "sun", dark: "moon" };

function aplicarTema(tema) {
  if (tema === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = tema;
  const b = $("#tema");
  b.replaceChildren(icono(ICONO_TEMA[tema]), h("span", { class: "sr-only", text: NOMBRE_TEMA[tema] }));
  b.title = NOMBRE_TEMA[tema];
  b.dataset.tema = tema;
}

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

function enlazar() {
  $("#fuente-vivo").addEventListener("click", () => cambiarFuente("vivo"));
  $("#fuente-replay").addEventListener("click", () => cambiarFuente("replay"));
  $("#replay-archivo").addEventListener("change", (e) => cargarReplay(e.target.value || null));
  $("#replay-play").addEventListener("click", reproducirReplay);
  $("#guard-switch").addEventListener("click", cambiarGuard);
  $("#tema").addEventListener("click", () => {
    const actual = $("#tema").dataset.tema || "auto";
    const sig = TEMAS[(TEMAS.indexOf(actual) + 1) % TEMAS.length];
    guardarPref("tema", sig);
    aplicarTema(sig);
  });

  const tabs = $("#pestanas");
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("[role=tab]");
    if (b) irAVista(b.dataset.vista);
  });
  tabs.addEventListener("keydown", (e) => {
    const i = ORDEN_VISTAS.indexOf(estado.vista);
    const mapa = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: ORDEN_VISTAS.length - 1 };
    if (!(e.key in mapa)) return;
    e.preventDefault();
    irAVista(ORDEN_VISTAS[(mapa[e.key] + ORDEN_VISTAS.length) % ORDEN_VISTAS.length], { foco: true });
  });
  // Atajos 1–6 para cambiar de vista durante la demo.
  document.addEventListener("keydown", (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target.closest?.("input, select, textarea")) return;
    const n = Number(e.key);
    if (n >= 1 && n <= ORDEN_VISTAS.length) irAVista(ORDEN_VISTAS[n - 1]);
  });
  window.addEventListener("hashchange", () => {
    const id = location.hash.slice(1);
    if (VISTAS[id] && id !== estado.vista) irAVista(id);
  });
}

async function iniciar() {
  aplicarTema(TEMAS.includes(leerPref("tema", "auto")) ? leerPref("tema", "auto") : "auto");
  enlazar();
  const deHash = location.hash.slice(1);
  if (VISTAS[deHash]) estado.vista = deHash;
  estado.fuente = leerPref("fuente", "vivo") === "replay" ? "replay" : "vivo";
  estado.replaySel = leerPref("replay", null);
  render();

  const [config, fixtures, catalogo, resultados] = await Promise.allSettled([
    pedirJson("/api/config"), pedirJson("/api/fixtures"), pedirJson("/api/catalog"), pedirJson("/api/results"),
  ]);
  if (config.status === "fulfilled") estado.config = config.value.datos;
  if (fixtures.status === "fulfilled") estado.fixtures = fixtures.value.datos || [];
  if (catalogo.status === "fulfilled") estado.catalogo = catalogo.value.datos;
  if (resultados.status === "fulfilled") estado.resultados = resultados.value.datos || [];
  if (estado.fixtures.length && !estado.fixtures.some((f) => f.escenario === estado.escenarioMomento)) {
    estado.escenarioMomento = estado.fixtures[0].escenario;
  }

  // El chequeo del proxy no bloquea: con el puerto cerrado tarda segundos.
  refrescarProxy();
  cargarResultado(null);
  if (estado.fuente === "vivo") conectarSse();
  else {
    await refrescarReplays();
    const nombre = estado.replays.some((r) => r.nombre === estado.replaySel) ? estado.replaySel : (estado.replays[0]?.nombre ?? null);
    await cargarReplay(nombre);
  }
  render();
  setInterval(refrescarProxy, 4000);
}

iniciar();
