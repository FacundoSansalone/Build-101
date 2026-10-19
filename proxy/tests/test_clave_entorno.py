"""La clave del recibo por entorno (`ClaveRecibo`, lote D de Vercel).

Con varias instancias sin disco compartido, la clave no puede salir de un
archivo que cada instancia crea: sale de `GUARD_SIGNING_KEY_PEM_B64` (el PEM
en base64) o de `GUARD_SIGNING_KEY_PEM` (el PEM directo), con el kid de
`GUARD_SIGNING_KID`. Lo que importa:

- se lee bien de las dos formas (base64 con o sin saltos, PEM con `\\n`
  escritos o reales) y el kid llega a la firma y a la JWK;
- lo que no sirve no arranca y el mensaje nunca trae la clave;
- la firma se verifica con la JWK pública que se publica;
- `crear_si_falta=False` no escribe nada.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from contracts import Ficha, OfertasVistas, Recibo, Veredicto
from guard.recibo import verificar_recibo
from proxy import guard_stub
from proxy.evaluador import (
    KID_POR_DEFECTO,
    VAR_CLAVE_PEM,
    VAR_CLAVE_PEM_B64,
    VAR_KID,
    ClaveRecibo,
    ClaveReciboInvalida,
    kid_del_entorno,
)

AHORA = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _pem(clave, formato=serialization.PrivateFormat.PKCS8, cifrado=None) -> bytes:
    return clave.private_bytes(
        serialization.Encoding.PEM, formato, cifrado or serialization.NoEncryption()
    )


@pytest.fixture
def clave_p256() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def pem(clave_p256) -> bytes:
    return _pem(clave_p256)


def _misma(a: ec.EllipticCurvePrivateKey, b: ec.EllipticCurvePrivateKey) -> bool:
    return a.private_numbers() == b.private_numbers()


def _cuerpo(pem: bytes) -> str:
    """Una línea del medio del PEM: si aparece en un mensaje, se filtró la clave."""
    return pem.decode("ascii").splitlines()[1]


def _recibo() -> Recibo:
    ficha = Ficha(pedido_original="un monitor", requisitos=[], confirmada=True)
    ofertas = OfertasVistas(
        carts=[{"cart_id": "cart_1", "item_label": "x", "amount": 10.0, "currency": "USD"}],
        offer_details=[],
    )
    veredicto = guard_stub.evaluar(ficha, ofertas, "cart_1", {}, checkout_hash="h", ahora=AHORA)
    assert veredicto.recibo is not None and not veredicto.recibo.firma
    return veredicto.recibo


def _publica_de_jwk(jwk: dict) -> ec.EllipticCurvePublicKey:
    def entero(valor: str) -> int:
        return int.from_bytes(base64.urlsafe_b64decode(valor + "=" * (-len(valor) % 4)), "big")

    return ec.EllipticCurvePublicNumbers(
        entero(jwk["x"]), entero(jwk["y"]), ec.SECP256R1()
    ).public_key()


# -- desde_entorno: lo que se lee ------------------------------------------------


def test_sin_variables_no_hay_clave_del_entorno():
    assert ClaveRecibo.desde_entorno({}) is None
    # Vacías (o solo espacios) cuentan como que no están, igual que el resto
    # de la configuración; el kid solo no alcanza.
    assert ClaveRecibo.desde_entorno(
        {VAR_CLAVE_PEM_B64: "", VAR_CLAVE_PEM: "  ", VAR_KID: "otra"}
    ) is None


def test_pem_en_base64(clave_p256, pem):
    clave = ClaveRecibo.desde_entorno({VAR_CLAVE_PEM_B64: base64.b64encode(pem).decode()})
    assert clave is not None
    assert _misma(clave.clave, clave_p256)
    assert clave.kid == KID_POR_DEFECTO == "guard-key-1"
    assert clave.origen == VAR_CLAVE_PEM_B64


def test_base64_con_saltos_y_espacios(clave_p256, pem):
    """`base64` sin `-w0` corta cada 76 columnas, y la UI puede sumar un salto."""
    crudo = base64.encodebytes(pem).decode()  # con "\n" cada 76
    assert "\n" in crudo.strip()
    clave = ClaveRecibo.desde_entorno({VAR_CLAVE_PEM_B64: f"  {crudo}\n"})
    assert _misma(clave.clave, clave_p256)


@pytest.mark.parametrize("forma", ["real", "escrito", "escrito_crlf"])
def test_pem_directo(clave_p256, pem, forma):
    texto = pem.decode("ascii")
    if forma == "escrito":
        texto = texto.replace("\n", "\\n")  # una sola línea, como en un .env
    elif forma == "escrito_crlf":
        texto = texto.replace("\n", "\\r\\n")
    clave = ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: texto})
    assert _misma(clave.clave, clave_p256)
    assert clave.origen == VAR_CLAVE_PEM


def test_pem_sec1_tambien(clave_p256):
    """`openssl ecparam -genkey` da `BEGIN EC PRIVATE KEY` (SEC1), no PKCS8."""
    sec1 = _pem(clave_p256, serialization.PrivateFormat.TraditionalOpenSSL)
    assert b"BEGIN EC PRIVATE KEY" in sec1
    clave = ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: sec1.decode()})
    assert _misma(clave.clave, clave_p256)


def test_el_kid_del_entorno(pem):
    env = {VAR_CLAVE_PEM_B64: base64.b64encode(pem).decode(), VAR_KID: " kaiai-2026.10_b "}
    clave = ClaveRecibo.desde_entorno(env)
    assert clave.kid == "kaiai-2026.10_b"
    assert clave.jwk_publica()["kid"] == "kaiai-2026.10_b"
    firmado = clave.firmar(_recibo())
    assert firmado.kid == "kaiai-2026.10_b"
    encabezado = firmado.firma.split(".")[0]
    assert b'"kid":"kaiai-2026.10_b"' in base64.urlsafe_b64decode(encabezado + "==")


def test_kid_del_entorno_por_defecto():
    assert kid_del_entorno({}) == KID_POR_DEFECTO
    assert kid_del_entorno({VAR_KID: "  "}) == KID_POR_DEFECTO
    assert kid_del_entorno({VAR_KID: "k2"}) == "k2"


# -- desde_entorno: errores sin filtrar la clave ---------------------------------


def _sin_filtrar(error: pytest.ExceptionInfo, *secretos: str) -> None:
    texto = f"{error.value} {error.value!r}"
    for secreto in secretos:
        assert secreto not in texto
    # Sin encadenar: un traceback no muestra la excepción de abajo (que
    # podría citar lo leído).
    assert error.value.__cause__ is None and error.value.__suppress_context__ is True


def test_las_dos_variables_a_la_vez_no(pem):
    with pytest.raises(ClaveReciboInvalida, match="definí una sola") as e:
        ClaveRecibo.desde_entorno(
            {VAR_CLAVE_PEM_B64: base64.b64encode(pem).decode(), VAR_CLAVE_PEM: pem.decode()}
        )
    assert _cuerpo(pem) not in str(e.value)


def test_base64_invalido(pem):
    """El PEM tal cual en la variable de base64 no es base64 (tiene `-`)."""
    with pytest.raises(ClaveReciboInvalida, match=VAR_CLAVE_PEM_B64) as e:
        ClaveRecibo.desde_entorno({VAR_CLAVE_PEM_B64: pem.decode()})
    _sin_filtrar(e, _cuerpo(pem), "BEGIN")
    assert VAR_CLAVE_PEM in str(e.value)  # la pista: la otra variable


def test_base64_que_no_es_pem():
    secreto = "no-es-una-clave-pero-podria-ser-un-secreto-XYZZY"
    with pytest.raises(ClaveReciboInvalida, match="no es una clave privada PEM") as e:
        ClaveRecibo.desde_entorno({VAR_CLAVE_PEM_B64: base64.b64encode(secreto.encode()).decode()})
    _sin_filtrar(e, secreto, base64.b64encode(secreto.encode()).decode())


def test_pem_roto(pem):
    roto = pem.decode().replace(_cuerpo(pem), _cuerpo(pem)[::-1])
    with pytest.raises(ClaveReciboInvalida, match=VAR_CLAVE_PEM) as e:
        ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: roto})
    _sin_filtrar(e, _cuerpo(pem), _cuerpo(pem)[::-1])


@pytest.mark.parametrize(
    "otra",
    [
        lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        lambda: ec.generate_private_key(ec.SECP384R1()),
    ],
    ids=["rsa", "p384"],
)
def test_una_clave_que_no_es_p256(otra):
    pem_otra = _pem(otra())
    with pytest.raises(ClaveReciboInvalida, match="P-256") as e:
        ClaveRecibo.desde_entorno({VAR_CLAVE_PEM_B64: base64.b64encode(pem_otra).decode()})
    assert _cuerpo(pem_otra) not in f"{e.value} {e.value!r}"


def test_una_clave_con_contrasena(clave_p256):
    cifrado = _pem(
        clave_p256, cifrado=serialization.BestAvailableEncryption(b"contrasena-secreta")
    )
    with pytest.raises(ClaveReciboInvalida, match="sin contraseña") as e:
        ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: cifrado.decode()})
    _sin_filtrar(e, _cuerpo(cifrado), "contrasena-secreta")


def test_una_clave_publica_no_sirve(clave_p256):
    publica = clave_p256.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(ClaveReciboInvalida):
        ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: publica.decode()})


def test_un_kid_invalido_no_se_muestra(pem):
    """Si alguien pega la clave en la variable del kid, el mensaje no la trae."""
    with pytest.raises(ClaveReciboInvalida, match=VAR_KID) as e:
        ClaveRecibo.desde_entorno(
            {VAR_CLAVE_PEM_B64: base64.b64encode(pem).decode(), VAR_KID: pem.decode()}
        )
    assert _cuerpo(pem) not in f"{e.value} {e.value!r}"
    with pytest.raises(ClaveReciboInvalida):
        ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: pem.decode(), VAR_KID: "con espacio"})


def test_el_repr_no_muestra_la_clave(clave_p256, pem):
    clave = ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: pem.decode()})
    assert repr(clave) == str(clave) == f"ClaveRecibo(kid='guard-key-1', origen='{VAR_CLAVE_PEM}')"
    texto = repr(clave)
    assert str(clave_p256.private_numbers().private_value) not in texto
    assert _cuerpo(pem) not in texto


def test_el_constructor_exige_p256():
    with pytest.raises(ClaveReciboInvalida, match="P-256"):
        ClaveRecibo(clave=ec.generate_private_key(ec.SECP384R1()))  # type: ignore[arg-type]
    with pytest.raises(ClaveReciboInvalida, match="kid"):
        ClaveRecibo(clave=ec.generate_private_key(ec.SECP256R1()), kid="")


# -- firma y JWK pública --------------------------------------------------------


def test_la_firma_se_verifica_con_la_jwk_publica(pem):
    clave = ClaveRecibo.desde_entorno({VAR_CLAVE_PEM_B64: base64.b64encode(pem).decode()})
    jwk = clave.jwk_publica()
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256"
    assert jwk["alg"] == "ES256" and jwk["use"] == "sig" and jwk["kid"] == clave.kid
    assert "d" not in jwk  # nunca la privada
    firmado = clave.firmar(_recibo())
    assert firmado.kid == clave.kid and firmado.firma.count(".") == 2
    assert verificar_recibo(firmado, _publica_de_jwk(jwk)) is True
    # Con otra clave no verifica, y alterado tampoco.
    otra = ec.generate_private_key(ec.SECP256R1()).public_key()
    assert verificar_recibo(firmado, otra) is False
    assert verificar_recibo(firmado.model_copy(update={"elegido": "cart_2"}), _publica_de_jwk(jwk)) is False


def test_clave_publica_tiene_la_forma_de_siempre(pem):
    """`{"kid", "jwk"}`, como `proxy.evaluador.clave_publica(key_path)`, para
    que `/v1/public-key` y `/guard/public-key` la usen igual."""
    clave = ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: pem.decode(), VAR_KID: "k9"})
    assert clave.clave_publica() == {"kid": "k9", "jwk": clave.jwk_publica()}


def test_firmar_veredicto(pem):
    clave = ClaveRecibo.desde_entorno({VAR_CLAVE_PEM: pem.decode()})
    recibo = _recibo()
    veredicto = Veredicto(
        status="aprobado", cart_id_elegido="cart_1", cart_id_recomendado="cart_1",
        ranking=["cart_1"], chequeos=[], motivo="ok", recibo=recibo,
    )
    firmado = clave.firmar_veredicto(veredicto)
    assert firmado.recibo.firma and verificar_recibo(firmado.recibo, clave.clave)
    # Ya firmado o sin recibo: igual.
    assert clave.firmar_veredicto(firmado) is firmado
    sin_recibo = veredicto.model_copy(update={"recibo": None})
    assert clave.firmar_veredicto(sin_recibo) is sin_recibo


# -- desde_archivo ---------------------------------------------------------------


def test_desde_archivo_sin_crear_no_escribe_nada(tmp_path):
    ruta = tmp_path / "no-existe" / "guard_signing_key.pem"
    with pytest.raises(ClaveReciboInvalida, match=VAR_CLAVE_PEM_B64):
        ClaveRecibo.desde_archivo(ruta, crear_si_falta=False)
    assert not ruta.exists() and not ruta.parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_desde_archivo_crea_la_de_la_demo_y_despues_la_lee(tmp_path):
    ruta = tmp_path / ".temp-db" / "guard_signing_key.pem"
    creada = ClaveRecibo.desde_archivo(ruta)  # crear_si_falta=True: lo de siempre
    assert ruta.is_file() and creada.kid == KID_POR_DEFECTO and creada.origen == str(ruta)
    leida = ClaveRecibo.desde_archivo(ruta, "k2", crear_si_falta=False)
    assert _misma(leida.clave, creada.clave) and leida.kid == "k2"
    assert _misma(ClaveRecibo.desde_archivo(ruta).clave, creada.clave)


def test_desde_archivo_es_la_misma_que_firma_el_proxy_hoy(tmp_path):
    """La del archivo es la de `guard.recibo.cargar_o_crear_clave`: lo que
    firma hoy `firmar_recibo(veredicto, key_path)` se verifica con ella."""
    from proxy.evaluador import clave_publica, firmar_recibo

    ruta = tmp_path / "guard_signing_key.pem"
    veredicto = Veredicto(
        status="aprobado", cart_id_elegido="cart_1", cart_id_recomendado="cart_1",
        ranking=["cart_1"], chequeos=[], motivo="ok", recibo=_recibo(),
    )
    de_hoy = firmar_recibo(veredicto, ruta)
    clave = ClaveRecibo.desde_archivo(ruta, crear_si_falta=False)
    assert verificar_recibo(de_hoy.recibo, _publica_de_jwk(clave.jwk_publica()))
    assert clave.clave_publica() == clave_publica(ruta)


def test_desde_archivo_que_no_es_p256(tmp_path):
    ruta = tmp_path / "rsa.pem"
    otra = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    ruta.write_bytes(otra)
    for crear in (True, False):
        with pytest.raises(ClaveReciboInvalida, match="P-256") as e:
            ClaveRecibo.desde_archivo(ruta, crear_si_falta=crear)
        assert _cuerpo(otra) not in str(e.value)


def test_desde_archivo_ilegible(tmp_path):
    with pytest.raises(ClaveReciboInvalida, match="no se pudo leer"):
        ClaveRecibo.desde_archivo(tmp_path, crear_si_falta=False)  # una carpeta
