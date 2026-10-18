"""Modelos compartidos del Pre-Sign Guard (Pydantic v2).

Acordados en el paso 0 a partir de docs/CONTRATOS_PROPUESTOS.md. Las
decisiones están anotadas en CLAUDE.md → "Decisiones".

**No se cambian sin acordarlo entre los tres.**
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Tipos comunes
# ---------------------------------------------------------------------------

Status = Literal["aprobado", "con_avisos", "escalar", "bloqueado"]

# Solo estos dos llegan a la firma (mismo criterio que demo_world/benchmark.py).
STATUS_LLEGAN_A_FIRMA: frozenset[str] = frozenset({"aprobado", "con_avisos"})

Origen = Literal[
    "cart_ap2",          # ap2.cart (lo que ve el agente; amount = lo que se cobra)
    "declarado",         # demo.offer_details (lo que dice el comercio)
    "catalogo_publico",  # GET :8011/products/<sku>
    "checkout_firmado",  # ap2.checkout (JWT firmado por el comercio)
    "ficha",             # lo que confirmó el usuario
    # Etapa 2, categorías variadas (fuentes que el vendedor no controla, o
    # que publica para todos):
    "fabricante",        # ficha del fabricante por GTIN (p. ej. Icecat)
    "pagina_publica",    # JSON-LD schema.org de la página pública del vendedor
]

EstadoEvidencia = Literal[
    "verificado",       # coincide con el catálogo público
    "solo_declarado",   # no hay fuente independiente para comparar
    "contradicho",      # no coincide con el catálogo público
    "no_verificable",   # no se puede verificar antes de comprar (calidad, entrega)
]

CampoRequisito = Literal[
    "precio", "moneda", "categoria", "uso", "talle", "marca", "comercio",
    "cantidad", "entregas",  # etapa 2 (docs/FLUJO.md)
    # Categorías variadas: cualquier atributo del producto, con el nombre de
    # schema.org (`gtin`, `model`, `color`…) o el de `additionalProperty`
    # (p. ej. "memoria_ram"). El nombre va en `Requisito.atributo`.
    "atributo",
]

# Data part que agrega el proxy a la tarea A2A con el veredicto. Va en un
# artefacto aparte: el ap2.checkout firmado no se toca.
GUARD_VEREDICTO_KEY = "guard.veredicto"

# Data part que agrega el proxy a la respuesta de find_products cuando el
# usuario aceptó una oportunidad: los parámetros nuevos para el agente del
# cliente. Como el veredicto, solo lo pone el proxy (si viene del comercio,
# se borra).
GUARD_PARAMETROS_KEY = "guard.parametros_autorizados"

# ---------------------------------------------------------------------------
# Ficha del pedido
# ---------------------------------------------------------------------------

# scenarios.json → intent.hard / intent.preferred  →  (campo, operador)
_CLAVES_INTENT: dict[str, tuple[CampoRequisito, str]] = {
    "max_price": ("precio", "maximo"),
    "currency": ("moneda", "igual"),
    "category": ("categoria", "igual"),
    "use": ("uso", "igual"),
    "size": ("talle", "incluye"),
    "brand": ("marca", "igual"),
    "merchant": ("comercio", "igual"),
    "quantity": ("cantidad", "igual"),
    "deliveries": ("entregas", "cubre"),  # [{"quantity": 10, "date": "2026-10-19"}]
}


class Entrega(BaseModel):
    """`cantidad` unidades a más tardar en `fecha`.

    En un cronograma (lista) las cantidades se suman: [10 el lunes, 10 el
    jueves] = 10 para el lunes y 20 en total para el jueves.
    """

    cantidad: int = Field(gt=0)
    fecha: date


class Requisito(BaseModel):
    campo: CampoRequisito
    # incluye: talle ∈ talles. cubre: el cronograma de entregas de la oferta
    # cubre, acumulado, cada `Entrega` pedida (valor = list[Entrega]).
    # minimo: valor ≥ (p. ej. memoria ≥ 16 GB).
    operador: Literal["igual", "maximo", "minimo", "incluye", "cubre"]
    valor: Any
    obligatorio: bool  # True = hard, False = preferido
    # Categorías variadas (opcionales; los requisitos de siempre no los usan):
    atributo: str | None = None  # nombre del atributo si campo == "atributo"
    unidad: str | None = None    # unidad del valor ("GB", "in", "kg"…), si tiene
    # De dónde salió el requisito: lo dijo el usuario, lo infirió el agente
    # (y el usuario lo confirmó), lo aceptó en una oportunidad o lo exige la
    # política. None = las fichas de antes (scenarios.json).
    origen: Literal["dicho", "inferido", "aceptado", "politica"] | None = None

    @model_validator(mode="after")
    def _atributo_con_nombre(self) -> Requisito:
        if self.campo == "atributo" and not (self.atributo and self.atributo.strip()):
            raise ValueError('un requisito de campo "atributo" necesita el nombre en `atributo`')
        return self


class Ficha(BaseModel):
    """El pedido del usuario en campos estructurados. El usuario la confirma.

    Criterio "lowest_price_among_compliant" (decisión 2 del paso 0): el
    ranking ordena por obligatorios cumplidos, después por preferidos
    cumplidos (más es mejor) y después por precio (menor es mejor).
    """

    pedido_original: str
    requisitos: list[Requisito]
    criterio: Literal["lowest_price_among_compliant"] = (
        "lowest_price_among_compliant"
    )
    confirmada: bool = False  # True cuando el usuario la aceptó
    # Categoría en la taxonomía de productos de Google (ID, como texto), si el
    # intake la clasificó. None = las fichas de antes.
    categoria_taxonomia: str | None = None

    @classmethod
    def desde_intent(cls, intent: dict, confirmada: bool = True) -> Ficha:
        """Adaptador desde scenarios.json → intent (benchmark, fixtures, demo).

        La ficha de scenarios.json es la de la demo y se toma como
        confirmada. Una clave desconocida es un error: un requisito que no se
        puede escribir no se puede chequear, y no se descarta en silencio.
        """
        requisitos: list[Requisito] = []
        for grupo, obligatorio in (("hard", True), ("preferred", False)):
            for clave, valor in (intent.get(grupo) or {}).items():
                if clave not in _CLAVES_INTENT:
                    raise ValueError(f"Clave de intent desconocida: {grupo}.{clave}")
                campo, operador = _CLAVES_INTENT[clave]
                requisitos.append(
                    Requisito(
                        campo=campo,
                        operador=operador,
                        valor=valor,
                        obligatorio=obligatorio,
                    )
                )
        criterio = intent.get("criterion", "lowest_price_among_compliant")
        return cls(
            pedido_original=intent.get("user_request", ""),
            requisitos=requisitos,
            criterio=criterio,
            confirmada=confirmada,
        )


# ---------------------------------------------------------------------------
# Oferta (salida del extractor + verificador)
# ---------------------------------------------------------------------------


class Evidencia(BaseModel):
    campo: str
    declarado: Any = None   # valor según demo.offer_details (o el cart_ap2)
    publico: Any = None     # valor según el catálogo público (None si no hay)
    estado: EstadoEvidencia
    origen_usado: Origen    # qué fuente usan los chequeos para este campo


class ValorAtributo(BaseModel):
    """Valor de un atributo genérico (schema.org `PropertyValue`: value + unitCode)."""

    valor: Any
    unidad: str | None = None


class Oferta(BaseModel):
    """Un producto ofrecido, solo con campos tipados. Sin texto libre."""

    cart_id: str
    sku: str
    titulo: str
    marca: str
    categoria: str
    uso: str
    precio: float           # el amount del ap2.cart: es lo que se va a cobrar
    moneda: str
    stock: int | None = None
    talles: list[int] = Field(default_factory=list)
    # Etapa 2: lo que el comercio declara que entrega (no se verifica antes de
    # comprar). `cantidad` = unidades del cart; `entregas` = cronograma.
    cantidad: int | None = None
    entregas: list[Entrega] = Field(default_factory=list)
    # Categorías variadas (opcionales): código de barras, categoría en la
    # taxonomía de Google y atributos genéricos con nombre de schema.org.
    gtin: str | None = None
    categoria_taxonomia: str | None = None
    atributos: dict[str, ValorAtributo] = Field(default_factory=dict)
    evidencia: dict[str, Evidencia] = Field(default_factory=dict)
    # El texto libre (item_label, description) NO llega a ningún chequeo.
    # Se guarda solo para el recibo y el dashboard.
    texto_libre_descartado: str | None = Field(default=None, exclude=True)


class OfertasVistas(BaseModel):
    """Lo que llegó del comercio, tal cual. Mismo formato que offers_seen."""

    carts: list[dict]          # ap2.cart sin modificar
    offer_details: list[dict]  # demo.offer_details sin modificar
    # Identidad del comercio (p. ej. merchant.id del catálogo público). La
    # completa quien llama (proxy o adaptador); el chequeo de política la usa.
    comercio_id: str | None = None


class CheckoutVisto(BaseModel):
    """El data part ap2.checkout tal como lo mandó el comercio, sin modificar.

    Lleva el checkout_jwt que firmó el comercio y que el usuario va a firmar
    después en su CheckoutMandate. El Guard lo LEE (decodifica el payload y
    recalcula el hash) para atarlo al cart que evaluó; nunca lo modifica.
    `item_label` y el `title` del JWT son texto libre: ningún chequeo los lee.
    """

    model_config = ConfigDict(extra="allow")  # item_label, amount_cents… tal cual

    checkout_jwt: str
    checkout_hash: str | None = None   # el que declaró el comercio
    cart_id: str | None = None
    amount: float | None = None
    currency: str | None = None


# ---------------------------------------------------------------------------
# Política de compras
# ---------------------------------------------------------------------------


class Politica(BaseModel):
    comercios_aprobados: list[str] = Field(default_factory=lambda: ["merchant_1"])
    monto_maximo_sin_aprobacion: float | None = None
    # Reglas tipo "más de USD 5.000 requiere tres cotizaciones": a futuro.


# ---------------------------------------------------------------------------
# Chequeo y veredicto
# ---------------------------------------------------------------------------


class Chequeo(BaseModel):
    tipo: Literal["cumplimiento", "evidencia", "integridad", "politica", "mandato"]
    resultado: Literal["ok", "aviso", "escalar", "bloquea"]
    detalle: str                       # legible por humanos, para el dashboard
    campo: str | None = None
    esperado: Any = None
    observado: Any = None
    origen: Origen | None = None


class Recibo(BaseModel):
    """Registro firmado de cómo se llegó a la decisión.

    Referencia los objetos firmados de AP2 por hash: nunca los modifica.
    `firma` es un JWS compacto ES256 cuyo payload es `payload_canonico()`.
    """

    id: str
    emitido: datetime
    ficha: Ficha
    consideradas: list[str]                  # cart_ids
    filtradas: dict[str, list[str]]          # cart_id -> motivos
    verificaciones: list[Evidencia]
    ranking: list[str]
    elegido: str
    por_que_gano: str
    sin_verificar: list[str]                 # p. ej. "calidad", "entrega", "reputación"
    status: Status
    checkout_hash: str | None = None         # ap2.checkout → checkout_hash
    firma: str | None = None                 # JWS compacto ES256
    kid: str | None = None

    def payload_canonico(self) -> bytes:
        """Bytes que se firman: todo menos `firma` y `kid`, JSON canónico."""
        datos = self.model_dump(mode="json", exclude={"firma", "kid"})
        return json.dumps(
            datos, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


class Veredicto(BaseModel):
    status: Status                     # se llama así por compatibilidad con el benchmark
    cart_id_elegido: str
    cart_id_recomendado: str | None    # el primero del ranking propio
    ranking: list[str]                 # cart_ids, del mejor al peor
    chequeos: list[Chequeo]
    motivo: str                        # una frase: por qué este veredicto
    recibo: Recibo | None = None


# ---------------------------------------------------------------------------
# La firma de evaluar() (la implementa guard/; el proxy usa un stub igual)
# ---------------------------------------------------------------------------


class Evaluador(Protocol):
    def __call__(
        self,
        ficha: Ficha,
        ofertas_vistas: OfertasVistas,
        cart_id_elegido: str,
        catalogo: dict[str, dict],      # sku -> producto del catálogo público
        politica: Politica | None = None,
        checkout_hash: str | None = None,
        ahora: datetime | None = None,  # para el recibo; None = ahora (UTC)
        checkout: CheckoutVisto | None = None,  # ap2.checkout firmado, si ya llegó
    ) -> Veredicto:
        """Determinística y sin I/O. Mismas entradas, mismo veredicto.

        Con `checkout`, además ata lo firmado por el comercio al cart evaluado
        (chequeos con origen "checkout_firmado"). Sin él (benchmark, antes del
        checkout) ese chequeo no se hace.
        """
        ...


# ---------------------------------------------------------------------------
# Etapa 2: contexto del pedido y oportunidades (docs/FLUJO.md)
# ---------------------------------------------------------------------------


class Flexibilidad(BaseModel):
    descripcion: str
    origen: Literal["dicha", "inferida"]  # la dijo el usuario o la dedujo el agente


class Turno(BaseModel):
    """Una repregunta del intake y lo que contestó el usuario."""

    pregunta: str
    respuesta: str | None = None  # None = omitió o venció


class Contexto(BaseModel):
    """Lo que hay detrás del pedido. Conocerlo NO autoriza a cambiar nada:
    los parámetros vigentes siguen siendo los de la ficha de la compra."""

    pedido: str                     # lo que pidió el agente del cliente
    # Repreguntas del intake hasta tener todo lo necesario (además de la
    # primera `pregunta`/`respuesta`). Vacío = una sola pregunta, como antes.
    historial: list[Turno] = Field(default_factory=list)
    pregunta: str                   # lo que le preguntó nuestro agente al usuario
    respuesta: str | None = None    # lo que contestó (None = omitió o venció)
    necesidad: str | None = None    # la necesidad en una frase
    # Cronograma real, si la necesidad lo tiene ("10 el lunes y 10 el jueves").
    necesidad_entregas: list[Entrega] = Field(default_factory=list)
    flexibilidades: list[Flexibilidad] = Field(default_factory=list)


class Cambio(BaseModel):
    """Una diferencia entre lo pedido y lo que ofrece el comercio."""

    campo: CampoRequisito
    pedido: Any = None
    ofrecido: Any = None


class Oportunidad(BaseModel):
    """Una oferta que no cumple el pedido pero podría cubrir la necesidad.

    La propone nuestro agente; solo el usuario la autoriza. Los números de
    `cambios` y `requisitos_propuestos` salen de los datos de la oferta (no
    del texto del LLM); `por_que` es la explicación para el usuario.
    """

    id: str
    cart_id: str
    sku: str = ""
    titulo: str = ""
    precio: float | None = None
    moneda: str = ""
    cambios: list[Cambio]
    por_que: str
    requisitos_propuestos: list[Requisito]


class ResultadoFiltro(BaseModel):
    """Salida del filtro de seguridad (Dev 1, `filtro/`)."""

    pasa: bool
    motivo: str = ""  # texto fijo del filtro: nunca copia texto del proveedor
    senales: list[str] = Field(default_factory=list)


class Filtro(Protocol):
    def __call__(self, respuesta: dict[str, Any]) -> ResultadoFiltro:
        """Revisa la respuesta del proveedor (el `result` de la tarea A2A)
        antes que cualquier otra etapa. Si falla, el proxy bloquea."""
        ...


# ---------------------------------------------------------------------------
# Evento (proxy → dashboard, por SSE en :8010/guard/events y en JSONL)
# ---------------------------------------------------------------------------


class Evento(BaseModel):
    id: str
    ts: datetime
    contexto_id: str | None = None     # context_id de A2A, agrupa una compra
    escenario: str | None = None       # demo_scenario
    guard_activo: bool
    tipo: Literal[
        "busqueda_enviada",     # find_products hacia el comercio
        "ofertas_recibidas",    # carts + offer_details
        "checkout_pedido",      # create_checkout(cart_id)
        "checkout_recibido",    # ap2.checkout firmado
        "veredicto",
        "reenviado",            # el proxy dejó pasar la respuesta
        "bloqueado",            # el proxy cortó el flujo
        "guard_cambiado",       # el interruptor Guard sí/no cambió
        "error",
        # Etapa 2 (docs/FLUJO.md):
        "pedido_detectado",     # nuestro agente le pregunta el porqué al usuario
        "contexto_registrado",  # el usuario contestó (u omitió, o venció)
        "oportunidad",          # consulta al usuario: negociación en pausa
        "oportunidad_decidida", # aceptada, rechazada o vencida
    ]
    datos: dict[str, Any] = Field(default_factory=dict)  # JSON del cart, JWT, Veredicto…
