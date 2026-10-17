// Vista 3: veredicto del Guard con sus chequeos agrupados por tipo.

import { cartDe } from "../lib/eventos.js";
import { RESULTADO, chip, chipGuard, dinero, h, icono, insignia, vacio } from "./comunes.js";
import { TIPOS_CHEQUEO, bannerStatus, tablaChequeos } from "./piezas.js";
import { selectorCompra } from "./flujo.js";

export function renderVeredicto(raiz, ctx) {
  const compra = ctx.compraConVeredicto();
  const v = compra?.veredicto ?? null;
  raiz.replaceChildren(
    h("header", { class: "vista-cab" },
      h("p", { class: "eyebrow", text: compra ? `Compra ${compra.contextoId.slice(0, 8)} · escenario ${compra.escenario ?? "?"}` : "Veredicto" }),
      h("h2", { text: "Veredicto del Guard" }),
      selectorCompra(ctx, { soloConVeredicto: true })),
    v ? cuerpo(compra, v, ctx) : vacio("Todavía no hay un veredicto",
      h("p", { text: "El Guard emite un veredicto cuando el agente pide el checkout con el Guard prendido." })),
  );
}

function cuerpo(compra, v, ctx) {
  const porTipo = new Map(TIPOS_CHEQUEO.map(([t]) => [t, []]));
  for (const c of v.chequeos || []) {
    if (!porTipo.has(c.tipo)) porTipo.set(c.tipo, []);
    porTipo.get(c.tipo).push(c);
  }
  const nombres = Object.fromEntries(TIPOS_CHEQUEO);
  return h("div", { class: "veredicto" },
    h("div", { class: "veredicto-cab" },
      bannerStatus(v, { grande: true }),
      h("div", { class: "veredicto-meta" }, chipGuard(compra.guardActivo),
        compra.bloqueo ? chip("Bloqueado antes de la firma", { tono: "crit", icon: "block" }) : null)),
    h("div", { class: "veredicto-grilla" },
      h("section", { class: "grupos", "aria-label": "Chequeos por tipo" },
        [...porTipo.entries()].map(([tipo, lista]) => h("section", { class: "grupo" },
          h("header", { class: "grupo-cab" },
            h("h3", { text: nombres[tipo] ?? tipo }),
            resumenGrupo(lista)),
          lista.length ? tablaChequeos(lista) : h("p", { class: "nota", text: "Sin chequeos de este tipo en este veredicto." })))),
      ranking(compra, v)),
  );
}

function resumenGrupo(lista) {
  if (!lista.length) return h("span", { class: "nota", text: "—" });
  const cuenta = {};
  for (const c of lista) cuenta[c.resultado] = (cuenta[c.resultado] ?? 0) + 1;
  return h("span", { class: "grupo-resumen" },
    ["bloquea", "escalar", "aviso", "ok"].filter((r) => cuenta[r]).map((r) =>
      h("span", { class: "grupo-cuenta" }, insignia(RESULTADO, r), h("span", { class: "mono", text: `×${cuenta[r]}` }))));
}

function ranking(compra, v) {
  const elegido = v.cart_id_elegido;
  const recomendado = v.cart_id_recomendado ?? v.ranking?.[0] ?? null;
  return h("section", { class: "ranking", "aria-label": "Ranking propio contra elegido" },
    h("h3", { text: "Ranking propio vs. elegido" }),
    h("p", { class: "nota", text: "El Guard rehace el ranking con datos estructurados y el criterio de la ficha: obligatorios → preferidos → precio." }),
    v.ranking?.length ? h("ol", { class: "ranking-lista" }, v.ranking.map((id) => {
      const { cart, detalle } = cartDe(compra, id);
      const marcas = [];
      if (id === recomendado) marcas.push(chip("recomendado", { tono: "ok", icon: "check" }));
      if (id === elegido) marcas.push(chip("elegido por el agente", { tono: id === recomendado ? "ok" : "crit", icon: "arrow" }));
      return h("li", { class: ["ranking-item", id === elegido && "es-elegido", id === recomendado && "es-recomendado"] },
        h("span", { class: "ranking-id mono", text: id }),
        h("span", { class: "ranking-producto" },
          h("strong", { text: detalle?.sku ?? "—" }), " ", detalle?.title ?? "",
          h("span", { class: "nota mono", text: ` ${dinero(cart?.amount, cart?.currency)}` })),
        h("span", { class: "ranking-marcas" }, marcas));
    })) : vacio("El veredicto no trae ranking"),
    v.ranking?.length && !v.ranking.includes(elegido)
      ? h("p", { class: "nota t-crit" }, icono("alert"), `El elegido (${elegido}) no está en el ranking propio.`) : null);
}
