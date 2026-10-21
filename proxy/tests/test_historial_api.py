"""El historial por `/v1` (panel del cliente, fase 1).

- `GET /v1/compras` con filtros (`desde`, `hasta`, `referencia`, `alerta`) y
  cursor opaco, y en cada compra `resultado`, `alertas` e
  `intento_de_estafa`; con varias instancias, sin N+1 al almacén.
- `GET /v1/compras/{id}/explicacion` y `GET /v1/resumen`.

Primero la validación contra el `NucleoFalso` de `test_api_v1`; después de
punta a punta con el gateway (el Guard real sobre el escenario B2, el filtro
que corta la inyección de B) y la prueba de fuga: nada del comercio sale.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from contracts import ResultadoFiltro
from proxy.api_v1 import codificar_cursor, decodificar_cursor
from proxy.estado import EstadoMemoria, EstadoSupabase
from proxy.evaluador import EvaluadorNoDisponible, cargar_evaluador
from proxy.tests.ayudantes import evaluador_espia
from proxy.tests.fake_world import cargar_fixture
from proxy.tests.gateway_ayudantes import (
    CLAVE_OTRO,
    GATEWAY_PUBLIC_URL,
    armar_gateway,
    buscar,
    checkout,
    cliente_publico,
    crear_compra,
)
from proxy.tests.postgrest_falso import BaseFalsa
from proxy.tests.test_api_v1 import ACME, COMPRA_ID, NucleoFalso, _app, _error
from proxy.tipos_gateway import ErrorApi

pytestmark = pytest.mark.anyio

URL_SUPABASE = "https://base.supabase.test"


@pytest.fixture
def nucleo() -> NucleoFalso:
    return NucleoFalso()


@pytest.fixture
async def cliente(nucleo: NucleoFalso) -> AsyncIterator[httpx.AsyncClient]:
    transporte = httpx.ASGITransport(app=_app(nucleo))
    async with httpx.AsyncClient(transport=transporte, base_url="http://gw.test:8030") as c:
        yield c


# -- el listado: validación ----------------------------------------------------------


async def test_el_listado_pasa_los_filtros_al_nucleo(cliente, nucleo):
    r = await cliente.get("/v1/compras", params={
        "limite": "10", "estado": "negociando", "desde": "2026-10-01",
        "hasta": "2026-10-20", "referencia": "  ord-1  ", "alerta": "dato_falso",
    })
    assert r.status_code == 200, r.text
    filtros = nucleo.filtros
    assert filtros.limite == 10 and filtros.estado == "negociando"
    assert filtros.desde == datetime(2026, 10, 1, tzinfo=timezone.utc)
    # Un día en `hasta` cuenta entero: hasta el comienzo del siguiente.
    assert filtros.hasta == datetime(2026, 10, 21, tzinfo=timezone.utc)
    assert filtros.referencia == "ord-1" and filtros.alerta == "dato_falso"
    assert filtros.cursor is None


@pytest.mark.parametrize("valor, esperado", [
    ("2026-10-21T10:00:00Z", datetime(2026, 10, 21, 10, tzinfo=timezone.utc)),
    ("2026-10-21T10:00:00+00:00", datetime(2026, 10, 21, 10, tzinfo=timezone.utc)),
    # Un `+` sin codificar llega como espacio.
    ("2026-10-21T07:00:00 03:00", datetime(2026, 10, 21, 4, tzinfo=timezone.utc)),
    ("2026-10-21T10:00:00", datetime(2026, 10, 21, 10, tzinfo=timezone.utc)),
    ("2026-10-21T10:00:00.123456-03:00", datetime(2026, 10, 21, 13, 0, 0, 123456, tzinfo=timezone.utc)),
])
async def test_fechas_con_hora(cliente, nucleo, valor, esperado):
    r = await cliente.get("/v1/compras", params={"desde": valor})
    assert r.status_code == 200, r.text
    assert nucleo.filtros.desde == esperado


@pytest.mark.parametrize("params, campo", [
    ({"desde": "ayer"}, "desde"),
    ({"desde": "2026-13-01"}, "desde"),
    ({"hasta": "2026-10-21T25:00:00Z"}, "hasta"),
    ({"hasta": "20261021"}, "hasta"),
    ({"desde": "2026-10-21", "hasta": "2026-10-20"}, "desde"),
    ({"desde": "2026-10-21T12:00:00Z", "hasta": "2026-10-21T12:00:00Z"}, "desde"),
    ({"alerta": "estafa"}, "alerta"),
    ({"referencia": "x" * 201}, "referencia"),
    ({"referencia": "ord\u0007"}, "referencia"),
    ({"estado": "inventado"}, "estado"),
    ({"cursor": "no es un cursor"}, "cursor"),
    ({"cursor": "e30"}, "cursor"),
    ({"cursor": base64.urlsafe_b64encode(b'{"v":2,"c":"2026-10-21T12:00:00+00:00","i":"cmp-0123456789abcdef"}').decode().rstrip("=")}, "cursor"),
    ({"cursor": base64.urlsafe_b64encode(b'{"v":1,"c":"2026-10-21T12:00:00+00:00","i":"cmp-1&x"}').decode().rstrip("=")}, "cursor"),
    ({"cursor": base64.urlsafe_b64encode(b'{"v":1,"c":"2026-10-21T12:00:00","i":"cmp-0123456789abcdef"}').decode().rstrip("=")}, "cursor"),
    ({"cursor": base64.urlsafe_b64encode(b'{"v":1,"c":"ayer","i":"cmp-0123456789abcdef","x":1}').decode().rstrip("=")}, "cursor"),
    ({"cursor": "A" * 300}, "cursor"),
])
async def test_filtros_invalidos_son_400_sin_llamar(cliente, nucleo, params, campo):
    error = _error(await cliente.get("/v1/compras", params=params), 400, "validacion")
    assert error["detalle"]["campos"][0]["campo"] == campo
    assert nucleo.llamadas == []


def test_el_cursor_va_y_vuelve():
    cursor = (datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=timezone.utc), COMPRA_ID)
    texto = codificar_cursor(cursor)
    assert "=" not in texto and decodificar_cursor(texto) == cursor
    assert codificar_cursor(None) is None
    with pytest.raises(ErrorApi):
        decodificar_cursor(texto[:-3])


async def test_el_listado_devuelve_siguiente_y_el_resultado(cliente, nucleo):
    cursor = (datetime(2026, 10, 21, 12, tzinfo=timezone.utc), COMPRA_ID)
    nucleo.siguiente = cursor
    datos = (await cliente.get("/v1/compras")).json()
    assert decodificar_cursor(datos["siguiente"]) == cursor
    compra = datos["compras"][0]
    assert {"resultado", "alertas", "intento_de_estafa"} <= set(compra)
    assert compra["resultado"] in ("en_curso", "sin_compra") and compra["alertas"] == []
    # El cursor de la respuesta sirve para la página siguiente.
    await cliente.get("/v1/compras", params={"cursor": datos["siguiente"]})
    assert nucleo.filtros.cursor == cursor
    nucleo.siguiente = None
    assert (await cliente.get("/v1/compras")).json()["siguiente"] is None


async def test_explicacion_y_resumen_por_el_nucleo(cliente, nucleo):
    r = await cliente.get(f"/v1/compras/{COMPRA_ID}/explicacion")
    assert r.status_code == 200 and r.json()["compra_id"] == COMPRA_ID
    assert [c[0] for c in nucleo.llamadas] == ["explicacion_compra"]
    _error(await cliente.get("/v1/compras/cmp-123/explicacion"), 404, "compra_desconocida")
    nucleo.falla = ErrorApi(404, "compra_desconocida", "No existe esa compra.")
    _error(await cliente.get(f"/v1/compras/{COMPRA_ID}/explicacion"), 404, "compra_desconocida")

    nucleo.completo = False
    r = await cliente.get("/v1/resumen")
    assert r.status_code == 200
    datos = r.json()
    assert datos["compras"] == 1 and datos["completo"] is False
    desde, hasta = (datetime.fromisoformat(datos[k]) for k in ("desde", "hasta"))
    assert hasta - desde == timedelta(days=30)
    assert hasta.time() == datetime.min.time() and hasta > datetime.now(timezone.utc)


@pytest.mark.parametrize("params, desde, hasta", [
    ({"desde": "2026-10-01", "hasta": "2026-10-10"},
     datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc)),
    ({"hasta": "2026-10-10"},
     datetime(2026, 9, 11, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc)),
])
async def test_la_ventana_del_resumen(cliente, nucleo, params, desde, hasta):
    datos = (await cliente.get("/v1/resumen", params=params)).json()
    assert datos["desde"] == desde.isoformat() and datos["hasta"] == hasta.isoformat()
    assert nucleo.llamadas[-1] == ("resumen_compras", (ACME, desde, hasta))


@pytest.mark.parametrize("params", [
    {"desde": "2025-01-01", "hasta": "2026-10-10"},
    {"desde": "2026-10-10", "hasta": "2026-10-01"},
    {"desde": "mañana"},
])
async def test_ventanas_invalidas_del_resumen(cliente, nucleo, params):
    _error(await cliente.get("/v1/resumen", params=params), 400, "validacion")
    assert nucleo.llamadas == []


# -- de punta a punta -------------------------------------------------------------------


@pytest.fixture
def config_gw(config):
    return replace(config, gateway_public_url=GATEWAY_PUBLIC_URL)


@pytest.fixture
def guard_real():
    try:
        cargado = cargar_evaluador()
    except EvaluadorNoDisponible:
        pytest.skip("guard.evaluar() con checkout= no está disponible")
    return cargado


def _ficha_b2() -> dict[str, Any]:
    return {"requisitos": [
        {"campo": "precio", "operador": "maximo", "valor": 150, "obligatorio": True},
        {"campo": "moneda", "operador": "igual", "valor": "USD", "obligatorio": True},
        {"campo": "categoria", "operador": "igual", "valor": "basketball_shoes", "obligatorio": True},
        {"campo": "uso", "operador": "igual", "valor": "outdoor", "obligatorio": True},
    ]}


def _textos_del_comercio(*escenarios: str) -> list[str]:
    textos: list[str] = []
    for escenario in escenarios:
        datos = cargar_fixture(escenario)
        textos += [c["item_label"] for c in datos["carts"]]
        textos += [d["description"] for d in datos["offer_details"]]
        textos += [d["title"] for d in datos["offer_details"]]
    return textos


def _sin_texto_del_comercio(respuesta: httpx.Response, *escenarios: str) -> None:
    volcado = respuesta.text
    for texto in _textos_del_comercio(*escenarios):
        assert texto not in volcado, texto
    for marca in ("Note for AI", "checkout_jwt", "eyJ", "item_label"):
        assert marca not in volcado, marca


async def _compra_b2_frenada(c: httpx.AsyncClient) -> str:
    compra_id = (await crear_compra(c, ficha=_ficha_b2(), referencia="ord-b2"))["compra_id"]
    contexto = (await buscar(c, compra_id, escenario="B2")).json()["result"]["contextId"]
    resp = await checkout(c, compra_id, contexto, "cart_4")
    assert resp.json()["result"]["status"]["state"] == "failed"
    return compra_id


async def test_explicacion_de_una_compra_frenada_por_un_dato_falso(config_gw, guard_real):
    mundo = armar_gateway(config_gw, guard_real, almacen=EstadoMemoria())
    async with cliente_publico(mundo.app) as c:
        compra_id = await _compra_b2_frenada(c)
        await mundo.esperar_fondo()
        r = await c.get(f"/v1/compras/{compra_id}/explicacion")
        assert r.status_code == 200, r.text
        datos = r.json()
        listado = (await c.get("/v1/compras")).json()["compras"]
        resumen = (await c.get("/v1/resumen")).json()
    assert datos["resultado"] == "frenada"
    assert datos["intento_de_estafa"] is True and "dato_falso" in datos["alertas"]
    assert datos["resumen"].startswith("Frenamos la compra antes de que la firmaras:")
    assert "según el catálogo público" in datos["resumen"]
    assert datos["monto"] == 149.0 and datos["moneda"] == "USD"
    assert datos["recibo"]["firmado"] is True
    etapas = [e["etapa"] for e in datos["linea_de_tiempo"]]
    assert etapas[0] == "pedido" and "verificacion" in etapas and "checkout" in etapas
    assert any(c["resultado"] == "bloquea" for c in datos["chequeos"])
    _sin_texto_del_comercio(r, "B2")
    (fila,) = listado
    assert fila["resultado"] == "frenada" and fila["intento_de_estafa"] is True
    assert "dato_falso" in fila["alertas"]
    assert resumen["compras"] == 1 and resumen["por_resultado"]["frenada"] == 1
    assert resumen["intentos_de_estafa"] == 1 and resumen["por_alerta"]["dato_falso"] == 1
    assert resumen["monto_frenado"] == [{"moneda": "USD", "total": 149.0}]


def _filtro_que_corta(objetivo: Any) -> ResultadoFiltro:
    if "Note for AI" in json.dumps(objetivo, ensure_ascii=False):
        return ResultadoFiltro(pasa=False, senales=["heuristica:instruccion_ia"])
    return ResultadoFiltro(pasa=True)


async def test_explicacion_de_una_compra_cortada_por_el_filtro(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=EstadoMemoria(),
                          filtro=_filtro_que_corta)
    async with cliente_publico(mundo.app) as c:
        cortada = (await crear_compra(c))["compra_id"]
        await buscar(c, cortada, escenario="B")
        honesta = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, honesta, escenario="baseline")).json()["result"]["contextId"]
        await checkout(c, honesta, contexto, "cart_1")
        await mundo.esperar_fondo()
        r = await c.get(f"/v1/compras/{cortada}/explicacion")
        datos = r.json()
        por_alerta = (await c.get("/v1/compras", params={"alerta": "manipulacion"})).json()
        aprobadas = (await c.get(f"/v1/compras/{honesta}/explicacion")).json()
    assert datos["resultado"] == "cortada_por_seguridad"
    assert datos["alertas"] == ["manipulacion"] and datos["intento_de_estafa"] is True
    assert datos["resumen"].startswith("El comercio intentó manipular a tu agente")
    (corte,) = [e for e in datos["linea_de_tiempo"] if e["etapa"] == "seguridad"]
    assert "instrucciones dirigidas a asistentes de IA" in corte["texto"]
    _sin_texto_del_comercio(r, "B")
    assert [v["compra_id"] for v in por_alerta["compras"]] == [cortada]
    assert aprobadas["resultado"] == "aprobada" and aprobadas["intento_de_estafa"] is False


async def test_la_explicacion_de_otro_integrador_es_404(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c, cliente_publico(mundo.app, CLAVE_OTRO) as otro:
        compra_id = (await crear_compra(c))["compra_id"]
        r = await otro.get(f"/v1/compras/{compra_id}/explicacion")
    assert r.status_code == 404 and r.json()["error"]["codigo"] == "compra_desconocida"


def _config_varias(config_gw):
    return replace(config_gw, instancias="varias", sondeo_s=0.02, barrera_s=3.0,
                   gracia_espera_s=0.4)


@pytest.fixture(params=["una", "varias"])
def gateway(request, config_gw):
    if request.param == "una":
        return armar_gateway(config_gw, evaluador_espia()[0], almacen=EstadoMemoria()), None
    base = BaseFalsa()
    almacen = EstadoSupabase(URL_SUPABASE, "clave", transport=base.transporte())
    return armar_gateway(_config_varias(config_gw), evaluador_espia()[0], almacen=almacen,
                         demo=None), base


async def test_el_cursor_recorre_todas_las_compras_sin_repetir(gateway):
    mundo, _ = gateway
    async with cliente_publico(mundo.app) as c:
        creadas = [(await crear_compra(c, referencia=f"ord-{i % 3}"))["compra_id"] for i in range(7)]
        vistas: list[str] = []
        params: dict[str, str] = {"limite": "3"}
        for _ in range(5):
            datos = (await c.get("/v1/compras", params=params)).json()
            vistas += [v["compra_id"] for v in datos["compras"]]
            if datos["siguiente"] is None:
                break
            params["cursor"] = datos["siguiente"]
        por_referencia = (await c.get("/v1/compras", params={"referencia": "ord-1"})).json()
        futuro = (await c.get("/v1/compras", params={"desde": "2999-01-01"})).json()
        de_hoy = (await c.get("/v1/compras", params={"hasta": "2999-01-01", "desde": "2000-01-01"})).json()
    assert vistas == list(reversed(creadas))
    assert [v["compra_id"] for v in por_referencia["compras"]] == [creadas[4], creadas[1]]
    assert futuro == {"compras": [], "siguiente": None}
    assert len(de_hoy["compras"]) == 7


async def test_el_listado_con_varias_instancias_no_hace_n_mas_1(config_gw):
    base = BaseFalsa()
    almacen = EstadoSupabase(URL_SUPABASE, "clave", transport=base.transporte())
    mundo = armar_gateway(_config_varias(config_gw), evaluador_espia()[0], almacen=almacen,
                          demo=None)
    async with cliente_publico(mundo.app) as c:
        compras = [(await crear_compra(c))["compra_id"] for _ in range(10)]
        for compra_id in compras[:3]:
            contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
            await checkout(c, compra_id, contexto, "cart_1")
        await mundo.esperar_fondo()
        antes = len(base.pedidos)
        datos = (await c.get("/v1/compras")).json()
        pedidos = base.pedidos[antes:]
    assert len(datos["compras"]) == 10
    assert sum(len(v["contextos"]) for v in datos["compras"]) == 3
    tablas = [p.url.path.rsplit("/", 1)[-1] for p in pedidos]
    # Una lectura de compras y una por tabla para las diez fotos (antes, dos por compra).
    assert tablas.count("gw_compras") == 1
    assert tablas.count("gw_contextos") == 1 and tablas.count("gw_aprobaciones") == 1
    assert len(pedidos) <= 4, tablas


async def test_el_resumen_cuenta_las_compras_del_periodo(gateway):
    mundo, _ = gateway
    async with cliente_publico(mundo.app) as c, cliente_publico(mundo.app, CLAVE_OTRO) as otro:
        for _ in range(3):
            await crear_compra(c)
        await crear_compra(otro)
        datos = (await c.get("/v1/resumen")).json()
        vacio = (await c.get("/v1/resumen", params={"desde": "2020-01-01", "hasta": "2020-02-01"})).json()
    assert datos["compras"] == 3 and datos["por_resultado"]["en_curso"] == 3
    assert datos["completo"] is True and datos["intentos_de_estafa"] == 0
    assert vacio["compras"] == 0 and vacio["monto_frenado"] == []


async def test_explicacion_con_varias_instancias_lee_el_historial_de_la_base(config_gw):
    base = BaseFalsa()
    instancia_a = armar_gateway(_config_varias(config_gw), evaluador_espia()[0], demo=None,
                                almacen=EstadoSupabase(URL_SUPABASE, "clave", transport=base.transporte()))
    instancia_b = armar_gateway(_config_varias(config_gw), evaluador_espia()[0], demo=None,
                                almacen=EstadoSupabase(URL_SUPABASE, "clave", transport=base.transporte()))
    async with cliente_publico(instancia_a.app) as a, cliente_publico(instancia_b.app) as b:
        compra_id = (await crear_compra(a))["compra_id"]
        contexto = (await buscar(a, compra_id)).json()["result"]["contextId"]
        await checkout(a, compra_id, contexto, "cart_1")
        await instancia_a.esperar_fondo()
        datos = (await b.get(f"/v1/compras/{compra_id}/explicacion")).json()
    assert datos["resultado"] == "aprobada"
    etapas = [e["etapa"] for e in datos["linea_de_tiempo"]]
    assert {"busqueda", "ofertas", "checkout", "verificacion", "resultado"} <= set(etapas)


async def test_si_el_historial_no_se_puede_leer_es_503(config_gw):
    class SinEventos(EstadoMemoria):
        async def listar_eventos_recientes(self, *args, **kwargs):
            from proxy.tipos_gateway import ErrorAlmacen

            raise ErrorAlmacen("no responde")

    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=SinEventos())
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        r = await c.get(f"/v1/compras/{compra_id}/explicacion")
    assert r.status_code == 503 and r.json()["error"]["codigo"] == "estado_no_disponible"


async def test_el_listado_por_alerta_mira_mas_alla_de_una_pagina(config_gw):
    """Con `alerta` el filtro se aplica en el gateway: sigue leyendo páginas del
    almacén hasta llenar la de la respuesta."""
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=EstadoMemoria(),
                          filtro=_filtro_que_corta)
    async with cliente_publico(mundo.app) as c:
        cortada = (await crear_compra(c))["compra_id"]
        await buscar(c, cortada, escenario="B")
        for _ in range(4):
            await crear_compra(c)
        datos = (await c.get("/v1/compras", params={"alerta": "manipulacion", "limite": "1"})).json()
        nada = (await c.get("/v1/compras", params={"alerta": "dato_falso"})).json()
    assert [v["compra_id"] for v in datos["compras"]] == [cortada]
    assert datos["siguiente"] is None
    assert nada == {"compras": [], "siguiente": None}
    await asyncio.sleep(0)


# -- el tamaño: el listado sin el detalle y el peor caso de una compra ---------------


async def test_el_listado_no_trae_el_detalle_de_los_chequeos(cliente, nucleo):
    from proxy.tests.test_api_v1 import _foto

    veredicto = {"status": "bloqueado", "cart_id": "cart_2", "chequeos": [],
                 "chequeos_detalle": [{"tipo": "mandato", "resultado": "ok", "caso": "cumple"}],
                 "alertas": ["no_cumple"], "monto": 10.0, "moneda": "USD"}
    nucleo.foto = _foto(veredictos=(veredicto,))
    listado = (await cliente.get("/v1/compras")).json()["compras"][0]
    assert listado["veredictos"] and "chequeos_detalle" not in listado["veredictos"][0]
    assert listado["veredictos"][0]["alertas"] == ["no_cumple"]
    assert listado["alertas"] == ["no_cumple"]
    # El GET de la compra (y los webhooks, que usan la misma vista) lo siguen trayendo.
    una = (await cliente.get(f"/v1/compras/{COMPRA_ID}")).json()
    assert una["veredictos"][0]["chequeos_detalle"] == veredicto["chequeos_detalle"]


def _veredicto_pesado(n_chequeos: int):
    from contracts import Chequeo, Veredicto

    largo = ["Texto de cuarenta caracteres del catalog"[:40]] * 20
    chequeos = [
        Chequeo(tipo="cumplimiento", resultado="aviso", detalle="x" * 500,
                campo=f"atributo:{'a' * 30}{i}", esperado=largo, observado=largo,
                origen="catalogo_publico")
        for i in range(n_chequeos)
    ]
    return Veredicto(status="bloqueado", cart_id_elegido="c" * 64, cart_id_recomendado=None,
                     ranking=[], chequeos=chequeos, motivo="m" * 2000)


@pytest.mark.parametrize("n_chequeos", [32, 150])
def test_el_peor_caso_de_una_compra_entra_holgado(n_chequeos):
    """20 veredictos (el máximo de la compra) con chequeos de los valores más
    pesados que pasan la lista blanca: la compra entra holgada en el cuerpo de
    un webhook (1 MB) y en la respuesta de Vercel (4,5 MB)."""
    from proxy.alertas import PRESUPUESTO_DETALLE_BYTES
    from proxy.estado import MAX_BYTES_CUERPO_WEBHOOK
    from proxy.tests.test_api_v1 import _foto
    from proxy.tipos_gateway import vista_veredicto
    from proxy.vistas import vista_compra

    vista = vista_veredicto(_veredicto_pesado(n_chequeos), contexto_id="c" * 200,
                            ts=datetime.now(timezone.utc), evaluador="guard")
    detalle = json.dumps(vista["chequeos_detalle"], ensure_ascii=False, separators=(",", ":"))
    assert len(detalle.encode("utf-8")) <= PRESUPUESTO_DETALLE_BYTES
    assert vista["alertas"] == []  # los avisos no alertan, aunque el detalle se recorte
    compra = vista_compra(_foto(veredictos=tuple([vista] * 20)), a2a_url=None)
    total = len(json.dumps(compra, ensure_ascii=False).encode("utf-8"))
    assert total < MAX_BYTES_CUERPO_WEBHOOK / 2, total
