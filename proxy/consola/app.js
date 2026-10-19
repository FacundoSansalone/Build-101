// Consola web de KaiAI: la vista. Toda la lógica está en logica.mjs; acá solo
// se lee el formulario, se llama a la API y se pinta lo que vuelve.
//
// Reglas de esta vista:
// - Todo texto que viene del motor o del comercio se pone con textContent:
//   el del comercio puede traer una inyección.
// - La clave vive en memoria (y en sessionStorage solo si se marca
//   "recordar en esta pestaña"). Solo viaja a este mismo origen y nunca se
//   escribe en el registro.
// - La consola no decide nada: cada botón es una llamada a la API.

import {
  EJEMPLO_INYECCION, ESCENARIOS, ESCENARIO_POR_DEFECTO,
  agruparChequeos, cartAutorizado, cartSugerido, claveValida, cuerpoAprobacion, cuerpoCompra,
  cuerpoDecision, cuerpoFiltrar, cuerpoOmitir, cuerpoRespuesta, describirRequisito, describirValor,
  detalleDe, esIdCompra, estadoFirma, formatearMonto, formatearRestante, headersA2a, hora,
  infoStatus, leerJson, leerTarea, mensajeErrorApi, nombreCampo, pedidoBusqueda, pedidoCheckout,
  proximoLunes, respuestaSugerida, rutaA2a, segundosRestantes, topeDe, verificarRecibo,
} from "./logica.mjs";

const CLAVE_SESION = "kaiai.consola.clave";
const SONDEO_MS = 1500;
const TIEMPO_API_MS = 30_000;
const TIEMPO_A2A_MS = 320_000; // Vercel corta a los 300 s
const MAX_REGISTRO = 300;

const $ = (id) => document.getElementById(id);

const estado = {
  clave: null,
  compra: null,
  rutaA2a: null,
  escenario: null,
  contexto: null,
  lectura: null,
  cart: null,
  buscando: false,
  sondeando: false,
  sondeo: null,
  pendiente: null,
  contestadas: new Set(),
  abortarBusqueda: null,
  publica: null,
  verificados: new Set(),
  ultimoEstado: null,
  generacion: 0,
};

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

function el(tag, { clase, texto, atributos } = {}, hijos = []) {
  const nodo = document.createElement(tag);
  if (clase) nodo.className = clase;
  if (texto !== undefined && texto !== null) nodo.textContent = String(texto);
  for (const [nombre, valor] of Object.entries(atributos || {})) nodo.setAttribute(nombre, valor);
  for (const hijo of hijos) if (hijo) nodo.append(hijo);
  return nodo;
}

