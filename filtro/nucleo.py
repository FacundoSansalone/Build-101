"""El filtro de seguridad: heurísticas, después juicios tipados, y falla cerrado.

Orden, para cada respuesta del proveedor:

1. Recorrer la respuesta. Si no se puede recorrer entera → no verificado.
2. Heurísticas (capa a). Si marcan algo → malicia. El modelo no se llama:
   lo bloqueado no llega a ningún LLM.
3. Modo solo heurísticas (`FILTRO_SOLO_HEURISTICAS=1`, para la demo offline)
   → pasa, con la señal `modo:solo_heuristicas`.
4. Sin cliente de decisiones → no verificado (el filtro no puede afirmar que
   es seguro).
5. Sin texto libre (solo ids, montos, enums, JWT) → pasa: no hay nada que
   juzgar y las heurísticas ya miraron todo.
6. Juicios tipados (capa b) con umbrales en código. Error de la API, "no sé"
   del modelo o texto demasiado largo → no verificado. Por encima del umbral
   → malicia.

El motivo es siempre uno de cuatro textos fijos y las señales son códigos
cortos: nada del proveedor sale del filtro.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from contracts import ResultadoFiltro
from decisiones import ClienteDecisiones, cliente_desde_entorno
from filtro import heuristicas
from filtro.modelo import MAX_CARACTERES_MODELO, juzgar
from filtro.textos import RespuestaExcesiva, Texto, extraer_textos

logger = logging.getLogger(__name__)

MOTIVO_MALICIA = (
    "La respuesta del proveedor tiene contenido dirigido a manipular al agente de compras."
)
MOTIVO_SIN_VERIFICAR = "No se pudo verificar la seguridad de la respuesta del proveedor."
MOTIVO_OK = "Sin señales de contenido malicioso."
MOTIVO_SOLO_HEURISTICAS = (
    "Sin señales en las heurísticas (modo solo heurísticas: sin la revisión del modelo)."
)

# El modo efectivo, para mostrarlo al arrancar (`describir()`).
DESCRIPCION_SOLO_HEURISTICAS = "solo heurísticas"
DESCRIPCION_SIN_CLIENTE = "sin cliente: bloquea todo"
# `ClienteDecisiones.nombre` → el valor de DECISIONES_PROVEEDOR.
_PROVEEDORES = {"openai-decisions": "openai"}

# El proxy corta el filtro a los 30 s y el adaptador hace hasta 3 intentos con
# 1,5 s de espera: 3 × 9 + 1,5 < 30.
TIMEOUT_POR_DEFECTO_S = 8.0
_TIMEOUT_MIN_S, _TIMEOUT_MAX_S = 1.0, 9.0
_TAMANO_CACHE = 512
_VERDADEROS = frozenset({"1", "true", "yes", "si", "sí", "on"})


def _no_verificado(senal: str) -> ResultadoFiltro:
    return ResultadoFiltro(pasa=False, motivo=MOTIVO_SIN_VERIFICAR, senales=[senal])


def _clave(textos: list[Texto]) -> str:
    crudo = json.dumps(sorted((t.campo, t.texto) for t in textos), ensure_ascii=False)
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class FiltroSeguridad:
    """Implementa `contracts.Filtro`. Seguro para llamarlo desde varios hilos
    (el proxy lo corre con `asyncio.to_thread`)."""

    cliente: ClienteDecisiones | None = None
    solo_heuristicas: bool = False
    config_invalida: bool = False  # el entorno pedía algo que no existe
    _cache: OrderedDict[str, ResultadoFiltro] = dataclasses.field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __call__(self, respuesta: dict[str, Any]) -> ResultadoFiltro:
        try:
            return self._filtrar(respuesta)
        except Exception as e:  # noqa: BLE001 - falla cerrado
            logger.error("El filtro de seguridad falló (%s): se bloquea", type(e).__name__)
            return _no_verificado("filtro:error_interno")

    def describir(self) -> str:
        """El modo en un texto corto: "solo heurísticas", "sin cliente:
        bloquea todo" o "heurísticas + modelo <proveedor>/<modelo>". Nunca la
        clave."""
        if self.solo_heuristicas:
            return DESCRIPCION_SOLO_HEURISTICAS
        if self.cliente is None:
            if self.config_invalida:
                return f"{DESCRIPCION_SIN_CLIENTE} (DECISIONES_PROVEEDOR inválido)"
            return DESCRIPCION_SIN_CLIENTE
        nombre = str(getattr(self.cliente, "nombre", "") or type(self.cliente).__name__)
        proveedor = _PROVEEDORES.get(nombre, nombre)
        modelo = getattr(self.cliente, "modelo", None)
        if not modelo:
            return f"heurísticas + modelo {proveedor}"
        return f"heurísticas + modelo {proveedor}/{modelo}"

    def _filtrar(self, respuesta: Any) -> ResultadoFiltro:
        try:
            textos = extraer_textos(respuesta)
        except RespuestaExcesiva:
            return _no_verificado("filtro:respuesta_excesiva")

        senales = heuristicas.revisar(t.texto for t in textos)
        if senales:
            return ResultadoFiltro(pasa=False, motivo=MOTIVO_MALICIA, senales=senales)
        if self.solo_heuristicas:
            return ResultadoFiltro(
                pasa=True, motivo=MOTIVO_SOLO_HEURISTICAS, senales=["modo:solo_heuristicas"]
            )
        if self.cliente is None:
            return _no_verificado(
                "filtro:config_invalida" if self.config_invalida else "filtro:sin_decisiones"
            )

        libres = [t for t in textos if t.libre]
        if not libres:
            return ResultadoFiltro(pasa=True, motivo=MOTIVO_OK)
        if sum(len(t.texto) for t in libres) > MAX_CARACTERES_MODELO:
            return _no_verificado("filtro:texto_excesivo")

        clave = _clave(libres)
        with self._lock:
            previo = self._cache.get(clave)
            if previo is not None:
                self._cache.move_to_end(clave)
        if previo is not None:
            return previo.model_copy(deep=True)

        try:
            juicio = juzgar(self.cliente, libres)
        except Exception as e:  # noqa: BLE001 - DecisionesError, red, timeout: falla cerrado
            logger.warning(
                "El filtro no pudo consultar decisiones (%s): se bloquea", type(e).__name__
            )
            return _no_verificado("filtro:decisiones_error")
        if juicio.sin_respuesta:
            return _no_verificado("filtro:sin_respuesta")

        senales = juicio.senales
        resultado = (
            ResultadoFiltro(pasa=False, motivo=MOTIVO_MALICIA, senales=senales)
            if senales
            else ResultadoFiltro(pasa=True, motivo=MOTIVO_OK)
        )
        with self._lock:
            self._cache[clave] = resultado
            while len(self._cache) > _TAMANO_CACHE:
                self._cache.popitem(last=False)
        return resultado.model_copy(deep=True)


def _timeout(env: Mapping[str, str]) -> float:
    try:
        valor = float(env.get("FILTRO_TIMEOUT_S") or TIMEOUT_POR_DEFECTO_S)
    except ValueError:
        valor = TIMEOUT_POR_DEFECTO_S
    return min(max(valor, _TIMEOUT_MIN_S), _TIMEOUT_MAX_S)


def desde_entorno(env: Mapping[str, str] | None = None) -> FiltroSeguridad:
    """Arma el filtro desde el entorno.

    - `FILTRO_SOLO_HEURISTICAS=1`: solo la capa (a), sin red (demo offline).
    - Si no, el cliente de `decisiones.cliente_desde_entorno` (`OPENAI_API_KEY`
      o `TYPESAFE_API_KEY`, `DECISIONES_PROVEEDOR`, `DECISIONES_MODELO_*`), con
      `FILTRO_TIMEOUT_S` por llamada (8 s por defecto, entre 1 y 9).
    - Sin ninguno de los dos, el filtro bloquea todo lo que no puede verificar.
    """
    return _armar(os.environ if env is None else env, avisar=True)


def _armar(env: Mapping[str, str], *, avisar: bool) -> FiltroSeguridad:
    """`desde_entorno` sin red. Con `avisar=False` (lo usa `describir()`) no
    deja nada en el log."""
    if (env.get("FILTRO_SOLO_HEURISTICAS") or "").strip().lower() in _VERDADEROS:
        if avisar:
            logger.warning("Filtro en modo solo heurísticas: sin la revisión del modelo")
        return FiltroSeguridad(solo_heuristicas=True)
    try:
        cliente = cliente_desde_entorno(env)
    except ValueError:
        if avisar:
            logger.error("DECISIONES_PROVEEDOR inválido: el filtro bloquea todo")
        return FiltroSeguridad(config_invalida=True)
    if cliente is None:
        if avisar:
            logger.error(
                "Filtro sin cliente de decisiones (OPENAI_API_KEY o TYPESAFE_API_KEY) ni "
                "FILTRO_SOLO_HEURISTICAS=1: bloquea todo lo que no puede verificar"
            )
        return FiltroSeguridad()
    if dataclasses.is_dataclass(cliente) and hasattr(cliente, "timeout"):
        cliente = dataclasses.replace(cliente, timeout=_timeout(env))
    return FiltroSeguridad(cliente=cliente)


# ---------------------------------------------------------------------------
# La función que carga el proxy: GUARD_FILTRO=filtro:filtrar
# ---------------------------------------------------------------------------

_por_defecto: FiltroSeguridad | None = None
_lock_defecto = threading.Lock()


def configurar(filtro: FiltroSeguridad | None) -> None:
    """Fija el filtro que usa `filtrar()`. `None` = volver a armarlo desde el
    entorno en la próxima llamada."""
    global _por_defecto
    with _lock_defecto:
        _por_defecto = filtro


def filtrar(respuesta: dict[str, Any]) -> ResultadoFiltro:
    """`contracts.Filtro`: revisa la respuesta del proveedor (el `result` de la
    tarea A2A) antes que cualquier otra etapa. La primera vez arma el filtro
    desde el entorno (`desde_entorno`)."""
    global _por_defecto
    filtro = _por_defecto
    if filtro is None:
        with _lock_defecto:
            if _por_defecto is None:
                _por_defecto = desde_entorno()
            filtro = _por_defecto
    return filtro(respuesta)


def describir(env: Mapping[str, str] | None = None) -> str:
    """El modo efectivo del filtro en un texto corto, para mostrarlo al
    arrancar: "heurísticas + modelo <proveedor>/<modelo>", "solo heurísticas"
    o "sin cliente: bloquea todo".

    Sin `env`: el filtro que usa `filtrar()` si ya está armado (`configurar()`
    o la primera llamada) y, si no, el que armaría con el entorno del proceso,
    sin guardarlo. Con `env`: el que armaría ese entorno. No llama a la red,
    no escribe en el log y nunca muestra la clave.
    """
    if env is None:
        filtro = _por_defecto
        if filtro is not None:
            return filtro.describir()
        env = os.environ
    return _armar(env, avisar=False).describir()
