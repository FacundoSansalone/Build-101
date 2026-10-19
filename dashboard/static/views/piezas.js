// Bloques que usan varias vistas: AP2, checkout, chequeos, recibo, mandatos.

import { decodificarJwt } from "../lib/jose.js";
import {
  cartDe, cartIdElegido, chequeosCheckoutFirmado, cuerpoAprobacion, montoCheckout, monedaCheckout,
  nombreMandato, topeDeFicha,
} from "../lib/eventos.js";
import {
  EVIDENCIA, RESULTADO, STATUS, chip, dinero, filaDato, h, icono, insignia, textoLibre,
  vacio, valorTexto, vistaJson, vistaJws,
} from "./comunes.js";

export const TIPOS_CHEQUEO = [
  ["cumplimiento", "Cumplimiento"],
  ["evidencia", "Evidencia"],
  ["integridad", "Integridad de la elección"],
  ["politica", "Política"],
  ["mandato", "Mandato"],
];

const GRAVEDAD = { bloquea: 0, escalar: 1, aviso: 2, ok: 3 };

/** Todo lo que una vista necesita saber de una compra, ya resuelto. */
export function datosCompra(compra, ctx) {
  const elegidoId = cartIdElegido(compra);
  const { cart, detalle } = cartDe(compra, elegidoId);
  const producto = (ctx.catalogo?.products || []).find((p) => p.sku === detalle?.sku) ?? null;
  // La del recibo o la que usa el proxy para esa compra: la del escenario
  // solo con PROXY_FICHA_DEMO=1 (o en un replay); si no, la de nuestro agente.
  const ficha = compra ? ctx.fichaVigente(compra).ficha : null;
  let jwt = null;
  if (compra?.checkout) {
    const c = compra.checkout;
    if (c.jwt_header && c.jwt_payload) jwt = { header: c.jwt_header, payload: c.jwt_payload };
    else if (c.checkout_jwt) { try { jwt = decodificarJwt(c.checkout_jwt); } catch { jwt = null; } }
  }
  return {
    elegidoId, cart, detalle, producto, ficha, jwt,
    textosPublicos: producto ? [producto.title, producto.description].filter(Boolean) : [],
    tope: topeDeFicha(ficha),
    monto: montoCheckout(compra?.checkout) ?? cart?.amount ?? null,
    moneda: monedaCheckout(compra?.checkout) ?? cart?.currency ?? "USD",
  };
}

export function etiquetaCart(compra, cartId) {
  const { cart, detalle } = cartDe(compra, cartId);
  if (!cartId) return "—";
  const partes = [cartId];
  if (detalle?.sku) partes.push(detalle.sku);
  if (typeof cart?.amount === "number") partes.push(dinero(cart.amount, cart.currency));
  return partes.join(" · ");
}

// ---------------------------------------------------------------------------
// AP2
// ---------------------------------------------------------------------------

export function bloqueAp2(compra, d) {
  const v = compra?.checkout?.ap2_violaciones;
  if (!Array.isArray(v)) {
    return vacio("Sin dato de AP2 en esta compra",
      "El evento checkout_recibido no trae ap2_violaciones.");
  }
  const ok = v.length === 0;
  const comparacion = typeof d.monto === "number" && typeof d.tope === "number"
    ? `${dinero(d.monto, d.moneda)} ${d.monto <= d.tope ? "≤" : ">"} ${dinero(d.tope, d.moneda)}`
    : "monto y tope sin dato";
  return h("div", { class: ["ap2", ok ? "t-ok" : "t-crit"] },
    h("p", { class: "ap2-resultado" },
      icono(ok ? "check" : "block"),
      h("strong", { text: ok ? "AP2: OK" : "AP2 frena" }),
      h("span", { class: "ap2-tope", text: `Tope del mandato: ${comparacion}` })),
    h("p", { class: "ap2-detalle" },
      "Evaluador de restricciones del SDK de AP2 (AmountRange) · ",
      h("code", { text: `ap2_violaciones: ${JSON.stringify(v)}` })));
}

// ---------------------------------------------------------------------------
// Checkout firmado por el comercio
// ---------------------------------------------------------------------------

function extractoPayload(p) {
  if (!p || typeof p !== "object") return p;
  const li = Array.isArray(p.line_items) ? p.line_items : [];
  return {
    id: p.id,
    merchant: p.merchant ? { id: p.merchant.id, name: p.merchant.name } : undefined,
    line_items: li.map((x) => ({ item: x.item, quantity: x.quantity })),
    currency: p.currency,
    totals: Array.isArray(p.totals) ? p.totals.filter((t) => t.type === "total") : undefined,
    iat: p.iat,
    exp: p.exp,
  };
}

