// Vista 7: nuestro agente (etapa 2, docs/FLUJO.md). Le pregunta al usuario
// para qué necesita lo que pidió y le consulta las alternativas que podrían
// servirle. El panel no decide: reenvía al proxy lo que contesta el usuario.
// Todo texto externo (pedido, pregunta, título, por qué…) entra como texto.

import {
  CAMPOS, ESPERAN_AL_USUARIO, FINALES, MAX_RESPUESTA, TEXTO_ESTADO, TEXTO_FINAL, compraElegida,
  cuerpoContexto, desenlaceContexto, describirRequisito, esperaAlUsuario, formatearEntrega,
  formatearValor, restante,
} from "../lib/agente.js";
import { chip, dinero, fechaHora, filaDato, h, icono, vacio } from "./comunes.js";

const TONO_ESTADO = {
  esperando_contexto: "serious", negociando: "neutro", esperando_decision: "serious",
  sin_oportunidad: "neutro", aceptada: "ok", rechazada: "neutro", vencida: "warn", bloqueada: "crit",
};
const TONO_FINAL = { aceptada: "ok", rechazada: "neutro", vencida: "warn", sin_oportunidad: "neutro", bloqueada: "crit" };
const ICONO_FINAL = { aceptada: "check", rechazada: "x", vencida: "alert", sin_oportunidad: "arrow", bloqueada: "block" };
// Sin contexto, la negociación sigue con el pedido literal.
const SIN_CONTEXTO = new Set(["omitido", "vencido", "sin_contexto", "sin_respuesta"]);

const QUIEN = {
  cliente: ["Agente del cliente", "cart"],
  nuestro: ["Nuestro agente", "shield"],
  usuario: ["Vos", "pen"],
  sistema: ["Proxy", "dot"],
};

export function renderAgente(raiz, ctx) {
  const compras = ctx.comprasAgente;
  const compra = compraElegida(compras, ctx.estado.agenteSel);
  const vivo = ctx.estado.fuente === "vivo";
  // replaceChildren(null) escribiría "null": se filtran los bloques vacíos.
  raiz.replaceChildren(...[
    h("header", { class: "vista-cab" },
      h("p", { class: "eyebrow", text: vivo
        ? `En vivo desde el proxy (${String(ctx.estado.config.proxy_url).replace(/^https?:\/\//, "")})`
        : `Replay · ${ctx.estado.replaySel ?? "sin archivo"} · solo lectura` }),
      h("h2", { text: "Nuestro agente" }),
      h("p", { class: "vista-bajada", text: "Te pregunta para qué necesitás lo que pediste, observa la negociación y, si aparece una alternativa que podría servirte, te la consulta. No negocia ni le habla al proveedor: vos autorizás y el agente del cliente ejecuta." }),
      compras.length > 1 ? selector(compras, ctx) : null),
    capasApagadas(ctx),
    otrasEsperando(compras, compra, ctx),
    compra ? conversacion(compra, ctx) : vacioAgente(ctx),
  ].filter(Boolean));
}

