"""Etapa 3 en el proxy: producto general, sin hardcodeos.

- Manda la ficha que confirmó el usuario: `PROXY_FICHA_DEMO=0` por defecto.
- Un escenario sin `intent` propio no cae al pedido de zapatillas; uno
  `"tipo": "general"` nunca tiene ficha de la demo.
- La ficha siempre lleva categoría de la taxonomía: el clasificador del
  agente o, si no hay, las palabras del pedido (`taxonomia.palabras`), y se
  clasifica antes de pedir la confirmación.
- El catálogo se pide por SKU (`/products/<sku>`) y, si no alcanza, entero.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import date
from typing import Any

import httpx
import pytest

from contracts import Contexto, Requisito
from intencion import Pregunta
from proxy.config import Config
from proxy.demo_local import ESCENARIOS_DEL_PEDIDO_GLOBAL, FuentesDemo, intent_del_escenario
from proxy.fuentes import (
    MAX_SKUS_POR_PEDIDO,
    cargar_catalogo,
    categoria_de_la_ficha,
    categorias_para_el_pedido,
    ruta_categoria,
    skus_de,
)
from proxy.tests.ayudantes import (
    RPC,
    buscar,
    checkout,
    cliente_de,
    evaluador_espia,
    eventos,
    veredictos,
)
from proxy.tests.fake_world import (
    CATALOG_HOST,
    HEADERS_A2A,
    MERCHANT_HOST,
    RuteoPorHost,
    app_catalogo,
    cuerpo,
    pedido_find_products,
)

pytestmark = pytest.mark.anyio

LAPTOPS = "328"
COMPUTADORAS = "278"
MONITORES = "305"
CALZADO = "187"


# -- 1. manda la ficha del usuario ----------------------------------------------------


def test_por_defecto_no_se_usan_las_fichas_de_la_demo():
    assert Config().ficha_demo is False
    assert Config.desde_entorno({}).ficha_demo is False
    assert Config.desde_entorno({"PROXY_FICHA_DEMO": "0"}).ficha_demo is False
    assert Config.desde_entorno({"PROXY_FICHA_DEMO": "1"}).ficha_demo is True


def test_proxy_ficha_demo_invalido_no_arranca():
    with pytest.raises(ValueError, match="PROXY_FICHA_DEMO"):
        Config.desde_entorno({"PROXY_FICHA_DEMO": "quizas"})


async def test_el_estado_dice_si_se_usan_las_fichas_de_la_demo(armar_proxy, config):
    """`smoke_checkout.py` lo lee: sus casos esperan la ficha de la demo."""
    con = armar_proxy(config=replace(config, ficha_demo=True))
    sin = armar_proxy(config=replace(config, ficha_demo=False))
    assert con.state.proxy.estado(con_token=False)["ficha_demo"] is True
    assert sin.state.proxy.estado(con_token=False)["ficha_demo"] is False


# -- 2. sin caída a zapatillas ------------------------------------------------------


def test_los_escenarios_de_zapatillas_son_los_que_no_tienen_intent_propio():
    """Verificado contra scenarios.json: solo esos cinco usan el pedido global."""
    from demo_world import scenario

    sin_propio = {
        nombre for nombre in scenario.scenario_names()
        if not isinstance(scenario.get_scenario(nombre).get("intent"), dict)
        and scenario.get_scenario(nombre).get("tipo") is None
    }
    assert sin_propio <= ESCENARIOS_DEL_PEDIDO_GLOBAL
    assert ESCENARIOS_DEL_PEDIDO_GLOBAL == {"baseline", "A", "B", "B2", "C"}


@pytest.mark.parametrize("nombre", sorted(ESCENARIOS_DEL_PEDIDO_GLOBAL))
def test_un_escenario_de_zapatillas_usa_el_pedido_global(nombre):
    from demo_world import scenario

    assert intent_del_escenario(nombre) == scenario.intent()


@pytest.mark.parametrize(
    ("nombre", "de"),
    [("notebook_honesto", "notebook_honesto"), ("notebook_malicioso", "notebook_honesto"),
     ("computadoras", "computadoras")],
)
def test_un_escenario_con_intent_propio_usa_el_suyo(nombre, de):
    from demo_world import scenario

    assert intent_del_escenario(nombre) == scenario.get_scenario(de)["intent"]


INTENT_MONITOR = {"query": "monitor", "hard": {"max_price": 300, "currency": "USD"}}


@pytest.fixture
def escenarios_nuevos(monkeypatch):
    """scenarios.json con presets del comercio general (los agrega otro lote)."""
    from demo_world import scenario

    nuevos = {
        "general": {"tipo": "general", "comportamiento": ["honesto"]},
        "general_con_intent": {"tipo": "general", "intent": INTENT_MONITOR},
        "fijo_nuevo_sin_intent": {"title": "un escenario fijo sin intent propio"},
        "fijo_nuevo_con_intent": {"intent": INTENT_MONITOR},
    }
    originales = scenario.scenario_names
    obtener = scenario.get_scenario
    monkeypatch.setattr(scenario, "scenario_names", lambda: [*originales(), *nuevos])
    monkeypatch.setattr(
        scenario, "get_scenario", lambda n: nuevos[n] if n in nuevos else obtener(n)
    )
    return nuevos


def test_un_escenario_general_no_tiene_ficha_de_la_demo(escenarios_nuevos):
    assert intent_del_escenario("general") is None
    # Aunque traiga un intent: el comercio general arma las ofertas con el pedido.
    assert intent_del_escenario("general_con_intent") is None


def test_un_escenario_nuevo_sin_intent_no_cae_a_zapatillas(escenarios_nuevos):
    assert intent_del_escenario("fijo_nuevo_sin_intent") is None
    assert intent_del_escenario("fijo_nuevo_con_intent") == INTENT_MONITOR


@pytest.mark.parametrize("nombre", [None, "", "../B", "B C", "x" * 41, 7])
def test_un_nombre_raro_no_tiene_ficha(nombre):
    assert intent_del_escenario(nombre) is None


@pytest.fixture
def sin_demo_world(monkeypatch, tmp_path):
    """`demo_world` no se puede importar: quedan los fixtures (en tmp_path)."""
    import proxy.demo_local as demo_local

    monkeypatch.setitem(sys.modules, "demo_world", None)  # import → ImportError
    monkeypatch.setattr(demo_local, "FIXTURES", tmp_path)
    (tmp_path / "etapa2").mkdir()
    return tmp_path


def test_sin_demo_world_los_fixtures_siguen_las_mismas_reglas(sin_demo_world):
    def escribir(nombre: str, datos: Any, carpeta: str = "") -> None:
        ruta = sin_demo_world / carpeta / nombre
        ruta.write_text(json.dumps(datos), encoding="utf-8")

    escribir("scenario_B.json", {"scenario": "B", "intent": {"query": "zapatillas"}})
    escribir("scenario_general.json", {"scenario": "general", "tipo": "general",
                                       "intent": INTENT_MONITOR})
    escribir("notebook.json", {"scenario": "notebook", "intent": INTENT_MONITOR}, "etapa2")
    escribir("sin_intent.json", {"scenario": "sin_intent"}, "etapa2")
    escribir("lista.json", ["no", "es", "un", "objeto"], "etapa2")
    (sin_demo_world / "etapa2" / "roto.json").write_text("{", encoding="utf-8")

    assert intent_del_escenario("B") == {"query": "zapatillas"}  # el fixture trae el suyo
    assert intent_del_escenario("general") is None
    assert intent_del_escenario("notebook") == INTENT_MONITOR
    assert intent_del_escenario("sin_intent") is None
    assert intent_del_escenario("lista") is None
    assert intent_del_escenario("roto") is None
    assert intent_del_escenario("no_existe") is None


def _sin_demo_scenario(respuesta: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
    """Un comercio que no es el de la demo: no declara `demo_scenario`."""
    for artefacto in (respuesta.get("result") or {}).get("artifacts") or []:
        for parte in artefacto.get("parts") or []:
            datos = parte.get("data")
            if isinstance(datos, dict):
                datos.pop("demo_scenario", None)
    return respuesta


async def test_sin_escenario_conocido_no_hay_ficha_de_zapatillas(armar_proxy, config, merchant):
    """Con PROXY_FICHA_DEMO=1 y un comercio que no dice el escenario, el
    Guard evalúa sin parámetros autorizados (escala), nunca con el pedido de
    zapatillas."""
    merchant.manipular = _sin_demo_scenario
    evaluador, llamadas = evaluador_espia()
    app = armar_proxy(config=replace(config, ficha_demo=True), evaluador=evaluador)
    async with cliente_de(app) as c:
        contexto = await buscar(c, None)  # ni el pedido ni la respuesta dicen el escenario
        await checkout(c, contexto, "cart_1")
    (llamada,) = llamadas
    assert llamada["ficha"].requisitos == [] and llamada["ficha"].confirmada is False


async def test_un_escenario_general_evalua_sin_ficha_de_la_demo(
    armar_proxy, config, merchant, escenarios_nuevos
):
    def como_general(respuesta, pedido):
        for artefacto in (respuesta.get("result") or {}).get("artifacts") or []:
            for parte in artefacto.get("parts") or []:
                if isinstance(parte.get("data"), dict) and "demo_scenario" in parte["data"]:
                    parte["data"]["demo_scenario"] = "general"
        return respuesta

    merchant.manipular = como_general
    evaluador, llamadas = evaluador_espia()
    app = armar_proxy(config=replace(config, ficha_demo=True), evaluador=evaluador)
    async with cliente_de(app) as c:
        contexto = await buscar(c, None)
        await checkout(c, contexto, "cart_1")
    (llamada,) = llamadas
    assert llamada["ficha"].requisitos == [] and llamada["ficha"].confirmada is False


# -- 3. categoría siempre -----------------------------------------------------------


def test_ruta_categoria_es_el_texto_del_arbol():
    assert ruta_categoria(LAPTOPS) == "Electronics > Computers > Laptops"
    assert ruta_categoria("999999999") is None
    assert ruta_categoria(None) is None
    assert ruta_categoria("abc") is None


@pytest.mark.parametrize(
    ("propuesta", "textos", "esperado"),
    [
        (None, ["Necesito una notebook para programar"], LAPTOPS),
        (None, ["20 computadoras para el lunes"], COMPUTADORAS),
        (None, ["algo para la oficina"], None),
        # Sin categoría en el pedido, la que nombra la respuesta.
        (None, ["algo para la oficina", "un monitor de 27 pulgadas"], MONITORES),
        # El pedido manda sobre las respuestas ("para la notebook" no cambia
        # que lo que se compra es un monitor).
        (None, ["un monitor", "para conectarlo a la notebook del trabajo"], MONITORES),
        # En la misma rama, la más específica; sin relación de rama, las
        # palabras del pedido (lo que se compra).
        (COMPUTADORAS, ["una notebook"], LAPTOPS),
        (LAPTOPS, ["una computadora"], LAPTOPS),
        (MONITORES, ["una notebook"], LAPTOPS),
        # Un ID que no está en el árbol (o no es un ID) no se usa.
        ("999999999", ["una notebook"], LAPTOPS),
        ("Laptops", ["una notebook"], LAPTOPS),
        (12, ["una notebook"], LAPTOPS),
        ("999999999", ["algo"], None),
    ],
)
def test_categoria_de_la_ficha(propuesta, textos, esperado):
    assert categoria_de_la_ficha(propuesta, textos) == esperado


class AgenteSinIntake:
    """Agente de intención sin intake (como Gemini): pregunta el porqué."""

    nombre = "sin_intake"

    def __init__(self, requisitos: list[Requisito] | None = None, categoria: Any = "nada",
                 falla: bool = False) -> None:
        self.requisitos = requisitos or [
            Requisito(campo="precio", operador="maximo", valor=1200.0, obligatorio=True)
        ]
        self.categoria = categoria
        self.falla = falla
        self.descripciones: list[str] = []
        if categoria != "nada":
            self.clasificar_taxonomia = self._clasificar

    def _clasificar(self, descripcion: str) -> Any:
        self.descripciones.append(descripcion)
        if self.falla:
            raise RuntimeError("caído")
        return self.categoria

    def preguntar(self, pedido: str, hoy: date, categorias: list[str] | None = None) -> Pregunta:
        return Pregunta(pregunta="¿Para qué la necesitás?", requisitos=list(self.requisitos))

    def armar_contexto(self, pedido: str, pregunta: str, respuesta: str, hoy: date) -> Contexto:
        return Contexto(pedido=pedido, pregunta=pregunta, respuesta=respuesta)

    def detectar_oportunidad(self, contexto, ficha, ofertas):
        return None


async def _compra(c: httpx.AsyncClient, estado: str, intentos: int = 300) -> dict[str, Any]:
    compras: list[dict[str, Any]] = []
    for _ in range(intentos):
        compras = (await c.get("/guard/compras")).json()["compras"]
        for compra in compras:
            if compra["estado"] == estado:
                return compra
        await asyncio.sleep(0.01)
    raise AssertionError(f"Ninguna compra llegó a {estado}: {compras}")


async def _comprar_sin_intake(
    app, pedido: str, respuesta: str | None = "Para programar.", escenario: str = "notebook_honesto"
):
    """find_products con `pedido`, el porqué y un checkout del cart_1."""
    async with cliente_de(app) as c:
        rpc = pedido_find_products(escenario)
        for parte in rpc["params"]["message"]["parts"]:
            if "catalog_search" in (parte.get("data") or {}):
                parte["data"]["catalog_search"] = pedido
        busqueda = asyncio.create_task(c.post(RPC, content=cuerpo(rpc), headers=HEADERS_A2A))
        compra = await _compra(c, "esperando_contexto")
        cuerpo_respuesta = (
            {"compra_id": compra["compra_id"], "respuesta": respuesta}
            if respuesta else {"compra_id": compra["compra_id"], "omitir": True}
        )
        await c.post("/guard/compras/contexto", json=cuerpo_respuesta)
        contexto = (await busqueda).json()["result"]["contextId"]
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        return task, await eventos(c)


@pytest.fixture
def config_rapida(config):
    return replace(config, espera_contexto_s=5.0, espera_decision_s=5.0, ficha_demo=False)


async def test_sin_clasificador_la_categoria_sale_de_las_palabras(armar_proxy, config_rapida):
    evaluador, llamadas = evaluador_espia()
    app = armar_proxy(config=config_rapida, agente=AgenteSinIntake(), evaluador=evaluador)
    _, evs = await _comprar_sin_intake(app, "Necesito una notebook para programar")
    (llamada,) = llamadas
    assert llamada["ficha"].categoria_taxonomia == LAPTOPS
    final = [e for e in evs if e["tipo"] == "contexto_registrado"][-1]["datos"]
    assert final["categoria_taxonomia"] == LAPTOPS
    assert final["categoria_ruta"] == "Electronics > Computers > Laptops"


@pytest.mark.parametrize(
    ("categoria", "falla", "esperada"),
    [
        (COMPUTADORAS, False, LAPTOPS),  # las palabras son más específicas en su rama
        (MONITORES, False, LAPTOPS),  # otra rama: mandan las palabras del pedido
        (None, False, LAPTOPS),  # no devolvió nada
        ("999999999", False, LAPTOPS),  # un ID fuera del árbol
        (COMPUTADORAS, True, LAPTOPS),  # falló
    ],
)
async def test_el_clasificador_del_agente_y_las_palabras(
    armar_proxy, config_rapida, categoria, falla, esperada
):
    evaluador, llamadas = evaluador_espia()
    agente = AgenteSinIntake(categoria=categoria, falla=falla)
    app = armar_proxy(config=config_rapida, agente=agente, evaluador=evaluador)
    _, evs = await _comprar_sin_intake(app, "Necesito una notebook para programar")
    (llamada,) = llamadas
    assert llamada["ficha"].categoria_taxonomia == esperada
    assert agente.descripciones == ["Necesito una notebook para programar / Para programar."]
    if falla:
        assert any(e["tipo"] == "error" and "clasificar" in e["datos"]["mensaje"] for e in evs)


async def test_sin_categoria_en_las_palabras_la_ficha_queda_sin_categoria(
    armar_proxy, config_rapida
):
    evaluador, llamadas = evaluador_espia()
    app = armar_proxy(config=config_rapida, agente=AgenteSinIntake(), evaluador=evaluador)
    _, evs = await _comprar_sin_intake(app, "algo lindo para regalar", respuesta=None)
    (llamada,) = llamadas
    assert llamada["ficha"].categoria_taxonomia is None
    final = [e for e in evs if e["tipo"] == "contexto_registrado"][-1]["datos"]
    assert final["categoria_taxonomia"] is None and final["categoria_ruta"] is None


async def test_sin_intake_la_ficha_del_agente_no_queda_confirmada(armar_proxy, config_rapida):
    """Sin intake no hay turno de confirmación: lo que extrajo el agente es
    una propuesta (los LLM no autorizan) y el Guard escala."""
    evaluador, llamadas = evaluador_espia()
    app = armar_proxy(config=config_rapida, agente=AgenteSinIntake(), evaluador=evaluador)
    await _comprar_sin_intake(app, "Necesito una notebook para programar")
    (llamada,) = llamadas
    assert [r.campo for r in llamada["ficha"].requisitos] == ["precio"]
    assert llamada["ficha"].confirmada is False


async def test_con_ficha_demo_apagada_manda_la_del_usuario_aunque_el_escenario_tenga(
    armar_proxy, config_rapida
):
    """notebook_honesto tiene ficha de la demo: con PROXY_FICHA_DEMO=0 (el
    default) manda la de la compra."""
    evaluador, llamadas = evaluador_espia()
    app = armar_proxy(config=config_rapida, agente=AgenteSinIntake(), evaluador=evaluador)
    await _comprar_sin_intake(app, "Necesito una notebook para programar")
    (llamada,) = llamadas
    assert llamada["ficha"].pedido_original == "Necesito una notebook para programar"
    assert len(llamada["ficha"].requisitos) == 1


# -- 3b. con intake: la categoría antes de confirmar ------------------------------------


async def test_con_intake_la_ficha_que_se_confirma_ya_lleva_la_categoria(
    armar_proxy, config_rapida
):
    from intencion.agente_decisiones import AgenteDecisiones
    from proxy.tests.test_intake_proxy import Modelo, _buscar, _confirmar, _responder, _turno

    modelo = Modelo()
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(modelo))
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        await _responder(c, await _turno(c, 0), "Juego los sábados en una cancha de cemento.")
        await _responder(c, await _turno(c, 1), "Hasta 150 dólares, y para el viernes.")
        tercera = await _turno(c, 2)
        # Antes de confirmar: la compra, el evento y la ficha ya tienen la categoría.
        assert tercera["intake"]["confirmar"] is True
        assert tercera["intake"]["categoria_taxonomia"] == CALZADO
        assert tercera["intake"]["categoria_ruta"] == "Apparel & Accessories > Shoes"
        compra = app.state.proxy.compras.get(tercera["compra_id"])
        assert compra.ficha_llm.categoria_taxonomia == CALZADO
        pedidos = [e for e in await eventos(c) if e["tipo"] == "pedido_detectado"]
        confirmacion = pedidos[-1]["datos"]
        assert confirmacion["confirmar"] is True
        assert confirmacion["categoria_taxonomia"] == CALZADO
        assert confirmacion["categoria_ruta"] == "Apparel & Accessories > Shoes"
        clasificaciones = modelo.taxonomia
        assert clasificaciones >= 1

        assert (await _confirmar(c, tercera)).status_code == 200
        await busqueda
        final = [e for e in await eventos(c) if e["tipo"] == "contexto_registrado"][-1]["datos"]
    assert final["confirmada"] is True
    assert final["categoria_taxonomia"] == CALZADO
    assert final["categoria_ruta"] == "Apparel & Accessories > Shoes"
    # Confirmar no agrega texto: no se vuelve a clasificar.
    assert modelo.taxonomia == clasificaciones


async def test_con_intake_sin_clasificador_la_categoria_sale_de_las_palabras(
    armar_proxy, config_rapida
):
    from intencion.agente_decisiones import AgenteDecisiones
    from proxy.tests.test_intake_proxy import Modelo, _buscar, _confirmar, _responder, _turno

    agente = AgenteDecisiones(Modelo())
    agente.clasificar_taxonomia = lambda descripcion: None  # no llega ni al primer nivel
    app = armar_proxy(config=config_rapida, agente=agente)
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        await _responder(c, await _turno(c, 0), "Para jugar.")
        await _responder(c, await _turno(c, 1), "Hasta 150 dólares, y para el viernes.")
        tercera = await _turno(c, 2)
        assert tercera["intake"]["categoria_taxonomia"] == CALZADO  # "zapatillas"
        await _confirmar(c, tercera)
        await busqueda
    compra = app.state.proxy.compras.get(tercera["compra_id"])
    assert compra.ficha_llm.categoria_taxonomia == CALZADO


# -- H5: las categorías del catálogo, primero las del pedido ---------------------------


def _producto(sku: str, categoria: str, taxonomia: str | None = None) -> dict[str, Any]:
    p = {"sku": sku, "category": categoria}
    if taxonomia:
        p["categoria_taxonomia"] = taxonomia
    return p


def test_categorias_para_el_pedido_pone_primero_las_de_su_rama():
    catalogo = {p["sku"]: p for p in [
        _producto("a", "auriculares", "543626"),
        _producto("b", "laptops", LAPTOPS),
        _producto("c", "monitores", MONITORES),
        _producto("d", "zapatillas_running", CALZADO),
        _producto("e", "otra cosa"),
        _producto("f", 7),  # type: ignore[arg-type]
    ]}
    assert categorias_para_el_pedido(catalogo, "una notebook") == [
        "laptops", "auriculares", "monitores", "otra cosa", "zapatillas_running"
    ]
    # Pedido de computadoras en general: las laptops están en esa rama.
    assert categorias_para_el_pedido(catalogo, "20 computadoras")[0] == "laptops"
    # Por nombre (sin taxonomía en el catálogo).
    assert categorias_para_el_pedido(catalogo, "otra cosa por favor")[0] == "otra cosa"
    # Sin pedido reconocible: orden alfabético.
    assert categorias_para_el_pedido(catalogo, "") == sorted(
        ["auriculares", "laptops", "monitores", "otra cosa", "zapatillas_running"]
    )


def test_categorias_para_el_pedido_con_mas_de_40_no_pierde_la_pedida():
    catalogo = {f"s{i}": _producto(f"s{i}", f"aaa_{i:02d}") for i in range(60)}
    catalogo["m"] = _producto("m", "zz_monitores", MONITORES)
    orden = categorias_para_el_pedido(catalogo, "quiero un monitor")
    assert orden[0] == "zz_monitores" and len(orden) == 61


# -- 5. catálogo por SKU ---------------------------------------------------------------

MERCHANT = {"id": "merchant_1", "name": "Generic Merchant"}
PRODUCTOS = {
    "NK-OUT-01": {"sku": "NK-OUT-01", "title": "Nike Outdoor", "price": 120.0},
    "AX-URB-04": {"sku": "AX-URB-04", "title": "Apex Urban", "price": 140.0},
}


class Catalogo:
    """Catálogo público falso con `/products` y, opcional, `/products/<sku>`."""

    def __init__(self, *, por_sku: bool = True, merchant_en_producto: bool = True,
                 falla_sku: str | None = None, otro_merchant_en: str | None = None,
                 lista: Any = None) -> None:
        self.pedidos: list[str] = []
        self.por_sku = por_sku
        self.merchant_en_producto = merchant_en_producto
        self.falla_sku = falla_sku
        self.otro_merchant_en = otro_merchant_en
        self.lista = lista

    def handler(self, request: httpx.Request) -> httpx.Response:
        ruta = request.url.raw_path.decode("ascii")
        self.pedidos.append(ruta)
        if ruta == "/products":
            if self.lista is not None:
                return self.lista(request)
            return httpx.Response(
                200, json={"merchant": MERCHANT, "products": list(PRODUCTOS.values())}
            )
        if ruta.startswith("/products/") and self.por_sku:
            sku = ruta.removeprefix("/products/")
            if sku == self.falla_sku:
                return httpx.Response(500, text="caído")
            producto = PRODUCTOS.get(sku)
            if producto is None:
                return httpx.Response(404, json={"error": "no está"})
            cuerpo_producto = dict(producto)
            if self.merchant_en_producto:
                merchant = dict(MERCHANT)
                if sku == self.otro_merchant_en:
                    merchant["id"] = "otro_comercio"
                cuerpo_producto["merchant"] = merchant
            return httpx.Response(200, json=cuerpo_producto)
        return httpx.Response(404, text="Not Found")

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


URL = "http://catalogo.test"


async def test_por_sku_pide_solo_esos_productos():
    cat = Catalogo()
    async with cat.cliente() as client:
        productos, comercio, fuente = await cargar_catalogo(
            client, URL, skus=["NK-OUT-01", "AX-URB-04", "NK-OUT-01"]
        )
    assert sorted(cat.pedidos) == ["/products/AX-URB-04", "/products/NK-OUT-01"]
    assert productos == PRODUCTOS  # sin la clave merchant
    assert (comercio, fuente) == ("merchant_1", "http")


async def test_por_sku_un_sku_que_no_esta_queda_afuera():
    cat = Catalogo()
    async with cat.cliente() as client:
        productos, comercio, _ = await cargar_catalogo(client, URL, skus=["NK-OUT-01", "NO-ESTA"])
    assert list(productos) == ["NK-OUT-01"] and comercio == "merchant_1"
    assert "/products" not in cat.pedidos


@pytest.mark.parametrize(
    "catalogo",
    [
        Catalogo(por_sku=False),  # no hay /products/<sku>: todo da 404
        Catalogo(merchant_en_producto=False),  # no dice de qué comercio es
        Catalogo(falla_sku="AX-URB-04"),  # uno falla
        Catalogo(otro_merchant_en="AX-URB-04"),  # dos comercios distintos
    ],
    ids=["sin_ruta", "sin_merchant", "falla", "dos_comercios"],
)
async def test_si_por_sku_no_alcanza_pide_la_lista_entera(catalogo):
    async with catalogo.cliente() as client:
        productos, comercio, fuente = await cargar_catalogo(
            client, URL, skus=["NK-OUT-01", "AX-URB-04"]
        )
    assert catalogo.pedidos[-1] == "/products"
    assert productos == PRODUCTOS and comercio == "merchant_1" and fuente == "http"


@pytest.mark.parametrize(
    "skus",
    [
        ["../admin"], ["a/b"], ["NK OUT"], [""], [".."], ["x" * 65], [7],
        [f"SKU-{i}" for i in range(MAX_SKUS_POR_PEDIDO + 1)],
    ],
    ids=["traversal", "barra", "espacio", "vacio", "puntos", "largo", "no_str", "muchos"],
)
async def test_skus_que_no_van_en_una_url_piden_la_lista_entera(skus):
    cat = Catalogo()
    async with cat.cliente() as client:
        productos, _, _ = await cargar_catalogo(client, URL, skus=skus)
    assert cat.pedidos == ["/products"]
    assert productos == PRODUCTOS


@pytest.mark.parametrize("skus", [None, []])
async def test_sin_skus_pide_la_lista_entera(skus):
    cat = Catalogo()
    async with cat.cliente() as client:
        productos, _, _ = await cargar_catalogo(client, URL, skus=skus)
    assert cat.pedidos == ["/products"] and productos == PRODUCTOS


@pytest.mark.parametrize(
    "respuesta",
    [
        lambda r: httpx.Response(200, json={**PRODUCTOS["NK-OUT-01"], "sku": "OTRO",
                                            "merchant": MERCHANT}),
        lambda r: httpx.Response(200, content=b'{"sku": "NK-OUT-01", "price": NaN}'),
        lambda r: httpx.Response(200, json=["no", "es", "objeto"]),
        lambda r: httpx.Response(200, content=b"{" + b" " * (300 * 1024) + b"}"),
        lambda r: httpx.Response(302, headers={"location": "http://otro.test/"}),
    ],
    ids=["otro_sku", "nan", "lista", "grande", "redireccion"],
)
async def test_una_respuesta_rara_por_sku_pide_la_lista_entera(respuesta):
    cat = Catalogo()
    original = cat.handler

    def handler(request):
        if request.url.path == "/products/NK-OUT-01":
            cat.pedidos.append(request.url.path)
            return respuesta(request)
        return original(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        productos, comercio, _ = await cargar_catalogo(client, URL, skus=["NK-OUT-01"])
    assert cat.pedidos[-1] == "/products"
    assert productos == PRODUCTOS and comercio == "merchant_1"


async def test_si_falla_todo_evalua_sin_catalogo_como_hoy():
    cat = Catalogo(falla_sku="NK-OUT-01", lista=lambda r: httpx.Response(503))
    async with cat.cliente() as client:
        assert await cargar_catalogo(client, URL, skus=["NK-OUT-01"]) == ({}, None, "ninguno")
        productos, comercio, fuente = await cargar_catalogo(
            client, URL, respaldo_fixture=True, skus=["NK-OUT-01"]
        )
    assert fuente == "fixture" and productos and comercio == "merchant_1"


async def test_un_timeout_por_sku_pide_la_lista_entera():
    cat = Catalogo()

    def handler(request):
        if request.url.path.startswith("/products/"):
            raise httpx.ReadTimeout("lento", request=request)
        return cat.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        productos, _, fuente = await cargar_catalogo(client, URL, skus=["NK-OUT-01"])
    assert productos == PRODUCTOS and fuente == "http"


def test_skus_de_las_ofertas():
    detalles = [{"sku": "A"}, {"sku": "B"}, {"sku": "A"}, {"sku": 3}, {}, "x", {"sku": ""}]
    assert skus_de(detalles) == ["A", "B"]
    assert skus_de(None) == []


async def test_el_checkout_se_evalua_con_el_catalogo_por_sku(config, stub, merchant):
    """En `create_checkout` el proxy pide al catálogo solo los SKU de las
    ofertas de esa compra (no la lista entera)."""
    from proxy.app import create_app

    pedidos: list[str] = []
    transporte = RuteoPorHost({
        MERCHANT_HOST: merchant.app,
        CATALOG_HOST: app_catalogo(por_sku=True, registro=pedidos),
    })
    evaluador, llamadas = evaluador_espia()
    app = create_app(config=replace(config, ficha_demo=True), transport=transporte,
                     evaluador=evaluador, demo=FuentesDemo())
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
    assert veredictos(task)
    assert "/products" not in pedidos
    assert {p for p in pedidos if p.startswith("/products/")}
    (llamada,) = llamadas
    assert llamada["catalogo"]  # los productos de las ofertas que están publicados
    assert set(llamada["catalogo"]) <= {p.removeprefix("/products/") for p in pedidos}


async def test_el_detector_de_oportunidades_tambien_pide_por_sku(config_rapida, stub, merchant):
    """Con intención: las categorías del intake salen de la lista entera (una
    vez por compra); el análisis de las ofertas y el checkout, por SKU."""
    from proxy.app import create_app

    pedidos: list[str] = []
    transporte = RuteoPorHost({
        MERCHANT_HOST: merchant.app,
        CATALOG_HOST: app_catalogo(por_sku=True, registro=pedidos),
    })
    evaluador, _ = evaluador_espia()
    agente = AgenteSinIntake()
    vistas: list[Any] = []
    agente.detectar_oportunidad = lambda contexto, ficha, ofertas: vistas.append(ofertas)
    app = create_app(config=config_rapida, transport=transporte, evaluador=evaluador,
                     agente=agente)
    # baseline: zapatillas publicadas en el catálogo (las notebooks no están:
    # ahí ningún SKU se encuentra y se pide la lista entera, como antes).
    await _comprar_sin_intake(app, "zapatillas Nike talle 42", escenario="baseline")
    assert pedidos.count("/products") == 1  # las categorías del intake
    assert len([p for p in pedidos if p.startswith("/products/")]) >= 2  # análisis + checkout
    (ofertas,) = vistas
    assert any(o.titulo for o in ofertas)  # verificadas con el catálogo por SKU


async def test_el_catalogo_por_sku_de_otro_comercio_no_se_usa(config, merchant):
    from proxy.app import create_app

    transporte = RuteoPorHost({
        MERCHANT_HOST: merchant.app,
        CATALOG_HOST: app_catalogo("otro_comercio", por_sku=True),
    })
    evaluador, llamadas = evaluador_espia()
    app = create_app(config=replace(config, ficha_demo=True), transport=transporte,
                     evaluador=evaluador, demo=FuentesDemo())
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_1")
        veredicto = [e for e in await eventos(c) if e["tipo"] == "veredicto"][-1]["datos"]
    (llamada,) = llamadas
    assert llamada["catalogo"] == {}
    assert "otro_comercio" in veredicto["catalogo_descartado"]


# -- (f) robustez: sin resultados --------------------------------------------------


def _sin_resultados(respuesta: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
    """El comercio no encontró nada: ni carts ni ofertas (preset sin_resultados)."""
    partes = (pedido.get("params") or {}).get("message", {}).get("parts", [])
    if not any("catalog_search" in (p.get("data") or {}) for p in partes):
        return respuesta
    for artefacto in (respuesta.get("result") or {}).get("artifacts") or []:
        artefacto["parts"] = [
            p for p in artefacto["parts"]
            if "ap2.cart" not in (p.get("data") or {})
        ]
        for p in artefacto["parts"]:
            if isinstance(p.get("data"), dict) and "demo.offer_details" in p["data"]:
                p["data"]["demo.offer_details"] = []
    return respuesta


async def test_sin_resultados_la_busqueda_pasa_sin_oportunidad_ni_errores(
    armar_proxy, config_rapida, merchant
):
    merchant.manipular = _sin_resultados
    agente = AgenteSinIntake()
    vistas: list[Any] = []
    agente.detectar_oportunidad = lambda contexto, ficha, ofertas: vistas.append(ofertas)
    app = armar_proxy(config=config_rapida, agente=agente)
    async with cliente_de(app) as c:
        rpc = pedido_find_products("baseline")
        busqueda = asyncio.create_task(c.post(RPC, content=cuerpo(rpc), headers=HEADERS_A2A))
        compra = await _compra(c, "esperando_contexto")
        await c.post("/guard/compras/contexto",
                     json={"compra_id": compra["compra_id"], "respuesta": "Para correr."})
        task = (await busqueda).json()["result"]
        evs = await eventos(c)
        (final,) = (await c.get("/guard/compras")).json()["compras"]
    assert task["status"]["state"] == "completed"
    assert not [p for a in task.get("artifacts") or [] for p in a["parts"]
                if "ap2.cart" in (p.get("data") or {})]
    assert final["estado"] == "sin_oportunidad"
    assert vistas in ([], [[]])  # nada que proponer
    assert not [e for e in evs if e["tipo"] in ("error", "oportunidad", "bloqueado")]


def test_la_categoria_de_la_ficha_saltea_complementos():
    # "mochila para la notebook" es una mochila (100), no una notebook (328).
    from proxy.fuentes import categoria_nombrada

    assert categoria_nombrada(["Quiero una mochila para la notebook"]) == "100"
    assert categoria_nombrada(["un monitor de 27 pulgadas"]) == "305"
