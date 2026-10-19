"""Prueba de las pausas por `/v1` con varias instancias (el motor en Vercel).

Recorre una compra de integrador como la describe docs/INTEGRACION.md (§5)
contra un gateway ya levantado: el balanceador de `deploy/vercel/local_multi.py`
(:8040, varias instancias de `index:app`) o el deploy de Vercel. La búsqueda
(`find_products`) queda retenida en una instancia mientras la pregunta del
porqué y la decisión sobre la oportunidad se contestan por `/v1` **desde
otros pedidos**, que el balanceador reparte a otras instancias. Es lo que
tiene que aguantar el núcleo multi-instancia: el estado y las esperas viven
en Supabase, no en la memoria de una instancia.

Casos (`--caso`):

- `aceptar` (por defecto): pregunta → respuesta → oportunidad → aceptar.
  Exige `guard.parametros_autorizados` en la búsqueda y el checkout del cart
  autorizado con el status esperado (`--esperado`, por defecto que llegue a
  la firma).
- `rechazar`: igual, pero rechaza: sin parámetros nuevos, sigue lo pedido.
- `omitir`: contesta `{"omitir": true}`: sigue el pedido literal.
- `sin-porque`: no contesta: al vencer la pregunta sigue literal.
- `sin-decidir`: contesta el porqué y no decide: la búsqueda termina `failed`
  ("en pausa"), la compra queda `vencida` y el A2A siguiente da 409.
- `matar-porque` / `matar-oportunidad`: con la búsqueda retenida, mata la
  instancia que la tiene (`POST /__balanceador/matar/<n>`, solo con
  local_multi.py) y exige el lado seguro pasado `vence` + la gracia
  (`--gracia`, la de `GATEWAY_GRACIA_ESPERA_S`): el porqué sigue literal y la
  oportunidad queda `vencida`. Antes de eso la compra se tiene que seguir
  viendo en espera.

`--otra-instancia` exige (por `X-Balanceador-Instancia`) que la respuesta, la
decisión y el checkout los atienda una instancia distinta de la que retiene
la búsqueda. `--aprobar-doble`: si el checkout escala, aprueba dos veces a la
vez (dos instancias) y exige una sola aprobación, que sirva una vez.

El comercio tiene que ser el general de la simulación con un comportamiento
que dé una oportunidad (`--escenario general_entrega_partes`, el de por
defecto: la mitad llega al plazo y el resto después). La clave del
integrador va en `KAIAI_CLAVE` (o `--clave`).

Desde la raíz del repo:

    KAIAI_CLAVE=psg_… PYTHONUTF8=1 world/ap2/.venv/Scripts/python \\
        proxy/scripts/smoke_v1_pausas.py --gateway http://127.0.0.1:8040 --otra-instancia

Sale con 0 si todo lo exigido se cumplió.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

RAIZ = Path(__file__).resolve().parents[2]
if str(RAIZ) not in sys.path:  # `python proxy/scripts/smoke_v1_pausas.py` desde donde sea
    sys.path.insert(0, str(RAIZ))

import httpx  # noqa: E402

from contracts import GUARD_PARAMETROS_KEY, GUARD_VEREDICTO_KEY  # noqa: E402
from proxy.scripts.smoke_v1_general import (  # noqa: E402
    EXTENSION_AP2,
    LLEGAN_A_FIRMA,
    STATUS,
    datos_de,
    elegir_cart,
    mensaje_rpc,
    texto_de,
)

GATEWAY_LOCAL = "http://127.0.0.1:8040"  # el balanceador de local_multi.py
# Vercel corta a los 300 s: el cliente espera un poco más para ver el 504.
TIMEOUT = httpx.Timeout(330.0, connect=10.0)
HEADER_INSTANCIA = "x-balanceador-instancia"
HEADER_PEDIDO = "X-Balanceador-Pedido"
HEADER_EVITAR = "X-Balanceador-Evitar"
CASOS = (
    "aceptar", "rechazar", "omitir", "sin-porque", "sin-decidir",
    "matar-porque", "matar-oportunidad",
)
ESPERADOS = (*STATUS, "firma")  # firma = aprobado o con_avisos
RESPUESTA_POR_DEFECTO = (
    "Es para un aula que arranca el lunes; si llegan la mitad el lunes y el "
    "resto en los días siguientes, sirve."
)
TEXTO_PAUSA = "en pausa"  # _TEXTO_PAUSA del proxy: "KaiAI: en pausa. …"
SONDEO_S = 1.0

Salida = Callable[[str], None]


# ---------------------------------------------------------------------------
# pedidos
# ---------------------------------------------------------------------------


def proximo_lunes(hoy: date) -> date:
    """"El lunes" dicho hoy: el próximo, nunca hoy (regla del intake)."""
    return hoy + timedelta(days=(0 - hoy.weekday()) % 7 or 7)


def ficha_de(args: argparse.Namespace) -> dict[str, Any]:
    """Lo que autorizó el usuario: tope, moneda, cantidad y, si hay plazo,
    todas las unidades para esa fecha (`entregas` `cubre`)."""
    requisitos: list[dict[str, Any]] = [
        {"campo": "precio", "operador": "maximo", "valor": args.tope, "obligatorio": True},
        {"campo": "moneda", "operador": "igual", "valor": args.moneda, "obligatorio": True},
    ]
    if args.cantidad:
        requisitos.append(
            {"campo": "cantidad", "operador": "minimo", "valor": args.cantidad, "obligatorio": True}
        )
        if args.plazo:
            requisitos.append({
                "campo": "entregas", "operador": "cubre", "obligatorio": True,
                "valor": [{"cantidad": args.cantidad, "fecha": args.plazo}],
            })
    ficha: dict[str, Any] = {"requisitos": requisitos}
    if args.categoria:
        ficha["categoria_taxonomia"] = args.categoria
    return ficha


def pedido_busqueda(args: argparse.Namespace) -> dict[str, Any]:
    partes: list[dict[str, Any]] = [
        {"catalog_search": args.pedido},
        {"risk_data": "smoke_risk_data"},
        {"shopping_agent_id": "trusted_shopping_agent"},
    ]
    if args.escenario:
        partes.append({"demo_scenario": args.escenario})
    return mensaje_rpc(partes, "Find products that match the user's request.")


def pedido_checkout(cart_id: str, contexto: str | None) -> dict[str, Any]:
    return mensaje_rpc(
        [{"cart_id": cart_id}, {"shopping_agent_id": "trusted_shopping_agent"}],
        "Create a checkout for the selected cart.", contexto,
    )


def _fecha(valor: Any) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# el gateway (y el balanceador, si está)
# ---------------------------------------------------------------------------


@dataclass
class Resp:
    status: int
    cuerpo: Any
    instancia: int | None
    texto: str = ""

    def error_a2a(self) -> str | None:
        """`data.codigo` de un error JSON-RPC del gateway."""
        if isinstance(self.cuerpo, dict) and isinstance(self.cuerpo.get("error"), dict):
            datos = self.cuerpo["error"].get("data")
            if isinstance(datos, dict):
                return datos.get("codigo")
        return None

    def tarea(self) -> dict[str, Any] | None:
        if self.status == 200 and isinstance(self.cuerpo, dict):
            resultado = self.cuerpo.get("result")
            if isinstance(resultado, dict):
                return resultado
        return None


@dataclass
class Gateway:
    http: httpx.AsyncClient
    balanceador: bool = False  # vino X-Balanceador-Instancia: es local_multi.py
    instancias: dict[str, int | None] = field(default_factory=dict)

    async def pedir(
        self,
        metodo: str,
        ruta: str,
        *,
        json_: Any = None,
        contenido: bytes | None = None,
        headers: dict[str, str] | None = None,
        evitar: tuple[int | None, ...] = (),
        paso: str | None = None,
    ) -> Resp:
        h = dict(headers or {})
        evitar_ok = sorted({i for i in evitar if i})
        if self.balanceador and evitar_ok:
            h[HEADER_EVITAR] = ",".join(str(i) for i in evitar_ok)
        resp = await self.http.request(metodo, ruta, json=json_, content=contenido, headers=h)
        instancia = _instancia(resp)
        if instancia is not None:
            self.balanceador = True
        if paso:
            self.instancias[paso] = instancia
        try:
            cuerpo = resp.json()
        except ValueError:
            cuerpo = None
        return Resp(resp.status_code, cuerpo, instancia, resp.text[:300])

    async def a2a(
        self,
        ruta: str,
        compra_id: str,
        pedido: dict[str, Any],
        *,
        pedido_id: str | None = None,
        evitar: tuple[int | None, ...] = (),
        paso: str | None = None,
    ) -> Resp:
        headers = {
            "content-type": "application/json",
            "X-Compra-Id": compra_id,
            "X-A2A-Extensions": EXTENSION_AP2,
        }
        if pedido_id and self.balanceador:
            headers[HEADER_PEDIDO] = pedido_id
        return await self.pedir(
            "POST", ruta, contenido=json.dumps(pedido).encode("utf-8"), headers=headers,
            evitar=evitar, paso=paso,
        )

    async def compra(self, compra_id: str) -> Resp:
        return await self.pedir("GET", f"/v1/compras/{compra_id}")

    async def instancia_de(self, pedido_id: str, espera_s: float = 5.0) -> int | None:
        """A qué instancia mandó el balanceador `pedido_id` (None sin balanceador)."""
        if not self.balanceador:
            return None
        fin = time.monotonic() + espera_s
        while time.monotonic() < fin:
            resp = await self.http.get(f"/__balanceador/pedido/{pedido_id}")
            if resp.status_code == 200:
                numero = resp.json().get("instancia")
                return numero if isinstance(numero, int) else None
            await asyncio.sleep(0.2)
        return None

    async def matar(self, numero: int) -> bool:
        resp = await self.http.post(f"/__balanceador/matar/{numero}")
        return resp.status_code == 200


def _instancia(resp: httpx.Response) -> int | None:
    valor = resp.headers.get(HEADER_INSTANCIA)
    return int(valor) if valor and valor.isdigit() else None


async def esperar_pendiente(
    gw: Gateway,
    compra_id: str,
    tipo: str,
    busqueda: asyncio.Task,
    espera_s: float,
    salida: Salida,
) -> dict[str, Any] | None:
    """La vista de la compra cuando aparece `pendiente.tipo == tipo` (cada GET
    va a la instancia que toque: ninguna tiene que cerrar la espera de otra),
    o None si la búsqueda terminó antes o pasó `espera_s`."""
    fin = time.monotonic() + espera_s
    vista: dict[str, Any] | None = None
    while time.monotonic() < fin:
        resp = await gw.compra(compra_id)
        if resp.status == 200 and isinstance(resp.cuerpo, dict):
            vista = resp.cuerpo
            pendiente = vista.get("pendiente") or {}
            if pendiente.get("tipo") == tipo:
                return vista
        elif resp.status >= 500:
            salida(f"  GET compra → {resp.status} (instancia {resp.instancia})")
        if busqueda.done():
            return None
        await asyncio.sleep(SONDEO_S)
    if vista is not None:
        salida(f"  la compra sigue en {vista.get('estado')} sin pendiente {tipo}")
    return None


# ---------------------------------------------------------------------------
# el flujo
# ---------------------------------------------------------------------------


@dataclass
class Corrida:
    gw: Gateway
    args: argparse.Namespace
    salida: Salida
    fallas: list[str] = field(default_factory=list)

    def falla(self, mensaje: str) -> None:
        self.fallas.append(mensaje)

    def esperar_status(self, status: str | None) -> None:
        esperado = self.args.esperado
        if esperado is None or status is None:
            return
        ok = status in LLEGAN_A_FIRMA if esperado == "firma" else status == esperado
        if not ok:
            self.falla(f"se esperaba {esperado} y llegó {status}")


async def flujo(gw: Gateway, args: argparse.Namespace, salida: Salida = print) -> int:
    c = Corrida(gw, args, salida)
    cuerpo = {
        "pedido": args.pedido,
        "comercio": args.comercio,
        "ficha": ficha_de(args),
        "intencion": {"preguntar": True},
        "referencia": f"smoke-v1-pausas-{args.caso}",
    }
    r = await gw.pedir("POST", "/v1/compras", json_=cuerpo, paso="compra")
    if r.status != 201 or not isinstance(r.cuerpo, dict):
        salida(f"POST /v1/compras → {r.status} {r.texto}")
        return _fin(["no se pudo crear la compra"], salida)
    compra = r.cuerpo
    compra_id = compra["compra_id"]
    ruta = urlsplit(compra["a2a_url"]).path
    salida(f"compra {compra_id}: estado={compra['estado']} intencion_activa="
           f"{compra.get('intencion_activa')} (instancia {r.instancia})")
    if not compra.get("intencion_activa"):
        return _fin(["el gateway no tiene la capa de intención: no hay pausas que probar"], salida)
    necesita_balanceador = args.otra_instancia or args.caso.startswith("matar")
    if necesita_balanceador and not gw.balanceador:
        return _fin([
            "este caso necesita el balanceador de deploy/vercel/local_multi.py "
            "(no vino X-Balanceador-Instancia)",
        ], salida)

    pedido_id = f"smoke-{uuid.uuid4().hex[:16]}"
    busqueda = asyncio.create_task(
        gw.a2a(ruta, compra_id, pedido_busqueda(args), pedido_id=pedido_id, paso="find_products")
    )
    try:
        return await _con_busqueda(c, compra_id, ruta, busqueda, pedido_id)
    finally:
        if not busqueda.done():
            busqueda.cancel()


async def _con_busqueda(
    c: Corrida, compra_id: str, ruta: str, busqueda: asyncio.Task, pedido_id: str
) -> int:
    gw, args, salida = c.gw, c.args, c.salida
    retiene = await gw.instancia_de(pedido_id)
    if gw.balanceador:
        salida(f"find_products: retenido en la instancia {retiene}")
    evitar = (retiene,)

    # -- el porqué ------------------------------------------------------------
    vista = await esperar_pendiente(gw, compra_id, "porque", busqueda, args.espera, salida)
    if vista is None:
        await _contar_busqueda(c, busqueda)
        return _fin([*c.fallas, "no llegó la pregunta del porqué"], salida)
    pendiente = vista["pendiente"]
    vence = _fecha(pendiente.get("vence"))
    salida(f"pregunta del porqué (vence {pendiente.get('vence')}): {pendiente.get('pregunta')}")

    if args.caso == "matar-porque":
        return await _matar_y_esperar(c, compra_id, ruta, busqueda, retiene, vence, "porque")
    if args.caso == "sin-porque":
        salida("  no contesto: al vencer tiene que seguir el pedido literal")
    else:
        cuerpo = {"omitir": True} if args.caso == "omitir" else {"respuesta": args.respuesta}
        r = await gw.pedir("POST", f"/v1/compras/{compra_id}/respuesta", json_=cuerpo,
                           evitar=evitar, paso="respuesta")
        estado = r.cuerpo.get("estado") if isinstance(r.cuerpo, dict) else None
        salida(f"POST respuesta → {r.status} estado={estado} (instancia {r.instancia})")
        if r.status != 200:
            c.falla(f"POST /respuesta dio {r.status}: {r.texto}")

    # -- la oportunidad ---------------------------------------------------------
    oportunidad: dict[str, Any] | None = None
    if args.caso not in ("sin-porque", "omitir"):
        vista = await esperar_pendiente(gw, compra_id, "oportunidad", busqueda, args.espera, salida)
        if vista is not None:
            oportunidad = vista["pendiente"].get("oportunidad") or {}
            vence = _fecha(vista["pendiente"].get("vence"))
            cambios = [(x.get("campo"), x.get("pedido"), x.get("ofrecido"))
                       for x in oportunidad.get("cambios") or []]
            salida(f"oportunidad {oportunidad.get('id')} (vence {vista['pendiente'].get('vence')}): "
                   f"cart {oportunidad.get('cart_id')}, cambios {cambios}")
        elif args.caso in ("sin-decidir", "matar-oportunidad") or args.exigir_oportunidad:
            await _contar_busqueda(c, busqueda)
            return _fin([*c.fallas, "no apareció la oportunidad"], salida)
        else:
            salida("  no hubo oportunidad: sigue lo pedido")

    if oportunidad is not None:
        if args.caso == "matar-oportunidad":
            return await _matar_y_esperar(c, compra_id, ruta, busqueda, retiene, vence, "oportunidad")
        if args.caso in ("aceptar", "rechazar"):
            decision = "aceptar" if args.caso == "aceptar" else "rechazar"
            r = await gw.pedir(
                "POST", f"/v1/compras/{compra_id}/decision",
                json_={"oportunidad_id": oportunidad.get("id"), "decision": decision},
                evitar=evitar, paso="decision",
            )
            estado = r.cuerpo.get("estado") if isinstance(r.cuerpo, dict) else None
            salida(f"POST decision {decision} → {r.status} estado={estado} (instancia {r.instancia})")
            if r.status != 200:
                c.falla(f"POST /decision dio {r.status}: {r.texto}")
            elif estado != ("aceptada" if decision == "aceptar" else "rechazada"):
                c.falla(f"la decisión no quedó aplicada en la respuesta (estado {estado})")
        else:
            salida("  no decido: tiene que vencer sin comprar nada")

    # -- lo que recibe el agente -----------------------------------------------
    r = await busqueda
    tarea = r.tarea()
    if tarea is None:
        salida(f"find_products → HTTP {r.status} {r.texto}")
        return _fin([*c.fallas, "find_products no devolvió una tarea"], salida)
    estado = (tarea.get("status") or {}).get("state")
    contexto = tarea.get("contextId")
    parametros = [d[GUARD_PARAMETROS_KEY] for d in datos_de(tarea) if GUARD_PARAMETROS_KEY in d]
    carts = [d["ap2.cart"] for d in datos_de(tarea) if isinstance(d.get("ap2.cart"), dict)]
    salida(f"find_products → estado={estado} contexto={contexto} carts="
           f"{[(x.get('cart_id'), x.get('amount')) for x in carts]} "
           f"parametros_autorizados={'sí' if parametros else 'no'} (instancia {r.instancia})")

    if args.caso == "sin-decidir":
        return await _vencida(c, compra_id, ruta, tarea, oportunidad, contexto)
    if estado != "completed":
        c.falla(f"find_products terminó {estado}: {texto_de(tarea)[:200]}")
        return _fin(c.fallas, salida)
    autorizado: str | None = None
    if args.caso == "aceptar" and oportunidad is not None:
        if not parametros:
            c.falla("se aceptó la oportunidad y no llegó guard.parametros_autorizados")
        else:
            autorizado = parametros[0].get("cart_id")
            if autorizado != oportunidad.get("cart_id"):
                c.falla(f"guard.parametros_autorizados es del cart {autorizado}, la oportunidad "
                        f"era del {oportunidad.get('cart_id')}")
    elif parametros:
        c.falla("llegó guard.parametros_autorizados sin que nadie aceptara")
    vista = (await gw.compra(compra_id)).cuerpo or {}
    salida(f"GET compra: estado={vista.get('estado')} decision="
           f"{'aceptada' if vista.get('estado') == 'aceptada' else '-'}")
    if args.caso in ("sin-porque", "omitir") and vista.get("estado") in (
        "esperando_contexto", "esperando_decision"
    ):
        c.falla(f"la compra quedó en {vista.get('estado')} después de seguir literal")

    # -- el checkout -------------------------------------------------------------
    if autorizado:
        cart_id, por_que = autorizado, "el autorizado por el usuario"
    else:
        cart, por_que = elegir_cart(carts, args.tope, args.moneda)
        cart_id = cart.get("cart_id") if cart else None
    if not cart_id or not contexto:
        return _fin([*c.fallas, "no hay cart o contexto para el checkout"], salida)
    salida(f"elijo {cart_id} ({por_que})")
    r = await gw.a2a(ruta, compra_id, pedido_checkout(cart_id, contexto), evitar=(retiene,),
                     paso="create_checkout")
    status = await _veredicto(c, r, "create_checkout")
    c.esperar_status(status)
    if status == "escalar" and args.aprobar_doble:
        await _aprobar_doble(c, compra_id, ruta, cart_id, contexto)
    _instancias(c, retiene)
    return _fin(c.fallas, salida)


async def _veredicto(c: Corrida, r: Resp, paso: str) -> str | None:
    tarea = r.tarea()
    if tarea is None:
        c.falla(f"{paso}: HTTP {r.status} {r.texto}")
        return None
    datos = datos_de(tarea)
    veredictos = [d[GUARD_VEREDICTO_KEY] for d in datos if GUARD_VEREDICTO_KEY in d]
    checkouts = [d["ap2.checkout"] for d in datos if "ap2.checkout" in d]
    if not veredictos:
        c.falla(f"{paso}: la tarea no trajo guard.veredicto")
        return None
    veredicto = veredictos[0]
    status = veredicto.get("status")
    c.salida(f"{paso} → status={status} ap2.checkout={'sí' if checkouts else 'no'} "
             f"(instancia {r.instancia})")
    c.salida(f"  motivo: {veredicto.get('motivo')}")
    if status in LLEGAN_A_FIRMA and not checkouts:
        c.falla(f"{paso}: {status} sin ap2.checkout")
    if status not in LLEGAN_A_FIRMA and checkouts and paso == "create_checkout":
        c.falla(f"{paso}: {status} con el ap2.checkout adentro")
    return status


async def _contar_busqueda(c: Corrida, busqueda: asyncio.Task) -> None:
    if not busqueda.done():
        return
    r = busqueda.result()
    tarea = r.tarea()
    if tarea is None:
        c.salida(f"  find_products → HTTP {r.status} {r.texto}")
    else:
        c.salida(f"  find_products terminó {(tarea.get('status') or {}).get('state')}: "
                 f"{texto_de(tarea)[:200]}")


async def _vencida(
    c: Corrida,
    compra_id: str,
    ruta: str,
    tarea: dict[str, Any],
    oportunidad: dict[str, Any] | None,
    contexto: str | None,
) -> int:
    """Sin decisión: `failed` "en pausa", compra `vencida`, el A2A siguiente 409."""
    estado = (tarea.get("status") or {}).get("state")
    if estado != "failed" or TEXTO_PAUSA not in texto_de(tarea):
        c.falla(f"sin decidir, la búsqueda terminó {estado}: {texto_de(tarea)[:200]}")
    if any("ap2.cart" in d for d in datos_de(tarea)):
        c.falla("sin decidir, al agente le llegaron las ofertas")
    vista = (await c.gw.compra(compra_id)).cuerpo or {}
    c.salida(f"GET compra: estado={vista.get('estado')}")
    if vista.get("estado") != "vencida":
        c.falla(f"sin decidir, la compra quedó {vista.get('estado')} (se esperaba vencida)")
    cart_id = (oportunidad or {}).get("cart_id") or "cart_1"
    r = await c.gw.a2a(ruta, compra_id, pedido_checkout(cart_id, contexto), paso="a2a_despues")
    c.salida(f"A2A después → HTTP {r.status} {r.error_a2a()}")
    if r.status != 409 or r.error_a2a() != "compra_vencida":
        c.falla(f"el A2A después de vencer dio {r.status} {r.error_a2a()} (se esperaba 409 "
                "compra_vencida)")
    return _fin(c.fallas, c.salida)


async def _matar_y_esperar(
    c: Corrida,
    compra_id: str,
    ruta: str,
    busqueda: asyncio.Task,
    retiene: int | None,
    vence: datetime | None,
    tipo: str,
) -> int:
    """Mata la instancia que retiene la búsqueda y exige el lado seguro, pero
    recién pasado `vence` + la gracia."""
    gw, args, salida = c.gw, c.args, c.salida
    if retiene is None or vence is None:
        return _fin([*c.fallas, "no sé qué instancia retiene la búsqueda o cuándo vence"], salida)
    if not await gw.matar(retiene):
        return _fin([*c.fallas, f"el balanceador no mató la instancia {retiene}"], salida)
    salida(f"maté la instancia {retiene}")
    r = await busqueda
    salida(f"find_products → HTTP {r.status} (lo que estaba en curso en la instancia muerta)")
    if r.tarea() is not None and (r.tarea().get("status") or {}).get("state") == "completed":
        c.falla("la búsqueda terminó bien aunque se mató su instancia")

    seguro = vence + timedelta(seconds=args.gracia)
    limite = seguro + timedelta(seconds=args.margen)
    en_espera = "esperando_contexto" if tipo == "porque" else "esperando_decision"
    salida(f"espero el lado seguro: vence {vence.isoformat()} + gracia {args.gracia:g} s")
    cambio: datetime | None = None
    estado = en_espera
    while _ahora() < limite:
        resp = await gw.compra(compra_id)
        if resp.status == 200 and isinstance(resp.cuerpo, dict):
            estado = resp.cuerpo.get("estado")
            if estado != en_espera:
                cambio = _ahora()
                break
        await asyncio.sleep(2.0)
    salida(f"GET compra: estado={estado}"
           + (f" (cambió a los {(cambio - vence).total_seconds():.0f} s de vence)" if cambio else ""))
    if cambio is None:
        return _fin([*c.fallas, f"pasado vence + gracia la compra sigue en {en_espera}"], salida)
    if cambio < seguro - timedelta(seconds=args.tolerancia):
        c.falla("la espera se cerró antes de vence + gracia: con la instancia viva, otra "
                "instancia le habría matado la pausa")
    if tipo == "porque":
        if estado != "negociando":
            c.falla(f"el porqué sin respuesta tiene que seguir literal (negociando), quedó {estado}")
        r = await gw.a2a(ruta, compra_id, pedido_busqueda(args), paso="find_products_2")
        tarea = r.tarea()
        estado_2 = (tarea.get("status") or {}).get("state") if tarea else None
        salida(f"find_products otra vez → {estado_2} (instancia {r.instancia})")
        if estado_2 != "completed":
            c.falla(f"después del lado seguro la búsqueda dio {r.status} {estado_2}")
    else:
        if estado != "vencida":
            c.falla(f"la oportunidad sin decisión tiene que quedar vencida, quedó {estado}")
        r = await gw.a2a(ruta, compra_id, pedido_checkout("cart_1", None), paso="a2a_despues")
        salida(f"A2A después → HTTP {r.status} {r.error_a2a()}")
        if r.status != 409 or r.error_a2a() != "compra_vencida":
            c.falla(f"el A2A después de vencer dio {r.status} {r.error_a2a()}")
    return _fin(c.fallas, salida)


async def _aprobar_doble(
    c: Corrida, compra_id: str, ruta: str, cart_id: str, contexto: str
) -> None:
    """Dos aprobaciones a la vez (en dos instancias): una sola, de un solo uso."""
    gw, salida = c.gw, c.salida
    cuerpo = {"cart_id": cart_id, "contexto_id": contexto}
    ruta_v1 = f"/v1/compras/{compra_id}/aprobaciones"
    r1, r2 = await asyncio.gather(
        gw.pedir("POST", ruta_v1, json_=cuerpo), gw.pedir("POST", ruta_v1, json_=cuerpo)
    )
    ids = [r.cuerpo.get("id") if isinstance(r.cuerpo, dict) else None for r in (r1, r2)]
    salida(f"aprobaciones → {r1.status}/{r2.status} ids={ids} instancias "
           f"{r1.instancia}/{r2.instancia}")
    if {r1.status, r2.status} - {200, 201} or not ids[0] or ids[0] != ids[1]:
        c.falla("dos aprobaciones a la vez no dieron la misma")
    if [r1.status, r2.status].count(201) > 1:
        c.falla("las dos aprobaciones se crearon (201 y 201)")
    if c.args.otra_instancia and r1.instancia == r2.instancia:
        c.falla("las dos aprobaciones cayeron en la misma instancia")
    r = await gw.a2a(ruta, compra_id, pedido_checkout(cart_id, contexto), paso="checkout_aprobado")
    tarea = r.tarea()
    llego = bool(tarea) and any("ap2.checkout" in d for d in datos_de(tarea))
    salida(f"checkout aprobado → ap2.checkout={'sí' if llego else 'no'} (instancia {r.instancia})")
    if not llego:
        c.falla("con la aprobación, el checkout no pasó")
    r = await gw.a2a(ruta, compra_id, pedido_checkout(cart_id, contexto), paso="checkout_reuso")
    tarea = r.tarea()
    otra_vez = bool(tarea) and any("ap2.checkout" in d for d in datos_de(tarea))
    salida(f"checkout otra vez → ap2.checkout={'sí' if otra_vez else 'no'} (instancia {r.instancia})")
    if otra_vez:
        c.falla("la aprobación sirvió dos veces")


def _instancias(c: Corrida, retiene: int | None) -> None:
    if not c.gw.balanceador:
        c.salida("instancias: sin balanceador (Vercel no las muestra)")
        return
    pasos = ", ".join(f"{p}={i}" for p, i in c.gw.instancias.items())
    c.salida(f"instancias: {pasos}")
    if not c.args.otra_instancia:
        return
    otros = {p: i for p, i in c.gw.instancias.items()
             if p in ("respuesta", "decision", "create_checkout")}
    if not otros:
        c.falla("--otra-instancia: no hubo respuesta, decisión ni checkout que comparar")
    for paso, instancia in otros.items():
        if instancia is None or instancia == retiene:
            c.falla(f"--otra-instancia: {paso} lo atendió la instancia {instancia}, la misma que "
                    "retiene la búsqueda")


def _fin(fallas: list[str], salida: Salida) -> int:
    for falla in fallas:
        salida(f"FALLA: {falla}")
    if fallas:
        return 1
    salida("OK")
    return 0


async def correr(
    args: argparse.Namespace,
    *,
    transporte: httpx.AsyncBaseTransport | None = None,
    salida: Salida = print,
) -> int:
    clave = args.clave or os.environ.get("KAIAI_CLAVE")
    if not clave:
        return _fin(["hace falta la clave del integrador (--clave o KAIAI_CLAVE)"], salida)
    salida(f"gateway {args.gateway} · caso {args.caso} · pedido {args.pedido!r} · "
           f"escenario {args.escenario or '-'}")
    try:
        async with httpx.AsyncClient(
            base_url=args.gateway.rstrip("/"), timeout=TIMEOUT, trust_env=False,
            transport=transporte, headers={"Authorization": f"Bearer {clave}"},
        ) as http:
            return await flujo(Gateway(http), args, salida)
    except httpx.HTTPError as e:
        return _fin([f"no se pudo hablar con el gateway ({type(e).__name__}: {e})"], salida)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pausas (porqué y oportunidad) de una compra por /v1 con varias instancias: "
        "las respuestas llegan por otras instancias que la que retiene la búsqueda."
    )
    p.add_argument("--gateway", default=GATEWAY_LOCAL,
                   help=f"URL del gateway ({GATEWAY_LOCAL}: local_multi.py; o la de Vercel)")
    p.add_argument("--clave", default=None, help="clave psg_… del integrador (o KAIAI_CLAVE)")
    p.add_argument("--caso", choices=CASOS, default="aceptar")
    p.add_argument("--otra-instancia", action="store_true",
                   help="exigir que respuesta, decisión y checkout vayan a otra instancia")
    p.add_argument("--aprobar-doble", action="store_true",
                   help="si el checkout escala: dos aprobaciones a la vez dan una sola")
    p.add_argument("--exigir-oportunidad", action="store_true",
                   help="fallar si no aparece la oportunidad (aceptar y rechazar)")
    p.add_argument("--pedido", default="20 computadoras para el lunes")
    p.add_argument("--respuesta", default=RESPUESTA_POR_DEFECTO, help="el porqué que contesta")
    p.add_argument("--tope", type=float, default=30000.0, help="precio máximo (obligatorio)")
    p.add_argument("--moneda", default="USD")
    p.add_argument("--cantidad", type=int, default=20, help="0: sin requisito de cantidad")
    p.add_argument("--plazo", default=None,
                   help="fecha YYYY-MM-DD para todas las unidades (por defecto, el próximo lunes; "
                   "vacío: sin plazo)")
    p.add_argument("--categoria", default="278",
                   help="ID de la taxonomía de Google (278 = computadoras); vacío: sin categoría")
    p.add_argument("--escenario", default="general_entrega_partes",
                   help="demo_scenario del comercio de la simulación; vacío: no se manda")
    p.add_argument("--comercio", default="merchant_1", help="id del comercio en el gateway")
    p.add_argument("--esperado", choices=ESPERADOS, default=None,
                   help="status del checkout (firma = aprobado o con_avisos); en aceptar, "
                   "por defecto firma")
    p.add_argument("--espera", type=float, default=200.0,
                   help="segundos máximos para que aparezca cada pendiente (200)")
    p.add_argument("--gracia", type=float, default=30.0,
                   help="la GATEWAY_GRACIA_ESPERA_S del motor (30)")
    p.add_argument("--margen", type=float, default=20.0,
                   help="segundos extra después de vence + gracia antes de fallar (20)")
    p.add_argument("--tolerancia", type=float, default=3.0,
                   help="diferencia de reloj aceptada con el gateway, en segundos (3)")
    return p


def leer_args(argv: list[str] | None = None, *, hoy: date | None = None) -> argparse.Namespace:
    args = parser().parse_args(argv)
    if args.plazo is None:
        args.plazo = proximo_lunes(hoy or date.today()).isoformat()
    elif args.plazo:
        date.fromisoformat(args.plazo)  # ValueError si no es una fecha
    if args.esperado is None and args.caso == "aceptar":
        args.esperado = "firma"
    return args


def main(argv: list[str] | None = None) -> None:
    sys.exit(asyncio.run(correr(leer_args(argv))))


if __name__ == "__main__":
    main()
