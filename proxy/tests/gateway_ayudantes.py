"""Ayudantes de los tests del gateway de producción (diseño del gateway, §4.5).

- integradores en memoria con claves generadas de verdad (`generar_clave`):
  `acme` (con webhook) y `otro` (sin webhook, para el aislamiento);
- dos comercios: `merchant_1` (el FakeMerchant de siempre, firma como
  `merchant_1`) y `tienda_b` (otro FakeMerchant en `merchant-b.test`, que
  firma como `tienda-b`), cada uno con sus `merchant_ids`;
- clientes del oyente público (`http://gw.test:8030`, con la clave) y del
  local (`http://localhost:8010`, con el token de /guard);
- un receptor de webhooks con `MockTransport` y un resolver que devuelve una
  IP pública ficticia (sin red ni DNS de verdad).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from proxy.app import create_app
from proxy.claves import RegistroIntegradores, generar_clave
from proxy.comercios import RegistroComercios
from proxy.seguridad import HEADER_TOKEN
from proxy.tests.fake_world import (
    CATALOG_HOST,
    CATALOG_URL,
    HEADERS_A2A,
    MERCHANT_HOST,
    MERCHANT_URL,
    FakeMerchant,
    RuteoPorHost,
    app_catalogo,
    cuerpo,
    pedido_create_checkout,
    pedido_find_products,
)
from proxy.tipos_gateway import HEADER_COMPRA, Comercio
from proxy.webhooks import Despachador

MERCHANT_B_HOST = "merchant-b.test"
MERCHANT_B_URL = f"http://{MERCHANT_B_HOST}/a2a/merchant_agent"
BASE_PUBLICA = "http://gw.test:8030"
BASE_LOCAL = "http://localhost:8010"
GATEWAY_PUBLIC_URL = "https://gw.example.com"
SECRETO_WEBHOOK = "whsec_" + "s" * 40
IP_PUBLICA = "93.184.216.34"
URL_WEBHOOK = "https://hooks.acme.example/guard"

# Claves de prueba: una por integrador (se generan al importar, nunca se
# guardan en claro en ningún archivo).
CLAVE_ACME, KID_ACME, SHA_ACME = generar_clave()
CLAVE_OTRO, KID_OTRO, SHA_OTRO = generar_clave()

COMERCIO_1 = Comercio(
    id="merchant_1", nombre="Demo", a2a_url=MERCHANT_URL, catalogo_url=CATALOG_URL,
    merchant_ids=("merchant_1",),
)
COMERCIO_B = Comercio(
    id="tienda_b", nombre="Tienda B", a2a_url=MERCHANT_B_URL, catalogo_url=None,
    merchant_ids=("tienda-b",),
)

FICHA = {
    "requisitos": [
        {"campo": "precio", "operador": "maximo", "valor": 150, "obligatorio": True},
        {"campo": "moneda", "operador": "igual", "valor": "USD", "obligatorio": True},
    ]
}


class FakeMerchantB(FakeMerchant):
    """Un segundo comercio: igual al de siempre, pero firma con su propio
    `merchant.id` (así el chequeo de la ruta lo reconoce)."""

    def __init__(self, merchant_id: str = "tienda-b") -> None:
        super().__init__()
        self.merchant_id = merchant_id

    def firmar_jwt(self, header: dict, payload: dict) -> str:
        if isinstance(payload.get("merchant"), dict):
            payload = {**payload, "merchant": {"id": self.merchant_id, "name": "Tienda B"}}
        return super().firmar_jwt(header, payload)


def datos_integradores(*, webhook: bool = True, comercios_acme: list[str] | None = None) -> dict:
    acme: dict[str, Any] = {
        "id": "acme",
        "nombre": "ACME Compras",
        "claves": [{"kid": KID_ACME, "sha256": SHA_ACME, "activa": True}],
        "comercios": comercios_acme if comercios_acme is not None else ["merchant_1", "tienda_b"],
    }
    if webhook:
        acme["webhook"] = {"url": URL_WEBHOOK, "secreto_env": "SECRETO_ACME"}
    otro = {
        "id": "otro",
        "claves": [{"kid": KID_OTRO, "sha256": SHA_OTRO, "activa": True}],
        "comercios": ["merchant_1"],
    }
    return {"integradores": [acme, otro]}


def registro_integradores(**kwargs: Any) -> RegistroIntegradores:
    return RegistroIntegradores.desde_dict(
        datos_integradores(**kwargs), {"SECRETO_ACME": SECRETO_WEBHOOK}
    )


def registro_comercios() -> RegistroComercios:
    # Directo con `Comercio`: el archivo exige https fuera de loopback y los
    # hosts de prueba son http.
    return RegistroComercios([COMERCIO_1, COMERCIO_B], "merchant_1")


@dataclass
class ReceptorWebhooks:
    """El backend del integrador: registra cada POST y responde `status`."""

    status: int = 200
    recibidos: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.recibidos.append(request)
        return httpx.Response(self.status)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def cuerpos(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.recibidos]

    def tipos(self) -> list[str]:
        return [c["tipo"] for c in self.cuerpos()]

    def de_tipo(self, tipo: str) -> list[httpx.Request]:
        return [r for r in self.recibidos if json.loads(r.content)["tipo"] == tipo]


async def resolver_publico(host: str, puerto: int) -> list[str]:
    return [IP_PUBLICA]


async def _sin_dormir(_segundos: float) -> None:
    return None


def despachador(integradores: RegistroIntegradores, receptor: ReceptorWebhooks, **kw: Any) -> Despachador:
    return Despachador(
        integradores.get, transport=receptor.transport(), resolver=resolver_publico,
        dormir=_sin_dormir, **kw,
    )


@dataclass
class Mundo:
    """Lo que arma `armar_gateway`: la app y las piezas para inspeccionar."""

    app: Any
    merchant: FakeMerchant
    merchant_b: FakeMerchantB
    receptor: ReceptorWebhooks
    integradores: RegistroIntegradores

    @property
    def proxy(self) -> Any:
        return self.app.state.proxy

    async def esperar_fondo(self) -> None:
        """Escrituras al almacén y webhooks en vuelo (tests)."""
        await self.proxy.en_fondo.esperar()
        if self.proxy.despachador is not None:
            await self.proxy.despachador.esperar()


def armar_gateway(
    config: Any,
    evaluador: Any,
    *,
    merchant: FakeMerchant | None = None,
    merchant_b: FakeMerchantB | None = None,
    integradores: RegistroIntegradores | None = None,
    receptor: ReceptorWebhooks | None = None,
    con_webhooks: bool = True,
    **kwargs: Any,
) -> Mundo:
    merchant = merchant or FakeMerchant()
    merchant_b = merchant_b or FakeMerchantB()
    integradores = integradores or registro_integradores()
    receptor = receptor or ReceptorWebhooks()
    transporte = RuteoPorHost({
        MERCHANT_HOST: merchant.app,
        CATALOG_HOST: app_catalogo(),
        MERCHANT_B_HOST: merchant_b.app,
    })
    kwargs.setdefault("comercios", registro_comercios())
    if con_webhooks:
        kwargs.setdefault("despachador", despachador(integradores, receptor))
    app = create_app(
        config=config, transport=transporte, evaluador=evaluador,
        integradores=integradores, **kwargs,
    )
    return Mundo(app, merchant, merchant_b, receptor, integradores)


@asynccontextmanager
async def cliente_publico(app: Any, clave: str | None = CLAVE_ACME) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente del oyente público (puerto 8030 en la URL: así lo ve el middleware)."""
    headers = {"Authorization": f"Bearer {clave}"} if clave else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_PUBLICA, timeout=10,
        headers=headers,
    ) as c:
        yield c


