"""El almacén para el historial (panel del cliente): el listado con filtros y
cursor, las lecturas de varias compras en una sola consulta por tabla y la
purga por antigüedad. El mismo contrato en `EstadoMemoria` y en
`EstadoSupabase` sobre el PostgREST falso (`postgrest_falso.BaseFalsa`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contracts import Evento
from proxy.estado import (
    EstadoMemoria,
    EstadoSupabase,
    fila_evento,
    fila_ofertas,
    fila_ultimo_veredicto,
)
from proxy.tests.postgrest_falso import BaseFalsa
from proxy.tests.test_estado import fila_apr, fila_compra
from proxy.tipos_gateway import EntregaWebhook, ErrorAlmacen

pytestmark = pytest.mark.anyio

AHORA = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
URL = "https://base.supabase.test"


@pytest.fixture(params=["memoria", "supabase"])
def almacen_y_base(request):
    if request.param == "memoria":
        return EstadoMemoria(durable=True), None
    base = BaseFalsa()
    return EstadoSupabase(URL, "clave", transport=base.transporte()), base


@pytest.fixture
def almacen(almacen_y_base):
    return almacen_y_base[0]


def cmp(n: int) -> str:
    return f"cmp-{n:016x}"


async def _cargar(almacen, n: int = 12, *, integrador: str = "acme") -> list[str]:
    """`n` compras, una por hora hacia atrás desde AHORA (la 0 es la más nueva),
    con estado y referencia que cambian."""
    ids = []
    for i in range(n):
        fila = fila_compra(cmp(i + (1000 if integrador != "acme" else 0)), integrador=integrador,
                           estado=("negociando", "aceptada", "bloqueada")[i % 3],
                           creada=AHORA - timedelta(hours=i))
        fila["referencia"] = f"ord-{i % 4}"
        await almacen.guardar_compra(fila)
        ids.append(fila["id"])
    return ids


async def test_el_listado_de_siempre_no_cambia(almacen):
    ids = await _cargar(almacen)
    assert [f["id"] for f in await almacen.listar_compras("acme", limite=5)] == ids[:5]
    assert [f["id"] for f in await almacen.listar_compras("acme", estado="aceptada")] == ids[1::3]


async def test_filtros_por_fecha_referencia_y_varios_estados(almacen):
    ids = await _cargar(almacen)
    await _cargar(almacen, 3, integrador="otro")
    desde, hasta = AHORA - timedelta(hours=5), AHORA - timedelta(hours=1)
    filas = await almacen.listar_compras("acme", desde=desde, hasta=hasta)
    # desde incluido, hasta excluido.
    assert [f["id"] for f in filas] == ids[2:6]
    filas = await almacen.listar_compras("acme", referencia="ord-1")
    assert [f["id"] for f in filas] == [ids[1], ids[5], ids[9]]
    filas = await almacen.listar_compras("acme", estado=("negociando", "bloqueada"))
    assert [f["id"] for f in filas] == [i for n, i in enumerate(ids) if n % 3 != 1]


async def test_el_cursor_sigue_donde_quedo(almacen):
    ids = await _cargar(almacen)
    vistas: list[str] = []
    cursor = None
    for _ in range(10):
        filas = await almacen.listar_compras("acme", limite=5, antes_de=cursor)
        if not filas:
            break
        vistas += [f["id"] for f in filas]
        ultima = filas[-1]
        cursor = (ultima["creada"], ultima["id"])
    assert vistas == ids


async def test_el_cursor_desempata_por_id_a_igual_fecha(almacen):
    for i in range(6):
        await almacen.guardar_compra(fila_compra(cmp(i), creada=AHORA))
    primera = await almacen.listar_compras("acme", limite=4)
    assert [f["id"] for f in primera] == [cmp(5), cmp(4), cmp(3), cmp(2)]
    segunda = await almacen.listar_compras("acme", limite=4, antes_de=(AHORA, cmp(2)))
    assert [f["id"] for f in segunda] == [cmp(1), cmp(0)]


@pytest.mark.parametrize("filtro", [
    {"antes_de": ("ayer", cmp(1))},
    {"antes_de": (AHORA, "cmp-1&x")},
    {"antes_de": "cmp-1"},
    {"referencia": 3},
    {"referencia": "x" * 201},
    {"estado": ("negociando", "inventado")},
    {"estado": ()},
    {"desde": "ayer"},
])
async def test_filtros_invalidos_no_llegan_a_la_base(almacen_y_base, filtro):
    almacen, base = almacen_y_base
    pedidos = len(base.pedidos) if base else 0
    with pytest.raises(ErrorAlmacen):
        await almacen.listar_compras("acme", **filtro)
    if base is not None:
        assert len(base.pedidos) == pedidos


async def test_una_referencia_con_signos_se_filtra_tal_cual(almacen):
    rara = 'ord,1 (x) "y".z:w'
    fila = fila_compra(cmp(1))
    fila["referencia"] = rara
    await almacen.guardar_compra(fila)
    await almacen.guardar_compra(fila_compra(cmp(2)))
    assert [f["id"] for f in await almacen.listar_compras("acme", referencia=rara)] == [cmp(1)]


# -- varias compras en una consulta ------------------------------------------------------


async def test_contextos_y_aprobaciones_de_varias_compras(almacen_y_base):
    almacen, base = almacen_y_base
    for i in range(1, 4):
        await almacen.guardar_compra(fila_compra(cmp(i)))
    await almacen.atar_contexto("ctx-a", cmp(1), "merchant_1")
    await almacen.atar_contexto("ctx-b", cmp(1), "merchant_1")
    await almacen.atar_contexto("ctx-c", cmp(2), "merchant_1")
    for n, (compra, ctx) in enumerate([(cmp(1), "ctx-a"), (cmp(2), "ctx-c"), (cmp(1), "ctx-b")]):
        apr = fila_apr(f"apr-{n}", creada=(AHORA + timedelta(minutes=n)).isoformat())
        apr.update(compra_id=compra, contexto_id=ctx)
        await almacen.guardar_aprobacion(apr)
    antes = len(base.pedidos) if base else 0
    contextos = await almacen.contextos_de_compras([cmp(1), cmp(2), cmp(3)])
    aprobaciones = await almacen.aprobaciones_de_compras([cmp(1), cmp(2), cmp(3)])
    assert contextos == {cmp(1): ["ctx-a", "ctx-b"], cmp(2): ["ctx-c"], cmp(3): []}
    if base is not None:
        # Una consulta por tabla (no una por compra).
        assert len(base.pedidos) - antes == 2
    assert {k: [f["id"] for f in v] for k, v in aprobaciones.items()} == {
        cmp(1): ["apr-0", "apr-2"], cmp(2): ["apr-1"], cmp(3): [],
    }
    # Lo mismo que de a una.
    for compra in (cmp(1), cmp(2), cmp(3)):
        assert contextos[compra] == await almacen.contextos_de_compra(compra)
    assert await almacen.contextos_de_compras([]) == {}
    assert await almacen.aprobaciones_de_compras([]) == {}


async def test_varias_compras_con_un_id_raro_no_llegan_a_la_base(almacen):
    with pytest.raises(ErrorAlmacen):
        await almacen.contextos_de_compras([cmp(1), "cmp-1,cmp-2"])
    with pytest.raises(ErrorAlmacen):
        await almacen.aprobaciones_de_compras(["cmp-1)"])


async def test_muchas_compras_y_muchas_filas_se_leen_enteras(monkeypatch):
    """Más ids que un pedido y más filas que una página de PostgREST."""
    from proxy import estado

    monkeypatch.setattr(estado, "IDS_POR_CONSULTA", 20)
    base = BaseFalsa()
    almacen = EstadoSupabase(URL, "clave", transport=base.transporte())
    almacen.FILAS_POR_PAGINA = 7  # varias páginas por consulta
    ids = [cmp(i) for i in range(50)]
    for compra in ids:
        await almacen.guardar_compra(fila_compra(compra))
    for n, compra in enumerate(ids):
        for k in range(3):
            await almacen.atar_contexto(f"ctx-{n}-{k}", compra, "merchant_1")
    antes = len(base.pedidos)
    contextos = await almacen.contextos_de_compras(ids)
    assert all(contextos[c] == [f"ctx-{n}-{k}" for k in range(3)] for n, c in enumerate(ids))
    # 3 trozos de ids (20, 20 y 10), de a 7 filas por página: 9 + 9 + 5 pedidos.
    assert len(base.pedidos) - antes == 23


# -- purga ---------------------------------------------------------------------------------


def _evento(n: int, compra: str, ts: datetime) -> Evento:
    return Evento(id=f"evt-{n}", ts=ts, contexto_id=None, escenario=None, guard_activo=True,
                  tipo="veredicto", datos={})


async def test_purgar_borra_lo_viejo_y_deja_lo_nuevo(almacen_y_base):
    almacen, base = almacen_y_base
    vieja, nueva = cmp(1), cmp(2)
    antes_de = AHORA - timedelta(days=90)
    await almacen.guardar_compra(fila_compra(vieja, creada=antes_de - timedelta(days=1)))
    await almacen.guardar_compra(fila_compra(nueva, creada=AHORA))
    await almacen.atar_contexto("ctx-viejo", vieja, "merchant_1")
    await almacen.atar_contexto("ctx-nuevo", nueva, "merchant_1")
    await almacen.guardar_ofertas(fila_ofertas("ctx-viejo", "merchant_1", [], [], None,
                                               antes_de - timedelta(days=1)))
    await almacen.guardar_ofertas(fila_ofertas("ctx-nuevo", "merchant_1", [], [], None, AHORA))
    await almacen.guardar_ultimo_veredicto(fila_ultimo_veredicto(
        "ctx-viejo", "cart_1", "aprobado", 1, "USD", antes_de - timedelta(days=1)))
    await almacen.guardar_ultimo_veredicto(fila_ultimo_veredicto(
        "ctx-nuevo", "cart_1", "aprobado", 1, "USD", AHORA))
    apr_vieja = fila_apr("apr-v", creada=(antes_de - timedelta(days=1)).isoformat())
    apr_vieja.update(compra_id=vieja, contexto_id="ctx-viejo")
    apr_nueva = fila_apr("apr-n", creada=AHORA.isoformat())
    apr_nueva.update(compra_id=nueva, contexto_id="ctx-nuevo")
    await almacen.guardar_aprobacion(apr_vieja)
    await almacen.guardar_aprobacion(apr_nueva)
    await almacen.guardar_evento(fila_evento(_evento(1, vieja, antes_de - timedelta(days=1)),
                                             integrador_id="acme", compra_id=vieja))
    await almacen.guardar_evento(fila_evento(_evento(2, nueva, AHORA),
                                             integrador_id="acme", compra_id=nueva))
    await almacen.guardar_entrega(EntregaWebhook(
        id="whe-viejo", integrador_id="acme", tipo="ping", compra_id=None,
        creada=antes_de - timedelta(days=1)))
    await almacen.guardar_entrega(EntregaWebhook(
        id="whe-nuevo", integrador_id="acme", tipo="ping", compra_id=None, creada=AHORA))

    borradas = await almacen.purgar(antes_de)

    assert borradas["gw_compras"] == 1 and borradas["gw_eventos"] == 1
    assert borradas["gw_ofertas"] == 1 and borradas["gw_ultimos_veredictos"] == 1
    assert borradas["gw_aprobaciones"] == 1 and borradas["gw_entregas_webhook"] == 1
    assert await almacen.cargar_compra(vieja) is None
    assert await almacen.cargar_compra(nueva) is not None
    assert await almacen.compra_de_contexto("ctx-viejo") is None
    assert await almacen.compra_de_contexto("ctx-nuevo") == nueva
    assert await almacen.cargar_ofertas("ctx-viejo") is None
    assert await almacen.cargar_ofertas("ctx-nuevo") is not None
    assert await almacen.cargar_ultimo_veredicto("ctx-viejo", "cart_1") is None
    assert [f["id"] for f in await almacen.aprobaciones_de_compra(nueva)] == ["apr-n"]
    assert await almacen.aprobaciones_de_compra(vieja) == []
    assert await almacen.listar_eventos(vieja) == []
    assert [f["id"] for f in await almacen.listar_eventos(nueva)] == ["evt-2"]
    assert [e["id"] for e in await almacen.listar_entregas("acme")] == ["whe-nuevo"]
    # Otra vez: no queda nada viejo.
    assert set((await almacen.purgar(antes_de)).values()) == {0}


async def test_purgar_con_una_fecha_rara_no_borra_nada(almacen):
    await almacen.guardar_compra(fila_compra(cmp(1), creada=AHORA - timedelta(days=200)))
    with pytest.raises(ErrorAlmacen):
        await almacen.purgar("hace mucho")
    assert await almacen.cargar_compra(cmp(1)) is not None


# -- esquema: 005_historial.sql ----------------------------------------------------------


def test_005_es_aditiva_idempotente_y_solo_indices():
    import re

    from proxy.tests.test_estado_supabase import SQL, _normal, _sin_comentarios, columnas_sql

    crudo = (SQL / "005_historial.sql").read_text(encoding="utf-8")
    texto = _normal(crudo)
    for prohibido in ("drop ", "truncate", "delete from", "alter ", "rename", "create table",
                      "insert ", "update "):
        assert prohibido not in texto, prohibido
    assert "003_gateway.sql" in crudo and "004_serverless.sql" in crudo
    sentencias = [s.strip() for s in _sin_comentarios(crudo).split(";") if s.strip()]
    tablas = columnas_sql("003_gateway.sql", "004_serverless.sql")
    indexadas: dict[str, set[str]] = {}
    for sentencia in sentencias:
        m = re.fullmatch(
            r"create index if not exists \w+\s+on public\.(\w+) \(([^)]*)\)(\s+where [^;]+)?",
            re.sub(r"\s+", " ", sentencia),
        )
        assert m is not None, sentencia
        columnas = [c.split()[0] for c in m.group(2).split(",")]
        assert set(columnas) <= tablas[m.group(1)], sentencia
        indexadas.setdefault(m.group(1), set()).add(columnas[0] if len(columnas) == 1 else ",".join(columnas))
    # La purga tiene un índice por la fecha de cada tabla.
    for tabla, (columna, _) in EstadoSupabase._PURGA.items():
        assert columna in indexadas[tabla], tabla
    # El listado: por integrador, `creada desc, id desc`.
    assert "integrador_id,creada,id" in indexadas["gw_compras"]


def test_aplicar_migraciones_aplica_005_despues_de_004():
    from proxy.tests.test_despliegue_vercel import _script

    migraciones = _script("aplicar_migraciones").MIGRACIONES
    assert migraciones == ("003_gateway.sql", "004_serverless.sql", "005_historial.sql")


# -- eventos: cuántos de un tipo y los más nuevos ---------------------------------------


async def test_contar_eventos_y_los_mas_nuevos(almacen_y_base):
    almacen, _ = almacen_y_base
    for compra in (cmp(1), cmp(2)):
        await almacen.guardar_compra(fila_compra(compra))
    for n in range(450):
        tipo = "busqueda_enviada" if n % 3 == 0 else "reenviado"
        evento = Evento(id=f"evt-{n:04d}", ts=AHORA + timedelta(seconds=n), contexto_id=None,
                        escenario=None, guard_activo=True, tipo=tipo, datos={})
        await almacen.guardar_evento(fila_evento(evento, integrador_id="acme", compra_id=cmp(1)))
    await almacen.guardar_evento(fila_evento(
        Evento(id="evt-otra", ts=AHORA, guard_activo=True, tipo="busqueda_enviada", datos={}),
        integrador_id="acme", compra_id=cmp(2)))
    assert await almacen.contar_eventos(cmp(1), "busqueda_enviada") == 150
    assert await almacen.contar_eventos(cmp(1), "veredicto") == 0
    assert await almacen.contar_eventos(cmp(2), "busqueda_enviada") == 1
    recientes = await almacen.listar_eventos_recientes(cmp(1), limite=250)
    # Los 250 más nuevos, en orden (no los más viejos).
    assert [f["id"] for f in recientes] == [f"evt-{n:04d}" for n in range(200, 450)]
    assert len(await almacen.listar_eventos_recientes(cmp(1), limite=1000)) == 450
    assert await almacen.listar_eventos_recientes(cmp(3)) == []
    with pytest.raises(ErrorAlmacen):
        await almacen.contar_eventos(cmp(1), "tipo raro;")
    with pytest.raises(ErrorAlmacen):
        await almacen.listar_eventos_recientes("cmp-1&x")


async def test_purgar_con_una_base_sin_004():
    """Con una sola instancia la base puede tener solo 003 (sin `gw_eventos` ni
    `gw_esperas`): la purga borra lo de 003 y saltea esas tablas."""
    base = BaseFalsa()
    del base.tablas["gw_eventos"], base.tablas["gw_esperas"]
    almacen = EstadoSupabase(URL, "clave", transport=base.transporte())
    await almacen.guardar_compra(fila_compra(cmp(1), creada=AHORA - timedelta(days=100)))
    borradas = await almacen.purgar(AHORA - timedelta(days=90))
    assert borradas["gw_compras"] == 1
    assert borradas["gw_eventos"] == 0 and borradas["gw_esperas"] == 0
    # Una tabla de 003 que falta sí es un error (la base no es la esperada).
    del base.tablas["gw_ofertas"]
    with pytest.raises(ErrorAlmacen):
        await almacen.purgar(AHORA - timedelta(days=90))


async def test_purgar_sin_004_con_el_codigo_de_postgrest():
    import httpx

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/gw_eventos", "/gw_esperas")):
            return httpx.Response(404, json={"code": "PGRST205", "message": "Could not find the table"})
        return httpx.Response(200, json=[])

    almacen = EstadoSupabase(URL, "clave", transport=httpx.MockTransport(responder))
    borradas = await almacen.purgar(AHORA)
    assert set(borradas.values()) == {0}
