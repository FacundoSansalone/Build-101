"""El ap2.checkout firmado, atado al cart que evaluó el Guard. Sin I/O.

El comercio firma un `checkout_jwt` (ES256) con el `Checkout` de AP2 y
declara `checkout_hash = b64url(sha256(checkout_jwt))`. El usuario firma ese
hash en su `CheckoutMandate`, y el `PaymentMandate` del sample toma el monto
(`amount_cents`) y la moneda del data part. El Guard:

- recalcula el hash y lo compara con el declarado (distinto → `bloquea`; sin
  hash declarado → `escalar`);
- decodifica el payload en forma pura (no verifica la firma ES256: no tiene
  la clave pública del comercio; AP2 la verifica después) y compara con lo
  evaluado: `id` y `cart_id` (distinto → `bloquea`), total firmado (distinto
  → mandato con el total firmado: sobre el tope `bloquea`, si no `escalar`),
  moneda (distinta → `escalar`; fuera de la moneda obligatoria → `bloquea`)
  y `merchant.id` (fuera de la política → `bloquea`);
- exige que el JWT sea coherente consigo mismo: la suma de `price × quantity`
  de los `line_items` (y el total de cada línea, si lo trae) tiene que ser el
  total firmado, con `price` un número ≥ 0 y `quantity` un entero ≥ 1 (si no
  → `bloquea`); `exp` vencido respecto de `ahora` → mandato `escalar`; `iat`
  más de 5 minutos en el futuro → `escalar`; `exp`/`iat` que no son enteros
  → `bloquea`. Sin `line_items`, `exp` o `iat` no se agrega ese chequeo;
- un JWT ilegible (no tres segmentos, base64/JSON inválido, encabezado que no
  es ES256, sin `totals` o sin un único total válido) → `bloquea`.

Nunca lee el `title` de los line_items ni el `item_label`: son texto libre.
Nunca modifica el checkout. Todos los chequeos llevan
`origen="checkout_firmado"`, y ningún `detalle` interpola texto del
comercio: va en `observado`, acotado (`guard.textos.acotar`).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from contracts import CheckoutVisto, Chequeo, Ficha, Oferta, Politica, Requisito
from guard.b64 import b64url, b64url_decode
from guard.chequeos import monto
from guard.normalizar import EPSILON_MONTO, contiene, iguales, monto_valido, numero
from guard.textos import acotar, moneda_legible

ORIGEN = "checkout_firmado"
_LARGO_FIRMA_ES256 = 64  # r || s
_DESFASAJE_IAT = 300     # segundos de reloj adelantado que se toleran en `iat`
_MEDIO_CENTAVO = EPSILON_MONTO * 100


class _Ausente:
    """Campo que el payload firmado no trae (distinto de `null`, que no es un entero)."""

    def __repr__(self) -> str:
        return "AUSENTE"


AUSENTE: Any = _Ausente()


class CheckoutIlegible(ValueError):
    """El checkout_jwt no se puede leer. El mensaje es un texto fijo del Guard."""


@dataclass(frozen=True)
class LineasFirmadas:
    """Σ price × quantity de los `line_items` firmados, o por qué no se puede sumar.

    `problema` es un texto fijo del Guard; `observado`, el valor del comercio
    que lo causó (nunca el `title`).
    """

    suma: float | None = None   # centavos / 100
    problema: str | None = None
    observado: Any = None


@dataclass(frozen=True)
class CheckoutFirmado:
    """Lo que el Guard usa del payload firmado (valores tal cual, sin el title)."""

    cart_id: Any
    total: float          # totals[type=total].amount / 100
    moneda: Any
    comercio: Any         # merchant.id
    lineas: LineasFirmadas | None = None   # None: el payload no trae line_items
    iat: Any = AUSENTE
    exp: Any = AUSENTE


def hash_checkout(checkout_jwt: str) -> str:
    """b64url sin relleno de sha256 del JWT como string (igual que el comercio)."""
    return b64url(hashlib.sha256(checkout_jwt.encode("utf-8")).digest())


def leer_checkout(checkout_jwt: str) -> CheckoutFirmado:
    """Decodifica el payload del JWT sin verificar la firma. Lanza CheckoutIlegible."""
    partes = checkout_jwt.split(".")
    if len(partes) != 3:
        raise CheckoutIlegible("no tiene tres segmentos")
    try:
        encabezado = json.loads(b64url_decode(partes[0]))
        payload = json.loads(b64url_decode(partes[1]))
        firma = b64url_decode(partes[2])
    except (ValueError, RecursionError) as error:
        raise CheckoutIlegible("base64url o JSON inválido") from error
    if not isinstance(encabezado, dict) or encabezado.get("alg") != "ES256":
        raise CheckoutIlegible("el encabezado no es ES256")
    if len(firma) != _LARGO_FIRMA_ES256:
        raise CheckoutIlegible("la firma no tiene el formato ES256")
    if not isinstance(payload, dict):
        raise CheckoutIlegible("el payload no es un objeto JSON")
    comercio = payload.get("merchant")
    return CheckoutFirmado(
        cart_id=payload.get("id"),
        total=_total(payload.get("totals")),
        moneda=payload.get("currency"),
        comercio=comercio.get("id") if isinstance(comercio, dict) else None,
        lineas=_lineas(payload["line_items"]) if "line_items" in payload else None,
        iat=payload.get("iat", AUSENTE),
        exp=payload.get("exp", AUSENTE),
    )


def _total(totales: Any) -> float:
    if not isinstance(totales, list):
        raise CheckoutIlegible("falta la lista totals")
    candidatos = _totales_de_tipo_total(totales)
    if len(candidatos) != 1:
        raise CheckoutIlegible("totals no tiene un único total")
    centavos = candidatos[0].get("amount")
    if isinstance(centavos, bool) or not isinstance(centavos, (int, float)):
        raise CheckoutIlegible("el total no es un número")
    valido = monto_valido(centavos)
    if valido is None:
        raise CheckoutIlegible("el total no es un monto válido")
    return valido / 100


def _totales_de_tipo_total(totales: list) -> list[dict]:
    return [t for t in totales if isinstance(t, dict) and t.get("type") == "total"]


def _centavos(valor: Any) -> float | None:
    """Número JSON finito (no bool, no string) como float; si no, None."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return numero(valor)


