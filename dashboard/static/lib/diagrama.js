// Diagrama de /demo: quién le habla a quién y qué decide el intermediario.
// Una máquina de estados pura: cada item de la línea de tiempo (evento del
// proxy o mensaje de la conversación) da los mensajes que viajan por las
// flechas y el color del Guard y de sus etapas. Sin DOM: node --test.

export const NODOS = ["usuario", "agente", "guard", "comercio", "fuentes", "ap2"];

// Aristas dibujadas. `agente-comercio` es el atajo: lo usa todo cuando el
// intermediario está apagado.
const ARISTAS = [
  "usuario-agente", "agente-guard", "guard-comercio", "agente-comercio",
  "guard-usuario", "guard-fuentes", "comercio-ap2",
];

/** La arista que une dos nodos y si se recorre al revés. null si no hay. */
export function arista(de, a) {
  if (ARISTAS.includes(`${de}-${a}`)) return { clave: `${de}-${a}`, invertida: false };
  if (ARISTAS.includes(`${a}-${de}`)) return { clave: `${a}-${de}`, invertida: true };
  return null;
}

const ETIQUETAS = {
  usuario: { etiqueta: "Vos" },
  agente: { etiqueta: "Tu agente", sub: "de compras" },
  guard: { etiqueta: "KaiAI", sub: "el intermediario" },
  comercio: { etiqueta: "Comercio" },
  fuentes: { etiqueta: "Fabricante · catálogo" },
  ap2: { etiqueta: "AP2 · pago" },
};

const LAYOUTS = {
  // Escritorio: una línea de izquierda a derecha. Apagado, el Guard baja y
  // la línea queda directa.
  horizontal: {
    ancho: 780, alto: 330,
    nodos: {
      usuario: { x: 70, y: 150, w: 100, h: 48 },
      agente: { x: 255, y: 150, w: 150, h: 56 },
      guard: { x: 480, y: 150, w: 200, h: 96 },
      comercio: { x: 690, y: 150, w: 130, h: 56 },
      fuentes: { x: 480, y: 36, w: 180, h: 40 },
      ap2: { x: 690, y: 290, w: 130, h: 40 },
    },
    aristas: {
      "usuario-agente": "M 120 150 L 180 150",
      "agente-guard": "M 330 150 L 380 150",
      "guard-comercio": "M 580 150 L 625 150",
      "agente-comercio": "M 330 150 L 625 150",
      "guard-usuario": "M 420 198 C 420 266, 70 266, 70 174",
      "guard-fuentes": "M 480 102 L 480 56",
      "comercio-ap2": "M 690 178 L 690 270",
    },
    apagado: { dx: 0, dy: 122 },
  },
  // Franja: la tarjeta compacta arriba del chat, del ancho de la conversación.
  // Píldoras en una línea, sin subtítulos. Apagado, el Guard baja y la línea
  // queda directa.
  franja: {
    ancho: 720, alto: 124,
    nodos: {
      usuario: { x: 36, y: 66, w: 60, h: 30 },
      agente: { x: 150, y: 66, w: 108, h: 30 },
      guard: { x: 360, y: 66, w: 232, h: 56 },
      comercio: { x: 556, y: 66, w: 100, h: 30 },
      fuentes: { x: 360, y: 14, w: 152, h: 22 },
      ap2: { x: 672, y: 66, w: 84, h: 24 },
    },
    aristas: {
      "usuario-agente": "M 66 66 L 96 66",
      "agente-guard": "M 204 66 L 244 66",
      "guard-comercio": "M 476 66 L 506 66",
      "agente-comercio": "M 204 66 L 506 66",
      "guard-usuario": "M 296 94 C 296 126, 36 126, 36 81",
      "guard-fuentes": "M 360 38 L 360 25",
      "comercio-ap2": "M 606 66 L 630 66",
    },
    etiquetas: { agente: { sub: null }, guard: { sub: null } },
    apagado: { dx: 0, dy: 34 },
  },
  // Teléfono: una columna. Apagado, el Guard se corre a la izquierda.
  vertical: {
    ancho: 400, alto: 620,
    nodos: {
      usuario: { x: 230, y: 40, w: 110, h: 44 },
      agente: { x: 230, y: 150, w: 170, h: 52 },
      guard: { x: 230, y: 300, w: 206, h: 100 },
      comercio: { x: 230, y: 460, w: 150, h: 52 },
      fuentes: { x: 330, y: 205, w: 120, h: 40 },
      ap2: { x: 230, y: 580, w: 130, h: 40 },
    },
    aristas: {
      "usuario-agente": "M 230 62 L 230 124",
      "agente-guard": "M 230 176 L 230 250",
      "guard-comercio": "M 230 350 L 230 434",
      "agente-comercio": "M 230 176 L 230 434",
      "guard-usuario": "M 127 300 C 40 300, 40 40, 175 40",
      "guard-fuentes": "M 290 250 L 318 225",
      "comercio-ap2": "M 230 486 L 230 560",
    },
    apagado: { dx: -120, dy: 0 },
  },
};

