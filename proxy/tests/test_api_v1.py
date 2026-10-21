"""API `/v1` (`proxy/api_v1.py`): rutas, validación estricta y errores.

Contra un `NucleoFalso` que registra las llamadas: acá no hay proxy ni
middleware de claves. Un middleware de test pone el `Integrador` en
`scope["state"]`, como el de autenticación; sin él, la API responde 401
sola (falla cerrado aunque el middleware no esté montado).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from contracts import Ficha, Requisito
from proxy import api_v1, openapi
from proxy.tipos_gateway import (
    Comercio,
    ErrorApi,
    FotoCompra,
    Integrador,
    PaginaCompras,
    SolicitudCompra,
    SolicitudEvaluar,
    SolicitudFiltrar,
)

pytestmark = pytest.mark.anyio

ACME = Integrador(id="acme", nombre="ACME", comercios=("merchant_1",))
COMPRA_ID = "cmp-0123456789abcdef"
AHORA = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
URL_PUBLICA = "https://gw.example.com"
SECRETO_DEL_NUCLEO = "texto-del-comercio-que-no-tiene-que-salir"

FICHA_OK = {
    "requisitos": [
        {"campo": "precio", "operador": "maximo", "valor": 20000, "obligatorio": True},
        {"campo": "moneda", "operador": "igual", "valor": "USD", "obligatorio": True},
        {"campo": "cantidad", "operador": "minimo", "valor": 20, "obligatorio": True},
    ],
    "categoria_taxonomia": "298",
}
COMPRA_OK = {
    "pedido": "  20 computadoras para el lunes  ",
    "comercio": "merchant_1",
    "ficha": FICHA_OK,
    "intencion": {"porque": "para un aula que arranca el lunes", "preguntar": False},
    "referencia": "ord-123",
}
CHECKOUT_OK = {
    "checkout_jwt": "eyJ.eyJ.sig", "checkout_hash": "abc", "cart_id": "cart_2",
    "amount": 145.0, "currency": "USD",
}


def _foto(**cambios: Any) -> FotoCompra:
    base: dict[str, Any] = dict(
        compra_id=COMPRA_ID, integrador_id="acme", comercio_id="merchant_1",
        referencia="ord-123", estado="creada", creada=AHORA, actualizada=AHORA,
        pedido="20 computadoras para el lunes", contextos=(),
        ficha=Ficha(
            pedido_original="20 computadoras para el lunes", confirmada=True,
            requisitos=[Requisito(campo="precio", operador="maximo", valor=20000, obligatorio=True)],
        ),
        intencion_activa=False, pregunta=None, vence=None, oportunidad=None,
        decision=None, aprobacion_pendiente=None, veredictos=(), aprobaciones=(),
    )
    base.update(cambios)
    return FotoCompra(**base)


class NucleoFalso:
    """Implementa `NucleoGateway`: anota cada llamada y devuelve fotos
    armadas a mano. `falla` hace que la próxima llamada lance eso."""

    ttl_compra_s = 24 * 3600.0

    def __init__(self) -> None:
        self.llamadas: list[tuple[str, tuple[Any, ...]]] = []
        self.falla: Exception | None = None
        self.foto = _foto()
        self.aprobacion_creada = True
        self.clave: dict[str, Any] | None = {"kid": "guard-key-1", "jwk": {}, "keys": [{}]}
        self.filtros: Any = None
        self.siguiente: Any = None
        self.eventos: list[dict[str, Any]] = []
        self.completo = True

    def _anotar(self, nombre: str, *args: Any) -> None:
        self.llamadas.append((nombre, args))
        if self.falla is not None:
            falla, self.falla = self.falla, None
            raise falla

    def comercios_de(self, integrador: Integrador) -> list[Comercio]:
        self._anotar("comercios_de", integrador)
        return [Comercio(id="merchant_1", nombre="Demo", a2a_url="http://127.0.0.1:8001/a2a",
                         catalogo_url=None)]

    def url_a2a(self, comercio_id: str) -> str:
        return f"{URL_PUBLICA}/a2a/m/{comercio_id}"

    async def crear_compra(self, integrador, solicitud):
        self._anotar("crear_compra", integrador, solicitud)
        return self.foto

    async def obtener_compra(self, integrador, compra_id):
        self._anotar("obtener_compra", integrador, compra_id)
        return self.foto

    async def listar_compras(self, integrador, filtros):
        self._anotar("listar_compras", integrador, filtros.limite, filtros.estado)
        self.filtros = filtros
        return PaginaCompras((self.foto, self.foto), self.siguiente)

    async def explicacion_compra(self, integrador, compra_id):
        self._anotar("explicacion_compra", integrador, compra_id)
        return self.foto, list(self.eventos)

    async def resumen_compras(self, integrador, desde, hasta):
        self._anotar("resumen_compras", integrador, desde, hasta)
        return [self.foto], self.completo

    async def responder_porque(self, integrador, compra_id, respuesta):
        self._anotar("responder_porque", integrador, compra_id, respuesta)
        return self.foto

    async def decidir_oportunidad(self, integrador, compra_id, oportunidad_id, decision):
        self._anotar("decidir_oportunidad", integrador, compra_id, oportunidad_id, decision)
        return self.foto

    async def aprobar(self, integrador, compra_id, cart_id, contexto_id):
        self._anotar("aprobar", integrador, compra_id, cart_id, contexto_id)
        return {
            "id": "apr-1", "contexto_id": "ctx-1", "cart_id": cart_id, "centavos": 145000,
            "monto": 1450.0, "moneda": "USD", "creada": "2026-10-18T12:00:00+00:00",
            "estado": "pendiente", "usada": None, "task_id": SECRETO_DEL_NUCLEO + " x",
        }, self.aprobacion_creada

    async def evaluar(self, integrador, solicitud):
        self._anotar("evaluar", integrador, solicitud)
        return {"status": "aprobado", "llega_a_firma": True, "comercio_firmado": "merchant_1"}

    async def filtrar(self, integrador, solicitud):
        self._anotar("filtrar", integrador, solicitud)
        return {"pasa": True, "status": "pasa", "motivo": "ok", "senales": []}

    def clave_publica(self):
        self._anotar("clave_publica")
        return self.clave

    def entregas_webhook(self, integrador, limite):
        self._anotar("entregas_webhook", integrador, limite)
        return [{"id": "whe-1"}]

    async def probar_webhook(self, integrador):
        self._anotar("probar_webhook", integrador)
        return {"id": "whe-2", "tipo": "ping"}


class PonerIntegrador:
    """Doble del middleware de autenticación: pone ACME salvo con el header
    `x-anonimo` (pedido sin clave)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            anonimo = any(k == b"x-anonimo" for k, _ in scope.get("headers", []))
            scope.setdefault("state", {})["integrador"] = None if anonimo else ACME
        await self.app(scope, receive, send)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def nucleo() -> NucleoFalso:
    return NucleoFalso()