export function bloqueHash(compra, ctx) {
  const c = compra?.checkout;
  if (!c?.checkout_hash) return null;
  const ok = c.checkout_jwt ? ctx.hashCoincide(c.checkout_jwt, c.checkout_hash) : null;
  return h("p", { class: "hash" },
    h("span", { class: "hash-nombre", text: "checkout_hash" }),
    h("code", { class: "hash-valor", text: c.checkout_hash }),
    ok === true ? chip("= SHA-256 del JWT, calculado acá", { tono: "ok", icon: "check" })
      : ok === false ? chip("no coincide con el JWT", { tono: "crit", icon: "x" }) : null);
}

export function bloqueCheckout(compra, d, ctx, { completo = true, textosPublicos = [] } = {}) {
  const c = compra?.checkout;
  if (!c) return vacio("Todavía no llegó el checkout firmado", "Se ve cuando el agente pide create_checkout.");
  const header = d.jwt?.header ?? {};
  const firma = `firma ${header.alg ?? "?"} del comercio · kid ${header.kid ?? "?"}`;
  const resaltar = { "line_items.0.item.title": (t) => textoLibre(t, textosPublicos) };
  if (!completo) {
    return h("div", { class: "checkout" },
      c.checkout_jwt ? vistaJws(c.checkout_jwt, { etiquetaFirma: firma, payloadMax: 10 }) : null,
      bloqueHash(compra, ctx));
  }
  return h("div", { class: "checkout" },
    c.checkout_jwt ? vistaJws(c.checkout_jwt, { etiquetaFirma: firma }) : null,
    h("div", { class: "jwt-partes" },
      h("div", {}, h("p", { class: "rotulo", text: "header" }), vistaJson(header)),
      h("div", {}, h("p", { class: "rotulo", text: "payload (extracto)" }),
        vistaJson(extractoPayload(d.jwt?.payload), { resaltar }))),
    d.jwt?.payload ? h("details", { class: "mas" },
      h("summary", { text: "payload completo firmado" }),
      vistaJson(d.jwt.payload, { resaltar })) : null,
    bloqueHash(compra, ctx));
}

// ---------------------------------------------------------------------------
// Veredicto y chequeos
// ---------------------------------------------------------------------------

export function bannerStatus(veredicto, { grande = false } = {}) {
  const s = STATUS[veredicto?.status] ?? { tono: "neutro" };
  return h("div", { class: ["banner", `t-${s.tono}`, grande && "banner-grande"] },
    insignia(STATUS, veredicto?.status, { grande: true }),
    h("p", { class: "banner-motivo", text: veredicto?.motivo ?? "" }));
}

export function ordenarChequeos(chequeos) {
  return [...(chequeos || [])].sort((a, b) => (GRAVEDAD[a.resultado] ?? 9) - (GRAVEDAD[b.resultado] ?? 9));
}

/** Lista compacta: los que no dieron OK, y los OK plegados. */
export function listaChequeos(chequeos) {
  const todos = ordenarChequeos(chequeos);
  const malos = todos.filter((c) => c.resultado !== "ok");
  const buenos = todos.filter((c) => c.resultado === "ok");
  const item = (c) => h("li", { class: "chequeo" },
    insignia(RESULTADO, c.resultado),
    h("span", { class: "chequeo-tipo", text: c.tipo }),
    h("span", { class: "chequeo-detalle", text: c.detalle }));
  return h("div", { class: "chequeos" },
    malos.length ? h("ul", { class: "lista-chequeos" }, malos.map(item)) : null,
    buenos.length ? h("details", { class: "mas" },
      h("summary", { text: `${buenos.length} ${buenos.length === 1 ? "chequeo" : "chequeos"} OK` }),
      h("ul", { class: "lista-chequeos" }, buenos.map(item))) : null);
}

export function tablaChequeos(chequeos) {
  return h("div", { class: "tabla-scroll" }, h("table", { class: "tabla" },
    h("thead", {}, h("tr", {},
      ["Resultado", "Campo", "Esperado", "Observado", "Origen", "Detalle"].map((t) => h("th", { scope: "col", text: t })))),
    h("tbody", {}, ordenarChequeos(chequeos).map((c) => h("tr", {},
      h("td", {}, insignia(RESULTADO, c.resultado)),
      h("td", { class: "mono", text: c.campo ?? "—" }),
      h("td", { class: "mono valor", text: valorTexto(c.esperado) }),
      h("td", { class: "mono valor", text: valorTexto(c.observado) }),
      h("td", { class: "mono", text: c.origen ?? "—" }),
      h("td", { text: c.detalle }),
    )))));
}

