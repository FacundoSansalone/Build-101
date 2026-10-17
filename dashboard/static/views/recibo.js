// Vista 4: recibo de la decisión y verificación de su firma en el navegador.

import { cartDe } from "../lib/eventos.js";
import { STATUS, chip, fechaHora, h, insignia, vacio, vistaJson, vistaJws } from "./comunes.js";
import { bloqueHash, panelVerificacion, tablaEvidencias } from "./piezas.js";
import { selectorCompra } from "./flujo.js";

export function renderRecibo(raiz, ctx) {
  const compra = ctx.compraConVeredicto();
  const recibo = compra?.veredicto?.recibo ?? null;
  raiz.replaceChildren(
    h("header", { class: "vista-cab" },
      h("p", { class: "eyebrow", text: compra ? `Compra ${compra.contextoId.slice(0, 8)} · escenario ${compra.escenario ?? "?"}` : "Recibo" }),
      h("h2", { text: "Recibo de la decisión" }),
      h("p", { class: "vista-bajada", text: "AP2 prueba qué se firmó. El recibo prueba cómo se llegó ahí, y referencia el checkout por hash sin modificarlo." }),
      selectorCompra(ctx, { soloConVeredicto: true })),
    recibo ? cuerpo(compra, recibo, ctx) : vacio("Todavía no hay un recibo",
      h("p", { text: "El recibo viaja dentro del veredicto (Veredicto.recibo) cuando el Guard está prendido." })),
  );
}

function sku(compra, id) {
  return cartDe(compra, id).detalle?.sku ?? null;
}

function chipsCarts(compra, ids, tono = "neutro") {
  return h("ul", { class: "chips" }, (ids || []).map((id) => h("li", {},
    chip(sku(compra, id) ? `${id} · ${sku(compra, id)}` : id, { tono }))));
}

function cuerpo(compra, r, ctx) {
  const filtradas = Object.entries(r.filtradas || {});
  return h("div", { class: "recibo" },
    h("section", { class: "recibo-cab" },
      insignia(STATUS, r.status, { grande: true }),
      h("dl", { class: "campos campos-anchos" },
        [["id", r.id], ["emitido", fechaHora(r.emitido)], ["elegido", sku(compra, r.elegido) ? `${r.elegido} · ${sku(compra, r.elegido)}` : r.elegido],
          ["criterio", r.ficha?.criterio ?? "—"]]
          .map(([k, v]) => [h("dt", { text: k }), h("dd", { class: "mono", text: v ?? "—" })]))),
    h("div", { class: "recibo-grilla" },
      h("section", { class: "bloque" },
        h("h3", { text: "Consideradas" }), chipsCarts(compra, r.consideradas),
        h("h3", { text: "Filtradas y por qué" }),
        filtradas.length ? h("ul", { class: "filtradas" }, filtradas.map(([id, motivos]) => h("li", {},
          h("strong", { class: "mono", text: sku(compra, id) ? `${id} · ${sku(compra, id)}` : id }),
          h("ul", {}, (motivos || []).map((m) => h("li", { text: m })))))) : h("p", { class: "nota", text: "Ninguna." }),
        h("h3", { text: "Ranking" }), h("ol", { class: "ranking-corto" }, (r.ranking || []).map((id) => h("li", { class: "mono", text: sku(compra, id) ? `${id} · ${sku(compra, id)}` : id }))),
        h("h3", { text: "Por qué ganó" }), h("p", { class: "por-que", text: r.por_que_gano }),
        h("h3", { text: "Sin verificar" }),
        h("p", { class: "nota", text: "No se puede verificar antes de comprar; el Guard lo declara en lugar de aprobarlo en silencio." }),
        h("ul", { class: "chips" }, (r.sin_verificar || []).map((s) => h("li", {}, chip(s, { tono: "warn", icon: "alert" }))))),
      h("section", { class: "bloque" },
        h("h3", { text: "Verificaciones contra el catálogo público" }),
        tablaEvidencias(r.verificaciones),
        h("h3", { text: "Referencia al checkout firmado" }),
        r.checkout_hash ? h("p", { class: "hash" }, h("span", { class: "hash-nombre", text: "checkout_hash" }), h("code", { class: "hash-valor", text: r.checkout_hash }),
          compra.checkout?.checkout_hash ? chip(compra.checkout.checkout_hash === r.checkout_hash ? "= el del ap2.checkout de esta compra" : "≠ el del ap2.checkout de esta compra",
            { tono: compra.checkout.checkout_hash === r.checkout_hash ? "ok" : "crit", icon: compra.checkout.checkout_hash === r.checkout_hash ? "check" : "x" }) : null)
          : h("p", { class: "nota", text: "El recibo no trae checkout_hash." }),
        bloqueHash(compra, ctx),
        h("h3", { text: "Firma del Guard" }),
        h("dl", { class: "campos" }, h("dt", { text: "kid" }), h("dd", { class: "mono", text: r.kid ?? "—" })),
        r.firma ? vistaJws(r.firma, { etiquetaFirma: "firma ES256 del Guard" }) : null,
        panelVerificacion(r, ctx),
        h("details", { class: "mas" }, h("summary", { text: "recibo completo (JSON)" }), vistaJson(r, { acortar: 40 })))),
  );
}
