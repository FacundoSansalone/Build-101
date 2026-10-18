"""Ficha desde el `intent` de `scenarios.json`, con atributos y categoría.

`Ficha.desde_intent()` (contrato) lee `hard`/`preferred` con las claves de
siempre. Los escenarios de otras categorías suman, sin tocar esas claves:

    "attributes": {
      "hard":      [{"atributo": "memoria_ram", "operador": "minimo", "valor": 16, "unidad": "GB"}],
      "preferred": [{"atributo": "weight", "operador": "maximo", "valor": 1.5, "unidad": "kg"}]
    },
    "google_product_category": "328"

Cada elemento es un `Requisito` de campo "atributo" (mismos nombres de campo
que el contrato). Una clave desconocida es un error, como en el contrato: un
requisito que no se puede escribir no se puede chequear.
"""

from __future__ import annotations

from typing import Any

from contracts import Ficha, Requisito

_CLAVES_ATRIBUTO = frozenset({"atributo", "operador", "valor", "unidad", "origen"})


def ficha_desde_intent(intent: dict[str, Any], confirmada: bool = True) -> Ficha:
    ficha = Ficha.desde_intent(intent, confirmada=confirmada)
    requisitos = list(ficha.requisitos)
    atributos = intent.get("attributes")
    if atributos is not None:
        if not isinstance(atributos, dict) or set(atributos) - {"hard", "preferred"}:
            raise ValueError('intent.attributes tiene que ser {"hard": [...], "preferred": [...]}')
        for grupo, obligatorio in (("hard", True), ("preferred", False)):
            for elemento in atributos.get(grupo) or []:
                if not isinstance(elemento, dict) or set(elemento) - _CLAVES_ATRIBUTO:
                    raise ValueError(f"Requisito de atributo inválido en attributes.{grupo}")
                requisitos.append(
                    Requisito(campo="atributo", obligatorio=obligatorio, **elemento)
                )
    categoria = intent.get("google_product_category")
    if categoria is not None and not (
        isinstance(categoria, (str, int))
        and not isinstance(categoria, bool)
        and str(categoria).strip().isdigit()
    ):
        raise ValueError("google_product_category tiene que ser un ID de la taxonomía de Google")
    return ficha.model_copy(
        update={
            "requisitos": requisitos,
            "categoria_taxonomia": str(categoria).strip() if categoria is not None else None,
        }
    )