/** Chips con la fuente del catálogo que usó el Guard y si el recibo quedó firmado. */
export function chipsFuente(compra) {
  const chips = [];
  if (compra?.fuenteCatalogo === "http") {
    chips.push(chip("catálogo público en vivo (:8011)", { tono: "ok", icon: "check", titulo: "El Guard verificó contra GET :8011/products" }));
  } else if (compra?.fuenteCatalogo === "fixture") {
    chips.push(chip("catálogo: copia local (fixture)", { tono: "warn", icon: "alert", titulo: ":8011 no respondió: el proxy usó fixtures/public_catalog.json (modo demo)" }));
  } else if (compra?.fuenteCatalogo === "ninguno") {
    chips.push(chip("sin catálogo público: solo datos declarados", {
      tono: "warn", icon: "alert",
      titulo: compra.catalogoDescartado ?? "El comercio no publica catálogo o no respondió: nada se pudo verificar y lo obligatorio escala",
    }));
  }
  if (compra?.reciboFirmado === true) chips.push(chip("recibo firmado", { tono: "ok", icon: "key" }));
  else if (compra?.reciboFirmado === false) chips.push(chip("recibo sin firmar", { tono: "warn", icon: "alert" }));
  return chips.length ? h("span", { class: "chips-fuente" }, chips) : null;
}

/**
 * Atadura entre el cart que evaluó el Guard y el checkout que firmó el
 * comercio (chequeos con origen "checkout_firmado": hash, cart, total,
 * moneda, comercio). Si no hay, lo dice: el Guard no recibió el checkout.
 */
export function bloqueAtadura(veredicto, { tabla = false } = {}) {
  if (!veredicto) return null;
  const lista = chequeosCheckoutFirmado(veredicto);
  if (!lista.length) {
    return h("p", { class: "nota", text: "Este veredicto no trae chequeos contra el checkout firmado: el Guard evaluó sin checkout=." });
  }
  if (tabla) return tablaChequeos(lista);
  return h("ul", { class: "lista-chequeos atadura" }, ordenarChequeos(lista).map((c) => h("li", { class: "chequeo" },
    insignia(RESULTADO, c.resultado),
    h("span", { class: "chequeo-tipo", text: c.campo ?? c.tipo }),
    h("span", { class: "chequeo-detalle", text: c.detalle }))));
}

/**
 * Checkout retenido por `escalar`: la evidencia del Guard y "Aprobar igual"
 * (POST /guard/aprobaciones a través del servidor del panel). La aprobación
 * es de un solo uso y el Guard vuelve a evaluar el reintento.
 */