def _entero(valor: Any) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _lineas(lineas: Any) -> LineasFirmadas:
    """Suma price × quantity de cada line_item. Lee `item.price`, `quantity` y
    `totals`; nunca el `title`."""
    if not isinstance(lineas, list):
        return LineasFirmadas(problema="no son una lista", observado=type(lineas).__name__)
    suma = 0.0
    for linea in lineas:
        item = linea.get("item") if isinstance(linea, dict) else None
        if not isinstance(item, dict):
            return LineasFirmadas(problema="traen un line_item sin item")
        precio, cantidad = _centavos(item.get("price")), linea.get("quantity")
        if precio is None or precio < 0:
            return LineasFirmadas(
                problema="traen un price que no es un número ≥ 0", observado=item.get("price")
            )
        if not _entero(cantidad) or cantidad < 1:
            return LineasFirmadas(
                problema="traen una quantity que no es un entero ≥ 1", observado=cantidad
            )
        try:
            importe = precio * cantidad
        except OverflowError:  # quantity entera que no entra en un float
            importe = math.inf
        if not math.isfinite(importe):
            return LineasFirmadas(problema="traen un price × quantity que no es finito")
        if "totals" in linea:
            totales = linea["totals"]
            candidatos = _totales_de_tipo_total(totales) if isinstance(totales, list) else []
            total_linea = _centavos(candidatos[0].get("amount")) if len(candidatos) == 1 else None
            if total_linea is None:
                return LineasFirmadas(
                    problema="traen un line_item cuyos totals no tienen un único total numérico"
                )
            if abs(total_linea - importe) > _MEDIO_CENTAVO:
                return LineasFirmadas(
                    problema="traen un line_item cuyo total no es su price × quantity",
                    observado=total_linea / 100,
                )
        suma += importe
    if not math.isfinite(suma):
        return LineasFirmadas(problema="suman un monto que no es finito")
    return LineasFirmadas(suma=suma / 100)


# ---------------------------------------------------------------------------
# Chequeos
# ---------------------------------------------------------------------------


