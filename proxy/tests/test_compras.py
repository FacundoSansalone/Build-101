"""Registro de compras de la etapa 2 (`proxy/compras.py`): las esperas.

`Compra.nueva_espera()` es la forma pública de volver a preguntar (cada
repregunta del intake): nadie fuera de `compras.py` toca sus `Future`.
"""

from __future__ import annotations

import pytest

from proxy.compras import EstadoInvalido, RegistroCompras

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_nueva_espera_vuelve_a_esperar_una_respuesta():
    registro = RegistroCompras()
    compra = registro.crear("pedido", "¿para qué?", None, "ctx-1")
    compra.fijar_plazo(1.0)
    registro.responder(compra.id, "para programar")
    assert await compra.esperar_respuesta() == ("para programar", False)
    assert compra.estado == "negociando"

    compra.nueva_espera("¿hasta cuánto?")
    assert compra.estado == "esperando_contexto"
    assert compra.pregunta == "¿hasta cuánto?"
    compra.fijar_plazo(1.0)
    registro.responder(compra.id, "hasta 1200 dólares")
    assert await compra.esperar_respuesta() == ("hasta 1200 dólares", False)


async def test_sin_nueva_espera_no_se_puede_responder_dos_veces():
    registro = RegistroCompras()
    compra = registro.crear("pedido", "¿para qué?", None, "ctx-1")
    registro.responder(compra.id, "una")
    compra.estado = "esperando_contexto"  # aunque el estado lo permita…
    with pytest.raises(EstadoInvalido):
        registro.responder(compra.id, "otra")  # …la espera ya se resolvió


async def test_nueva_espera_despues_de_vencer():
    registro = RegistroCompras()
    compra = registro.crear("pedido", "¿para qué?", None, "ctx-1")
    compra.fijar_plazo(0.0)
    assert await compra.esperar_respuesta() == (None, True)
    compra.nueva_espera("¿y para cuándo?")
    compra.fijar_plazo(1.0)
    registro.responder(compra.id, "el lunes")
    assert await compra.esperar_respuesta() == ("el lunes", False)
