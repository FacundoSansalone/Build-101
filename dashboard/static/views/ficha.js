// Vista 5: la ficha del pedido, confirmada por el usuario.

import { chip, h, icono, vacio } from "./comunes.js";

const CAMPOS = {
  precio: "Precio", moneda: "Moneda", categoria: "Categoría", uso: "Uso",
  talle: "Talle", marca: "Marca", comercio: "Comercio",
};

const CRITERIOS = {
  lowest_price_among_compliant: "El más barato entre los que cumplen: obligatorios cumplidos → preferidos cumplidos → menor precio.",
};

function condicion(q) {
  if (q.operador === "maximo") return `≤ ${q.valor}`;
  if (q.operador === "incluye") return `incluye ${q.valor}`;
  return `= ${q.valor}`;
}

export function renderFicha(raiz, ctx) {
  const { estado } = ctx;
  const compra = ctx.compraConVeredicto();
  const desdeRecibo = compra?.veredicto?.recibo?.ficha ?? null;
  const esc = estado.escenarioFicha ?? compra?.escenario ?? estado.escenarioMomento;
  const ficha = estado.escenarioFicha ? ctx.ficha(esc) : (desdeRecibo ?? ctx.ficha(esc));
  const fuente = !estado.escenarioFicha && desdeRecibo
    ? `Recibo de la compra ${compra.contextoId.slice(0, 8)} (la ficha que usó el Guard)`
    : `fixtures/scenario_${esc}.json → intent, armada con Ficha.desde_intent()`;

  raiz.replaceChildren(
    h("header", { class: "vista-cab" },
      h("p", { class: "eyebrow", text: "Ficha del pedido" }),
      h("h2", { text: "Lo que pidió el usuario, en campos que se pueden chequear" }),
      h("div", { class: "filtros" },
        h("label", { class: "campo" }, h("span", { text: "Fuente" }),
          h("select", { onchange: (e) => ctx.set({ escenarioFicha: e.target.value || null }) },
            h("option", { value: "", text: desdeRecibo ? "La del último recibo" : `Escenario ${esc}` }),
            ctx.fixtures.map((f) => h("option", { value: f.escenario, selected: estado.escenarioFicha === f.escenario ? true : null, text: `Fixture · escenario ${f.escenario}` })))))),
    ficha ? h("div", { class: "ficha" },
      h("div", { class: "ficha-cab" },
        ficha.confirmada ? chip("Confirmada por el usuario", { tono: "ok", icon: "check" }) : chip("Sin confirmar", { tono: "warn", icon: "alert" }),
        h("p", { class: "nota", text: `Fuente: ${fuente}` })),
      h("blockquote", { class: "pedido" }, h("p", { text: ficha.pedido_original })),
      h("table", { class: "tabla tabla-ficha" },
        h("thead", {}, h("tr", {}, ["Campo", "Condición", "Tipo"].map((t) => h("th", { scope: "col", text: t })))),
        h("tbody", {}, (ficha.requisitos || []).map((q) => h("tr", {},
          h("td", { text: CAMPOS[q.campo] ?? q.campo }),
          h("td", { class: "mono", text: condicion(q) }),
          h("td", {}, q.obligatorio ? chip("Obligatorio", { tono: "neutro", icon: "lock" }) : chip("Preferido", { tono: "neutro" })))))),
      h("p", { class: "criterio" }, icono("list"), h("strong", { text: "Criterio: " }), CRITERIOS[ficha.criterio] ?? ficha.criterio),
      h("p", { class: "nota", text: "Un requisito que no está escrito no se puede chequear. El LLM solo arma esta ficha; las decisiones del Guard son código determinístico." }))
      : vacio("No hay ficha para mostrar", h("p", { text: "Falta fixtures/scenario_*.json o la compra no trae recibo." })),
  );
}
