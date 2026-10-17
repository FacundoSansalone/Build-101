"""Recibo de decisión: se arma sin I/O y se firma afuera (decisión 6).

- `armar_recibo()` lo usa `evaluar()`: puro y determinístico.
- `firmar_recibo()` / `verificar_recibo()`: JWS compacto ES256 sobre
  `Recibo.payload_canonico()`, con la firma en formato JWS (r || s, 64 bytes,
  base64url), igual que `_create_jwt()` del comercio del sample.
- `cargar_o_crear_clave()`: la clave del Guard en PEM (PKCS8). Por defecto
  en `.temp-db/guard_signing_key.pem` (ignorado por git), `kid: guard-key-1`.
- `clave_publica_jwk()`: la clave pública como JWK, para publicarla.

El recibo referencia los objetos firmados de AP2 por hash
(`checkout_hash`): nunca los modifica.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)

from contracts import Chequeo, Ficha, Oferta, Politica, Recibo, Status
from guard.b64 import b64url as _b64url
from guard.b64 import b64url_decode as _b64url_decode
from guard.extractor import Descarte
from guard.ranking import Puntaje, describir
from guard.verificador import NO_VERIFICABLES

KID_POR_DEFECTO = "guard-key-1"
RUTA_CLAVE_POR_DEFECTO = ".temp-db/guard_signing_key.pem"

ClaveEC = Union[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]


# ---------------------------------------------------------------------------
# Armado (sin I/O, lo llama evaluar())
# ---------------------------------------------------------------------------


def armar_recibo(
    *,
    ficha: Ficha,
    ofertas: list[Oferta],
    requisitos_por_oferta: dict[str, list[Chequeo]],
    descartes: list[Descarte],
    puntajes: list[Puntaje],
    cart_id_elegido: str,
    comercio_id: str | None,
    politica: Politica,
    status: Status,
    checkout_hash: str | None,
    ahora: datetime | None,
) -> Recibo:
    """Recibo sin firmar. `id` = hash de las entradas que usó la decisión.

    Las ofertas se hashean sin texto libre (`texto_libre_descartado` no se
    serializa): el recibo, como el veredicto, no depende de él.
    """
    emitido = ahora if ahora is not None else datetime.now(timezone.utc)
    elegida = next((o for o in ofertas if o.cart_id == cart_id_elegido), None)
    return Recibo(
        id=_id_recibo(
            ficha, ofertas, descartes, cart_id_elegido, comercio_id, politica,
            checkout_hash, emitido,
        ),
        emitido=emitido,
        ficha=ficha,
        consideradas=[o.cart_id for o in ofertas],
        filtradas=_filtradas(requisitos_por_oferta, descartes),
        verificaciones=list(elegida.evidencia.values()) if elegida else [],
        ranking=[p.cart_id for p in puntajes],
        elegido=cart_id_elegido,
        por_que_gano=_por_que_gano(puntajes, cart_id_elegido, elegida),
        sin_verificar=_sin_verificar(elegida),
        status=status,
        checkout_hash=checkout_hash,
    )


def _id_recibo(
    ficha: Ficha,
    ofertas: list[Oferta],
    descartes: list[Descarte],
    cart_id_elegido: str,
    comercio_id: str | None,
    politica: Politica,
    checkout_hash: str | None,
    emitido: datetime,
) -> str:
    entradas = {
        "ficha": ficha.model_dump(mode="json"),
        "ofertas": [o.model_dump(mode="json") for o in ofertas],
        "descartes": [d.chequeo.model_dump(mode="json") for d in descartes],
        "cart_id_elegido": cart_id_elegido,
        "comercio_id": comercio_id,
        "politica": politica.model_dump(mode="json"),
        "checkout_hash": checkout_hash,
        "emitido": emitido.isoformat(),
    }
    canonico = json.dumps(
        entradas, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return "rcb-" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()[:32]


SIN_CART_ID = "(sin cart_id)"  # clave de filtradas para carts sin cart_id válido


def _filtradas(
    requisitos_por_oferta: dict[str, list[Chequeo]], descartes: list[Descarte]
) -> dict[str, list[str]]:
    """Ofertas que no pasan el mandato o un obligatorio, y lo descartado.

    Sale de los chequeos que ya calculó `evaluar()` (no repite la lógica).
    """
    filtradas: dict[str, list[str]] = {}
    for cart_id, chequeos in requisitos_por_oferta.items():
        motivos = [c.detalle for c in chequeos if c.resultado in ("bloquea", "escalar")]
        if motivos:
            filtradas[cart_id] = motivos
    for descarte in descartes:
        clave = descarte.cart_id if descarte.cart_id is not None else SIN_CART_ID
        filtradas.setdefault(clave, []).append(descarte.chequeo.detalle)
    return filtradas


def _por_que_gano(
    puntajes: list[Puntaje], cart_id_elegido: str, elegida: Oferta | None
) -> str:
    if elegida is None or not puntajes:
        return (
            f"El carrito elegido ({cart_id_elegido}) no está entre las ofertas "
            "recibidas: no hay con qué justificar la elección."
        )
    elegido = next(p for p in puntajes if p.cart_id == cart_id_elegido)
    primero = puntajes[0]
    texto = f"Elegida {describir(elegido, elegida.moneda)}."
    criterio = "(obligatorios cumplidos, después preferidos, después precio)"
    if primero.cart_id == cart_id_elegido:
        return f"{texto} Es la primera del ranking propio {criterio}."
    return (
        f"{texto} La primera del ranking propio {criterio} es "
        f"{describir(primero, elegida.moneda)}."
    )


def _sin_verificar(elegida: Oferta | None) -> list[str]:
    """Límites declarados + lo que solo declara el comercio."""
    sin_verificar = list(NO_VERIFICABLES)
    if elegida is not None:
        sin_verificar += [
            f"{e.campo} (solo declarado por el comercio)"
            for e in elegida.evidencia.values()
            if e.estado == "solo_declarado"
        ]
    return sin_verificar


# ---------------------------------------------------------------------------
# Firma (afuera de evaluar())
# ---------------------------------------------------------------------------


def firmar_recibo(
    recibo: Recibo, clave_privada: ec.EllipticCurvePrivateKey, kid: str = KID_POR_DEFECTO
) -> Recibo:
    """Copia del recibo con `firma` (JWS compacto ES256) y `kid`."""
    if not isinstance(clave_privada.curve, ec.SECP256R1):
        raise ValueError("ES256 requiere una clave EC P-256")
    encabezado = _b64url(
        json.dumps({"alg": "ES256", "kid": kid}, sort_keys=True, separators=(",", ":")).encode()
    )
    payload = _b64url(recibo.payload_canonico())
    entrada = f"{encabezado}.{payload}".encode("ascii")
    r, s = decode_dss_signature(clave_privada.sign(entrada, ec.ECDSA(hashes.SHA256())))
    firma = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return recibo.model_copy(update={"firma": f"{encabezado}.{payload}.{firma}", "kid": kid})


def verificar_recibo(recibo: Recibo, clave_publica: ClaveEC) -> bool:
    """True solo si la firma es válida y corresponde a este recibo tal cual.

    Cualquier alteración del recibo (o del `kid`), una firma mal formada o
    una clave distinta dan False. No lanza excepciones.
    """
    if not recibo.firma:
        return False
    partes = recibo.firma.split(".")
    if len(partes) != 3:
        return False
    encabezado_b64, payload_b64, firma_b64 = partes
    try:
        encabezado = json.loads(_b64url_decode(encabezado_b64))
        firma = _b64url_decode(firma_b64)
    except ValueError:  # base64, UTF-8 o JSON inválidos
        return False
    if not isinstance(encabezado, dict) or encabezado.get("alg") != "ES256":
        return False
    if encabezado.get("kid") != recibo.kid:
        return False
    if payload_b64 != _b64url(recibo.payload_canonico()) or len(firma) != 64:
        return False
    der = encode_dss_signature(
        int.from_bytes(firma[:32], "big"), int.from_bytes(firma[32:], "big")
    )
    publica = _publica(clave_publica)
    try:
        publica.verify(
            der, f"{encabezado_b64}.{payload_b64}".encode("ascii"), ec.ECDSA(hashes.SHA256())
        )
    except InvalidSignature:
        return False
    return True


# ---------------------------------------------------------------------------
# Clave del Guard
# ---------------------------------------------------------------------------


def cargar_o_crear_clave(
    path: str | os.PathLike = RUTA_CLAVE_POR_DEFECTO,
) -> ec.EllipticCurvePrivateKey:
    """Carga la clave PEM o, si no existe, crea una P-256 y la guarda."""
    ruta = Path(path)
    if ruta.exists():
        clave = load_pem_private_key(ruta.read_bytes(), password=None)
        if not isinstance(clave, ec.EllipticCurvePrivateKey) or not isinstance(
            clave.curve, ec.SECP256R1
        ):
            raise ValueError(f"{ruta} no es una clave EC P-256")
        return clave
    clave = ec.generate_private_key(ec.SECP256R1())
    ruta.parent.mkdir(parents=True, exist_ok=True)
    pem = clave.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as archivo:
        archivo.write(pem)
    return clave


def clave_publica_jwk(clave: ClaveEC, kid: str = KID_POR_DEFECTO) -> dict:
    """JWK (EC P-256) de la clave pública, con `kid`. Nunca incluye `d`."""
    numeros = _publica(clave).public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numeros.x.to_bytes(32, "big")),
        "y": _b64url(numeros.y.to_bytes(32, "big")),
        "alg": "ES256",
        "use": "sig",
        "kid": kid,
    }


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------


def _publica(clave: ClaveEC) -> ec.EllipticCurvePublicKey:
    if isinstance(clave, ec.EllipticCurvePrivateKey):
        return clave.public_key()
    return clave