/** Posiciones, aristas (paths SVG), etiquetas y cuánto se corre el Guard apagado. */
export function geometria(orientacion = "horizontal") {
  const base = LAYOUTS[orientacion] ?? LAYOUTS.horizontal;
  const nodos = {};
  for (const id of NODOS) nodos[id] = { id, ...base.nodos[id], ...ETIQUETAS[id], ...base.etiquetas?.[id] };
  return { orientacion, ancho: base.ancho, alto: base.alto, nodos, aristas: { ...base.aristas }, apagado: { ...base.apagado } };
}

// ---------------------------------------------------------------- estado

const ETAPAS = ["filtro", "intencion", "verificacion"];

export function estadoInicial(guardActivo = true) {
  const e = {
    guardActivo: Boolean(guardActivo),
    guard: "inactivo",
    etapas: { filtro: "inactivo", intencion: "inactivo", verificacion: "inactivo" },
    nodos: { usuario: "inactivo", agente: "inactivo", comercio: "inactivo", fuentes: "inactivo", ap2: "inactivo" },
    // Un pedido que quedó en el Guard (o salió directo) y espera su evento del proxy.
    pendiente: null,
  };
  e.guard = colorGuard(e);
  return e;
}

/** El color del Guard sale de sus etapas: rojo > ámbar > trabajando > verde. */
function colorGuard(e) {
  if (!e.guardActivo) return "apagado";
  const valores = ETAPAS.map((k) => e.etapas[k]);
  for (const v of ["bloqueado", "aviso", "trabajando", "ok"]) if (valores.includes(v)) return v;
  return "inactivo";
}

function copiar(e) {
  return { ...e, etapas: { ...e.etapas }, nodos: { ...e.nodos } };
}

const flujo = (de, a, tono = "neutro") => ({ de, a, tono, ...arista(de, a) });

/** De `de` a `a` pasando por el Guard (prendido) o por el atajo (apagado). */
function via(activo, de, a, tono) {
  return activo ? [flujo(de, "guard", tono), flujo("guard", a, tono)] : [flujo(de, a, tono)];
}

const PENDIENTES = { find_products: "busqueda", create_checkout: "checkout", initiate_payment: "pago" };

function pasoDemo(e, item) {
  switch (item.tipo) {
    case "usuario":
      if (item.destino && item.destino !== "agente") {
        e.etapas.intencion = "trabajando";
        return [flujo("usuario", "guard")];
      }
      return [flujo("usuario", "agente")];
    case "auto":
      return [flujo("usuario", "agente", "tenue")];
    case "agente":
      return item.oculto ? [] : [flujo("agente", "usuario")];
    case "herramienta": {
      const pendiente = PENDIENTES[item.nombre];
      if (!pendiente) return [];
      e.pendiente = { tipo: pendiente, activo: e.guardActivo };
      return [flujo("agente", e.guardActivo ? "guard" : "comercio")];
    }
    case "pago":
      e.nodos.ap2 = "ok";
      return [flujo("ap2", "comercio", "ok"), ...via(e.guardActivo, "comercio", "agente", "ok")];
    default:
      return [];
  }
}

