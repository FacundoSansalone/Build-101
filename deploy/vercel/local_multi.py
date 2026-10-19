"""Vercel en tu máquina: varias instancias de `index:app` detrás de un balanceador.

Levanta N procesos `uvicorn index:app` (puertos 8041, 8042, …), todos con el
mismo entorno, y un balanceador en :8040 que se porta como Vercel en lo que
importa para el motor:

- **Reparte por pedido, no por conexión** (round-robin): dos pedidos seguidos
  del mismo cliente, aun por la misma conexión keep-alive, caen en instancias
  distintas. Es el peor caso de Vercel (sin estado compartido entre
  instancias), el que tiene que aguantar el núcleo multi-instancia.
- **Streaming:** la respuesta (también SSE) pasa a medida que llega.
- **Corte a los 300 s** (`maxDuration` del plan Hobby): si la instancia no
  contestó, 504 `FUNCTION_INVOCATION_TIMEOUT`; si ya estaba mandando el
  cuerpo, se corta la conexión, como hace Vercel.
- **Cuerpo de hasta 4,5 MB:** más, 413 `FUNCTION_PAYLOAD_TOO_LARGE`.
- `X-Balanceador-Instancia: <n>` en cada respuesta (1, 2, …).
- **Matar una instancia a mitad de camino:** `--matar <n> --a-los <s>`, o
  `POST /__balanceador/matar/<n>` (solo desde esta máquina). Lo que estaba en
  curso en ella termina en 500 `FUNCTION_INVOCATION_FAILED` y los pedidos
  siguientes van a las otras.

Ayudas para las pruebas (no existen en Vercel; el balanceador las saca antes
de reenviar):

- `X-Balanceador-Pedido: <id>` en el pedido: después,
  `GET /__balanceador/pedido/<id>` dice a qué instancia fue (mientras sigue
  en curso, también).
- `X-Balanceador-Evitar: <n>[,<m>]`: si se puede, no lo manda a esas.
- `GET /__balanceador/estado`: instancias, puertos y si siguen vivas.

Por defecto las instancias arrancan con un entorno **limpio**, como en
Vercel: solo lo esencial del sistema (PATH, TEMP, …) más lo que traigan los
`--env` (formato `.env`: `NOMBRE=valor`; un valor vacío es "sin definir").
`--heredar-entorno` les pasa además todo tu entorno. Los valores nunca se
imprimen.

Desde la raíz del repo (Windows con Git Bash o Linux):

    PYTHONUTF8=1 world/ap2/.venv/Scripts/python deploy/vercel/local_multi.py \\
        --instancias 2 --env .temp-db/vercel/vercel.env --env .temp-db/vercel/local.env

Ver deploy/vercel/README.md ("Prueba local").
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import itertools
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

RAIZ = Path(__file__).resolve().parents[2]

LIMITE_VERCEL_S = 300.0  # maxDuration de Hobby (vercel.json)
MAX_CUERPO_VERCEL = 4_500_000  # 4,5 MB: pedido y respuesta de una función
PUERTO_BALANCEADOR = 8040
PUERTO_PRIMERA_INSTANCIA = 8041
PREFIJO_CONTROL = "/__balanceador/"
HEADER_INSTANCIA = "x-balanceador-instancia"
HEADER_PEDIDO = "x-balanceador-pedido"
HEADER_EVITAR = "x-balanceador-evitar"
MAX_PEDIDOS_RECORDADOS = 2000
_ID_PEDIDO_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_NOMBRE_VARIABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Hop-by-hop (RFC 9110 §7.6.1): no se reenvían en ningún sentido.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
})

# Lo mínimo del sistema para que Python y los sockets anden (en Windows, sin
# SYSTEMROOT no hay sockets). Lo demás llega solo por --env.
_ENTORNO_ESENCIAL = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "LANG",
    "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY",
    "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)

Log = Callable[[str], None]


def _log(mensaje: str) -> None:
    print(f"[balanceador] {mensaje}", flush=True)


# ---------------------------------------------------------------------------
# entorno
# ---------------------------------------------------------------------------


def leer_env(path: Path) -> dict[str, str]:
    """Variables de un archivo `.env` (`NOMBRE=valor`, `export NOMBRE=valor`,
    comentarios con `#`, comillas simples o dobles alrededor del valor).

    Un valor vacío se toma como "sin definir" y no se pasa (así sirve
    `env.ejemplo` copiado tal cual). Los errores nombran la línea, nunca el
    valor."""
    variables: dict[str, str] = {}
    texto = Path(path).read_text(encoding="utf-8-sig")
    for numero, linea in enumerate(texto.splitlines(), 1):
        limpia = linea.strip()
        if not limpia or limpia.startswith("#"):
            continue
        if limpia.startswith("export "):
            limpia = limpia[len("export "):].lstrip()
        nombre, sep, valor = limpia.partition("=")
        nombre = nombre.strip()
        if not sep or not _NOMBRE_VARIABLE_RE.fullmatch(nombre):
            raise ValueError(f"{path}:{numero}: se esperaba NOMBRE=valor")
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "'\"":
            valor = valor[1:-1]
        if valor:
            variables[nombre] = valor
        else:
            variables.pop(nombre, None)
    return variables


def armar_entorno(
    archivos: Iterable[Path], *, heredar: bool, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """El entorno de cada instancia (el mismo para todas)."""
    base = os.environ if base is None else base
    if heredar:
        entorno = dict(base)
    else:
        entorno = {k: base[k] for k in _ENTORNO_ESENCIAL if k in base}
    for archivo in archivos:
        entorno.update(leer_env(archivo))
    pythonpath = entorno.get("PYTHONPATH")
    entorno["PYTHONPATH"] = str(RAIZ) + (os.pathsep + pythonpath if pythonpath else "")
    entorno.setdefault("PYTHONUTF8", "1")
    entorno["PYTHONUNBUFFERED"] = "1"
    return entorno


# ---------------------------------------------------------------------------
# instancias
# ---------------------------------------------------------------------------


@dataclass
class Instancia:
    numero: int  # 1, 2, …
    puerto: int
    host: str = "127.0.0.1"
    proceso: subprocess.Popen | None = None
    # Solo para los tests (sin proceso): marcarla muerta a mano.
    muerta: bool = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.puerto}"

    def viva(self) -> bool:
        if self.muerta:
            return False
        return self.proceso is None or self.proceso.poll() is None

    def matar(self) -> None:
        """Muerte abrupta (SIGKILL / TerminateProcess), como una instancia que
        Vercel recicla o que se cae: no hay apagado ordenado."""
        self.muerta = True
        if self.proceso is not None and self.proceso.poll() is None:
            self.proceso.kill()


@dataclass
class _Pedido:
    instancia: int
    en_curso: bool = True
    metodo: str = ""
    ruta: str = ""
    inicio: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# balanceador (ASGI puro, para no meterse con el streaming)
# ---------------------------------------------------------------------------


class Balanceador:
    """Reparte cada pedido HTTP a la siguiente instancia viva."""

    def __init__(
        self,
        instancias: list[Instancia],
        *,
        limite_s: float = LIMITE_VERCEL_S,
        max_cuerpo: int = MAX_CUERPO_VERCEL,
        transporte: httpx.AsyncBaseTransport | None = None,
        log: Log = _log,
    ) -> None:
        if not instancias:
            raise ValueError("hace falta al menos una instancia")
        self.instancias = instancias
        self.limite_s = limite_s
        self.max_cuerpo = max_cuerpo
        self._transporte = transporte
        self._log = log
        self._siguiente = 0
        self._pedidos: collections.OrderedDict[str, _Pedido] = collections.OrderedDict()
        self._client: httpx.AsyncClient | None = None

    # -- elección ------------------------------------------------------------
    def elegir(self, evitar: frozenset[int] = frozenset()) -> Instancia | None:
        """Round-robin sobre las vivas, salteando `evitar` si queda otra."""
        total = len(self.instancias)
        candidatas = [
            self.instancias[(self._siguiente + paso) % total] for paso in range(total)
        ]
        vivas = [i for i in candidatas if i.viva()]
        if not vivas:
            return None
        elegida = next((i for i in vivas if i.numero not in evitar), vivas[0])
        self._siguiente = (self.instancias.index(elegida) + 1) % total
        return elegida

    def _cliente(self) -> httpx.AsyncClient:
        if self._client is None:
            # Sin timeout propio: el corte lo maneja el balanceador (limite_s).
            self._client = httpx.AsyncClient(
                transport=self._transporte, timeout=httpx.Timeout(None),
                follow_redirects=False, trust_env=False,
            )
        return self._client

    async def cerrar(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _recordar(self, pedido_id: str | None, datos: _Pedido) -> None:
        if not pedido_id:
            return
        self._pedidos[pedido_id] = datos
        self._pedidos.move_to_end(pedido_id)
        while len(self._pedidos) > MAX_PEDIDOS_RECORDADOS:
            self._pedidos.popitem(last=False)

    # -- ASGI ----------------------------------------------------------------
    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return  # websockets: fuera de alcance
        if scope["path"].startswith(PREFIJO_CONTROL):
            await self._control(scope, receive, send)
            return
        await self._reenviar(scope, receive, send)

    async def _lifespan(self, receive: Callable, send: Callable) -> None:
        while True:
            mensaje = await receive()
            if mensaje["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif mensaje["type"] == "lifespan.shutdown":
                await self.cerrar()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _control(self, scope: dict, receive: Callable, send: Callable) -> None:
        cliente = (scope.get("client") or ("", 0))[0]
        if cliente not in ("127.0.0.1", "::1", "localhost", "testclient"):
            await _json(send, 403, {"error": "solo desde esta máquina"})
            return
        resto = scope["path"][len(PREFIJO_CONTROL):].strip("/")
        metodo = scope["method"]
        if metodo == "GET" and resto == "estado":
            await _json(send, 200, {
                "instancias": [
                    {"numero": i.numero, "puerto": i.puerto, "viva": i.viva()}
                    for i in self.instancias
                ],
                "limite_s": self.limite_s,
            })
            return
        if metodo == "GET" and resto.startswith("pedido/"):
            pedido = self._pedidos.get(resto[len("pedido/"):])
            if pedido is None:
                await _json(send, 404, {"error": "pedido desconocido"})
                return
            await _json(send, 200, {
                "instancia": pedido.instancia, "en_curso": pedido.en_curso,
                "metodo": pedido.metodo, "ruta": pedido.ruta,
            })
            return
        if metodo == "POST" and resto.startswith("matar/"):
            numero = resto[len("matar/"):]
            instancia = next((i for i in self.instancias if str(i.numero) == numero), None)
            if instancia is None:
                await _json(send, 404, {"error": "instancia desconocida"})
                return
            self._log(f"matando la instancia {instancia.numero} (pedido de control)")
            instancia.matar()
            await _json(send, 200, {"matada": instancia.numero})
            return
        await _json(send, 404, {"error": "ruta de control desconocida"})

    async def _reenviar(self, scope: dict, receive: Callable, send: Callable) -> None:
        loop = asyncio.get_running_loop()
        limite = loop.time() + self.limite_s
        cuerpo = await _leer_cuerpo(receive, self.max_cuerpo)
        if cuerpo is None:
            await _error_vercel(send, 413, "FUNCTION_PAYLOAD_TOO_LARGE", None)
            return

        headers, pedido_id, evitar = self._headers_hacia_instancia(scope)
        ruta = scope.get("raw_path") or scope["path"].encode()
        if isinstance(ruta, bytes):
            ruta = ruta.decode("latin-1")
        if scope.get("query_string"):
            ruta += "?" + scope["query_string"].decode("latin-1")

        intentadas: set[int] = set()
        while True:
            instancia = self.elegir(frozenset(evitar | intentadas))
            if instancia is None or instancia.numero in intentadas:
                await _error_vercel(send, 503, "NO_INSTANCES_AVAILABLE", None)
                return
            intentadas.add(instancia.numero)
            registro = _Pedido(instancia.numero, metodo=scope["method"], ruta=scope["path"])
            self._recordar(pedido_id, registro)
            try:
                await self._una_instancia(
                    instancia, scope["method"], ruta, headers, cuerpo, limite, send
                )
                return
            except _NoLlego:
                # La instancia no recibió el pedido (no arrancó o ya murió):
                # Vercel lo mandaría a otra. Se reintenta con la siguiente.
                self._log(f"la instancia {instancia.numero} no atiende; pruebo otra")
                continue
            finally:
                registro.en_curso = False

    def _headers_hacia_instancia(
        self, scope: dict
    ) -> tuple[list[tuple[bytes, bytes]], str | None, set[int]]:
        headers: list[tuple[bytes, bytes]] = []
        pedido_id: str | None = None
        evitar: set[int] = set()
        conexion = set()
        for nombre, valor in scope["headers"]:
            if nombre.lower() == b"connection":
                conexion |= {t.strip().lower() for t in valor.decode("latin-1").split(",")}
        for nombre, valor in scope["headers"]:
            bajo = nombre.decode("latin-1").lower()
            if bajo == HEADER_PEDIDO:
                texto = valor.decode("latin-1").strip()
                pedido_id = texto if _ID_PEDIDO_RE.fullmatch(texto) else None
                continue
            if bajo == HEADER_EVITAR:
                evitar |= {
                    int(t) for t in valor.decode("latin-1").split(",") if t.strip().isdigit()
                }
                continue
            if bajo in _HOP_BY_HOP or bajo in conexion or bajo == "content-length":
                continue
            if bajo.startswith("x-forwarded-"):
                continue  # los pone el balanceador, como Vercel
            headers.append((nombre, valor))
        cliente = (scope.get("client") or ("127.0.0.1", 0))[0]
        host = next((v for n, v in scope["headers"] if n.lower() == b"host"), b"")
        headers += [
            (b"x-forwarded-for", cliente.encode("latin-1")),
            (b"x-forwarded-proto", scope.get("scheme", "http").encode("latin-1")),
            (b"x-real-ip", cliente.encode("latin-1")),
        ]
        if host:
            headers.append((b"x-forwarded-host", host))
        return headers, pedido_id, evitar

    async def _una_instancia(
        self,
        instancia: Instancia,
        metodo: str,
        ruta: str,
        headers: list[tuple[bytes, bytes]],
        cuerpo: bytes,
        limite: float,
        send: Callable,
    ) -> None:
        loop = asyncio.get_running_loop()
        cliente = self._cliente()
        pedido = cliente.build_request(
            metodo, instancia.url + ruta, headers=headers, content=cuerpo
        )
        try:
            respuesta = await asyncio.wait_for(
                cliente.send(pedido, stream=True), timeout=max(0.0, limite - loop.time())
            )
        except (asyncio.TimeoutError, TimeoutError):
            self._log(f"instancia {instancia.numero}: {metodo} {ruta} pasó {self.limite_s:g} s → 504")
            await _error_vercel(send, 504, "FUNCTION_INVOCATION_TIMEOUT", instancia.numero)
            return
        except httpx.ConnectError as e:
            raise _NoLlego from e
        except httpx.HTTPError:
            self._log(f"instancia {instancia.numero}: se cortó antes de responder → 500")
            await _error_vercel(send, 500, "FUNCTION_INVOCATION_FAILED", instancia.numero)
            return

        empezado = False
        try:
            salida = [
                (n, v) for n, v in respuesta.headers.raw
                if n.decode("latin-1").lower() not in _HOP_BY_HOP
                and n.decode("latin-1").lower() != HEADER_INSTANCIA
            ]
            salida.append((HEADER_INSTANCIA.encode(), str(instancia.numero).encode()))
            await send({"type": "http.response.start", "status": respuesta.status_code,
                        "headers": salida})
            empezado = True
            fragmentos = respuesta.aiter_raw()
            enviados = 0
            avisado = False
            while True:
                restante = limite - loop.time()
                if restante <= 0:
                    raise TimeoutError
                try:
                    fragmento = await asyncio.wait_for(anext(fragmentos), timeout=restante)
                except StopAsyncIteration:
                    break
                enviados += len(fragmento)
                if enviados > self.max_cuerpo and not avisado:
                    # Vercel corta las respuestas sin streaming de más de 4,5 MB;
                    # acá solo se avisa (el motor no debería llegar nunca).
                    avisado = True
                    self._log(f"instancia {instancia.numero}: la respuesta pasa 4,5 MB")
                await send({"type": "http.response.body", "body": fragmento, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except (asyncio.TimeoutError, TimeoutError):
            # Ya se mandaron los headers: Vercel corta la conexión. Volver sin
            # cerrar el cuerpo hace que uvicorn la corte también.
            self._log(f"instancia {instancia.numero}: {metodo} {ruta} pasó {self.limite_s:g} "
                      "s mandando el cuerpo → se corta la conexión")
        except httpx.HTTPError:
            self._log(f"instancia {instancia.numero}: se cortó a mitad de la respuesta")
            if not empezado:
                await _error_vercel(send, 500, "FUNCTION_INVOCATION_FAILED", instancia.numero)
        finally:
            await respuesta.aclose()


class _NoLlego(Exception):
    """La conexión con la instancia no se pudo abrir: el pedido no llegó."""


async def _leer_cuerpo(receive: Callable, maximo: int) -> bytes | None:
    """El cuerpo entero (Vercel también lo junta antes de invocar), o None si
    pasa el máximo."""
    partes: list[bytes] = []
    total = 0
    while True:
        mensaje = await receive()
        if mensaje["type"] == "http.disconnect":
            break
        trozo = mensaje.get("body", b"")
        total += len(trozo)
        if total > maximo:
            return None
        partes.append(trozo)
        if not mensaje.get("more_body", False):
            break
    return b"".join(partes)


async def _json(send: Callable, status: int, datos: dict[str, Any]) -> None:
    import json

    cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(cuerpo)).encode()),
    ]})
    await send({"type": "http.response.body", "body": cuerpo})


async def _error_vercel(send: Callable, status: int, codigo: str, instancia: int | None) -> None:
    """Un error con la forma de los de Vercel (texto plano y `x-vercel-error`)."""
    cuerpo = f"A server error has occurred\n\n{codigo}\n".encode()
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(cuerpo)).encode()),
        (b"x-vercel-error", codigo.encode()),
    ]
    if instancia is not None:
        headers.append((HEADER_INSTANCIA.encode(), str(instancia).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": cuerpo})


# ---------------------------------------------------------------------------
# procesos
# ---------------------------------------------------------------------------


def encontrar_app(app: str, entorno: Mapping[str, str]) -> bool:
    """¿Está el módulo de `app` (`modulo:variable`) en la raíz o en el
    PYTHONPATH de las instancias?"""
    partes = app.partition(":")[0].split(".")
    rutas = [Path(r) for r in entorno.get("PYTHONPATH", "").split(os.pathsep) if r] or [RAIZ]
    return any(
        (base.joinpath(*partes).with_suffix(".py")).exists()
        or (base.joinpath(*partes) / "__init__.py").exists()
        for base in rutas
    )


def puerto_libre(host: str, puerto: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, puerto))
        except OSError:
            return False
    return True


def _volcar(prefijo: str, flujo: Any) -> None:
    for linea in iter(flujo.readline, ""):
        print(f"{prefijo} {linea.rstrip()}", flush=True)


# Cada instancia lee su stdin (un pipe del balanceador) y sale cuando se
# cierra: si el balanceador muere de golpe (en Windows no se apagan los
# hijos), las instancias no quedan huérfanas ocupando los puertos.
_ARRANQUE = (
    "import os, sys, threading\n"
    "threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()\n"
    "import uvicorn\n"
    "sys.argv[0] = 'uvicorn'\n"
    "uvicorn.main()\n"
)


def arrancar_instancia(
    instancia: Instancia, app: str, entorno: dict[str, str], log_level: str
) -> None:
    comando = [
        sys.executable, "-c", _ARRANQUE, app, "--host", instancia.host,
        "--port", str(instancia.puerto), "--log-level", log_level,
        # Confía en los X-Forwarded-* del balanceador (como detrás de Vercel).
        "--forwarded-allow-ips", "127.0.0.1",
    ]
    extra: dict[str, Any] = {}
    if os.name == "nt":
        # Ctrl+C no les llega directo: las apaga el balanceador al salir.
        extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    instancia.proceso = subprocess.Popen(
        comando, cwd=RAIZ, env=entorno, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
        **extra,
    )
    threading.Thread(
        target=_volcar, args=(f"[i{instancia.numero}]", instancia.proceso.stdout), daemon=True
    ).start()


def esperar_lista(instancia: Instancia, espera_s: float) -> bool:
    """Lista cuando contesta cualquier cosa por HTTP (la app mínima de 503
    también cuenta: se ve en sus logs por qué)."""
    fin = time.monotonic() + espera_s
    while time.monotonic() < fin:
        if not instancia.viva():
            return False
        try:
            httpx.get(f"{instancia.url}/v1/openapi.json", timeout=2.0, trust_env=False)
            return True
        except httpx.HTTPError:
            time.sleep(0.3)
    return False


def apagar(instancias: Iterable[Instancia]) -> None:
    instancias = list(instancias)
    for instancia in instancias:
        proceso = instancia.proceso
        if proceso is None or proceso.poll() is not None:
            continue
        try:
            proceso.stdin.close()  # el perro guardián de la instancia la hace salir
        except (AttributeError, OSError):
            proceso.terminate()
    for instancia in instancias:
        proceso = instancia.proceso
        if proceso is None:
            continue
        try:
            proceso.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proceso.kill()


async def _servir(balanceador: Balanceador, args: argparse.Namespace) -> None:
    import uvicorn

    config = uvicorn.Config(
        balanceador, host=args.host, port=args.puerto, log_level="warning",
        lifespan="on", server_header=False, date_header=False, timeout_keep_alive=75,
    )
    servidor = uvicorn.Server(config)
    if args.matar is not None:
        instancia = next((i for i in balanceador.instancias if i.numero == args.matar), None)

        def matar() -> None:
            if instancia is not None:
                _log(f"matando la instancia {instancia.numero} (--matar, a los {args.a_los:g} s)")
                instancia.matar()

        asyncio.get_running_loop().call_later(args.a_los, matar)
    if args.durante is not None:
        def salir() -> None:
            _log(f"--durante {args.durante:g} s: salgo")
            servidor.should_exit = True

        asyncio.get_running_loop().call_later(args.durante, salir)
    await servidor.serve()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Varias instancias de index:app detrás de un balanceador por pedido "
        "(prueba local del deploy en Vercel)."
    )
    p.add_argument("--instancias", "-n", type=int, default=2, help="cuántas instancias (2)")
    p.add_argument("--puerto", type=int, default=PUERTO_BALANCEADOR,
                   help=f"puerto del balanceador ({PUERTO_BALANCEADOR})")
    p.add_argument("--primer-puerto", type=int, default=PUERTO_PRIMERA_INSTANCIA,
                   help=f"puerto de la primera instancia ({PUERTO_PRIMERA_INSTANCIA}, y siguen)")
    p.add_argument("--host", default="127.0.0.1", help="interfaz del balanceador (127.0.0.1)")
    p.add_argument("--app", default="index:app", help="la app ASGI (index:app)")
    p.add_argument("--env", action="append", type=Path, default=[],
                   help="archivo .env con el entorno de las instancias (se puede repetir; "
                   "el último gana)")
    p.add_argument("--heredar-entorno", action="store_true",
                   help="pasar además todo tu entorno (por defecto, limpio como en Vercel)")
    p.add_argument("--limite", type=float, default=LIMITE_VERCEL_S,
                   help="corte por pedido en segundos (300, el de Hobby)")
    p.add_argument("--matar", type=int, default=None, metavar="N",
                   help="matar la instancia N (1, 2, …) a mitad de camino")
    p.add_argument("--a-los", type=float, default=30.0, metavar="S",
                   help="segundos después de arrancar para --matar (30)")
    p.add_argument("--durante", type=float, default=None, metavar="S",
                   help="salir solo después de S segundos (para pruebas guionadas)")
    p.add_argument("--espera-arranque", type=float, default=60.0,
                   help="segundos para que arranque cada instancia (60)")
    p.add_argument("--log-level", default="info", help="nivel de log de las instancias (info)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.instancias < 1:
        print("--instancias tiene que ser 1 o más", file=sys.stderr)
        return 2
    if args.matar is not None and not 1 <= args.matar <= args.instancias:
        print(f"--matar tiene que ser una instancia entre 1 y {args.instancias}", file=sys.stderr)
        return 2
    try:
        entorno = armar_entorno(args.env, heredar=args.heredar_entorno)
    except (OSError, ValueError) as e:
        print(f"no se pudo leer el entorno: {e}", file=sys.stderr)
        return 2
    if not encontrar_app(args.app, entorno):
        print(f"no encuentro el módulo de {args.app} (la entrada de Vercel es index.py, "
              "en la raíz del repo)", file=sys.stderr)
        return 2

    instancias = [
        Instancia(numero=n, puerto=args.primer_puerto + n - 1)
        for n in range(1, args.instancias + 1)
    ]
    ocupados = [
        p for p in itertools.chain([args.puerto], (i.puerto for i in instancias))
        if not puerto_libre(args.host if p == args.puerto else "127.0.0.1", p)
    ]
    if ocupados:
        print(f"puertos ocupados: {ocupados}", file=sys.stderr)
        return 2

    nombres = sorted(set(entorno) - set(_ENTORNO_ESENCIAL) - {"PYTHONPATH", "PYTHONUTF8",
                                                              "PYTHONUNBUFFERED"})
    _log(f"entorno de las instancias: {len(nombres)} variables propias ({', '.join(nombres)})")
    try:
        for instancia in instancias:
            arrancar_instancia(instancia, args.app, entorno, args.log_level)
        for instancia in instancias:
            if not esperar_lista(instancia, args.espera_arranque):
                print(f"la instancia {instancia.numero} (:{instancia.puerto}) no arrancó; "
                      "mirá sus logs arriba", file=sys.stderr)
                return 1
        puertos = ", ".join(f":{i.puerto}" for i in instancias)
        _log(f"http://{args.host}:{args.puerto} → instancias {puertos} "
             f"(por pedido, corte a {args.limite:g} s). Ctrl+C para salir.")
        balanceador = Balanceador(instancias, limite_s=args.limite)
        asyncio.run(_servir(balanceador, args))
    except KeyboardInterrupt:
        pass
    finally:
        apagar(instancias)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
