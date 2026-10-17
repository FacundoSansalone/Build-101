// Vista 1: el momento clave, escenario B lado a lado (DEMO.md).

import { estadoCompra, mandatosDe, parLadoALado } from "../lib/eventos.js";
import {
  chipGuard, dinero, h, icono, leyendaTextoLibre, tarjeta, textoLibre, vacio, vistaJson, vistaJws,
} from "./comunes.js";
import {
  bannerStatus, bloqueAp2, bloqueCheckout, bloqueMandatos, datosCompra, lineaCompra,
  lineaRanking, listaChequeos, panelVerificacion, resumenFicha, resumenProducto, tablaEvidencias,
} from "./piezas.js";

const FRASE_1 = "Todas las firmas son válidas y AP2 aprobó todo.";
const FRASE_2 = "Y aun así el usuario compró algo que no quería.";

export function renderMomento(raiz, ctx) {
  const { estado } = ctx;
  const esc = estado.escenarioMomento;
  const delEscenario = ctx.compras.filter((c) => c.escenario === esc);
  const par = parLadoALado(ctx.compras, esc);
  const buscar = (id) => delEscenario.find((c) => c.contextoId === id) ?? null;
  const sin = (estado.momentoSin && buscar(estado.momentoSin)) || par.sin;
  const con = (estado.momentoCon && buscar(estado.momentoCon)) || par.con;
  const fixture = ctx.fixture(esc);

  const escenarios = [...new Set([...ctx.fixtures.map((f) => f.escenario), ...ctx.compras.map((c) => c.escenario).filter(Boolean)])];
  const selectorCompra = (activo, actual, clave) => {
    const opciones = delEscenario.filter((c) => c.guardActivo === activo);
    return h("label", { class: "campo" },
      h("span", { text: activo ? "Compra con Guard" : "Compra sin Guard" }),
      h("select", {
        disabled: opciones.length === 0 ? true : null,
        onchange: (e) => ctx.set({ [clave]: e.target.value || null }),
      },
      h("option", { value: "", text: opciones.length ? "La más reciente" : "Ninguna todavía" }),
      opciones.map((c) => h("option", {
        value: c.contextoId, selected: actual?.contextoId === c.contextoId && estado[clave] ? true : null,
        text: `${new Date(c.ultimoTs).toLocaleTimeString("es-AR", { hour12: false })} · ${c.contextoId.slice(0, 8)} · ${estadoCompra(c).texto}`,
      }))));
  };

  raiz.replaceChildren(
    h("header", { class: "vista-cab momento-cab" },
      h("p", { class: "eyebrow", text: `Escenario ${esc}${fixture?.title ? ` · ${fixture.title}` : ""}` }),
      h("h2", { class: "frase" }, FRASE_1, " ", h("em", { text: FRASE_2 })),
      h("div", { class: "filtros" },
        h("label", { class: "campo" }, h("span", { text: "Escenario" }),
          h("select", { onchange: (e) => ctx.set({ escenarioMomento: e.target.value, momentoSin: null, momentoCon: null }) },
            escenarios.map((s) => h("option", { value: s, selected: s === esc ? true : null, text: s })))),
        selectorCompra(false, sin, "momentoSin"),
        selectorCompra(true, con, "momentoCon"))),
    h("div", { class: "lado-a-lado" },
      h("section", { class: "columna col-sin", "aria-label": "Sin Guard" }, columnaSin(sin, ctx, fixture)),
      h("section", { class: "columna col-con", "aria-label": "Con Guard" }, columnaCon(con, ctx, fixture))),
  );
}

function cabColumna(titulo, activo, compra) {
  return h("header", { class: "col-cab" },
    h("h3", { text: titulo }),
    chipGuard(activo),
    compra ? h("span", { class: "col-ctx mono", title: compra.contextoId, text: `compra ${compra.contextoId.slice(0, 8)}` }) : null);
}

