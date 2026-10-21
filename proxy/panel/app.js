// Panel del cliente de KaiAI: el controlador. La lógica está en logica.mjs, los
// nodos los arma vista.mjs y la firma del recibo la verifica recibo.mjs (el
// mismo archivo que usa la consola); acá se lee la página, se llama a /v1 y se
// pinta lo que vuelve.
//
// Reglas de esta página:
// - Todo texto que viene de la API se pone con textContent (vista.mjs).
// - La clave vive en memoria (y en sessionStorage solo si se marca "recordar
//   en esta pestaña"). Solo viaja a este mismo origen, en el header
//   Authorization, y nunca se escribe en la página.
// - Un compra_id va a una URL solo después de pasar por esIdCompra.
// - El panel no decide ni cambia nada: solo lee el historial.

import {
  ALERTAS, NOMBRE_TEMA, PERIODOS, PERIODO_POR_DEFECTO, RESULTADOS, TEMAS, chequeosDe, claveValida,
  combinarCompras, datosDeCompra, esIdCompra, filtrarPorResultado, hashDeCompra, infoResultado,
  lineaDeTiempo, mensajeEntrada, mensajeErrorApi, notaResumen, planResultado, queryListado,
  queryResumen, reciboParaVerificar, referenciaValida, rutaDe, seguirCargando, separarAlertas,
  siguienteTema, siguienteValido, tarjetasResumen, textoPeriodo, textoVerificacion, tipoDeClave,
  ventanaPeriodo,
} from "./logica.mjs";
import { crearVista } from "./vista.mjs";
import { verificarRecibo } from "./recibo.mjs";

const CLAVE_SESION = "kaiai.panel.clave";
const TEMA_SESION = "kaiai.panel.tema";
const TIEMPO_API_MS = 30_000;
const CLAVE_CAIDA = "Tu clave dejó de valer (está dada de baja o la cambiaron). Entrá de nuevo.";

const $ = (selector) => document.querySelector(selector);
const v = crearVista(document);

const estado = {
  clave: null,
  tipoClave: null,
  periodo: PERIODO_POR_DEFECTO,
  ventana: null,
  filtros: { resultado: "", alerta: "", referencia: "" },
  compras: [],
  siguiente: null,
  // Contadores: una respuesta de una sesión, una carga o un detalle viejos
  // se descarta.
  sesion: 0,
  cargaResumen: 0,
  cargaLista: 0,
  cargaDetalle: 0,
  ultimaReiniciaba: true,
  publica: null,
  detalle: null,
  abierta: null,
  vista: null,
  desdeLaLista: false,
};

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

/** Un texto de estado (role="status"), con su tono: ok, crit o espera. */
function estadoEn(selector, texto, tono = "") {
  const nodo = $(selector);
  nodo.textContent = texto || "";
  if (tono) nodo.setAttribute("data-tono", tono);
  else nodo.removeAttribute("data-tono");
}

function mostrarError(selector, texto) {
  $(`${selector}-texto`).textContent = texto;
  $(selector).hidden = false;
}

// ---------------------------------------------------------------------------
// HTTP: siempre a /v1 de este mismo origen
// ---------------------------------------------------------------------------

function leerJson(texto) {
  if (typeof texto !== "string" || !texto.trim()) return null;
  try {
    return JSON.parse(texto);
  } catch {
    return null;
  }
}

/** Un pedido a /v1. `{status, datos, autenticar}`; nunca lanza (status 0 si no hubo respuesta). */
async function pedirApi(metodo, ruta, { conClave = true } = {}) {
  if (!ruta.startsWith("/v1/")) throw new Error("ruta fuera de /v1");
  const headers = { Accept: "application/json" };
  if (conClave && estado.clave) headers.Authorization = `Bearer ${estado.clave}`;
  const control = new AbortController();
  const reloj = setTimeout(() => control.abort(), TIEMPO_API_MS);
  try {
    const respuesta = await fetch(ruta, {
      method: metodo, headers, credentials: "omit", cache: "no-store", redirect: "error",
      referrerPolicy: "no-referrer", signal: control.signal,
    });
    const texto = metodo === "HEAD" ? "" : await respuesta.text().catch(() => "");
    return {
      status: respuesta.status,
      datos: leerJson(texto),
      autenticar: respuesta.headers.get("WWW-Authenticate"),
    };
  } catch {
    return { status: 0, datos: null, autenticar: null };
  } finally {
    clearTimeout(reloj);
  }
}

