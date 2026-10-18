"""La demo de una pantalla (`/demo`): el input único y la conversación con el
agente de compras.

La pantalla reemplaza a la UI de ADK. Lo que escribe el usuario va:

- a **nuestro agente** (el intake del proxy), si tiene una pregunta pendiente
  de esta corrida: el porqué, la confirmación de la ficha o la decisión sobre
  una alternativa. Se reenvía al proxy con el token, igual que el panel;
- si no, al **agente de compras** por la API de `adk web` (`ADK_URL`): una
  sesión por compra y `POST /run_sse`.

En **modo demo** (`DEMO_AUTO`, prendido por defecto) los pasos del sample que
no aportan a la demo se contestan solos: la billetera y el permiso, la
tarjeta, la confirmación del resumen y el OTP (`123`). Se detectan por el
estado de la sesión de ADK y por qué herramientas se llamaron, nunca por el
texto del comercio. La elección del producto es siempre del usuario.

El selector de comercio escribe el escenario activo en el archivo que lee el
comercio en cada búsqueda (`demo_world.scenario`, solo lectura), sin
reiniciar nada.

El panel no decide: transporta lo que dice el usuario y muestra lo que pasa.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from dashboard.server import (
    STATIC_DIR,
    Config,
    _cliente_proxy,
    _error,
    _leer_json,
    _reenviar_json,
)

ADK_APP = "shopping_agent"
ADK_USUARIO = "demo"

MAX_TEXTO = 1000            # lo que escribe el usuario (igual que el porqué)
MAX_TEXTO_AGENTE = 4000     # lo que se guarda de cada respuesta del agente
MAX_MENSAJES = 400
MAX_ARG = 300
TOLERANCIA_RELOJ = timedelta(seconds=2)

# Modo demo: tope por paso y en total, para que nunca entre en un bucle.
TOPES_AUTO = {"billetera": 3, "tarjeta": 2, "continuar": 3, "confirmar": 2, "otp": 2}
MAX_AUTOS = 12
OTP_DEMO = "123"  # el del sample (DEMO.md)

ESPERAN_AL_USUARIO = frozenset({"esperando_contexto", "esperando_decision"})

# Selector visible del comercio: categoría → (escenario, etiqueta).
GRUPOS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Zapatillas", (
        ("baseline", "honesto"), ("B", "inyección"), ("B2", "miente un dato"), ("C", "precio inflado"),
    )),
    ("Notebook", (
        ("notebook_honesto", "honesto"), ("notebook_malicioso", "inyección"),
        ("notebook_dato_falso", "miente un dato"),
    )),
    ("Auriculares", (("auriculares_honesto", "honesto"), ("auriculares_malicioso", "inyección"))),
    ("Computadoras", (("computadoras", "entrega en dos partes"),)),
)
ESCENARIOS = frozenset(e for _, opciones in GRUPOS for e, _ in opciones)
ARCHIVO_ESCENARIO = "demo_scenario"  # demo_world.scenario.SCENARIO_FILE_NAME

# Herramientas del agente de compras que se cuentan en el relato, con los
# argumentos que se muestran (el resto no sale del servidor).
HERRAMIENTAS_VISIBLES = {
    "find_products": ("catalog_search",),
    "select_cart": ("cart_id",),
    "create_checkout": (),
    "initiate_payment": (),
}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _fecha(texto: Any) -> datetime | None:
    if not isinstance(texto, str):
        return None
    try:
        fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Ruteo del input único
# ---------------------------------------------------------------------------


class Ruta(NamedTuple):
    """A dónde va lo que escribió el usuario.

    `destino`: `agente` (ADK), `contexto` / `confirmar` / `omitir`
    (`POST /guard/compras/contexto`), `decision` (`/guard/compras/decision`)
    o `aclarar` (hay una alternativa pendiente y no se entiende si es sí o no).
    """

    destino: str
    cuerpo: dict[str, Any] | None = None


_AFIRMATIVAS = frozenset({
    "si", "ok", "okay", "dale", "confirmo", "acepto", "aceptar", "yes", "claro", "listo",
    "perfecto", "bueno", "va", "obvio", "confirmar", "sip",
})
_AFIRMATIVAS_DOBLES = frozenset({"de acuerdo", "esta bien", "me sirve"})
_NEGATIVAS = frozenset({"no", "nop", "nah", "rechazo", "rechazar", "negativo"})
_OMITIR = frozenset({"omitir", "omiti", "saltear", "saltea", "skip", "paso"})


def _palabras(texto: str) -> list[str]:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(c)
    )
    return re.findall(r"[a-z0-9]+", sin_tildes)


def es_afirmativo(texto: str) -> bool:
    palabras = _palabras(texto)
    if not palabras or "no" in palabras:
        return False
    return palabras[0] in _AFIRMATIVAS or " ".join(palabras[:2]) in _AFIRMATIVAS_DOBLES


def es_negativo(texto: str) -> bool:
    palabras = _palabras(texto)
    return bool(palabras) and palabras[0] in _NEGATIVAS


def compra_pendiente(
    compras: list[Any], desde: datetime | None = None, ahora: datetime | None = None
) -> dict[str, Any] | None:
    """La pregunta pendiente más nueva de esta corrida (no vencida)."""
    ahora = ahora or _ahora()
    candidatas = []
    for c in compras or []:
        if not isinstance(c, dict) or c.get("estado") not in ESPERAN_AL_USUARIO:
            continue
        compra_id = c.get("compra_id")
        if not isinstance(compra_id, str) or not 0 < len(compra_id) <= 200:
            continue
        creada, vence = _fecha(c.get("creada")), _fecha(c.get("vence"))
        if vence is not None and vence <= ahora:
            continue
        if desde is not None and (creada is None or creada < desde - TOLERANCIA_RELOJ):
            continue  # de una corrida anterior
        candidatas.append((creada or ahora, c))
    candidatas.sort(key=lambda t: t[0], reverse=True)
    return candidatas[0][1] if candidatas else None


def rutear(
    compras: list[Any], texto: str, desde: datetime | None = None, ahora: datetime | None = None
) -> Ruta:
    compra = compra_pendiente(compras, desde, ahora)
    if compra is None:
        return Ruta("agente")
    respuesta = texto.strip()[:MAX_TEXTO]
    compra_id = compra["compra_id"]
    if compra["estado"] == "esperando_decision":
        if es_afirmativo(respuesta):
            return Ruta("decision", {"compra_id": compra_id, "decision": "aceptar"})
        if es_negativo(respuesta):
            return Ruta("decision", {"compra_id": compra_id, "decision": "rechazar"})
        return Ruta("aclarar")
    intake = compra.get("intake") if isinstance(compra.get("intake"), dict) else {}
    if intake.get("confirmar") is True and es_afirmativo(respuesta):
        return Ruta("confirmar", {"compra_id": compra_id, "confirmar": True})
    if " ".join(_palabras(respuesta)) in _OMITIR:
        return Ruta("omitir", {"compra_id": compra_id, "omitir": True})
    return Ruta("contexto", {"compra_id": compra_id, "respuesta": respuesta})


# ---------------------------------------------------------------------------
# Modo demo: qué paso del sample se contesta solo
# ---------------------------------------------------------------------------


class PasoAuto(NamedTuple):
    paso: str     # billetera | tarjeta | continuar | confirmar | otp
    texto: str    # lo que se le manda al agente
    detalle: str | None = None


def _partes(evento: dict[str, Any]) -> list[dict[str, Any]]:
    contenido = evento.get("content")
    partes = contenido.get("parts") if isinstance(contenido, dict) else None
    return [p for p in partes or [] if isinstance(p, dict)]


def _llamadas(evento: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for p in _partes(evento):
        llamada = p.get("functionCall") or p.get("function_call")
        if isinstance(llamada, dict) and isinstance(llamada.get("name"), str):
            args = llamada.get("args")
            out.append((llamada["name"], args if isinstance(args, dict) else {}))
    return out


def _respuestas(evento: dict[str, Any]) -> list[tuple[str, Any]]:
    out = []
    for p in _partes(evento):
        r = p.get("functionResponse") or p.get("function_response")
        if isinstance(r, dict) and isinstance(r.get("name"), str):
            out.append((r["name"], r.get("response")))
    return out


def _texto(evento: dict[str, Any]) -> str:
    return "".join(
        p["text"] for p in _partes(evento)
        if isinstance(p.get("text"), str) and not p.get("thought")
    ).strip()


def _error_de(evento: dict[str, Any]) -> str:
    partes = [evento.get("errorMessage"), evento.get("error_message")]
    meta = evento.get("customMetadata") or evento.get("custom_metadata")
    if isinstance(meta, dict):
        partes.append(meta.get("error"))
    return " ".join(p for p in partes if isinstance(p, str))


def _menciona_guard(valor: Any) -> bool:
    try:
        return "Pre-Sign Guard" in json.dumps(valor, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


def bloqueo_del_guard(eventos: list[dict[str, Any]]) -> bool:
    """¿El Guard bloqueó o retuvo algo en esta sesión? (el error de la
    herramienta del sample lo dice: `world/NOTICE.md`)."""
    return any(
        "Pre-Sign Guard" in _error_de(e) or any(_menciona_guard(r) for _, r in _respuestas(e))
        for e in eventos
    )


def _ultimo_autor(eventos: list[dict[str, Any]]) -> str | None:
    for e in reversed(eventos):
        if e.get("author") not in (None, "user") and _texto(e):
            return e["author"]
    return None


def _respondio(eventos: list[dict[str, Any]], herramienta: str) -> bool:
    return any(nombre == herramienta for e in eventos for nombre, _ in _respuestas(e))


def _ultima_respuesta(eventos: list[dict[str, Any]], herramientas: set[str]) -> Any:
    for e in reversed(eventos):
        for nombre, respuesta in reversed(_respuestas(e)):
            if nombre in herramientas:
                return respuesta
    return None


def pide_codigo(eventos: list[dict[str, Any]]) -> bool:
    """¿La última respuesta del pago es el desafío (OTP)? La herramienta del
    sample devuelve "Verification required: …" solo en ese caso."""
    respuesta = _ultima_respuesta(eventos, {"initiate_payment", "initiate_payment_with_otp"})
    return respuesta is not None and "Verification required" in json.dumps(respuesta)


def _textos_en(valor: Any) -> list[str]:
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, dict):
        return [t for v in valor.values() for t in _textos_en(v)]
    if isinstance(valor, list):
        return [t for v in valor for t in _textos_en(v)]
    return []


def alias_de_tarjeta(eventos: list[dict[str, Any]]) -> str | None:
    """La tarjeta terminada en 4444 de la lista de la billetera (o la primera)."""
    for e in reversed(eventos):
        for nombre, respuesta in _respuestas(e):
            if nombre != "get_payment_methods":
                continue
            aliases = [t for t in _textos_en(respuesta) if 0 < len(t) <= 120]
            if aliases:
                return next((a for a in aliases if "4444" in a), aliases[0])
    return None


def siguiente_auto(
    estado: dict[str, Any], eventos: list[dict[str, Any]], enviados: Counter
) -> PasoAuto | None:
    """El paso del sample que se contesta solo, o None si le toca al usuario.

    Se decide con el estado de la sesión de ADK y las herramientas que se
    llamaron (quién habló último es el autor del evento, no su texto).
    Antes de que el usuario elija un producto, nunca; después de un bloqueo
    del Guard, nunca.
    """
    if sum(enviados.values()) >= MAX_AUTOS:
        return None
    if estado.get("payment_receipt") or not estado.get("chosen_cart_id"):
        return None
    if bloqueo_del_guard(eventos):
        return None
    autor = _ultimo_autor(eventos)

    def puede(paso: str) -> bool:
        return enviados[paso] < TOPES_AUTO[paso]

    if estado.get("initiate_payment_task_id"):
        # Solo si el pago pidió el código (si falló, no hay nada que contestar).
        return PasoAuto("otp", OTP_DEMO) if pide_codigo(eventos) and puede("otp") else None
    if estado.get("checkout_data") and not estado.get("checkout_mandate_sdjwt"):
        # Solo con el checkout firmado en el estado: si el LLM "dice" que lo
        # creó y no está, no se confirma nada (DEMO.md).
        if autor == "root_agent" and puede("confirmar"):
            return PasoAuto("confirmar", "Sí, confirmo la compra.")
        return None
    seguir = PasoAuto("continuar", "Sí, seguí.")
    if autor == "payment_method_collector" and not estado.get("payment_credential_token"):
        alias = alias_de_tarjeta(eventos)
        if alias is None:
            # Contó su plan y espera antes de pedir la lista: que siga.
            return seguir if puede("continuar") else None
        if not puede("tarjeta"):
            return None
        digitos = re.findall(r"\d{4}", alias)
        return PasoAuto("tarjeta", alias, digitos[-1] if digitos else None)
    direccion = _respondio(eventos, "get_shipping_address")
    if autor == "shipping_address_collector" and not direccion:
        if not puede("billetera"):
            return None
        if enviados["billetera"] == 0:
            return PasoAuto("billetera", "Quiero usar mi billetera digital: Google Wallet.")
        return PasoAuto("billetera", "Sí, está bien.")
    # Después de elegir, el agente principal (o el de la dirección, ya con
    # ella) a veces cuenta su plan y espera: que siga.
    narra = autor == "root_agent" or (autor == "shipping_address_collector" and direccion)
    if narra and not estado.get("checkout_data") and puede("continuar"):
        return seguir
    return None


# ---------------------------------------------------------------------------
# Conversación con el agente de compras (API de adk web)
# ---------------------------------------------------------------------------


class ErrorADK(RuntimeError):
    def __init__(self, codigo: str) -> None:
        super().__init__(codigo)
        self.codigo = codigo


def _cliente_adk(cfg: Config) -> httpx.AsyncClient:
    # read largo: find_products puede quedar retenido en el proxy mientras
    # nuestro agente espera la respuesta del usuario (hasta ~10 min).
    return httpx.AsyncClient(
        transport=cfg.adk_transport,
        timeout=httpx.Timeout(15.0, connect=2.0, read=660.0),
        trust_env=False,
    )


class Conversacion:
    """Una compra de la demo: la sesión de ADK y lo que se dijo.

    Una sola por servidor (la demo tiene una pantalla). `nueva()` empieza otra
    compra: otra sesión de ADK y la línea de tiempo desde cero.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.auto = cfg.demo_auto
        self._ids = itertools.count(1)
        self._generacion = 0
        self._tarea: asyncio.Task | None = None
        self._reiniciar()

    def _reiniciar(self) -> None:
        self.sesion_id: str | None = None
        self.creada = _ahora()
        self.mensajes: list[dict[str, Any]] = []
        self.ocupado = False
        self.fin: str | None = None
        self.enviados: Counter = Counter()

    def nueva(self) -> None:
        self._generacion += 1
        if self._tarea is not None and not self._tarea.done():
            self._tarea.cancel()
        self._tarea = None
        self._reiniciar()

    def estado_json(self) -> dict[str, Any]:
        return {
            "sesion": {"id": self.sesion_id, "creada": _iso(self.creada)},
            "ocupado": self.ocupado,
            "auto": self.auto,
            "fin": self.fin,
            "mensajes": self.mensajes,
            "escenario": escenario_activo(self.cfg),
        }

    def registrar(self, tipo: str, **datos: Any) -> dict[str, Any]:
        mensaje = {"id": f"m-{next(self._ids)}", "ts": _iso(_ahora()), "tipo": tipo, **datos}
        self.mensajes.append(mensaje)
        del self.mensajes[:-MAX_MENSAJES]
        return mensaje

    def iniciar(self, texto: str) -> None:
        """Le manda el texto al agente en segundo plano (puede tardar minutos)."""
        self.ocupado = True
        self.registrar("usuario", texto=texto, destino="agente")
        self._tarea = asyncio.create_task(self._correr(texto, self._generacion))

    # -- ADK -----------------------------------------------------------------

    def _url(self, ruta: str) -> str:
        return f"{self.cfg.adk_url}{ruta}"

    @property
    def _ruta_sesiones(self) -> str:
        return f"/apps/{ADK_APP}/users/{ADK_USUARIO}/sessions"

    async def _crear_sesion(self, cliente: httpx.AsyncClient) -> str:
        r = await cliente.post(self._url(self._ruta_sesiones), json={})
        if r.status_code != 200:
            raise ErrorADK("adk_error")
        sesion_id = r.json().get("id")
        if not isinstance(sesion_id, str) or not sesion_id:
            raise ErrorADK("adk_error")
        return sesion_id

    async def _leer_sesion(self, cliente: httpx.AsyncClient) -> tuple[dict, list]:
        r = await cliente.get(self._url(f"{self._ruta_sesiones}/{self.sesion_id}"))
        if r.status_code != 200:
            raise ErrorADK("adk_error")
        datos = r.json()
        estado = datos.get("state") if isinstance(datos.get("state"), dict) else {}
        eventos = [e for e in datos.get("events") or [] if isinstance(e, dict)]
        return estado, eventos

    async def _turno(self, cliente: httpx.AsyncClient, texto: str, generacion: int) -> list[tuple[str, str]]:
        """Un turno del agente. Las herramientas se publican al llegar (el
        diagrama las anima); los textos, al final del turno, cuando ya se sabe
        si el modo demo contesta ese paso."""
        cuerpo = {
            "appName": ADK_APP, "userId": ADK_USUARIO, "sessionId": self.sesion_id,
            "newMessage": {"role": "user", "parts": [{"text": texto}]}, "streaming": False,
        }
        textos: list[tuple[str, str]] = []
        async with cliente.stream("POST", self._url("/run_sse"), json=cuerpo) as r:
            if r.status_code != 200:
                raise ErrorADK("adk_error")
            async for linea in r.aiter_lines():
                if not linea.startswith("data:"):
                    continue
                try:
                    evento = json.loads(linea[5:].strip())
                except ValueError:
                    continue
                if generacion != self._generacion:
                    return []
                if isinstance(evento, dict):
                    self._ingerir(evento, textos)
        return textos

    def _ingerir(self, evento: dict[str, Any], textos: list[tuple[str, str]]) -> None:
        if set(evento) == {"error"}:  # el error de adk web, fuera de un evento
            raise ErrorADK("adk_error")
        if evento.get("partial"):
            return
        error = _error_de(evento)
        if error:
            guard = "Pre-Sign Guard" in error
            ultimo = self.mensajes[-1] if self.mensajes else {}
            if not (ultimo.get("tipo") == "error_agente" and ultimo.get("guard") == guard):
                self.registrar("error_agente", guard=guard)  # sin el texto: puede traer datos del comercio
        for nombre, args in _llamadas(evento):
            if nombre in HERRAMIENTAS_VISIBLES:
                visibles = {
                    k: args[k][:MAX_ARG] for k in HERRAMIENTAS_VISIBLES[nombre]
                    if isinstance(args.get(k), str)
                }
                self.registrar("herramienta", nombre=nombre, args=visibles)
        texto = _texto(evento)
        if texto and evento.get("author") not in (None, "user"):
            textos.append((str(evento["author"])[:64], texto[:MAX_TEXTO_AGENTE]))

    async def _correr(self, texto: str, generacion: int) -> None:
        try:
            async with _cliente_adk(self.cfg) as cliente:
                if self.sesion_id is None:
                    self.sesion_id = await self._crear_sesion(cliente)
                actual = texto
                while True:
                    textos = await self._turno(cliente, actual, generacion)
                    estado, eventos = await self._leer_sesion(cliente)
                    if generacion != self._generacion:
                        return
                    paso = siguiente_auto(estado, eventos, self.enviados) if self.auto else None
                    # En modo demo, después de elegir el producto, la narración
                    # del cobro no aporta: se ve solo lo último que dice el
                    # agente cuando espera al usuario.
                    narracion = self.auto and bool(estado.get("chosen_cart_id"))
                    for i, (autor, t) in enumerate(textos):
                        ultimo = i == len(textos) - 1
                        oculto = paso is not None or (narracion and not ultimo)
                        self.registrar("agente", texto=t, autor=autor, oculto=oculto)
                    if estado.get("payment_receipt") and self.fin != "pagado":
                        self.fin = "pagado"
                        cart_id = estado.get("chosen_cart_id")
                        self.registrar("pago", cart_id=cart_id if isinstance(cart_id, str) else None)
                    if paso is None:
                        return
                    if self.cfg.demo_pausa_auto_s > 0:
                        # Plan gratuito de Gemini: espaciar los turnos.
                        await asyncio.sleep(self.cfg.demo_pausa_auto_s)
                        if generacion != self._generacion:
                            return
                    self.enviados[paso.paso] += 1
                    self.registrar("auto", paso=paso.paso, texto=paso.texto, detalle=paso.detalle)
                    actual = paso.texto
        except asyncio.CancelledError:
            raise
        except ErrorADK as e:
            if generacion == self._generacion:
                self.registrar("error", codigo=e.codigo)
        except httpx.HTTPError as e:
            if generacion == self._generacion:
                conexion = isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
                self.registrar("error", codigo="adk_no_responde" if conexion else "adk_error")
        except ValueError:
            if generacion == self._generacion:
                self.registrar("error", codigo="adk_error")
        finally:
            if generacion == self._generacion:
                self.ocupado = False


