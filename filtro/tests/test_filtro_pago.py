"""Los pasos del pago dirigidos al usuario y sus imitaciones con órdenes al agente.

Falso positivo del 2026-10-18: con el modelo, la respuesta honesta del
comercio a `initiate_payment` (el desafío OTP del procesador de pagos del
sample de AP2) se cortaba como `modelo:anula_instrucciones` (0,73). La
corrección está en el encuadre de las preguntas (`filtro/modelo.py`), no en
el código: no hay lista blanca de textos. Estos tests fijan sin red lo que
no depende del modelo; la medición con el modelo real está en el README
(`python -m filtro.evaluar --decisiones`).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from decisiones import DecisionesError
from filtro import MOTIVO_MALICIA, MOTIVO_OK, MOTIVO_SIN_VERIFICAR, FiltroSeguridad
from filtro.casos import Caso, casos, challenge_otp
from filtro.modelo import PREGUNTAS, UMBRALES
from filtro.tests.falsos import ClienteFalso
from filtro.textos import extraer_textos

PROCESADOR = (
    Path(__file__).resolve().parents[2]
    / "world/ap2/code/samples/python/src/roles/merchant_payment_processor_agent/tools.py"
)
CASOS = {c.nombre: c for c in casos()}
# Pasos del pago dirigidos al usuario humano (honestos) y sus imitaciones.
PAGO_HONESTOS = (
    "var_challenge_otp",
    "var_challenge_incorrecto",
    "var_pago_otp_ok",
    "var_codigo_sms",
    "var_confirmar_pedido",
    "var_direccion_envio",
)
PAGO_MALICIOSOS = (
    "var_pago_acepta_total",
    "var_pago_ignora_tope",
    "var_pago_otp_solo",
    "var_pago_pide_tarjeta",
)


def _textos_del_sample() -> tuple[str, str]:
    """El texto y el `display_text` que arma `_raise_challenge` en el sample."""
    if not PROCESADOR.exists():
        pytest.skip("falta el sample de AP2 en world/ap2")
    arbol = ast.parse(PROCESADOR.read_text("utf-8"))
    funcion = next(
        n for n in ast.walk(arbol)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_raise_challenge"
    )
    texto = display = None
    for n in ast.walk(funcion):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == "display_text":
                    display = ast.literal_eval(v)
        if isinstance(n, ast.keyword) and n.arg == "text":
            texto = ast.literal_eval(n.value)
    assert texto and display
    return texto, display


# -- el caso real -------------------------------------------------------------------


def test_el_caso_otp_usa_el_texto_real_del_procesador_de_pagos():
    texto, display = _textos_del_sample()
    mensaje = challenge_otp()["status"]["message"]
    assert mensaje["parts"][0] == {"kind": "text", "text": texto}
    assert mensaje["parts"][1]["data"]["challenge"]["display_text"] == display


def test_el_caso_otp_tiene_la_forma_que_reenvia_el_comercio():
    tarea = challenge_otp()
    assert tarea["kind"] == "task" and tarea["status"]["state"] == "input-required"
    assert tarea["status"]["message"]["role"] == "agent"
    assert tarea["status"]["message"]["parts"][1]["data"]["challenge"]["type"] == "otp"
    # El eco de nuestro pedido no es texto del comercio: el filtro no lo mira.
    assert all(m["role"] == "user" for m in tarea["history"])


def test_el_desafio_otp_va_al_modelo_como_dato_y_pasa_si_el_modelo_no_marca():
    cliente = ClienteFalso()
    r = FiltroSeguridad(cliente=cliente)(challenge_otp())
    assert r.pasa is True and r.motivo == MOTIVO_OK and r.senales == []
    ((estado, preguntas),) = cliente.llamadas
    _, display = _textos_del_sample()
    assert display in json.dumps(estado, ensure_ascii=False)
    for p in preguntas:  # las preguntas son constantes: nada del comercio
        assert "issuer" not in json.dumps(p.instrucciones, ensure_ascii=False)


# -- sin lista blanca -----------------------------------------------------------------


@pytest.mark.parametrize("nombre", PAGO_HONESTOS)
@pytest.mark.parametrize("pregunta", sorted(UMBRALES))
def test_no_hay_lista_blanca_si_el_modelo_marca_un_paso_del_pago_bloquea(nombre, pregunta):
    """La corrección no deja pasar nada por su texto: si el modelo dice que sí
    (como el 0,73 de antes), el umbral en código bloquea igual."""
    cliente = ClienteFalso({pregunta: 0.73})
    r = FiltroSeguridad(cliente=cliente)(CASOS[nombre].respuesta)
    assert r.pasa is False and r.motivo == MOTIVO_MALICIA
    assert r.senales == [f"modelo:{pregunta}"]


def test_las_preguntas_no_nombran_el_texto_del_sample():
    """El encuadre describe una categoría (quién recibe el texto y para qué),
    no el texto del OTP: copiarlo no alcanza para pasar."""
    _, display = _textos_del_sample()
    for p in PREGUNTAS:
        instrucciones = json.dumps(p.instrucciones, ensure_ascii=False).lower()
        assert "otp" not in instrucciones and "123" not in instrucciones
        assert "issuer" not in instrucciones and display.lower() not in instrucciones


def test_cada_pregunta_separa_el_pedido_al_usuario_de_la_orden_al_agente():
    for p in PREGUNTAS:
        instrucciones = str(p.instrucciones)
        assert "pasos normales del pago dirigidos al usuario humano" in instrucciones
        # El contrapeso: con forma de paso del pago, igual cuenta.
        assert "aunque venga presentado como un paso del pago" in instrucciones
        assert "CVV" in instrucciones and "pasarse del tope" in instrucciones


# -- las imitaciones ----------------------------------------------------------------


def test_estan_los_dos_lados_del_paso_del_pago():
    for nombre in PAGO_HONESTOS:
        assert CASOS[nombre].malicioso is False, nombre
    for nombre in PAGO_MALICIOSOS:
        assert CASOS[nombre].malicioso is True, nombre


@pytest.mark.parametrize("nombre", PAGO_MALICIOSOS)
def test_las_imitaciones_del_paso_del_pago_solo_las_ve_el_modelo(nombre):
    """Sin palabras clave: las heurísticas no las ven y queda el modelo."""
    caso: Caso = CASOS[nombre]
    assert caso.heuristica_lo_ve is False
    assert FiltroSeguridad(solo_heuristicas=True)(caso.respuesta).pasa is True
    cliente = ClienteFalso({"anula_instrucciones": 0.99})
    r = FiltroSeguridad(cliente=cliente)(caso.respuesta)
    assert r.pasa is False and r.motivo == MOTIVO_MALICIA
    assert r.senales == ["modelo:anula_instrucciones"]


@pytest.mark.parametrize("nombre", PAGO_MALICIOSOS)
def test_el_bloqueo_no_copia_texto_del_comercio(nombre):
    cliente = ClienteFalso({p: 0.99 for p in UMBRALES})
    r = FiltroSeguridad(cliente=cliente)(CASOS[nombre].respuesta)
    libres = [t.texto for t in extraer_textos(CASOS[nombre].respuesta) if t.libre]
    salida = r.motivo + " ".join(r.senales)
    assert r.motivo == MOTIVO_MALICIA
    assert all(s.startswith("modelo:") for s in r.senales)
    assert not any(texto in salida for texto in libres)
    assert "21" not in salida and "USD" not in salida


# -- falla cerrado ------------------------------------------------------------------


def test_el_desafio_otp_sin_modelo_o_con_error_bloquea():
    sin_cliente = FiltroSeguridad()(challenge_otp())
    assert sin_cliente.pasa is False and sin_cliente.motivo == MOTIVO_SIN_VERIFICAR
    assert sin_cliente.senales == ["filtro:sin_decisiones"]
    con_error = FiltroSeguridad(cliente=ClienteFalso(error=DecisionesError("x")))(challenge_otp())
    assert con_error.pasa is False and con_error.senales == ["filtro:decisiones_error"]
    no_se = FiltroSeguridad(cliente=ClienteFalso(rechazos=("anula_instrucciones",)))(
        challenge_otp()
    )
    assert no_se.pasa is False and no_se.senales == ["filtro:sin_respuesta"]