function textoDeFalla(r) {
  return r.status === 0
    ? "No se pudo hablar con KaiAI. Revisá la conexión y probá de nuevo."
    : mensajeErrorApi(r.status, r.datos);
}

// ---------------------------------------------------------------------------
// Sesión: entrar y salir
// ---------------------------------------------------------------------------

function guardarSesion(clave) {
  try {
    if (clave) sessionStorage.setItem(CLAVE_SESION, clave);
    else sessionStorage.removeItem(CLAVE_SESION);
  } catch {
    // Sin sessionStorage (modo privado estricto): la clave queda solo en memoria.
  }
}

function leerSesion() {
  try {
    return sessionStorage.getItem(CLAVE_SESION);
  } catch {
    return null;
  }
}

async function entrar(evento) {
  evento?.preventDefault();
  const valor = $("#clave").value.trim();
  if (!claveValida(valor)) {
    $("#clave").setAttribute("aria-invalid", "true");
    estadoEn("#entrada-estado", "La clave tiene la forma psg_… (te la da el operador de KaiAI).", "crit");
    $("#clave").focus();
    return;
  }
  $("#clave").removeAttribute("aria-invalid");
  $("#entrar").disabled = true;
  estadoEn("#entrada-estado", "Probando la clave…", "espera");
  estado.clave = valor;
  const sesion = ++estado.sesion;
  estado.ventana = ventanaPeriodo(estado.periodo);
  // La clave se valida leyendo el resumen del período (lo que se muestra arriba).
  const r = await pedirApi("GET", `/v1/resumen${queryResumen(estado.ventana)}`);
  if (sesion !== estado.sesion) return;
  $("#entrar").disabled = false;
  if (r.status !== 200 || !r.datos) {
    estado.clave = null;
    if (r.status === 401) {
      guardarSesion(null);
      $("#clave").setAttribute("aria-invalid", "true");
    }
    estadoEn("#entrada-estado", mensajeEntrada(r.status, r.datos), "crit");
    $("#clave").focus();
    return;
  }
  guardarSesion($("#recordar").checked ? valor : null);
  $("#clave").value = "";
  ocultarClave();
  estadoEn("#entrada-estado", "");
  mostrarPanel();
  pintarResumen(r.datos);
  detectarClave(sesion);
  rutear();
  if (estado.vista === "lista") $("#t-panel").focus();
  cargarLista({ reiniciar: true });
}

/**
 * ¿Es de solo lectura? `HEAD /v1/comercios` no cambia nada: la de lectura da
 * 403 `insufficient_scope` y la completa, 200. Si no se sabe, no se dice nada.
 */
async function detectarClave(sesion) {
  const r = await pedirApi("HEAD", "/v1/comercios");
  if (sesion !== estado.sesion) return;
  estado.tipoClave = tipoDeClave(r.status, r.autenticar);
  const insignia = $("#clave-tipo");
  if (estado.tipoClave === "desconocida") {
    insignia.hidden = true;
    $("#aviso-clave").hidden = true;
    return;
  }
  const lectura = estado.tipoClave === "lectura";
  insignia.replaceChildren(lectura
    ? v.chip({ texto: "Solo lectura", tono: "ok", icono: "check" }, { prefijo: "Clave: " })
    : v.chip({ texto: "Clave completa", tono: "aviso", icono: "alerta" }));
  insignia.hidden = false;
  $("#aviso-clave").hidden = lectura;
}

function mostrarPanel() {
  $("#entrada").hidden = true;
  $("#app").hidden = false;
  $("#salir").hidden = false;
}