# ---------------------------------------------------------------------------
# Escenario del comercio
# ---------------------------------------------------------------------------


def _carpeta_escenario(cfg: Config) -> Path:
    return cfg.carpeta_world_temp


def escenario_activo(cfg: Config) -> str | None:
    try:
        nombre = (_carpeta_escenario(cfg) / ARCHIVO_ESCENARIO).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return nombre if nombre in ESCENARIOS else (nombre[:32] or None)


def _escenarios_del_mundo() -> set[str] | None:
    """Los escenarios que conoce el comercio (demo_world, solo lectura)."""
    try:
        from demo_world import scenario
    except ImportError:
        return None
    try:
        return set(scenario.scenario_names())
    except (OSError, ValueError, KeyError):
        return None


def fijar_escenario(cfg: Config, nombre: str) -> str:
    """Escribe el escenario activo donde lo lee el comercio en cada búsqueda
    (`$TEMP_DB_DIR/demo_scenario`, el mismo archivo que `set_scenario`)."""
    del_mundo = _escenarios_del_mundo()
    if nombre not in ESCENARIOS or (del_mundo is not None and nombre not in del_mundo):
        raise ValueError(nombre)
    carpeta = _carpeta_escenario(cfg)
    carpeta.mkdir(parents=True, exist_ok=True)
    temporal = carpeta / f".{ARCHIVO_ESCENARIO}.tmp"
    temporal.write_text(nombre, encoding="utf-8")
    os.replace(temporal, carpeta / ARCHIVO_ESCENARIO)
    return nombre


