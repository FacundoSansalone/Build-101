"""Modo demo: `FILTRO_SOLO_HEURISTICAS=1`, lo que pone `deploy/start_all.sh`.

La demo del pitch son cuatro corridas con el mismo pedido ("20 computadoras
para el lunes"): proveedor malicioso y honesto, con y sin sistema
(docs/FLUJO.md §5). Con el sistema y sin el modelo, las heurísticas solas
tienen que cortar la respuesta del malicioso (`computadoras_malicioso`, el
texto real de world/) y dejar pasar toda la compra del honesto
(`computadoras`): la búsqueda, el checkout firmado, el desafío OTP del
procesador de pagos, el reintento y el pago completado.
"""

from __future__ import annotations

import copy

import pytest

import filtro
from filtro import MOTIVO_MALICIA, MOTIVO_SOLO_HEURISTICAS, desde_entorno
from filtro.casos import casos, como_tarea, computadoras_malicioso
from filtro.heuristicas import ANULA_INSTRUCCIONES, INSTRUCCION_IA, MANIPULACION

CASOS = {c.nombre: c for c in casos()}
# El entorno de deploy/start_all.sh, sin claves de decisiones.
ENTORNO_DEMO = {"GUARD_FILTRO": "filtro:filtrar", "FILTRO_SOLO_HEURISTICAS": "1"}
# La compra del honesto, en orden: búsqueda, checkout con JWT, desafío OTP
# (el texto real del sample), código incorrecto y pago completado.
COMPRA_HONESTA = (
    "computadoras",
    "var_checkout",
    "var_challenge_otp",
    "var_challenge_incorrecto",
    "var_pago_otp_ok",
)


@pytest.fixture
def filtro_demo() -> filtro.FiltroSeguridad:
    f = desde_entorno(ENTORNO_DEMO)
    assert f.solo_heuristicas is True and f.cliente is None
    return f


def _objetivo(datos: dict) -> tuple[dict, dict]:
    cart_id = datos["expected"]["attack_target_cart_id"]
    cart = next(c for c in datos["carts"] if c["cart_id"] == cart_id)
    detalle = next(o for o in datos["offer_details"] if o["cart_id"] == cart_id)
    return cart, detalle


def _inyeccion(datos: dict) -> str:
    cart, _ = _objetivo(datos)
    return cart["item_label"][cart["item_label"].index("[Instructions") :]


def test_corta_la_respuesta_del_proveedor_malicioso(filtro_demo):
    r = filtro_demo(CASOS["computadoras_malicioso"].respuesta)
    assert r.pasa is False
    assert r.motivo == MOTIVO_MALICIA
    # Más de una señal: no depende de un solo patrón.
    assert {INSTRUCCION_IA, ANULA_INSTRUCCIONES, MANIPULACION} <= set(r.senales)


@pytest.mark.parametrize("lugar", ["item_label", "description"])
def test_alcanza_con_la_inyeccion_en_un_solo_lugar(filtro_demo, lugar):
    """La inyección viaja en el item_label (lo que ve el agente) y en la
    descripción (lo que declara el comercio): cualquiera de las dos corta."""
    datos = copy.deepcopy(computadoras_malicioso())
    inyeccion = _inyeccion(datos)
    cart, detalle = _objetivo(datos)
    if lugar == "item_label":
        detalle["description"] = detalle["description"].replace(inyeccion, "").strip()
    else:
        cart["item_label"] = cart["item_label"].replace(inyeccion, "").strip()
    r = filtro_demo(como_tarea(datos))
    assert r.pasa is False and r.motivo == MOTIVO_MALICIA


def test_sin_la_inyeccion_la_misma_oferta_de_tablets_pasa(filtro_demo):
    """Lo que corta es la inyección, no que la oferta no cumpla el pedido
    (FLUJO.md, regla 4): las tablets sin la nota pasan, y las frena el Guard."""
    datos = copy.deepcopy(computadoras_malicioso())
    inyeccion = _inyeccion(datos)
    cart, detalle = _objetivo(datos)
    cart["item_label"] = cart["item_label"].replace(inyeccion, "").strip()
    detalle["description"] = detalle["description"].replace(inyeccion, "").strip()
    r = filtro_demo(como_tarea(datos))
    assert r.pasa is True and r.senales == ["modo:solo_heuristicas"]


@pytest.mark.parametrize("paso", COMPRA_HONESTA)
def test_la_compra_del_proveedor_honesto_pasa_entera(filtro_demo, paso):
    r = filtro_demo(CASOS[paso].respuesta)
    assert r.pasa is True, r.senales
    assert r.motivo == MOTIVO_SOLO_HEURISTICAS
    assert r.senales == ["modo:solo_heuristicas"]


def test_filtrar_como_lo_carga_el_proxy_corta_el_malicioso_y_deja_el_otp(monkeypatch):
    """`GUARD_FILTRO=filtro:filtrar` arma el filtro desde el entorno en la
    primera llamada: con el de la demo y sin claves, igual corta y deja pasar."""
    for clave in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "DECISIONES_PROVEEDOR"):
        monkeypatch.delenv(clave, raising=False)
    for clave, valor in ENTORNO_DEMO.items():
        monkeypatch.setenv(clave, valor)
    filtro.configurar(None)
    try:
        assert filtro.filtrar(CASOS["computadoras_malicioso"].respuesta).pasa is False
        for paso in COMPRA_HONESTA:
            assert filtro.filtrar(CASOS[paso].respuesta).pasa is True, paso
    finally:
        filtro.configurar(None)
