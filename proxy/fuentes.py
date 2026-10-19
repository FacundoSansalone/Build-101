"""De dónde saca el proxy la ficha, el catálogo público y el chequeo de AP2.

El catálogo público no siempre existe: sin él, el Guard evalúa sin catálogo
(todo queda "solo declarado" y escala). El respaldo en `fixtures/` es solo
para la demo offline y hay que pedirlo (`CATALOGO_RESPALDO_FIXTURE=1`).
Para evaluar se piden solo los productos de las ofertas (`/products/<sku>`)
y, si eso no alcanza, la lista entera (`/products`).
`demo_world` se importa en solo lectura (no se modifica).

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
from typing import Any
from urllib.parse import quote

import httpx

from contracts import Ficha
from proxy.config import REPO_ROOT

logger = logging.getLogger("proxy.fuentes")

FIXTURES = REPO_ROOT / "fixtures"


def cargar_intent() -> dict[str, Any]:
    """`demo_world.scenario.intent()`; si no se puede, el del fixture baseline."""
    try:
        from demo_world import scenario

        return scenario.intent()
    except Exception as e:  # noqa: BLE001 - cualquier falla cae al fixture
        logger.warning("demo_world no disponible (%s); intent desde fixtures/", e)
        datos = json.loads((FIXTURES / "scenario_baseline.json").read_text("utf-8"))
        return datos["intent"]


def ficha_del_intent(intent: dict[str, Any]) -> Ficha:
    """La ficha confirmada de un `intent` de la demo, con los atributos
    (`intent.attributes`) y la categoría de la taxonomía de Google
    (`google_product_category`): `guard.ficha.ficha_desde_intent`.

    Se importa al usarla: si `guard/` no está, el proxy ya no arranca por el
    evaluador (fail-closed) y el error que se ve es ese.
    """
    from guard.ficha import ficha_desde_intent

    return ficha_desde_intent(intent)


_NOMBRE_ESCENARIO = re.compile(r"[A-Za-z0-9_-]{1,40}")

# Los escenarios de zapatillas de la etapa 1: en scenarios.json no tienen
# `intent` propio y usan el pedido global. Ningún otro escenario cae a ese
# pedido (un test lo verifica contra scenarios.json).
ESCENARIOS_DEL_PEDIDO_GLOBAL = frozenset({"baseline", "A", "B", "B2", "C"})
# El comercio general arma las ofertas con el pedido del usuario: nunca hay
# ficha de la demo, aunque el preset traiga un `intent`.
TIPO_GENERAL = "general"


def intent_del_escenario(nombre: str | None) -> dict[str, Any] | None:
    """La ficha confirmada de la demo para ese escenario, o None.

    1. `demo_world`: el `intent` propio del escenario (si world/ lo define)
       o, solo para los de zapatillas (`ESCENARIOS_DEL_PEDIDO_GLOBAL`), el
       `intent` global de scenarios.json.
    2. Si no: `fixtures/scenario_<nombre>.json` y `fixtures/etapa2/<nombre>.json`
       (cada fixture trae su `intent`).

    None si el escenario no se conoce (con un comercio real no hay
    `demo_scenario` y manda la ficha que confirmó el usuario), si es del
    comercio general (`"tipo": "general"`) o si no tiene `intent` propio y no
    es de zapatillas: nunca se usa el pedido de otro escenario.
    El nombre viene en datos A2A: solo se acepta un identificador simple.
    """
    if not isinstance(nombre, str) or not _NOMBRE_ESCENARIO.fullmatch(nombre):
        return None
    try:
        from demo_world import scenario

        if nombre in scenario.scenario_names():
            info = scenario.get_scenario(nombre)
            if nombre in ESCENARIOS_DEL_PEDIDO_GLOBAL and _sin_intent_propio(info):
                return scenario.intent()
            return _intent_propio(info)
    except Exception as e:  # noqa: BLE001 - cualquier falla cae a los fixtures
        logger.warning("demo_world no disponible (%s); intent desde fixtures/", e)
    for ruta in (FIXTURES / f"scenario_{nombre}.json", FIXTURES / "etapa2" / f"{nombre}.json"):
        if ruta.is_file():
            try:
                datos = json.loads(ruta.read_text("utf-8"))
            except (OSError, ValueError):
                return None
            return _intent_propio(datos)
    return None


def _sin_intent_propio(info: Any) -> bool:
    return isinstance(info, dict) and "tipo" not in info and not isinstance(
        info.get("intent"), dict
    )


def _intent_propio(info: Any) -> dict[str, Any] | None:
    """El `intent` del escenario (o del fixture), salvo en el comercio general."""
    if not isinstance(info, dict) or info.get("tipo") == TIPO_GENERAL:
        return None
    intent = info.get("intent")
    return intent if isinstance(intent, dict) else None


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
    from taxonomia import categoria_por_palabras as por_palabras

    arbol = _arbol()
    for texto in textos:
        categoria = por_palabras(texto or "", arbol) if isinstance(texto, str) else None
        if categoria is not None and categoria in arbol:
            return categoria
    return None


def categoria_de_la_ficha(propuesta: Any, textos: Iterable[str]) -> str | None:
    """`Ficha.categoria_taxonomia` de una compra, sin modelo.

    `propuesta` es lo que devolvió el clasificador del agente (puede ser
    None o cualquier cosa): solo cuenta un ID del árbol. Si no hay, la de las
    palabras de `textos`. Si las dos están y la de las palabras es
    descendiente de la propuesta, la más específica.
    """
    clasificada = _id_del_arbol(propuesta)
    palabras = categoria_nombrada(textos)
    if clasificada is None:
        return palabras
    if palabras is not None and _arbol().es_descendiente(
        palabras, clasificada, incluir_igual=False
    ):
        return palabras
    return clasificada


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


def violaciones_ap2(cart: dict[str, Any], intent: dict[str, Any] | None) -> list[str] | None:
    """Lo que frena el SDK de AP2 para este cart con el tope de su compra
    ([] = OK). None si no se pudo, o si la compra no tiene tope (`intent`
    None): nunca se usa el tope de otro pedido."""
    if intent is None:
        return None
    try:
        from demo_world.ap2_check import ap2_violations

        return list(ap2_violations(cart, intent))
    except Exception as e:  # noqa: BLE001 - es solo para mostrar
        logger.warning("No se pudo correr ap2_check para %s: %s", cart.get("cart_id"), e)
        return None