function vaciarPanel() {
  for (const id of ["#tarjetas", "#filas", "#d-resultado", "#d-estafa", "#d-avisos", "#d-datos",
    "#d-linea", "#d-chequeos", "#d-recibo", "#clave-tipo"]) $(id).replaceChildren();
  for (const id of ["#periodo-texto", "#d-resumen", "#resumen-nota"]) $(id).textContent = "";
  for (const id of ["#resumen-estado", "#lista-estado", "#detalle-estado", "#verificacion"]) estadoEn(id, "");
  for (const id of ["#app", "#salir", "#clave-tipo", "#aviso-clave", "#lista-error", "#detalle-error",
    "#detalle", "#tabla", "#lista-vacia", "#cargar-mas", "#vista-compra"]) $(id).hidden = true;
  $("#vista-lista").hidden = false;
}

function salir(mensaje = "Saliste: borramos la clave y los datos de esta pestaña.") {
  estado.sesion += 1;
  estado.clave = null;
  guardarSesion(null);
  Object.assign(estado, {
    tipoClave: null, ventana: null, compras: [], siguiente: null, publica: null, detalle: null,
    abierta: null, vista: null, desdeLaLista: false,
  });
  vaciarPanel();
  reiniciarFiltros();
  $("#clave").value = "";
  ocultarClave();
  $("#recordar").checked = false;
  $("#entrada").hidden = false;
  if (location.hash) history.replaceState(null, "", `${location.pathname}${location.search}`);
  document.title = "KaiAI · Panel";
  estadoEn("#entrada-estado", mensaje);
  $("#clave").focus();
}

function ocultarClave() {
  $("#clave").type = "password";
  $("#mostrar-clave").setAttribute("aria-pressed", "false");
  $("#mostrar-clave").textContent = "Mostrar";
}

function alternarClave() {
  const input = $("#clave");
  const visible = input.type === "password";
  input.type = visible ? "text" : "password";
  $("#mostrar-clave").setAttribute("aria-pressed", String(visible));
  $("#mostrar-clave").textContent = visible ? "Ocultar" : "Mostrar";
}

// ---------------------------------------------------------------------------
// Resumen
// ---------------------------------------------------------------------------

function pintarResumen(datos) {
  $("#tarjetas").replaceChildren(...tarjetasResumen(datos).map((t) => v.tarjetaResumen(t)));
  $("#periodo-texto").textContent = textoPeriodo(estado.ventana);
  const nota = notaResumen(datos);
  $("#resumen-nota").textContent = nota;
  $("#resumen-nota").hidden = !nota;
}

async function cargarResumen() {
  const sesion = estado.sesion;
  const carga = ++estado.cargaResumen;
  $("#tarjetas").setAttribute("aria-busy", "true");
  estadoEn("#resumen-estado", "Cargando el resumen…", "espera");
  const r = await pedirApi("GET", `/v1/resumen${queryResumen(estado.ventana)}`);
  // Si cambiaron el período mientras tanto, esta respuesta es de otro.
  if (sesion !== estado.sesion || carga !== estado.cargaResumen) return;
  $("#tarjetas").removeAttribute("aria-busy");
  if (r.status === 401) {
    salir(CLAVE_CAIDA);
    return;
  }
  if (r.status !== 200 || !r.datos) {
    estadoEn("#resumen-estado", `No se pudo cargar el resumen: ${textoDeFalla(r)}`, "crit");
    return;
  }
  estadoEn("#resumen-estado", "");
  pintarResumen(r.datos);
}

function cambiarPeriodo(evento) {
  const dias = Number(evento.target.value);
  if (!PERIODOS.includes(dias) || dias === estado.periodo) return;
  estado.periodo = dias;
  estado.ventana = ventanaPeriodo(dias);
  cargarResumen();
  cargarLista({ reiniciar: true });
}

// ---------------------------------------------------------------------------
// Lista de compras
// ---------------------------------------------------------------------------

function llenarFiltros() {
  $("#f-resultado").replaceChildren(
    v.el("option", { texto: "Todos", atributos: { value: "" } }),
    ...Object.entries(RESULTADOS).map(([id, info]) => v.el("option", { texto: info.texto, atributos: { value: id } })),
  );
  $("#f-alerta").replaceChildren(
    v.el("option", { texto: "Todas", atributos: { value: "" } }),
    ...Object.entries(ALERTAS).map(([id, info]) => v.el("option", { texto: info.texto, atributos: { value: id } })),
  );
}