def _app(nucleo: NucleoFalso, **kwargs: Any) -> Starlette:
    kwargs.setdefault("url_publica", URL_PUBLICA)
    return Starlette(
        routes=api_v1.rutas_v1(nucleo, **kwargs), middleware=[Middleware(PonerIntegrador)],
    )


@pytest.fixture
async def cliente(nucleo: NucleoFalso) -> AsyncIterator[httpx.AsyncClient]:
    transporte = httpx.ASGITransport(app=_app(nucleo))
    async with httpx.AsyncClient(transport=transporte, base_url="http://gw.test:8030") as c:
        yield c


def _error(r: httpx.Response, status: int, codigo: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    cuerpo = r.json()
    assert cuerpo["error"]["codigo"] == codigo, cuerpo
    assert r.headers["cache-control"] == "no-store"
    return cuerpo["error"]


def _campos(error: dict[str, Any]) -> set[str]:
    return {c["campo"] for c in error["detalle"]["campos"]}


def _llamadas(nucleo: NucleoFalso, nombre: str) -> list[tuple[Any, ...]]:
    return [args for n, args in nucleo.llamadas if n == nombre]


# -- POST /v1/compras --------------------------------------------------------


async def test_crear_compra_feliz(cliente, nucleo):
    r = await cliente.post("/v1/compras", json=COMPRA_OK)
    assert r.status_code == 201, r.text
    vista = r.json()
    assert vista["compra_id"] == COMPRA_ID
    assert vista["a2a_url"] == f"{URL_PUBLICA}/a2a/m/merchant_1"
    assert vista["estado"] == "creada"
    assert r.headers["cache-control"] == "no-store"

    [(integrador, solicitud)] = _llamadas(nucleo, "crear_compra")
    assert integrador is ACME
    assert isinstance(solicitud, SolicitudCompra)
    assert solicitud.pedido == "20 computadoras para el lunes"
    assert solicitud.comercio_id == "merchant_1"
    assert solicitud.porque == "para un aula que arranca el lunes"
    assert solicitud.preguntar_porque is False
    assert solicitud.referencia == "ord-123"
    assert solicitud.ficha.confirmada is True
    assert solicitud.ficha.pedido_original == "20 computadoras para el lunes"
    assert solicitud.ficha.categoria_taxonomia == "298"
    assert [r.campo for r in solicitud.ficha.requisitos] == ["precio", "moneda", "cantidad"]


async def test_crear_compra_minima(cliente, nucleo):
    r = await cliente.post("/v1/compras", json={"pedido": "algo", "ficha": FICHA_OK})
    assert r.status_code == 201
    [(_, solicitud)] = _llamadas(nucleo, "crear_compra")
    assert solicitud.comercio_id is None
    assert solicitud.porque is None and solicitud.preguntar_porque is False
    assert solicitud.referencia is None


async def test_sin_integrador_da_401(cliente, nucleo):
    r = await cliente.post("/v1/compras", json=COMPRA_OK, headers={"x-anonimo": "1"})
    _error(r, 401, "no_autenticado")
    assert r.headers["www-authenticate"] == "Bearer"
    assert nucleo.llamadas == []


async def test_sin_json_da_415(cliente):
    r = await cliente.post("/v1/compras", content=json.dumps(COMPRA_OK),
                           headers={"content-type": "text/plain"})
    _error(r, 415, "se_espera_json")
    r = await cliente.post("/v1/compras", content=json.dumps(COMPRA_OK))
    _error(r, 415, "se_espera_json")


async def test_json_con_charset_sirve(cliente):
    r = await cliente.post("/v1/compras", content=json.dumps(COMPRA_OK),
                           headers={"content-type": "application/json; charset=utf-8"})
    assert r.status_code == 201


def _anidado(niveles: int) -> Any:
    valor: Any = 1
    for _ in range(niveles):
        valor = [valor]
    return valor


async def test_cuerpo_anidado_de_mas_da_400(cliente, nucleo):
    """Más de `MAX_PROFUNDIDAD_JSON` niveles es `json_invalido`, en todas las
    rutas: pydantic no vuelve a serializar ~100 niveles y `deepcopy` revienta
    en ~300 (la compra no se podía guardar ni mostrar)."""
    ficha = {"requisitos": [
        *FICHA_OK["requisitos"][:2],
        {"campo": "marca", "operador": "incluye", "valor": _anidado(120), "obligatorio": False},
    ]}
    _error(await cliente.post("/v1/compras", json={"pedido": "x", "ficha": ficha}),
           400, "json_invalido")
    _error(await cliente.post("/v1/filtrar", json={"contenido": _anidado(300)}),
           400, "json_invalido")
    _error(await cliente.post("/v1/evaluar", json={"x": {"y": _anidado(api_v1.MAX_PROFUNDIDAD_JSON)}}),
           400, "json_invalido")
    # Un nivel de más ya corta (`a2a.anidado_de_mas`, el mismo chequeo del proxy).
    _error(await cliente.post("/v1/filtrar", json={"contenido": _anidado(api_v1.MAX_PROFUNDIDAD_JSON)}),
           400, "json_invalido")
    assert nucleo.llamadas == []
    # Justo en el límite pasa (el cuerpo es el primer nivel).
    r = await cliente.post("/v1/filtrar", json={"contenido": _anidado(api_v1.MAX_PROFUNDIDAD_JSON - 1)})
    assert r.status_code == 200, r.text


async def test_cuerpo_grande_da_413(nucleo):
    transporte = httpx.ASGITransport(app=_app(nucleo, max_cuerpo_bytes=100))
    async with httpx.AsyncClient(transport=transporte, base_url="http://gw.test:8030") as c:
        r = await c.post("/v1/compras", json=COMPRA_OK)
        _error(r, 413, "cuerpo_demasiado_grande")

        # Sin Content-Length (chunked): se corta al leer.
        async def trozos():
            for _ in range(10):
                yield b" " * 50

        r = await c.post("/v1/compras", content=trozos(),
                         headers={"content-type": "application/json"})
        _error(r, 413, "cuerpo_demasiado_grande")
    assert nucleo.llamadas == []


async def test_json_invalido_da_400(cliente):
    for crudo in ("{no es json", '{"pedido": NaN}', '{"x": 1e999}', ""):
        r = await cliente.post("/v1/compras", content=crudo,
                               headers={"content-type": "application/json"})
        _error(r, 400, "json_invalido")


async def test_cuerpo_que_no_es_objeto_da_400(cliente):
    r = await cliente.post("/v1/compras", json=[COMPRA_OK])
    _error(r, 400, "validacion")


async def test_claves_desconocidas_dan_400(cliente, nucleo):
    cuerpo = {
        **COMPRA_OK, "extra": 1,
        "ficha": {**FICHA_OK, "tope": 3},
        "intencion": {"porque": "x", "urgente": True},
    }
    error = _error(await cliente.post("/v1/compras", json=cuerpo), 400, "validacion")
    assert _campos(error) == {"extra", "ficha.tope", "intencion.urgente"}
    assert nucleo.llamadas == []

    requisito_raro = {**FICHA_OK, "requisitos": [
        *FICHA_OK["requisitos"],
        {"campo": "marca", "operador": "igual", "valor": "Dell", "obligatorio": False, "peso": 3},
    ]}
    error = _error(
        await cliente.post("/v1/compras", json={**COMPRA_OK, "ficha": requisito_raro}),
        400, "validacion",
    )
    assert _campos(error) == {"ficha.requisitos[3].peso"}


async def test_validacion_devuelve_todos_los_campos(cliente):
    cuerpo = {
        "pedido": "   ", "comercio": "Merchant 1",
        "ficha": {"requisitos": [], "categoria_taxonomia": "abc"},
        "intencion": {"porque": "", "preguntar": "sí"},
        "referencia": "x" * 201,
    }
    error = _error(await cliente.post("/v1/compras", json=cuerpo), 400, "validacion")
    assert _campos(error) == {
        "pedido", "comercio", "ficha.requisitos", "ficha.categoria_taxonomia",
        "intencion.porque", "intencion.preguntar", "referencia",
    }
    for campo in error["detalle"]["campos"]:
        assert set(campo) == {"campo", "problema"}


async def test_falta_ficha_y_pedido(cliente):
    error = _error(await cliente.post("/v1/compras", json={}), 400, "validacion")
    assert _campos(error) == {"pedido", "ficha"}


async def test_referencia_con_controles_da_400(cliente):
    r = await cliente.post("/v1/compras", json={**COMPRA_OK, "referencia": "ord\x001"})
    assert _campos(_error(r, 400, "validacion")) == {"referencia"}


async def test_requisito_invalido_para_el_modelo_da_validacion(cliente):
    ficha = {"requisitos": [
        *FICHA_OK["requisitos"],
        {"campo": "atributo", "operador": "igual", "valor": "x", "obligatorio": "quizás"},
    ]}
    error = _error(await cliente.post("/v1/compras", json={**COMPRA_OK, "ficha": ficha}),
                   400, "validacion")
    assert all(c.startswith("ficha.requisitos[3]") for c in _campos(error))


async def test_requisito_estricto_y_cronograma(cliente, nucleo):
    precio, moneda, _ = FICHA_OK["requisitos"]
    malos = [
        ({"campo": "marca", "operador": "igual", "valor": "Dell", "obligatorio": "true"},
         "ficha.requisitos[2].obligatorio"),
        ({"campo": "entregas", "operador": "cubre", "valor": [{"cantidad": 0, "fecha": "2026-10-19"}],
          "obligatorio": True}, "ficha.requisitos[2].valor"),
        ({"campo": "entregas", "operador": "cubre", "valor": "el lunes", "obligatorio": True},
         "ficha.requisitos[2].valor"),
    ]
    for requisito, campo in malos:
        r = await cliente.post(
            "/v1/compras", json={**COMPRA_OK, "ficha": {"requisitos": [precio, moneda, requisito]}},
        )
        assert _campos(_error(r, 400, "validacion")) == {campo}
    bueno = {"campo": "entregas", "operador": "cubre", "obligatorio": True,
             "valor": [{"cantidad": 10, "fecha": "2026-10-19"}, {"cantidad": 10, "fecha": "2026-10-22"}]}
    r = await cliente.post(
        "/v1/compras", json={**COMPRA_OK, "ficha": {"requisitos": [precio, moneda, bueno]}},
    )
    assert r.status_code == 201, r.text


async def test_ficha_sin_tope(cliente, nucleo):
    precio, moneda, cantidad = FICHA_OK["requisitos"]
    variantes = [
        [moneda, cantidad],                                      # sin precio
        [precio, cantidad],                                      # sin moneda
        [{**precio, "valor": 0}, moneda],                        # tope 0
        [{**precio, "valor": "20000"}, moneda],                  # tope en texto
        [{**precio, "valor": True}, moneda],                     # bool no es número
        [{**precio, "obligatorio": False}, moneda],              # preferido no es tope
        [{**precio, "operador": "minimo"}, moneda],              # no es un máximo
        [precio, {**moneda, "valor": "usd"}],                    # moneda no ISO
        [precio, {**moneda, "obligatorio": False}],
    ]
    for requisitos in variantes:
        r = await cliente.post("/v1/compras", json={**COMPRA_OK, "ficha": {"requisitos": requisitos}})
        _error(r, 400, "ficha_sin_tope")
    assert nucleo.llamadas == []


async def test_ficha_que_pesa_de_mas_da_400(cliente, nucleo):
    """La compra vive en memoria y en el almacén (24 h) y una clave puede tener
    1000: con fichas de casi 1 MB (lo que deja el cuerpo) eran ~1 GB de JSON
    por integrador, varias veces más en objetos de Python. Una ficha de 50
    requisitos completos pesa unos pocos KB."""
    precio, moneda, _ = FICHA_OK["requisitos"]
    pesada = {"requisitos": [
        precio, moneda,
        {"campo": "marca", "operador": "incluye", "valor": ["x"] * 20_000, "obligatorio": False},
    ]}
    error = _error(await cliente.post("/v1/compras", json={**COMPRA_OK, "ficha": pesada}),
                   400, "validacion")
    assert _campos(error) == {"ficha"}
    assert nucleo.llamadas == []
    # El máximo de requisitos, cada uno con un atributo, entra holgado.
    llena = {"requisitos": [precio, moneda] + [
        {"campo": "atributo", "atributo": f"atributo_{i:02d}_con_nombre_largo", "unidad": "GB",
         "operador": "incluye", "valor": ["valor uno", "valor dos", "valor tres"],
         "obligatorio": False}
        for i in range(api_v1.MAX_REQUISITOS - 2)
    ]}
    r = await cliente.post("/v1/compras", json={**COMPRA_OK, "ficha": llena})
    assert r.status_code == 201, r.text


async def test_requisito_no_soportado(cliente, nucleo):
    precio, moneda, _ = FICHA_OK["requisitos"]
    for raro, campo in [
        ({"campo": "color_del_alma", "operador": "igual", "valor": "x", "obligatorio": True},
         "ficha.requisitos[2].campo"),
        ({"campo": "marca", "operador": "entre", "valor": "x", "obligatorio": True},
         "ficha.requisitos[2].operador"),
    ]:
        r = await cliente.post(
            "/v1/compras", json={**COMPRA_OK, "ficha": {"requisitos": [precio, moneda, raro]}},
        )
        error = _error(r, 400, "requisito_no_soportado")
        assert _campos(error) == {campo}
    assert nucleo.llamadas == []


async def test_error_del_nucleo_pasa_con_status_y_cuerpo(cliente, nucleo):
    nucleo.falla = ErrorApi(429, "demasiadas_compras", "Demasiadas compras activas.", {"max": 1})
    r = await cliente.post("/v1/compras", json=COMPRA_OK)
    assert r.status_code == 429
    assert r.json() == {"error": {
        "codigo": "demasiadas_compras", "mensaje": "Demasiadas compras activas.",
        "detalle": {"max": 1},
    }}

    nucleo.falla = ErrorApi(403, "comercio_no_permitido", "No.")
    _error(await cliente.post("/v1/compras", json=COMPRA_OK), 403, "comercio_no_permitido")


async def test_excepcion_del_nucleo_da_500_sin_texto(cliente, nucleo):
    nucleo.falla = RuntimeError(SECRETO_DEL_NUCLEO)
    r = await cliente.post("/v1/compras", json=COMPRA_OK)
    error = _error(r, 500, "interno")
    assert "detalle" not in error
    assert SECRETO_DEL_NUCLEO not in r.text


# -- GET /v1/compras ---------------------------------------------------------


async def test_listar_compras(cliente, nucleo):
    r = await cliente.get("/v1/compras", params={"limite": "1", "estado": "negociando"})
    assert r.status_code == 200
    # El núcleo devolvió 2: la API respeta el límite igual.
    assert len(r.json()["compras"]) == 1
    assert _llamadas(nucleo, "listar_compras") == [(ACME, 1, "negociando")]

    r = await cliente.get("/v1/compras")
    assert r.status_code == 200 and len(r.json()["compras"]) == 2
    assert _llamadas(nucleo, "listar_compras")[-1] == (ACME, 50, None)


async def test_listar_compras_valida_parametros(cliente, nucleo):
    for params in ({"limite": "0"}, {"limite": "201"}, {"limite": "diez"}, {"limite": "-1"},
                   {"estado": "inventado"}):
        _error(await cliente.get("/v1/compras", params=params), 400, "validacion")
    assert nucleo.llamadas == []


async def test_obtener_compra(cliente, nucleo):
    nucleo.foto = _foto(
        estado="esperando_contexto", pregunta="¿Para qué?", vence=AHORA, contextos=("ctx-1",),
        veredictos=({"status": "escalar", "cart_id": "cart_2"},),
    )
    r = await cliente.get(f"/v1/compras/{COMPRA_ID}")
    assert r.status_code == 200
    vista = r.json()
    assert vista["pendiente"]["tipo"] == "porque"
    assert vista["contextos"] == ["ctx-1"]
    assert vista["veredictos"] == [{"status": "escalar", "cart_id": "cart_2"}]
    assert _llamadas(nucleo, "obtener_compra") == [(ACME, COMPRA_ID)]


async def test_compra_id_invalido_da_404_sin_llamar(cliente, nucleo):
    for malo in ("cmp-123", "cmp-0123456789ABCDEF", "otra-cosa"):
        _error(await cliente.get(f"/v1/compras/{malo}"), 404, "compra_desconocida")
        _error(await cliente.post(f"/v1/compras/{malo}/respuesta", json={"omitir": True}),
               404, "compra_desconocida")
    assert nucleo.llamadas == []


async def test_compra_de_otro_integrador_404_del_nucleo(cliente, nucleo):
    nucleo.falla = ErrorApi(404, "compra_desconocida", "No existe esa compra.")
    _error(await cliente.get(f"/v1/compras/{COMPRA_ID}"), 404, "compra_desconocida")


# -- respuesta, decisión y aprobación ----------------------------------------


async def test_responder_porque(cliente, nucleo):
    url = f"/v1/compras/{COMPRA_ID}/respuesta"
    assert (await cliente.post(url, json={"respuesta": "  para un aula  "})).status_code == 200
    assert (await cliente.post(url, json={"omitir": True})).status_code == 200
    assert _llamadas(nucleo, "responder_porque") == [
        (ACME, COMPRA_ID, "para un aula"), (ACME, COMPRA_ID, None),
    ]


async def test_responder_porque_valida(cliente, nucleo):
    url = f"/v1/compras/{COMPRA_ID}/respuesta"
    for cuerpo in ({}, {"respuesta": "x", "omitir": True}, {"respuesta": ""},
                   {"respuesta": "x" * 1001}, {"omitir": False}, {"omitir": "sí"},
                   {"respuesta": 3}, {"respuesta": "x", "otra": 1}):
        _error(await cliente.post(url, json=cuerpo), 400, "validacion")
    assert nucleo.llamadas == []


async def test_responder_en_otro_estado_da_409_del_nucleo(cliente, nucleo):
    nucleo.falla = ErrorApi(409, "estado", "La compra no espera una respuesta.",
                            {"estado": "negociando"})
    r = await cliente.post(f"/v1/compras/{COMPRA_ID}/respuesta", json={"omitir": True})
    assert _error(r, 409, "estado")["detalle"] == {"estado": "negociando"}


async def test_decidir_oportunidad(cliente, nucleo):
    url = f"/v1/compras/{COMPRA_ID}/decision"
    r = await cliente.post(url, json={"oportunidad_id": "op-1", "decision": "aceptar"})
    assert r.status_code == 200
    assert _llamadas(nucleo, "decidir_oportunidad") == [(ACME, COMPRA_ID, "op-1", "aceptar")]
    for cuerpo in ({"oportunidad_id": "op-1", "decision": "quizás"},
                   {"oportunidad_id": "op 1", "decision": "aceptar"},
                   {"decision": "rechazar"},
                   {"oportunidad_id": "op-1", "decision": "aceptar", "por": "llm"}):
        _error(await cliente.post(url, json=cuerpo), 400, "validacion")
    nucleo.falla = ErrorApi(409, "oportunidad_distinta", "Esa oportunidad ya no está vigente.")
    r = await cliente.post(url, json={"oportunidad_id": "op-2", "decision": "rechazar"})
    _error(r, 409, "oportunidad_distinta")


async def test_aprobar_crea_o_devuelve_la_existente(cliente, nucleo):
    url = f"/v1/compras/{COMPRA_ID}/aprobaciones"
    r = await cliente.post(url, json={"cart_id": "cart_2", "contexto_id": "ctx-1"})
    assert r.status_code == 201
    vista = r.json()
    assert vista["id"] == "apr-1" and vista["centavos"] == 145000 and vista["monto"] == 1450.0
    assert vista["task_id"] is None  # el del núcleo no tenía formato seguro
    assert SECRETO_DEL_NUCLEO not in r.text

    nucleo.aprobacion_creada = False
    r = await cliente.post(url, json={"cart_id": "cart_2"})
    assert r.status_code == 200
    assert _llamadas(nucleo, "aprobar") == [
        (ACME, COMPRA_ID, "cart_2", "ctx-1"), (ACME, COMPRA_ID, "cart_2", None),
    ]


async def test_aprobar_valida_y_pasa_errores(cliente, nucleo):
    url = f"/v1/compras/{COMPRA_ID}/aprobaciones"
    for cuerpo in ({}, {"cart_id": "cart 2"}, {"cart_id": "c", "contexto_id": "ctx 1"},
                   {"cart_id": "c", "monto": 1}):
        _error(await cliente.post(url, json=cuerpo), 400, "validacion")
    assert nucleo.llamadas == []
    for codigo in ("sin_escalar", "cart_distinto"):
        nucleo.falla = ErrorApi(409, codigo, "No.")
        _error(await cliente.post(url, json={"cart_id": "cart_2"}), 409, codigo)
    nucleo.falla = ErrorApi(503, "estado_no_disponible", "El almacén no responde.")
    _error(await cliente.post(url, json={"cart_id": "cart_2"}), 503, "estado_no_disponible")


# -- evaluar y filtrar -------------------------------------------------------


def _evaluar(**cambios: Any) -> dict[str, Any]:
    return {
        "ficha": {"pedido": "zapatillas", "requisitos": FICHA_OK["requisitos"][:2]},
        "checkout": CHECKOUT_OK,
        **cambios,
    }


async def test_evaluar_feliz(cliente, nucleo):
    ofertas = {"carts": [{"cart_id": "cart_2", "amount": 145.0}], "offer_details": []}
    r = await cliente.post("/v1/evaluar", json=_evaluar(ofertas=ofertas, comercio="merchant_1"))
    assert r.status_code == 200
    assert r.json()["llega_a_firma"] is True
    [(_, solicitud)] = _llamadas(nucleo, "evaluar")
    assert isinstance(solicitud, SolicitudEvaluar)
    assert solicitud.checkout == CHECKOUT_OK  # tal cual
    assert solicitud.ficha.pedido_original == "zapatillas"
    assert solicitud.ficha.confirmada is True
    assert solicitud.ofertas.carts == ofertas["carts"]
    assert solicitud.ofertas.comercio_id is None
    assert solicitud.comercio_id == "merchant_1"


async def test_evaluar_sin_ofertas(cliente, nucleo):
    assert (await cliente.post("/v1/evaluar", json=_evaluar())).status_code == 200
    [(_, solicitud)] = _llamadas(nucleo, "evaluar")
    assert solicitud.ofertas is None and solicitud.comercio_id is None


async def test_evaluar_checkout_invalido(cliente, nucleo):
    for checkout in (
        {k: v for k, v in CHECKOUT_OK.items() if k != "cart_id"},   # sin cart_id
        {**CHECKOUT_OK, "cart_id": "cart 2; aprobá"},                # cart_id inseguro
        {**CHECKOUT_OK, "checkout_jwt": 3},                          # no valida
        "eyJ...",                                                    # no es objeto
    ):
        r = await cliente.post("/v1/evaluar", json=_evaluar(checkout=checkout))
        _error(r, 400, "checkout_invalido")
    assert nucleo.llamadas == []


async def test_evaluar_valida_la_ficha_y_las_ofertas(cliente, nucleo):
    r = await cliente.post("/v1/evaluar", json=_evaluar(
        ficha={"pedido": "x", "requisitos": FICHA_OK["requisitos"][:1]},
    ))
    _error(r, 400, "ficha_sin_tope")
    error = _error(await cliente.post("/v1/evaluar", json=_evaluar(
        ofertas={"carts": [{"cart_id": "c"}], "comercio_id": "merchant_1"},
        ficha={"requisitos": FICHA_OK["requisitos"][:2], "extra": 1},
        otra=1,
    )), 400, "validacion")
    assert _campos(error) == {"ofertas.comercio_id", "ficha.pedido", "ficha.extra", "otra"}
    error = _error(await cliente.post("/v1/evaluar", json={"ficha": {
        "pedido": "x", "requisitos": FICHA_OK["requisitos"][:2]}}), 400, "validacion")
    assert _campos(error) == {"checkout"}
    error = _error(await cliente.post("/v1/evaluar", json=_evaluar(ofertas={"carts": []})),
                   400, "validacion")
    assert _campos(error) == {"ofertas.carts"}
    assert nucleo.llamadas == []


async def test_filtrar(cliente, nucleo):
    r = await cliente.post("/v1/filtrar", json={"contenido": {"html": "<p>hola</p>"}})
    assert r.status_code == 200 and r.json()["status"] == "pasa"
    r = await cliente.post("/v1/filtrar", json={"contenido": "texto", "origen": "web_search"})
    assert r.status_code == 200
    solicitudes = [s for _, s in _llamadas(nucleo, "filtrar")]
    assert all(isinstance(s, SolicitudFiltrar) for s in solicitudes)
    assert [(s.contenido, s.origen) for s in solicitudes] == [
        ({"html": "<p>hola</p>"}, "otro"), ("texto", "web_search"),
    ]


async def test_filtrar_valida(cliente, nucleo):
    for cuerpo in ({}, {"contenido": "x", "origen": "email"}, {"contenido": "x", "extra": 1}):
        _error(await cliente.post("/v1/filtrar", json=cuerpo), 400, "validacion")
    assert nucleo.llamadas == []


# -- comercios, webhooks y públicas ------------------------------------------


async def test_comercios(cliente):
    r = await cliente.get("/v1/comercios")
    assert r.json() == {"comercios": [
        {"id": "merchant_1", "nombre": "Demo", "a2a_url": f"{URL_PUBLICA}/a2a/m/merchant_1"},
    ]}
    _error(await cliente.get("/v1/comercios", headers={"x-anonimo": "1"}), 401, "no_autenticado")


async def test_webhooks(cliente, nucleo):
    r = await cliente.get("/v1/webhooks/entregas", params={"limite": "5"})
    assert r.json() == {"entregas": [{"id": "whe-1"}]}
    assert _llamadas(nucleo, "entregas_webhook") == [(ACME, 5)]

    r = await cliente.post("/v1/webhooks/prueba", json={})
    assert r.status_code == 200 and r.json()["tipo"] == "ping"
    _error(await cliente.post("/v1/webhooks/prueba", json={"url": "http://interno"}),
           400, "validacion")
    nucleo.falla = ErrorApi(409, "sin_webhook", "No hay webhook configurado.")
    _error(await cliente.post("/v1/webhooks/prueba", json={}), 409, "sin_webhook")


async def test_public_key_es_publica(cliente, nucleo):
    r = await cliente.get("/v1/public-key", headers={"x-anonimo": "1"})
    assert r.status_code == 200 and r.json()["kid"] == "guard-key-1"
    nucleo.clave = None
    _error(await cliente.get("/v1/public-key"), 503, "sin_clave_recibo")


async def test_openapi_lista_todas_las_rutas(cliente, nucleo):
    r = await cliente.get("/v1/openapi.json", headers={"x-anonimo": "1"})
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.1")
    assert spec["servers"] == [{"url": URL_PUBLICA}]
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"

    # Las rutas finales (404/405 en JSON) no son operaciones: no van al spec.
    rutas = {
        (metodo.lower(), ruta.path)
        for ruta in api_v1.rutas_v1(nucleo) if isinstance(ruta, Route) and ruta.include_in_schema
        for metodo in ruta.methods if metodo != "HEAD"
    }
    en_spec = {(m, p) for p, ops in spec["paths"].items() for m in ops}
    assert en_spec == rutas
    assert len(rutas) == 17

    publicas = {p for p, ops in spec["paths"].items() for op in ops.values()
                if op.get("security") == []}
    assert publicas == {"/v1/public-key", "/v1/openapi.json", "/v1/docs"}
    # Todo `$ref` apunta a un esquema que existe.
    refs = set()
    pila: list[Any] = [spec]
    while pila:
        actual = pila.pop()
        if isinstance(actual, dict):
            if "$ref" in actual:
                refs.add(actual["$ref"].rsplit("/", 1)[-1])
            pila.extend(actual.values())
        elif isinstance(actual, list):
            pila.extend(actual)
    assert refs <= set(spec["components"]["schemas"])


# -- rutas que no existen -----------------------------------------------------


@pytest.mark.parametrize(
    "ruta", ["/v1/nada", "/v1", "/v1/", "/v1/compras/", f"/v1/compras/{COMPRA_ID}/nada"],
)
async def test_ruta_inexistente_da_404_en_json(cliente, nucleo, ruta):
    """docs/INTEGRACION.md §11 y el spec prometen `no_encontrado` en JSON, no
    el `Not Found` en texto de Starlette (ni una redirección por la barra)."""
    for metodo in ("GET", "POST", "PUT"):
        r = await cliente.request(metodo, ruta, json={})
        _error(r, 404, "no_encontrado")
    assert nucleo.llamadas == []


async def test_metodo_que_la_ruta_no_acepta_da_405_en_json(cliente, nucleo):
    casos = [
        ("DELETE", "/v1/compras", {"GET", "HEAD", "POST"}),
        ("PUT", f"/v1/compras/{COMPRA_ID}", {"GET", "HEAD"}),
        ("GET", f"/v1/compras/{COMPRA_ID}/decision", {"POST"}),
        ("PATCH", "/v1/evaluar", {"POST"}),
        ("PROPFIND", "/v1/comercios", {"GET", "HEAD"}),  # un método raro, también JSON
    ]
    for metodo, ruta, permitidos in casos:
        r = await cliente.request(metodo, ruta)
        _error(r, 405, "metodo_no_permitido")
        assert {m.strip() for m in r.headers["allow"].split(",")} == permitidos, ruta
    assert nucleo.llamadas == []


async def test_sin_clave_la_ruta_inexistente_da_401(cliente, nucleo):
    """Como el middleware: sin clave no se dice qué rutas existen. Una ruta
    pública con otro método sí da 405 (no pide clave)."""
    _error(await cliente.get("/v1/nada", headers={"x-anonimo": "1"}), 401, "no_autenticado")
    _error(await cliente.delete("/v1/compras", headers={"x-anonimo": "1"}), 401, "no_autenticado")
    r = await cliente.post("/v1/public-key", json={}, headers={"x-anonimo": "1"})
    _error(r, 405, "metodo_no_permitido")
    assert r.headers["allow"] == "GET, HEAD"
    assert nucleo.llamadas == []


async def test_docs_es_html_y_escapa(nucleo):
    maliciosa = 'https://gw.example.com/"><script>alert(1)</script>'
    transporte = httpx.ASGITransport(app=_app(nucleo, url_publica=maliciosa))
    async with httpx.AsyncClient(transport=transporte, base_url="http://gw.test:8030") as c:
        r = await c.get("/v1/docs", headers={"x-anonimo": "1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "<script" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "/v1/compras/{compra_id}/aprobaciones" in r.text


def test_pagina_docs_escapa_todo_el_spec():
    spec = openapi.especificacion("https://gw.example.com")
    spec["paths"]["/v1/<b>x</b>"] = {"get": {"summary": "<img src=x>", "responses": {}}}
    pagina = openapi.pagina_docs(spec)
    assert "<img" not in pagina and "<b>" not in pagina
    assert pagina.startswith("<!doctype html>")


def test_openapi_documenta_solo_lectura_en_los_post():
    """Una clave de solo lectura (`proxy/claves.py`) solo consulta: todo POST de
    `/v1` con clave documenta el 403 `solo_lectura`; las GET y las públicas, no."""
    spec = openapi.especificacion(URL_PUBLICA)
    for ruta, ops in spec["paths"].items():
        for metodo, op in ops.items():
            publica = op.get("security") == []
            respuesta = op["responses"].get("403")
            if metodo == "post" and not publica:
                assert respuesta is not None, (metodo, ruta)
                assert "solo_lectura" in respuesta["description"], (metodo, ruta)
            elif respuesta is not None:
                assert "solo_lectura" not in respuesta["description"], (metodo, ruta)
    # El 403 de `POST /v1/compras` sigue con `comercio_no_permitido`.
    crear = spec["paths"]["/v1/compras"]["post"]["responses"]["403"]["description"]
    assert "comercio_no_permitido" in crear and "solo_lectura" in crear
    respuesta = spec["paths"]["/v1/compras/{compra_id}/respuesta"]["post"]["responses"]["403"]
    assert respuesta["description"] == "Códigos: solo_lectura"
    assert "solo_lectura" in spec["components"]["schemas"]["Error"]["description"]