export function panelAprobacion(compra, ctx, { compacto = false } = {}) {
  const est = ctx.estadoAprobacion(compra);
  if (!est.clave) return null;
  if (est.clave === "aprobado_humano") {
    return h("div", { class: ["panel-aprobacion", "t-ok"], role: "status" },
      h("p", { class: "aprobacion-estado" }, icono("check"), h("strong", { text: "Aprobado por humano" })),
      h("p", { text: "El Guard volvió a evaluar el reintento (escalar, mismo monto) y el checkout pasó intacto con la aprobación, que ya quedó usada." }));
  }
  const v = compra.veredicto;
  const cuerpo = cuerpoAprobacion(compra);
  const enVivo = ctx.estado.fuente === "vivo" && ctx.estado.proxy.conectado === true;
  const mensaje = ctx.mensajeAprobacion(compra.contextoId);
  const aprobada = est.clave === "aprobada";
  const malos = ordenarChequeos(v?.chequeos).filter((c) => c.resultado !== "ok");
  return h("div", { class: ["panel-aprobacion", aprobada ? "t-ok" : "t-serious"] },
    h("p", { class: "aprobacion-estado" },
      icono(aprobada ? "check" : "up"),
      h("strong", { text: aprobada ? "Aprobado por humano" : "Retenido — esperando decisión humana" })),
    aprobada ? h("p", { class: "aprobacion-paso", text: "Pedile al agente que reintente el checkout." }) : null,
    v ? [
      h("p", { class: "rotulo", text: "Evidencia del Guard" }),
      h("p", { class: "aprobacion-motivo", text: v.motivo ?? "" }),
      compacto ? listaChequeos(v.chequeos) : tablaChequeos(malos.length ? malos : v.chequeos),
    ] : h("p", { class: "nota", text: "Esta compra no trae el veredicto del Guard." }),
    cuerpo ? h("p", { class: "nota mono", text: `contexto ${cuerpo.contexto_id} · ${cuerpo.cart_id}` }) : null,
    est.clave === "retenido" ? h("div", { class: "aprobacion-acciones" },
      h("button", {
        class: "boton boton-primario", type: "button",
        disabled: !enVivo || !cuerpo || ctx.aprobando(compra.contextoId) ? true : null,
        onclick: () => ctx.aprobar(compra),
      }, icono("check"), ctx.aprobando(compra.contextoId) ? "Aprobando…" : "Aprobar igual"),
      h("span", { class: "nota", text: enVivo
        ? "Vale una sola vez, para este contexto, este cart y este monto. El Guard vuelve a evaluar el reintento."
        : "Solo en vivo, con el proxy conectado." })) : null,
    // Con la aprobación pendiente ya se muestra el paso siguiente: el aviso del
    // POST solo hace falta si falló o si todavía no se ve como aprobada.
    mensaje && (!mensaje.ok || !aprobada)
      ? h("p", { class: ["nota", mensaje.ok ? "t-ok" : "t-crit"], role: "status", text: mensaje.texto }) : null,
    aprobada ? h("p", { class: "nota", text: est.texto }) : null);
}

export function tablaEvidencias(evidencias) {
  if (!evidencias?.length) return vacio("El recibo no trae verificaciones");
  return h("div", { class: "tabla-scroll" }, h("table", { class: "tabla" },
    h("thead", {}, h("tr", {},
      ["Campo", "Declarado", "Catálogo público", "Estado", "Origen usado"].map((t) => h("th", { scope: "col", text: t })))),
    h("tbody", {}, evidencias.map((e) => h("tr", {},
      h("td", { class: "mono", text: e.campo }),
      h("td", { class: "mono valor", text: valorTexto(e.declarado) }),
      h("td", { class: "mono valor", text: valorTexto(e.publico) }),
      h("td", {}, insignia(EVIDENCIA, e.estado)),
      h("td", { class: "mono", text: e.origen_usado ?? "—" }),
    )))));
}

export function lineaRanking(compra, veredicto, ctx) {
  if (!veredicto?.ranking?.length) return null;
  const elegido = veredicto.cart_id_elegido;
  const pos = veredicto.ranking.indexOf(elegido);
  const primero = veredicto.cart_id_recomendado ?? veredicto.ranking[0];
  return h("p", { class: "ranking-linea" },
    "Ranking propio: 1.º ", h("strong", { text: etiquetaCart(compra, primero, ctx) }),
    elegido === primero ? " — coincide con la elección del agente."
      : [" — el agente eligió ", h("strong", { text: etiquetaCart(compra, elegido, ctx) }),
        pos >= 0 ? ` (${pos + 1}.º en el ranking propio).` : " (fuera del ranking propio)."]);
}

// ---------------------------------------------------------------------------
// Recibo y verificación de la firma
// ---------------------------------------------------------------------------

const PASOS = {
  formato: "JWS compacto ES256",
  kid: "kid coincide",
  firma: "Firma válida (WebCrypto)",
  contenido: "Contenido firmado = recibo mostrado",
};

export function panelVerificacion(recibo, ctx, { compacto = false } = {}) {
  if (!recibo?.firma) {
    return vacio("Recibo sin firma", "El recibo llegó sin el JWS del Guard: no hay nada que verificar.");
  }
  const v = ctx.verificacion(recibo);
  const boton = h("button", {
    class: "boton boton-primario", type: "button",
    onclick: () => ctx.verificar(recibo),
    disabled: v?.estado === "verificando" ? true : null,
  }, icono("key"), v ? "Verificar de nuevo" : "Verificar la firma en este navegador");
  let resultado = null;
  if (v?.estado === "verificando") resultado = h("p", { class: "nota", text: "Verificando…" });
  else if (v?.estado === "error") resultado = h("p", { class: "nota t-crit", text: v.mensaje });
  else if (v?.estado === "listo") {
    const r = v.resultado;
    resultado = h("div", { class: "verificacion", role: "status" },
      h("p", { class: ["verificacion-total", r.ok ? "t-ok" : "t-crit"] },
        icono(r.ok ? "check" : "x"),
        r.ok ? "Recibo verificado: lo firmó el Guard y no cambió." : "La verificación falló."),
      compacto ? null : h("ul", { class: "pasos-verif" }, r.pasos.map((p) => h("li", { class: p.ok ? "t-ok" : "t-crit" },
        icono(p.ok ? "check" : "x"), h("strong", { text: PASOS[p.id] ?? p.id }), h("span", { text: p.detalle })))),
      h("p", { class: "nota", text: `Clave pública: kid ${v.clave?.kid ?? "—"} · origen: ${v.clave?.origen ?? "—"}` }));
  }
  return h("div", { class: "panel-verif" }, boton, resultado);
}