def _pedido_de_ejemplo(cfg: Config, escenario: str) -> str | None:
    for path in (
        cfg.fixtures_dir / f"scenario_{escenario}.json",
        cfg.fixtures_dir / "etapa2" / f"{escenario}.json",
    ):
        datos = _leer_json(path)
        intent = datos.get("intent") if isinstance(datos, dict) else None
        pedido = intent.get("user_request") if isinstance(intent, dict) else None
        if isinstance(pedido, str) and pedido.strip():
            return pedido.strip()[:MAX_TEXTO]
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _conversacion(request: Request) -> Conversacion:
    return request.app.state.demo


async def _cuerpo_con(request: Request, claves: set[str]) -> dict[str, Any] | None:
    try:
        cuerpo = await request.json()
    except ValueError:
        return None
    return cuerpo if isinstance(cuerpo, dict) and set(cuerpo) == claves else None


async def pagina_demo(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "demo.html", headers={"Cache-Control": "no-cache"})


async def api_demo_estado(request: Request) -> Response:
    return JSONResponse(_conversacion(request).estado_json())


async def api_demo_escenarios(request: Request) -> Response:
    cfg: Config = request.app.state.cfg
    grupos = [
        {
            "categoria": categoria,
            "opciones": [
                {"id": e, "etiqueta": etiqueta, "pedido": _pedido_de_ejemplo(cfg, e)}
                for e, etiqueta in opciones
            ],
        }
        for categoria, opciones in GRUPOS
    ]
    return JSONResponse({"activo": escenario_activo(cfg), "grupos": grupos})


