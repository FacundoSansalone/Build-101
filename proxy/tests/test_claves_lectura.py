"""Claves de solo lectura (`"permisos": ["lectura"]`), para el panel web.

Una clave de lectura es del mismo integrador que su clave completa y ve lo
mismo (sus compras, sus comercios, sus entregas), pero solo puede lo que está
en la lista blanca `claves.RUTAS_LECTURA` (GET en `/v1`). Todo POST de `/v1`,
lo que no está en la lista y todo `/a2a/*` da 403 `solo_lectura`, con la
forma de error de `/v1`, antes de llegar a la app (el comercio no recibe
nada). El permiso sale de la clave autenticada, nunca de un header.

Se prueba en el oyente público y en el local, con el almacén en memoria, con
varias instancias sobre una base compartida y en la app de Vercel; además el
formato del archivo (y su compatibilidad con los JSON de siempre), que la
comparación siga en tiempo constante, la CLI y `generar_secretos.py
--clave-lectura`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

import httpx
import pytest

from proxy import claves
from proxy.arranque import cargar_gateway, cargar_integradores
from proxy.claves import (
    PERMISO_LECTURA,
    RUTAS_LECTURA,
    RegistroIntegradores,
    generar_clave,
    hash_clave,
)
from proxy.estado import EstadoSupabase
from proxy.tests.ayudantes import evaluador_espia
from proxy.tests.fake_world import (
    CATALOG_HOST,
    HEADERS_A2A,
    MERCHANT_HOST,
    FakeMerchant,
    RuteoPorHost,
    app_catalogo,
    cuerpo,
    pedido_find_products,
)
from proxy.tests.gateway_ayudantes import (
    BASE_LOCAL,
    BASE_PUBLICA,
    CLAVE_ACME,
    CLAVE_OTRO,
    FICHA,
    GATEWAY_PUBLIC_URL,
    KID_ACME,
    MERCHANT_B_HOST,
    SECRETO_WEBHOOK,
    SHA_ACME,
    FakeMerchantB,
    armar_gateway,
    buscar,
    crear_compra,
    datos_integradores,
)
from proxy.tests.postgrest_falso import BaseFalsa
from proxy.tipos_gateway import HEADER_COMPRA, RUTAS_V1_PUBLICAS, ConfigInvalida

pytestmark = pytest.mark.anyio

# Clave de lectura de acme (se genera al importar; nunca queda en un archivo).
CLAVE_LECTURA, KID_LECTURA, SHA_LECTURA = generar_clave()
# Una de lectura dada de baja, también de acme.
CLAVE_LECTURA_BAJA, KID_LECTURA_BAJA, SHA_LECTURA_BAJA = generar_clave()


def datos_con_lectura(**kwargs: Any) -> dict[str, Any]:
    """Los integradores de siempre (`acme` y `otro`) y, en `acme`, una clave de
    lectura activa y otra dada de baja."""
    datos = datos_integradores(**kwargs)
    acme = next(i for i in datos["integradores"] if i["id"] == "acme")
    acme["claves"] += [
        {"kid": KID_LECTURA, "sha256": SHA_LECTURA, "creada": "2026-10-21", "activa": True,
         "permisos": [PERMISO_LECTURA]},
        {"kid": KID_LECTURA_BAJA, "sha256": SHA_LECTURA_BAJA, "activa": False,
         "permisos": [PERMISO_LECTURA]},
    ]
    return datos


def registro_con_lectura(**kwargs: Any) -> RegistroIntegradores:
    return RegistroIntegradores.desde_dict(
        datos_con_lectura(**kwargs), {"SECRETO_ACME": SECRETO_WEBHOOK}
    )


def _acme_con(*entradas: dict[str, Any]) -> dict[str, Any]:
    return {"integradores": [{"id": "acme", "claves": list(entradas)}]}


# ===========================================================================
# formato del archivo de claves
# ===========================================================================


def test_sin_permisos_es_una_clave_completa():
    registro = registro_con_lectura()
    completa = registro.autenticar_clave(CLAVE_ACME)
    assert completa is not None and completa.integrador.id == "acme"
    assert completa.permisos is None and completa.solo_lectura is False
    assert completa.permite("POST", "/v1/compras") and completa.permite("POST", "/a2a/m/merchant_1")


def test_una_clave_de_lectura_es_del_mismo_integrador():
    registro = registro_con_lectura()
    lectura = registro.autenticar_clave(CLAVE_LECTURA)
    completa = registro.autenticar_clave(CLAVE_ACME)
    assert lectura is not None and lectura.solo_lectura is True
    assert lectura.permisos == frozenset({PERMISO_LECTURA})
    # El mismo `Integrador` (mismos comercios, webhook y tope): ve lo mismo.
    assert lectura.integrador == completa.integrador == registro.get("acme")
    # `autenticar()` sigue devolviendo el integrador, sea cual sea la clave.
    assert registro.autenticar(CLAVE_LECTURA) == registro.get("acme")


def test_una_clave_de_lectura_dada_de_baja_no_autentica():
    registro = registro_con_lectura()
    assert registro.autenticar_clave(CLAVE_LECTURA_BAJA) is None
    assert registro.autenticar(CLAVE_LECTURA_BAJA) is None


def test_solo_claves_de_lectura_cuentan_como_claves_activas():
    """Una clave de lectura activa abre el oyente público (el panel la usa)."""
    clave, kid, sha = generar_clave()
    registro = RegistroIntegradores.desde_dict(
        _acme_con({"kid": kid, "sha256": sha, "permisos": ["lectura"]}), {}
    )
    assert registro.hay_claves() is True
    assert registro.autenticar_clave(clave).solo_lectura is True


def test_permisos_repetidos_valen_una_vez():
    clave, kid, sha = generar_clave()
    registro = RegistroIntegradores.desde_dict(
        _acme_con({"kid": kid, "sha256": sha, "permisos": ["lectura", "lectura"]}), {}
    )
    assert registro.autenticar_clave(clave).permisos == frozenset({"lectura"})


@pytest.mark.parametrize(
    "permisos",
    [
        "lectura",               # texto, no lista
        [],                      # ¿ninguno o todos? ambiguo: no arranca
        None,                    # null explícito
        ["escritura"],           # desconocido
        ["Lectura"],             # distingue mayúsculas
        ["lectura", "admin"],    # uno conocido y otro no
        [1],
        [["lectura"]],
        {"lectura": True},
        True,
    ],
    ids=["texto", "vacia", "null", "desconocido", "mayuscula", "mezcla", "numero",
         "anidada", "objeto", "bool"],
)
def test_permisos_invalidos_no_arranca(permisos):
    datos = _acme_con({"kid": "a1b2c3d4e5f6", "sha256": "0" * 64, "permisos": permisos})
    with pytest.raises(ConfigInvalida) as e:
        RegistroIntegradores.desde_dict(datos, {})
    mensaje = str(e.value)
    assert "permisos" in mensaje and "claves[0]" in mensaje
    assert "0" * 64 not in mensaje  # el hash no se muestra


def test_permisos_invalidos_en_la_variable_o_el_archivo_no_arrancan(config, tmp_path):
    malos = _acme_con({"kid": "a1b2c3d4e5f6", "sha256": "0" * 64, "permisos": ["admin"]})
    with pytest.raises(ConfigInvalida, match="GATEWAY_CLAVES_JSON: .*permisos"):
        cargar_integradores(config, {"GATEWAY_CLAVES_JSON": json.dumps(malos)})
    archivo = tmp_path / "claves.json"
    archivo.write_text(json.dumps(malos), encoding="utf-8")
    with pytest.raises(ConfigInvalida, match="permisos"):
        cargar_integradores(replace(config, claves_archivo=archivo), {})
    # `python -m proxy` no arranca (SystemExit con el motivo).
    with pytest.raises(SystemExit, match="permisos"):
        cargar_gateway(config, {"GATEWAY_CLAVES_JSON": json.dumps(malos)})


def test_permisos_invalidos_en_vercel_es_motor_no_configurado():
    from proxy import vercel
    from proxy.tests import test_vercel_entrada as tv

    malos = datos_con_lectura(webhook=False)
    malos["integradores"][0]["claves"][-1]["permisos"] = ["todo"]
    app = vercel.crear_app_serverless(
        tv.entorno(GATEWAY_CLAVES_JSON=json.dumps(malos)),
        almacen=EstadoSupabase(tv.URL_SUPABASE, tv.SECRETO_SUPABASE,
                               transport=BaseFalsa().transporte()),
        transport=_transporte(FakeMerchant()),
    )
    assert isinstance(app, vercel.AppNoConfigurada)


def test_una_clave_de_lectura_valida_con_los_comercios_del_ejemplo(config, tmp_path):
    """La validación cruzada del arranque no cambia: el integrador con su clave
    de lectura nombra comercios de `deploy/vercel/comercios.ejemplo.json`."""
    from proxy.tests.test_despliegue_vercel import DEPLOY_VERCEL

    ejemplo = (DEPLOY_VERCEL / "comercios.ejemplo.json").read_text(encoding="utf-8")
    comercios = ejemplo.replace("<tu-dominio>", "mi-kaiai")
    datos = datos_con_lectura(webhook=False, comercios_acme=["merchant_1"])
    integradores, registro_comercios, _ = cargar_gateway(
        config, {"GATEWAY_CLAVES_JSON": json.dumps(datos), "GATEWAY_COMERCIOS_JSON": comercios},
    )
    assert integradores.autenticar_clave(CLAVE_LECTURA).solo_lectura is True
    assert registro_comercios.get("merchant_1") is not None


# -- compatibilidad con los JSON de siempre -------------------------------------------

# El formato documentado antes de las claves de lectura (proxy/README.md,
# "Integradores"), con los placeholders completados.
_FORMATO_DE_SIEMPRE = """
{
  "integradores": [
    {
      "id": "acme",
      "nombre": "ACME Compras",
      "claves": [{"kid": "a1b2c3d4e5f6", "sha256": "%s", "creada": "2026-10-18", "activa": true}],
      "comercios": ["merchant_1", "tienda_b"],
      "webhook": {"url": "https://acme.example/hooks/guard", "secreto_env": "WEBHOOK_SECRETO_ACME"},
      "max_compras_activas": 1000
    }
  ]
}
"""


def _registros_de_siempre() -> list[tuple[str, dict[str, Any], dict[str, str]]]:
    from proxy.tests.test_despliegue_vercel import _script

    gs = _script("generar_secretos")
    con_webhook, _, secreto_env, secreto = gs.registro_integradores(
        "acme", "ACME", ["merchant_1"], "https://acme.example/hook"
    )
    sin_webhook = gs.registro_integradores("demo", None, ["merchant_1"], None)[0]
    kid, sha = "a1b2c3d4e5f6", "0" * 64
    return [
        ("tests", datos_integradores(), {"SECRETO_ACME": SECRETO_WEBHOOK}),
        ("tests_sin_webhook", datos_integradores(webhook=False), {}),
        ("readme", json.loads(_FORMATO_DE_SIEMPRE % sha), {"WEBHOOK_SECRETO_ACME": SECRETO_WEBHOOK}),
        ("smoke_v1_general",
         {"integradores": [{"id": "kaiai-smoke", "claves": [{"kid": kid, "sha256": sha, "activa": True}],
                            "comercios": ["merchant_1"]}]}, {}),
        ("generar_secretos", con_webhook, {secreto_env: secreto}),
        ("generar_secretos_sin_webhook", sin_webhook, {}),
    ]


@pytest.mark.parametrize(
    ("nombre", "datos", "env"), _registros_de_siempre(), ids=[r[0] for r in _registros_de_siempre()]
)
def test_los_json_de_claves_de_siempre_dan_claves_completas(nombre, datos, env):
    registro = RegistroIntegradores.desde_dict(datos, env)
    guardadas = list(registro._claves.values())
    assert guardadas and all(g.permisos is None for g in guardadas), nombre


def test_las_claves_de_los_tests_siguen_siendo_completas():
    registro = registro_con_lectura()
    for clave in (CLAVE_ACME, CLAVE_OTRO):
        autenticada = registro.autenticar_clave(clave)
        assert autenticada is not None and autenticada.solo_lectura is False


# -- tiempo constante ------------------------------------------------------------------


def test_la_comparacion_sigue_en_tiempo_constante(monkeypatch):
    """Una sola `hmac.compare_digest` por intento, siempre entre dos sha256 hex,
    sea una clave completa, de lectura, de baja, con el secreto cambiado, con
    un kid que no existe o basura: el tiempo no dice qué kid existe ni qué
    permiso tiene."""
    registro = registro_con_lectura()
    comparaciones: list[tuple[int, int]] = []
    real = claves.hmac.compare_digest

    def espia(a: bytes, b: bytes) -> bool:
        comparaciones.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(claves.hmac, "compare_digest", espia)
    secreto_cambiado = f"psg_{KID_LECTURA}_" + "Z" * 43
    casos = [CLAVE_ACME, CLAVE_LECTURA, CLAVE_LECTURA_BAJA, secreto_cambiado,
             generar_clave()[0], "psg_basura", "", None]
    for clave in casos:
        comparaciones.clear()
        registro.autenticar_clave(clave)
        assert comparaciones == [(64, 64)], clave
        comparaciones.clear()
        registro.autenticar(clave)
        assert comparaciones == [(64, 64)], clave
    assert hash_clave(CLAVE_LECTURA) == SHA_LECTURA


# ===========================================================================
# el gateway con una clave de lectura
# ===========================================================================


@dataclass
class Gateway:
    app: Any
    base: str
    merchant: FakeMerchant
    nombre: str


def _transporte(merchant: FakeMerchant) -> RuteoPorHost:
    return RuteoPorHost({
        MERCHANT_HOST: merchant.app,
        CATALOG_HOST: app_catalogo(),
        MERCHANT_B_HOST: FakeMerchantB().app,
    })


def _app_vercel(merchant: FakeMerchant) -> Any:
    """La app de Vercel (todo es del oyente público), con la clave de lectura
    en `GATEWAY_CLAVES_JSON` y la base compartida falsa."""
    from proxy import vercel
    from proxy.tests import test_vercel_entrada as tv

    app = vercel.crear_app_serverless(
        tv.entorno(GATEWAY_CLAVES_JSON=json.dumps(datos_con_lectura(webhook=False))),
        almacen=EstadoSupabase(tv.URL_SUPABASE, tv.SECRETO_SUPABASE,
                               transport=BaseFalsa().transporte()),
        transport=_transporte(merchant),
    )
    assert not isinstance(app, vercel.AppNoConfigurada)
    return app


MODOS = [
    ("memoria", "publico"), ("memoria", "local"),
    ("varias", "publico"), ("varias", "local"),
    ("vercel", "publico"),
]


@pytest.fixture(params=MODOS, ids=[f"{m}-{o}" for m, o in MODOS])
def gateway(request, config) -> Gateway:
    modo, oyente = request.param
    merchant = FakeMerchant()
    nombre = f"{modo}-{oyente}"
    if modo == "vercel":
        from proxy.tests.test_vercel_entrada import URL_PUBLICA

        return Gateway(_app_vercel(merchant), URL_PUBLICA, merchant, nombre)
    base = BASE_PUBLICA if oyente == "publico" else BASE_LOCAL
    config = replace(config, gateway_public_url=GATEWAY_PUBLIC_URL)
    kwargs: dict[str, Any] = {}
    if modo == "varias":
        config = replace(config, instancias="varias", sondeo_s=0.02, gracia_espera_s=0.4,
                         barrera_s=3.0)
        kwargs = {"demo": None, "almacen": EstadoSupabase(
            "https://base.supabase.test", "clave-de-prueba", transport=BaseFalsa().transporte()
        )}
    # Con la consola y el panel prendidos, como en Vercel: sus rutas también
    # se clasifican (`test_cada_ruta_de_la_app_esta_clasificada`).
    mundo = armar_gateway(
        config, evaluador_espia()[0], merchant=merchant, integradores=registro_con_lectura(),
        consola=True, panel=True, **kwargs,
    )
    return Gateway(mundo.app, base, merchant, nombre)


def _cliente(gw: Gateway, clave: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {clave}"} if clave else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gw.app), base_url=gw.base, timeout=10, headers=headers,
    )


def _es_solo_lectura(r: httpx.Response, donde: Any) -> None:
    assert r.status_code == 403, (donde, r.status_code, r.text)
    if r.request.method != "HEAD":
        cuerpo_json = r.json()
        assert set(cuerpo_json) == {"error"}, donde
        assert cuerpo_json["error"]["codigo"] == "solo_lectura", donde
        assert set(cuerpo_json["error"]) == {"codigo", "mensaje"}, donde
        assert r.headers["content-type"].startswith("application/json"), donde
    assert r.headers["www-authenticate"] == 'Bearer error="insufficient_scope"', donde


async def _ids_de_compras(c: httpx.AsyncClient) -> list[str]:
    resp = await c.get("/v1/compras")
    assert resp.status_code == 200, resp.text
    return [v["compra_id"] for v in resp.json()["compras"]]


# -- lo que puede ------------------------------------------------------------------------


async def test_lectura_ve_lo_mismo_que_la_completa(gateway):
    async with (_cliente(gateway, CLAVE_ACME) as completa,
                _cliente(gateway, CLAVE_LECTURA) as lectura,
                _cliente(gateway, CLAVE_OTRO) as otro):
        compra = (await crear_compra(completa))["compra_id"]
        ajena = (await crear_compra(otro))["compra_id"]
        rutas = [
            "/v1/compras", "/v1/compras?limite=5", "/v1/compras?estado=creada",
            f"/v1/compras/{compra}",
            f"/v1/compras/{compra}/explicacion",
            "/v1/resumen",
            "/v1/comercios",
            "/v1/webhooks/entregas",
            # Lo de otro integrador: igual que con la completa (404), no se ve.
            f"/v1/compras/{ajena}",
            f"/v1/compras/{ajena}/explicacion",
            # Un id mal formado: el mismo error que con la completa.
            "/v1/compras/no-es-un-id",
        ]
        for ruta in rutas:
            r_completa, r_lectura = await completa.get(ruta), await lectura.get(ruta)
            assert r_lectura.status_code == r_completa.status_code, (gateway.nombre, ruta)
            assert r_lectura.json() == r_completa.json(), (gateway.nombre, ruta)
        assert await _ids_de_compras(lectura) == [compra]
        assert (await lectura.get(f"/v1/compras/{compra}")).status_code == 200
        r_ajena = await lectura.get(f"/v1/compras/{ajena}")
        assert r_ajena.status_code == 404
        assert r_ajena.json()["error"]["codigo"] == "compra_desconocida"
    assert gateway.merchant.recibidos == []


async def test_lectura_y_las_rutas_publicas_de_siempre(gateway):
    async with _cliente(gateway, CLAVE_LECTURA) as lectura, _cliente(gateway, None) as anonimo:
        for ruta in sorted(RUTAS_V1_PUBLICAS):
            r_lectura, r_anonimo = await lectura.get(ruta), await anonimo.get(ruta)
            assert r_lectura.status_code == r_anonimo.status_code == 200, ruta
            assert r_lectura.content == r_anonimo.content, ruta


# -- lo que no puede ---------------------------------------------------------------------


def _lo_que_no_puede(compra: str) -> list[tuple[str, str, Any]]:
    return [
        ("POST", "/v1/compras", {"pedido": "zapatillas para correr", "ficha": FICHA}),
        ("POST", f"/v1/compras/{compra}/respuesta", {"porque": "para correr"}),
        ("POST", f"/v1/compras/{compra}/decision",
         {"oportunidad_id": "opo-1", "decision": "aceptar"}),
        ("POST", f"/v1/compras/{compra}/aprobaciones",
         {"cart_id": "cart_1", "contexto_id": "ctx-1"}),
        ("POST", "/v1/evaluar", {"ficha": FICHA, "checkout": {}}),
        ("POST", "/v1/filtrar", {"contenido": "hola", "origen": "otro"}),
        ("POST", "/v1/webhooks/prueba", {}),
        ("POST", f"/v1/compras/{compra}/explicacion", {}),
        ("POST", "/v1/resumen", {}),
        ("PUT", f"/v1/compras/{compra}", {}),
        ("PATCH", f"/v1/compras/{compra}", {}),
        ("DELETE", f"/v1/compras/{compra}", None),
        ("HEAD", "/v1/compras", None),
        ("OPTIONS", "/v1/compras", None),
        # GET fuera de la lista blanca: tampoco (aunque la ruta exista con POST).
        ("GET", f"/v1/compras/{compra}/respuesta", None),
        ("GET", f"/v1/compras/{compra}/aprobaciones", None),
        ("GET", "/v1/evaluar", None),
        ("GET", "/v1/webhooks/prueba", None),
        ("GET", "/v1/nada", None),
        ("GET", "/v1", None),
        ("GET", "/v1/", None),
        ("GET", "/v1/compras/", None),
        ("GET", f"/v1/compras/{compra}/explicacion/otra", None),
        ("GET", f"/v1/compras/{compra}/eventos", None),
    ]


async def test_lectura_no_puede_escribir_ni_salir_de_la_lista(gateway):
    async with _cliente(gateway, CLAVE_ACME) as completa, _cliente(gateway, CLAVE_LECTURA) as lectura:
        compra = (await crear_compra(completa))["compra_id"]
        antes = (await completa.get(f"/v1/compras/{compra}")).json()
        for metodo, ruta, body in _lo_que_no_puede(compra):
            r = await lectura.request(metodo, ruta, json=body)
            _es_solo_lectura(r, (gateway.nombre, metodo, ruta))
        # Nada cambió: ni compras nuevas ni la compra de antes.
        assert await _ids_de_compras(completa) == [compra]
        assert (await completa.get(f"/v1/compras/{compra}")).json() == antes
    assert gateway.merchant.recibidos == []


async def test_cada_ruta_de_v1_de_la_app_sigue_la_lista_blanca(gateway):
    """Recorre las rutas reales de la app: lo que está en `RUTAS_LECTURA` (o es
    público) responde como a la clave completa; todo lo demás, también una
    ruta nueva que nadie sumó a la lista, es 403 para la de lectura."""
    lista = set(RUTAS_LECTURA)
    probadas: dict[str, int] = {"permitidas": 0, "prohibidas": 0}
    async with _cliente(gateway, CLAVE_ACME) as completa, _cliente(gateway, CLAVE_LECTURA) as lectura:
        compra = (await crear_compra(completa))["compra_id"]
        for ruta in gateway.app.routes:
            patron = getattr(ruta, "path", "")
            if not patron.startswith("/v1/") or ":path}" in patron:
                continue
            concreta = re.sub(r"\{[^}]+\}", "x", patron.replace("{compra_id}", compra))
            for metodo in sorted(getattr(ruta, "methods", None) or ()):
                body = {} if metodo in {"POST", "PUT", "PATCH"} else None
                r = await lectura.request(metodo, concreta, json=body)
                if patron in RUTAS_V1_PUBLICAS:
                    assert r.status_code != 403, (metodo, patron)
                elif (metodo, patron) in lista:
                    r_completa = await completa.request(metodo, concreta)
                    assert r.status_code == r_completa.status_code != 403, (metodo, patron)
                    probadas["permitidas"] += 1
                else:
                    _es_solo_lectura(r, (gateway.nombre, metodo, patron))
                    probadas["prohibidas"] += 1
        assert await _ids_de_compras(completa) == [compra]
    # Las 4 GET de hoy (compras, una compra, comercios, entregas) y, como
    # mínimo, los 7 POST y los HEAD.
    assert probadas["permitidas"] >= 4 and probadas["prohibidas"] >= 11, probadas


async def test_cada_ruta_de_la_app_esta_clasificada(gateway):
    """Todas las rutas reales de la app, no solo las de `/v1`, caen en una
    clase conocida: una ruta nueva sin clasificar rompe este test. Las
    estáticas de la consola y del panel del cliente son iguales con la clave
    de lectura, sin clave o con la completa (la página es pública; los datos
    los pide el navegador a `/v1` con la clave)."""
    from proxy import consola, panel
    from proxy.config import RUTA_PURGA

    estaticas = consola.RUTAS_ABIERTAS | panel.RUTAS_ABIERTAS
    publico = gateway.base == BASE_PUBLICA or gateway.nombre.startswith("vercel")
    sin_clasificar: list[str] = []
    vistas: set[str] = set()
    async with (_cliente(gateway, CLAVE_LECTURA) as lectura, _cliente(gateway, None) as anonimo,
                _cliente(gateway, CLAVE_ACME) as completa):
        for ruta in gateway.app.routes:
            patron = getattr(ruta, "path", "")
            if patron in estaticas:
                for metodo in sorted(getattr(ruta, "methods", None) or ()):
                    respuestas = [await c.request(metodo, patron) for c in (lectura, anonimo, completa)]
                    codigos = {r.status_code for r in respuestas}
                    assert len(codigos) == 1 and codigos <= {200, 307}, (gateway.nombre, metodo, patron, codigos)
                    assert len({r.content for r in respuestas}) == 1, (gateway.nombre, metodo, patron)
                    assert all(
                        r.headers["content-security-policy"].startswith("default-src 'none'")
                        for r in respuestas
                    ), patron
                vistas.add(patron)
            elif patron == "/v1" or patron.startswith("/v1/"):
                continue  # test_cada_ruta_de_v1_de_la_app_sigue_la_lista_blanca
            elif patron.startswith("/a2a/"):
                continue  # test_lectura_no_sirve_para_el_trafico_a2a
            elif patron.startswith("/guard/"):
                # La API de control: en el público no existe; en el local la
                # protege ControlLocal (test_control.py).
                if publico:
                    r = await lectura.get(patron)
                    assert r.status_code == 404, (gateway.nombre, patron)
            elif patron == RUTA_PURGA:
                continue  # solo con su secreto (test_retencion.py)
            else:
                sin_clasificar.append(patron)
    assert sin_clasificar == [], (gateway.nombre, sin_clasificar)
    assert vistas == estaticas, (gateway.nombre, sorted(estaticas - vistas))
    assert {"/panel", "/panel/app.js", "/consola"} <= vistas


async def test_v1_clave_dice_de_quien_es_y_que_puede(gateway):
    """`GET /v1/clave`: el integrador, el `kid` y los permisos de la clave que
    autenticó (`[]` es una completa). Lo usa el panel para avisar si entraron
    con una completa. Nunca el secreto ni el sha256, y sin efectos."""
    async with (_cliente(gateway, CLAVE_ACME) as completa, _cliente(gateway, CLAVE_LECTURA) as lectura,
                _cliente(gateway, CLAVE_OTRO) as otro, _cliente(gateway, None) as anonimo):
        compra = (await crear_compra(completa))["compra_id"]
        antes = (await completa.get(f"/v1/compras/{compra}")).json()
        r_lectura = await lectura.get("/v1/clave")
        r_completa = await completa.get("/v1/clave")
        r_otro = await otro.get("/v1/clave")
        sin_clave = await anonimo.get("/v1/clave")
        despues = (await completa.get(f"/v1/compras/{compra}")).json()
        ids = await _ids_de_compras(completa)
    assert r_lectura.status_code == r_completa.status_code == r_otro.status_code == 200, gateway.nombre
    assert r_lectura.json() == {
        "integrador": {"id": "acme", "nombre": "ACME Compras"}, "kid": KID_LECTURA,
        "permisos": ["lectura"],
    }
    assert r_completa.json() == {
        "integrador": {"id": "acme", "nombre": "ACME Compras"}, "kid": KID_ACME, "permisos": [],
    }
    assert r_otro.json()["integrador"]["id"] == "otro" and r_otro.json()["permisos"] == []
    for r, clave, sha in ((r_lectura, CLAVE_LECTURA, SHA_LECTURA), (r_completa, CLAVE_ACME, SHA_ACME)):
        assert clave not in r.text and clave.rsplit("_", 1)[-1] not in r.text and sha not in r.text
    assert sin_clave.status_code == 401 and sin_clave.json()["error"]["codigo"] == "no_autenticado"
    # Sin efectos: ni compras nuevas, ni cambios, ni nada al comercio.
    assert ids == [compra] and despues == antes
    assert gateway.merchant.recibidos == []


async def test_v1_clave_con_una_clave_de_baja_o_mal_escrita_es_401(gateway):
    cambiada = CLAVE_LECTURA[:-1] + _otra_letra(CLAVE_LECTURA)
    for clave in (cambiada, CLAVE_LECTURA_BAJA):
        async with _cliente(gateway, clave) as c:
            r = await c.get("/v1/clave")
        assert r.status_code == 401 and r.json()["error"]["codigo"] == "clave_invalida", gateway.nombre


def test_la_lista_blanca_es_solo_get_en_v1():
    assert RUTAS_LECTURA, "la lista no puede estar vacía"
    for metodo, patron in RUTAS_LECTURA:
        assert metodo == "GET", patron
        assert patron.startswith("/v1/") and patron not in RUTAS_V1_PUBLICAS, patron
    pedidas = {
        "/v1/compras", "/v1/compras/{compra_id}", "/v1/compras/{compra_id}/explicacion",
        "/v1/resumen", "/v1/comercios", "/v1/webhooks/entregas", "/v1/clave",
    }
    assert {p for _, p in RUTAS_LECTURA} == pedidas


async def test_lectura_no_sirve_para_el_trafico_a2a(gateway):
    async with _cliente(gateway, CLAVE_ACME) as completa, _cliente(gateway, CLAVE_LECTURA) as lectura:
        compra = (await crear_compra(completa))["compra_id"]
        respuestas = [
            await buscar(lectura, compra),
            await buscar(lectura, compra, metodo="message/stream"),
            await buscar(lectura, compra, comercio="tienda_b"),
            await lectura.get("/a2a/m/merchant_1/.well-known/agent-card.json"),
            await lectura.get("/a2a/m/merchant_1/.well-known/agent.json"),
            # La ruta de la demo (anónima en el local): con una clave de
            # lectura tampoco pasa, ni como anónimo.
            await lectura.post("/a2a/merchant_agent", content=cuerpo(pedido_find_products("B")),
                               headers=HEADERS_A2A),
            await lectura.get("/a2a/merchant_agent/.well-known/agent-card.json"),
        ]
        for n, r in enumerate(respuestas):
            _es_solo_lectura(r, (gateway.nombre, n))
        assert gateway.merchant.recibidos == []
        vista = (await completa.get(f"/v1/compras/{compra}")).json()
        assert vista["estado"] == "creada" and vista["contextos"] == []

        # La forma es la del 401 de una clave inválida en esa misma ruta.
        async with _cliente(gateway, CLAVE_LECTURA[:-1] + _otra_letra(CLAVE_LECTURA)) as mala:
            invalida = await buscar(mala, compra)
        assert invalida.status_code == 401
        assert set(invalida.json()["error"]) == set(respuestas[0].json()["error"])

        # Con la clave completa, el mismo pedido sí pasa.
        ok = await buscar(completa, compra)
        assert ok.status_code == 200, ok.text
        assert gateway.merchant.recibidos


def _otra_letra(clave: str) -> str:
    """El último carácter cambiado por otro (con "Q" fijo, 1 de cada 64 claves
    ya terminaba en Q y la "inválida" era la válida)."""
    return "R" if clave.endswith("Q") else "Q"


async def test_lectura_mal_escrita_o_de_baja_es_401_no_403(gateway):
    """Primero se autentica: una clave de lectura que no vale es `clave_invalida`
    (401), igual que una completa; el 403 es solo para la que sí vale."""
    cambiada = CLAVE_LECTURA[:-1] + _otra_letra(CLAVE_LECTURA)
    for clave in (cambiada, CLAVE_LECTURA_BAJA):
        async with _cliente(gateway, clave) as c:
            for metodo, ruta in (("GET", "/v1/compras"), ("POST", "/v1/compras"),
                                 ("POST", "/a2a/m/merchant_1")):
                r = await c.request(metodo, ruta, json={})
                assert r.status_code == 401, (gateway.nombre, metodo, ruta)
                assert r.json()["error"]["codigo"] == "clave_invalida"


async def test_el_permiso_sale_de_la_clave_no_de_lo_que_manda_el_cliente(gateway):
    trucos = [
        {"X-Permisos": "completa"}, {"X-Solo-Lectura": "0"}, {"X-Alcance": "compras"},
        {"X-HTTP-Method-Override": "GET"}, {"X-Method-Override": "GET"},
        {HEADER_COMPRA: "cmp-0123456789abcdef"},
    ]
    async with _cliente(gateway, CLAVE_ACME) as completa, _cliente(gateway, CLAVE_LECTURA) as lectura:
        for extra in trucos:
            r = await lectura.post("/v1/compras", json={"pedido": "x", "ficha": FICHA}, headers=extra)
            _es_solo_lectura(r, (gateway.nombre, extra))
            r = await lectura.post("/v1/compras?permisos=completa&_method=GET",
                                   json={"pedido": "x", "ficha": FICHA}, headers=extra)
            _es_solo_lectura(r, (gateway.nombre, "query", extra))
        assert await _ids_de_compras(completa) == []
        # La completa con esos headers sigue siendo completa.
        r = await completa.post("/v1/compras", json={"pedido": "zapatillas", "ficha": FICHA},
                                headers={"X-Permisos": "lectura", "X-Solo-Lectura": "1"})
        assert r.status_code == 201, r.text
    # Las dos claves juntas no suman permisos: es ambiguo (401).
    async with _cliente(gateway, None) as c:
        r = await c.post("/v1/compras", json={"pedido": "x", "ficha": FICHA}, headers=[
            ("Authorization", f"Bearer {CLAVE_LECTURA}"), ("Authorization", f"Bearer {CLAVE_ACME}"),
        ])
        assert r.status_code == 401 and r.json()["error"]["codigo"] == "clave_invalida"


async def test_la_clave_de_lectura_no_se_loguea(gateway, caplog):
    with caplog.at_level("DEBUG"):
        async with _cliente(gateway, CLAVE_LECTURA) as lectura:
            await lectura.get("/v1/compras")
            await lectura.post("/v1/compras", json={})
            await lectura.post("/a2a/m/merchant_1", json={})
    secreto = CLAVE_LECTURA.split("_", 2)[2]
    assert all(secreto not in r.getMessage() for r in caplog.records)


# ===========================================================================
# CLI: python -m proxy.claves nueva --lectura
# ===========================================================================


def test_cli_nueva_lectura_imprime_una_clave_de_lectura(capsys):
    assert claves.main(["nueva", "--integrador", "acme", "--lectura"]) == 0
    salida = capsys.readouterr()
    clave, objeto_json = salida.out.strip().splitlines()
    objeto = json.loads(objeto_json)
    assert objeto["permisos"] == ["lectura"] and objeto["activa"] is True
    assert set(objeto) == {"kid", "sha256", "creada", "activa", "permisos"}
    assert clave not in salida.err and "lectura" in salida.err
    registro = RegistroIntegradores.desde_dict(_acme_con(objeto), {})
    assert registro.autenticar_clave(clave).solo_lectura is True


# ===========================================================================
# deploy/vercel/generar_secretos.py --clave-lectura
# ===========================================================================


def _gs():
    from proxy.tests.test_despliegue_vercel import _script

    return _script("generar_secretos")


def _base(carpeta, *extra: str) -> None:
    """Lo que ya generó el operador: el integrador acme y su clave completa."""
    assert _gs().main(["--carpeta", str(carpeta), "--integrador", "acme", "--sin-recibo", *extra]) == 0


def test_generar_secretos_clave_lectura_agrega_la_entrada(tmp_path, capsys):
    gs, carpeta = _gs(), tmp_path / "vercel"
    _base(carpeta)
    leer = lambda nombre: (carpeta / nombre).read_text(encoding="utf-8")  # noqa: E731
    antes = json.loads(leer("GATEWAY_CLAVES_JSON.json"))
    clave_completa = leer("clave_integrador_acme.txt")
    capsys.readouterr()

    assert gs.main(["--carpeta", str(carpeta), "--clave-lectura", "acme"]) == 0
    salida = capsys.readouterr()

    clave_lectura = leer("clave_lectura_acme.txt")
    assert clave_lectura.startswith("psg_") and "\n" not in clave_lectura
    despues = json.loads(leer("GATEWAY_CLAVES_JSON.json"))
    # Solo se sumó una clave al integrador: lo demás queda igual.
    (acme,) = despues["integradores"]
    nueva = acme["claves"][-1]
    assert acme["claves"][:-1] == antes["integradores"][0]["claves"]
    assert {k: v for k, v in acme.items() if k != "claves"} == {
        k: v for k, v in antes["integradores"][0].items() if k != "claves"
    }
    assert nueva["permisos"] == ["lectura"] and nueva["activa"] is True
    assert nueva["sha256"] == hash_clave(clave_lectura)
    # El JSON sigue en una línea (es el valor de la variable de Vercel).
    assert "\n" not in leer("GATEWAY_CLAVES_JSON.json")

    registro = RegistroIntegradores.desde_dict(despues, {})
    assert registro.autenticar_clave(clave_lectura).solo_lectura is True
    assert registro.autenticar_clave(clave_completa).solo_lectura is False
    # vercel.env queda coherente (lo usa local_multi.py --env).
    entorno = _script_local_multi().leer_env(carpeta / "vercel.env")
    assert json.loads(entorno["GATEWAY_CLAVES_JSON"]) == despues
    assert clave_lectura not in leer("vercel.env")  # la clave es del cliente

    impreso = salida.out + salida.err
    for secreto in (clave_lectura, clave_lectura.split("_", 2)[2], clave_completa):
        assert secreto not in impreso
    assert "clave_lectura_acme.txt" in impreso
    assert "GATEWAY_CLAVES_JSON" in impreso and "vercel deploy" in impreso


def _script_local_multi():
    from proxy.tests.test_despliegue_vercel import _script

    return _script("local_multi")


def test_generar_secretos_clave_lectura_con_webhook_y_otro_integrador(tmp_path, capsys):
    """El registro puede traer webhooks (su secreto no está en el entorno del
    script) y varios integradores: se toca solo el pedido."""
    gs, carpeta = _gs(), tmp_path / "vercel"
    _base(carpeta, "--webhook-url", "https://acme.example/hook")
    ruta = carpeta / "GATEWAY_CLAVES_JSON.json"
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    otro = {"id": "otro", "claves": [], "comercios": ["merchant_1"]}
    datos["integradores"].append(otro)
    ruta.write_text(json.dumps(datos), encoding="utf-8")

    assert gs.main(["--carpeta", str(carpeta), "--clave-lectura", "otro"]) == 0
    despues = json.loads(ruta.read_text(encoding="utf-8"))
    assert despues["integradores"][0] == datos["integradores"][0]
    (nueva,) = despues["integradores"][1]["claves"]
    assert nueva["permisos"] == ["lectura"]
    assert (carpeta / "clave_lectura_otro.txt").exists()
    capsys.readouterr()


def test_generar_secretos_clave_lectura_no_pisa_sin_forzar(tmp_path, capsys):
    gs, carpeta = _gs(), tmp_path / "vercel"
    _base(carpeta)
    assert gs.main(["--carpeta", str(carpeta), "--clave-lectura", "acme"]) == 0
    json_antes = (carpeta / "GATEWAY_CLAVES_JSON.json").read_bytes()
    clave_antes = (carpeta / "clave_lectura_acme.txt").read_bytes()
    assert gs.main(["--carpeta", str(carpeta), "--clave-lectura", "acme"]) == 1
    assert (carpeta / "GATEWAY_CLAVES_JSON.json").read_bytes() == json_antes
    assert (carpeta / "clave_lectura_acme.txt").read_bytes() == clave_antes
    # Con --forzar: otra clave de lectura (la anterior sigue en el JSON hasta
    # que el operador la dé de baja).
    assert gs.main(["--carpeta", str(carpeta), "--clave-lectura", "acme", "--forzar"]) == 0
    datos = json.loads((carpeta / "GATEWAY_CLAVES_JSON.json").read_text(encoding="utf-8"))
    lecturas = [c for c in datos["integradores"][0]["claves"] if c.get("permisos") == ["lectura"]]
    assert len(lecturas) == 2
    assert (carpeta / "clave_lectura_acme.txt").read_bytes() != clave_antes
    capsys.readouterr()


@pytest.mark.parametrize(
    ("args", "codigo"),
    [
        (["--clave-lectura", "nadie"], 2),                       # el integrador no está
        (["--clave-lectura", "No Vale"], 2),                     # id inválido
        (["--clave-lectura", "acme", "--dominio", "x.ngrok-free.app"], 2),
        (["--clave-lectura", "acme", "--webhook-url", "https://a.example/h"], 2),
        (["--clave-lectura", "acme", "--comercio", "tienda_b"], 2),
    ],
    ids=["integrador_inexistente", "id_invalido", "con_dominio", "con_webhook", "con_comercio"],
)
def test_generar_secretos_clave_lectura_rechaza_sin_tocar_nada(tmp_path, capsys, args, codigo):
    gs, carpeta = _gs(), tmp_path / "vercel"
    _base(carpeta)
    antes = {p.name: p.read_bytes() for p in carpeta.iterdir()}
    assert gs.main(["--carpeta", str(carpeta), *args]) == codigo
    assert {p.name: p.read_bytes() for p in carpeta.iterdir()} == antes
    capsys.readouterr()


def test_generar_secretos_clave_lectura_sin_registro_no_crea_nada(tmp_path, capsys):
    gs, carpeta = _gs(), tmp_path / "vercel"
    carpeta.mkdir()
    assert gs.main(["--carpeta", str(carpeta), "--clave-lectura", "acme"]) == 2
    assert list(carpeta.iterdir()) == []
    assert "GATEWAY_CLAVES_JSON.json" in capsys.readouterr().err


def test_generar_secretos_clave_lectura_con_registro_invalido(tmp_path, capsys):
    gs, carpeta = _gs(), tmp_path / "vercel"
    carpeta.mkdir()
    (carpeta / "GATEWAY_CLAVES_JSON.json").write_text("{no es json", encoding="utf-8")
    assert gs.main(["--carpeta", str(carpeta), "--clave-lectura", "acme"]) == 2
    assert sorted(p.name for p in carpeta.iterdir()) == ["GATEWAY_CLAVES_JSON.json"]
    capsys.readouterr()


# ===========================================================================
# docs
# ===========================================================================


def test_el_ejemplo_del_readme_trae_una_clave_completa_y_una_de_lectura():
    """El JSON de proxy/README.md ("Integradores") carga y muestra los dos tipos."""
    from proxy.tests.test_despliegue_vercel import RAIZ

    readme = (RAIZ / "proxy" / "README.md").read_text(encoding="utf-8")
    seccion = readme.split("### Integradores (`GATEWAY_CLAVES_ARCHIVO`)", 1)[1]
    bloque = seccion.split("```json", 1)[1].split("```", 1)[0]
    texto = bloque.replace("<64 hex>", "0" * 64).replace("<otros 64 hex>", "1" * 64)
    registro = RegistroIntegradores.desde_dict(
        json.loads(texto), {"WEBHOOK_SECRETO_ACME": SECRETO_WEBHOOK}
    )
    assert sorted(g.permisos is not None for g in registro._claves.values()) == [False, True]


def test_la_tabla_de_integracion_coincide_con_la_lista_blanca():
    """docs/INTEGRACION.md §4 lista exactamente lo que puede una clave de lectura."""
    from proxy.tests.test_despliegue_vercel import RAIZ

    doc = (RAIZ / "docs" / "INTEGRACION.md").read_text(encoding="utf-8")
    seccion = doc.split("### Claves de solo lectura", 1)[1].split("\n## ", 1)[0]
    permitidas = set(re.findall(r"^\| `(GET) (/v1/[^`]+)` \| Sí", seccion, re.MULTILINE))
    assert permitidas == set(RUTAS_LECTURA)
