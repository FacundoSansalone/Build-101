// Panel del cliente de KaiAI: arma los nodos de la página. Recibe el
// `document` (en los tests de Node, uno falso) y pone todo texto con
// textContent: nunca HTML. Los atributos salen de valores que ya validó
// logica.mjs (un id de compra, un tono, un ícono de la lista fija).
//
// Lo que viene de la API se muestra como texto aunque traiga "<img …>": un
// pedido o una referencia los escribe la empresa, pero igual son datos.

import {
  alertasDe, fechaHoraLocal, hashDeCompra, infoResultado, montoMostrado,
} from "./logica.mjs";

export const SVG_NS = "http://www.w3.org/2000/svg";

// Íconos de trazo (24×24), dibujados con currentColor: van siempre junto a un
// texto, nunca solos.
export const ICONOS = Object.freeze({
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  freno: ["M7 7l10 10", "M17 7L7 17"],
  pausa: ["M9.5 6.5v11", "M14.5 6.5v11"],
  escudo: ["M12 3.5l7 2.8v5.6c0 4.2-2.9 7.3-7 8.9-4.1-1.6-7-4.7-7-8.9V6.3z", "M9 12l2 2 4-4"],
  reloj: ["M12 3.5a8.5 8.5 0 1 0 0 17a8.5 8.5 0 1 0 0-17", "M12 7.5V12l3 2"],
  curso: ["M6 12h.01", "M12 12h.01", "M18 12h.01"],
  guion: ["M7 12h10"],
  lupa: ["M10.5 4.5a6 6 0 1 0 0 12a6 6 0 1 0 0-12", "M15 15l4.5 4.5"],
  alerta: ["M12 4l9 16H3z", "M12 10v4", "M12 17h.01"],
  info: ["M12 3.5a8.5 8.5 0 1 0 0 17a8.5 8.5 0 1 0 0-17", "M12 11v5", "M12 8h.01"],
  pregunta: [
    "M12 3.5a8.5 8.5 0 1 0 0 17a8.5 8.5 0 1 0 0-17",
    "M9.6 9.6a2.5 2.5 0 1 1 3.4 2.3c-.6.3-1 .8-1 1.5v.3", "M12 16.8h.01",
  ],
  carro: ["M3.5 5h2.2l2 10h9.8l2-7H6.6", "M9.5 19h.01", "M16.5 19h.01"],
  moneda: [
    "M12 3.5a8.5 8.5 0 1 0 0 17a8.5 8.5 0 1 0 0-17",
    "M14.6 9.3c-.4-.9-1.4-1.5-2.6-1.5-1.5 0-2.6.8-2.6 2s1 1.6 2.6 2 2.6.9 2.6 2.1-1.1 2-2.6 2c-1.2 0-2.2-.6-2.6-1.5",
    "M12 6.3v1.5", "M12 16.2v1.5",
  ],
  recibo: ["M6.5 3.5h11v17l-2.75-1.6-2.75 1.6-2.75-1.6-2.75 1.6z", "M9.5 8.5h5", "M9.5 12h5"],
});

const TONOS = new Set(["ok", "aviso", "crit", "info", "neutro"]);

function tono(valor) {
  return TONOS.has(valor) ? valor : "neutro";
}