async def api_demo_escenario(request: Request) -> Response:
    cuerpo = await _cuerpo_con(request, {"escenario"})
    if cuerpo is None or not isinstance(cuerpo["escenario"], str):
        return _error(400, "se_espera", formato={"escenario": sorted(ESCENARIOS)})
    try:
        activo = fijar_escenario(request.app.state.cfg, cuerpo["escenario"])
    except ValueError:
        return _error(400, "escenario_desconocido", opciones=sorted(ESCENARIOS))
    except OSError:
        return _error(500, "no_se_pudo_escribir_el_escenario")
    _conversacion(request).nueva()  # otro comercio: otra compra
    return JSONResponse({"activo": activo})


async def api_demo_nueva(request: Request) -> Response:
    try:
        cuerpo = await request.json()
    except ValueError:
        cuerpo = None
    if not isinstance(cuerpo, dict):
        return _error(400, "se_espera", formato={})
    conversacion = _conversacion(request)
    conversacion.nueva()
    return JSONResponse(conversacion.estado_json())


async def api_demo_auto(request: Request) -> Response:
    cuerpo = await _cuerpo_con(request, {"auto"})
    if cuerpo is None or not isinstance(cuerpo["auto"], bool):
        return _error(400, "se_espera", formato={"auto": "bool"})
    conversacion = _conversacion(request)
    conversacion.auto = cuerpo["auto"]
    return JSONResponse({"auto": conversacion.auto})