function lineaFirma(tono, iconoNombre, texto) {
  return h("div", { class: ["linea-firma", `t-${tono}`], role: "separator", "aria-label": `Firma del usuario: ${texto}` },
    h("span", { class: "linea-firma-rotulo" }, icono("pen"), "Firma del usuario · CheckoutMandate + PaymentMandate"),
    h("span", { class: "linea-firma-estado" }, icono(iconoNombre), texto));
}

function sinCompra(activo, ctx, fixture) {
  const instruccion = ctx.estado.fuente === "vivo"
    ? `En vivo: ${activo ? "prendé" : "apagá"} el Guard con el interruptor de arriba y hacé la compra del escenario ${ctx.estado.escenarioMomento}.`
    : "Elegí otro archivo de replay o pasá a «En vivo (proxy)».";
  const cart = fixture?.carts?.find((c) => c.cart_id === fixture?.expected?.attack_target_cart_id);
  const producto = (ctx.catalogo?.products || []).find((p) => p.sku === fixture?.attack?.target_sku);
  return h("div", { class: "col-vacia" },
    vacio(`Todavía no hay una compra del escenario ${ctx.estado.escenarioMomento} con el Guard ${activo ? "prendido" : "apagado"}.`,
      h("p", { text: instruccion })),
    cart ? tarjeta({ titulo: "Lo que manda el comercio en este escenario", subtitulo: `Fixture exportado por world/ (fixtures/scenario_${fixture.scenario}.json): no es una compra en vivo`, clase: "tarjeta-fixture" },
      vistaJson(cart, { resaltar: { item_label: (t) => textoLibre(t, producto ? [producto.title, producto.description] : []) } }),
      leyendaTextoLibre()) : null);
}

function columnaSin(compra, ctx, fixture) {
  if (!compra) return [cabColumna("Sin Guard", false, null), sinCompra(false, ctx, fixture)];
  const d = datosCompra(compra, ctx);
  const estado = estadoCompra(compra);
  const mandatos = mandatosDe(compra);
  const firmo = estado.clave === "firmado";
  return [
    cabColumna("Sin Guard", false, compra),
    tarjeta({ paso: "1", titulo: "Respuesta del comercio", subtitulo: "ap2.cart que eligió el agente · los carts no se firman" },
      d.cart ? vistaJson(d.cart, { resaltar: { item_label: (t) => textoLibre(t, d.textosPublicos) } })
        : vacio("No se vio el ap2.cart elegido"),
      leyendaTextoLibre({ conExtra: d.textosPublicos.length > 0 }),
      d.detalle ? h("p", { class: "nota" }, "El agente lee ese texto y recomienda ", h("strong", { text: d.detalle.sku }), ".") : null),
    tarjeta({ paso: "2", titulo: "Checkout firmado por el comercio", subtitulo: "ap2.checkout → checkout_jwt" },
      bloqueCheckout(compra, d, ctx, { textosPublicos: d.textosPublicos }),
      h("p", { class: "nota", text: "El item_label viaja como título dentro del checkout firmado." })),
    tarjeta({ paso: "3", titulo: "Verificación de AP2", subtitulo: "Restricciones del mandato" }, bloqueAp2(compra, d)),
    tarjeta({ paso: "4", titulo: "Sin Guard", subtitulo: "Nadie mira la decisión", clase: "tarjeta-apagada" },
      h("p", { text: "Nadie compara la elección con la ficha del pedido ni con el catálogo público del comercio." })),
    firmo ? lineaFirma("ok", "check", "firmó")
      : estado.clave === "para_firmar" ? lineaFirma("neutro", "arrow", "se le presentó para firmar")
        : lineaFirma("neutro", "dash", estado.texto.toLowerCase()),
    tarjeta({ paso: "5", titulo: firmo ? "El usuario firma y paga" : "Llega a la firma", subtitulo: "Mandatos firmados por el usuario (SD-JWT)" },
      mandatos.length ? bloqueMandatos(mandatos, compra, ctx)
        : vacio("El proxy no registró mandatos en esta compra",
          "El checkout firmado llegó intacto al agente: lo siguiente es la firma del usuario."),
      lineaCompra(resumenFicha(d.ficha), resumenProducto(d.producto, d.monto, d.moneda)),
      d.producto ? h("p", { class: "nota", text: "Producto según el catálogo público del comercio (:8011)." }) : null),
  ];
}

