"""Prueba real: find_products + create_checkout a través del proxy.

Usa el mismo cliente A2A del sample que el Shopping Agent
(`PaymentRemoteA2aClient`) y los mismos mensajes que sus tools. Necesita
levantados el comercio (:8001, con GOOGLE_API_KEY: `create_checkout` pasa
por el LLM del sample), el catálogo (:8011) y el proxy (:8010).

Desde la raíz del repo:

    PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python \\
        -m proxy.scripts.smoke_checkout --escenario B --cart cart_4 --esperado failed

Escalar con aprobación humana (C/cart_1): el primer intento queda retenido,
el script hace `POST /guard/aprobaciones` (lo que hace "Aprobar igual" en el
dashboard, con el token de `.temp-db/guard_api_token`) y reintenta como el
Shopping Agent:

    ... -m proxy.scripts.smoke_checkout --escenario C --cart cart_1 --aprobar \\
        --esperado completed --merchant-key <pem del comercio>

Chequea además que el checkout que llega al cliente sea byte a byte el que
recibió el proxy del comercio (evento `checkout_recibido`), que su
`checkout_hash` sea el SHA-256 del JWT y, con `--merchant-key`, que la firma
ES256 del comercio siga valiendo. Sale con código 0 si todo da bien.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from contracts import GUARD_VEREDICTO_KEY
from proxy.config import REPO_ROOT
from proxy.seguridad import HEADER_TOKEN

PROXY_URL = "http://localhost:8010/a2a/merchant_agent"
TOKEN_FILE = REPO_ROOT / ".temp-db" / "guard_api_token"


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


def _b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _b64url_decode(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _firma_valida(jwt: str, pem_path: str) -> bool:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    with open(pem_path, "rb") as f:
        clave = serialization.load_pem_private_key(f.read(), password=None)
    h, p, s = jwt.split(".")
    raw = _b64url_decode(s)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        clave.public_key().verify(der, f"{h}.{p}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    except Exception:  # noqa: BLE001 - InvalidSignature
        return False
    return True


def _leer_token(path: str) -> str | None:
    """El token que el proxy escribe al arrancar (lo mismo que hace el dashboard)."""
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _base(merchant_url: str) -> str:
    partes = urlsplit(merchant_url)
    return f"{partes.scheme}://{partes.netloc}"


async def _checkout(client: Any, contexto: str, cart: str) -> Any:
    from common.a2a_message_builder import A2aMessageBuilder

    # Igual que roles/shopping_agent/tools.py::create_checkout
    pedido = (
        A2aMessageBuilder()
        .set_context_id(contexto)
        .add_text("Create a checkout for the selected cart.")
        .add_data("cart_id", cart)
        .add_data("shopping_agent_id", "trusted_shopping_agent")
        .build()
    )
    return await client.send_a2a_message(pedido)


def _mostrar(titulo: str, task: Any) -> tuple[str, list[dict], list[dict]]:
    datos = _data_parts(task)
    checkouts = [d["ap2.checkout"] for d in datos if "ap2.checkout" in d]
    veredictos = [d[GUARD_VEREDICTO_KEY] for d in datos if GUARD_VEREDICTO_KEY in d]
    estado = task.status.state.value
    print(f"{titulo}: estado={estado} tarea={task.id}")
    if checkouts:
        jwt = checkouts[0]["checkout_jwt"]
        print(f"  ap2.checkout: hash={checkouts[0]['checkout_hash']} jwt={jwt[:40]}…")
    else:
        print("  ap2.checkout: (no llegó: no se presenta para firma)")
    for v in veredictos:
        print(f"  guard.veredicto: {v['status']} — {v['motivo']}")
    if _texto_estado(task):
        print(f"  mensaje: {_texto_estado(task)}")
    return estado, checkouts, veredictos


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
    contexto = task.context_id
    print(f"find_products: estado={task.status.state.value} contexto={contexto}")
    print(f"  carts: {[(c['cart_id'], c['amount']) for c in carts]}")
    if task.status.state.value != "completed" or not carts:
        fallas.append("find_products no devolvió carts")
        return _fin(fallas)

    checkout_task = await _checkout(client, contexto, args.cart)
    estado, checkouts, veredictos = _mostrar(f"create_checkout({args.cart})", checkout_task)
    if estado == "failed" and checkouts:
        fallas.append("tarea failed con el checkout adentro")
    if estado == "failed" and not veredictos:
        fallas.append("tarea failed sin guard.veredicto")

    base = _base(args.merchant_url)
    if args.aprobar:
        if not (estado == "failed" and veredictos and veredictos[0]["status"] == "escalar"):
            fallas.append("se pidió --aprobar pero el checkout no quedó retenido por escalar")
            return _fin(fallas)
        token = _leer_token(args.token_file)
        if token is None:
            fallas.append(f"no se pudo leer el token de la API del proxy en {args.token_file}")
            return _fin(fallas)
        async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
            r = await http.post(
                f"{base}/guard/aprobaciones",
                json={"contexto_id": contexto, "cart_id": args.cart},
                headers={HEADER_TOKEN: token},  # como el reenvío del dashboard (N4)
            )
        print(f"POST /guard/aprobaciones → {r.status_code} {r.json()}")
        if r.status_code not in (200, 201):
            fallas.append(f"la aprobación devolvió {r.status_code}")
            return _fin(fallas)
        checkout_task = await _checkout(client, contexto, args.cart)
        estado, checkouts, veredictos = _mostrar(
            f"create_checkout({args.cart}) reintento", checkout_task
        )

    if args.esperado and estado != args.esperado:
        fallas.append(f"se esperaba {args.esperado} y llegó {estado}")
    if estado == "completed" and not checkouts:
        fallas.append("tarea completed sin ap2.checkout")
    if checkouts:
        await _verificar_intacto(base, contexto, checkouts[0], args, fallas)
    if args.json:
        print(json.dumps(checkout_task.model_dump(mode="json", by_alias=True), indent=2))
    return _fin(fallas)


async def _verificar_intacto(
    base: str, contexto: str, checkout: dict[str, Any], args: argparse.Namespace,
    fallas: list[str],
) -> None:
    jwt = checkout["checkout_jwt"]
    if _b64url(hashlib.sha256(jwt.encode("ascii")).digest()) != checkout["checkout_hash"]:
        fallas.append("el checkout_hash no es el SHA-256 del checkout_jwt")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
        eventos = (await http.get(f"{base}/guard/events/history")).json()
    recibidos = [
        e["datos"] for e in eventos
        if e["tipo"] == "checkout_recibido" and e["contexto_id"] == contexto
    ]
    if not recibidos:
        fallas.append("el proxy no registró checkout_recibido para esta compra")
    elif recibidos[-1]["checkout_jwt"] != jwt or recibidos[-1]["checkout_hash"] != checkout["checkout_hash"]:
        fallas.append("el checkout que llegó no es el que el proxy recibió del comercio")
    else:
        print("  checkout byte a byte igual al que el comercio entregó al proxy: OK")
    reenviados = [e for e in eventos if e["tipo"] == "reenviado" and e["contexto_id"] == contexto]
    if args.aprobar and not any(e["datos"].get("aprobacion_humana") for e in reenviados):
        fallas.append("falta el evento reenviado con aprobacion_humana")
    if args.merchant_key:
        if _firma_valida(jwt, args.merchant_key):
            print("  firma ES256 del comercio: válida")
        else:
            fallas.append("la firma ES256 del comercio no valida")


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
    parser.add_argument("--aprobar", action="store_true",
                        help="si queda retenido (escalar), aprobar y reintentar")
    parser.add_argument("--merchant-key", default=None,
                        help="PEM del comercio (MERCHANT_SIGNING_KEY_PATH) para verificar la firma")
    parser.add_argument("--token-file", default=str(TOKEN_FILE),
                        help="token de la API del proxy para --aprobar (lo escribe el proxy)")
    parser.add_argument("--json", action="store_true", help="imprime la tarea completa")
    sys.exit(asyncio.run(correr(parser.parse_args())))


if __name__ == "__main__":
    main()
