"""Webhooks firmados (diseño del gateway, §1.6): firma, SSRF con IP fijada,
reintentos con backoff, cola con tope, registro de entregas y mapeo de eventos.

Sin red real ni sleeps reales: `httpx.MockTransport`, un resolver falso y un
`dormir` que solo anota cuánto habría esperado.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from contracts import Evento
from proxy.tipos_gateway import (
    HEADER_ENTREGA_WEBHOOK,
    HEADER_EVENTO_WEBHOOK,
    HEADER_FIRMA_WEBHOOK,
    ConfigWebhook,
    EntregaWebhook,
    Integrador,
)
from proxy.webhooks import (
    BACKOFF_S,
    MAX_INTENTOS,
    TIPOS_WEBHOOK,
    DestinoNoPermitido,
    Despachador,
    cuerpo_webhook,
    firmar,
    resolver_destino,
    tipo_webhook,
    validar_url_estatica,
    verificar_firma,
)

pytestmark = pytest.mark.anyio

SECRETO = b"whsec_" + b"x" * 40
URL = "https://hooks.acme.example/guard?v=1"
IP_PUBLICA = "93.184.216.34"
AHORA = 1_790_000_000.0
COMPRA = {"compra_id": "cmp-0123456789abcdef", "estado": "negociando"}


def integrador(id: str = "acme", url: str | None = URL) -> Integrador:
    webhook = ConfigWebhook(url=url, secreto=SECRETO) if url else None
    return Integrador(id=id, nombre=id.upper(), webhook=webhook)


class Receptor:
    """Backend del integrador: responde una lista de status (el último se repite)."""

    def __init__(self, *respuestas: int | type[Exception]) -> None:
        self.respuestas = list(respuestas) or [200]
        self.pedidos: list[httpx.Request] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        r = self.respuestas[min(len(self.pedidos) - 1, len(self.respuestas) - 1)]
        if isinstance(r, type):
            raise r("falla simulada", request=request)
        headers = {"Location": "https://otro.example/"} if 300 <= r < 400 else {}
        return httpx.Response(r, headers=headers, text="ok" * 5000)


class Reloj:
    """Reloj que avanza con cada `dormir`: el `t` de la firma cambia por intento."""

    def __init__(self) -> None:
        self.ahora = AHORA
        self.esperas: list[float] = []

    def __call__(self) -> float:
        return self.ahora

    async def dormir(self, segundos: float) -> None:
        self.esperas.append(segundos)
        self.ahora += segundos


async def resolver_publico(host: str, puerto: int) -> list[str]:
    return [IP_PUBLICA]


def despachador(
    receptor: Receptor,
    *,
    integradores: dict[str, Integrador] | None = None,
    reloj: Reloj | None = None,
    resolver: Any = resolver_publico,
    **kw: Any,
) -> Despachador:
    integradores = integradores if integradores is not None else {"acme": integrador()}
    reloj = reloj or Reloj()
    return Despachador(
        integradores.get,
        transport=httpx.MockTransport(receptor.handler),
        resolver=resolver,
        reloj=reloj,
        dormir=reloj.dormir,
        azar=lambda: 0.5,  # jitter neutro: factor 1,0
        **kw,
    )


def evento(tipo: str, **datos: Any) -> Evento:
    return Evento(
        id="ev-1", ts=datetime(2026, 10, 18, tzinfo=timezone.utc), contexto_id="ctx-1",
        guard_activo=True, tipo=tipo, datos=datos,
    )


# -- firma ---------------------------------------------------------------------


def test_firma_y_verificacion() -> None:
    cuerpo = b'{"a":1}'
    cabecera = firmar(SECRETO, cuerpo, 1000)
    assert cabecera.startswith("t=1000,v1=")
    assert verificar_firma(SECRETO, cuerpo, cabecera, ahora=1000)
    assert verificar_firma(SECRETO, cuerpo, cabecera, ahora=1300)
    assert verificar_firma(SECRETO, cuerpo, cabecera, ahora=700)


def test_firma_vieja_o_del_futuro_se_rechaza() -> None:
    cabecera = firmar(SECRETO, b"x", 1000)
    assert not verificar_firma(SECRETO, b"x", cabecera, ahora=1301)
    assert not verificar_firma(SECRETO, b"x", cabecera, ahora=699)
    assert verificar_firma(SECRETO, b"x", cabecera, ahora=1500, tolerancia=600)


def test_firma_alterada_se_rechaza() -> None:
    cabecera = firmar(SECRETO, b"cuerpo", 1000)
    assert not verificar_firma(SECRETO, b"cuerpo!", cabecera, ahora=1000)
    assert not verificar_firma(b"otro-secreto" * 4, b"cuerpo", cabecera, ahora=1000)
    # Cambiar `t` invalida la firma: `t` está firmado.
    t_cambiado = cabecera.replace("t=1000", "t=1001")
    assert not verificar_firma(SECRETO, b"cuerpo", t_cambiado, ahora=1000)
    for rara in ("", "t=1000", "v1=abc", "t=abc,v1=00", "basura", "t=1,t=2,v1=00", "t=-5,v1=00"):
        assert not verificar_firma(SECRETO, b"cuerpo", rara, ahora=1000)


def test_firma_acepta_varios_v1() -> None:
    cabecera = firmar(SECRETO, b"c", 1000)
    otra = "v1=" + "0" * 64
    assert verificar_firma(SECRETO, b"c", f"{cabecera},{otra}", ahora=1000)


def _verificar_de_la_doc() -> Any:
    """`verificar_webhook` tal como lo muestra docs/INTEGRACION.md §7."""
    doc = (Path(__file__).resolve().parents[2] / "docs" / "INTEGRACION.md").read_text("utf-8")
    [bloque] = [
        b.split("\n", 1)[1] for b in doc.split("```python")[1:]
        if "def verificar_webhook" in b.split("```", 1)[0]
    ]
    espacio: dict[str, Any] = {}
    exec(bloque.split("```", 1)[0], espacio)  # noqa: S102 - código de la doc, no de afuera
    return espacio["verificar_webhook"]


def test_el_ejemplo_de_la_doc_verifica_y_nunca_lanza() -> None:
    """El receptor copia ese ejemplo: tiene que aceptar lo que firma el
    gateway y rechazar (sin lanzar) una cabecera rara. Antes, `v1` con un
    carácter no ASCII o `t=²` hacían lanzar a `compare_digest` / `int`."""
    verificar = _verificar_de_la_doc()
    secreto = SECRETO.decode()
    cuerpo = cuerpo_webhook(
        entrega_id="whe-1", tipo="ping", integrador_id="acme",
        creado=datetime(2026, 10, 18, tzinfo=timezone.utc), compra=None,
    )
    ahora = int(time.time())
    assert verificar(secreto, cuerpo, firmar(SECRETO, cuerpo, ahora)) is True
    assert verificar(secreto, cuerpo + b" ", firmar(SECRETO, cuerpo, ahora)) is False
    assert verificar(secreto, cuerpo, firmar(SECRETO, cuerpo, ahora - 3600)) is False
    for rara in (f"t={ahora},v1=ñ", "t=²,v1=00", "v1=00", "", "t=,v1="):
        assert verificar(secreto, cuerpo, rara) is False, rara


def test_cuerpo_compacto_y_ordenado() -> None:
    cuerpo = cuerpo_webhook(
        entrega_id="whe-1", tipo="compra.veredicto", integrador_id="acme",
        creado=datetime(2026, 10, 18, tzinfo=timezone.utc), compra={"z": 1, "a": "ñ"},
    )
    assert cuerpo == (
        '{"compra":{"a":"ñ","z":1},"creado":"2026-10-18T00:00:00+00:00","datos":{},'
        '"id":"whe-1","integrador":"acme","tipo":"compra.veredicto"}'
    ).encode()


# -- mapeo de eventos ------------------------------------------------------------


@pytest.mark.parametrize(
    ("tipo", "datos", "esperado"),
    [
        ("pedido_detectado", {}, "compra.pregunta"),
        ("oportunidad", {}, "compra.oportunidad"),
        ("veredicto", {"veredicto": {}}, "compra.veredicto"),
        ("bloqueado", {"requiere_aprobacion": True}, "compra.requiere_aprobacion"),
        ("bloqueado", {"requiere_aprobacion": True, "etapa": "filtro"}, "compra.requiere_aprobacion"),
        ("bloqueado", {"etapa": "filtro"}, "compra.bloqueada"),
        ("bloqueado", {"sin_veredicto": True}, "compra.bloqueada"),
        ("bloqueado", {}, None),  # un bloqueo del Guard ya sale como compra.veredicto
        ("bloqueado", {"requiere_aprobacion": "true"}, None),
        ("bloqueado", {"sin_veredicto": 1}, None),
        ("reenviado", {}, None),
        ("ofertas_recibidas", {}, None),
        ("contexto_registrado", {}, None),
        ("oportunidad_decidida", {}, None),
        ("error", {"etapa": "x"}, None),
    ],
)
def test_tipo_webhook(tipo: str, datos: dict[str, Any], esperado: str | None) -> None:
    assert tipo_webhook(evento(tipo, **datos)) == esperado
    assert esperado is None or esperado in TIPOS_WEBHOOK


def test_tipos_webhook() -> None:
    assert set(TIPOS_WEBHOOK) == {
        "compra.pregunta", "compra.oportunidad", "compra.requiere_aprobacion",
        "compra.veredicto", "compra.bloqueada", "ping",
    }


# -- destino ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.acme.example/x",
        "ftp://hooks.acme.example/x",
        "https://user:pw@hooks.acme.example/x",
        "https://@hooks.acme.example/x",
        "https:///x",
        "https://hooks.acme.example:99999/x",
        "https://hooks acme.example/x",
        "",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/x",
        "https://localhost/x",
    ],
)
def test_url_estatica_invalida(url: str) -> None:
    with pytest.raises(DestinoNoPermitido):
        validar_url_estatica(url, permitir_inseguro=False)


def test_url_estatica_valida_y_modo_inseguro() -> None:
    validar_url_estatica(URL, permitir_inseguro=False)
    validar_url_estatica("https://8.8.8.8:8443/h", permitir_inseguro=False)
    validar_url_estatica("http://127.0.0.1:9000/h", permitir_inseguro=True)
    with pytest.raises(DestinoNoPermitido):
        validar_url_estatica("https://u:p@127.0.0.1/h", permitir_inseguro=True)


@pytest.mark.parametrize(
    "ips",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["169.254.169.254"],
        ["::1"],
        ["fc00::1"],
        ["::ffff:127.0.0.1"],
        ["192.168.0.10"],
        ["100.64.0.1"],
        ["224.0.0.1"],
        [IP_PUBLICA, "10.0.0.1"],  # basta una interna
        # Revisión adversarial: IPv4 embebidas que `is_global` aceptaba.
        ["64:ff9b::a9fe:a9fe"],     # NAT64 → 169.254.169.254 (metadata)
        ["64:ff9b::a00:1"],         # NAT64 → 10.0.0.1
        ["64:ff9b::7f00:1"],        # NAT64 → 127.0.0.1
        ["::7f00:1"],               # IPv4-compatible
        ["::5db8:d822"],            # IPv4-compatible, aunque la de adentro sea pública
        ["::ffff:0:7f00:1"],        # IPv4-traducida
        ["2002:7f00:1::1"],         # 6to4 → 127.0.0.1
        ["fec0::1"],                # site-local (obsoleta): `is_global` la acepta
        ["feff::1"],                # el final de fec0::/10
    ],
)
async def test_resolver_destino_rechaza_ips_no_globales(ips: list[str]) -> None:
    async def resolver(host: str, puerto: int) -> list[str]:
        return ips

    with pytest.raises(DestinoNoPermitido):
        await resolver_destino(URL, permitir_inseguro=False, resolver=resolver)


async def test_resolver_destino_publico() -> None:
    llamadas: list[tuple[str, int]] = []

    async def resolver(host: str, puerto: int) -> list[str]:
        llamadas.append((host, puerto))
        return [IP_PUBLICA, IP_PUBLICA, "2606:4700::1111"]

    host, puerto, ips = await resolver_destino(URL, permitir_inseguro=False, resolver=resolver)
    assert (host, puerto) == ("hooks.acme.example", 443)
    assert ips == [IP_PUBLICA, "2606:4700::1111"]
    assert llamadas == [("hooks.acme.example", 443)]


async def test_resolver_destino_inseguro_no_resuelve() -> None:
    async def resolver(host: str, puerto: int) -> list[str]:
        raise AssertionError("no debería resolver")

    assert await resolver_destino(
        "http://127.0.0.1:9000/h", permitir_inseguro=True, resolver=resolver
    ) == ("127.0.0.1", 9000, [])


async def test_url_http_se_rechaza_salvo_inseguro() -> None:
    receptor = Receptor(200)
    d = despachador(receptor, integradores={"acme": integrador(url="http://hooks.acme.example/h")})
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["error"], e["intentos"]) == ("descartada", "destino_no_permitido", 1)
    assert receptor.pedidos == []
    await d.cerrar()

    receptor = Receptor(200)
    d = despachador(
        receptor, integradores={"acme": integrador(url="http://127.0.0.1:9000/h")},
        permitir_inseguro=True,
    )
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    assert d.entregas("acme")[0]["estado"] == "entregada"
    [pedido] = receptor.pedidos
    assert str(pedido.url) == "http://127.0.0.1:9000/h"
    assert "sni_hostname" not in pedido.extensions
    await d.cerrar()


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.7", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1"]
)
async def test_destino_interno_se_descarta_sin_reintento(ip: str) -> None:
    async def resolver(host: str, puerto: int) -> list[str]:
        return [ip]

    receptor = Receptor(200)
    reloj = Reloj()
    d = despachador(receptor, resolver=resolver, reloj=reloj)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["error"], e["intentos"]) == ("descartada", "destino_no_permitido", 1)
    assert receptor.pedidos == [] and reloj.esperas == []
    await d.cerrar()


async def test_resolucion_en_cada_intento_contra_rebinding() -> None:
    """Primero resuelve a una pública (500), después a la metadata: se corta."""
    respuestas = iter([[IP_PUBLICA], ["169.254.169.254"]])

    async def resolver(host: str, puerto: int) -> list[str]:
        return next(respuestas)

    receptor = Receptor(500)
    d = despachador(receptor, resolver=resolver)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["error"], e["intentos"]) == ("descartada", "destino_no_permitido", 2)
    assert len(receptor.pedidos) == 1
    await d.cerrar()


# -- entrega ---------------------------------------------------------------------


async def test_pedido_sale_a_la_ip_fijada_con_host_y_sni() -> None:
    receptor = Receptor(204)
    d = despachador(receptor)
    entrega_id = d.notificar("acme", "compra.oportunidad", COMPRA, {"extra": 1})
    await d.esperar()
    [pedido] = receptor.pedidos
    assert pedido.method == "POST"
    assert str(pedido.url) == f"https://{IP_PUBLICA}/guard?v=1"  # httpx omite el :443
    assert pedido.headers["host"] == "hooks.acme.example"
    assert pedido.extensions["sni_hostname"] == "hooks.acme.example"
    assert pedido.headers["content-type"] == "application/json"
    assert pedido.headers["user-agent"] == "KaiAI-Webhooks/1"
    assert pedido.headers[HEADER_EVENTO_WEBHOOK] == "compra.oportunidad"
    assert pedido.headers[HEADER_ENTREGA_WEBHOOK] == entrega_id
    assert entrega_id is not None and entrega_id.startswith("whe-") and len(entrega_id) == 36

    cuerpo = json.loads(pedido.content)
    assert cuerpo["id"] == entrega_id
    assert cuerpo["tipo"] == "compra.oportunidad"
    assert cuerpo["integrador"] == "acme"
    assert cuerpo["compra"] == COMPRA
    assert cuerpo["datos"] == {"extra": 1}
    assert verificar_firma(
        SECRETO, pedido.content, pedido.headers[HEADER_FIRMA_WEBHOOK], ahora=int(AHORA)
    )
    [e] = d.entregas("acme")
    assert e["estado"] == "entregada" and e["status_http"] == 204 and e["intentos"] == 1
    assert e["destino_host"] == "hooks.acme.example"
    assert e["compra_id"] == "cmp-0123456789abcdef"
    await d.cerrar()


async def test_ipv6_y_puerto_explicito() -> None:
    async def resolver(host: str, puerto: int) -> list[str]:
        return ["2606:4700::1111"]

    receptor = Receptor(200)
    d = despachador(
        receptor, resolver=resolver,
        integradores={"acme": integrador(url="https://hooks.acme.example:8443/h")},
    )
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [pedido] = receptor.pedidos
    assert str(pedido.url) == "https://[2606:4700::1111]:8443/h"
    assert pedido.headers["host"] == "hooks.acme.example:8443"
    await d.cerrar()


async def test_500_500_200_reintenta_con_backoff() -> None:
    receptor = Receptor(500, 503, 200)
    reloj = Reloj()
    d = despachador(receptor, reloj=reloj)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["intentos"], e["status_http"], e["error"]) == ("entregada", 3, 200, None)
    assert reloj.esperas == [1.0, 4.0]

    # Misma entrega, mismo cuerpo; la firma cambia porque cambia `t`.
    ids = {p.headers[HEADER_ENTREGA_WEBHOOK] for p in receptor.pedidos}
    firmas = [p.headers[HEADER_FIRMA_WEBHOOK] for p in receptor.pedidos]
    assert ids == {e["id"]}
    assert len(set(firmas)) == 3
    assert {p.content for p in receptor.pedidos} == {receptor.pedidos[0].content}
    assert [f.split(",")[0] for f in firmas] == [
        f"t={int(AHORA)}", f"t={int(AHORA) + 1}", f"t={int(AHORA) + 5}"
    ]
    for p in receptor.pedidos:
        t = int(p.headers[HEADER_FIRMA_WEBHOOK].split(",")[0][2:])
        assert verificar_firma(SECRETO, p.content, p.headers[HEADER_FIRMA_WEBHOOK], ahora=t)
    await d.cerrar()


async def test_jitter_dentro_de_rango() -> None:
    for azar, factor in ((0.0, 0.8), (1.0, 1.2)):
        reloj = Reloj()
        d = despachador(Receptor(500, 200), reloj=reloj)
        d._azar = lambda azar=azar: azar  # noqa: SLF001 (prueba del rango del jitter)
        d.notificar("acme", "compra.veredicto", COMPRA)
        await d.esperar()
        assert reloj.esperas == [pytest.approx(1.0 * factor)]
        await d.cerrar()


async def test_agota_los_intentos() -> None:
    receptor = Receptor(502)
    reloj = Reloj()
    d = despachador(receptor, reloj=reloj)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["error"], e["intentos"], e["status_http"]) == (
        "fallida", "http_5xx", MAX_INTENTOS, 502
    )
    assert reloj.esperas == [float(s) for s in BACKOFF_S]
    await d.cerrar()


@pytest.mark.parametrize("status", [408, 429])
async def test_408_y_429_se_reintentan(status: int) -> None:
    receptor = Receptor(status, 200)
    d = despachador(receptor)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["intentos"]) == ("entregada", 2)
    await d.cerrar()


async def test_400_falla_en_un_intento() -> None:
    receptor = Receptor(400)
    reloj = Reloj()
    d = despachador(receptor, reloj=reloj)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["error"], e["intentos"], e["status_http"]) == (
        "fallida", "http_4xx", 1, 400
    )
    assert reloj.esperas == []
    await d.cerrar()


async def test_302_no_se_sigue() -> None:
    receptor = Receptor(302)
    d = despachador(receptor)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["error"], e["intentos"], e["status_http"]) == (
        "fallida", "redireccion", 1, 302
    )
    assert len(receptor.pedidos) == 1  # no fue a la Location
    await d.cerrar()


async def test_timeout_reintenta() -> None:
    receptor = Receptor(httpx.ReadTimeout, httpx.ConnectTimeout, 200)
    reloj = Reloj()
    d = despachador(receptor, reloj=reloj)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [e] = d.entregas("acme")
    assert (e["estado"], e["intentos"]) == ("entregada", 3)
    assert reloj.esperas == [1.0, 4.0]
    await d.cerrar()


async def test_timeout_y_red_quedan_registrados_al_agotar() -> None:
    d = despachador(Receptor(httpx.ReadTimeout), max_intentos=2)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    assert d.entregas("acme")[0]["error"] == "timeout"
    await d.cerrar()

    d = despachador(Receptor(httpx.ConnectError), max_intentos=2)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    e = d.entregas("acme")[0]
    assert (e["estado"], e["error"], e["intentos"]) == ("fallida", "red", 2)
    await d.cerrar()


async def test_dns_caido_es_red_y_se_reintenta() -> None:
    llamadas = 0

    async def resolver(host: str, puerto: int) -> list[str]:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 1:
            raise OSError("sin DNS")
        return [IP_PUBLICA]

    d = despachador(Receptor(200), resolver=resolver)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    assert (d.entregas("acme")[0]["estado"], d.entregas("acme")[0]["intentos"]) == ("entregada", 2)
    await d.cerrar()


async def test_nat64_con_una_ipv4_publica_adentro_se_permite() -> None:
    async def resolver(host: str, puerto: int) -> list[str]:
        return ["64:ff9b::5db8:d822"]  # 93.184.216.34

    _, _, ips = await resolver_destino(
        "https://hooks.acme.example/x", permitir_inseguro=False, resolver=resolver
    )
    assert ips == ["64:ff9b::5db8:d822"]


async def test_cliente_sin_keep_alive() -> None:
    """Revisión adversarial: el pool de httpcore reutiliza conexiones por
    (esquema, IP, puerto) sin mirar el SNI; con keep-alive, el webhook de un
    integrador podía salir por la conexión TLS abierta con el host de otro
    que resuelve a la misma IP."""
    d = Despachador({"acme": integrador()}.get, resolver=resolver_publico)
    pool = d._client._transport._pool  # noqa: SLF001
    assert pool._max_keepalive_connections == 0  # noqa: SLF001
    await d.cerrar()


async def test_cliente_propio_sin_entorno_ni_redirecciones() -> None:
    d = despachador(Receptor(200))
    assert d._client.follow_redirects is False  # noqa: SLF001
    assert d._client._trust_env is False  # noqa: SLF001
    assert d._client.timeout.read == 10.0  # noqa: SLF001
    await d.cerrar()


# -- cola, cierre y registro -------------------------------------------------------


async def test_sin_webhook_no_programa_nada() -> None:
    receptor = Receptor(200)
    d = despachador(
        receptor, integradores={"acme": integrador(), "sin": integrador("sin", url=None)}
    )
    assert d.notificar("sin", "compra.veredicto", COMPRA) is None
    assert d.notificar("desconocido", "compra.veredicto", COMPRA) is None
    await d.esperar()
    assert d.entregas("sin") == [] and receptor.pedidos == []
    await d.cerrar()


async def test_notificar_nunca_lanza() -> None:
    def roto(_id: str) -> Integrador | None:
        raise RuntimeError("registro roto")

    d = Despachador(roto, transport=httpx.MockTransport(Receptor().handler))
    assert d.notificar("acme", "compra.veredicto", COMPRA) is None
    await d.cerrar()


async def test_cola_llena() -> None:
    liberar = asyncio.Event()

    async def lento(request: httpx.Request) -> httpx.Response:
        await liberar.wait()
        return httpx.Response(200)

    d = Despachador(
        {"acme": integrador()}.get, transport=httpx.MockTransport(lento),
        resolver=resolver_publico, max_pendientes=2,
    )
    a = d.notificar("acme", "compra.veredicto", COMPRA)
    b = d.notificar("acme", "compra.veredicto", COMPRA)
    c = d.notificar("acme", "compra.veredicto", COMPRA)
    assert None not in (a, b, c)
    por_id = {e["id"]: e for e in d.entregas("acme")}
    assert (por_id[c]["estado"], por_id[c]["error"]) == ("descartada", "cola_llena")
    assert por_id[a]["estado"] == "pendiente"
    liberar.set()
    await d.esperar()
    por_id = {e["id"]: e for e in d.entregas("acme")}
    assert por_id[a]["estado"] == por_id[b]["estado"] == "entregada"
    await d.cerrar()


class Goteo(httpx.AsyncByteStream):
    """Un cuerpo que nunca termina: un byte cada tanto (slowloris)."""

    async def __aiter__(self):
        while True:
            await asyncio.sleep(0.01)
            yield b"x"


def receptor_que_gotea(status: int):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=Goteo())

    return handler


async def _sin_colgarse(d: Despachador) -> None:
    """`d.esperar()` con un tope: antes del arreglo, estos tests no terminaban."""
    await asyncio.wait_for(d.esperar(), 3)


async def test_un_receptor_que_no_contesta_no_retiene_el_intento() -> None:
    """Los timeouts de httpx son por operación: un receptor que manda un byte
    cada 9 s retenía la entrega horas (y su lugar en la cola, que es de todos).
    Cada intento tiene un tope total (`timeout_s`)."""
    nunca = asyncio.Event()

    async def colgado(request: httpx.Request) -> httpx.Response:
        await nunca.wait()
        return httpx.Response(200)

    d = Despachador(
        {"acme": integrador()}.get, transport=httpx.MockTransport(colgado),
        resolver=resolver_publico, timeout_s=0.05, max_intentos=1,
    )
    d.notificar("acme", "compra.veredicto", COMPRA)
    await _sin_colgarse(d)
    [e] = d.entregas("acme")
    assert (e["estado"], e["error"]) == ("fallida", "timeout")
    # El ping de /v1/webhooks/prueba tampoco se cuelga.
    ping = await asyncio.wait_for(d.probar("acme"), 3)
    assert (ping["estado"], ping["error"]) == ("fallida", "timeout")
    await d.cerrar()


async def test_un_2xx_con_cuerpo_que_gotea_se_entrega_sin_leerlo() -> None:
    d = Despachador(
        {"acme": integrador()}.get, transport=httpx.MockTransport(receptor_que_gotea(200)),
        resolver=resolver_publico, timeout_s=0.2, max_intentos=1,
    )
    d.notificar("acme", "compra.veredicto", COMPRA)
    await _sin_colgarse(d)
    [e] = d.entregas("acme")
    assert (e["estado"], e["status_http"]) == ("entregada", 200)
    await d.cerrar()


async def test_un_error_con_cuerpo_que_gotea_vale_por_su_status() -> None:
    """El cuerpo de un error se lee solo para el log: si el tope corta esa
    lectura, cuenta el status que ya llegó (un 400 no se vuelve reintentable)."""
    for status, error, estado in ((500, "http_5xx", "fallida"), (400, "http_4xx", "fallida")):
        d = Despachador(
            {"acme": integrador()}.get,
            transport=httpx.MockTransport(receptor_que_gotea(status)),
            resolver=resolver_publico, timeout_s=0.05, max_intentos=1,
        )
        d.notificar("acme", "compra.veredicto", COMPRA)
        await _sin_colgarse(d)
        [e] = d.entregas("acme")
        assert (e["estado"], e["error"], e["status_http"]) == (estado, error, status)
        await d.cerrar()


async def test_un_integrador_no_llena_la_cola_de_los_demas() -> None:
    """La cola (1000 entregas) es de todos: un integrador cuyo webhook no
    contesta (o falla y se reintenta) la llenaba y los webhooks de los demás
    se descartaban (`cola_llena`). Cada uno tiene su parte."""
    nunca = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "malo.example":
            await nunca.wait()
        return httpx.Response(200)

    integradores = {
        "malo": integrador("malo", "https://malo.example/hook"),
        "acme": integrador(),
    }
    d = Despachador(integradores.get, transport=httpx.MockTransport(handler),
                    resolver=resolver_publico)
    for _ in range(1000):
        d.notificar("malo", "compra.veredicto", COMPRA)
    estados = [e["estado"] for e in d.entregas("malo", 500)]
    assert estados.count("pendiente") < 1000
    d.notificar("acme", "compra.veredicto", COMPRA)
    for _ in range(100):
        if d.entregas("acme")[0]["estado"] != "pendiente":
            break
        await asyncio.sleep(0.01)
    [e] = d.entregas("acme")
    assert (e["estado"], e["error"]) == ("entregada", None)
    await d.cerrar()


async def test_cerrar_cancela_lo_pendiente() -> None:
    nunca = asyncio.Event()
    empezo = asyncio.Event()

    async def colgado(request: httpx.Request) -> httpx.Response:
        empezo.set()
        await nunca.wait()
        return httpx.Response(200)

    d = Despachador(
        {"acme": integrador()}.get, transport=httpx.MockTransport(colgado),
        resolver=resolver_publico,
    )
    en_curso = d.notificar("acme", "compra.veredicto", COMPRA)
    await empezo.wait()
    sin_empezar = d.notificar("acme", "compra.veredicto", COMPRA)
    await d.cerrar(timeout_s=1)
    por_id = {e["id"]: e for e in d.entregas("acme")}
    for entrega_id in (en_curso, sin_empezar):
        assert (por_id[entrega_id]["estado"], por_id[entrega_id]["error"]) == (
            "descartada", "cerrado"
        )
    # Después de cerrar, lo nuevo también se descarta.
    tarde = d.notificar("acme", "compra.veredicto", COMPRA)
    assert {e["id"]: e for e in d.entregas("acme")}[tarde]["error"] == "cerrado"


def test_notificar_sin_loop() -> None:
    receptor = Receptor(200)
    d = Despachador(
        {"acme": integrador()}.get, transport=httpx.MockTransport(receptor.handler)
    )
    entrega_id = d.notificar("acme", "compra.veredicto", COMPRA)
    [e] = d.entregas("acme")
    assert e["id"] == entrega_id
    assert (e["estado"], e["error"], e["intentos"]) == ("descartada", "sin_loop", 0)
    assert receptor.pedidos == []


async def test_entregas_filtra_por_integrador_y_ordena() -> None:
    integradores = {"acme": integrador("acme"), "beta": integrador("beta")}
    d = despachador(Receptor(200), integradores=integradores)
    a1 = d.notificar("acme", "compra.pregunta", COMPRA)
    b1 = d.notificar("beta", "compra.veredicto", None)
    a2 = d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    assert [e["id"] for e in d.entregas("acme")] == [a2, a1]
    assert [e["id"] for e in d.entregas("beta")] == [b1]
    assert [e["id"] for e in d.entregas("acme", limite=1)] == [a2]
    assert d.entregas("nadie") == []
    assert d.entregas("beta")[0]["compra_id"] is None
    await d.cerrar()


async def test_registro_guarda_las_ultimas_500() -> None:
    d = despachador(Receptor(200))
    ids = [d.notificar("acme", "compra.veredicto", COMPRA) for _ in range(505)]
    await d.esperar()
    entregas = d.entregas("acme", limite=10_000)
    assert len(entregas) == 500
    assert entregas[0]["id"] == ids[-1] and entregas[-1]["id"] == ids[5]
    await d.cerrar()


async def test_probar_manda_un_ping_en_un_intento() -> None:
    receptor = Receptor(500)
    reloj = Reloj()
    d = despachador(
        receptor, reloj=reloj,
        integradores={"acme": integrador(), "sin": integrador("sin", url=None)},
    )
    resultado = await d.probar("acme")
    assert (resultado["tipo"], resultado["estado"], resultado["intentos"]) == ("ping", "fallida", 1)
    assert resultado["error"] == "http_5xx" and reloj.esperas == []
    [pedido] = receptor.pedidos
    assert pedido.headers[HEADER_EVENTO_WEBHOOK] == "ping"
    cuerpo = json.loads(pedido.content)
    assert cuerpo["tipo"] == "ping" and cuerpo["compra"] is None
    assert d.entregas("acme")[0]["id"] == resultado["id"]

    receptor.respuestas = [200]
    assert (await d.probar("acme"))["estado"] == "entregada"

    with pytest.raises(LookupError):
        await d.probar("sin")
    with pytest.raises(LookupError):
        await d.probar("desconocido")
    await d.cerrar()


# -- almacén -----------------------------------------------------------------------


class AlmacenEspia:
    nombre = "memoria"
    durable = False

    def __init__(self, falla: bool = False) -> None:
        self.falla = falla
        self.entregas: list[EntregaWebhook] = []

    async def guardar_entrega(self, entrega: EntregaWebhook) -> None:
        if self.falla:
            raise RuntimeError("almacén caído")
        self.entregas.append(entrega)


async def test_guarda_la_entrega_en_el_almacen() -> None:
    almacen = AlmacenEspia()
    d = despachador(Receptor(500, 200), almacen=almacen)
    entrega_id = d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    [guardada] = almacen.entregas
    assert guardada.id == entrega_id
    assert (guardada.estado, guardada.intentos, guardada.status_http) == ("entregada", 2, 200)
    assert guardada.actualizada is not None
    await d.cerrar()


async def test_almacen_que_falla_no_rompe_la_entrega() -> None:
    almacen = AlmacenEspia(falla=True)
    receptor = Receptor(200)
    d = despachador(receptor, almacen=almacen)
    d.notificar("acme", "compra.veredicto", COMPRA)
    await d.esperar()
    assert d.entregas("acme")[0]["estado"] == "entregada"
    assert len(receptor.pedidos) == 1
    await d.cerrar()
