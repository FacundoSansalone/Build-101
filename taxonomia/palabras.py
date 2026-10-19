"""Categoría de la taxonomía de Google a partir de las palabras del pedido.

Determinístico, sin modelo ni I/O: una tabla de frases (español e inglés,
sin tildes) a IDs del subconjunto del repo. Sirve de respaldo cuando no hay
clasificador (`taxonomia.clasificar` necesita `decisiones`) o el clasificador
no devuelve nada, para que la ficha de la compra siempre tenga categoría.

Gana la frase más larga que aparece en el texto ("computadora de escritorio"
le gana a "computadora" y a "escritorio"); con dos del mismo largo, la
categoría más profunda del árbol. Una palabra ambigua sola ("pantalla",
"silla de comer") no se mapea: mejor sin categoría que con una equivocada.
"""

from __future__ import annotations

import re
import unicodedata

from taxonomia.arbol import Taxonomia

# ID → frases. Los IDs están en `datos/taxonomy-with-ids.en-US.subconjunto.txt`
# (un test lo exige); las frases van en minúscula y sin tildes.
FRASES: dict[str, tuple[str, ...]] = {
    # Electronics > Computers
    "328": (
        "notebook", "notebooks", "laptop", "laptops", "portatil", "portatiles",
        "computadora portatil", "computadoras portatiles", "ultrabook", "ultrabooks",
    ),
    "325": (
        "computadora de escritorio", "computadoras de escritorio", "pc de escritorio",
        "pcs de escritorio", "desktop", "desktops", "computadora de mesa",
    ),
    "4745": ("tablet", "tablets", "tableta", "tabletas", "ipad", "ipads"),
    "278": (
        "computadora", "computadoras", "computador", "computadores", "ordenador",
        "ordenadores", "computer", "computers", "pc", "pcs",
    ),
    # Electronics > Video
    "305": (
        "monitor", "monitores", "computer monitor", "monitor de pc", "monitores de pc",
        "pantalla para pc", "pantalla de computadora",
    ),
    "404": (
        "televisor", "televisores", "television", "televisiones", "tele", "smart tv",
        "tv", "tvs", "televisor led",
    ),
    "396": ("proyector", "proyectores", "projector", "projectors"),
    # Electronics > Electronics Accessories > Computer Components
    "303": ("teclado", "teclados", "keyboard", "keyboards"),
    "304": ("mouse", "mouses", "raton", "ratones", "mice", "trackball"),
    "380": (
        "disco duro", "discos duros", "disco externo", "discos externos", "hard drive",
        "hard drives", "ssd", "ssd externo",
    ),
    "3712": (
        "pendrive", "pendrives", "pen drive", "memoria usb", "memorias usb",
        "usb flash", "flash drive", "flash drives",
    ),
    # Electronics > otros
    "500106": ("impresora", "impresoras", "printer", "printers", "multifuncion"),
    "267": (
        "celular", "celulares", "telefono celular", "telefonos celulares", "smartphone",
        "smartphones", "movil", "moviles", "telefono movil", "mobile phone", "iphone",
    ),
    "1294": (
        "consola", "consolas", "consola de videojuegos", "consolas de videojuegos",
        "playstation", "xbox", "game console", "video game console",
    ),
    "5497": ("router", "routers", "router wifi", "routers wifi", "enrutador", "wireless router"),
    "543626": (
        "auricular", "auriculares", "headphone", "headphones", "audifono", "audifonos",
        "cascos",
    ),
    "543627": ("headset", "headsets", "auriculares con microfono", "auricular con microfono"),
    "249": ("parlante", "parlantes", "altavoz", "altavoces", "speaker", "speakers", "bafle"),
    # Cameras & Optics
    "312": ("webcam", "webcams", "camara web", "camaras web"),
    "152": ("camara de fotos", "camaras de fotos", "camara digital", "camaras digitales",
            "digital camera"),
    "142": ("camara", "camaras", "camera", "cameras"),
    # Furniture
    # "silla" sola es ambigua (comedor u oficina) y las de oficina no cuelgan
    # de Chairs (443) sino de Office Furniture: sin frase, sin categoría.
    "2045": (
        "silla de oficina", "sillas de oficina", "silla ergonomica", "sillas ergonomicas",
        "office chair", "office chairs", "silla gamer", "sillas gamer",
        "silla de escritorio", "sillas de escritorio", "desk chair", "desk chairs",
    ),
    "4191": ("escritorio", "escritorios", "desk", "desks", "mesa de trabajo"),
    # Otros
    "100": ("mochila", "mochilas", "backpack", "backpacks"),
    "736": (
        "cafetera", "cafeteras", "maquina de cafe", "maquinas de cafe", "espresso",
        "coffee maker", "coffee makers",
    ),
    "187": (
        "zapatilla", "zapatillas", "zapato", "zapatos", "calzado", "shoe", "shoes",
        "sneaker", "sneakers", "botines",
    ),
}


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y con un espacio entre palabras."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c)
    )
    return " ".join(re.findall(r"[a-z0-9]+", sin_tildes.lower()))


def _aparece(frase: str, texto: str) -> bool:
    return f" {frase} " in f" {texto} "


def categoria_por_palabras(texto: str, taxonomia: Taxonomia | None = None) -> str | None:
    """El ID de la categoría que nombra el texto, o None si no nombra ninguna.

    Gana la frase más larga; con un empate, la categoría más profunda si se
    pasa el árbol, y si no, la primera de `FRASES`.
    """
    limpio = normalizar(texto)
    if not limpio:
        return None
    mejor: tuple[int, int, int] | None = None  # (largo de la frase, nivel, -orden)
    elegido: str | None = None
    for orden, (id_categoria, frases) in enumerate(FRASES.items()):
        for frase in frases:
            if not _aparece(frase, limpio):
                continue
            categoria = taxonomia.get(id_categoria) if taxonomia is not None else None
            clave = (len(frase), categoria.nivel if categoria else 0, -orden)
            if mejor is None or clave > mejor:
                mejor, elegido = clave, id_categoria
    return elegido
