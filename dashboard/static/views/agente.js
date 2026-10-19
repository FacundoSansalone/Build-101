// Vista 7: nuestro agente (etapa 2, docs/FLUJO.md). Le pregunta al usuario
// para qué necesita lo que pidió y le consulta las alternativas que podrían
// servirle. El panel no decide: reenvía al proxy lo que contesta el usuario.
// Todo texto externo (pedido, pregunta, título, por qué…) entra como texto.

import {
  CAMPOS, ESPERAN_AL_USUARIO, FINALES, MAX_RESPUESTA, PARAMETROS, TEXTO_ESTADO, TEXTO_FINAL,
  compraElegida, cuerpoContexto, desenlaceContexto, describirParametro, describirRequisito,
  esperaAlUsuario, formatearEntrega, formatearValor, mismosRequisitos, nombreOferta, restante,
  tieneIntake, turnoActual,
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
// Ficha parcial del intake: estado de cada parámetro.
const TONO_PARAMETRO = { resuelto: "ok", a_confirmar: "warn", faltante: "crit" };
const ICONO_PARAMETRO = { resuelto: "check", a_confirmar: "alert", faltante: "dash" };

/** El estado de la compra en palabras, con el turno del intake si hay. */
function textoEstado(c) {
  if (c.estado === "esperando_contexto" && c.intake?.confirmar) return "Esperando que confirmes la ficha";
  if (c.estado === "esperando_contexto" && turnoActual(c) > 0) return `Esperando tu respuesta (repregunta ${turnoActual(c)})`;
  if (c.estado === "negociando" && tieneIntake(c) && !c.intake.final) return "Revisando tu respuesta";
  return TEXTO_ESTADO[c.estado] ?? (c.estado ? `Estado: ${c.estado}` : "Sin estado");
}

/** ¿Terminó la parte de preguntas (o no hubo intake)? */
const intakeTerminado = (c) => !tieneIntake(c) || c.intake.final === true;

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
          text: `${textoEstado(c)} · ${resumir(c.pedido) || "sin pedido"} · ${c.compra_id}`,
        })))));
}

/** Lo que informa GET /guard/state: capa de intención o filtro apagados. */
function capasApagadas(ctx) {
  const p = ctx.estado.proxy;
  if (ctx.estado.fuente !== "vivo" || p.conectado !== true) return null;
  const avisos = [];
  if (p.intencion === null) {
    avisos.push("Capa de intención apagada en el proxy (¿falta OPENAI_API_KEY o GOOGLE_API_KEY?): no pregunta el porqué ni busca alternativas; la negociación sigue con el pedido literal.");
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
      chip(textoEstado(c), { tono: TONO_ESTADO[c.estado] ?? "neutro" }),
      c.creada ? h("span", { class: "nota mono", text: fechaHora(c.creada) }) : null,
      c.contexto_id ? h("span", { class: "nota mono", title: "context_id de A2A", text: `contexto ${c.contexto_id.slice(0, 8)}` }) : null),
    h("ol", { class: "chat" },
      msjPedido(c),
      ...chatTurnos(c, ctx, ahora, habilitado),
      // Entre turnos del intake el proxy revisa la respuesta (todavía no negocia).
      c.estado === "negociando" && !intakeTerminado(c)
        ? mensaje("nuestro", {}, h("p", { text: "Estoy revisando tu respuesta…" })) : null,
      intakeTerminado(c) ? bloqueEntendi(c) : null,
      fichaFinal(c, ctx),
      // Mientras nuestro agente arma el contexto, la búsqueda sigue retenida.
      c.estado === "negociando" && fin !== "armando" && intakeTerminado(c) ? mensaje("sistema", { tono: "neutro" }, h("p", {
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
        d.obligatorio ? null : chip("preferido", { tono: "neutro" }),
        d.inferido ? chip("lo deduje", { tono: "warn", icon: "alert", titulo: "Lo dedujo nuestro agente y lo confirmaste" }) : null);
    })),
    acumulado ? h("p", { class: "nota", text: "Entregas acumuladas: al menos esa cantidad entregada a más tardar en cada fecha." }) : null,
  ];
}