function columnaCon(compra, ctx, fixture) {
  if (!compra) return [cabColumna("Con Guard", true, null), sinCompra(true, ctx, fixture)];
  const d = datosCompra(compra, ctx);
  const v = compra.veredicto;
  const recibo = v?.recibo ?? null;
  const bloqueado = Boolean(compra.bloqueo);
  return [
    cabColumna("Con Guard", true, compra),
    tarjeta({ paso: "1", titulo: "Respuesta del comercio", subtitulo: "La misma respuesta llega al proxy" },
      d.cart?.item_label ? h("div", { class: "descartado" },
        h("p", { class: "rotulo" }, icono("x"), "Texto libre descartado: no llega a ningún chequeo"),
        h("p", { class: "descartado-texto" }, h("s", {}, textoLibre(d.cart.item_label)))) : null,
      d.detalle ? h("div", {},
        h("p", { class: "rotulo", text: "Campos tipados que sí se usan (declarados por el comercio)" }),
        h("dl", { class: "campos" },
          [["sku", d.detalle.sku], ["marca", d.detalle.brand], ["categoría", d.detalle.category], ["uso", d.detalle.use],
            ["precio (ap2.cart)", dinero(d.cart?.amount, d.cart?.currency)], ["talles", (d.detalle.sizes || []).join(", ")]]
            .map(([k, val]) => [h("dt", { text: k }), h("dd", { class: "mono", text: val ?? "—" })]))) : vacio("No se vieron las ofertas")),
    tarjeta({ paso: "2", titulo: "Checkout firmado por el comercio", subtitulo: "Intacto: el Guard no toca objetos firmados" },
      bloqueCheckout(compra, d, ctx, { completo: false }),
      h("p", { class: "nota", text: "Lo referencia por su hash en el recibo." })),
    tarjeta({ paso: "3", titulo: "Verificación de AP2", subtitulo: "Restricciones del mandato" }, bloqueAp2(compra, d)),
    tarjeta({ paso: "4", titulo: "Pre-Sign Guard", subtitulo: "Decide con datos verificados, no con el texto libre" },
      v ? [
        bannerStatus(v),
        h("p", { class: "rotulo", text: "Verificador contra el catálogo público (:8011)" }),
        tablaEvidencias(recibo?.verificaciones),
        h("p", { class: "rotulo", text: "Chequeos" }),
        listaChequeos(v.chequeos),
        lineaRanking(compra, v, ctx),
      ] : vacio("Esta compra no tiene veredicto del Guard")),
    bloqueado ? lineaFirma("crit", "block", "bloqueado antes de la firma")
      : lineaFirma("neutro", "arrow", estadoCompra(compra).texto.toLowerCase()),
    tarjeta({
      paso: "5",
      titulo: bloqueado ? "No se presenta nada para firmar" : "Recibo del Guard",
      subtitulo: "Recibo de la decisión, firmado por el Guard",
      clase: bloqueado ? "tarjeta-bloqueo" : "",
    },
    bloqueado ? h("p", { class: "motivo-bloqueo" }, icono("block"), compra.bloqueo.motivo ?? "") : null,
    recibo ? [
      h("dl", { class: "campos" },
        [["recibo", recibo.id], ["status", recibo.status], ["elegido", recibo.elegido], ["kid", recibo.kid ?? "—"]]
          .map(([k, val]) => [h("dt", { text: k }), h("dd", { class: "mono", text: val ?? "—" })])),
      recibo.firma ? vistaJws(recibo.firma, { etiquetaFirma: "firma ES256 del Guard", payloadMax: 10 }) : null,
      panelVerificacion(recibo, ctx, { compacto: true }),
      h("button", { class: "boton boton-enlace", type: "button", onclick: () => ctx.irARecibo(compra) }, "Ver el recibo completo", icono("arrow")),
    ] : vacio("La compra no trae recibo")),
  ];
}