function hayFiltros() {
  const f = estado.filtros;
  return Boolean(f.resultado || f.alerta || f.referencia);
}

async function cargarLista({ reiniciar = false } = {}) {
  if (!estado.clave) return;
  const sesion = estado.sesion;
  const carga = ++estado.cargaLista;
  estado.ultimaReiniciaba = reiniciar;
  if (reiniciar) {
    estado.compras = [];
    estado.siguiente = null;
    $("#filas").replaceChildren();
    $("#tabla").hidden = true;
  }
  $("#cargar-mas").disabled = true;
  $("#lista-error").hidden = true;
  $("#lista-vacia").hidden = true;
  $("#tabla").setAttribute("aria-busy", "true");
  estadoEn("#lista-estado", reiniciar ? "Cargando las compras…" : "Cargando más compras…", "espera");

  const plan = planResultado(estado.filtros.resultado);
  let cursor = reiniciar ? null : estado.siguiente;
  let siguiente = null;
  let nuevas = [];
  let paginas = 0;
  let falla = null;
  do {
    const r = await pedirApi("GET", `/v1/compras${queryListado({
      ...estado.ventana, referencia: estado.filtros.referencia, alerta: estado.filtros.alerta,
      estado: plan.estado, cursor,
    })}`);
    if (sesion !== estado.sesion || carga !== estado.cargaLista) return;
    if (r.status === 401) {
      salir(CLAVE_CAIDA);
      return;
    }
    if (r.status !== 200 || !Array.isArray(r.datos?.compras)) {
      falla = textoDeFalla(r);
      break;
    }
    paginas += 1;
    nuevas = combinarCompras(nuevas, filtrarPorResultado(r.datos.compras, plan.local));
    siguiente = siguienteValido(r.datos.siguiente);
    cursor = siguiente;
  } while (seguirCargando({ encontradas: nuevas.length, paginas, siguiente, local: plan.local }));

  $("#tabla").removeAttribute("aria-busy");
  $("#cargar-mas").disabled = false;
  if (falla) {
    estadoEn("#lista-estado", "");
    mostrarError("#lista-error", `No se pudieron cargar las compras: ${falla}`);
    $("#cargar-mas").hidden = !estado.siguiente;
    return;
  }
  const antes = estado.compras.length;
  estado.compras = combinarCompras(estado.compras, nuevas);
  estado.siguiente = siguiente;
  const filas = estado.compras.slice(antes).map((c) => v.filaCompra(c)).filter(Boolean);
  $("#filas").append(...filas);
  pintarPieLista(filas.length, paginas, plan.local);
}

function pintarPieLista(agregadas, paginas, local) {
  const total = estado.compras.length;
  const hayMas = Boolean(estado.siguiente);
  $("#tabla").hidden = total === 0;
  $("#cargar-mas").hidden = !hayMas;
  const vacia = $("#lista-vacia");
  if (total === 0) {
    vacia.textContent = hayMas
      ? `Revisamos ${paginas} ${paginas === 1 ? "página" : "páginas"} y no hay compras con ese resultado. Tocá «Cargar más» para seguir buscando.`
      : hayFiltros() ? "No hay compras con esos filtros en este período." : "Todavía no hay compras en este período.";
    vacia.hidden = false;
  }
  const cuantas = `${total} ${total === 1 ? "compra" : "compras"}`;
  const texto = total === 0 ? vacia.textContent
    : agregadas === 0 && local && hayMas ? `Mostramos ${cuantas}: en estas páginas no había más con ese resultado.`
    : `Mostramos ${cuantas}${hayMas ? " (hay más)" : ""}.`;
  estadoEn("#lista-estado", texto);
}

function aplicarFiltros(evento) {
  evento.preventDefault();
  const referencia = referenciaValida($("#f-referencia").value);
  if (referencia === null) {
    $("#f-referencia").setAttribute("aria-invalid", "true");
    estadoEn("#lista-estado", "La referencia es de hasta 200 caracteres, sin saltos de línea.", "crit");
    $("#f-referencia").focus();
    return;
  }
  $("#f-referencia").removeAttribute("aria-invalid");
  estado.filtros = { resultado: $("#f-resultado").value, alerta: $("#f-alerta").value, referencia };
  const plan = planResultado(estado.filtros.resultado);
  $("#filtro-nota").hidden = !(plan.local && !plan.estado);
  cargarLista({ reiniciar: true });
}