async def _compras_del_proxy(cfg: Config) -> list[Any]:
    """Las compras de nuestro agente; sin proxy, ninguna (nada pendiente)."""
    try:
        async with _cliente_proxy(cfg) as cliente:
            r = await cliente.get(f"{cfg.proxy_url}/guard/compras")
        datos = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return []
    compras = datos.get("compras") if isinstance(datos, dict) else None
    return compras if isinstance(compras, list) else []


RUTAS_PROXY = {
    "contexto": "/guard/compras/contexto",
    "confirmar": "/guard/compras/contexto",
    "omitir": "/guard/compras/contexto",
    "decision": "/guard/compras/decision",
}


async def api_demo_mensaje(request: Request) -> Response:
    """El input único: a nuestro agente si tiene una pregunta pendiente de
    esta corrida; si no, al agente de compras."""
    cfg: Config = request.app.state.cfg
    cuerpo = await _cuerpo_con(request, {"texto"})
    texto = cuerpo["texto"].strip() if cuerpo and isinstance(cuerpo["texto"], str) else ""
    if not 0 < len(texto) <= MAX_TEXTO:
        return _error(400, "se_espera", formato={"texto": f"str (1..{MAX_TEXTO})"})
    conversacion = _conversacion(request)
    ruta = rutear(await _compras_del_proxy(cfg), texto, desde=conversacion.creada)

    if ruta.destino == "aclarar":
        return _error(422, "se_espera_si_o_no", destino="decision")
    if ruta.destino in RUTAS_PROXY:
        respuesta = await _reenviar_json(cfg, "POST", RUTAS_PROXY[ruta.destino], cuerpo=ruta.cuerpo)
        salida: dict[str, Any] = {"destino": ruta.destino}
        if respuesta.status_code == 200:
            conversacion.registrar("usuario", texto=texto, destino=ruta.destino)
        else:
            try:
                datos = json.loads(bytes(respuesta.body))
            except ValueError:
                datos = {}
            if isinstance(datos, dict):
                salida.update({k: datos[k] for k in ("error", "estado", "detalle") if k in datos})
        return JSONResponse(salida, status_code=respuesta.status_code)

    if conversacion.ocupado:
        return _error(409, "agente_ocupado", destino="agente")
    conversacion.iniciar(texto)
    return JSONResponse({"destino": "agente"}, status_code=202)