def chequeos_checkout(
    checkout: CheckoutVisto,
    *,
    cart_id_elegido: str,
    elegida: Oferta | None,
    ficha: Ficha,
    politica: Politica,
    comercio_id: str | None,
    checkout_hash: str | None,
    ahora: datetime | None = None,
) -> tuple[list[Chequeo], str]:
    """Chequeos del checkout firmado y el hash recalculado (va al recibo).

    `ahora` (el de `evaluar()`; None → `datetime.now(UTC)`, naive → UTC) es
    el instante contra el que se miden `exp` e `iat`.
    """
    recalculado = hash_checkout(checkout.checkout_jwt)
    chequeos: list[Chequeo] = []
    try:
        firmado: CheckoutFirmado | None = leer_checkout(checkout.checkout_jwt)
    except CheckoutIlegible as error:
        firmado = None
        chequeos.append(
            _chequeo(
                "integridad", "bloquea", "checkout_jwt",
                f"El checkout_jwt firmado es ilegible ({error}): no se puede atar "
                "a lo evaluado.",
            )
        )
    chequeos.append(_chequeo_hash(recalculado, checkout.checkout_hash, checkout_hash))
    chequeos.append(_chequeo_cart_id(cart_id_elegido, checkout.cart_id, firmado))
    if firmado is not None:
        chequeos += _chequeos_monto(checkout, firmado, elegida, ficha)
        if firmado.lineas is not None:
            chequeos.append(_chequeo_lineas(firmado.lineas, firmado.total, firmado.moneda))
        chequeos += _chequeos_moneda(checkout, firmado, elegida, ficha)
        chequeos.append(_chequeo_comercio(firmado.comercio, politica, comercio_id))
        chequeos += _chequeos_vigencia(firmado, _segundos(ahora))
    return chequeos, recalculado


def _chequeo(
    tipo: str, resultado: str, campo: str, detalle: str,
    esperado: Any = None, observado: Any = None,
) -> Chequeo:
    return Chequeo(
        tipo=tipo, resultado=resultado, detalle=detalle, campo=campo,
        esperado=esperado, observado=observado, origen=ORIGEN,
    )


def _chequeo_hash(
    recalculado: str, declarado: str | None, por_parametro: str | None
) -> Chequeo:
    declarados = [h for h in (declarado, por_parametro) if h is not None]
    if not declarados:
        return _chequeo(
            "integridad", "escalar", "checkout_hash",
            "El comercio no declaró el checkout_hash: no se puede confirmar qué "
            "checkout firmaría el usuario.",
            esperado=recalculado,
        )
    distinto = next((h for h in declarados if h != recalculado), None)
    if distinto is not None:
        return _chequeo(
            "integridad", "bloquea", "checkout_hash",
            "El checkout_hash declarado no es el hash del checkout_jwt firmado: el "
            "usuario firmaría un checkout distinto del evaluado.",
            esperado=recalculado, observado=acotar(distinto),
        )
    return _chequeo(
        "integridad", "ok", "checkout_hash",
        "El checkout_hash declarado es el hash del checkout_jwt firmado.",
        esperado=recalculado, observado=recalculado,
    )


def _chequeo_cart_id(
    cart_id_elegido: str, cart_id_data_part: str | None, firmado: CheckoutFirmado | None
) -> Chequeo:
    ids = [cart_id_data_part] if cart_id_data_part is not None else []
    if firmado is not None:
        ids.insert(0, firmado.cart_id)  # el id firmado (puede faltar: None)
    if not ids:  # JWT ilegible y data part sin cart_id: ya bloquea por ilegible
        return _chequeo(
            "integridad", "escalar", "cart_id",
            "El checkout no trae un cart_id legible para compararlo con el elegido.",
            esperado=acotar(cart_id_elegido),
        )
    distintos = [i for i in ids if i != cart_id_elegido]
    if distintos:
        return _chequeo(
            "integridad", "bloquea", "cart_id",
            "El checkout es de un carrito distinto del elegido: no corresponde a "
            "lo evaluado.",
            esperado=acotar(cart_id_elegido), observado=acotar(distintos[0]),
        )
    return _chequeo(
        "integridad", "ok", "cart_id",
        "El checkout corresponde al carrito elegido.",
        esperado=acotar(cart_id_elegido), observado=acotar(cart_id_elegido),
    )


