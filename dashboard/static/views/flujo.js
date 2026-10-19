// Vista 2: flujo por compra (línea de tiempo de eventos del proxy).

import { describirEvento, estadoCompra } from "../lib/eventos.js";
import { chip, chipGuard, h, hora, icono, vacio, vistaJson } from "./comunes.js";
import { panelAprobacion } from "./piezas.js";

const ICONO_EVENTO = {
  busqueda_enviada: "search",
  ofertas_recibidas: "cart",
  checkout_pedido: "arrow",
  checkout_recibido: "doc",
  veredicto: "shield",
  reenviado: "send",
  bloqueado: "block",
  guard_cambiado: "shield",
  error: "alert",
  pedido_detectado: "list",
  contexto_registrado: "pen",
  oportunidad: "up",
  oportunidad_decidida: "check",
};

const TONO_DECISION = { aceptada: "ok", rechazada: "neutro", vencida: "warn" };

const TONO_ESTADO = {
  bloqueado: "crit", retenido: "serious", aprobado_humano: "ok", firmado: "ok", para_firmar: "neutro",
  escalar: "serious", error: "crit", en_curso: "neutro", corte_seguridad: "crit", sin_autorizacion: "warn",
  sin_resultados: "warn",
};

function tonoEvento(e) {
  if (e.tipo === "bloqueado" && e.datos?.requiere_aprobacion) return "serious";
  if (e.tipo === "reenviado" && e.datos?.aprobacion_humana) return "ok";
  if (e.tipo === "bloqueado" && e.datos?.etapa === "intencion") return "warn";
  if (e.tipo === "bloqueado" || e.tipo === "error") return "crit";
  if (e.tipo === "veredicto") {
    return { aprobado: "ok", con_avisos: "warn", escalar: "serious", bloqueado: "crit" }[e.datos?.veredicto?.status] ?? "neutro";
  }
  if (e.tipo === "checkout_recibido" && Array.isArray(e.datos?.ap2_violaciones)) {
    return e.datos.ap2_violaciones.length === 0 ? "ok" : "crit";
  }
  // Nuestro agente: la consulta pausa la negociación hasta que decida el usuario.
  if (e.tipo === "oportunidad") return "serious";
  if (e.tipo === "oportunidad_decidida") return TONO_DECISION[e.datos?.decision] ?? "neutro";
  return "neutro";
}

export function selectorCompra(ctx, { soloConVeredicto = false } = {}) {
  const { estado } = ctx;
  const lista = soloConVeredicto ? ctx.compras.filter((c) => c.veredicto) : ctx.compras;
  return h("div", { class: "filtros" },
    h("label", { class: "campo" }, h("span", { text: "Compra" }),
      h("select", {
        name: "compra",
        disabled: lista.length === 0 ? true : null,
        onchange: (e) => ctx.set({ compraSel: e.target.value || null, seguirUltima: !e.target.value }),
      },
      h("option", { value: "", text: lista.length ? "Seguir la última" : "Sin compras todavía" }),
      lista.map((c) => h("option", {
        value: c.contextoId,
        selected: !estado.seguirUltima && estado.compraSel === c.contextoId ? true : null,
        text: `${c.escenario ?? "?"} · ${c.guardActivo ? "Guard sí" : c.guardActivo === false ? "Guard no" : "Guard ?"} · ${estadoCompra(c).texto} · ${c.contextoId.slice(0, 8)}`,
      })))));
}

