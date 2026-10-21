"""Historial de las compras de integrador: lo que se guarda de cada evento en
`gw_eventos` (panel del cliente, fase 1).

Los eventos de una compra de integrador (los que van al JSONL privado) se
guardan en el almacén en segundo plano y sin decidir nada
(`ProxyGuard._emitir`), para que el panel arme la línea de tiempo
(`proxy/explicacion.py`) desde cualquier instancia. **La base nunca guarda
texto libre del comercio**: de cada tipo de evento pasa solo lo de esta lista
blanca, y cada valor vuelve a validarse (ids con `a2a.ID_SEGURO_RE` o
`a2a.cart_id_seguro`, montos finitos, monedas ISO, fechas, cronogramas y
etiquetas fijas):

| tipo | qué se guarda |
|---|---|
| `busqueda_enviada` | nada (el texto de la búsqueda no) |
| `ofertas_recibidas` | cuántas ofertas; por cart `cart_id`, `monto` y `moneda`; por ficha `cart_id`, `sku`, `precio`, `moneda`, `cantidad` y `entregas` |
| `checkout_pedido` | `cart_id` |
| `checkout_recibido` | `cart_id` y si el checkout es de otro cart (nunca el JWT) |
| `veredicto` | `status`, `cart_id`, `llega_a_firma`, `chequeos_detalle` y `alertas` (`proxy/alertas.py`), `monto`, `moneda`, el id del recibo y si está firmado, `evaluador`, `fuente` y si se descartó el catálogo |
| `reenviado` | `metodo` y la aprobación humana (nunca los mandatos) |
| `bloqueado` | `etapa`, `causa`, `status`, `cart_id`, `requiere_aprobacion`, `sin_veredicto`, las `senales` del filtro (`SENAL_RE`) y el `motivo` solo si es uno nuestro (un corte con `causa` conocida o un bloqueo del proxy); el de un veredicto del Guard no (puede citar el catálogo) |
| `pedido_detectado` | `turno` y `vence` (la pregunta no: la arma un LLM) |
| `contexto_registrado` | `turno`, el porqué del usuario acotado (`LARGO_PORQUE`), `omitido`, `vencido`, el cronograma de la necesidad y cuántas flexibilidades |
| `oportunidad` | `vence`, ids, `precio`, `moneda` y los `cambios` tipados (sin título ni `por_que`) |
| `oportunidad_decidida` | la decisión y los requisitos autorizados tipados |

`error` y `guard_cambiado` no se guardan: un error puede citar bytes del
comercio. El `escenario` (lo manda el comercio) tampoco. Funciones puras.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from contracts import STATUS_LLEGAN_A_FIRMA, Evento
from proxy import a2a, alertas
from proxy.sin_estado import MAX_SENALES, SENAL_RE

TIPOS_GUARDADOS = frozenset({
    "busqueda_enviada", "ofertas_recibidas", "checkout_pedido", "checkout_recibido",
    "veredicto", "reenviado", "bloqueado", "pedido_detectado", "contexto_registrado",
    "oportunidad", "oportunidad_decidida",
})

MAX_OFERTAS = 50
MAX_ENTREGAS = 20
MAX_REQUISITOS = 50
LARGO_PORQUE = 300
LARGO_MOTIVO = 300
MAX_TURNO = 100

STATUS = frozenset({"aprobado", "con_avisos", "escalar", "bloqueado"})
ETAPAS = frozenset({"seguridad", "intencion"})
# Por qué cortó la etapa 2 (`app.CAUSAS_CORTE`: un motivo fijo cada una).
CAUSAS = frozenset({"filtro", "filtro_fallo", "ilegible", "ya_bloqueada", "contexto_ajeno",
                    "sin_decision"})
METODOS = frozenset({"find_products", "create_checkout", "otro"})
EVALUADORES = frozenset({"guard", "stub"})
FUENTES_CATALOGO = frozenset({"http", "fixture", "ninguno"})
DECISIONES = frozenset({"aceptada", "rechazada", "vencida"})
CAMPOS_REQUISITO = frozenset({
    "precio", "moneda", "categoria", "uso", "talle", "marca", "comercio", "cantidad", "entregas",
    "atributo",
})
OPERADORES = frozenset({"igual", "maximo", "minimo", "incluye", "cubre"})
ORIGENES_REQUISITO = frozenset({"dicho", "inferido", "aceptado", "politica"})
RECIBO_RE = re.compile(r"rcb-[0-9a-f]{8,64}")
# Lo que antepone `ProxyGuard._bloqueo` al motivo de un bloqueo del proxy.
PREFIJO_BLOQUEO_PROXY = "No se pudo evaluar el checkout:"


# -- valores -------------------------------------------------------------------


def _id(valor: Any) -> str | None:
    return a2a.id_seguro(valor)


def _cart(valor: Any) -> str | None:
    return a2a.cart_id_seguro(valor)


def _numero(valor: Any) -> float | int | None:
    seguro, _ = alertas.valor_saneado(valor, confiable=False)
    return seguro if isinstance(seguro, (int, float)) and not isinstance(seguro, bool) else None


def _moneda(valor: Any) -> str | None:
    return valor if isinstance(valor, str) and alertas.MONEDA_RE.fullmatch(valor) else None


def _entero_positivo(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    return valor if 0 < valor <= 1_000_000 else None


def _fecha(valor: Any) -> str | None:
    seguro, _ = alertas.valor_saneado(valor, confiable=False)
    return seguro if isinstance(seguro, str) and alertas.FECHA_HORA_RE.fullmatch(seguro) else None


def _bool(valor: Any) -> bool:
    return valor is True


def _de(valor: Any, permitidos: frozenset[str]) -> str | None:
    return valor if isinstance(valor, str) and valor in permitidos else None


def _lista(valor: Any) -> list[Any]:
    return valor if isinstance(valor, list) else []


def _limpio(texto: Any, largo: int) -> str | None:
    """Sin caracteres de control ni de formato (los bidi incluidos), con los
    saltos de línea como espacios, y recortado. None si no es texto."""
    if not isinstance(texto, str):
        return None
    plano = "".join(
        " " if c in "\n\r\t" else c
        for c in texto
        if c in "\n\r\t" or not unicodedata.category(c).startswith(("C", "Zl", "Zp"))
    )
    plano = " ".join(plano.split())
    return plano[:largo] or None


def _entregas_ofrecidas(valor: Any) -> list[dict[str, Any]]:
    """El cronograma que declara el comercio (`[{quantity, date}]`), tipado
    como lo lee el Guard; uno que no valida entero no se toma."""
    from guard.normalizar import entregas_validas  # perezoso: como el resto del proxy

    cronograma = entregas_validas(valor) if isinstance(valor, list) else None
    if not cronograma:
        return []
    return [
        {"cantidad": cantidad, "fecha": fecha.isoformat()}
        for fecha, cantidad in cronograma[:MAX_ENTREGAS]
    ]


def _entregas_tipadas(valor: Any) -> list[dict[str, Any]]:
    """Un cronograma ya en `Entrega` (`[{cantidad, fecha}]`)."""
    seguro, _ = alertas.valor_saneado(valor, confiable=False)
    if not isinstance(seguro, list):
        return []
    return [e for e in seguro[:MAX_ENTREGAS] if isinstance(e, dict)]


def _senales(valor: Any) -> list[str]:
    return [s for s in _lista(valor) if isinstance(s, str) and SENAL_RE.fullmatch(s)][:MAX_SENALES]


# -- por tipo ----------------------------------------------------------------


def _ofertas(datos: dict[str, Any]) -> dict[str, Any]:
    carts = [c for c in _lista(datos.get("carts")) if isinstance(c, dict)]
    detalles = [d for d in _lista(datos.get("offer_details")) if isinstance(d, dict)]
    return {
        "ofertas": len(carts),
        "carts": [
            {"cart_id": _cart(c.get("cart_id")), "monto": _numero(c.get("amount")),
             "moneda": _moneda(c.get("currency"))}
            for c in carts[:MAX_OFERTAS]
        ],
        "detalles": [
            {"cart_id": _cart(d.get("cart_id")), "sku": _id(d.get("sku")),
             "precio": _numero(d.get("price")), "moneda": _moneda(d.get("currency")),
             "cantidad": _entero_positivo(d.get("quantity")),
             "entregas": _entregas_ofrecidas(d.get("deliveries"))}
            for d in detalles[:MAX_OFERTAS]
        ],
    }


def _veredicto(datos: dict[str, Any]) -> dict[str, Any]:
    veredicto = datos.get("veredicto") if isinstance(datos.get("veredicto"), dict) else {}
    status = _de(veredicto.get("status"), STATUS)
    chequeos = veredicto.get("chequeos")
    monto, moneda = alertas.monto_evaluado(chequeos)
    recibo = veredicto.get("recibo") if isinstance(veredicto.get("recibo"), dict) else {}
    recibo_id = recibo.get("id")
    return {
        "status": status,
        "cart_id": _cart(veredicto.get("cart_id_elegido")),
        "llega_a_firma": status in STATUS_LLEGAN_A_FIRMA,
        "chequeos_detalle": alertas.chequeos_detalle(chequeos),
        "alertas": alertas.alertas_de(chequeos),
        "monto": monto,
        "moneda": moneda,
        "recibo_id": recibo_id if isinstance(recibo_id, str) and RECIBO_RE.fullmatch(recibo_id) else None,
        "recibo_firmado": _bool(datos.get("recibo_firmado")),
        "evaluador": _de(datos.get("evaluador"), EVALUADORES),
        "fuente": _de(datos.get("fuente"), FUENTES_CATALOGO),
        "catalogo_descartado": bool(datos.get("catalogo_descartado")),
    }


def _bloqueado(datos: dict[str, Any]) -> dict[str, Any]:
    etapa = _de(datos.get("etapa"), ETAPAS)
    causa = _de(datos.get("causa"), CAUSAS)
    sin_veredicto = _bool(datos.get("sin_veredicto"))
    motivo = None
    # Solo un motivo nuestro: el fijo de un corte (su causa es conocida) o el
    # de un bloqueo del proxy. El de un veredicto del Guard puede citar el
    # catálogo: el evento `veredicto` ya trae los chequeos saneados.
    if (etapa is not None and causa is not None) or sin_veredicto:
        motivo = _limpio(datos.get("motivo"), LARGO_MOTIVO)
        if sin_veredicto and etapa is None and not (motivo or "").startswith(PREFIJO_BLOQUEO_PROXY):
            motivo = None
    return {
        "etapa": etapa,
        "causa": causa,
        "status": _de(datos.get("status"), STATUS),
        "requiere_aprobacion": _bool(datos.get("requiere_aprobacion")),
        "sin_veredicto": sin_veredicto,
        "cart_id": _cart(datos.get("cart_id")),
        "motivo": motivo,
        "senales": _senales(datos.get("senales")),
    }


def _turno(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    return valor if 0 <= valor <= MAX_TURNO else None


def _contexto(datos: dict[str, Any]) -> dict[str, Any]:
    contexto = datos.get("contexto") if isinstance(datos.get("contexto"), dict) else {}
    return {
        "turno": _turno(datos.get("turno")),
        # El porqué es texto del usuario (no del comercio): acotado y limpio.
        "respuesta": _limpio(datos.get("respuesta"), LARGO_PORQUE),
        "omitido": _bool(datos.get("omitido")),
        "vencido": _bool(datos.get("vencido")),
        "necesidad_entregas": _entregas_tipadas(contexto.get("necesidad_entregas")),
        "flexibilidades": len([f for f in _lista(contexto.get("flexibilidades")) if isinstance(f, dict)]),
    }


def _cambio(cambio: dict[str, Any]) -> dict[str, Any]:
    campo = _de(cambio.get("campo"), CAMPOS_REQUISITO)
    # Lo pedido es de la ficha; lo ofrecido, de la oferta del comercio.
    pedido, falta_p = alertas.valor_saneado(cambio.get("pedido"), confiable=True)
    ofrecido, falta_o = alertas.valor_saneado(cambio.get("ofrecido"), confiable=False)
    item: dict[str, Any] = {"campo": campo, "pedido": pedido, "ofrecido": ofrecido}
    if campo is None or falta_p or falta_o:
        item["omitido"] = True
    return item


def _oportunidad(datos: dict[str, Any]) -> dict[str, Any]:
    op = datos.get("oportunidad") if isinstance(datos.get("oportunidad"), dict) else {}
    return {
        "vence": _fecha(datos.get("vence")),
        "oportunidad_id": _id(op.get("id")),
        "cart_id": _cart(op.get("cart_id")),
        "sku": _id(op.get("sku")),
        "precio": _numero(op.get("precio")),
        "moneda": _moneda(op.get("moneda")),
        "cambios": [_cambio(c) for c in _lista(op.get("cambios"))[:MAX_REQUISITOS] if isinstance(c, dict)],
    }


def requisito_saneado(crudo: dict[str, Any]) -> dict[str, Any]:
    """Un requisito con cada valor controlado: lo aceptado en una
    oportunidad sale de la oferta del comercio (sus textos no pasan)."""
    origen = _de(crudo.get("origen"), ORIGENES_REQUISITO)
    valor, falta = alertas.valor_saneado(crudo.get("valor"), confiable=origen != "aceptado")
    atributo = crudo.get("atributo")
    atributo = atributo if isinstance(atributo, str) and alertas.ATRIBUTO_RE.fullmatch(atributo) else None
    unidad, falta_unidad = alertas.valor_saneado({"valor": 0, "unidad": crudo.get("unidad")},
                                                 confiable=False)
    item: dict[str, Any] = {
        "campo": _de(crudo.get("campo"), CAMPOS_REQUISITO),
        "operador": _de(crudo.get("operador"), OPERADORES),
        "valor": valor,
        "obligatorio": crudo.get("obligatorio") if isinstance(crudo.get("obligatorio"), bool) else None,
        "atributo": atributo,
        "unidad": unidad["unidad"] if isinstance(unidad, dict) else None,
        "origen": origen,
    }
    if (falta or falta_unidad or item["campo"] is None
            or (crudo.get("atributo") is not None and atributo is None)):
        item["omitido"] = True
    return item


def _decidida(datos: dict[str, Any]) -> dict[str, Any]:
    requisitos = datos.get("requisitos_autorizados")
    return {
        "oportunidad_id": _id(datos.get("oportunidad_id")),
        "decision": _de(datos.get("decision"), DECISIONES),
        "requisitos_autorizados": [
            requisito_saneado(r) for r in requisitos[:MAX_REQUISITOS] if isinstance(r, dict)
        ] if isinstance(requisitos, list) else None,
    }


_PROYECCIONES = {
    "busqueda_enviada": lambda datos: {},
    "ofertas_recibidas": _ofertas,
    "checkout_pedido": lambda datos: {"cart_id": _cart(datos.get("cart_id"))},
    "checkout_recibido": lambda datos: {
        "cart_id": _cart(datos.get("cart_id")),
        "cart_distinto": "cart_id_checkout" in datos,
    },
    "veredicto": _veredicto,
    "reenviado": lambda datos: {
        "metodo": _de(datos.get("metodo"), METODOS),
        "aprobacion_humana": _bool(datos.get("aprobacion_humana")),
        "aprobacion_id": _id(datos.get("aprobacion_id")),
    },
    "bloqueado": _bloqueado,
    "pedido_detectado": lambda datos: {
        "turno": _turno(datos.get("turno")), "vence": _fecha(datos.get("vence")),
    },
    "contexto_registrado": _contexto,
    "oportunidad": _oportunidad,
    "oportunidad_decidida": _decidida,
}
assert set(_PROYECCIONES) == TIPOS_GUARDADOS


def proyectar(tipo: str, datos: Any) -> dict[str, Any] | None:
    """Lo que se guarda de un evento (ver la tabla del módulo), o None si ese
    tipo no se guarda."""
    proyeccion = _PROYECCIONES.get(tipo) if isinstance(tipo, str) else None
    if proyeccion is None:
        return None
    return proyeccion(datos if isinstance(datos, dict) else {})


def fila_historial(evento: Evento, *, integrador_id: str, compra_id: str) -> dict[str, Any] | None:
    """La fila de `gw_eventos` (como `estado.fila_evento`) con los datos
    proyectados, el `contexto_id` solo si es seguro y sin `escenario`. None si
    el tipo no se guarda."""
    datos = proyectar(evento.tipo, evento.datos)
    if datos is None:
        return None
    return {
        "id": evento.id,
        "ts": evento.ts.isoformat(),
        "integrador_id": integrador_id,
        "compra_id": compra_id,
        "contexto_id": _id(evento.contexto_id),
        "escenario": None,
        "guard_activo": evento.guard_activo,
        "tipo": evento.tipo,
        "datos": datos,
    }
