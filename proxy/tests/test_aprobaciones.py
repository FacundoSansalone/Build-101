"""Corrección 7: `escalar` retiene el checkout hasta que un humano lo aprueba.

La aprobación es de un solo uso y vale para ese contexto, cart y monto. El
agente reintenta create_checkout y el checkout pasa intacto.
"""

from __future__ import annotations

import json

import pytest

from proxy.tests.ayudantes import (
    buscar,
    checkout,
    checkouts,
    cliente_de,
    eventos,
    evaluador_espia,
    solo_en_checkout,
    texto_estado,
    veredictos,
)
from proxy.tests.fake_world import FakeMerchant, cargar_fixture

pytestmark = pytest.mark.anyio

APROBACIONES = "/guard/aprobaciones"


async def _aprobar(c, contexto: str, cart_id: str):
    return await c.post(APROBACIONES, json={"contexto_id": contexto, "cart_id": cart_id})


async def test_escalar_retiene_el_checkout(armar_proxy, merchant: FakeMerchant):
    evaluador, _ = evaluador_espia("escalar", motivo="precio contradicho por el catálogo")
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "C")
        resp = await checkout(c, contexto, "cart_1")
        evs = await eventos(c)

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert checkouts(task) == []
    assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content
    texto = texto_estado(task)
    assert texto.startswith("Pre-Sign Guard: escalar. precio contradicho por el catálogo")
    assert "requiere aprobación humana" in texto.lower()
    (v,) = veredictos(task)
    assert v["status"] == "escalar"
    bloqueado = evs[-1]
    assert bloqueado["tipo"] == "bloqueado"
    assert bloqueado["datos"] == {
        "cart_id": "cart_1",
        "motivo": "precio contradicho por el catálogo",
        "status": "escalar",
        "requiere_aprobacion": True,
    }


async def test_aprobar_sin_escalar_previo_da_409(cliente):
    contexto = await buscar(cliente, "B")
    await checkout(cliente, contexto, "cart_4")  # bloqueado, no escalar
    assert (await _aprobar(cliente, contexto, "cart_4")).status_code == 409
    assert (await _aprobar(cliente, "ctx-que-no-existe", "cart_1")).status_code == 409


async def test_aprobacion_humana_deja_pasar_el_reintento_una_vez(
    armar_proxy, merchant: FakeMerchant
):
    evaluador, llamadas = evaluador_espia("escalar")
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "C")
        retenido = (await checkout(c, contexto, "cart_1")).json()["result"]
        assert retenido["status"]["state"] == "failed"

        resp = await _aprobar(c, contexto, "cart_1")
        assert resp.status_code == 201
        aprobacion = resp.json()
        assert aprobacion["contexto_id"] == contexto and aprobacion["cart_id"] == "cart_1"
        assert aprobacion["estado"] == "pendiente"
        lista = (await c.get(APROBACIONES)).json()
        assert [a["id"] for a in lista["pendientes"]] == [aprobacion["id"]]
        assert lista["usadas"] == []

        reintento = await checkout(c, contexto, "cart_1")
        lista = (await c.get(APROBACIONES)).json()
        otra_vez = (await checkout(c, contexto, "cart_1")).json()["result"]
        evs = await eventos(c)

    task = reintento.json()["result"]
    assert task["status"]["state"] == "completed"
    # El checkout pasa intacto: byte a byte el que firmó el comercio.
    firmado = merchant.checkouts[1]
    assert checkouts(task) == [firmado]
    assert firmado["checkout_jwt"].encode() in reintento.content
    assert merchant.firma_valida(checkouts(task)[0]["checkout_jwt"])
    assert veredictos(task)[0]["status"] == "escalar"
    assert lista["pendientes"] == []
    assert [a["id"] for a in lista["usadas"]] == [aprobacion["id"]]
    assert lista["usadas"][0]["estado"] == "usada"
    # De un solo uso: el siguiente reintento vuelve a quedar retenido.
    assert otra_vez["status"]["state"] == "failed"
    assert checkouts(otra_vez) == []
    reenviados = [e for e in evs if e["tipo"] == "reenviado"
                  and e["datos"].get("aprobacion_humana")]
    assert len(reenviados) == 1
    assert reenviados[0]["datos"]["metodo"] == "create_checkout"
    assert reenviados[0]["contexto_id"] == contexto
    assert len(llamadas) == 3  # el Guard evalúa cada intento


async def test_aprobacion_no_aplica_si_cambia_el_monto(armar_proxy, merchant: FakeMerchant):
    evaluador, _ = evaluador_espia("escalar")
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "C")
        await checkout(c, contexto, "cart_1")
        assert (await _aprobar(c, contexto, "cart_1")).status_code == 201

        def mas_caro(respuesta, pedido):
            cart = next(x for x in cargar_fixture("C")["carts"] if x["cart_id"] == "cart_1")
            nuevo = merchant.armar_checkout(cart, monto=cart["amount"] + 4)
            merchant.ultimo_checkout = nuevo
            parte = respuesta["result"]["artifacts"][0]["parts"][0]
            parte["data"]["ap2.checkout"] = nuevo
            return respuesta

        merchant.manipular = solo_en_checkout(mas_caro)
        resp = await checkout(c, contexto, "cart_1")
        lista = (await c.get(APROBACIONES)).json()

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert checkouts(task) == []
    assert len(lista["pendientes"]) == 1 and lista["usadas"] == []


