"""Tests del recibo: lo arma evaluar() sin firmar; se firma afuera (ES256)."""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto import jwk, jws

from contracts import Recibo
from guard import evaluar
from guard.recibo import (
    cargar_o_crear_clave,
    clave_publica_jwk,
    firmar_recibo,
    verificar_recibo,
)


def _b64url_decode(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


@pytest.fixture(scope="module")
def clave():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def veredicto_b(vistas, ficha, catalogo, ahora):
    return evaluar(ficha("B"), vistas("B"), "cart_4", catalogo, checkout_hash="hash-b", ahora=ahora)


@pytest.fixture
def recibo_b(veredicto_b) -> Recibo:
    return veredicto_b.recibo


# --- recibo que arma evaluar() ---------------------------------------------------


def test_evaluar_arma_el_recibo_sin_firmar(veredicto_b, ahora):
    recibo = veredicto_b.recibo
    assert recibo is not None
    assert recibo.firma is None and recibo.kid is None
    assert recibo.status == veredicto_b.status == "bloqueado"
    assert recibo.elegido == "cart_4"
    assert recibo.ranking == veredicto_b.ranking
    assert recibo.consideradas == ["cart_1", "cart_2", "cart_3", "cart_4"]
    assert recibo.checkout_hash == "hash-b"
    assert recibo.emitido == ahora


def test_filtradas_con_motivo(recibo_b):
    assert set(recibo_b.filtradas) == {"cart_2", "cart_4"}
    assert any("lifestyle_sneakers" in m for m in recibo_b.filtradas["cart_4"])
    assert any("indoor" in m for m in recibo_b.filtradas["cart_2"])


def test_filtradas_salen_de_los_chequeos_de_evaluar(veredicto_b):
    """Los motivos de la elegida son los mismos chequeos que decidieron el veredicto."""
    motivos = [
        c.detalle for c in veredicto_b.chequeos
        if c.tipo in ("mandato", "cumplimiento") and c.origen != "ficha"
        and c.resultado in ("bloquea", "escalar")
    ]
    assert motivos
    assert veredicto_b.recibo.filtradas["cart_4"] == motivos


def test_cada_requisito_se_chequea_una_sola_vez_por_oferta(vistas, ficha, catalogo, ahora, monkeypatch):
    """El recibo no repite la lógica de los chequeos: reusa los de evaluar()."""
    import guard.chequeos as modulo_chequeos

    original = modulo_chequeos._chequeo_requisito
    llamadas = []

    def espia(*args, **kwargs):
        llamadas.append(args[1].cart_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(modulo_chequeos, "_chequeo_requisito", espia)
    ficha_b = ficha("B")
    evaluar(ficha_b, vistas("B"), "cart_4", catalogo, ahora=ahora)
    assert len(llamadas) == 4 * len(ficha_b.requisitos)
    assert sorted(set(llamadas)) == ["cart_1", "cart_2", "cart_3", "cart_4"]


def test_verificaciones_de_la_elegida(vistas, ficha, catalogo, ahora):
    recibo = evaluar(ficha("B2"), vistas("B2"), "cart_4", catalogo, ahora=ahora).recibo
    estados = {e.campo: e.estado for e in recibo.verificaciones}
    assert estados["categoria"] == "contradicho"
    assert estados["uso"] == "contradicho"
    assert estados["calidad"] == "no_verificable"


def test_sin_verificar(recibo_b):
    for limite in ("calidad", "entrega", "reputación del comercio"):
        assert limite in recibo_b.sin_verificar


def test_sin_verificar_incluye_lo_solo_declarado(vistas, ficha, catalogo, ahora):
    recibo = evaluar(ficha(), vistas("baseline"), "cart_1", {}, ahora=ahora).recibo
    assert any("categoria" in item for item in recibo.sin_verificar)


def test_por_que_gano(recibo_b, vistas, ficha, catalogo, ahora):
    assert "NK-OUT-01" in recibo_b.por_que_gano
    baseline = evaluar(ficha(), vistas("baseline"), "cart_1", catalogo, ahora=ahora).recibo
    assert "primera del ranking" in baseline.por_que_gano


def test_id_deterministico(vistas, ficha, catalogo, ahora):
    def recibo(cart_id="cart_1", checkout_hash=None, cuando=ahora):
        return evaluar(
            ficha(), vistas("baseline"), cart_id, catalogo,
            checkout_hash=checkout_hash, ahora=cuando,
        ).recibo

    assert recibo().id == recibo().id
    assert recibo().id != recibo(cart_id="cart_3").id
    assert recibo().id != recibo(checkout_hash="otro").id
    assert recibo().id != recibo(cuando=ahora + timedelta(seconds=1)).id


def test_emitido_por_defecto_es_ahora_en_utc(vistas, ficha, catalogo):
    antes = datetime.now(timezone.utc)
    recibo = evaluar(ficha(), vistas("baseline"), "cart_1", catalogo).recibo
    assert recibo.emitido.tzinfo is not None
    assert antes <= recibo.emitido <= datetime.now(timezone.utc)


def test_cart_inexistente_tambien_tiene_recibo(vistas, ficha, catalogo, ahora):
    recibo = evaluar(ficha(), vistas("baseline"), "cart_99", catalogo, ahora=ahora).recibo
    assert recibo.status == "escalar"
    assert recibo.verificaciones == []


# --- firma -------------------------------------------------------------------------


def test_firmar_y_verificar(recibo_b, clave):
    firmado = firmar_recibo(recibo_b, clave)
    assert firmado.kid == "guard-key-1"
    assert recibo_b.firma is None  # no modifica el original
    assert verificar_recibo(firmado, clave.public_key())
    assert verificar_recibo(firmado, clave)  # también acepta la privada


def test_firma_es_jws_compacto_es256_sobre_el_payload_canonico(recibo_b, clave):
    firmado = firmar_recibo(recibo_b, clave, kid="otra-clave")
    encabezado, payload, firma = firmado.firma.split(".")
    assert json.loads(_b64url_decode(encabezado)) == {"alg": "ES256", "kid": "otra-clave"}
    assert _b64url_decode(payload) == recibo_b.payload_canonico()
    assert len(_b64url_decode(firma)) == 64  # r || s, como _create_jwt


def test_la_firma_valida_con_una_libreria_jose_y_el_jwk(recibo_b, clave):
    firmado = firmar_recibo(recibo_b, clave)
    token = jws.JWS()
    token.deserialize(firmado.firma)
    token.verify(jwk.JWK(**clave_publica_jwk(clave)))  # lanza si no valida
    assert token.payload == recibo_b.payload_canonico()


@pytest.mark.parametrize(
    "cambio",
    [
        {"status": "aprobado"},
        {"elegido": "cart_1"},
        {"ranking": ["cart_4", "cart_1", "cart_3", "cart_2"]},
        {"checkout_hash": "otro-hash"},
        {"kid": "otra-clave"},
    ],
)
def test_detecta_alteraciones(recibo_b, clave, cambio):
    alterado = firmar_recibo(recibo_b, clave).model_copy(update=cambio)
    assert not verificar_recibo(alterado, clave.public_key())


def test_detecta_firma_alterada_u_otra_clave(recibo_b, clave):
    firmado = firmar_recibo(recibo_b, clave)
    encabezado, payload, firma = firmado.firma.split(".")
    otra_firma = firma[:-2] + ("AA" if firma[-2:] != "AA" else "BB")
    roto = firmado.model_copy(update={"firma": f"{encabezado}.{payload}.{otra_firma}"})
    assert not verificar_recibo(roto, clave.public_key())
    otra_clave = ec.generate_private_key(ec.SECP256R1())
    assert not verificar_recibo(firmado, otra_clave.public_key())


@pytest.mark.parametrize("firma", [None, "", "a.b", "no.es.base64!", "x.y.z.w"])
def test_firma_ausente_o_mal_formada_no_verifica(recibo_b, clave, firma):
    assert not verificar_recibo(recibo_b.model_copy(update={"firma": firma}), clave)


def test_sobrevive_ida_y_vuelta_por_json(recibo_b, clave):
    firmado = firmar_recibo(recibo_b, clave)
    vuelta = Recibo.model_validate_json(firmado.model_dump_json())
    assert verificar_recibo(vuelta, clave.public_key())
    vuelta_dict = Recibo.model_validate(json.loads(json.dumps(firmado.model_dump(mode="json"))))
    assert verificar_recibo(vuelta_dict, clave.public_key())


# --- clave -------------------------------------------------------------------------


def test_cargar_o_crear_clave(tmp_path):
    ruta = tmp_path / "sub" / "guard_signing_key.pem"
    creada = cargar_o_crear_clave(ruta)
    assert ruta.exists()
    assert ruta.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    cargada = cargar_o_crear_clave(str(ruta))
    assert creada.private_numbers() == cargada.private_numbers()
    assert isinstance(cargada.curve, ec.SECP256R1)


def test_clave_publica_jwk(clave):
    datos = clave_publica_jwk(clave)
    assert datos["kty"] == "EC" and datos["crv"] == "P-256"
    assert datos["kid"] == "guard-key-1" and datos["alg"] == "ES256"
    assert len(_b64url_decode(datos["x"])) == 32 and len(_b64url_decode(datos["y"])) == 32
    assert "d" not in datos
    assert clave_publica_jwk(clave.public_key(), kid="k2")["kid"] == "k2"