def _topes(ficha: Ficha) -> list[float]:
    """Topes obligatorios válidos de la ficha (el mandato)."""
    return [
        tope
        for r in ficha.requisitos
        if r.obligatorio and r.campo == "precio" and r.operador == "maximo"
        and (tope := monto_valido(r.valor)) is not None
    ]


def _monedas_obligatorias(ficha: Ficha) -> list[Requisito]:
    return [r for r in ficha.requisitos if r.obligatorio and r.campo == "moneda"]


def _chequeos_monto(
    checkout: CheckoutVisto, firmado: CheckoutFirmado, elegida: Oferta | None, ficha: Ficha
) -> list[Chequeo]:
    """Total firmado contra el cart; montos del data part contra el total firmado.

    Cada monto que no coincide con el del cart evaluado se compara además con
    el tope del mandato (el chequeo de mandato del cart no lo cubrió).
    """
    moneda = firmado.moneda
    cobrado = elegida.precio if elegida is not None else None
    cart = cobrado if cobrado is not None and cobrado == cobrado else None  # NaN: sin dato
    chequeos: list[Chequeo] = []
    a_comparar_con_tope: list[tuple[str, float]] = []

    if cart is not None and abs(firmado.total - cart) <= EPSILON_MONTO:
        chequeos.append(
            _chequeo(
                "integridad", "ok", "total",
                f"El total firmado ({monto(firmado.total, moneda)}) coincide con el "
                "monto del carrito evaluado.",
                esperado=cart, observado=firmado.total,
            )
        )
    else:
        chequeos.append(
            _chequeo(
                "integridad", "escalar", "total",
                f"El total firmado ({monto(firmado.total, moneda)}) no coincide con "
                "el monto del carrito evaluado "
                f"({monto(cart, moneda) if cart is not None else 'sin dato'}).",
                esperado=cart, observado=firmado.total,
            )
        )
        a_comparar_con_tope.append(("El total firmado", firmado.total))

    for campo, valor, divisor in (
        ("amount", checkout.amount, 1),
        ("amount_cents", (checkout.model_extra or {}).get("amount_cents"), 100),
    ):
        if valor is None:
            continue
        valido = monto_valido(valor)
        if valido is None:
            chequeos.append(
                _chequeo(
                    "integridad", "escalar", campo,
                    f"El monto del ap2.checkout ({campo}) es ilegible: no se puede "
                    "comparar con el total firmado.",
                    esperado=firmado.total, observado=acotar(valor),
                )
            )
            continue
        importe = valido / divisor
        if abs(importe - firmado.total) <= EPSILON_MONTO:
            chequeos.append(
                _chequeo(
                    "integridad", "ok", campo,
                    f"El monto del ap2.checkout ({campo}) coincide con el total firmado.",
                    esperado=firmado.total, observado=importe,
                )
            )
            continue
        chequeos.append(
            _chequeo(
                "integridad", "escalar", campo,
                f"El monto del ap2.checkout ({campo}: {monto(importe, moneda)}) no "
                f"coincide con el total firmado ({monto(firmado.total, moneda)}); el "
                "PaymentMandate usaría ese monto.",
                esperado=firmado.total, observado=importe,
            )
        )
        if cart is None or abs(importe - cart) > EPSILON_MONTO:
            a_comparar_con_tope.append((f"El monto del ap2.checkout ({campo})", importe))

    for tope in _topes(ficha):
        for fuente, importe in a_comparar_con_tope:
            if importe > tope + EPSILON_MONTO:
                resultado = "bloquea"
                detalle = (
                    f"{fuente} ({monto(importe, moneda)}) supera el tope del "
                    f"mandato ({monto(tope, moneda)})."
                )
            else:
                resultado = "ok"
                detalle = (
                    f"{fuente} ({monto(importe, moneda)}) está dentro del tope del "
                    f"mandato ({monto(tope, moneda)})."
                )
            chequeos.append(
                _chequeo("mandato", resultado, "precio", detalle, esperado=tope, observado=importe)
            )
    return chequeos


