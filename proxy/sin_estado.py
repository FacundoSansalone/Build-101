"""Servicios sin estado del gateway: `POST /v1/evaluar` y `POST /v1/filtrar`.

Son las dos piezas que un integrador puede usar sin pasar su tráfico A2A por
el gateway (modo mínimo):

- `evaluar_sin_estado`: el mismo Guard que corre antes de la firma en el
  proxy, pero con la ficha, el checkout y las ofertas que manda el integrador.
  No hay compra, ni contexto, ni aprobaciones: el integrador firma solo si la
  respuesta es 200 y `llega_a_firma` es true.
- `filtrar_contenido`: el filtro de seguridad sobre lo que el agente lee fuera
  del gateway (resultados de búsqueda, páginas, salidas de herramientas).

Mismas reglas que el proxy: falla cerrado (lo que no se pudo revisar o evaluar
sale `bloqueado`) y nada del texto del vendedor ni del contenido recibido sale
en la respuesta. Los motivos son textos fijos de este módulo o las plantillas
del Guard (`tipos_gateway.vista_veredicto`).
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import logging
import re
from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import ValidationError

from contracts import (
    CheckoutVisto,
    Filtro,
    OfertasVistas,
    Politica,
    ResultadoFiltro,
    Veredicto,
)
from proxy import a2a
from proxy.evaluador import ClaveRecibo, EvaluadorCargado, firmar_recibo
from proxy.fuentes import cargar_catalogo, skus_de
from proxy.tipos_gateway import (
    ID_SEGURO_RE,
    Comercio,
    ErrorApi,
    SolicitudEvaluar,
    SolicitudFiltrar,
    es_id,
    vista_veredicto,
)

logger = logging.getLogger("proxy.sin_estado")

# -- /v1/filtrar ---------------------------------------------------------------
MOTIVO_PASA = "Sin señales de contenido malicioso."
MOTIVO_BLOQUEA = (
    "El filtro de seguridad detectó contenido dirigido a manipular al agente: "
    "no se lo pases a tu modelo."
)
MOTIVO_FALLA = (
    "El filtro de seguridad no pudo revisar el contenido: tratalo como bloqueado."
)
# Las señales del filtro son etiquetas cortas (`heuristica:orden_al_agente`,
# `modelo:...`). Solo pasan las que tienen esa forma: una señal con texto libre
# podría traer un pedazo del contenido y se descarta.
SENAL_RE = re.compile(r"^[a-z_]{1,30}:[a-z0-9_]{1,40}$")
MAX_SENALES = 20
TIMEOUT_FILTRO_S = 30.0  # el mismo tope que el proxy (`_TIMEOUT_FILTRO_S`)

# -- /v1/evaluar ---------------------------------------------------------------
MOTIVO_EVALUAR_FALLA = "El Guard no pudo evaluar el checkout."
MOTIVO_COMERCIO_DISTINTO = "El checkout lo firmó un comercio distinto del indicado."
MOTIVO_SIN_TOTAL = (
    "El checkout no tiene un total firmado exacto: no se puede evaluar sin ofertas."
)
TIMEOUT_FABRICANTE_S = 15.0  # igual que el proxy: todas las consultas de una evaluación

_T = TypeVar("_T")


async def en_hilo(ejecutor: Executor | None, funcion: Callable[..., _T], *args: Any) -> _T:
    """`funcion(*args)` en otro hilo, sin frenar el loop.

    Con `ejecutor` (los pools propios del proxy, `ejecutor_filtro` y
    `ejecutor_llm`: los usan `ProxyGuard` y, por la integración, estos
    servicios), en ese; con None, `asyncio.to_thread` como siempre. El
    contexto va copiado, igual que en `to_thread`. Si la espera se cancela
    antes de que tome un hilo, no corre. Un ejecutor cerrado lanza al
    programar: quien llama lo trata como cualquier falla (falla cerrado).
    """
    if ejecutor is None:
        return await asyncio.to_thread(funcion, *args)
    contexto = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(
        ejecutor, functools.partial(contexto.run, funcion, *args)
    )


# ---------------------------------------------------------------------------
# Filtro
# ---------------------------------------------------------------------------


def envolver_contenido(solicitud: SolicitudFiltrar) -> dict[str, Any]:
    """Lo que recibe el filtro: un dict con el contenido externo entero.

    El `Filtro` acepta cualquier dict y `extraer_textos` lo recorre completo,
    así que no hace falta adaptar el contenido a la forma de una tarea A2A. Va
    copiado: el filtro corre en otro hilo y no debe ver cambios de nadie.
    """
    return {
        "kind": "contenido_externo",
        "origen": solicitud.origen,
        "contenido": copy.deepcopy(solicitud.contenido),
    }


def _senales_seguras(senales: Any) -> list[str]:
    if not isinstance(senales, list):
        return []
    return [s for s in senales if isinstance(s, str) and SENAL_RE.fullmatch(s)][:MAX_SENALES]


def _resultado_filtro(pasa: bool, motivo: str, senales: list[str]) -> dict[str, Any]:
    return {
        "pasa": pasa,
        "status": "pasa" if pasa else "bloqueado",
        "motivo": motivo,
        "senales": senales,
    }


async def filtrar_contenido(
    filtro: Filtro | None,
    solicitud: SolicitudFiltrar,
    *,
    timeout_s: float = TIMEOUT_FILTRO_S,
    ejecutor: Executor | None = None,
) -> dict[str, Any]:
    """`{"pasa", "status", "motivo", "senales"}` para `/v1/filtrar`.

    Falla cerrado, siempre con 200 y `bloqueado`: sin filtro configurado
    (`filtro:no_configurado`), si el filtro lanza o devuelve algo que no es
    un `ResultadoFiltro` (`filtro:error`) o si tarda más de `timeout_s`
    (`filtro:timeout`). Corre en un hilo (el de `ejecutor`, si hay), como en
    el proxy: un filtro lento no frena al resto. El motivo es siempre un texto
    fijo de este módulo (ni el del filtro): la respuesta nunca devuelve nada
    del contenido recibido.
    """
    if filtro is None:
        return _resultado_filtro(False, MOTIVO_FALLA, ["filtro:no_configurado"])
    try:
        salida = await asyncio.wait_for(
            en_hilo(ejecutor, filtro, envolver_contenido(solicitud)), timeout_s
        )
        resultado = (
            salida if isinstance(salida, ResultadoFiltro) else ResultadoFiltro.model_validate(salida)
        )
    except TimeoutError:
        logger.warning("El filtro tardó más de %.1f s con contenido externo", timeout_s)
        return _resultado_filtro(False, MOTIVO_FALLA, ["filtro:timeout"])
    except Exception as e:  # noqa: BLE001 - fail-closed; sin el texto de la excepción
        logger.warning("El filtro falló con contenido externo: %s", type(e).__name__)
        return _resultado_filtro(False, MOTIVO_FALLA, ["filtro:error"])
    if resultado.pasa is True:
        return _resultado_filtro(True, MOTIVO_PASA, _senales_seguras(resultado.senales))
    return _resultado_filtro(False, MOTIVO_BLOQUEA, _senales_seguras(resultado.senales))


# ---------------------------------------------------------------------------
# Guard sin estado
# ---------------------------------------------------------------------------


def merchant_firmado(checkout_jwt: Any) -> str | None:
    """`merchant.id` del payload del checkout (None si no se lee).

    Es la identidad del comercio que firmó, la misma que usa el proxy
    (`app.comercio_firmado`). No verifica la firma: el Guard no tiene la clave
    pública del comercio y AP2 la verifica después. Da coherencia con el
    comercio indicado, no autenticidad.
    """
    _, payload = a2a.decodificar_jwt(checkout_jwt)
    merchant = payload.get("merchant") if isinstance(payload, dict) else None
    comercio = merchant.get("id") if isinstance(merchant, dict) else None
    return comercio if isinstance(comercio, str) and comercio else None


def carts_desde_checkout(checkout: dict[str, Any]) -> list[dict[str, Any]] | None:
    """El único cart que se puede armar sin ofertas: el de lo **firmado**.

    `item_label` va vacío (es texto libre del vendedor y ningún chequeo lo
    lee) y el monto es el total firmado exacto (`a2a.monto_firmado`), nunca el
    `amount` del data part, que el comercio puede poner a gusto. None si no
    hay un total firmado exacto: no hay con qué evaluar.
    """
    firmado = a2a.monto_firmado(checkout)
    cart_id = checkout.get("cart_id")
    if firmado is None or not es_id(ID_SEGURO_RE, cart_id):
        return None
    centavos, moneda = firmado
    return [{"cart_id": cart_id, "item_label": "", "amount": centavos / 100, "currency": moneda}]


def veredicto_bloqueado(cart_id: str, motivo: str) -> Veredicto:
    """Un `bloqueado` armado acá, sin chequeos ni recibo, cuando el Guard no
    llegó a decidir (o no hacía falta): nunca llega a la firma."""
    return Veredicto(
        status="bloqueado",
        cart_id_elegido=cart_id,
        cart_id_recomendado=None,
        ranking=[],
        chequeos=[],
        motivo=motivo,
    )


async def con_fabricante(
    fabricante: Any,
    catalogo: dict[str, dict],
    offer_details: list[dict[str, Any]],
    *,
    timeout_s: float = TIMEOUT_FABRICANTE_S,
    ejecutor: Executor | None = None,
) -> tuple[dict[str, dict], dict[str, Any]]:
    """El catálogo más la ficha del fabricante de cada GTIN de las ofertas, y
    el resumen de qué fuente respondió. Igual que `ProxyGuard._con_fabricante`
    (las consultas, en un hilo del `ejecutor` si hay).

    Sin fuente, sin dato, con falla o si tarda de más, el Guard no tiene fuente
    independiente y escala lo obligatorio: nunca aprueba por eso.
    """
    from fabricante import datos_externos
    from fabricante.consultas import consultar_gtins, gtins_de_ofertas, resumen
    from guard.fuentes import con_fuentes

    if fabricante is None:
        return catalogo, resumen(None, [])
    gtins = gtins_de_ofertas(offer_details, catalogo)
    try:
        resultados = await asyncio.wait_for(
            en_hilo(ejecutor, consultar_gtins, fabricante, gtins), timeout_s
        )
    except Exception as e:  # noqa: BLE001 - sin fabricante, lo obligatorio escala
        logger.warning("La fuente del fabricante no respondió: %s", type(e).__name__)
        datos = resumen(fabricante, [])
        datos["error"] = type(e).__name__
        return catalogo, datos
    return con_fuentes(catalogo, datos_externos(resultados)), resumen(fabricante, resultados)


def _validar_checkout(checkout: Any) -> tuple[CheckoutVisto, str]:
    """El checkout leído y su `cart_id`, o 400 `checkout_invalido`.

    El mensaje es fijo: el detalle de pydantic podría citar valores del
    checkout (texto del vendedor).
    """
    if not isinstance(checkout, dict):
        raise ErrorApi(400, "checkout_invalido", "checkout tiene que ser el data part ap2.checkout.")
    try:
        visto = CheckoutVisto.model_validate(checkout)
    except ValidationError:
        raise ErrorApi(
            400, "checkout_invalido", "checkout no tiene el formato de ap2.checkout."
        ) from None
    cart_id = checkout.get("cart_id")
    if not es_id(ID_SEGURO_RE, cart_id):
        raise ErrorApi(
            400, "checkout_invalido", "checkout.cart_id falta o no es un identificador válido."
        )
    return visto, cart_id


def _respuesta(
    veredicto: Veredicto, *, firmado: str | None, ts: datetime, evaluador: str
) -> dict[str, Any]:
    vista = vista_veredicto(veredicto, contexto_id=None, ts=ts, evaluador=evaluador)
    vista["comercio_firmado"] = firmado if es_id(ID_SEGURO_RE, firmado) else None
    return vista


def firmar_veredicto(
    veredicto: Veredicto, *, clave: ClaveRecibo | None, key_path: Path | None
) -> Veredicto:
    """El veredicto con el recibo firmado por `clave` (la `ClaveRecibo`
    inyectada: del entorno con varias instancias) o, si no hay, por la del
    archivo `key_path` (lo de siempre, `guard.recibo.cargar_o_crear_clave`).

    Exactamente una de las dos: las dos a la vez son ambiguas (¿con cuál se
    firmó?) y ninguna dejaría los recibos sin firma sin que nadie lo pidiera.
    Las fallas de la firma las maneja quien llama (recibo null)."""
    if (clave is None) == (key_path is None):
        raise TypeError("firmar el recibo pide clave= o key_path= (exactamente uno)")
    if clave is not None:
        return clave.firmar_veredicto(veredicto)
    return firmar_recibo(veredicto, key_path)


async def evaluar_sin_estado(
    solicitud: SolicitudEvaluar,
    *,
    evaluador: EvaluadorCargado,
    key_path: Path | None = None,
    clave: ClaveRecibo | None = None,
    client: httpx.AsyncClient,
    comercio: Comercio | None,
    politica: Politica | None,
    fabricante: Any = None,
    ahora: datetime | None = None,
    ejecutor: Executor | None = None,
) -> dict[str, Any]:
    """El veredicto del Guard para un checkout que manda el integrador.

    `vista_veredicto(...)` (sin texto del comercio, `contexto_id: null`) más
    `comercio_firmado`. Lanza `ErrorApi` 400 solo si el checkout es inválido;
    todo lo demás que impide evaluar da un `bloqueado` con motivo fijo.

    - El catálogo sale de `comercio` (nunca de una URL que mande el
      integrador). Sin comercio, se evalúa sin catálogo: todo queda "solo
      declarado" y lo obligatorio escala.
    - Un catálogo que dice ser de otro comercio que el que firmó se descarta,
      como en el proxy.
    - El comercio de la evaluación es el `merchant.id` **firmado**; si el
      comercio indicado tiene `merchant_ids` y no está, se bloquea sin evaluar.
    - Un solo instante por evaluación: el `ts` de la vista es el `ahora` del
      Guard (fecha del recibo y `exp`/`iat` del JWT). Sin `ahora`, el reloj
      se lee una vez acá.
    - `ejecutor`: dónde corren las consultas al fabricante (None = el del loop).
    - El recibo lo firma `clave` (una `ClaveRecibo`, la del entorno con varias
      instancias) o, si no se pasa, la del archivo `key_path` (lo de siempre).
      Exactamente uno de los dos (`TypeError` antes de evaluar); si la firma
      falla, el recibo va null.
    """
    if (clave is None) == (key_path is None):
        raise TypeError("evaluar_sin_estado pide clave= o key_path= (exactamente uno)")
    ts = ahora or datetime.now(UTC)
    visto, cart_id = _validar_checkout(solicitud.checkout)
    checkout = solicitud.checkout
    firmado = merchant_firmado(visto.checkout_jwt)

    def bloqueado(motivo: str) -> dict[str, Any]:
        return _respuesta(
            veredicto_bloqueado(cart_id, motivo), firmado=firmado, ts=ts, evaluador=evaluador.nombre
        )

    if comercio is not None and comercio.merchant_ids and firmado not in comercio.merchant_ids:
        logger.warning("evaluar: el checkout no lo firmó el comercio %s", comercio.id)
        return bloqueado(MOTIVO_COMERCIO_DISTINTO)

    # Sin ofertas (o sin carts), el único cart que se puede evaluar es el firmado.
    recibidas = solicitud.ofertas
    if recibidas is not None and recibidas.carts:
        carts = copy.deepcopy(recibidas.carts)
    else:
        carts = carts_desde_checkout(checkout)
        if carts is None:
            return bloqueado(MOTIVO_SIN_TOTAL)
    offer_details = copy.deepcopy(recibidas.offer_details) if recibidas is not None else []

    catalogo: dict[str, dict] = {}
    if comercio is not None:
        catalogo, comercio_catalogo, _ = await cargar_catalogo(
            client, comercio.catalogo_url, comercio.respaldo_fixture,
            skus=skus_de(offer_details),  # por SKU; si no alcanza, la lista entera
        )
        if catalogo and comercio_catalogo and firmado and comercio_catalogo != firmado:
            logger.warning(
                "evaluar: catálogo de %s descartado (el checkout lo firmó otro comercio)",
                comercio.id,
            )
            catalogo = {}
    catalogo, _ = await con_fabricante(fabricante, catalogo, offer_details, ejecutor=ejecutor)

    # Un evaluar() que no ata lo firmado no decide (el proxy no arranca con él).
    if not evaluador.acepta_checkout:
        return bloqueado(MOTIVO_EVALUAR_FALLA)
    try:
        resultado = evaluador.evaluar(
            solicitud.ficha,
            OfertasVistas(carts=carts, offer_details=offer_details, comercio_id=firmado),
            cart_id,
            catalogo,
            politica=politica,
            checkout_hash=checkout.get("checkout_hash"),
            ahora=ts,  # el mismo instante que el `ts` de la vista
            checkout=visto,  # siempre: el Guard ata lo firmado al cart
        )
        veredicto = (
            resultado
            if isinstance(resultado, Veredicto)
            else Veredicto.model_validate(
                resultado.model_dump() if hasattr(resultado, "model_dump") else resultado
            )
        )
    except Exception as e:  # noqa: BLE001 - nunca aprobar si evaluar() falla
        logger.error("evaluar() falló en /v1/evaluar: %s", type(e).__name__)
        return bloqueado(MOTIVO_EVALUAR_FALLA)

    try:
        veredicto = firmar_veredicto(veredicto, clave=clave, key_path=key_path)
    except Exception as e:  # noqa: BLE001 - sin firma, la respuesta va con recibo null
        logger.error("No se pudo firmar el recibo en /v1/evaluar: %s", type(e).__name__)
    return _respuesta(veredicto, firmado=firmado, ts=ts, evaluador=evaluador.nombre)
