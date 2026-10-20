"""De dónde saca el proxy la ficha y el catálogo público (y qué le pide a la demo).

El catálogo público no siempre existe: sin él, el Guard evalúa sin catálogo
(todo queda "solo declarado" y escala). El respaldo en `fixtures/` es solo
para la demo offline y hay que pedirlo (`CATALOGO_RESPALDO_FIXTURE=1`).
Para evaluar se piden solo los productos de las ofertas (`/products/<sku>`)
y, si eso no alcanza, la lista entera (`/products`).

Este módulo es del motor: no importa `demo_world` (ni nada de la
simulación). La ficha de los escenarios fijos y el chequeo del SDK de AP2
llegan por inyección (`FuentesDemoProtocolo`; la implementación de la demo
local es `proxy.demo_local.FuentesDemo`, que pasa `python -m proxy`).

La categoría de la taxonomía de Google de la ficha sale del clasificador del
agente o, si no hay, de las palabras del pedido (`taxonomia.palabras`): los
nombres que se muestran son los del árbol (nuestros), nunca texto del
comercio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from contracts import Ficha
from proxy.config import REPO_ROOT

logger = logging.getLogger("proxy.fuentes")

FIXTURES = REPO_ROOT / "fixtures"


class FuentesDemoProtocolo(Protocol):
    """Lo que el proxy le pide a la demo local (`create_app(..., demo=)`).

    Sin `demo` (el motor desplegado) no hay ficha de los escenarios fijos
    (como `PROXY_FICHA_DEMO=0`) ni `ap2_violaciones` (va en null). La de la
    demo es `proxy.demo_local.FuentesDemo`. Ninguno de los dos autoriza nada:
    la ficha de la demo es la que confirmó el usuario de la grabación y AP2
    es solo para mostrar.
    """

    def intent_del_escenario(self, nombre: str | None) -> dict[str, Any] | None:
        """El `intent` confirmado de ese escenario fijo, o None."""
        ...

    def violaciones_ap2(
        self, cart: dict[str, Any], intent: dict[str, Any] | None
    ) -> list[str] | None:
        """Lo que frena el SDK de AP2 ([] = OK), o None si no se pudo."""
        ...


def ficha_del_intent(intent: dict[str, Any]) -> Ficha:
    """La ficha confirmada de un `intent` de la demo, con los atributos
    (`intent.attributes`) y la categoría de la taxonomía de Google
    (`google_product_category`): `guard.ficha.ficha_desde_intent`.

    Se importa al usarla: si `guard/` no está, el proxy ya no arranca por el
    evaluador (fail-closed) y el error que se ve es ese.
    """
    from guard.ficha import ficha_desde_intent

    return ficha_desde_intent(intent)


# -- categoría de la taxonomía de Google ----------------------------------------------


def _arbol() -> Any:
    """El subconjunto de la taxonomía del repo (el mismo que usa el Guard)."""
    from taxonomia import cargar_subconjunto

    return cargar_subconjunto()


def _id_del_arbol(valor: Any) -> str | None:
    if not isinstance(valor, str) or not valor.isdigit():
        return None
    return valor if valor in _arbol() else None


def ruta_categoria(id_categoria: Any) -> str | None:
    """La ruta del árbol ("Electronics > Computers > Laptops"), o None."""
    valido = _id_del_arbol(id_categoria)
    return _arbol()[valido].ruta_texto if valido else None


def categoria_nombrada(textos: Iterable[str]) -> str | None:
    """La categoría que nombran los textos, en orden: el primero que nombre
    una manda (el pedido antes que las respuestas: "un monitor" + "para la
    notebook" es un monitor)."""
    # `nombradas` saltea complementos ("mochila para la notebook" es una
    # mochila, no una notebook); devuelve de la mejor a la peor.
    from taxonomia.palabras import nombradas

    arbol = _arbol()
    for texto in textos:
        candidatas = nombradas(texto or "", arbol) if isinstance(texto, str) else []
        categoria = candidatas[0][0] if candidatas else None
        if categoria is not None and categoria in arbol:
            return categoria
    return None


def categoria_de_la_ficha(propuesta: Any, textos: Iterable[str]) -> str | None:
    """`Ficha.categoria_taxonomia` de una compra, sin modelo.

    `propuesta` es lo que devolvió el clasificador del agente (puede ser
    None o cualquier cosa): solo cuenta un ID del árbol. Si no hay, la de las
    palabras de `textos`. Si están las dos:

    - en la misma rama, la más específica;
    - sin relación de rama, la de las palabras: el pedido nombra lo que se
      compra ("notebooks" es Laptops) y una propuesta de otra rama (Desktop
      Computers) hacía que el Guard escalara las notebooks de verdad.
    """
    clasificada = _id_del_arbol(propuesta)
    palabras = categoria_nombrada(textos)
    if clasificada is None or palabras is None:
        return clasificada or palabras
    arbol = _arbol()
    if arbol.es_descendiente(clasificada, palabras):
        return clasificada  # la propuesta es igual o más específica
    return palabras  # más específica en la rama, o de otra rama


def categorias_para_el_pedido(catalogo: Mapping[str, Any], texto: str) -> list[str]:
    """Las categorías del catálogo público para el intake, primero las del
    pedido: las de productos en la rama de su categoría de la taxonomía o
    las que nombra el pedido; después el resto, en orden alfabético.

    El intake ofrece como mucho 40 (`intencion.intake.MAX_CATEGORIAS`): con
    un catálogo grande, la pedida no queda afuera por el orden alfabético.
    """
    from taxonomia.palabras import normalizar

    productos = [p for p in catalogo.values() if isinstance(p, dict)]
    todas = sorted({p["category"] for p in productos if isinstance(p.get("category"), str)})
    pedida = categoria_nombrada([texto])
    arbol = _arbol()
    en_rama = {
        p["category"]
        for p in productos
        if pedida is not None
        and isinstance(p.get("category"), str)
        and _id_del_arbol(p.get("categoria_taxonomia")) is not None
        and arbol.es_descendiente(p["categoria_taxonomia"], pedida)
    }
    palabras = f" {normalizar(texto)} "
    nombradas = {
        c for c in todas if normalizar(c) and f" {normalizar(c)} " in palabras
    }
    primero = [c for c in todas if c in en_rama or c in nombradas]
    return primero + [c for c in todas if c not in primero]


def _indexar(catalogo: dict[str, Any]) -> tuple[dict[str, dict], str | None]:
    lista = catalogo.get("products")
    productos = {
        p["sku"]: p
        for p in (lista if isinstance(lista, list) else [])
        if isinstance(p, dict) and isinstance(p.get("sku"), str)
    }
    merchant = catalogo.get("merchant") or {}
    comercio_id = merchant.get("id") if isinstance(merchant, dict) else None
    return productos, comercio_id if isinstance(comercio_id, str) else None


MAX_CATALOGO_BYTES = 2 * 1024 * 1024
MAX_PRODUCTO_BYTES = 256 * 1024
MAX_SKUS_POR_PEDIDO = 20  # más que esto, la lista entera (como antes)
_TIMEOUT_CATALOGO_S = 5.0
# Un SKU que va en la URL: solo un identificador simple (nada de "/", "..",
# "?" ni espacios). Uno que no lo es no se pide por SKU: la lista entera.
_SKU_EN_URL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def skus_de(offer_details: Iterable[Any] | None) -> list[str]:
    """Los SKU que declaran las ofertas (`demo.offer_details`), sin repetir:
    los únicos que el Guard busca en el catálogo."""
    skus: list[str] = []
    for detalle in offer_details or []:
        sku = detalle.get("sku") if isinstance(detalle, dict) else None
        if isinstance(sku, str) and sku and sku not in skus:
            skus.append(sku)
    return skus


class _NoAlcanza(Exception):
    """El pedido por SKU no sirve para esta evaluación: se pide la lista entera."""


async def _producto(
    client: httpx.AsyncClient, catalogo_url: str, sku: str
) -> tuple[dict[str, Any], str | None] | None:
    """`GET <catalogo>/products/<sku>` → (producto sin `merchant`, merchant.id).

    None si da 404 (el SKU no está publicado). Cualquier otra cosa que no sea
    el producto de ese SKU es una falla (`_NoAlcanza`)."""
    try:
        resp = await client.get(
            f"{catalogo_url}/products/{quote(sku, safe='')}", timeout=_TIMEOUT_CATALOGO_S
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        if resp.status_code != 200 or len(resp.content) > MAX_PRODUCTO_BYTES:
            raise ValueError(f"respuesta {resp.status_code} de {len(resp.content)} bytes")
        datos = json.loads(resp.content, parse_constant=_sin_constantes)
    except (httpx.HTTPError, ValueError, RecursionError) as e:
        raise _NoAlcanza(f"{sku}: {type(e).__name__}") from e
    if not isinstance(datos, dict) or datos.get("sku") != sku:
        raise _NoAlcanza(f"{sku}: no devolvió el producto de ese SKU")
    producto = dict(datos)
    merchant = producto.pop("merchant", None)
    comercio_id = merchant.get("id") if isinstance(merchant, dict) else None
    return producto, comercio_id if isinstance(comercio_id, str) and comercio_id else None


async def _cargar_por_sku(
    client: httpx.AsyncClient, catalogo_url: str, skus: Iterable[Any]
) -> tuple[dict[str, dict], str] | None:
    """Los productos de esos SKU, en paralelo, con el comercio del catálogo.

    None ("no alcanza", la lista entera) si hay más de `MAX_SKUS_POR_PEDIDO`,
    si alguno no va en una URL, si alguno falla, si no se encontró ninguno
    (puede ser un catálogo sin `/products/<sku>`) o si los productos no dicen
    de qué comercio son, o dicen dos distintos: así el chequeo "un catálogo
    de otro comercio no se usa" sigue valiendo.
    """
    pedidos = list(dict.fromkeys(skus))
    if len(pedidos) > MAX_SKUS_POR_PEDIDO or not all(
        isinstance(s, str) and _SKU_EN_URL.fullmatch(s) for s in pedidos
    ):
        return None
    respuestas = await asyncio.gather(
        *(_producto(client, catalogo_url, s) for s in pedidos), return_exceptions=True
    )
    for respuesta in respuestas:
        if isinstance(respuesta, asyncio.CancelledError):
            raise respuesta
        if isinstance(respuesta, BaseException):
            # Cualquier falla (también una inesperada): la lista entera, que
            # tiene su propio fail-closed.
            logger.info("Catálogo por SKU: %r; pido la lista entera", respuesta)
            return None
    encontrados = {s: r for s, r in zip(pedidos, respuestas) if r is not None}
    comercios = {comercio for _, comercio in encontrados.values()}
    if not encontrados or len(comercios) != 1 or None in comercios:
        return None
    return {s: producto for s, (producto, _) in encontrados.items()}, comercios.pop()


async def cargar_catalogo(
    client: httpx.AsyncClient,
    catalogo_url: str | None,
    respaldo_fixture: bool = False,
    skus: Iterable[Any] | None = None,
) -> tuple[dict[str, dict], str | None, str]:
    """El catálogo público → (sku -> producto, merchant.id, fuente).

    Con `skus` (los de las ofertas que se evalúan, `skus_de`), primero
    `GET <catalogo>/products/<sku>` por cada uno, en paralelo (hasta
    `MAX_SKUS_POR_PEDIDO`); si eso no alcanza (`_cargar_por_sku`), o sin
    `skus`, `GET <catalogo>/products` entero, como siempre.

    `fuente` es "http", "fixture" o "ninguno":
    - sin `catalogo_url` (el comercio no publica catálogo) no se consulta nada;
    - si el catálogo no responde (o responde algo ilegible) se evalúa sin
      catálogo, salvo que se pida el respaldo de `fixtures/` para la demo.
    """
    if not catalogo_url:
        return {}, None, "ninguno"
    if skus:
        por_sku = await _cargar_por_sku(client, catalogo_url, skus)
        if por_sku is not None:
            productos, comercio_id = por_sku
            return productos, comercio_id, "http"
    try:
        resp = await client.get(f"{catalogo_url}/products", timeout=_TIMEOUT_CATALOGO_S)
        resp.raise_for_status()
        if len(resp.content) > MAX_CATALOGO_BYTES:
            raise ValueError("el catálogo es demasiado grande")
        datos = json.loads(resp.content, parse_constant=_sin_constantes)
        if not isinstance(datos, dict):
            raise ValueError("el catálogo no devolvió un objeto")
        productos, comercio_id = _indexar(datos)
        return productos, comercio_id, "http"
    except (httpx.HTTPError, ValueError, RecursionError) as e:
        if not respaldo_fixture:
            logger.warning("Catálogo público no disponible (%s); evalúo sin catálogo", e)
            return {}, None, "ninguno"
        logger.warning("Catálogo público no disponible (%s); uso fixtures/ (demo)", e)
        datos = json.loads((FIXTURES / "public_catalog.json").read_text("utf-8"))
        productos, comercio_id = _indexar(datos)
        return productos, comercio_id, "fixture"


def _sin_constantes(nombre: str) -> None:
    raise ValueError(f"constante JSON no estándar en el catálogo: {nombre}")


def intent_ap2(ficha: Ficha) -> dict[str, Any] | None:
    """El mandato de AP2 de una compra (`{"hard": {"max_price", "currency"}}`,
    lo que lee `demo_world.ap2_check`) con el tope y la moneda obligatorios de
    su ficha. None si la ficha no los tiene: no hay mandato que chequear."""
    tope = next(
        (r.valor for r in ficha.requisitos
         if r.obligatorio and r.campo == "precio" and r.operador == "maximo"),
        None,
    )
    moneda = next(
        (r.valor for r in ficha.requisitos
         if r.obligatorio and r.campo == "moneda" and r.operador == "igual"),
        None,
    )
    if isinstance(tope, bool) or not isinstance(tope, (int, float)) or not isinstance(moneda, str):
        return None
    return {"hard": {"max_price": float(tope), "currency": moneda}}