/** Los filtros y el período como al entrar (sin cargar nada). */
function reiniciarFiltros() {
  $("#f-resultado").value = "";
  $("#f-alerta").value = "";
  $("#f-referencia").value = "";
  $("#f-referencia").removeAttribute("aria-invalid");
  $("#filtro-nota").hidden = true;
  estado.filtros = { resultado: "", alerta: "", referencia: "" };
  estado.periodo = PERIODO_POR_DEFECTO;
  $(`#periodo-${PERIODO_POR_DEFECTO}`).checked = true;
}

function limpiarFiltros() {
  const periodo = estado.periodo;
  reiniciarFiltros();
  // "Limpiar" saca los filtros, no cambia el período elegido.
  estado.periodo = periodo;
  $(`#periodo-${periodo}`).checked = true;
  cargarLista({ reiniciar: true });
}

/** Un clic en cualquier parte de la fila abre la compra (el enlace navega solo). */
function clicEnFila(evento) {
  if (evento.target.closest("a")) return;
  const hash = hashDeCompra(evento.target.closest("tr")?.getAttribute("data-compra-id"));
  if (hash) location.hash = hash;
}

// ---------------------------------------------------------------------------
// Rutas (#/ y #/compra/<id>)
// ---------------------------------------------------------------------------

function rutear() {
  if (!estado.clave) return;
  const anterior = estado.vista;
  const ruta = rutaDe(location.hash);
  if (ruta.vista === "ancla") return;
  if (ruta.vista === "compra") {
    estado.desdeLaLista = anterior === "lista";
    estado.vista = "compra";
    abrirCompra(ruta.id);
    return;
  }
  if (ruta.vista === "compra_invalida") {
    estado.desdeLaLista = anterior === "lista";
    estado.vista = "compra";
    compraInvalida();
    return;
  }
  estado.vista = "lista";
  mostrarLista(anterior === "compra");
}

function mostrarLista(volviendo) {
  estado.cargaDetalle += 1;
  $("#vista-compra").removeAttribute("aria-busy");
  $("#vista-compra").hidden = true;
  $("#vista-lista").hidden = false;
  document.title = "KaiAI · Panel";
  if (!volviendo) return;
  const fila = estado.abierta ? $(`tr[data-compra-id="${estado.abierta}"] a`) : null;
  (fila || $("#t-compras")).focus();
}

function volver() {
  if (estado.desdeLaLista) history.back();
  else location.hash = "#/";
}

function prepararDetalle() {
  $("#vista-compra").removeAttribute("aria-busy");
  $("#vista-lista").hidden = true;
  $("#vista-compra").hidden = false;
  $("#detalle").hidden = true;
  $("#detalle-error").hidden = true;
  estadoEn("#verificacion", "");
  window.scrollTo(0, 0);
}

function compraInvalida() {
  estado.cargaDetalle += 1;
  estado.detalle = null;
  prepararDetalle();
  estadoEn("#detalle-estado", "");
  mostrarError("#detalle-error", "Ese enlace no es de una compra: revisá la dirección.");
  $("#reintentar-detalle").hidden = true;
  $("#volver").focus();
}