// ---------------------------------------------------------------------------
// Mandatos del usuario (SD-JWT)
// ---------------------------------------------------------------------------

export function bloqueMandatos(mandatos, compra, ctx) {
  return h("div", { class: "mandatos" }, mandatos.map((m) => {
    const dec = ctx.mandato(m.token);
    const nombre = nombreMandato(m.nombre);
    let cuerpo;
    if (!dec) cuerpo = h("p", { class: "nota", text: "Decodificando…" });
    else if (dec.error) {
      cuerpo = [h("p", { class: "nota", text: `No se pudo decodificar: ${dec.error}` }),
        h("code", { class: "token", text: String(m.token).slice(0, 120) + "…" })];
    } else {
      const claims = dec.claims;
      const hashMandato = buscarClave(claims, "checkout_hash");
      const coincide = hashMandato && compra?.checkout?.checkout_hash
        ? hashMandato === compra.checkout.checkout_hash : null;
      const extracto = {};
      for (const k of CLAIMS_EXTRACTO) {
        const v = buscarValor(claims, k);
        if (v !== undefined) extracto[k] = v;
      }
      const hayExtracto = Object.keys(extracto).length > 0;
      cuerpo = [
        dec.jwt ? vistaJws(dec.jwt, { etiquetaFirma: `firma ${dec.header?.alg ?? "?"} del usuario`, payloadMax: 10 }) : null,
        hayExtracto ? vistaJson(extracto, { acortar: 24 }) : null,
        coincide === true ? chip("referencia el mismo checkout_hash del comercio", { tono: "ok", icon: "check" })
          : coincide === false ? chip("checkout_hash distinto al del checkout", { tono: "crit", icon: "x" }) : null,
        h("details", { class: "mas", open: hayExtracto ? null : true },
          h("summary", { text: "claims divulgados completos" }), vistaJson(claims, { acortar: 24 })),
      ];
    }
    return h("section", { class: "mandato" },
      h("h4", {}, icono("pen"), `${nombre} · SD-JWT firmado por el usuario`), cuerpo);
  }));
}

// Lo que importa mostrar de cada mandato; el resto queda plegado.
const CLAIMS_EXTRACTO = ["vct", "checkout_hash", "transaction_id", "payment_amount", "payee", "payment_instrument"];

function buscarValor(obj, clave) {
  if (!obj || typeof obj !== "object") return undefined;
  if (!Array.isArray(obj) && Object.hasOwn(obj, clave)) return obj[clave];
  for (const v of Object.values(obj)) {
    const r = buscarValor(v, clave);
    if (r !== undefined) return r;
  }
  return undefined;
}

function buscarClave(obj, clave) {
  const v = buscarValor(obj, clave);
  return typeof v === "string" ? v : null;
}

export function lineaCompra(pedido, compro) {
  return h("dl", { class: "comparacion" },
    filaDato("Pidió", pedido), filaDato("Compró", compro));
}

export function resumenFicha(ficha) {
  if (!ficha) return "—";
  const r = ficha.requisitos || [];
  const txt = (q) => q.operador === "maximo" ? `≤ ${q.valor}` : q.operador === "incluye" ? `talle ${q.valor}` : String(q.valor);
  const obl = r.filter((q) => q.obligatorio).map(txt);
  const pref = r.filter((q) => !q.obligatorio).map(txt);
  return `${obl.join(" · ")}${pref.length ? ` (preferido: ${pref.join(", ")})` : ""}`;
}

export function resumenProducto(producto, monto, moneda) {
  if (!producto) return monto !== null ? dinero(monto, moneda) : "—";
  return `${producto.sku} · ${producto.category} · ${producto.use} · ${producto.brand} · ${dinero(monto ?? producto.price, moneda)}`;
}