/** El pedido del agente al comercio: si ya salió del agente, solo lo que falta. */
function pedidoAlComercio(e, tipo, activo) {
  const p = e.pendiente;
  e.pendiente = null;
  if (p?.tipo === tipo) {
    if (activo && p.activo) return [flujo("guard", "comercio")];
    if (!activo && !p.activo) return [];
  }
  return via(activo, "agente", "comercio");
}

function pasoProxy(e, item) {
  const d = item.datos || {};
  if (typeof item.guard_activo === "boolean") e.guardActivo = item.guard_activo;
  const on = e.guardActivo;
  switch (item.tipo) {
    case "guard_cambiado":
      e.guardActivo = d.guard_activo === true;
      return [];
    case "busqueda_enviada":
      return pedidoAlComercio(e, "busqueda", on);
    case "ofertas_recibidas":
      if (!on) return [flujo("comercio", "agente")];
      e.etapas.filtro = "trabajando";
      return [flujo("comercio", "guard")];
    case "reenviado":
      if (d.mandatos) {
        e.nodos.ap2 = "trabajando";
        return [...pedidoAlComercio(e, "pago", on), flujo("comercio", "ap2")];
      }
      if (!on) return [];
      if (d.metodo === "find_products") {
        e.etapas.filtro = "ok";
        return [flujo("guard", "agente", "ok")];
      }
      if (d.metodo === "create_checkout") {
        e.etapas.verificacion = "ok";
        return [flujo("guard", "agente", "ok")];
      }
      return [];
    case "bloqueado":
      if (d.etapa === "seguridad") {
        e.etapas.filtro = "bloqueado";
        return [flujo("guard", "agente", "crit")];
      }
      if (d.etapa === "intencion") {
        e.etapas.intencion = "aviso";
        return [flujo("guard", "agente", "aviso")];
      }
      if (d.requiere_aprobacion) {
        e.etapas.verificacion = "aviso";
        return [flujo("guard", "usuario", "aviso")];
      }
      e.etapas.verificacion = "bloqueado";
      return [flujo("guard", "agente", "crit")];
    case "checkout_pedido":
      return pedidoAlComercio(e, "checkout", on);
    case "checkout_recibido":
      if (!on) return [flujo("comercio", "agente")];
      e.etapas.verificacion = "trabajando";
      return [flujo("comercio", "guard")];
    case "veredicto": {
      const status = d.veredicto?.status;
      e.nodos.fuentes = "trabajando";
      e.etapas.verificacion = status === "bloqueado" ? "bloqueado"
        : status === "escalar" ? "aviso"
          : status === "aprobado" || status === "con_avisos" ? "ok" : e.etapas.verificacion;
      return [flujo("guard", "fuentes"), flujo("fuentes", "guard")];
    }
    case "pedido_detectado":
      e.etapas.intencion = "trabajando";
      return [flujo("guard", "usuario")];
    case "contexto_registrado":
      e.etapas.intencion = "ok";
      return [];
    case "oportunidad":
      e.etapas.intencion = "aviso";
      return [flujo("guard", "usuario", "aviso")];
    case "oportunidad_decidida":
      e.etapas.intencion = d.decision === "vencida" ? "aviso" : "ok";
      return [];
    default:
      return [];
  }
}

/** Un paso: {estado (nuevo, no muta), flujos: [{de, a, tono, clave, invertida}]}. */
export function paso(estado, item) {
  const e = copiar(estado);
  const flujos = item?.origen === "demo" ? pasoDemo(e, item) : pasoProxy(e, item || {});
  e.guard = colorGuard(e);
  return { estado: e, flujos: flujos.filter((f) => f.clave) };
}

/** Toda la línea de tiempo: el estado final y todos los flujos en orden. */
export function recorrer(items, guardActivo = true) {
  let estado = estadoInicial(guardActivo);
  const flujos = [];
  for (const item of items || []) {
    const r = paso(estado, item);
    estado = r.estado;
    flujos.push(...r.flujos);
  }
  return { estado, flujos };
}