async function abrirCompra(id) {
  if (!esIdCompra(id)) {
    compraInvalida();
    return;
  }
  const sesion = estado.sesion;
  const carga = ++estado.cargaDetalle;
  estado.abierta = id;
  estado.detalle = null;
  prepararDetalle();
  $("#reintentar-detalle").hidden = false;
  $("#vista-compra").setAttribute("aria-busy", "true");
  estadoEn("#detalle-estado", "Cargando la compra…", "espera");
  $("#volver").focus();
  const [explicacion, compra] = await Promise.all([
    pedirApi("GET", `/v1/compras/${id}/explicacion`),
    pedirApi("GET", `/v1/compras/${id}`),
  ]);
  if (sesion !== estado.sesion || carga !== estado.cargaDetalle) return;
  $("#vista-compra").removeAttribute("aria-busy");
  if (explicacion.status === 401 || compra.status === 401) {
    salir(CLAVE_CAIDA);
    return;
  }
  if (explicacion.status !== 200 || !explicacion.datos || typeof explicacion.datos !== "object"
      || explicacion.datos.compra_id !== id) {
    estadoEn("#detalle-estado", "");
    const noEsta = explicacion.status === 404;
    $("#reintentar-detalle").hidden = noEsta;
    mostrarError("#detalle-error", noEsta
      ? "No encontramos esa compra: puede ser de otra empresa o ya no estar guardada (guardamos 90 días)."
      : explicacion.status === 200
        ? "La respuesta no es de esta compra: no la mostramos."
        : `No se pudo cargar la compra: ${textoDeFalla(explicacion)}`);
    return;
  }
  estado.detalle = {
    id,
    explicacion: explicacion.datos,
    compra: compra.status === 200 && compra.datos?.compra_id === id ? compra.datos : null,
  };
  pintarDetalle(estado.detalle);
  estadoEn("#detalle-estado", "");
  $("#d-titulo").focus();
}

function pintarDetalle({ explicacion: e, compra }) {
  const info = infoResultado(e.resultado);
  document.title = `KaiAI · ${info.texto}`;
  $("#d-resultado").replaceChildren(v.chipResultado(e.resultado, { clase: "chip chip-grande", prefijo: "Resultado: " }));
  const resumen = $("#d-resumen");
  resumen.textContent = typeof e.resumen === "string" && e.resumen ? e.resumen : info.descripcion;
  resumen.setAttribute("data-tono", info.tono);

  const { estafa, avisos } = separarAlertas(e.alertas);
  const tipos = estafa.length || e.intento_de_estafa !== true
    ? estafa : [{ texto: "Detectamos un intento de estafa." }];
  $("#d-estafa").replaceChildren(v.bloqueEstafa(tipos));
  $("#d-avisos").replaceChildren(...v.avisos(avisos));
  $("#d-avisos").hidden = avisos.length === 0;
  $("#d-datos").replaceChildren(...v.datos(datosDeCompra(compra, e)));

  const linea = lineaDeTiempo(e.linea_de_tiempo);
  $("#d-linea").replaceChildren(...linea.map((paso) => v.itemLinea(paso)));
  $("#d-linea-vacia").hidden = linea.length > 0;
  const chequeos = chequeosDe(e.chequeos);
  $("#d-chequeos").replaceChildren(...chequeos.map((c) => v.itemChequeo(c)));
  $("#d-chequeos-vacio").hidden = chequeos.length > 0;
  pintarRecibo(e.recibo);
  $("#detalle").hidden = false;
}

function pintarRecibo(recibo) {
  const r = recibo && typeof recibo === "object" && typeof recibo.id === "string" ? recibo : null;
  estadoEn("#verificacion", "");
  $("#d-sin-recibo").hidden = Boolean(r);
  $("#verificar").hidden = !r?.firmado;
  $("#verificar").disabled = false;
  $("#d-recibo").replaceChildren(...(r ? v.datos([
    { nombre: "Recibo", valor: r.id, mono: true },
    { nombre: "Clave de firma (kid)", valor: typeof r.kid === "string" && r.kid ? r.kid : "—", mono: true },
    { nombre: "Firmado", valor: r.firmado ? "Sí" : "No: este recibo no tiene firma" },
  ]) : []));
}

// ---------------------------------------------------------------------------
// El recibo: la firma se verifica en el navegador (recibo.mjs, WebCrypto)
// ---------------------------------------------------------------------------

async function leerCompra(id) {
  if (!esIdCompra(id)) return null;
  const r = await pedirApi("GET", `/v1/compras/${id}`);
  if (r.status === 401) {
    salir(CLAVE_CAIDA);
    return null;
  }
  return r.status === 200 && r.datos?.compra_id === id ? r.datos : null;
}

