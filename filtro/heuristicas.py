"""Capa (a): heurísticas determinísticas y baratas sobre los textos del proveedor.

Buscan lo que un comercio honesto no tiene por qué escribir en una oferta:
notas dirigidas a un asistente de IA, órdenes de rankear o recomendar,
pedidos de ignorar instrucciones, marcadores de rol de un chat, pedidos de
datos sensibles, nombres de herramientas del agente y texto oculto.

Que una oferta no cumpla el pedido no es una señal (docs/FLUJO.md, regla 4):
"no podemos entregar 20 el lunes" o "no recomendado para gaming" pasan. Los
patrones son estrictos a propósito; lo que se les escapa lo juzga la capa (b)
(`filtro/modelo.py`).

Cada señal es un código corto. Nunca se copia texto del proveedor.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from filtro.textos import GRIEGAS_CONFUSABLES, normalizar

INSTRUCCION_IA = "heuristica:instruccion_ia"
ANULA_INSTRUCCIONES = "heuristica:anula_instrucciones"
MARCADOR_ROL = "heuristica:marcador_rol"
MANIPULACION = "heuristica:manipulacion"
EXFILTRACION = "heuristica:exfiltracion"
HERRAMIENTAS = "heuristica:herramientas"
TEXTO_OCULTO = "heuristica:texto_oculto"
HOMOGLIFOS = "heuristica:homoglifos"

# ---------------------------------------------------------------------------
# Patrones sobre el texto normalizado (minúsculas, sin acentos, `_ - /` = espacio)
# ---------------------------------------------------------------------------

_QUAL_IA = r"(?:(?:ai|llm|automated|autonomous|shopping|purchasing|buying|procurement|virtual|digital)\s+){0,3}"
_SUST_IA = (
    r"(?:assistants?|agents?|llms?|bots?|chatbots?|language\s+models?"
    r"|ai\s+(?:systems?|models?|readers?|tools?))\b"
)
# "Note for AI:" sí; "note for AI powered workflows" no.
_IA_SOLA = r"(?:ai|ia|llm)s?(?=\s*[:\],.;)]|\s*$)"

# Lo que el comercio promociona: "this", "it", "this product", "cart 4"...
_OBJ = (
    r"(?:this\s+(?:product|item|offer|listing|one|cart|model|option)|this|it|us|our\s+\w+"
    r"|me|cart\s*\d+)"
)
_PALABRAS = r"(?:\s+[\w']+){0,3}?"

_PATRONES: dict[str, tuple[str, ...]] = {
    INSTRUCCION_IA: (
        # Notas o mensajes para la IA
        rf"\b(?:note|notice|message|instructions?|reminder|attention|memo|directive|warning)s?"
        rf"\s+(?:for|to)\s+(?:all\s+|any\s+|the\s+|every\s+|each\s+)?{_QUAL_IA}(?:{_SUST_IA}|{_IA_SOLA})",
        r"\b(?:nota|aviso|mensaje|instrucciones|instruccion|atencion|recordatorio|advertencia)"
        r"\s+(?:para|a|al)\s+(?:el\s+|la\s+|los\s+|las\s+|todos\s+los\s+|cualquier\s+)?"
        r"(?:asistentes?|agentes?|ia\b|modelos?\s+de\s+(?:lenguaje|ia)|bots?|llms?)",
        # Saludos a la IA
        rf"\b(?:dear|hey|hello|hi|greetings)\s*,?\s+(?:the\s+)?{_QUAL_IA}(?:{_SUST_IA}|ai\b|llm\b)",
        r"\b(?:hola|querido|querida|estimado|estimada)\s*,?\s+(?:asistente|agente|ia|modelo|bot)\b",
        # "Si sos una IA..."
        r"\bif\s+(?:you\s+are|you're|youre)\s+(?:an?\s+|the\s+)?(?:ai|llm|language\s+model|assistant"
        r"|agent|bot|automated|shopping\s+(?:assistant|agent))\b",
        r"\bas\s+an?\s+(?:ai|llm|language\s+model)\b(?!\s+(?:powered|ready|enabled|based|driven))",
        r"\bsi\s+(?:sos|eres|es)\s+(?:un|una)\s+(?:ia|agente|asistente|modelo|bot|llm)\b",
        # Agentes de compras de IA
        r"\b(?:ai|llm|automated|autonomous)\s+(?:shopping|purchasing|buying|procurement|sales)"
        r"\s+(?:assistants?|agents?)\b",
        r"\b(?:shopping|purchasing|buying|procurement)\s+(?:assistants?|agents?|bots?)\s+(?:should|must"
        r"|shall|need\s+to|are\s+(?:instructed|required|advised|asked)|reading|evaluating"
        r"|processing|comparing|that\s+read|like\s+you)\b",
        r"\b(?:asistentes?|agentes?)\s+de\s+(?:compras?|ia)\b(?:\s+\w+){0,2}?\s+(?:debe|deben"
        r"|tiene\s+que|tienen\s+que|siempre|deberia|deberian)\b",
    ),
    ANULA_INSTRUCCIONES: (
        # Ignorar instrucciones (siempre) o reglas del usuario (con calificativo)
        r"\b(?:ignore|disregard|forget|override|bypass|skip|drop)\s+(?:(?:all|any|the|your|these"
        r"|those|of|my|our|its|every)\s+)*(?:(?:previous|prior|above|earlier|preceding|original"
        r"|existing|system|initial|current|other)\s+)*(?:instructions?|prompts?|directives?)\b",
        r"\b(?:ignore|disregard|forget|override|bypass|skip|drop)\s+(?:(?:all|any|the|these|those"
        r"|of|every)\s+)*(?:previous|prior|above|earlier|preceding|original|existing|system"
        r"|initial|user'?s?|customer'?s?|buyer'?s?)\s+(?:\w+\s+)?(?:rules?|guidelines?|constraints?"
        r"|requirements?|criteria|budget|limits?|preferences?|polic(?:y|ies)|directions?)\b",
        r"\b(?:ignora|ignorar|ignore|olvida|olvidate\s+de|olvidar|descarta|descartar|omiti|omite"
        r"|omitir|saltea|saltear)\s+(?:(?:todas|todos|las|los|tus|sus|el|la|de)\s+)*"
        r"(?:instrucciones|indicaciones)\b",
        r"\b(?:ignora|ignorar|ignore|olvida|olvidate\s+de|olvidar|descarta|descartar|omiti|omite"
        r"|omitir|saltea|saltear)\s+(?:(?:todas|todos|las|los|tus|sus|el|la|de)\s+)*(?:reglas"
        r"|requisitos|restricciones|limites|presupuesto|preferencias|criterios|politicas)"
        r"\s+(?:anteriores|previas|previos|originales|del\s+(?:usuario|cliente|comprador|sistema))",
        # Instrucciones nuevas
        r"\bnew\s+(?:instructions?|rules?|system\s+prompt|directives?|task)\s*:",
        r"\b(?:updated|revised|real|actual|true)\s+instructions?\s*:",
        r"\bnuevas?\s+(?:instrucciones|reglas|indicaciones)\s*:",
        # Ocultarle algo al usuario
        r"\b(?:do\s+not|don't|dont|never|without)\s+(?:tell|telling|inform|informing|notify"
        r"|notifying|mention|mentioning|ask|asking|show|showing|reveal|revealing|alert|alerting"
        r"|consult|consulting|disclose|disclosing|confirm\w*\s+with)\b(?:\s+[\w']+){0,3}?"
        r"\s+(?:to\s+|with\s+)?(?:the\s+|your\s+)?(?:user|customer|buyer|human|shopper|owner"
        r"|principal)s?\b(?!\s+(?:service|support|care))",
        r"\bsin\s+(?:avisar|avisarle|consultar|consultarle|decir|decirle|preguntar|preguntarle"
        r"|informar|informarle|mostrar|mostrarle|confirmarlo\s+con)\b(?:\s+\w+){0,3}?"
        r"\s+(?:al|a\s+la|a\s+los)\s+(?:usuario|usuaria|cliente|comprador|humano)s?",
        r"\bno\s+(?:se\s+lo\s+|se\s+la\s+|le\s+|les\s+)?(?:digas|menciones|avises|informes|muestres"
        r"|consultes|preguntes|cuentes)\b(?:\s+\w+){0,3}?\s+(?:al|a\s+la|a\s+los)\s+(?:usuario"
        r"|cliente|comprador)",
        # Cambiarle el rol
        r"\byou\s+are\s+(?:now\s+)?(?:a|an|the|in)\s+(?:[\w']+\s+){0,3}?(?:assistant|agent|ai|bot|mode)\b",
        r"\b(?:dan|jailbreak|god|unrestricted)\s+mode\b",
        r"\b(?:ahora\s+)?(?:sos|eres)\s+(?:un|una|el|la)\s+(?:\w+\s+){0,2}?(?:asistente|agente|ia|bot)\b",
        # Autoridad falsa: "el usuario ya lo aprobó", "el pedido cambió"
        r"\b(?:user|customer|buyer|principal|owner)s?\s+(?:has|have|had|already)\s+(?:already\s+)?"
        r"(?:pre\s?approved|preapproved|approved|authori[sz]ed)\b",
        r"\b(?:user|customer|buyer)'?s?\s+(?:requirements?|request|instructions?|budget|preferences?"
        r"|criteria|constraints?|order)\s+(?:have|has)\s+(?:now\s+)?(?:been\s+)?(?:updated|changed"
        r"|modified|revised|overridden|replaced|relaxed)\b",
        r"\bel\s+(?:usuario|cliente|comprador)\s+(?:ya\s+)?(?:aprobo|autorizo|preaprobo|pre\s+aprobo"
        r"|cambio|actualizo|modifico)\b",
        r"\b(?:requisitos|pedido|instrucciones|presupuesto|criterios)\s+del\s+(?:usuario|cliente"
        r"|comprador)\s+(?:cambiaron|se\s+actualizaron|se\s+modificaron|fueron\s+(?:actualizados"
        r"|modificados|cambiados))",
    ),
    MARCADOR_ROL: (
        r"\[\s*/?\s*(?:system|sys|inst|instructions?|assistant|developer|admin)\s*\]",
        r"<\s*/?\s*(?:system|sys|instructions?|prompt|assistant|developer|admin|im\s+start|im\s+end)\s*>",
        r"<<\s*/?\s*sys\s*>>",
        r"(?:^|\n)\s*#{1,6}\s*(?:system|admin|developer)\b",
        r"\bsystem\s+(?:prompt|override|instructions?|directive)s?\b",
        r"\bsystem\s+(?:message|note|notice)\s*:",
        r"\b(?:admin|administrator|developer|operator|root)\s+override\b",
        r"\b(?:admin|administrator|developer|operator)\s+(?:command|instruction)s?\s*:",
        r"[\"']role[\"']\s*:\s*[\"'](?:system|developer|assistant)[\"']",
        r"\b(?:mensaje|aviso|instruccion|instrucciones|nota|prompt)\s+del\s+sistema\b",
        r"\b(?:begin|end|start)\s+(?:of\s+)?(?:system|hidden|secret)\s+(?:prompt|message|instructions?)\b",
    ),
    MANIPULACION: (
        # Órdenes de rankear, elegir o recomendar lo propio
        rf"\b(?:always|must|should\s+always|be\s+sure\s+to|make\s+sure\s+(?:to|you))\s+(?:rank|recommend"
        rf"|choose|select|pick|prefer|prioriti[sz]e|favou?r|suggest|promote)\s+{_OBJ}\b",
        r"\b(?:always|must|should)\s+(?:be\s+)?(?:ranked|listed|shown|placed|recommended|selected"
        r"|chosen)\s+(?:first|on\s+top|at\s+the\s+top|above|before|highest|#\s*1)",
        rf"\brank(?:ed|ing)?\s+{_OBJ}{_PALABRAS}\s+(?:first|highest|higher|on\s+top|at\s+the\s+top"
        rf"|#\s*1|number\s+one|above|over|ahead\s+of|before\s+(?:all|the|other|others|any))",
        rf"\b(?:put|list|show|display|present|position)\s+{_OBJ}{_PALABRAS}\s+(?:first|at\s+the\s+top"
        rf"|above\s+(?:all|the\s+other|other|others|every|any))",
        rf"\b(?:prioriti[sz]e|favou?r|place)\s+{_OBJ}{_PALABRAS}\s+(?:above|over|ahead\s+of|before)"
        rf"\s+(?:all|the\s+other|other|others|every|any|competing|the\s+rest)",
        rf"\b(?:recommend|choose|select|pick|prefer)\s+{_OBJ}{_PALABRAS}\s+(?:above|ahead\s+of|before)"
        rf"\s+(?:all|the\s+other|other|others|every|any|competing|the\s+rest)",
        rf"\b(?:recommend|present|show|suggest|offer|pitch|propose)\s+{_OBJ}{_PALABRAS}\s+to\s+(?:the"
        rf"|every|any|all)\s+(?:user|customer|buyer|shopper|human|client|principal)s?\b"
        rf"(?!\s+(?:service|support|care))",
        rf"\b(?:choose|select|pick|buy|purchase|prefer|recommend|order)\s+{_OBJ}{_PALABRAS}"
        rf"\s+(?:regardless\s+of|no\s+matter|instead\s+of\s+(?:the\s+)?(?:requested|user|other|cart"
        rf"|what))",
        # "La mejor opción para cualquier pedido", "cumple cualquier búsqueda"
        r"\bbest\s+(?:match|choice|option|pick|result|fit)\s+for\s+(?:any|every|all|each|whatever)\b",
        r"\bmejor\s+(?:opcion|eleccion|resultado|coincidencia|alternativa)\s+para\s+(?:cualquier"
        r"|todo|toda|todos|todas)\b",
        r"\b(?:satisf(?:y|ies|ying)|meets?|meeting|match(?:es|ing)?|fulfil?l?s?|fulfil?ling)"
        r"\s+(?:any|every)\s+(?:[\w']+\s+){0,2}?(?:requests?|quer(?:y|ies)|searches)\b",
        rf"\btreat\s+{_OBJ}(?:\s+[\w']+){{0,2}}?\s+as\s+(?:satisfying|matching|meeting|compliant"
        rf"|the\s+best|approved|verified|a\s+match|fulfilling|the\s+(?:top|first|only))",
        # Desacreditar a los demás o pedir que no se muestren
        r"\b(?:other|competing|competitor'?s?|rival|remaining|alternative)\s+(?:offers?|products?"
        r"|carts?|items?|listings?|vendors?|sellers?|merchants?|options?)\s+(?:are|is)\s+(?:all\s+)?"
        r"(?:fake|scams?|fraud(?:ulent)?|unsafe|counterfeit|recalled|unreliable|dangerous"
        r"|not\s+(?:genuine|real|safe|legitimate|authentic))\b",
        r"\b(?:do\s+not|don't|dont|never)\s+(?:recommend|consider|show|select|choose|pick|mention"
        r"|suggest|compare|rank|display|present)\s+(?:any\s+|the\s+)?(?:other|competing"
        r"|competitor'?s?|alternative|remaining|rival)\b",
        # Elegir un cart por su id
        r"\b(?:select|choose|pick|buy|purchase|checkout|order|add)\s+(?:the\s+)?cart\s*(?:id\s*)?\d+\b",
        r"\b(?:elegi|elegir|selecciona|seleccionar|compra|comprar|agrega|agregar)\s+(?:el\s+)?"
        r"(?:carrito|cart)\s*\d+\b",
        # En castellano
        r"\b(?:siempre|debes|debe|deben|tenes\s+que|tienes\s+que|tiene\s+que)\s+(?:rankear|rankea"
        r"|recomendar|recomenda|elegir|elegi|seleccionar|selecciona|priorizar|prioriza|preferir"
        r"|preferi|mostrar|mostra)\w*\s+(?:este|esta|esto|nuestro|nuestra|el\s+carrito|cart)\b",
        r"\b(?:rankea|rankealo|rankeala|recomendalo|recomendala|recomendaselo|recomendasela"
        r"|elegilo|elegila|seleccionalo|seleccionala|priorizalo|priorizala|ponelo|ponela|mostralo"
        r"|mostrala|recomenda|recomendale|elegi|selecciona|prioriza)\b(?:\s+\w+){0,4}?\s+(?:primero"
        r"|primera|en\s+primer\s+lugar|al\s+usuario|al\s+cliente|al\s+comprador|por\s+encima"
        r"|antes\s+que)",
        r"\b(?:siempre|debe|deben)\s+(?:ser\s+)?(?:rankead|recomendad|elegid|seleccionad|priorizad"
        r"|mostrad)[oa]s?\s+(?:primero|en\s+primer\s+lugar)",
        r"\bsiempre\s+(?:rankea|recomenda|elegi|selecciona|prioriza)\b",
    ),
    EXFILTRACION: (
        r"\b(?:send|share|reveal|provide|give|include|print|output|tell|disclose|forward|email|post"
        r"|leak|repeat|reply\s+with|respond\s+with|answer\s+with|paste|attach)\s+(?:me\s+|us\s+)?"
        r"(?:with\s+)?(?:the\s+|your\s+|all\s+|any\s+|their\s+|his\s+|her\s+)?(?:(?:user|customer"
        r"|buyer|owner|client|shopper|principal)(?:'?s|s')?\s+)?(?:full\s+)?(?:credit\s+card"
        r"|card\s+(?:number|details|data|info|cvv)|cvv|cvc|security\s+code|pin|password|passcode"
        r"|api\s+keys?|secret\s+keys?|private\s+keys?|access\s+tokens?|credentials?|mandates?|otp"
        r"|one\s+time\s+(?:password|code)|ssn|social\s+security|bank\s+(?:account|details))\b",
        r"\b(?:reveal|print|output|repeat|show|tell|disclose|leak|share)\s+(?:me\s+|us\s+)?(?:your|the)"
        r"\s+(?:system\s+prompt|(?:initial|original|hidden|secret|internal|full)\s+(?:instructions"
        r"|prompt|rules|configuration))\b",
        r"\b(?:envia|enviame|envianos|manda|mandame|mandanos|comparti|compartime|compartinos|revela"
        r"|revelame|pasame|pasanos|decime|decinos|dame|danos|inclui|proporciona|proporcioname"
        r"|indicame|responde\s+con|contesta\s+con)\s+(?:\w+\s+){0,3}?(?:numero\s+de\s+(?:la\s+)?tarjeta"
        r"|tarjeta\s+de\s+credito|datos\s+de\s+(?:la\s+)?tarjeta|cvv|cvc|contrasena|token|credenciales"
        r"|mandato|codigo\s+otp|prompt\s+del\s+sistema|instrucciones\s+del\s+sistema)\b",
    ),
    HERRAMIENTAS: (
        r"\b(?:call|invoke|execute|trigger)\s+(?:the\s+)?(?:[\w']+\s+){0,2}?(?:tool|function|api"
        r"|endpoint)s?\b",
    ),
}
_COMPILADOS = {
    codigo: tuple(re.compile(p) for p in patrones) for codigo, patrones in _PATRONES.items()
}

# ---------------------------------------------------------------------------
# Patrones sobre el texto tal cual (mayúsculas, guiones bajos, URLs)
# ---------------------------------------------------------------------------

# Sobre texto que puede traer miles de blancos seguidos: sin cuantificadores
# vecinos que puedan repartirse lo mismo (eso es cuadrático) y con topes.
_CRUDOS: dict[str, tuple[re.Pattern[str], ...]] = {
    MARCADOR_ROL: (
        # "SYSTEM:" al principio o después de un punto (no "OPERATING SYSTEM:")
        re.compile(
            r"(?:^|[\n.!?\]\)])[ \t]*(?:[^\w\s]{1,3}[ \t]*)?(?:SYSTEM|ASSISTANT|DEVELOPER|ADMIN)[ \t]*:"
        ),
        re.compile(r"<\|[^|>\s]{1,30}\|>"),
    ),
    HERRAMIENTAS: (
        re.compile(
            r"\b(?:select_cart|create_checkout|find_products|initiate_payment|update_cart"
            r"|send_signed_payment|sign_mandate|get_shipping_address|create_payment_mandate"
            r"|update_chosen_cart_mandate|delegate_to_\w+|transfer_to_agent)\b",
            re.IGNORECASE,
        ),
        re.compile(r"[\"'](?:tool_calls?|function_call|tool_use|tool_code)[\"']", re.IGNORECASE),
    ),
    EXFILTRACION: (
        # imagen markdown con datos en la URL: ![x](https://host/p?d=...)
        re.compile(r"!\[[^\]\n]{0,200}\]\([ \t]{0,10}https?://[^)\s?]{0,500}\?[^)\s]{0,500}="),
        re.compile(r"[?&]\w{1,50}=\{"),  # parámetro con plantilla: ?card={card_number}
    ),
    TEXTO_OCULTO: (
        re.compile(r"<!--"),
        re.compile(r"display\s*:\s*none", re.IGNORECASE),
        re.compile(r"visibility\s*:\s*hidden", re.IGNORECASE),
        re.compile(r"font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?\s*[;\"'}]", re.IGNORECASE),
        re.compile(r"opacity\s*:\s*0(?:\.0+)?\s*[;\"'}]", re.IGNORECASE),
        re.compile(r"\s{80,}"),  # texto empujado fuera de la vista
    ),
}

# ---------------------------------------------------------------------------
# Caracteres invisibles y homoglifos
# ---------------------------------------------------------------------------

_BIDI_RTL = frozenset({"R", "AL"})
_ZW_UNION = frozenset({"‌", "‍"})  # ZWNJ y ZWJ: legítimos en emojis y en escrituras no latinas
_MARCAS_RTL = frozenset({"‎", "‏", "؜"})  # LRM, RLM, ALM: legítimas en texto RTL


def _es_ascii_alnum(c: str) -> bool:
    return c.isascii() and c.isalnum()


def _oculto(texto: str) -> bool:
    rtl = None
    for i, c in enumerate(texto):
        o = ord(c)
        cat = unicodedata.category(c)
        if 0xFE00 <= o <= 0xFE0D or 0xE0100 <= o <= 0xE01EF:
            return True  # selectores de variación usados para esconder datos
        if cat == "Cc" and c not in "\t\n\r":
            return True
        if cat != "Cf":
            continue
        if c == "﻿" and i == 0:
            continue  # BOM al principio: codificación, no ataque
        if c in _ZW_UNION:
            vecinos = texto[i - 1 : i] + texto[i + 1 : i + 2]
            if not any(_es_ascii_alnum(v) for v in vecinos):
                continue
            return True
        if c in _MARCAS_RTL:
            if rtl is None:
                rtl = any(unicodedata.bidirectional(x) in _BIDI_RTL for x in texto)
            if rtl:
                continue
        return True  # bidi, ancho cero, etiquetas (U+E00xx), guion suave, etc.
    return False


_PALABRA_UNICODE = re.compile(r"\w+")


def _homoglifos(texto: str) -> bool:
    """Una palabra con letras latinas y cirílicas (o griegas que se confunden)."""
    if texto.isascii():
        return False
    for palabra in _PALABRA_UNICODE.findall(unicodedata.normalize("NFKC", texto)):
        if palabra.isascii():
            continue
        latina = otra = False
        for c in palabra:
            if c.isascii():
                latina = latina or c.isalpha()
            elif c in GRIEGAS_CONFUSABLES or "CYRILLIC" in unicodedata.name(c, ""):
                otra = True
        if latina and otra:
            return True
    return False


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------


def revisar_texto(texto: str) -> set[str]:
    senales: set[str] = set()
    if _oculto(texto):
        senales.add(TEXTO_OCULTO)
    if _homoglifos(texto):
        senales.add(HOMOGLIFOS)
    for codigo, patrones in _CRUDOS.items():
        if codigo not in senales and any(p.search(texto) for p in patrones):
            senales.add(codigo)
    normal = normalizar(texto)
    for codigo, patrones in _COMPILADOS.items():
        if codigo not in senales and any(p.search(normal) for p in patrones):
            senales.add(codigo)
    return senales


def revisar(textos: Iterable[str]) -> list[str]:
    """Códigos de las señales que aparecen en algún texto (ordenados, sin
    repetidos). Lista vacía = las heurísticas no ven nada."""
    senales: set[str] = set()
    for texto in textos:
        senales |= revisar_texto(texto)
    return sorted(senales)