/** Lleva el foco a `id` (un título) y muestra entero el bloque `bloque`. */
function enfocar(id, bloque) {
  $(id).focus({ preventScroll: true });
  $(bloque || id).scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function vaciar(nodo) {
  nodo.replaceChildren();
}

function mostrar(nodo, visible = true) {
  nodo.hidden = !visible;
}

function estadoEn(id, texto, clase = "") {
  const nodo = $(id);
  nodo.textContent = texto || "";
  nodo.className = `estado${clase ? ` ${clase}` : ""}`;
}

function registrar(texto, clase = "") {
  const lista = $("registro");
  const ahora = new Date();
  const item = el("li", { clase }, [
    el("time", { texto: hora(ahora), atributos: { datetime: ahora.toISOString() } }),
    el("span", { texto }),
  ]);
  lista.append(item);
  while (lista.children.length > MAX_REGISTRO) lista.firstElementChild.remove();
  lista.scrollTop = lista.scrollHeight;
}

// ---------------------------------------------------------------------------
// HTTP (siempre a este mismo origen)
// ---------------------------------------------------------------------------

function conTiempo(ms, externo) {
  const control = new AbortController();
  const reloj = setTimeout(() => control.abort(), ms);
  if (externo) externo.addEventListener("abort", () => control.abort(), { once: true });
  return { senal: control.signal, listo: () => clearTimeout(reloj) };
}

/** Un pedido a /v1. `{status, datos, error}`; nunca lanza. */
async function pedirApi(metodo, ruta, cuerpo, { silencioso = false, conClave = true } = {}) {
  if (!ruta.startsWith("/v1/")) throw new Error("ruta fuera de /v1");
  const headers = { Accept: "application/json" };
  if (conClave && estado.clave) headers.Authorization = `Bearer ${estado.clave}`;
  if (cuerpo !== undefined) headers["Content-Type"] = "application/json";
  const tiempo = conTiempo(TIEMPO_API_MS);
  let respuesta;
  try {
    respuesta = await fetch(ruta, {
      method: metodo, headers, body: cuerpo === undefined ? undefined : JSON.stringify(cuerpo),
      credentials: "omit", cache: "no-store", redirect: "error", signal: tiempo.senal,
    });
  } catch (e) {
    tiempo.listo();
    const error = e?.name === "AbortError" ? "el motor no respondió a tiempo" : "no se pudo hablar con el motor";
    registrar(`${metodo} ${ruta} → ${error}`, "mal");
    return { status: 0, datos: null, error };
  }
  const texto = await respuesta.text().catch(() => "");
  tiempo.listo();
  const leido = leerJson(texto);
  const datos = leido.ok ? leido.datos : null;
  const error = respuesta.ok ? null : mensajeErrorApi(respuesta.status, datos);
  if (!silencioso || !respuesta.ok) {
    registrar(`${metodo} ${ruta} → ${respuesta.status}`, respuesta.ok ? "ok" : "mal");
  }
  return { status: respuesta.status, datos, error };
}

/** Un pedido A2A de la compra. `{status, lectura, error}`; nunca lanza. */
async function pedirA2a(pedido, nombre, externo) {
  const compraId = estado.compra?.compra_id;
  if (!estado.rutaA2a || !esIdCompra(compraId) || !estado.clave) {
    return { status: 0, lectura: null, error: "falta la compra o la clave" };
  }
  const headers = { ...headersA2a(compraId), Accept: "application/json" };
  headers.Authorization = `Bearer ${estado.clave}`;
  const tiempo = conTiempo(TIEMPO_A2A_MS, externo);
  registrar(`POST ${estado.rutaA2a} (${nombre})…`);
  let respuesta;
  try {
    respuesta = await fetch(estado.rutaA2a, {
      method: "POST", headers, body: JSON.stringify(pedido),
      credentials: "omit", cache: "no-store", redirect: "error", signal: tiempo.senal,
    });
  } catch (e) {
    tiempo.listo();
    const error = e?.name === "AbortError"
      ? (externo?.aborted ? "cancelado" : "el pedido tardó demasiado")
      : "no se pudo hablar con el motor";
    registrar(`${nombre} → ${error}`, "mal");
    return { status: 0, lectura: null, error };
  }
  // Con latido, el cuerpo empieza con espacios: se lee entero y después se parsea.
  const texto = await respuesta.text().catch(() => "");
  tiempo.listo();
  const leido = leerJson(texto);
  const lectura = leido.ok ? leerTarea(leido.datos) : null;
  let error = null;
  if (!leido.ok) {
    error = respuesta.status === 504
      ? "HTTP 504: el pedido pasó el tiempo máximo de la función (300 s en Vercel)"
      : `HTTP ${respuesta.status}: ${leido.error}`;
  } else if (lectura.tipo === "ilegible") {
    error = mensajeErrorApi(respuesta.status, leido.datos);
  }
  const resumen = lectura?.tipo === "tarea"
    ? `${lectura.estado || "sin estado"}`
    : lectura?.tipo === "error" ? `error ${lectura.error.codigo || ""}`.trim() : "ilegible";
  registrar(`${nombre} → ${respuesta.status} (${resumen})`, lectura?.tipo === "tarea" ? "ok" : "mal");
  return { status: respuesta.status, lectura, error };
}

// ---------------------------------------------------------------------------
// Clave
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

async function usarClave(evento) {
  evento?.preventDefault();
  const valor = $("clave").value.trim();
  if (!claveValida(valor)) {
    $("clave").setAttribute("aria-invalid", "true");
    estadoEn("clave-estado", "La clave tiene que tener la forma psg_… (la que te dio el operador).", "mal");
    return;
  }
  $("clave").removeAttribute("aria-invalid");
  estado.clave = valor;
  guardarSesion($("recordar").checked ? valor : null);
  await cargarComercios();
}

function olvidarClave() {
  estado.clave = null;
  $("clave").value = "";
  $("recordar").checked = false;
  guardarSesion(null);
  llenarComercios([]);
  estadoEn("clave-estado", "Clave olvidada.");
  registrar("Clave olvidada");
}

function alternarClave() {
  const input = $("clave");
  const visible = input.type === "password";
  input.type = visible ? "text" : "password";
  $("mostrar-clave").setAttribute("aria-pressed", String(visible));
  $("mostrar-clave").textContent = visible ? "Ocultar" : "Mostrar";
}

function llenarComercios(comercios) {
  const select = $("comercio");
  vaciar(select);
  if (!comercios.length) {
    select.append(el("option", { texto: "Cargá la clave primero", atributos: { value: "" } }));
    return;
  }
  for (const c of comercios) {
    const nombre = typeof c.nombre === "string" && c.nombre && c.nombre !== c.id ? `${c.id} (${c.nombre})` : c.id;
    select.append(el("option", { texto: nombre, atributos: { value: c.id } }));
  }
}

async function cargarComercios() {
  estadoEn("clave-estado", "Probando la clave…", "espera");
  const r = await pedirApi("GET", "/v1/comercios");
  if (r.status === 200 && Array.isArray(r.datos?.comercios)) {
    const comercios = r.datos.comercios.filter((c) => c && typeof c.id === "string");
    llenarComercios(comercios);
    estadoEn(
      "clave-estado",
      comercios.length
        ? `Clave válida: ${comercios.length} ${comercios.length === 1 ? "comercio habilitado" : "comercios habilitados"}.`
        : "La clave vale, pero no tiene comercios habilitados.",
      comercios.length ? "ok" : "mal",
    );
    return;
  }
  llenarComercios([]);
  estadoEn("clave-estado", r.status === 401 ? `La clave no es válida: ${r.error}` : `No se pudo cargar: ${r.error}`, "mal");
}

// ---------------------------------------------------------------------------
// Compra
// ---------------------------------------------------------------------------

const CAMPOS_FORM = ["pedido", "comercio", "tope", "moneda", "cantidad", "plazo", "categoria"];

function marcarErrores(errores) {
  for (const id of CAMPOS_FORM) $(id).removeAttribute("aria-invalid");
  const caja = $("compra-errores");
  vaciar(caja);
  if (!errores.length) return;
  const lista = el("ul");
  for (const e of errores) {
    if (CAMPOS_FORM.includes(e.campo)) $(e.campo).setAttribute("aria-invalid", "true");
    lista.append(el("li", { texto: e.mensaje }));
  }
  caja.append(lista);
}

function reiniciarCompra() {
  estado.generacion += 1;
  estado.abortarBusqueda?.abort();
  detenerSondeo();
  Object.assign(estado, {
    compra: null, rutaA2a: null, escenario: null, contexto: null, lectura: null, cart: null,
    buscando: false, pendiente: null, abortarBusqueda: null, ultimoEstado: null,
  });
  estado.contestadas.clear();
  for (const id of ["compra-vista", "sec-busqueda", "sec-checkout", "sec-compra", "pend-porque",
    "pend-oportunidad", "resultado-busqueda", "veredicto", "escalar"]) mostrar($(id), false);
  estadoEn("busqueda-estado", "");
  estadoEn("checkout-estado", "");
  $("crear").disabled = false;
}

async function crearCompra(evento) {
  evento.preventDefault();
  if (!estado.clave) {
    marcarErrores([{ campo: "", mensaje: "Primero cargá tu clave de integrador (paso 1)." }]);
    $("clave").focus();
    return;
  }
  const { cuerpo, errores } = cuerpoCompra({
    pedido: $("pedido").value,
    comercio: $("comercio").value,
    tope: $("tope").value,
    moneda: $("moneda").value,
    cantidad: $("cantidad").value,
    plazo: $("plazo").value,
    categoria: $("categoria").value,
    preguntar: $("preguntar").checked,
  });
  marcarErrores(errores);
  if (!cuerpo) return;
  reiniciarCompra();
  $("crear").disabled = true;
  const r = await pedirApi("POST", "/v1/compras", cuerpo);
  $("crear").disabled = false;
  if (r.status !== 201 || !r.datos) {
    marcarErrores([{ campo: "", mensaje: `No se pudo crear la compra: ${r.error}` }]);
    return;
  }
  const ruta = rutaA2a(r.datos.a2a_url, location.origin);
  if (!esIdCompra(r.datos.compra_id) || !ruta) {
    marcarErrores([{ campo: "", mensaje: "La compra no trajo una URL A2A de este sitio: no sigo." }]);
    return;
  }
  estado.compra = r.datos;
  estado.rutaA2a = ruta;
  estado.ultimoEstado = r.datos.estado;
  estado.escenario = $("escenario").value || null;
  registrar(`Compra ${r.datos.compra_id} creada (${r.datos.estado})`, "ok");
  pintarCompra(r.datos);
  mostrar($("sec-busqueda"));
  estadoEn("busqueda-estado", "Lista para buscar.");
  $("buscar").disabled = false;
  $("buscar").focus();
}

function pintarCompra(vista) {
  mostrar($("compra-vista"));
  $("c-id").textContent = vista.compra_id || "—";
  $("c-estado").textContent = vista.estado || "—";
  $("c-comercio").textContent = vista.comercio || "—";
  $("c-intencion").textContent = vista.intencion_activa
    ? "Activa: puede preguntarte el porqué y proponerte alternativas"
    : "Apagada: sigue el pedido literal";
  const lista = $("c-requisitos");
  vaciar(lista);
  for (const r of vista.ficha?.requisitos || []) lista.append(el("li", { texto: describirRequisito(r) }));
  const categoria = vista.ficha?.categoria_taxonomia;
  $("c-categoria").textContent = categoria ? `Categoría de la taxonomía de Google: ${categoria}` : "";
}

function actualizarCompra(vista) {
  if (!vista || vista.compra_id !== estado.compra?.compra_id) return;
  // Con varias instancias, un sondeo que salió antes puede volver después de
  // una respuesta más nueva: se descarta la vista más vieja.
  const nueva = Date.parse(vista.actualizada);
  const actual = Date.parse(estado.compra.actualizada);
  if (Number.isFinite(nueva) && Number.isFinite(actual) && nueva < actual) return;
  estado.compra = vista;
  if (vista.estado !== estado.ultimoEstado) {
    if (estado.ultimoEstado !== null) registrar(`La compra pasó a ${vista.estado}`);
    estado.ultimoEstado = vista.estado;
  }
  pintarCompra(vista);
  if (estado.buscando) pintarPendiente(vista.pendiente);
}

// ---------------------------------------------------------------------------
// Búsqueda y pausas
// ---------------------------------------------------------------------------

function iniciarSondeo() {
  detenerSondeo();
  estado.sondeo = setInterval(() => {
    sondear();
    pintarRestante();
  }, SONDEO_MS);
  sondear();
}

function detenerSondeo() {
  if (estado.sondeo) clearInterval(estado.sondeo);
  estado.sondeo = null;
}

async function sondear() {
  if (estado.sondeando || !estado.compra) return;
  estado.sondeando = true;
  const generacion = estado.generacion;
  try {
    const r = await pedirApi("GET", `/v1/compras/${estado.compra.compra_id}`, undefined, { silencioso: true });
    if (generacion === estado.generacion && r.status === 200) actualizarCompra(r.datos);
  } finally {
    estado.sondeando = false;
  }
}

function clavePendiente(p) {
  if (!p) return null;
  if (p.tipo === "oportunidad") return `oportunidad:${p.oportunidad?.id}`;
  return `${p.tipo}:${p.vence}`;
}

function pintarPendiente(recibido) {
  // Una pausa ya contestada no se vuelve a mostrar (un sondeo viejo puede
  // llegar después de la respuesta).
  const pendiente = estado.contestadas.has(clavePendiente(recibido)) ? null : recibido;
  const tipo = pendiente?.tipo;
  const nueva = clavePendiente(pendiente) !== clavePendiente(estado.pendiente);
  estado.pendiente = tipo === "porque" || tipo === "oportunidad" ? pendiente : null;
  mostrar($("pend-porque"), tipo === "porque");
  mostrar($("pend-oportunidad"), tipo === "oportunidad");
  if (tipo === "porque" && nueva) {
    $("porque-pregunta").textContent = pendiente.pregunta || "¿Para qué lo necesitás?";
    if (!$("porque-respuesta").value.trim()) {
      const cantidad = (estado.compra?.ficha?.requisitos || []).find((r) => r?.campo === "cantidad")?.valor;
      $("porque-respuesta").value = respuestaSugerida(cantidad);
    }
    $("responder").disabled = false;
    $("omitir").disabled = false;
    estadoEn("busqueda-estado", "KaiAI retiene el pedido y espera tu respuesta.", "espera");
    registrar("KaiAI pregunta el porqué");
    enfocar("pend-porque-titulo", "pend-porque");
  }
  if (tipo === "oportunidad" && nueva) {
    pintarOportunidad(pendiente.oportunidad || {});
    $("aceptar").disabled = false;
    $("rechazar").disabled = false;
    estadoEn("busqueda-estado", "KaiAI retiene la respuesta del comercio y espera tu decisión.", "espera");
    registrar(`KaiAI propone una oportunidad (${pendiente.oportunidad?.id || "sin id"})`);
    enfocar("pend-op-titulo", "pend-oportunidad");
  }
  pintarRestante();
}

function pintarOportunidad(op) {
  const titulo = op.titulo || (op.sku ? `SKU ${op.sku}` : op.cart_id || "Oferta");
  $("op-titulo").textContent = `${titulo}${op.cart_id ? ` · ${op.cart_id}` : ""}`;
  $("op-precio").textContent = typeof op.precio === "number" ? `Precio: ${formatearMonto(op.precio, op.moneda)}` : "";
  const cuerpo = $("op-cambios");
  vaciar(cuerpo);
  for (const c of op.cambios || []) {
    cuerpo.append(el("tr", {}, [
      el("th", { texto: nombreCampo(c.campo), atributos: { scope: "row" } }),
      el("td", { texto: describirValor(c.pedido) }),
      el("td", { texto: c.omitido ? `${describirValor(c.ofrecido)} (texto omitido)` : describirValor(c.ofrecido) }),
    ]));
  }
  $("op-porque").textContent = op.por_que || "";
}

function pintarRestante() {
  const p = estado.pendiente;
  const texto = formatearRestante(segundosRestantes(p?.vence));
  if (p?.tipo === "porque") $("porque-restante").textContent = texto;
  if (p?.tipo === "oportunidad") $("op-restante").textContent = texto;
}

async function responder(omitir) {
  const cuerpo = omitir ? cuerpoOmitir() : cuerpoRespuesta($("porque-respuesta").value);
  if (!cuerpo) {
    $("porque-respuesta").setAttribute("aria-invalid", "true");
    estadoEn("busqueda-estado", "Escribí una respuesta (hasta 1000 caracteres) u omití.", "mal");
    return;
  }
  $("porque-respuesta").removeAttribute("aria-invalid");
  $("responder").disabled = true;
  $("omitir").disabled = true;
  const r = await pedirApi("POST", `/v1/compras/${estado.compra.compra_id}/respuesta`, cuerpo);
  if (r.status === 200) {
    estado.contestadas.add(clavePendiente(estado.pendiente));
    mostrar($("pend-porque"), false);
    estadoEn("busqueda-estado", omitir ? "Omitido: sigue el pedido literal." : "Respuesta enviada. La búsqueda sigue…", "espera");
    actualizarCompra(r.datos);
    return;
  }
  $("responder").disabled = false;
  $("omitir").disabled = false;
  estadoEn("busqueda-estado", `No se pudo responder: ${r.error}`, "mal");
}

async function decidir(decision) {
  const op = estado.pendiente?.oportunidad;
  const cuerpo = cuerpoDecision(op?.id, decision);
  if (!cuerpo) {
    estadoEn("busqueda-estado", "La oportunidad no tiene un identificador válido: no se puede decidir.", "mal");
    return;
  }
  $("aceptar").disabled = true;
  $("rechazar").disabled = true;
  const r = await pedirApi("POST", `/v1/compras/${estado.compra.compra_id}/decision`, cuerpo);
  if (r.status === 200) {
    estado.contestadas.add(clavePendiente(estado.pendiente));
    mostrar($("pend-oportunidad"), false);
    estadoEn("busqueda-estado", decision === "aceptar"
      ? "Aceptaste: KaiAI le pasa los parámetros nuevos a tu agente."
      : "Rechazaste: siguen los parámetros que autorizaste.", "espera");
    actualizarCompra(r.datos);
    return;
  }
  $("aceptar").disabled = false;
  $("rechazar").disabled = false;
  estadoEn("busqueda-estado", `No se pudo decidir: ${r.error}`, "mal");
}

async function buscar() {
  if (!estado.compra || estado.buscando) return;
  const generacion = estado.generacion;
  estado.buscando = true;
  estado.lectura = null;
  estado.cart = null;
  estado.pendiente = null;
  $("buscar").disabled = true;
  $("porque-respuesta").value = "";
  for (const id of ["resultado-busqueda", "sec-checkout", "sec-compra", "veredicto"]) mostrar($(id), false);
  estadoEn("busqueda-estado", "Buscando a través de KaiAI… (puede quedar retenido mientras te pregunta algo)", "espera");
  const control = new AbortController();
  estado.abortarBusqueda = control;
  iniciarSondeo();
  const r = await pedirA2a(pedidoBusqueda(estado.compra.pedido, estado.escenario), "find_products", control.signal);
  if (generacion !== estado.generacion) return;
  detenerSondeo();
  estado.abortarBusqueda = null;
  estado.buscando = false;
  pintarPendiente(null);
  await sondear();
  pintarBusqueda(r);
}

function pintarBusqueda(r) {
  mostrar($("resultado-busqueda"));
  const mensaje = $("busqueda-mensaje");
  const parametros = $("parametros");
  mostrar(mensaje, false);
  mostrar(parametros, false);
  mostrar($("carts"), false);
  $("buscar").disabled = false;

  const lectura = r.lectura;
  if (!lectura || lectura.tipo !== "tarea") {
    const texto = lectura?.tipo === "error"
      ? `${lectura.error.mensaje}${lectura.error.codigo ? ` (${lectura.error.codigo})` : ""}`
      : `No llegó una tarea: ${r.error || "respuesta ilegible"}`;
    mensaje.textContent = texto;
    mostrar(mensaje);
    estadoEn("busqueda-estado", "La búsqueda no terminó bien.", "mal");
    return;
  }
  estado.lectura = lectura;
  estado.contexto = lectura.contexto;
  if (lectura.estado !== "completed") {
    mensaje.textContent = lectura.texto || `La tarea terminó ${lectura.estado || "sin estado"}.`;
    mostrar(mensaje);
    estadoEn("busqueda-estado", `La búsqueda terminó ${lectura.estado || "sin estado"}: a tu agente no le llegó nada del comercio.`, "mal");
    return;
  }
  const autorizado = cartAutorizado(lectura.parametros);
  if (lectura.parametros) {
    parametros.textContent = typeof lectura.parametros.texto === "string" && lectura.parametros.texto
      ? lectura.parametros.texto
      : "Llegaron parámetros autorizados nuevos para tu agente.";
    mostrar(parametros);
  }
  if (!lectura.carts.length) {
    mensaje.textContent = "El comercio no tiene nada que cumpla tu pedido.";
    mostrar(mensaje);
    estadoEn("busqueda-estado", "Búsqueda terminada, sin ofertas.");
    return;
  }
  const { tope, moneda } = topeDe(estado.compra.ficha);
  pintarCarts(lectura, autorizado || cartSugerido(lectura.carts, tope ?? Infinity, moneda), autorizado);
  estadoEn("busqueda-estado", `Búsqueda terminada: ${lectura.carts.length} ${lectura.carts.length === 1 ? "oferta" : "ofertas"}.`, "ok");
  mostrar($("sec-checkout"));
}

const CAMPOS_DETALLE = [
  ["sku", "SKU"], ["title", "Título"], ["brand", "Marca"], ["category", "Categoría"],
  ["quantity", "Cantidad"], ["deliveries", "Entregas"], ["stock", "Stock"], ["price", "Precio"],
  ["description", "Descripción"],
];

function textoDetalle(clave, valor) {
  if (clave === "deliveries" && Array.isArray(valor)) {
    return valor.map((e) => `${describirValor(e?.quantity)} el ${describirValor(e?.date)}`).join(", ");
  }
  return describirValor(valor);
}

function pintarCarts(lectura, elegido, autorizado) {
  const lista = $("lista-carts");
  vaciar(lista);
  for (const cart of lectura.carts) {
    const radio = el("input", { atributos: { type: "radio", name: "cart", value: cart.cart_id || "" } });
    radio.disabled = !cart.seguro;
    radio.checked = cart.seguro && cart.cart_id === elegido;
    radio.addEventListener("change", () => elegirCart(cart.cart_id));
    const meta = el("span", { clase: "cart-meta", texto: cart.seguro ? cart.cart_id : "cart_id con formato inválido: no se puede elegir" });
    if (cart.cart_id === autorizado) meta.append(el("span", { clase: "insignia", texto: "Autorizado por vos" }));
    const etiqueta = el("label", { clase: "cart" }, [
      radio,
      el("span", { clase: "cart-titulo", texto: cart.item_label || "(sin título)" }),
      el("span", { clase: "cart-monto", texto: formatearMonto(cart.amount, cart.currency) }),
      meta,
    ]);
    const item = el("li", { clase: cart.cart_id === autorizado ? "autorizado" : "" }, [etiqueta]);
    const detalle = detalleDe(lectura.detalles, cart.cart_id);
    if (detalle) {
      const datos = el("dl");
      for (const [clave, nombre] of CAMPOS_DETALLE) {
        if (detalle[clave] === undefined || detalle[clave] === null || detalle[clave] === "") continue;
        datos.append(el("dt", { texto: nombre }), el("dd", { texto: textoDetalle(clave, detalle[clave]) }));
      }
      item.append(el("details", { clase: "cart-detalle" }, [el("summary", { texto: "Lo que declara el comercio" }), datos]));
    }
    lista.append(item);
  }
  mostrar($("carts"));
  elegirCart(lectura.carts.some((c) => c.seguro && c.cart_id === elegido) ? elegido : null);
}

function elegirCart(cartId) {
  estado.cart = cartId;
  $("checkout").disabled = !cartId;
  estadoEn("checkout-estado", cartId ? `Cart elegido: ${cartId}.` : "Elegí un cart.");
}

// ---------------------------------------------------------------------------
// Checkout, veredicto y aprobación
// ---------------------------------------------------------------------------

async function pedirCheckout() {
  const cart = estado.cart;
  if (!cart || !estado.contexto) return;
  const generacion = estado.generacion;
  $("checkout").disabled = true;
  $("reintentar").disabled = true;
  mostrar($("veredicto"), false);
  estadoEn("checkout-estado", `Pidiendo el checkout de ${cart}… KaiAI lo verifica antes de dártelo.`, "espera");
  const r = await pedirA2a(pedidoCheckout(cart, estado.contexto), "create_checkout");
  if (generacion !== estado.generacion) return;
  $("checkout").disabled = false;
  $("reintentar").disabled = false;
  pintarVeredicto(r, cart);
  enfocar("v-status", "veredicto");
  await cargarCompra();
}

function pintarVeredicto(r, cart) {
  mostrar($("veredicto"));
  mostrar($("escalar"), false);
  const lectura = r.lectura;
  const pastilla = $("v-status");
  const firma = $("v-firma");
  vaciar($("v-chequeos"));
  if (!lectura || lectura.tipo !== "tarea") {
    pastilla.className = "pastilla mal";
    pastilla.textContent = "Sin veredicto";
    $("v-motivo").textContent = lectura?.tipo === "error"
      ? `${lectura.error.mensaje}${lectura.error.codigo ? ` (${lectura.error.codigo})` : ""}`
      : r.error || "La respuesta no se pudo leer.";
    firma.className = "firma mal";
    firma.textContent = "No llegó ningún checkout: no hay nada que firmar.";
    estadoEn("checkout-estado", "El checkout no se pudo evaluar.", "mal");
    return;
  }
  const veredicto = lectura.veredicto;
  const info = infoStatus(veredicto?.status);
  pastilla.className = `pastilla ${info.clase}`;
  pastilla.textContent = veredicto ? info.etiqueta : "Sin guard.veredicto";
  $("v-motivo").textContent = veredicto?.motivo || lectura.texto || "";
  const firmable = estadoFirma(lectura);
  firma.className = `firma ${firmable === "no" ? "mal" : firmable === "firma" ? "ok" : "aviso"}`;
  firma.textContent = {
    firma: "Llegó el checkout firmado por el comercio: tu agente lo puede firmar.",
    aprobada: "Llegó el checkout con tu aprobación humana: tu agente lo puede firmar.",
    no: "No llegó ningún checkout: no hay nada que firmar.",
  }[firmable];
  for (const g of agruparChequeos(veredicto?.chequeos)) {
    const cuenta = Object.entries(g.cuenta).map(([res, n]) => `${n} ${res}`).join(", ");
    const clase = { ok: "ok", aviso: "aviso", escalar: "escalar", bloquea: "mal" }[g.peor] || "";
    $("v-chequeos").append(el("li", { clase, texto: `${g.tipo}: ${cuenta}` }));
  }
  if (veredicto?.status === "escalar" && firmable === "no") {
    mostrar($("escalar"));
    $("aprobar").disabled = false;
    estadoEn("aprobacion-estado", "");
  }
  estadoEn("checkout-estado", `Veredicto de ${cart}: ${veredicto?.status || "ninguno"}.`, firmable === "no" ? "mal" : "ok");
}

async function aprobar() {
  const cuerpo = cuerpoAprobacion(estado.cart, estado.contexto);
  if (!cuerpo) return;
  const seguro = window.confirm(
    `¿Aprobás el checkout de ${estado.cart} aunque KaiAI lo escaló? Vale una sola vez, para este cart y este total.`,
  );
  if (!seguro) return;
  $("aprobar").disabled = true;
  const r = await pedirApi("POST", `/v1/compras/${estado.compra.compra_id}/aprobaciones`, cuerpo);
  if (r.status === 200 || r.status === 201) {
    estadoEn("aprobacion-estado", `Aprobación registrada${r.datos?.id ? ` (${r.datos.id})` : ""}. Ahora reintentá el checkout.`, "ok");
    $("reintentar").focus();
    return;
  }
  $("aprobar").disabled = false;
  estadoEn("aprobacion-estado", `No se pudo aprobar: ${r.error}`, "mal");
}

// ---------------------------------------------------------------------------
// La compra completa: veredictos y recibos
// ---------------------------------------------------------------------------

async function clavePublica() {
  if (estado.publica) return estado.publica;
  const r = await pedirApi("GET", "/v1/public-key", undefined, { conClave: false });
  if (r.status === 200 && r.datos) estado.publica = r.datos;
  return estado.publica;
}

async function cargarCompra() {
  if (!estado.compra) return;
  const r = await pedirApi("GET", `/v1/compras/${estado.compra.compra_id}`);
  if (r.status !== 200 || !r.datos) return;
  actualizarCompra(r.datos);
  mostrar($("sec-compra"));
  $("r-estado").textContent = r.datos.estado || "—";
  const aprobaciones = Array.isArray(r.datos.aprobaciones) ? r.datos.aprobaciones : [];
  $("r-aprobaciones").textContent = aprobaciones.length
    ? aprobaciones.map((a) => `${a.cart_id || "?"} ${formatearMonto(a.monto, a.moneda)} (${a.estado || "?"})`).join(" · ")
    : "ninguna";
  const lista = $("lista-veredictos");
  vaciar(lista);
  const veredictos = Array.isArray(r.datos.veredictos) ? r.datos.veredictos : [];
  if (!veredictos.length) lista.append(el("li", { clase: "ayuda", texto: "Todavía no hay veredictos." }));
  const publica = veredictos.some((v) => v?.recibo?.jws) ? await clavePublica() : null;
  for (const v of veredictos) lista.append(itemVeredicto(v, publica));
}

function itemVeredicto(v, publica) {
  const info = infoStatus(v?.status);
  const firma = el("p", { clase: "firma", texto: v?.recibo?.jws ? "Verificando la firma…" : "Sin recibo firmado." });
  const item = el("li", { clase: "item-veredicto" }, [
    el("p", {}, [el("span", { clase: `pastilla ${info.clase}`, texto: info.etiqueta })]),
    el("p", { clase: "motivo", texto: v?.motivo || "" }),
    el("p", {
      clase: "meta",
      texto: [`cart ${v?.cart_id || "—"}`, v?.contexto_id ? `contexto ${v.contexto_id}` : null, v?.ts,
        v?.llega_a_firma ? "llega a la firma" : "no llega a la firma"].filter(Boolean).join(" · "),
    }),
    firma,
  ]);
  if (v?.recibo?.jws) {
    item.append(el("p", { clase: "meta", texto: `Recibo ${v.recibo.id || "?"} · kid ${v.recibo.kid || "?"}` }));
    verificar(v.recibo, publica, firma, item);
  }
  return item;
}

async function verificar(recibo, publica, nodo, item) {
  if (!publica) {
    nodo.className = "firma aviso";
    nodo.textContent = "No se pudo bajar la clave pública (GET /v1/public-key): la firma no se verificó.";
    return;
  }
  const resultado = await verificarRecibo(recibo, publica);
  nodo.className = `firma ${resultado.ok ? "ok" : "mal"}`;
  nodo.textContent = resultado.ok ? "Firma verificada ✓ (ES256, en tu navegador)" : `Firma inválida ✗: ${resultado.motivo}`;
  const clave = `${recibo.id}:${resultado.ok}`;
  if (!estado.verificados.has(clave)) {
    estado.verificados.add(clave);
    registrar(`Recibo ${recibo.id || "?"}: ${resultado.ok ? "firma verificada" : `firma inválida (${resultado.motivo})`}`, resultado.ok ? "ok" : "mal");
  }
  if (resultado.payload) {
    const texto = JSON.stringify(resultado.payload, null, 2);
    item.append(el("details", {}, [
      el("summary", { texto: "Ver el contenido del recibo (dato no confiable: incluye lo que declaró el comercio)" }),
      el("pre", { texto }),
    ]));
  }
}

// ---------------------------------------------------------------------------
// Filtro
// ---------------------------------------------------------------------------

async function filtrar(evento) {
  evento.preventDefault();
  if (!estado.clave) {
    estadoEn("filtro-estado", "Primero cargá tu clave de integrador (paso 1).", "mal");
    return;
  }
  const cuerpo = cuerpoFiltrar($("filtro-texto").value, $("filtro-origen").value);
  if (!cuerpo) {
    estadoEn("filtro-estado", "Pegá algún texto.", "mal");
    return;
  }
  estadoEn("filtro-estado", "Filtrando…", "espera");
  mostrar($("filtro-resultado"), false);
  const r = await pedirApi("POST", "/v1/filtrar", cuerpo);
  if (r.status !== 200 || !r.datos) {
    estadoEn("filtro-estado", `No se pudo filtrar: ${r.error}`, "mal");
    return;
  }
  estadoEn("filtro-estado", "");
  mostrar($("filtro-resultado"));
  const pasa = r.datos.pasa === true;
  $("f-status").className = `pastilla ${pasa ? "ok" : "mal"}`;
  $("f-status").textContent = pasa ? "Pasa" : "Bloqueado";
  $("f-motivo").textContent = r.datos.motivo || (pasa ? "El filtro no vio nada raro." : "");
  const senales = $("f-senales");
  vaciar(senales);
  const lista = Array.isArray(r.datos.senales) ? r.datos.senales : [];
  if (!lista.length) senales.append(el("li", { texto: "ninguna" }));
  for (const s of lista) senales.append(el("li", { clase: pasa ? "" : "mal", texto: String(s) }));
}

// ---------------------------------------------------------------------------
// Pestañas
// ---------------------------------------------------------------------------

function elegirPestana(tab, enfocar = false) {
  for (const otra of document.querySelectorAll('[role="tab"]')) {
    const activa = otra === tab;
    otra.setAttribute("aria-selected", String(activa));
    otra.tabIndex = activa ? 0 : -1;
    mostrar($(otra.getAttribute("aria-controls")), activa);
  }
  if (enfocar) tab.focus();
}

function teclaPestana(evento) {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const i = tabs.indexOf(evento.currentTarget);
  const destino = {
    ArrowRight: tabs[(i + 1) % tabs.length],
    ArrowLeft: tabs[(i - 1 + tabs.length) % tabs.length],
    Home: tabs[0],
    End: tabs[tabs.length - 1],
  }[evento.key];
  if (destino) {
    evento.preventDefault();
    elegirPestana(destino, true);
  }
}

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

function llenarEscenarios() {
  const select = $("escenario");
  for (const e of ESCENARIOS) {
    const opcion = el("option", { texto: e.etiqueta, atributos: { value: e.id } });
    opcion.selected = e.id === ESCENARIO_POR_DEFECTO;
    select.append(opcion);
  }
  const ayuda = () => {
    const e = ESCENARIOS.find((x) => x.id === select.value);
    $("escenario-ayuda").textContent = `${e ? e.ayuda : ""} Solo lo entiende el comercio de la simulación.`;
  };
  select.addEventListener("change", ayuda);
  ayuda();
}

function arrancar() {
  llenarEscenarios();
  $("plazo").value = proximoLunes();
  $("form-clave").addEventListener("submit", usarClave);
  $("olvidar").addEventListener("click", olvidarClave);
  $("mostrar-clave").addEventListener("click", alternarClave);
  $("recordar").addEventListener("change", () => guardarSesion($("recordar").checked ? estado.clave : null));
  $("form-compra").addEventListener("submit", crearCompra);
  $("buscar").addEventListener("click", buscar);
  $("responder").addEventListener("click", () => responder(false));
  $("omitir").addEventListener("click", () => responder(true));
  $("aceptar").addEventListener("click", () => decidir("aceptar"));
  $("rechazar").addEventListener("click", () => decidir("rechazar"));
  $("checkout").addEventListener("click", pedirCheckout);
  $("reintentar").addEventListener("click", pedirCheckout);
  $("aprobar").addEventListener("click", aprobar);
  $("refrescar").addEventListener("click", cargarCompra);
  $("nueva").addEventListener("click", () => {
    reiniciarCompra();
    registrar("Nueva compra");
    $("pedido").focus();
  });
  $("form-filtro").addEventListener("submit", filtrar);
  $("filtro-ejemplo").addEventListener("click", () => {
    $("filtro-texto").value = EJEMPLO_INYECCION;
    $("filtro-texto").focus();
  });
  $("limpiar-registro").addEventListener("click", () => vaciar($("registro")));
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.addEventListener("click", () => elegirPestana(tab));
    tab.addEventListener("keydown", teclaPestana);
  }
  const guardada = leerSesion();
  if (claveValida(guardada)) {
    $("clave").value = guardada;
    $("recordar").checked = true;
    usarClave();
  }
}

arrancar();
