"""Capa de intención (etapa 2, docs/FLUJO.md): el porqué del pedido y la
detección de oportunidades. Usa un LLM para preguntar y proponer; nunca
autoriza nada: eso lo hace el usuario, y el Guard verifica antes de la firma.
"""

from intencion.agente import (
    MODELO_POR_DEFECTO,
    PREGUNTA_POR_DEFECTO,
    Agente,
    AgenteGemini,
    Pregunta,
    RespuestaInvalida,
    armar_contexto,
    candidatas,
    detectar_oportunidad,
    no_cumplidos,
    preguntar,
)

__all__ = [
    "MODELO_POR_DEFECTO",
    "PREGUNTA_POR_DEFECTO",
    "Agente",
    "AgenteGemini",
    "Pregunta",
    "RespuestaInvalida",
    "armar_contexto",
    "candidatas",
    "detectar_oportunidad",
    "no_cumplidos",
    "preguntar",
]