@asynccontextmanager
async def cliente_local(app: Any, clave: str | None = None) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente del oyente local, con el token de /guard (como el dashboard)."""
    headers = {HEADER_TOKEN: app.state.proxy.api_token}
    if clave:
        headers["Authorization"] = f"Bearer {clave}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_LOCAL, timeout=10, headers=headers,
    ) as c:
        yield c


def ruta_a2a(comercio: str = "merchant_1") -> str:
    return f"/a2a/m/{comercio}"


async def crear_compra(c: httpx.AsyncClient, **cambios: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"pedido": "zapatillas Nike para exterior", "ficha": FICHA}
    body.update(cambios)
    resp = await c.post("/v1/compras", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def buscar(
    c: httpx.AsyncClient, compra_id: str, *, comercio: str = "merchant_1",
    escenario: str = "B", metodo: str = "message/send", contexto: str | None = None,
) -> httpx.Response:
    pedido = pedido_find_products(escenario, metodo=metodo)
    if contexto:
        pedido["params"]["message"]["contextId"] = contexto
    return await c.post(
        ruta_a2a(comercio), content=cuerpo(pedido),
        headers={**HEADERS_A2A, HEADER_COMPRA: compra_id},
    )


async def checkout(
    c: httpx.AsyncClient, compra_id: str, contexto: str, cart_id: str, *,
    comercio: str = "merchant_1", metodo: str = "message/send",
) -> httpx.Response:
    return await c.post(
        ruta_a2a(comercio), content=cuerpo(pedido_create_checkout(contexto, cart_id, metodo)),
        headers={**HEADERS_A2A, HEADER_COMPRA: compra_id},
    )


def _b64url_decode(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def jws_valido(jws: str, jwk: dict[str, Any]) -> bool:
    """Verifica un JWS compacto ES256 con la JWK pública (como lo haría el
    integrador con `GET /v1/public-key`)."""
    try:
        encabezado, payload, firma = jws.split(".")
        crudo = _b64url_decode(firma)
        publica = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_b64url_decode(jwk["x"]), "big"),
            int.from_bytes(_b64url_decode(jwk["y"]), "big"),
            ec.SECP256R1(),
        ).public_key()
        publica.verify(
            encode_dss_signature(int.from_bytes(crudo[:32], "big"), int.from_bytes(crudo[32:], "big")),
            f"{encabezado}.{payload}".encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError, KeyError):
        return False
    return True