export function crearVista(doc) {
  function el(tag, { clase, texto, atributos } = {}, hijos = []) {
    const nodo = doc.createElement(tag);
    if (clase) nodo.className = clase;
    if (texto !== undefined && texto !== null) nodo.textContent = String(texto);
    for (const [nombre, valor] of Object.entries(atributos || {})) nodo.setAttribute(nombre, valor);
    for (const hijo of hijos) if (hijo) nodo.append(hijo);
    return nodo;
  }

  function icono(nombre) {
    const svg = doc.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", "icono");
    for (const d of Object.hasOwn(ICONOS, nombre) ? ICONOS[nombre] : ICONOS.guion) {
      const trazo = doc.createElementNS(SVG_NS, "path");
      trazo.setAttribute("d", d);
      svg.append(trazo);
    }
    return svg;
  }

  /** Un chip: ícono + texto, con el tono como atributo (el color lo pone el CSS). */
  function chip(info, { prefijo = "", clase = "chip" } = {}) {
    const nodo = el("span", { clase, atributos: { "data-tono": tono(info?.tono) } }, [icono(info?.icono)]);
    if (prefijo) nodo.append(el("span", { clase: "sr-only", texto: prefijo }));
    nodo.append(el("span", { texto: info?.texto ?? "" }));
    return nodo;
  }

  function chipResultado(resultado, opciones) {
    return chip(infoResultado(resultado), opciones);
  }

  /** Las alertas de una compra como chips; "Ninguna" si no hay. */
  function chipsAlertas(lista) {
    const alertas = alertasDe(lista);
    if (!alertas.length) return el("span", { clase: "sin-alertas", texto: "Ninguna" });
    const caja = el("span", { clase: "chips" });
    for (const a of alertas) {
      caja.append(chip({ ...a, texto: a.corto }, { prefijo: a.estafa ? "Intento de estafa: " : "Aviso: " }));
    }
    return caja;
  }

  function celda(etiqueta, hijos, clase = "") {
    return el("td", { clase, atributos: { "data-etiqueta": etiqueta, role: "cell" } }, hijos);
  }

  /** Una fila del listado. null si la compra no tiene un id válido. */
  function filaCompra(compra) {
    const hash = hashDeCompra(compra?.compra_id);
    if (!hash) return null;
    const pedido = typeof compra.pedido === "string" && compra.pedido.trim() ? compra.pedido : "(sin pedido)";
    const creada = typeof compra.creada === "string" ? compra.creada : "";
    const monto = montoMostrado(compra);
    const referencia = typeof compra.referencia === "string" && compra.referencia ? compra.referencia : "—";
    const comercio = typeof compra.comercio === "string" && compra.comercio ? compra.comercio : "—";
    const enlace = el("a", { clase: "enlace-compra", texto: pedido, atributos: { href: hash } });
    return el("tr", { clase: "fila", atributos: { "data-compra-id": compra.compra_id, role: "row" } }, [
      celda("Fecha", [el("time", { texto: fechaHoraLocal(creada), atributos: creada ? { datetime: creada } : {} })]),
      celda("Pedido", [enlace], "col-pedido"),
      celda("Referencia", [el("span", { texto: referencia })], "col-referencia"),
      celda("Comercio", [el("span", { texto: comercio })]),
      celda("Monto", [el("span", { clase: monto.tope ? "tope" : "", texto: monto.texto })], "col-monto"),
      celda("Resultado", [chipResultado(compra.resultado)]),
      celda("Alertas", [chipsAlertas(compra.alertas)]),
    ]);
  }

  /** Una tarjeta del resumen. */
  function tarjetaResumen(t) {
    const hijos = [
      el("p", { clase: "tarjeta-titulo" }, [icono(t.icono), el("span", { texto: t.titulo })]),
      el("p", { clase: "tarjeta-valor", texto: t.valor }),
    ];
    if (Array.isArray(t.lineas) && t.lineas.length > 1) {
      hijos.push(el("ul", { clase: "tarjeta-lineas" }, t.lineas.slice(1).map((l) => el("li", { texto: l }))));
    } else if (t.detalle) {
      hijos.push(el("p", { clase: "tarjeta-detalle", texto: t.detalle }));
    }
    return el("li", { clase: "tarjeta-resumen", atributos: { "data-tono": tono(t.tono), "data-clave": t.clave } }, hijos);
  }

  /** Un paso de la línea de tiempo (de `lineaDeTiempo`). */
  function itemLinea(paso) {
    const marca = el("span", { clase: "linea-marca", atributos: { "aria-hidden": "true" } });
    const cuando = paso.ts
      ? el("time", { clase: "linea-cuando", texto: paso.cuando, atributos: { datetime: paso.ts } })
      : el("span", { clase: "linea-cuando", texto: paso.cuando });
    return el("li", { clase: paso.omitido ? "paso paso-omitido" : "paso" }, [
      marca,
      el("p", { clase: "linea-cab" }, [el("span", { clase: "linea-etapa", texto: paso.nombre }), cuando]),
      el("p", { clase: "linea-texto", texto: paso.texto }),
    ]);
  }

  /** Un chequeo de "Qué revisamos" (de `chequeosDe`). */
  function itemChequeo(c) {
    return el("li", { clase: "chequeo", atributos: { "data-tono": tono(c.info.tono) } }, [
      chip(c.info, { clase: "chip chip-chequeo" }),
      el("p", { clase: "chequeo-texto", texto: c.texto }),
    ]);
  }

  /** Los pares nombre/valor de una `<dl>` (de `datosDeCompra`). */
  function datos(lista) {
    const salida = [];
    for (const d of lista) {
      salida.push(el("div", {}, [
        el("dt", { texto: d.nombre }),
        el("dd", { clase: d.mono ? "mono" : "", texto: d.valor }),
      ]));
    }
    return salida;
  }

  /**
   * El bloque de estafa del detalle (de `separarAlertas`): el badge con cada
   * tipo, o una nota de que no vimos ninguno.
   */
  function bloqueEstafa(estafa) {
    if (!estafa.length) {
      return el("p", { clase: "sin-estafa", atributos: { "data-tono": "ok" } }, [
        icono("check"), el("span", { texto: "No vimos intentos de estafa en esta compra." }),
      ]);
    }
    return el("div", { clase: "estafa", atributos: { "data-tono": "crit" } }, [
      el("p", { clase: "estafa-titulo" }, [icono("alerta"), el("strong", { texto: "Intento de estafa" })]),
      el("ul", { clase: "estafa-tipos" }, estafa.map((a) => el("li", { texto: a.texto }))),
    ]);
  }

  /** Los avisos neutros (no_cumple, sin_verificar): no son estafa. */
  function avisos(lista) {
    return lista.map((a) => el("li", { clase: "aviso-neutro", atributos: { "data-tono": "neutro" } }, [
      icono("info"), el("span", { texto: a.texto }),
    ]));
  }

  return {
    el, icono, chip, chipResultado, chipsAlertas, filaCompra, tarjetaResumen, itemLinea,
    itemChequeo, datos, bloqueEstafa, avisos,
  };
}
