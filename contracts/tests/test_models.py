"""Tests de los contratos (sin red ni LLM)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts import (
    CheckoutVisto,
    Chequeo,
    Evento,
    Ficha,
    Oferta,
    OfertasVistas,
    Recibo,
    Veredicto,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
ESCENARIOS = ["baseline", "A", "B", "B2", "C"]


def _fixture(nombre: str) -> dict:
    return json.loads((FIXTURES / f"scenario_{nombre}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("nombre", ESCENARIOS)
def test_ficha_desde_intent_de_cada_fixture(nombre):
    ficha = Ficha.desde_intent(_fixture(nombre)["intent"])
    por_campo = {r.campo: r for r in ficha.requisitos}
    assert ficha.confirmada
    assert por_campo["precio"].operador == "maximo"
    assert por_campo["precio"].valor == 150.0
    assert por_campo["talle"].operador == "incluye"
    assert por_campo["marca"].obligatorio is False
    assert all(r.obligatorio for r in ficha.requisitos if r.campo != "marca")


def test_ficha_rechaza_claves_desconocidas():
    with pytest.raises(ValueError):
        Ficha.desde_intent({"hard": {"color": "rojo"}})


@pytest.mark.parametrize("nombre", ESCENARIOS)
def test_ofertas_vistas_acepta_offers_seen_tal_cual(nombre):
    datos = _fixture(nombre)
    vistas = OfertasVistas(carts=datos["carts"], offer_details=datos["offer_details"])
    assert vistas.carts == datos["carts"]
    assert vistas.comercio_id is None


def test_oferta_no_serializa_texto_libre():
    oferta = Oferta(
        cart_id="cart_1", sku="X", titulo="t", marca="m", categoria="c",
        uso="u", precio=1.0, moneda="USD", texto_libre_descartado="[Note ...]",
    )
    assert "texto_libre_descartado" not in oferta.model_dump()


def _recibo(**extra) -> Recibo:
    return Recibo(
        id="r1",
        emitido=datetime(2026, 10, 17, tzinfo=timezone.utc),
        ficha=Ficha.desde_intent(_fixture("B")["intent"]),
        consideradas=["cart_1"],
        filtradas={},
        verificaciones=[],
        ranking=["cart_1"],
        elegido="cart_1",
        por_que_gano="único",
        sin_verificar=["calidad"],
        status="aprobado",
        **extra,
    )


def test_payload_canonico_excluye_firma_y_kid():
    sin_firma = _recibo().payload_canonico()
    con_firma = _recibo(firma="a.b.c", kid="guard-key-1").payload_canonico()
    assert sin_firma == con_firma
    assert {"firma", "kid"}.isdisjoint(json.loads(sin_firma))


def test_veredicto_round_trip_json_con_status():
    v = Veredicto(
        status="bloqueado",
        cart_id_elegido="cart_4",
        cart_id_recomendado="cart_1",
        ranking=["cart_1", "cart_4"],
        chequeos=[Chequeo(tipo="cumplimiento", resultado="bloquea", detalle="x")],
        motivo="no cumple",
        recibo=_recibo(),
    )
    dump = v.model_dump(mode="json")
    assert dump["status"] == "bloqueado"
    assert Veredicto.model_validate(dump) == v


def test_checkout_visto_conserva_el_data_part_tal_cual():
    data_part = {
        "cart_id": "cart_1",
        "checkout_jwt": "h.p.s",
        "checkout_hash": "abc",
        "item_label": "texto libre",
        "amount": 120.0,
        "amount_cents": 12000,
        "currency": "USD",
    }
    visto = CheckoutVisto(**data_part)
    assert visto.model_dump() == data_part


def test_evento_minimo():
    e = Evento(id="e1", ts=datetime.now(timezone.utc), guard_activo=True, tipo="veredicto")
    assert e.datos == {}