function resumir(texto, n = 48) {
  const t = String(texto ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

function selector(compras, ctx) {
  return h("div", { class: "filtros" },
    h("label", { class: "campo" }, h("span", { text: "Compra" }),
      h("select", { name: "compra-agente", onchange: (e) => ctx.set({ agenteSel: e.target.value || null }) },
        h("option", { value: "", text: "Seguir la última" }),
        compras.map((c) => h("option", {
          value: c.compra_id,
          selected: ctx.estado.agenteSel === c.compra_id ? true : null,
          text: `${TEXTO_ESTADO[c.estado] ?? c.estado ?? "?"} · ${resumir(c.pedido) || "sin pedido"} · ${c.compra_id}`,
        })))));
}

/** Lo que informa GET /guard/state: capa de intención o filtro apagados. */
function capasApagadas(ctx) {
  const p = ctx.estado.proxy;
  if (ctx.estado.fuente !== "vivo" || p.conectado !== true) return null;
  const avisos = [];
  if (p.intencion === null) {
    avisos.push("Capa de intención apagada en el proxy (¿falta GOOGLE_API_KEY?): no pregunta el porqué ni busca alternativas; la negociación sigue con el pedido literal.");
  }
  if (p.filtro === false) avisos.push("El proxy no tiene cargado el filtro de seguridad.");
  if (!avisos.length) return null;
  return h("div", { class: "aviso t-warn" }, icono("alert"), h("span", { text: avisos.join(" ") }));
}

/** Otras compras que esperan al usuario (la elegida se ve abajo). */
function otrasEsperando(compras, actual, ctx) {
  const ahora = ctx.ahora();
  const otras = compras.filter((c) => c !== actual && esperaAlUsuario(c, ahora));
  if (!otras.length) return null;
  // Sin role="status": se vuelve a dibujar con cada render; el aviso para
  // lectores de pantalla lo da anunciar() cuando llega el evento.
  return h("div", { class: "aviso t-serious" }, icono("alert"),
    h("span", {}, otras.length === 1 ? "Otra compra espera tu respuesta: " : `${otras.length} compras esperan tu respuesta: `,
      otras.map((c) => h("button", {
        type: "button", class: "boton boton-enlace", onclick: () => ctx.set({ agenteSel: c.compra_id }),
      }, resumir(c.pedido, 32) || c.compra_id))));
}

function vacioAgente(ctx) {
  if (ctx.estado.fuente === "vivo") {
    return vacio(ctx.estado.proxy.conectado === false ? "Proxy desconectado: nuestro agente no ve pedidos" : "Todavía no hay pedidos",
      h("p", { text: "Cuando el agente del cliente busca algo a través del proxy (:8010), el proxy retiene la búsqueda y nuestro agente te pregunta acá para qué lo necesitás." }),
      h("p", { text: "Sin el flujo levantado: pasá a «Replay (archivo)» arriba." }));
  }
  return vacio("Este replay no tiene conversaciones con nuestro agente",
    h("p", { text: "Las arman los eventos pedido_detectado, contexto_registrado, oportunidad y oportunidad_decidida del proxy (etapa 2)." }));
}

// ---------------------------------------------------------------------------
// Conversación
// ---------------------------------------------------------------------------

function conversacion(c, ctx) {
  const ahora = ctx.ahora();
  // Igual que "Aprobar igual": solo en vivo y con el proxy conectado.
  const habilitado = ctx.estado.fuente === "vivo" && ctx.estado.proxy.conectado === true;
  const fin = desenlaceContexto(c);
  return h("section", { class: "agente", "aria-label": "Conversación con nuestro agente" },
    h("header", { class: "agente-cab" },
      h("h3", { class: "agente-id", text: `Compra ${c.compra_id}` }),
      chip(TEXTO_ESTADO[c.estado] ?? (c.estado ? `Estado: ${c.estado}` : "Sin estado"), { tono: TONO_ESTADO[c.estado] ?? "neutro" }),
      c.creada ? h("span", { class: "nota mono", text: fechaHora(c.creada) }) : null,
      c.contexto_id ? h("span", { class: "nota mono", title: "context_id de A2A", text: `contexto ${c.contexto_id.slice(0, 8)}` }) : null),
    h("ol", { class: "chat" },
      msjPedido(c),
      c.pregunta ? mensaje("nuestro", {}, h("p", { class: "msj-texto", text: c.pregunta })) : null,
      bloqueRespuesta(c, ctx, ahora, habilitado),
      bloqueEntendi(c),
      // Mientras nuestro agente arma el contexto, la búsqueda sigue retenida.
      c.estado === "negociando" && fin !== "armando" ? mensaje("sistema", { tono: "neutro" }, h("p", {
        text: SIN_CONTEXTO.has(fin)
          ? "El agente del cliente negocia con el proveedor con el pedido literal."
          : "El agente del cliente negocia con el proveedor. Si una oferta no cumple el pedido pero podría cubrir tu necesidad, te la consulto acá.",
      })) : null,
      c.oportunidad ? tarjetaOportunidad(c, ctx, ahora, habilitado) : null,
      FINALES.has(c.estado) ? mensajeFinal(c) : null),
    // Si la compra ya no espera nada, el error del último POST (por ejemplo,
    // un 409 porque venció) se ve acá abajo.
    ESPERAN_AL_USUARIO.has(c.estado) ? null : avisoPost(c, ctx, { soloErrores: true }),
  );
}

function mensaje(quien, { tono = null, clase = null } = {}, ...cuerpo) {
  const [nombre, ic] = QUIEN[quien];
  const extra = Array.isArray(clase) ? clase : [clase];
  return h("li", { class: ["msj", `msj-${quien}`, tono && `t-${tono}`, ...extra] },
    h("p", { class: "msj-quien" }, icono(ic), nombre),
    h("div", { class: "msj-burbuja" }, ...cuerpo));
}

function listaRequisitos(requisitos) {
  const acumulado = requisitos.some((q) => q?.operador === "cubre");
  return [
    h("ul", { class: "requisitos" }, requisitos.map((q) => {
      const d = describirRequisito(q);
      return h("li", {},
        h("span", { class: "req-campo", text: d.campo }),
        h("span", { class: "mono", text: d.condicion }),
        d.obligatorio ? null : chip("preferido", { tono: "neutro" }));
    })),
    acumulado ? h("p", { class: "nota", text: "Entregas acumuladas: al menos esa cantidad entregada a más tardar en cada fecha." }) : null,
  ];
}

function msjPedido(c) {
  if (!c.pedido && !c.requisitos.length) return null;
  return mensaje("cliente", {},
    c.pedido ? h("blockquote", { class: "msj-pedido" }, h("p", { text: c.pedido })) : null,
    c.requisitos.length
      ? [h("p", { class: "rotulo", text: "Parámetros autorizados (pedido explícito)" }), listaRequisitos(c.requisitos)]
      : null);
}

/** Lo que contestó el usuario, o el cuadro para contestar. */
function bloqueRespuesta(c, ctx, ahora, habilitado) {
  if (c.estado === "esperando_contexto") return composer(c, ctx, ahora, habilitado);
  const fin = desenlaceContexto(c);
  // Lo que contestó el usuario: del contexto o, si todavía no está (o no se
  // pudo armar), lo que este panel le mandó al proxy.
  const respuesta = fin === "respondido" ? c.contexto.respuesta : ctx.respuestaEnviada(c.compra_id);
  switch (fin) {
    case "respondido": case "armando": case "sin_contexto":
      return respuesta ? mensaje("usuario", {}, h("p", { class: "msj-texto", text: respuesta })) : null;
    case "omitido": return mensaje("sistema", { tono: "neutro" }, h("p", { text: "Omitiste la pregunta." }));
    case "vencido": return mensaje("sistema", { tono: "warn" }, h("p", { text: "No hubo respuesta a tiempo." }));
    case "sin_respuesta": return mensaje("sistema", { tono: "neutro" }, h("p", { text: "Sin respuesta al porqué." }));
    default: return null;
  }
}

function notaHabilitado(ctx, habilitado, texto) {
  if (habilitado) return texto;
  return ctx.estado.fuente === "vivo" ? "Solo en vivo, con el proxy conectado." : "Replay: solo lectura.";
}

function composer(c, ctx, ahora, habilitado) {
  const id = c.compra_id;
  const vencido = restante(c.vence, ahora)?.vencido === true;
  const enviando = ctx.enviandoAgente(id);
  const activo = habilitado && !vencido && !enviando && !ctx.yaEnviado(c);
  const borrador = habilitado ? ctx.borrador(id) : "";
  const contar = (t) => `${t.trim().length}/${MAX_RESPUESTA}`;
  const puedeEnviar = (t) => activo && cuerpoContexto(id, t) !== null;

  // El texto del borrador va en .value (no en el HTML): así un render que
  // dibuja lo mismo devuelve los nodos de antes y no se pierde lo escrito.
  const contador = h("span", { class: "nota mono", "aria-hidden": "true", text: contar(borrador) });
  const enviar = h("button", {
    class: "boton boton-primario", type: "button", disabled: puedeEnviar(borrador) ? null : true,
    onclick: () => ctx.responder(c),
  }, icono("send"), enviando ? "Enviando…" : "Enviar");
  const area = h("textarea", {
    class: "composer-texto", rows: "3", maxlength: String(MAX_RESPUESTA), "data-foco": `respuesta:${id}`,
    "aria-label": "Tu respuesta: para qué lo necesitás y qué es flexible",
    placeholder: "Contale para qué lo necesitás y qué es flexible",
    disabled: activo ? null : true,
    oninput: (e) => {
      ctx.escribir(id, e.target.value);
      contador.textContent = contar(e.target.value);
      enviar.disabled = !puedeEnviar(e.target.value);
    },
    onkeydown: (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        if (!enviar.disabled) ctx.responder(c);
      }
    },
  });
  area.value = borrador;

  return mensaje("usuario", { clase: "msj-composer" },
    area,
    h("div", { class: "acciones" },
      enviar,
      h("button", {
        class: "boton", type: "button", disabled: activo ? null : true,
        onclick: () => ctx.responder(c, { omitir: true }),
      }, "Omitir"),
      contador),
    cuenta(c, ahora, {
      falta: "para contestar",
      vencio: "Se venció el plazo: la negociación sigue con el pedido literal (falta que el proxy lo confirme).",
    }),
    h("p", { class: "nota", text: notaHabilitado(ctx, habilitado,
      "Si no contestás u omitís, la negociación sigue con el pedido literal, sin buscar alternativas. Ctrl+Enter envía.") }),
    avisoPost(c, ctx));
}

/**
 * Cuenta regresiva hasta `vence`. El número lo actualiza app.js cada segundo
 * (`[data-vence]`) sin volver a dibujar la vista.
 */
function cuenta(c, ahora, { falta, vencio }) {
  const plazo = restante(c.vence, ahora);
  if (!plazo) return null;
  if (plazo.vencido) return h("p", { class: "cuenta t-warn" }, icono("alert"), vencio);
  return h("p", { class: "cuenta" },
    "Quedan ", h("span", { class: "mono cuenta-valor", "data-vence": c.vence, text: plazo.texto }), ` ${falta}.`);
}

function bloqueEntendi(c) {
  const fin = desenlaceContexto(c);
  if (!fin) return null;
  if (fin === "armando") {
    return mensaje("nuestro", {}, h("p", { text: "Estoy armando el contexto con tu respuesta…" }));
  }
  if (fin === "sin_contexto") {
    return mensaje("nuestro", {}, h("p", { text: "No pude armar el contexto: la negociación sigue con el pedido literal, sin buscar alternativas." }));
  }
  if (fin !== "respondido") {
    return mensaje("nuestro", {}, h("p", { text: "Sin contexto: la negociación sigue con el pedido literal, sin buscar alternativas." }));
  }
  const x = c.contexto;
  const entregas = Array.isArray(x.necesidad_entregas) ? x.necesidad_entregas : [];
  const flex = Array.isArray(x.flexibilidades) ? x.flexibilidades : [];
  return mensaje("nuestro", { clase: "msj-entendi" },
    h("p", { class: "rotulo", text: "Lo que entendí" }),
    h("dl", { class: "campos" },
      filaDato("Necesidad", x.necesidad || "No la pude resumir en una frase."),
      entregas.length ? filaDato("Cuándo lo necesitás",
        h("ul", { class: "entregas" }, entregas.map((e) => h("li", { class: "mono", text: formatearEntrega(e) })))) : null),
    flex.length ? [
      h("p", { class: "rotulo", text: "Qué es flexible" }),
      h("ul", { class: "flexibilidades" }, flex.map((f) => h("li", {},
        h("span", { text: f?.descripcion ?? "—" }),
        f?.origen === "inferida"
          ? chip("inferida", { tono: "warn", icon: "alert", titulo: "La dedujo nuestro agente: no lo dijiste" })
          : chip("lo dijiste", { tono: "ok", icon: "check" })))),
    ] : h("p", { class: "nota", text: "No identifiqué nada flexible." }),
    h("p", { class: "nota", text: "Conocer la necesidad no autoriza a cambiar el pedido: los parámetros de arriba siguen vigentes hasta que aceptes un cambio." }));
}

function tablaCambios(cambios) {
  return h("div", { class: "tabla-scroll" }, h("table", { class: "tabla tabla-cambios" },
    h("thead", {}, h("tr", {}, ["Campo", "Pediste", "", "Ofrece"].map((t) => h("th", { scope: "col", text: t })))),
    h("tbody", {}, cambios.map((x) => h("tr", {},
      h("th", { scope: "row", text: CAMPOS[x?.campo] ?? String(x?.campo ?? "—") }),
      h("td", { class: "mono valor", text: formatearValor(x?.campo, x?.pedido) }),
      h("td", { class: "flecha", "aria-hidden": "true" }, icono("arrow")),
      h("td", { class: "mono valor ofrecido", text: formatearValor(x?.campo, x?.ofrecido) }))))));
}

function tarjetaOportunidad(c, ctx, ahora, habilitado) {
  const o = c.oportunidad;
  const pendiente = c.estado === "esperando_decision";
  const vencido = restante(c.vence, ahora)?.vencido === true;
  const enviando = ctx.enviandoAgente(c.compra_id);
  const activo = pendiente && habilitado && !vencido && !enviando && !ctx.yaEnviado(c);
  const cambios = Array.isArray(o.cambios) ? o.cambios : [];
  const propuestos = Array.isArray(o.requisitos_propuestos) ? o.requisitos_propuestos : [];
  const precio = typeof o.precio === "number" ? dinero(o.precio, o.moneda || "") : "—";
  return mensaje("nuestro", { clase: ["msj-oportunidad", pendiente && "pendiente"] },
    h("h4", { class: "oportunidad-titulo" }, icono("up"), "Posible punto de encuentro"),
    h("p", { text: "La oferta no cumple el pedido, pero podría cubrir tu necesidad. Esto es lo que cambia:" }),
    cambios.length ? tablaCambios(cambios) : h("p", { class: "nota", text: "La consulta no trae el detalle de los cambios." }),
    h("dl", { class: "campos" },
      filaDato("Oferta", h("strong", { text: o.titulo || "—" })),
      o.sku ? filaDato("SKU", h("span", { class: "mono", text: o.sku })) : null,
      filaDato("Precio", h("span", { class: "mono", text: precio })),
      o.cart_id ? filaDato("Cart", h("span", { class: "mono", text: o.cart_id })) : null),
    o.por_que ? [h("p", { class: "rotulo", text: "Por qué podría servirte" }), h("p", { class: "por-que", text: o.por_que })] : null,
    propuestos.length ? h("details", { class: "mas" },
      h("summary", { text: "Parámetros que quedarían autorizados si aceptás" }), listaRequisitos(propuestos)) : null,
    pendiente ? [
      h("p", { class: "oportunidad-pregunta", text: "¿Aceptás cambiar el pedido por esta opción?" }),
      h("div", { class: "acciones" },
        h("button", {
          class: "boton boton-primario", type: "button", disabled: activo ? null : true,
          onclick: () => ctx.decidir(c, "aceptar"),
        }, icono("check"), "Aceptar"),
        h("button", {
          class: "boton", type: "button", disabled: activo ? null : true,
          onclick: () => ctx.decidir(c, "rechazar"),
        }, icono("x"), "Rechazar"),
        enviando ? h("span", { class: "nota", text: "Enviando…" }) : null),
      cuenta(c, ahora, {
        falta: "para decidir",
        vencio: "Se venció el plazo: no se compra nada (falta que el proxy lo confirme).",
      }),
      h("p", { class: "nota", text: notaHabilitado(ctx, habilitado, "El silencio no es autorización: si no decidís, no se compra nada.") }),
      avisoPost(c, ctx),
    ] : null);
}

function mensajeFinal(c) {
  const quien = c.estado === "aceptada" || c.estado === "rechazada" ? "nuestro" : "sistema";
  if (c.estado === "bloqueada") {
    return mensaje(quien, { tono: TONO_FINAL.bloqueada, clase: "msj-final" },
      h("p", { class: "msj-final-texto" }, icono(ICONO_FINAL.bloqueada), h("strong", { text: TEXTO_FINAL.bloqueada })),
      h("p", { class: "nota", text: "El filtro de seguridad cortó la conversación: lo bloqueado no llega a ningún agente y no se compra nada." }));
  }
  const autorizados = Array.isArray(c.requisitos_autorizados) ? c.requisitos_autorizados : [];
  let detalle = null;
  if (c.estado === "aceptada") {
    detalle = [
      autorizados.length
        ? [h("p", { class: "rotulo", text: "Parámetros autorizados ahora" }), listaRequisitos(autorizados)]
        : h("p", { class: "nota", text: "El proxy no informó los parámetros autorizados." }),
      h("p", { class: "nota", text: "El agente del cliente sigue con estos parámetros. Antes de la firma, el Guard verifica la compra contra ellos." }),
    ];
  } else if (c.estado === "rechazada") {
    detalle = h("p", { class: "nota", text: "Esa oferta no se acepta: el agente del cliente sigue con el pedido literal." });
  }
  return mensaje(quien, { tono: TONO_FINAL[c.estado], clase: "msj-final" },
    h("p", { class: "msj-final-texto" }, icono(ICONO_FINAL[c.estado] ?? "dot"), h("strong", { text: TEXTO_FINAL[c.estado] })),
    detalle);
}

/** Resultado del último POST de esta compra (en curso, error u OK). */
function avisoPost(c, ctx, { soloErrores = false } = {}) {
  const m = ctx.mensajeAgente(c.compra_id);
  if (!m || (soloErrores && m.ok)) return null;
  return h("p", { class: ["nota", m.ok ? "t-ok" : "t-crit", "aviso-post"], role: "status", text: m.texto });
}