// Con intake, lo que entendió nuestro agente no está autorizado hasta que
// termina (lo inferido, solo si el usuario confirma); sin intake, el pedido
// explícito es lo autorizado.
function rotuloRequisitos(c) {
  if (!tieneIntake(c)) return "Parámetros autorizados (pedido explícito)";
  return c.intake.final === true
    ? "Parámetros vigentes: con estos verifica el Guard"
    : "Lo que entendió nuestro agente (todavía sin autorizar)";
}

function msjPedido(c) {
  if (!c.pedido && !c.requisitos.length) return null;
  return mensaje("cliente", {},
    c.pedido ? h("blockquote", { class: "msj-pedido" }, h("p", { text: c.pedido })) : null,
    c.requisitos.length
      ? [h("p", { class: "rotulo", text: rotuloRequisitos(c) }), listaRequisitos(c.requisitos)]
      : null);
}

// ---------------------------------------------------------------------------
// Turnos del intake: el porqué, las repreguntas y la confirmación de la ficha
// ---------------------------------------------------------------------------

/**
 * Una pregunta por turno, cada una con su respuesta (o el cuadro para
 * contestar, en el turno actual). Sin turnos en los eventos (solo el sondeo
 * del proxy, o un proxy sin intake), una sola pregunta como antes.
 */
function chatTurnos(c, ctx, ahora, habilitado) {
  if (!c.turnos.length) {
    return [
      c.pregunta ? mensaje("nuestro", {}, h("p", { class: "msj-texto", text: c.pregunta })) : null,
      bloqueRespuesta(c, ctx, ahora, habilitado),
    ];
  }
  const actual = turnoActual(c);
  return c.turnos.flatMap((t) => {
    const esperando = c.estado === "esperando_contexto" && t.turno === actual;
    let respuesta;
    if (esperando) respuesta = composer(c, ctx, ahora, habilitado, { confirmar: t.confirmar });
    else if (t.turno === 0 && t.respuesta === null && !t.omitido && !t.vencido) {
      respuesta = bloqueRespuesta(c, ctx, ahora, habilitado); // eventos de un proxy sin `respuesta`
    } else respuesta = respuestaTurno(c, t, ctx);
    return [msjPregunta(t), respuesta];
  });
}

function msjPregunta(t) {
  const faltan = t.faltan.map((n) => PARAMETROS[n] ?? String(n));
  const ficha = t.ficha_parcial;
  return mensaje("nuestro", { clase: t.confirmar ? "msj-confirmar" : null },
    t.turno > 0 ? h("p", { class: "rotulo", text: t.confirmar ? "Ficha de la compra" : `Repregunta ${t.turno}` }) : null,
    h("p", { class: "msj-texto", text: t.pregunta || "—" }),
    faltan.length ? h("p", { class: "faltan" },
      h("span", { class: "nota", text: "Me falta:" }), faltan.map((f) => chip(f, { tono: "serious", icon: "dash" }))) : null,
    ficha.length && t.confirmar ? tablaFicha(ficha) : null,
    ficha.length && !t.confirmar ? h("details", { class: "mas" },
      h("summary", { text: "Lo que tengo hasta ahora" }), tablaFicha(ficha)) : null);
}

function respuestaTurno(c, t, ctx) {
  if (t.respuesta) return mensaje("usuario", {}, h("p", { class: "msj-texto", text: t.respuesta }));
  if (t.confirmada) {
    return mensaje("usuario", {}, h("p", { class: "msj-final-texto" }, icono("check"), h("strong", { text: "Confirmaste la ficha." })));
  }
  if (t.omitido) return mensaje("sistema", { tono: "neutro" }, h("p", { text: "Omitiste la pregunta." }));
  if (t.vencido) return mensaje("sistema", { tono: "warn" }, h("p", { text: "No hubo respuesta a tiempo." }));
  // El proxy ya aceptó la respuesta y el evento todavía no llegó.
  const enviada = ctx.respuestaEnviada(c.compra_id, t.turno);
  return enviada ? mensaje("usuario", {}, h("p", { class: "msj-texto", text: enviada })) : null;
}