async def test_aprobacion_no_salta_un_bloqueado(armar_proxy):
    estados = iter(["escalar", "bloqueado"])
    from contracts import Veredicto
    from proxy.evaluador import EvaluadorCargado

    def evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
                checkout_hash=None, ahora=None, checkout=None):
        return Veredicto(status=next(estados), cart_id_elegido=cart_id_elegido,
                         cart_id_recomendado=None, ranking=[], chequeos=[], motivo="m")

    app = armar_proxy(evaluador=EvaluadorCargado(evaluar=evaluar, nombre="guard"))
    async with cliente_de(app) as c:
        contexto = await buscar(c, "C")
        await checkout(c, contexto, "cart_1")
        assert (await _aprobar(c, contexto, "cart_1")).status_code == 201
        resp = await checkout(c, contexto, "cart_1")

    assert resp.json()["result"]["status"]["state"] == "failed"
    assert checkouts(resp.json()["result"]) == []


@pytest.mark.parametrize(
    "body",
    [{}, {"contexto_id": "c"}, {"contexto_id": "", "cart_id": "x"},
     {"contexto_id": 1, "cart_id": "x"}, {"contexto_id": "c", "cart_id": "x", "otro": 1},
     [], "c"],
)
async def test_aprobacion_invalida_da_400(cliente, body):
    resp = await cliente.post(APROBACIONES, content=json.dumps(body),
                              headers={"content-type": "application/json"})
    assert resp.status_code == 400


async def test_aprobacion_con_json_roto_da_400(cliente):
    resp = await cliente.post(APROBACIONES, content=b"{",
                              headers={"content-type": "application/json"})
    assert resp.status_code == 400


async def test_aprobar_dos_veces_devuelve_la_misma(armar_proxy):
    evaluador, _ = evaluador_espia("escalar")
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "C")
        await checkout(c, contexto, "cart_1")
        a = await _aprobar(c, contexto, "cart_1")
        b = await _aprobar(c, contexto, "cart_1")
        lista = (await c.get(APROBACIONES)).json()

    assert a.status_code == 201 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]
    assert len(lista["pendientes"]) == 1


# -- compras de integrador (revisión adversarial del gateway) --------------------------


def test_el_registro_no_deja_aprobar_por_el_panel_un_checkout_de_integrador():
    """La marca `de_api` no depende de que la compra siga en memoria: el
    dashboard nunca aprueba un checkout de integrador (lo aprueba su dueño
    por /v1)."""
    from proxy.aprobaciones import DeIntegrador, RegistroAprobaciones

    registro = RegistroAprobaciones()
    registro.registrar_veredicto("ctx-api", "cart_1", "escalar", 12000, "USD", de_api=True)
    with pytest.raises(DeIntegrador):
        registro.aprobar("ctx-api", "cart_1")
    # Un veredicto posterior sin la marca no la borra.
    registro.registrar_veredicto("ctx-api", "cart_1", "escalar", 12000, "USD")
    with pytest.raises(DeIntegrador):
        registro.aprobar("ctx-api", "cart_1")
    aprobacion, creada = registro.aprobar("ctx-api", "cart_1", via_api=True)
    assert creada and aprobacion.via_api
    assert "via_api" not in aprobacion.a_json()  # el JSON de siempre


def test_solo_api_ignora_una_aprobacion_que_no_es_del_dueno():
    from proxy.aprobaciones import RegistroAprobaciones

    registro = RegistroAprobaciones()
    registro.registrar_veredicto("ctx-1", "cart_1", "escalar", 12000, "USD")
    registro.aprobar("ctx-1", "cart_1")  # del panel (contexto de la demo)
    assert registro.disponible("ctx-1", "cart_1", 12000, "USD") is not None
    assert registro.disponible("ctx-1", "cart_1", 12000, "USD", solo_api=True) is None
    assert registro.consumir("ctx-1", "cart_1", 12000, "USD", solo_api=True) is None
    assert registro.consumir("ctx-1", "cart_1", 12000, "USD") is not None


def test_usar_una_aprobacion_reclamada_aunque_otro_la_haya_descartado():
    """Dos checkouts concurrentes: el que perdió el reclamo la descarta de
    memoria; el que lo ganó igual la usa (si no, se quemaba sin pasar)."""
    from proxy.aprobaciones import RegistroAprobaciones

    registro = RegistroAprobaciones()
    registro.registrar_veredicto("ctx-1", "cart_1", "escalar", 12000, "USD", de_api=True)
    aprobacion, _ = registro.aprobar("ctx-1", "cart_1", via_api=True)
    assert registro.descartar(aprobacion.id)
    usada = registro.usar(aprobacion, "task-1")
    assert usada.estado == "usada" and usada.task_id == "task-1"
    assert registro.listar()["pendientes"] == []
    assert [a["id"] for a in registro.listar()["usadas"]] == [aprobacion.id]
    registro.restaurar_pendiente(aprobacion)  # una usada no vuelve
    assert registro.pendiente("ctx-1", "cart_1") is None
