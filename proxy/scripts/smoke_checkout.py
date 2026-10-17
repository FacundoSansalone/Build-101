"""Prueba real: find_products + create_checkout a través del proxy.

Usa el mismo cliente A2A del sample que el Shopping Agent
(`PaymentRemoteA2aClient`) y los mismos mensajes que sus tools. Necesita
levantados el comercio (:8001, con GOOGLE_API_KEY: `create_checkout` pasa
por el LLM del sample), el catálogo (:8011) y el proxy (:8010).

Desde la raíz del repo:

    PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python \\
        -m proxy.scripts.smoke_checkout --escenario B --cart cart_4 --esperado failed

Sale con código 0 si todo lo que se chequea da bien.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from contracts import GUARD_VEREDICTO_KEY

PROXY_URL = "http://localhost:8010/a2a/merchant_agent"


def _data_parts(task: Any) -> list[dict[str, Any]]:
    datos = []
    for artifact in task.artifacts or []:
        for part in artifact.parts:
            data = getattr(part.root, "data", None)
            if isinstance(data, dict):
                datos.append(data)
    return datos


def _texto_estado(task: Any) -> str:
    message = task.status.message
    if not message:
        return ""
    return " ".join(getattr(p.root, "text", "") for p in message.parts)


async def correr(args: argparse.Namespace) -> int:
    from common.a2a_extension_utils import EXTENSION_URI
    from common.a2a_message_builder import A2aMessageBuilder
    from common.payment_remote_a2a_client import PaymentRemoteA2aClient

    fallas: list[str] = []
    client = PaymentRemoteA2aClient(
        name="merchant_agent",
        base_url=args.merchant_url,
        required_extensions={EXTENSION_URI},
    )
    card = await client.get_agent_card()
    print(f"Agent Card: url={card.url}")
    if card.url != args.merchant_url:
        fallas.append(f"la card apunta a {card.url}, no a {args.merchant_url}")

    # Igual que roles/shopping_agent/subagents/shopper/tools.py::find_products
    busqueda = (
        A2aMessageBuilder()
        .add_text("Find products that match the user's request.")
        .add_data("catalog_search", "Nike basketball shoes for outdoor use, size 42")
        .add_data("risk_data", "smoke_risk_data")
        .add_data("shopping_agent_id", "trusted_shopping_agent")
        .add_data("demo_scenario", args.escenario)
        .build()
    )
    task = await client.send_a2a_message(busqueda)
    carts = [d["ap2.cart"] for d in _data_parts(task) if "ap2.cart" in d]
    print(f"find_products: estado={task.status.state.value} contexto={task.context_id}")
    print(f"  carts: {[(c['cart_id'], c['amount']) for c in carts]}")
    if task.status.state.value != "completed" or not carts:
        fallas.append("find_products no devolvió carts")
        return _fin(fallas)

    # Igual que roles/shopping_agent/tools.py::create_checkout
    pedido = (
        A2aMessageBuilder()
        .set_context_id(task.context_id)
        .add_text("Create a checkout for the selected cart.")
        .add_data("cart_id", args.cart)
        .add_data("shopping_agent_id", "trusted_shopping_agent")
        .build()
    )
    checkout_task = await client.send_a2a_message(pedido)
    datos = _data_parts(checkout_task)
    checkouts = [d["ap2.checkout"] for d in datos if "ap2.checkout" in d]
    veredictos = [d[GUARD_VEREDICTO_KEY] for d in datos if GUARD_VEREDICTO_KEY in d]
    estado = checkout_task.status.state.value
    print(f"create_checkout({args.cart}): estado={estado}")
    if checkouts:
        jwt = checkouts[0]["checkout_jwt"]
        print(f"  ap2.checkout: hash={checkouts[0]['checkout_hash']} jwt={jwt[:40]}…")
    else:
        print("  ap2.checkout: (no llegó: no se presenta para firma)")
    if veredictos:
        v = veredictos[0]
        print(f"  guard.veredicto: {v['status']} — {v['motivo']}")
    if _texto_estado(checkout_task):
        print(f"  mensaje: {_texto_estado(checkout_task)}")

    if args.esperado and estado != args.esperado:
        fallas.append(f"se esperaba {args.esperado} y llegó {estado}")
    if estado == "completed" and not checkouts:
        fallas.append("tarea completed sin ap2.checkout")
    if estado == "failed" and checkouts:
        fallas.append("tarea failed con el checkout adentro")
    if args.json:
        print(json.dumps(checkout_task.model_dump(mode="json", by_alias=True), indent=2))
    return _fin(fallas)


def _fin(fallas: list[str]) -> int:
    if fallas:
        for falla in fallas:
            print(f"FALLA: {falla}")
        return 1
    print("OK")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--merchant-url", default=PROXY_URL)
    parser.add_argument("--escenario", default="B")
    parser.add_argument("--cart", default="cart_4")
    parser.add_argument("--esperado", choices=["completed", "failed"], default=None)
    parser.add_argument("--json", action="store_true", help="imprime la tarea completa")
    sys.exit(asyncio.run(correr(parser.parse_args())))


if __name__ == "__main__":
    main()
