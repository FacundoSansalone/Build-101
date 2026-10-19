// Vista 5: la ficha del pedido. La que usó el Guard (el recibo) o, antes del
// veredicto, la que va a usar: la que armó nuestro agente con el usuario o,
// solo si el proxy corre con PROXY_FICHA_DEMO=1, la del escenario de la demo
// (lib/eventos.js → fichaVigente). Con PROXY_FICHA_DEMO=0, el default, la
// del escenario se ve solo si se elige a mano, rotulada como fixture.

import { describirRequisito } from "../lib/agente.js";
import { chip, h, icono, vacio } from "./comunes.js";

const CRITERIOS = {
  lowest_price_among_compliant: "El más barato entre los que cumplen: obligatorios cumplidos → preferidos cumplidos → menor precio.",
};

function textoFuente(vigente, compra, esc, replay) {
  switch (vigente.fuente) {
    case "recibo": return `Recibo de la compra ${compra.contextoId.slice(0, 8)} (la ficha que usó el Guard)`;
    case "intake": return "La que armó nuestro agente con el usuario (GET /guard/compras): con esa verifica el Guard";
    case "escenario":
      return replay ? `fixtures/scenario_${esc}.json → intent (replay de una grabación de ese escenario)`
        : `fixtures/scenario_${esc}.json → intent: el proxy corre con PROXY_FICHA_DEMO=1 y manda esta`;
    default: return "";
  }
}

function opcionVigente(vigente) {
  switch (vigente.fuente) {
    case "recibo": return "La del último recibo";
    case "intake": return "La que armó nuestro agente";
    case "escenario": return "La del escenario de la compra";
    default: return "La de la compra (todavía no hay)";
  }
}

function chipFicha(ficha, elegida, fichaDemo, replay) {
  if (elegida) {
    const uso = replay ? "" : fichaDemo ? ": el proxy la usa" : ": el proxy no la usa";
    return chip(`Fixture del escenario${uso}`, { tono: "neutro", icon: "doc" });
  }
  return ficha.confirmada
    ? chip("Confirmada por el usuario", { tono: "ok", icon: "check" })
    : chip("Sin confirmar", { tono: "warn", icon: "alert" });
}

export function renderFicha(raiz, ctx) {
  const { estado } = ctx;
  const compra = ctx.compraActual();
  const vigente = ctx.fichaVigente(compra);
  const fichaDemo = estado.proxy.ficha_demo === true;
  const replay = estado.fuente === "replay";
  // Un fixture elegido a mano se muestra como fixture: con PROXY_FICHA_DEMO=0
  // no es la ficha de ninguna compra.
  const elegida = estado.escenarioFicha;
  const ficha = elegida ? ctx.ficha(elegida) : vigente.ficha;
  const fuente = elegida
    ? `fixtures/scenario_${elegida}.json → intent, armada con Ficha.desde_intent()${fichaDemo || replay ? "" : " (PROXY_FICHA_DEMO=0: manda la que confirma el usuario)"}`
    : textoFuente(vigente, compra, compra?.escenario, replay);

  raiz.replaceChildren(
    h("header", { class: "vista-cab" },
      h("p", { class: "eyebrow", text: "Ficha del pedido" }),
      h("h2", { text: "Lo que pidió el usuario, en campos que se pueden chequear" }),
      h("div", { class: "filtros" },
        h("label", { class: "campo" }, h("span", { text: "Fuente" }),
          h("select", { name: "ficha-fuente", onchange: (e) => ctx.set({ escenarioFicha: e.target.value || null }) },
            h("option", { value: "", text: opcionVigente(vigente) }),
            ctx.fixtures.map((f) => h("option", { value: f.escenario, selected: elegida === f.escenario ? true : null, text: `Fixture · escenario ${f.escenario}` })))))),
    ficha ? h("div", { class: "ficha" },
      h("div", { class: "ficha-cab" },
        chipFicha(ficha, elegida, fichaDemo, replay),
        h("p", { class: "nota", text: `Fuente: ${fuente}` })),
      ficha.pedido_original ? h("blockquote", { class: "pedido" }, h("p", { text: ficha.pedido_original })) : null,
      h("table", { class: "tabla tabla-ficha" },
        h("thead", {}, h("tr", {}, ["Campo", "Condición", "Tipo"].map((t) => h("th", { scope: "col", text: t })))),
        h("tbody", {}, (ficha.requisitos || []).map((q) => {
          const d = describirRequisito(q);
          return h("tr", {},
            h("td", { text: d.campo }),
            h("td", { class: "mono", text: d.condicion }),
            h("td", {}, d.obligatorio ? chip("Obligatorio", { tono: "neutro", icon: "lock" }) : chip("Preferido", { tono: "neutro" })));
        }))),
      ficha.categoria_taxonomia
        ? h("p", { class: "nota", text: `Categoría (taxonomía de Google): ${ficha.categoria_taxonomia}` }) : null,
      h("p", { class: "criterio" }, icono("list"), h("strong", { text: "Criterio: " }), CRITERIOS[ficha.criterio] ?? ficha.criterio),
      h("p", { class: "nota", text: "Un requisito que no está escrito no se puede chequear. Los modelos entienden y proponen; el usuario autoriza la ficha y el Guard la verifica con código determinístico." }))
      : vacio(elegida ? "No hay ficha para ese escenario" : "Todavía no hay ficha para mostrar",
        h("p", { text: elegida
          ? `Falta fixtures/scenario_${elegida}.json o no tiene intent.`
          : "La arma nuestro agente con el usuario (vista Agente) y, después del checkout, queda en el recibo. Para ver un escenario de prueba, elegilo en «Fuente»." })),
  );
}