export function renderFlujo(raiz, ctx) {
  const { estado } = ctx;
  const compra = ctx.compraActual();
  raiz.replaceChildren(
    h("header", { class: "vista-cab" },
      h("p", { class: "eyebrow", text: estado.fuente === "vivo" ? "En vivo desde el proxy (:8010)" : `Replay · ${estado.replaySel ?? "sin archivo"}` }),
      h("h2", { text: "Flujo de cada compra" }),
      h("div", { class: "filtros" },
        h("button", { class: "boton", type: "button", onclick: () => ctx.descargarJsonl(), disabled: ctx.eventos.length === 0 ? true : null },
          icono("download"), "Guardar eventos (.jsonl)"),
        h("span", { class: "nota", text: `${ctx.eventos.length} eventos · ${ctx.compras.length} compras` }))),
    ctx.eventos.length === 0 ? vacioFlujo(ctx) : h("div", { class: "flujo" },
      h("nav", { class: "compras", "aria-label": "Compras" },
        h("h3", { class: "rotulo", text: "Compras (la más reciente arriba)" }),
        h("ul", {}, ctx.compras.map((c) => {
          const est = estadoCompra(c);
          const activa = compra?.contextoId === c.contextoId;
          return h("li", {}, h("button", {
            type: "button", class: ["compra-item", activa && "activa"], "aria-pressed": activa ? "true" : "false",
            onclick: () => ctx.set({ compraSel: c.contextoId, seguirUltima: false }),
          },
          h("span", { class: "compra-linea" }, h("strong", { text: `Escenario ${c.escenario ?? "?"}` }), chipGuard(c.guardActivo)),
          h("span", { class: "compra-linea" }, chip(est.texto, { tono: TONO_ESTADO[est.clave] ?? "neutro" }),
            h("span", { class: "mono nota", text: hora(c.ultimoTs) }))));
        })),
        estado.seguirUltima ? null : h("button", { class: "boton boton-enlace", type: "button", onclick: () => ctx.set({ seguirUltima: true, compraSel: null }) }, "Seguir la última"),
        ctx.sistema.length ? h("div", { class: "sistema" },
          h("h3", { class: "rotulo", text: "Proxy" }),
          h("ul", {}, ctx.sistema.slice(-8).reverse().map((e) => {
            const d = describirEvento(e);
            return h("li", { class: `t-${tonoEvento(e)}` }, h("span", { class: "mono nota", text: hora(e.ts) }), " ", d.titulo, ": ", d.resumen);
          }))) : null),
      compra ? lineaDeTiempo(compra, ctx) : vacio("Elegí una compra")),
  );
}

function vacioFlujo(ctx) {
  if (ctx.estado.fuente === "vivo") {
    return vacio(ctx.estado.proxy.conectado === false ? "Proxy desconectado: no llegan eventos" : "Esperando eventos del proxy",
      h("p", { text: "Los eventos llegan por SSE desde el proxy del Guard (:8010/guard/events) cuando el Shopping Agent compra a través de él." }),
      h("p", { text: "Sin el flujo levantado: pasá a «Replay (archivo)» arriba." }));
  }
  return vacio("Este replay no tiene eventos",
    h("p", { text: "Guardá un ensayo real en dashboard/demo/*.jsonl (o usá .logs/guard_events.jsonl que escribe el proxy)." }));
}

function lineaDeTiempo(compra, ctx) {
  const est = estadoCompra(compra);
  return h("section", { class: "linea-tiempo", "aria-label": "Eventos de la compra" },
    h("header", { class: "lt-cab" },
      h("h3", {}, `Compra ${compra.contextoId.slice(0, 8)}`),
      chip(`Escenario ${compra.escenario ?? "?"}`),
      chipGuard(compra.guardActivo),
      chip(est.texto, { tono: TONO_ESTADO[est.clave] ?? "neutro" }),
      compra.busqueda ? h("p", { class: "lt-busqueda" }, h("span", { class: "rotulo", text: "Búsqueda" }), h("q", { text: compra.busqueda })) : null),
    panelAprobacion(compra, ctx, { compacto: true }),
    h("ol", { class: "eventos" }, compra.eventos.map((e) => {
      const d = describirEvento(e);
      const tono = tonoEvento(e);
      const abierto = ctx.detallesAbiertos.has(e.id);
      return h("li", { class: ["evento", `t-${tono}`, ctx.esNuevo(e.id) && "nuevo"] },
        h("span", { class: "evento-icono" }, icono(ICONO_EVENTO[e.tipo] ?? "dot")),
        h("div", { class: "evento-cuerpo" },
          h("p", { class: "evento-linea" },
            h("time", { class: "mono", datetime: e.ts, text: hora(e.ts) }),
            h("strong", { text: d.titulo }),
            e.guard_activo ? null : h("span", { class: "nota", text: "(Guard apagado)" }),
            e.contexto_id ? null : h("span", { class: "nota", title: "El proxy la emite antes de conocer el context_id",
              text: "(sin contexto_id: asociada por tiempo)" })),
          d.resumen ? h("p", { class: "evento-resumen", text: d.resumen }) : null,
          h("details", {
            class: "mas", open: abierto ? true : null,
            ontoggle: (ev) => ev.target.open ? ctx.detallesAbiertos.add(e.id) : ctx.detallesAbiertos.delete(e.id),
          }, h("summary", { text: "datos" }), vistaJson(e.datos, { acortar: 40 }))));
    })));
}
