"""Servidor del dashboard (Starlette, puerto 8020).

Sirve los estáticos de `dashboard/static/` (HTML + CSS + JS vanilla, sin build)
y una API chica de solo lectura sobre los datos guardados del repo:

- resultados del benchmark (`results/` y `world/ap2/results/`),
- fixtures y catálogo público (`fixtures/`),
- eventos guardados para replay (`.logs/guard_events.jsonl` y
  `dashboard/demo/*.jsonl`).

Además reenvía al proxy del Guard (`PROXY_URL`, por defecto :8010) las
llamadas `/guard/*` desde el mismo origen, para que el navegador no dependa de
que el proxy tenga CORS. El dashboard no decide nada: muestra y reenvía la
decisión del humano ("Aprobar igual") cuando el Guard escala, y lo que el
usuario le contesta a nuestro agente (el porqué del pedido, aceptar o
rechazar una oportunidad).

Como es la puerta al proxy, la API (`/api/*`) solo atiende `Host`
localhost/127.0.0.1 con su puerto, y los POST exigen JSON y el `Origin` del
propio panel (ver `SoloLocal`). Los POST que reenvía al proxy llevan el
token de su API de control (`X-Guard-Token`), que el proxy escribe en
`.temp-db/guard_api_token` en cada arranque: lo lee el servidor, nunca el
navegador.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import mimetypes
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from contracts import Evento, Ficha

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# 127.0.0.1 y no localhost: en Windows localhost prueba primero ::1, donde el
# proxy no escucha, y cada intento fallido suma ~2 s (el panel daba "proxy
# desconectado"). El proxy acepta Host 127.0.0.1:8010.
DEFAULT_PROXY_URL = "http://127.0.0.1:8010"
DEFAULT_PORT = 8020
GUARD_KID = "guard-key-1"  # CLAUDE.md → Decisiones, punto 6
# Token de la API de control del proxy (N4): lo escribe el proxy al arrancar.
TOKEN_RELATIVO = Path(".temp-db") / "guard_api_token"
HEADER_TOKEN = "X-Guard-Token"
_TOKEN_VALIDO = re.compile(r"[A-Za-z0-9_-]{32,256}")

# En Windows, mimetypes lee el registro y a veces sirve .js como text/plain:
# el navegador rechaza los módulos ES con ese tipo. Lo fijamos acá.
for _ext, _tipo in (
    (".js", "text/javascript"),
    (".mjs", "text/javascript"),
    (".css", "text/css"),
    (".svg", "image/svg+xml"),
    (".json", "application/json"),
):
    mimetypes.add_type(_tipo, _ext)

# Orden de la demo (DEMO.md → escenarios). Los desconocidos van al final.
ORDEN_ESCENARIOS = ["baseline", "A", "B", "B2", "C"]

# Nombres de archivo aceptados en la URL. Además se buscan en una lista
# blanca armada listando las carpetas: nunca se arma una ruta con lo que
# llega del cliente.
_NOMBRE_SEGURO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ESCENARIO_SEGURO = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# Origen de los resultados del benchmark, en orden de preferencia.
ORIGENES_RESULTADOS = {"results": "results", "world": "world/ap2/results"}

REPLAY_LOG = "guard_events.jsonl"


@dataclass(frozen=True)
class Config:
    repo_root: Path = REPO_ROOT
    proxy_url: str = DEFAULT_PROXY_URL
    port: int = DEFAULT_PORT
    # Archivo con el token del proxy (None: <repo_root>/.temp-db/guard_api_token).
    token_path: Path | None = None
    # Solo para tests: transporte de httpx hacia el proxy.
    proxy_transport: httpx.AsyncBaseTransport | None = field(
        default=None, compare=False
    )

    @property
    def origenes(self) -> tuple[str, ...]:
        """Orígenes del propio panel (los únicos que pueden hacer POST)."""
        return (f"http://localhost:{self.port}", f"http://127.0.0.1:{self.port}")

    @property
    def hosts(self) -> frozenset[str]:
        """`Host` aceptados en /api/* (protege del DNS rebinding)."""
        return frozenset({f"localhost:{self.port}", f"127.0.0.1:{self.port}"})

    @property
    def fixtures_dir(self) -> Path:
        return self.repo_root / "fixtures"

    @property
    def log_eventos(self) -> Path:
        return self.repo_root / ".logs" / REPLAY_LOG

    @property
    def demo_dir(self) -> Path:
        return self.repo_root / "dashboard" / "demo"

    @property
    def archivo_token(self) -> Path:
        return self.token_path or self.repo_root / TOKEN_RELATIVO

    @property
    def clave_guard_pem(self) -> Path:
        return self.repo_root / ".temp-db" / "guard_signing_key.pem"

    def carpeta_resultados(self, origen: str) -> Path:
        return self.repo_root / ORIGENES_RESULTADOS[origen]


def config_desde_entorno() -> Config:
    token = os.environ.get("GUARD_API_TOKEN_PATH")
    token_path = Path(token) if token else REPO_ROOT / TOKEN_RELATIVO
    if not token_path.is_absolute():
        token_path = REPO_ROOT / token_path
    return Config(
        proxy_url=os.environ.get("PROXY_URL", DEFAULT_PROXY_URL).rstrip("/"),
        port=int(os.environ.get("DASHBOARD_PORT", DEFAULT_PORT)),
        token_path=token_path,
    )


def verificar_host_local(host: str, permitir_remoto: bool) -> None:
    """El panel muestra mandatos y checkouts completos y reenvía el
    interruptor y las aprobaciones al proxy: solo escucha en loopback salvo
    DASHBOARD_ALLOW_REMOTE=1."""
    if permitir_remoto or host.strip().lower() == "localhost":
        return
    try:
        if ipaddress.ip_address(host.strip().strip("[]")).is_loopback:
            return
    except ValueError:
        pass
    raise SystemExit(
        f"DASHBOARD_HOST={host!r} no es una interfaz local. Si de verdad hace "
        "falta escuchar en la red, definí DASHBOARD_ALLOW_REMOTE=1."
    )


# Rutas que cambian estado en el proxy (solo con JSON y desde el propio panel).
RUTAS_POST = frozenset({
    "/api/proxy/guard/state",
    "/api/proxy/guard/aprobaciones",
    "/api/proxy/guard/compras/contexto",
    "/api/proxy/guard/compras/decision",
})


class SoloLocal:
    """La API del panel es la puerta al proxy: misma protección que /guard/*.

    - `Host` de /api/* tiene que ser localhost/127.0.0.1 con el puerto del
      panel (403): frena el DNS rebinding.
    - Los POST exigen el `Origin` del panel (403 si falta o es otro; el
      navegador siempre lo manda en un POST) y, si viene `Sec-Fetch-Site`,
      que sea `same-origin`: el panel agrega el token del proxy, así que solo
      reenvía pedidos de su propia página, no de curl ni de una herramienta
      HTTP del agente (N4).
    - Los POST exigen `Content-Type: application/json` (415).

    ASGI puro: no interfiere con el SSE reenviado.
    """

    def __init__(self, app: ASGIApp, cfg: Config) -> None:
        self.app = app
        self.cfg = cfg

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            rechazo = self._rechazo(scope)
            if rechazo is not None:
                await rechazo(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _rechazo(self, scope: Scope) -> Response | None:
        headers = Headers(scope=scope)
        if headers.get("host", "").strip().lower() not in self.cfg.hosts:
            return _error(403, "host_no_permitido")
        if scope["method"] != "POST" or scope["path"] not in RUTAS_POST:
            return None
        origen = headers.get("origin")
        if origen is None:
            return _error(403, "origen_requerido")
        if origen not in self.cfg.origenes:
            return _error(403, "origen_no_permitido")
        sitio = headers.get("sec-fetch-site")
        if sitio is not None and sitio != "same-origin":
            return _error(403, "origen_no_permitido")
        tipo = headers.get("content-type", "").split(";")[0].strip().lower()
        if tipo != "application/json":
            return _error(415, "se_espera_json")
        return None


def _error(status: int, codigo: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": codigo, **extra}, status_code=status)


def _leer_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fecha_iso(texto: Any) -> datetime | None:
    if not isinstance(texto, str):
        return None
    try:
        fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Resultados del benchmark
# ---------------------------------------------------------------------------


def listar_resultados(cfg: Config) -> list[dict[str, Any]]:
    """Archivos de resultados válidos, del más nuevo al más viejo.

    La fecha es `meta.started_at` (la que escribe benchmark.py); si falta, la
    fecha de modificación del archivo. A igual fecha, `results/` va primero.
    """
    items: list[tuple[datetime, int, dict[str, Any]]] = []
    for prioridad, origen in enumerate(ORIGENES_RESULTADOS):
        carpeta = cfg.carpeta_resultados(origen)
        if not carpeta.is_dir():
            continue
        for path in carpeta.glob("*.json"):
            if not path.is_file() or not _NOMBRE_SEGURO.match(path.name):
                continue
            datos = _leer_json(path)
            if not isinstance(datos, dict) or not isinstance(
                datos.get("scenarios"), dict
            ):
                continue
            meta = datos.get("meta") if isinstance(datos.get("meta"), dict) else {}
            modificado = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            fecha = _fecha_iso(meta.get("started_at")) or modificado
            info = {
                "id": f"{origen}/{path.name}",
                "origen": origen,
                "carpeta": ORIGENES_RESULTADOS[origen],
                "nombre": path.name,
                "fecha": fecha.isoformat(),
                "fecha_de": "meta.started_at"
                if _fecha_iso(meta.get("started_at"))
                else "archivo",
                "guard": meta.get("guard"),
                "model": meta.get("model"),
                "decider": meta.get("decider"),
                "source": meta.get("source"),
                "trials": meta.get("trials"),
                "escenarios": list(datos["scenarios"].keys()),
            }
            items.append((fecha, prioridad, info))
    items.sort(key=lambda t: (-t[0].timestamp(), t[1]))
    return [info for _, _, info in items]


def elegir_resultado(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """El más nuevo con `meta.guard` no nulo; si no hay, el más nuevo."""
    con_guard = [i for i in items if i.get("guard")]
    candidatos = con_guard or items
    return candidatos[0] if candidatos else None


def _cargar_resultado(cfg: Config, info: dict[str, Any]) -> dict[str, Any] | None:
    path = cfg.carpeta_resultados(info["origen"]) / info["nombre"]
    datos = _leer_json(path)
    if not isinstance(datos, dict):
        return None
    datos["_archivo"] = info
    return datos


async def api_resultados(request: Request) -> Response:
    return JSONResponse(listar_resultados(request.app.state.cfg))


async def api_resultado_ultimo(request: Request) -> Response:
    cfg: Config = request.app.state.cfg
    items = listar_resultados(cfg)
    info = elegir_resultado(items)
    if info is None:
        return _error(
            404,
            "sin_resultados",
            buscado_en=list(ORIGENES_RESULTADOS.values()),
        )
    datos = _cargar_resultado(cfg, info)
    if datos is None:
        return _error(500, "resultado_ilegible", archivo=info["id"])
    return JSONResponse(datos)


async def api_resultado(request: Request) -> Response:
    cfg: Config = request.app.state.cfg
    origen = request.path_params["origen"]
    nombre = request.path_params["nombre"]
    if origen not in ORIGENES_RESULTADOS or not _NOMBRE_SEGURO.match(nombre):
        return _error(400, "nombre_invalido")
    info = next(
        (i for i in listar_resultados(cfg) if i["id"] == f"{origen}/{nombre}"),
        None,
    )
    if info is None:
        return _error(404, "no_encontrado")
    datos = _cargar_resultado(cfg, info)
    if datos is None:
        return _error(500, "resultado_ilegible", archivo=info["id"])
    return JSONResponse(datos)


# ---------------------------------------------------------------------------
# Fixtures y catálogo público
# ---------------------------------------------------------------------------


def _fixtures(cfg: Config) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not cfg.fixtures_dir.is_dir():
        return out
    for path in cfg.fixtures_dir.glob("scenario_*.json"):
        nombre = path.stem.removeprefix("scenario_")
        if path.is_file() and _ESCENARIO_SEGURO.match(nombre):
            out[nombre] = path
    return out


def _orden_escenario(nombre: str) -> tuple[int, str]:
    if nombre in ORDEN_ESCENARIOS:
        return (ORDEN_ESCENARIOS.index(nombre), nombre)
    return (len(ORDEN_ESCENARIOS), nombre)


def _fixture(cfg: Config, escenario: str) -> dict[str, Any] | None:
    if not _ESCENARIO_SEGURO.match(escenario):
        return None
    path = _fixtures(cfg).get(escenario)
    if path is None:
        return None
    datos = _leer_json(path)
    return datos if isinstance(datos, dict) else None


async def api_fixtures(request: Request) -> Response:
    cfg: Config = request.app.state.cfg
    lista = []
    for nombre in sorted(_fixtures(cfg), key=_orden_escenario):
        datos = _fixture(cfg, nombre) or {}
        lista.append(
            {
                "escenario": nombre,
                "title": datos.get("title"),
                "description": datos.get("description"),
                "attack": datos.get("attack"),
                "expected": datos.get("expected"),
            }
        )
    return JSONResponse(lista)


async def api_fixture(request: Request) -> Response:
    escenario = request.path_params["escenario"]
    if not _ESCENARIO_SEGURO.match(escenario):
        return _error(400, "nombre_invalido")
    datos = _fixture(request.app.state.cfg, escenario)
    if datos is None:
        return _error(404, "no_encontrado", escenario=escenario)
    return JSONResponse(datos)


async def api_fixture_ficha(request: Request) -> Response:
    """La ficha del escenario, armada con el contrato (`Ficha.desde_intent`)."""
    escenario = request.path_params["escenario"]
    if not _ESCENARIO_SEGURO.match(escenario):
        return _error(400, "nombre_invalido")
    datos = _fixture(request.app.state.cfg, escenario)
    if datos is None or not isinstance(datos.get("intent"), dict):
        return _error(404, "no_encontrado", escenario=escenario)
    try:
        ficha = Ficha.desde_intent(datos["intent"])
    except (ValueError, ValidationError) as e:
        return _error(422, "intent_invalido", detalle=str(e))
    return JSONResponse(ficha.model_dump(mode="json"))


async def api_catalogo(request: Request) -> Response:
    cfg: Config = request.app.state.cfg
    datos = _leer_json(cfg.fixtures_dir / "public_catalog.json")
    if not isinstance(datos, dict):
        return _error(404, "sin_catalogo")
    return JSONResponse(datos)


# ---------------------------------------------------------------------------
# Replay de eventos guardados
# ---------------------------------------------------------------------------


def _replays(cfg: Config) -> dict[str, tuple[Path, str]]:
    """nombre → (ruta, origen). Lista blanca: solo lo que hay en disco."""
    out: dict[str, tuple[Path, str]] = {}
    if cfg.log_eventos.is_file():
        out[REPLAY_LOG] = (cfg.log_eventos, ".logs")
    if cfg.demo_dir.is_dir():
        for path in sorted(cfg.demo_dir.glob("*.jsonl")):
            if (
                path.is_file()
                and _NOMBRE_SEGURO.match(path.name)
                and path.name not in out
            ):
                out[path.name] = (path, "dashboard/demo")
    return out


def leer_eventos(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Eventos válidos según el contrato y cantidad de líneas descartadas."""
    eventos: list[dict[str, Any]] = []
    invalidas = 0
    try:
        texto = path.read_text(encoding="utf-8")
    except OSError:
        return [], 0
    for linea in texto.splitlines():
        if not linea.strip():
            continue
        try:
            evento = Evento.model_validate_json(linea)
        except ValidationError:
            invalidas += 1
            continue
        eventos.append(evento.model_dump(mode="json"))
    return eventos, invalidas


async def api_replays(request: Request) -> Response:
    cfg: Config = request.app.state.cfg
    lista = []
    for nombre, (path, origen) in _replays(cfg).items():
        eventos, invalidas = leer_eventos(path)
        stat = path.stat()
        lista.append(
            {
                "nombre": nombre,
                "origen": origen,
                "eventos": len(eventos),
                "invalidas": invalidas,
                "compras": len(
                    {e["contexto_id"] for e in eventos if e.get("contexto_id")}
                ),
                "escenarios": sorted(
                    {e["escenario"] for e in eventos if e.get("escenario")},
                    key=_orden_escenario,
                ),
                "bytes": stat.st_size,
                "modificado": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
            }
        )
    return JSONResponse(lista)


async def api_replay(request: Request) -> Response:
    nombre = request.path_params["nombre"]
    if not _NOMBRE_SEGURO.match(nombre) or not nombre.endswith(".jsonl"):
        return _error(400, "nombre_invalido")
    entrada = _replays(request.app.state.cfg).get(nombre)
    if entrada is None:
        return _error(404, "no_encontrado")
    eventos, invalidas = leer_eventos(entrada[0])
    return JSONResponse(eventos, headers={"X-Lineas-Invalidas": str(invalidas)})


# ---------------------------------------------------------------------------
# Clave pública del Guard (para verificar el recibo en el navegador)
# ---------------------------------------------------------------------------


def _b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def jwk_publica_desde_pem(pem: bytes) -> dict[str, str]:
    """JWK pública (EC P-256) a partir de una clave PEM privada o pública.

    Solo se expone la parte pública.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        clave = serialization.load_pem_private_key(pem, password=None)
        publica = clave.public_key()
    except (ValueError, TypeError):
        publica = serialization.load_pem_public_key(pem)
    if not isinstance(publica, ec.EllipticCurvePublicKey) or not isinstance(
        publica.curve, ec.SECP256R1
    ):
        raise ValueError("La clave del Guard no es EC P-256")
    numeros = publica.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numeros.x.to_bytes(32, "big")),
        "y": _b64url(numeros.y.to_bytes(32, "big")),
    }


def _clave_local(cfg: Config) -> dict[str, Any] | None:
    if cfg.clave_guard_pem.is_file():
        try:
            jwk = jwk_publica_desde_pem(cfg.clave_guard_pem.read_bytes())
        except (OSError, ValueError, TypeError):
            jwk = None
        if jwk:
            return {
                "kid": GUARD_KID,
                "jwk": jwk,
                "origen": ".temp-db/guard_signing_key.pem (parte pública)",
            }
    guardada = _leer_json(cfg.demo_dir / "guard_public_key.json")
    if isinstance(guardada, dict) and isinstance(guardada.get("jwk"), dict):
        return {
            "kid": guardada.get("kid", GUARD_KID),
            "jwk": guardada["jwk"],
            "origen": "dashboard/demo/guard_public_key.json",
        }
    return None


async def api_clave_publica(request: Request) -> Response:
    """Primero la del proxy; si no responde, la local (plan B offline)."""
    cfg: Config = request.app.state.cfg
    detalle_proxy = None
    try:
        async with _cliente_proxy(cfg) as cliente:
            r = await cliente.get(f"{cfg.proxy_url}/guard/public-key")
        if r.status_code == 200:
            datos = r.json()
            if isinstance(datos, dict) and isinstance(datos.get("jwk"), dict):
                return JSONResponse({**datos, "origen": "proxy"})
        detalle_proxy = f"HTTP {r.status_code}"
    except (httpx.HTTPError, ValueError) as e:
        detalle_proxy = type(e).__name__
    local = _clave_local(cfg)
    if local is not None:
        return JSONResponse({**local, "proxy": detalle_proxy})
    return _error(404, "sin_clave", proxy=detalle_proxy)


# ---------------------------------------------------------------------------
# Reenvío al proxy del Guard (mismo origen para el navegador)
# ---------------------------------------------------------------------------


def _cliente_proxy(cfg: Config, timeout: httpx.Timeout | None = None):
    # En Windows, conectar a un puerto cerrado tarda ~2 s en fallar: cortamos
    # antes para que el panel muestre "proxy desconectado" enseguida.
    timeout = timeout or httpx.Timeout(3.0, connect=1.0)
    # trust_env=False: un HTTP_PROXY del entorno no se mete entre el panel y el proxy.
    return httpx.AsyncClient(transport=cfg.proxy_transport, timeout=timeout, trust_env=False)


def leer_token(cfg: Config) -> str | None:
    """El token de la API de control del proxy, o None si no está o no tiene
    la forma de uno (`secrets.token_urlsafe`). Se lee en cada POST: si el
    proxy se reinicia con otro token, el panel lo toma sin reiniciarse."""
    try:
        with open(cfg.archivo_token, "rb") as f:
            crudo = f.read(1025)
    except OSError:
        return None
    try:
        token = crudo.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return token if len(crudo) <= 1024 and _TOKEN_VALIDO.fullmatch(token) else None


def _proxy_desconectado(cfg: Config, e: Exception) -> JSONResponse:
    return _error(
        502,
        "proxy_desconectado",
        proxy_url=cfg.proxy_url,
        detalle=type(e).__name__,
    )


async def _reenviar_json(
    cfg: Config,
    metodo: str,
    ruta: str,
    *,
    cuerpo: Any = None,
    params: dict[str, Any] | None = None,
) -> Response:
    headers: dict[str, str] = {}
    if metodo != "GET":
        # Los POST de /guard/* exigen el token del proxy (N4). Lo agrega el
        # servidor: el navegador nunca lo ve.
        token = leer_token(cfg)
        if token is None:
            return _error(
                503,
                "sin_token_del_proxy",
                detalle="No se encontró el token de la API del proxy "
                f"({TOKEN_RELATIVO.as_posix()}): ¿está levantado el proxy?",
            )
        headers[HEADER_TOKEN] = token
    try:
        async with _cliente_proxy(cfg) as cliente:
            r = await cliente.request(
                metodo, f"{cfg.proxy_url}{ruta}", json=cuerpo, params=params,
                headers=headers,
            )
    except httpx.HTTPError as e:
        return _proxy_desconectado(cfg, e)
    try:
        datos = r.json()
    except ValueError:
        return _error(502, "respuesta_invalida", status=r.status_code)
    return JSONResponse(datos, status_code=r.status_code)


async def api_proxy_estado(request: Request) -> Response:
    cfg: Config = request.app.state.cfg
    if request.method == "GET":
        return await _reenviar_json(cfg, "GET", "/guard/state")
    try:
        cuerpo = await request.json()
    except ValueError:
        return _error(400, "json_invalido")
    if (
        not isinstance(cuerpo, dict)
        or set(cuerpo) != {"guard_activo"}
        or not isinstance(cuerpo["guard_activo"], bool)
    ):
        return _error(400, "se_espera", formato={"guard_activo": "bool"})
    return await _reenviar_json(cfg, "POST", "/guard/state", cuerpo=cuerpo)


_MAX_ID = 200


async def api_proxy_aprobaciones(request: Request) -> Response:
    """Aprobación humana de un checkout retenido (`escalar`): "Aprobar igual".

    El panel no decide: solo reenvía la decisión del humano al proxy, que la
    ata al contexto, al cart y al monto del último `escalar`.
    """
    cfg: Config = request.app.state.cfg
    if request.method == "GET":
        return await _reenviar_json(cfg, "GET", "/guard/aprobaciones")
    try:
        cuerpo = await request.json()
    except ValueError:
        return _error(400, "json_invalido")
    if (
        not isinstance(cuerpo, dict)
        or set(cuerpo) != {"contexto_id", "cart_id"}
        or not all(
            isinstance(cuerpo[k], str) and 0 < len(cuerpo[k]) <= _MAX_ID
            for k in ("contexto_id", "cart_id")
        )
    ):
        return _error(400, "se_espera", formato={"contexto_id": "str", "cart_id": "str"})
    return await _reenviar_json(cfg, "POST", "/guard/aprobaciones", cuerpo=cuerpo)


# ---------------------------------------------------------------------------
# Nuestro agente: el porqué del pedido y la consulta de una oportunidad
# (docs/FLUJO.md, pasos 1, 5 y 6). El panel no decide: reenvía lo que
# contesta el usuario.
# ---------------------------------------------------------------------------

_MAX_RESPUESTA = 1000
DECISIONES = frozenset({"aceptar", "rechazar"})


def _compra_id_valido(valor: Any) -> bool:
    return isinstance(valor, str) and 0 < len(valor) <= _MAX_ID


def cuerpo_contexto_valido(cuerpo: Any) -> bool:
    """Exactamente `{"compra_id", "respuesta"}` (1..1000 caracteres sin los
    espacios de los bordes), `{"compra_id", "omitir": true}` o, en el turno
    de confirmación del intake, `{"compra_id", "confirmar": true}`."""
    if not isinstance(cuerpo, dict) or not _compra_id_valido(cuerpo.get("compra_id")):
        return False
    claves = set(cuerpo)
    if claves == {"compra_id", "respuesta"}:
        respuesta = cuerpo["respuesta"]
        return isinstance(respuesta, str) and 0 < len(respuesta.strip()) <= _MAX_RESPUESTA
    if claves == {"compra_id", "omitir"}:
        return cuerpo["omitir"] is True
    if claves == {"compra_id", "confirmar"}:
        return cuerpo["confirmar"] is True
    return False


def cuerpo_decision_valido(cuerpo: Any) -> bool:
    """Exactamente `{"compra_id", "decision": "aceptar" | "rechazar"}`."""
    return (
        isinstance(cuerpo, dict)
        and set(cuerpo) == {"compra_id", "decision"}
        and _compra_id_valido(cuerpo["compra_id"])
        and isinstance(cuerpo["decision"], str)
        and cuerpo["decision"] in DECISIONES
    )


async def api_proxy_compras(request: Request) -> Response:
    """Compras con su contexto y su oportunidad, de la más nueva a la más vieja."""
    return await _reenviar_json(request.app.state.cfg, "GET", "/guard/compras")


async def api_proxy_compras_contexto(request: Request) -> Response:
    """La respuesta del usuario al porqué del pedido, u "omitir"."""
    try:
        cuerpo = await request.json()
    except ValueError:
        return _error(400, "json_invalido")
    if not cuerpo_contexto_valido(cuerpo):
        return _error(
            400,
            "se_espera",
            formato=[
                {"compra_id": "str", "respuesta": f"str (1..{_MAX_RESPUESTA})"},
                {"compra_id": "str", "omitir": True},
                {"compra_id": "str", "confirmar": True},
            ],
        )
    return await _reenviar_json(
        request.app.state.cfg, "POST", "/guard/compras/contexto", cuerpo=cuerpo
    )


async def api_proxy_compras_decision(request: Request) -> Response:
    """Aceptar o rechazar la oportunidad que propone nuestro agente."""
    try:
        cuerpo = await request.json()
    except ValueError:
        return _error(400, "json_invalido")
    if not cuerpo_decision_valido(cuerpo):
        return _error(
            400,
            "se_espera",
            formato={"compra_id": "str", "decision": sorted(DECISIONES)},
        )
    return await _reenviar_json(
        request.app.state.cfg, "POST", "/guard/compras/decision", cuerpo=cuerpo
    )


async def api_proxy_historial(request: Request) -> Response:
    try:
        limite = int(request.query_params.get("limit", "200"))
    except ValueError:
        return _error(400, "limit_invalido")
    limite = max(1, min(limite, 1000))
    return await _reenviar_json(
        request.app.state.cfg,
        "GET",
        "/guard/events/history",
        params={"limit": limite},
    )


async def api_proxy_eventos(request: Request) -> Response:
    """Reenvía el SSE del proxy (`event: evento`) tal cual llega."""
    cfg: Config = request.app.state.cfg
    cliente = _cliente_proxy(cfg, httpx.Timeout(5.0, connect=1.0, read=None))
    try:
        pedido = cliente.build_request(
            "GET",
            f"{cfg.proxy_url}/guard/events",
            headers={"Accept": "text/event-stream"},
        )
        respuesta = await cliente.send(pedido, stream=True)
    except httpx.HTTPError as e:
        await cliente.aclose()
        return _proxy_desconectado(cfg, e)
    if respuesta.status_code != 200:
        await respuesta.aclose()
        await cliente.aclose()
        return _error(502, "proxy_sin_eventos", status=respuesta.status_code)

    async def cuerpo():
        # aiter_bytes y no aiter_raw: no reenviamos Content-Encoding, así que
        # el cuerpo tiene que salir decodificado.
        try:
            async for trozo in respuesta.aiter_bytes():
                yield trozo
        except httpx.HTTPError:
            return
        finally:
            await respuesta.aclose()
            await cliente.aclose()

    return StreamingResponse(
        cuerpo(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


async def api_config(request: Request) -> Response:
    return JSONResponse({"proxy_url": request.app.state.cfg.proxy_url})


class _Estaticos(StaticFiles):
    """Estáticos sin caché: en la demo se recarga y tiene que verse lo último."""

    async def get_response(self, path: str, scope) -> Response:
        respuesta = await super().get_response(path, scope)
        respuesta.headers["Cache-Control"] = "no-cache"
        return respuesta


def create_app(cfg: Config | None = None) -> Starlette:
    cfg = cfg or config_desde_entorno()
    rutas = [
        Route("/api/config", api_config),
        Route("/api/results", api_resultados),
        Route("/api/results/latest", api_resultado_ultimo),
        Route("/api/results/{origen}/{nombre}", api_resultado),
        Route("/api/fixtures", api_fixtures),
        Route("/api/fixtures/{escenario}", api_fixture),
        Route("/api/fixtures/{escenario}/ficha", api_fixture_ficha),
        Route("/api/catalog", api_catalogo),
        Route("/api/replay", api_replays),
        Route("/api/replay/{nombre}", api_replay),
        Route("/api/public-key", api_clave_publica),
        Route("/api/proxy/guard/state", api_proxy_estado, methods=["GET", "POST"]),
        Route(
            "/api/proxy/guard/aprobaciones", api_proxy_aprobaciones, methods=["GET", "POST"]
        ),
        Route("/api/proxy/guard/compras", api_proxy_compras),
        Route(
            "/api/proxy/guard/compras/contexto", api_proxy_compras_contexto, methods=["POST"]
        ),
        Route(
            "/api/proxy/guard/compras/decision", api_proxy_compras_decision, methods=["POST"]
        ),
        Route("/api/proxy/guard/events", api_proxy_eventos),
        Route("/api/proxy/guard/events/history", api_proxy_historial),
        Mount("/", app=_Estaticos(directory=STATIC_DIR, html=True), name="static"),
    ]
    app = Starlette(routes=rutas, middleware=[Middleware(SoloLocal, cfg=cfg)])
    app.state.cfg = cfg
    return app