def _chequeo_lineas(lineas: LineasFirmadas, total: float, moneda: Any) -> Chequeo:
    """Los line_items firmados tienen que sumar el total firmado (si no, `bloquea`)."""
    if lineas.problema is not None or lineas.suma is None:
        return _chequeo(
            "integridad", "bloquea", "line_items",
            f"Los line_items del checkout firmado {lineas.problema or 'son ilegibles'}: "
            "no se puede confirmar que sumen el total firmado.",
            esperado=total, observado=acotar(lineas.observado),
        )
    if abs(lineas.suma - total) > EPSILON_MONTO:
        return _chequeo(
            "integridad", "bloquea", "line_items",
            f"Los line_items del checkout firmado suman {monto(lineas.suma, moneda)} "
            f"(price × quantity) y el total firmado es {monto(total, moneda)}: "
            "el checkout firmado se contradice a sí mismo.",
            esperado=total, observado=lineas.suma,
        )
    return _chequeo(
        "integridad", "ok", "line_items",
        f"Los line_items del checkout firmado (price × quantity) suman el total "
        f"firmado ({monto(total, moneda)}).",
        esperado=total, observado=lineas.suma,
    )


def _chequeos_moneda(
    checkout: CheckoutVisto, firmado: CheckoutFirmado, elegida: Oferta | None, ficha: Ficha
) -> list[Chequeo]:
    """Moneda firmada contra la del cart; la del data part contra la firmada."""
    moneda_cart = elegida.moneda if elegida is not None and elegida.moneda else None
    chequeos: list[Chequeo] = []
    distintas_del_cart: list[tuple[str, Any]] = []

    if not isinstance(firmado.moneda, str) or not firmado.moneda.strip():
        chequeos.append(
            _chequeo(
                "integridad", "escalar", "moneda",
                "El checkout firmado no indica una moneda legible.",
                esperado=acotar(moneda_cart), observado=acotar(firmado.moneda),
            )
        )
    elif moneda_cart is not None and iguales(firmado.moneda, moneda_cart):
        chequeos.append(
            _chequeo(
                "integridad", "ok", "moneda",
                f"La moneda firmada ({moneda_legible(moneda_cart) or 'sin código ISO'}) "
                "coincide con la del carrito evaluado.",
                esperado=acotar(moneda_cart), observado=acotar(firmado.moneda),
            )
        )
    else:
        chequeos.append(
            _chequeo(
                "integridad", "escalar", "moneda",
                "La moneda firmada no coincide con la del carrito evaluado.",
                esperado=acotar(moneda_cart), observado=acotar(firmado.moneda),
            )
        )
        distintas_del_cart.append(("La moneda firmada", firmado.moneda))

    if checkout.currency is not None:
        if iguales(checkout.currency, firmado.moneda):
            chequeos.append(
                _chequeo(
                    "integridad", "ok", "moneda",
                    "La moneda del ap2.checkout coincide con la firmada.",
                    esperado=acotar(firmado.moneda), observado=acotar(checkout.currency),
                )
            )
        else:
            chequeos.append(
                _chequeo(
                    "integridad", "escalar", "moneda",
                    "La moneda del ap2.checkout (la que usaría el PaymentMandate) no "
                    "coincide con la firmada.",
                    esperado=acotar(firmado.moneda), observado=acotar(checkout.currency),
                )
            )
            if moneda_cart is None or not iguales(checkout.currency, moneda_cart):
                distintas_del_cart.append(("La moneda del ap2.checkout", checkout.currency))

    for requisito in _monedas_obligatorias(ficha):
        pedida = requisito.valor
        for fuente, moneda in distintas_del_cart:
            if isinstance(moneda, str) and iguales(moneda, pedida):
                chequeos.append(
                    _chequeo(
                        "mandato", "ok", "moneda",
                        f"{fuente} cumple la moneda del mandato ({acotar(pedida)}).",
                        esperado=pedida, observado=acotar(moneda),
                    )
                )
            elif isinstance(moneda, str) and moneda.strip():
                chequeos.append(
                    _chequeo(
                        "mandato", "bloquea", "moneda",
                        f"{fuente} no cumple la moneda del mandato ({acotar(pedida)}).",
                        esperado=pedida, observado=acotar(moneda),
                    )
                )
    return chequeos


