"""Prueba sin LLM por `/v1`: una compra de integrador de punta a punta.

Crea la compra con `POST /v1/compras` (la ficha que autorizó el usuario:
tope, moneda, cantidad y `categoria_taxonomia`), busca (`find_products`) y
pide el checkout (`create_checkout`) por A2A con `X-Compra-Id`, como el
agente de un integrador (docs/INTEGRACION.md, §5), e imprime el `status` del
`guard.veredicto`.

Por defecto arma un **gateway de prueba en este proceso**, sin abrir puertos:
el mismo `create_app` de `python -m proxy`, con una clave de API temporal
(se genera al arrancar y no se guarda en ningún archivo), el Guard real, el
filtro solo con heurísticas y sin agente de intención: ningún LLM de nuestro
lado. Habla con el comercio y el catálogo que estén levantados, por ejemplo
con `bash deploy/start_all.sh general` (:8001 y :8011). El `create_checkout`
del comercio del sample pasa por su propio LLM (necesita su GOOGLE_API_KEY).

Desde la raíz del repo:

    PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python \\
        -m proxy.scripts.smoke_v1_general --pedido "un monitor de 27 pulgadas" \\
        --tope 400 --categoria 305 --escenario general --esperado aprobado

Contra un gateway ya levantado (con `GATEWAY_CLAVES_ARCHIVO`), con su clave:

    KAIAI_CLAVE=psg_… ... -m proxy.scripts.smoke_v1_general \\
        --gateway http://localhost:8030 --comercio merchant_1

El cart es el más barato dentro del tope (o `--cart`; si ninguno entra, el
más barato: así el Guard igual evalúa). Sale con 0 si llegó el veredicto, el
checkout llegó solo con `aprobado`/`con_avisos` y, con `--esperado`, el
status es ese.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from contracts import GUARD_VEREDICTO_KEY

EXTENSION_AP2 = "https://github.com/google-agentic-commerce/ap2/v1"
MERCHANT_URL = "http://127.0.0.1:8001/a2a/merchant_agent"
CATALOGO_URL = "http://127.0.0.1:8011"
# El oyente público del gateway de prueba: nunca se abre el puerto (ASGI en
# este proceso); el puerto de la URL es el que reconoce el middleware.
BASE_PRUEBA = "http://gw.prueba:8030"
INTEGRADOR = "smoke"
STATUS = ("aprobado", "con_avisos", "escalar", "bloqueado")
LLEGAN_A_FIRMA = ("aprobado", "con_avisos")
TIMEOUT = httpx.Timeout(600.0, connect=10.0)  # create_checkout usa el LLM del comercio

Salida = Callable[[str], None]


def ficha_de(args: argparse.Namespace) -> dict[str, Any]:
    """La ficha de la compra: lo que autorizó el usuario (tope obligatorio)."""
    requisitos: list[dict[str, Any]] = [
        {"campo": "precio", "operador": "maximo", "valor": args.tope, "obligatorio": True},
        {"campo": "moneda", "operador": "igual", "valor": args.moneda, "obligatorio": True},
    ]
    if args.cantidad:
        requisitos.append(
            {"campo": "cantidad", "operador": "minimo", "valor": args.cantidad, "obligatorio": True}
        )
    ficha: dict[str, Any] = {"requisitos": requisitos}
    if args.categoria:
        ficha["categoria_taxonomia"] = args.categoria
    return ficha


def mensaje_rpc(partes: list[dict[str, Any]], texto: str, contexto: str | None = None) -> dict:
    """Un `message/send` como lo arma el agente del cliente del sample."""
    message: dict[str, Any] = {
        "kind": "message",
        "messageId": uuid.uuid4().hex,
        "role": "agent",
        "parts": [{"kind": "text", "text": texto}, *({"kind": "data", "data": d} for d in partes)],
    }
    if contexto:
        message["contextId"] = contexto
    return {
        "id": uuid.uuid4().hex,
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {"message": message, "configuration": {"blocking": True}},
    }


def datos_de(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        parte["data"]
        for artefacto in task.get("artifacts") or []
        for parte in artefacto.get("parts") or []
        if isinstance(parte, dict) and isinstance(parte.get("data"), dict)
    ]


def texto_de(task: dict[str, Any]) -> str:
    mensaje = (task.get("status") or {}).get("message") or {}
    return " ".join(p.get("text", "") for p in mensaje.get("parts") or [] if isinstance(p, dict))


def elegir_cart(carts: list[dict[str, Any]], tope: float, moneda: str) -> tuple[dict | None, str]:
    """El más barato dentro del tope y en la moneda; si ninguno, el más barato."""
    validos = [
        c for c in carts
        if isinstance(c.get("amount"), (int, float)) and not isinstance(c["amount"], bool)
    ]
    if not validos:
        return None, "ningún cart con monto"
    dentro = [c for c in validos if c["amount"] <= tope and c.get("currency") == moneda]
    if dentro:
        return min(dentro, key=lambda c: c["amount"]), "el más barato dentro del tope"
    return min(validos, key=lambda c: c["amount"]), (
        "ninguno entra en el tope y la moneda: el más barato"
    )


async def _rpc(
    http: httpx.AsyncClient, ruta: str, compra_id: str, pedido: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """(tarea, error). El error es el del JSON-RPC o del gateway, si hubo."""
    resp = await http.post(
        ruta,
        content=json.dumps(pedido).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "X-Compra-Id": compra_id,
            "X-A2A-Extensions": EXTENSION_AP2,
        },
    )
    try:
        cuerpo = resp.json()
    except ValueError:
        return None, f"HTTP {resp.status_code}: la respuesta no es JSON"
    if not isinstance(cuerpo, dict):
        return None, f"HTTP {resp.status_code}: la respuesta no es un objeto"
    if resp.status_code >= 400 or "error" in cuerpo:
        error = cuerpo.get("error")
        return None, f"HTTP {resp.status_code}: {json.dumps(error, ensure_ascii=False)[:300]}"
    resultado = cuerpo.get("result")
    if not isinstance(resultado, dict):
        return None, "el JSON-RPC no trajo una tarea"
    return resultado, None


async def flujo(http: httpx.AsyncClient, args: argparse.Namespace, salida: Salida = print) -> int:
    """Compra → búsqueda → checkout por un gateway (`http` ya autenticado)."""
    fallas: list[str] = []
    cuerpo = {
        "pedido": args.pedido,
        "comercio": args.comercio,
        "ficha": ficha_de(args),
        "referencia": "smoke-v1-general",
    }
    resp = await http.post("/v1/compras", json=cuerpo)
    if resp.status_code != 201:
        salida(f"POST /v1/compras → {resp.status_code} {resp.text[:300]}")
        return _fin(["no se pudo crear la compra"], salida)
    compra = resp.json()
    compra_id = compra["compra_id"]
    ruta = urlsplit(compra["a2a_url"]).path
    ficha = compra.get("ficha") or {}
    salida(f"compra {compra_id}: estado={compra['estado']} comercio={compra['comercio']}")
    requisitos = [(r["campo"], r["operador"], r["valor"]) for r in ficha.get("requisitos", [])]
    salida(f"  categoria_taxonomia={ficha.get('categoria_taxonomia')} requisitos={requisitos}")

    partes: list[dict[str, Any]] = [
        {"catalog_search": args.pedido},
        {"risk_data": "smoke_risk_data"},
        {"shopping_agent_id": "trusted_shopping_agent"},
    ]
    if args.escenario:
        partes.append({"demo_scenario": args.escenario})
    busqueda, error = await _rpc(
        http, ruta, compra_id, mensaje_rpc(partes, "Find products that match the user's request.")
    )
    if busqueda is None:
        salida(f"find_products: {error}")
        return _fin(["find_products falló"], salida)
    estado = (busqueda.get("status") or {}).get("state")
    contexto = busqueda.get("contextId")
    carts = [d["ap2.cart"] for d in datos_de(busqueda) if isinstance(d.get("ap2.cart"), dict)]
    salida(f"find_products: estado={estado} contexto={contexto}")
    salida(f"  carts: {[(c.get('cart_id'), c.get('amount'), c.get('currency')) for c in carts]}")
    if estado != "completed" or not carts or not contexto:
        if texto_de(busqueda):
            salida(f"  mensaje: {texto_de(busqueda)}")
        return _fin(["find_products no devolvió carts"], salida)

    if args.cart:
        # El comercio general suma un sufijo por contexto (`cart_1_43a28e`):
        # `--cart cart_1` elige ese, como en los escenarios fijos.
        cart = next(
            (c for c in carts if c.get("cart_id") == args.cart
             or str(c.get("cart_id", "")).startswith(f"{args.cart}_")),
            None,
        )
        por_que = "el pedido por --cart"
        if cart is None:
            return _fin([f"no hay {args.cart} entre los carts"], salida)
    else:
        cart, por_que = elegir_cart(carts, args.tope, args.moneda)
        if cart is None:
            return _fin([por_que], salida)
    salida(f"elijo {cart['cart_id']} ({por_que})")

    pedido_checkout = mensaje_rpc(
        [{"cart_id": cart["cart_id"]}, {"shopping_agent_id": "trusted_shopping_agent"}],
        "Create a checkout for the selected cart.", contexto,
    )
    tarea, error = await _rpc(http, ruta, compra_id, pedido_checkout)
    if tarea is None:
        salida(f"create_checkout: {error}")
        return _fin(["create_checkout falló"], salida)
    datos = datos_de(tarea)
    veredictos = [d[GUARD_VEREDICTO_KEY] for d in datos if GUARD_VEREDICTO_KEY in d]
    checkouts = [d["ap2.checkout"] for d in datos if "ap2.checkout" in d]
    salida(f"create_checkout: estado={(tarea.get('status') or {}).get('state')}")
    if not veredictos:
        return _fin(["la tarea no trajo guard.veredicto"], salida)
    veredicto = veredictos[0]
    status = veredicto.get("status")
    salida(f"status={status}")
    salida(f"  motivo: {veredicto.get('motivo')}")
    for chequeo in veredicto.get("chequeos") or []:
        if chequeo.get("resultado") != "ok":
            salida(f"  chequeo {chequeo.get('tipo')}: {chequeo.get('resultado')}")
    salida(f"  ap2.checkout: {'llegó (para firmar)' if checkouts else 'no llegó'}")
    if status in LLEGAN_A_FIRMA and not checkouts:
        fallas.append(f"{status} sin ap2.checkout")
    if status not in LLEGAN_A_FIRMA and checkouts:
        fallas.append(f"{status} con el ap2.checkout adentro")
    vista = (await http.get(f"/v1/compras/{compra_id}")).json()
    ultimo = (vista.get("veredictos") or [{}])[-1]
    salida(f"GET /v1/compras/{compra_id}: estado={vista.get('estado')} "
           f"veredicto={ultimo.get('status')} recibo={(ultimo.get('recibo') or {}).get('id')}")
    if args.esperado and status != args.esperado:
        fallas.append(f"se esperaba {args.esperado} y llegó {status}")
    if args.json:
        salida(json.dumps({"compra": vista, "tarea": tarea}, ensure_ascii=False, indent=2))
    return _fin(fallas, salida)


def _fin(fallas: list[str], salida: Salida) -> int:
    for falla in fallas:
        salida(f"FALLA: {falla}")
    if fallas:
        return 1
    salida("OK")
    return 0


@asynccontextmanager
async def gateway_de_prueba(
    args: argparse.Namespace,
    *,
    transporte: httpx.AsyncBaseTransport | None = None,
    evaluador: Any = None,
):
    """Un gateway en este proceso con una clave temporal → cliente autenticado.

    `transporte` lleva al comercio y al catálogo (None: la red, los que estén
    levantados); `evaluador`, solo en los tests (si no, el Guard real: sin él
    no arranca). Los eventos y la clave del recibo van a una carpeta
    temporal: nada queda en `.logs/` ni en `.temp-db/`.
    """
    from filtro.nucleo import FiltroSeguridad
    from proxy.app import create_app
    from proxy.claves import RegistroIntegradores, generar_clave
    from proxy.comercios import RegistroComercios
    from proxy.config import Config
    from proxy.evaluador import cargar_evaluador
    from proxy.tipos_gateway import Comercio

    clave, kid, sha = generar_clave()
    integradores = RegistroIntegradores.desde_dict(
        {"integradores": [{
            "id": INTEGRADOR,
            "claves": [{"kid": kid, "sha256": sha, "activa": True}],
            "comercios": [args.comercio],
        }]},
        {},
    )
    comercios = RegistroComercios(
        [Comercio(
            id=args.comercio, nombre="Comercio de la prueba", a2a_url=args.merchant_url,
            catalogo_url=args.catalogo_url or None, merchant_ids=tuple(args.merchant_id),
        )],
        args.comercio,
    )
    with tempfile.TemporaryDirectory(prefix="kaiai-smoke-") as tmp:
        base = Path(tmp)
        config = Config(
            merchant_upstream_url=args.merchant_url,
            catalogo_url=args.catalogo_url or None,
            events_jsonl=base / "guard_events.jsonl",
            guard_key_path=base / "guard_signing_key.pem",
            api_token_path=None,
            gateway_public_url=BASE_PRUEBA,
        )
        app = create_app(
            config,
            transport=transporte,
            evaluador=evaluador or cargar_evaluador(),
            filtro=FiltroSeguridad(solo_heuristicas=True),
            filtro_modo="solo heurísticas",
            fabricante=_fabricante(),
            integradores=integradores,
            comercios=comercios,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE_PRUEBA, timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {clave}"},
            ) as http:
                yield http


def _fabricante() -> Any:
    """La fuente del fabricante de `FABRICANTE_FUENTE` (por defecto, el fixture)."""
    from proxy.__main__ import cargar_fabricante

    return cargar_fabricante()


async def correr(
    args: argparse.Namespace,
    *,
    transporte: httpx.AsyncBaseTransport | None = None,
    evaluador: Any = None,
    salida: Salida = print,
) -> int:
    from proxy.evaluador import EvaluadorNoDisponible

    try:
        if args.gateway:
            clave = args.clave or os.environ.get("KAIAI_CLAVE")
            if not clave:
                return _fin(["con --gateway hace falta la clave (--clave o KAIAI_CLAVE)"], salida)
            async with httpx.AsyncClient(
                base_url=args.gateway.rstrip("/"), timeout=TIMEOUT, trust_env=False,
                transport=transporte, headers={"Authorization": f"Bearer {clave}"},
            ) as http:
                return await flujo(http, args, salida)
        salida(f"gateway de prueba en este proceso → comercio {args.merchant_url}, "
               f"catálogo {args.catalogo_url or '(ninguno)'}")
        async with gateway_de_prueba(args, transporte=transporte, evaluador=evaluador) as http:
            return await flujo(http, args, salida)
    except EvaluadorNoDisponible as e:  # fail-closed: sin el Guard real no hay prueba
        return _fin([f"el gateway de prueba no arranca: {e}"], salida)
    except httpx.HTTPError as e:
        return _fin([f"no se pudo hablar con el gateway ({type(e).__name__}: {e})"], salida)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--pedido", default="un monitor de 27 pulgadas")
    p.add_argument("--tope", type=float, default=400.0, help="precio máximo (obligatorio)")
    p.add_argument("--moneda", default="USD")
    p.add_argument("--cantidad", type=int, default=1, help="0: sin requisito de cantidad")
    p.add_argument("--categoria", default="305",
                   help="ID de la taxonomía de Google (305 = monitores); vacío: sin categoría")
    p.add_argument("--escenario", default="general",
                   help="demo_scenario para el comercio de la demo; vacío: no se manda")
    p.add_argument("--cart", default=None, help="cart a pedir (si no, el más barato en el tope)")
    p.add_argument("--esperado", choices=STATUS, default=None)
    p.add_argument("--comercio", default="merchant_1", help="id del comercio en el gateway")
    p.add_argument("--merchant-url", default=MERCHANT_URL, help="gateway de prueba: el comercio")
    p.add_argument("--merchant-id", action="append", default=None,
                   help="gateway de prueba: merchant.id firmados válidos (merchant_1)")
    p.add_argument("--catalogo-url", default=CATALOGO_URL,
                   help="gateway de prueba: el catálogo público; vacío: sin catálogo")
    p.add_argument("--gateway", default=None, help="URL de un gateway ya levantado")
    p.add_argument("--clave", default=None, help="clave psg_… del gateway (o KAIAI_CLAVE)")
    p.add_argument("--json", action="store_true", help="imprime la compra y la tarea")
    return p


def leer_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = parser().parse_args(argv)
    args.merchant_id = args.merchant_id or ["merchant_1"]
    return args


def main(argv: list[str] | None = None) -> None:
    sys.exit(asyncio.run(correr(leer_args(argv))))


if __name__ == "__main__":
    main()