/** La ficha parcial: cada parámetro con su valor, su estado y de dónde salió. */
function tablaFicha(parcial) {
  return h("div", { class: "tabla-scroll" }, h("table", { class: "tabla tabla-ficha" },
    h("thead", {}, h("tr", {}, ["Parámetro", "Valor", "Estado", "Origen"].map((t) => h("th", { scope: "col", text: t })))),
    h("tbody", {}, parcial.map((p) => {
      const d = describirParametro(p);
      return h("tr", {},
        h("th", { scope: "row", text: d.nombre }),
        h("td", { class: "mono valor", text: d.valor }),
        h("td", {}, chip(d.estado, { tono: TONO_PARAMETRO[p?.estado] ?? "neutro", icon: ICONO_PARAMETRO[p?.estado] ?? null })),
        h("td", {},
          d.origen ? chip(d.origen, { tono: d.origen === "lo deduje" ? "warn" : "ok" }) : null,
          d.obligatorio ? null : chip("preferido", { tono: "neutro" })));
    }))));
}

/** Cómo terminó el intake: la ficha autorizada (confirmada o no). */
function fichaFinal(c, ctx) {
  const i = c.intake;
  if (!i?.final) return null;
  // Solo si el proxy usa las fichas de los escenarios (PROXY_FICHA_DEMO=1, o
  // un replay de una grabación): por defecto manda la que confirmó el usuario.
  const fichaDemo = ctx.estado.fuente === "replay" || ctx.estado.proxy.ficha_demo === true;
  const autorizada = Array.isArray(i.ficha_autorizada) ? i.ficha_autorizada : [];
  const sinGuard = Array.isArray(i.sin_verificar_guard) ? i.sin_verificar_guard : [];
  return mensaje("nuestro", { clase: "msj-ficha" },
    h("p", { class: "rotulo", text: i.confirmada ? "Ficha confirmada: parámetros autorizados" : "Ficha sin confirmar: parámetros autorizados" }),
    autorizada.length ? listaRequisitos(autorizada)
      : h("p", { class: "nota", text: "No quedó ningún parámetro autorizado: sin tope ni moneda, el Guard escala la compra." }),
    h("p", { class: "nota", text: i.confirmada
      ? "La confirmaste: con estos parámetros negocia el agente del cliente y el Guard verifica antes de la firma."
      : "No la confirmaste: quedó autorizado solo lo que dijiste con claridad; lo que deduje, no." }),
    sinGuard.length ? [
      h("p", { class: "rotulo", text: "Lo anoté, pero el Guard todavía no lo verifica" }),
      listaRequisitos(sinGuard)] : null,
    // Con PROXY_FICHA_DEMO=1 y un escenario fijo que trae ficha confirmada,
    // manda esa (la de arriba); por defecto, la del intake.
    fichaDemo && c.requisitos.length && c.estado !== "esperando_contexto" && !mismosRequisitos(c.requisitos, autorizada)
      ? h("p", { class: "aviso t-warn" }, icono("alert"), h("span", {
        text: "El proxy corre con PROXY_FICHA_DEMO=1: en este escenario de prueba manda la ficha del escenario (la de arriba), no esta." }))
      : null,
    i.mensaje ? h("p", { class: "aviso t-warn" }, icono("alert"), h("span", { text: i.mensaje })) : null,
    i.sin_tiempo ? h("p", { class: "nota", text: "No quedaba tiempo de la llamada para seguir preguntando." }) : null);
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

function composer(c, ctx, ahora, habilitado, { confirmar = false } = {}) {
  const id = c.compra_id;
  const vencido = restante(c.vence, ahora)?.vencido === true;
  const enviando = ctx.enviandoAgente(id);
  const activo = habilitado && !vencido && !enviando && !ctx.yaEnviado(c);
  const borrador = habilitado ? ctx.borrador(id) : "";
  const contar = (t) => `${t.trim().length}/${MAX_RESPUESTA}`;
  const puedeEnviar = (t) => activo && cuerpoContexto(id, t) !== null;
  const repregunta = turnoActual(c) > 0;
  const etiqueta = confirmar
    ? "Si algo de la ficha está mal, escribí la corrección"
    : repregunta ? "Tu respuesta: lo que me falta saber" : "Tu respuesta: para qué lo necesitás y qué es flexible";

  // El texto del borrador va en .value (no en el HTML): así un render que
  // dibuja lo mismo devuelve los nodos de antes y no se pierde lo escrito.
  const contador = h("span", { class: "nota mono", "aria-hidden": "true", text: contar(borrador) });
  const enviar = h("button", {
    class: confirmar ? "boton" : "boton boton-primario", type: "button", disabled: puedeEnviar(borrador) ? null : true,
    onclick: () => ctx.responder(c),
  }, icono("send"), enviando ? "Enviando…" : confirmar ? "Enviar corrección" : "Enviar");
  const area = h("textarea", {
    class: "composer-texto", rows: confirmar ? "2" : "3", maxlength: String(MAX_RESPUESTA), "data-foco": `respuesta:${id}`,
    "aria-label": etiqueta,
    placeholder: confirmar ? "Por ejemplo: el talle es 43" : repregunta ? "Por ejemplo: hasta 150 dólares, para el viernes" : "Contale para qué lo necesitás y qué es flexible",
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

  // Confirmar la ficha: el botón principal del último turno del intake.
  const confirmarBoton = confirmar ? h("button", {
    class: "boton boton-primario", type: "button", disabled: activo ? null : true,
    onclick: () => ctx.confirmar(c),
  }, icono("check"), enviando ? "Enviando…" : "Confirmar ficha") : null;
  let nota;
  if (confirmar) nota = "Si no confirmás, queda autorizado solo lo que dijiste con claridad: lo que deduje, no. Ctrl+Enter envía la corrección.";
  else if (repregunta) nota = "Si omitís, la compra sigue con lo que ya dijiste; lo que falte no se autoriza. Ctrl+Enter envía.";
  else nota = "Si no contestás u omitís, la negociación sigue con el pedido literal, sin buscar alternativas. Ctrl+Enter envía.";

  return mensaje("usuario", { clase: "msj-composer" },
    confirmarBoton ? h("div", { class: "acciones" }, confirmarBoton) : null,
    area,
    h("div", { class: "acciones" },
      enviar,
      h("button", {
        class: "boton", type: "button", disabled: activo ? null : true,
        onclick: () => ctx.responder(c, { omitir: true }),
      }, confirmar ? "No confirmar" : "Omitir"),
      contador),
    cuenta(c, ahora, {
      falta: confirmar ? "para confirmar" : "para contestar",
      vencio: repregunta
        ? "Se venció el plazo: la compra sigue con lo que ya dijiste (falta que el proxy lo confirme)."
        : "Se venció el plazo: la negociación sigue con el pedido literal (falta que el proxy lo confirme).",
    }),
    h("p", { class: "nota", text: notaHabilitado(ctx, habilitado, nota) }),
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
  const nombre = nombreOferta(o);
  return mensaje("nuestro", { clase: ["msj-oportunidad", pendiente && "pendiente"] },
    h("h4", { class: "oportunidad-titulo" }, icono("up"), "Posible punto de encuentro"),
    h("p", { text: "La oferta no cumple el pedido, pero podría cubrir tu necesidad. Esto es lo que cambia:" }),
    cambios.length ? tablaCambios(cambios) : h("p", { class: "nota", text: "La consulta no trae el detalle de los cambios." }),
    h("dl", { class: "campos" },
      filaDato("Oferta", h("strong", { text: nombre.texto }),
        nombre.nota ? h("span", { class: "nota", text: ` (${nombre.nota})` }) : null),
      o.sku && nombre.texto !== o.sku ? filaDato("SKU", h("span", { class: "mono", text: o.sku })) : null,
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