def _chequeo_comercio(comercio: Any, politica: Politica, comercio_id: str | None) -> Chequeo:
    aprobados = list(politica.comercios_aprobados)
    if not isinstance(comercio, str) or not comercio.strip():
        return Chequeo(
            tipo="politica", resultado="escalar", campo="comercio", origen=ORIGEN,
            detalle=(
                "El checkout firmado no identifica al comercio: no se puede "
                "confirmar que esté entre los aprobados por la política."
            ),
            esperado=aprobados, observado=acotar(comercio),
        )
    aprobado = next((a for a in aprobados if iguales(a, comercio)), None)
    if aprobado is None:
        return Chequeo(
            tipo="politica", resultado="bloquea", campo="comercio", origen=ORIGEN,
            detalle=(
                "El comercio que firmó el checkout está fuera de los aprobados por "
                f"la política ({', '.join(acotar(a) for a in aprobados) or 'ninguno'})."
            ),
            esperado=aprobados, observado=acotar(comercio),
        )
    if comercio_id and not contiene([comercio_id], comercio):
        return Chequeo(
            tipo="integridad", resultado="escalar", campo="comercio", origen=ORIGEN,
            detalle=(
                f"El checkout lo firmó un comercio aprobado ({acotar(aprobado)}) "
                f"distinto del que se verificó contra el catálogo público "
                f"({acotar(comercio_id)})."
            ),
            esperado=acotar(comercio_id), observado=acotar(comercio),
        )
    return Chequeo(
        tipo="politica", resultado="ok", campo="comercio", origen=ORIGEN,
        detalle=f"El comercio que firmó el checkout ({acotar(aprobado)}) está aprobado por la política.",
        esperado=aprobados, observado=acotar(comercio),
    )


def _segundos(ahora: datetime | None) -> int:
    """`ahora` en segundos UNIX (None → reloj UTC; sin zona horaria → UTC)."""
    if ahora is None:
        ahora = datetime.now(timezone.utc)
    elif ahora.utcoffset() is None:
        ahora = ahora.replace(tzinfo=timezone.utc)
    return math.floor(ahora.timestamp())


def _chequeos_vigencia(firmado: CheckoutFirmado, ahora: int) -> list[Chequeo]:
    """`exp` e `iat` del JWT (segundos enteros) contra el instante de la evaluación."""
    chequeos: list[Chequeo] = []
    exp, iat = firmado.exp, firmado.iat
    if exp is not AUSENTE:
        if not _entero(exp):
            chequeos.append(
                _chequeo(
                    "integridad", "bloquea", "exp",
                    "El checkout firmado trae un exp que no es un entero (segundos): "
                    "no se puede saber hasta cuándo vale.",
                    esperado=ahora, observado=acotar(exp),
                )
            )
        elif exp <= ahora:
            chequeos.append(
                _chequeo(
                    "mandato", "escalar", "exp",
                    "El checkout firmado venció (exp) antes de esta evaluación: el "
                    "comercio tiene que emitir uno vigente.",
                    esperado=ahora, observado=exp,
                )
            )
        else:
            chequeos.append(
                _chequeo(
                    "mandato", "ok", "exp",
                    "El checkout firmado está vigente (exp posterior a esta evaluación).",
                    esperado=ahora, observado=exp,
                )
            )
    if iat is not AUSENTE:
        if not _entero(iat):
            chequeos.append(
                _chequeo(
                    "integridad", "bloquea", "iat",
                    "El checkout firmado trae un iat que no es un entero (segundos): "
                    "no se puede saber cuándo se emitió.",
                    esperado=ahora, observado=acotar(iat),
                )
            )
        elif iat > ahora + _DESFASAJE_IAT:
            chequeos.append(
                _chequeo(
                    "integridad", "escalar", "iat",
                    "El checkout firmado dice haberse emitido (iat) más de 5 minutos "
                    "después de esta evaluación.",
                    esperado=ahora, observado=iat,
                )
            )
        else:
            chequeos.append(
                _chequeo(
                    "integridad", "ok", "iat",
                    "La emisión del checkout firmado (iat) no está en el futuro.",
                    esperado=ahora, observado=iat,
                )
            )
    return chequeos