async function clavePublica() {
  if (estado.publica) return estado.publica;
  const r = await pedirApi("GET", "/v1/public-key", { conClave: false });
  if (r.status === 200 && r.datos && typeof r.datos === "object") estado.publica = r.datos;
  return estado.publica;
}

async function verificarFirma() {
  const detalle = estado.detalle;
  if (!detalle) return;
  const sesion = estado.sesion;
  const listo = () => sesion === estado.sesion && detalle === estado.detalle;
  $("#verificar").disabled = true;
  estadoEn("#verificacion", "Verificando la firma en tu navegador…", "espera");
  if (!detalle.compra) detalle.compra = await leerCompra(detalle.id);
  if (!listo()) return;
  const recibo = reciboParaVerificar(detalle.compra, detalle.explicacion.recibo);
  if (!recibo) {
    estadoEn("#verificacion", "No encontramos el recibo firmado de esta compra: no se pudo verificar.", "crit");
    $("#verificar").disabled = false;
    return;
  }
  const guardada = Boolean(estado.publica);
  let publica = await clavePublica();
  if (!listo()) return;
  if (!publica) {
    estadoEn("#verificacion", "No se pudo bajar la clave pública de KaiAI: la firma no se verificó. Probá de nuevo.", "crit");
    $("#verificar").disabled = false;
    return;
  }
  let resultado = await verificarRecibo(recibo, publica);
  if (!resultado.ok && guardada) {
    estado.publica = null;
    publica = await clavePublica();
    if (!listo()) return;
    if (publica) resultado = await verificarRecibo(recibo, publica);
  }
  if (!listo()) return;
  const { texto, tono } = textoVerificacion(resultado);
  estadoEn("#verificacion", texto, tono);
  $("#verificar").disabled = false;
}

// ---------------------------------------------------------------------------
// Tema
// ---------------------------------------------------------------------------

function aplicarTema(tema) {
  if (tema === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", tema);
  const boton = $("#tema");
  boton.setAttribute("data-tema", tema);
  boton.setAttribute("aria-label", NOMBRE_TEMA[tema]);
  boton.title = NOMBRE_TEMA[tema];
}

function cambiarTema() {
  const tema = siguienteTema($("#tema").getAttribute("data-tema") || "auto");
  aplicarTema(tema);
  try {
    sessionStorage.setItem(TEMA_SESION, tema);
  } catch {
    // Sin sessionStorage, el tema dura lo que la página.
  }
}

function temaGuardado() {
  try {
    const tema = sessionStorage.getItem(TEMA_SESION);
    return TEMAS.includes(tema) ? tema : "auto";
  } catch {
    return "auto";
  }
}

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

/** "Saltar al contenido" lleva el foco sin tocar el hash (que es la ruta). */
function saltarAlContenido(evento) {
  evento.preventDefault();
  $("#principal").focus();
}

function arrancar() {
  aplicarTema(temaGuardado());
  $(".saltar").addEventListener("click", saltarAlContenido);
  llenarFiltros();
  $("#form-entrada").addEventListener("submit", entrar);
  $("#mostrar-clave").addEventListener("click", alternarClave);
  $("#salir").addEventListener("click", () => salir());
  $("#tema").addEventListener("click", cambiarTema);
  for (const radio of document.querySelectorAll('input[name="periodo"]')) radio.addEventListener("change", cambiarPeriodo);
  $("#filtros").addEventListener("submit", aplicarFiltros);
  $("#limpiar-filtros").addEventListener("click", limpiarFiltros);
  $("#filas").addEventListener("click", clicEnFila);
  $("#cargar-mas").addEventListener("click", () => cargarLista());
  $("#reintentar-lista").addEventListener("click", () => cargarLista({ reiniciar: estado.ultimaReiniciaba }));
  $("#volver").addEventListener("click", volver);
  $("#reintentar-detalle").addEventListener("click", rutear);
  $("#verificar").addEventListener("click", verificarFirma);
  window.addEventListener("hashchange", rutear);
  const guardada = leerSesion();
  if (claveValida(guardada)) {
    $("#clave").value = guardada;
    $("#recordar").checked = true;
    entrar();
  }
}

arrancar();
