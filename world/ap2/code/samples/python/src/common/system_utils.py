# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper functions related to the system."""

import os

from google.adk.models.google_llm import Gemini
from google.adk.models.registry import LLMRegistry

# HACKATHON (Pre-Sign Guard): el modelo de los agentes se puede cambiar con
# AP2_MODEL (por defecto, el del sample). El preview satura (503) y comparte
# un solo cupo por minuto entre todos los agentes.
AP2_MODEL = os.environ.get("AP2_MODEL") or "gemini-3.1-flash-lite-preview"

# HACKATHON (Pre-Sign Guard): el agente de compras (root y sus 3 sub-agentes)
# usa un modelo más chico que el del sample, SHOPPING_AGENT_MODEL. Los
# servidores (comercio, credenciales, pagos) siguen con AP2_MODEL para elegir
# su herramienta.
SHOPPING_AGENT_MODEL = (
    os.environ.get("SHOPPING_AGENT_MODEL") or "gemma-4-26b-a4b-it"
)


class _Gemma4(Gemini):
  """Gemma 4 por la API de Gemini. Tiene function calling nativo, pero el
  registro de ADK solo conoce gemma-3 (que arma las llamadas en texto)."""

  @classmethod
  def supported_models(cls) -> list[str]:
    return [r"gemma-4.*"]


LLMRegistry.register(_Gemma4)

DEBUG_MODE_INSTRUCTIONS = """
    This is really important! If the agent or user asks you to be verbose or if debug_mode is True, do the following:
      1. If this is the the start of a new task, explain who you are, what you are going to do, what tools you use, and what agents you delegate to.
      2. During the task, provide regular status updates on what you are doing, what you have done so far, and what you plan to do next.
      3. If you are delegating to another agent, ask the agent or tool to also be verbose.
      4. If at any point in the task you send or receive data, show the data in a clear, formatted way. Do not summarize it in english. Simple format the JSON objects.
      5. Step 4 is so important that I'm going to repeat it:
        a. If at any point in the task you create, send or receive data, show the data in a clear, formatted way. Do not summarize it in english. Simple format the JSON objects.
"""
