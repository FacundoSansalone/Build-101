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

"""Tools used by the shopper subagent.

Finds products (as Cart objects) and lets the user select one.
"""

from ap2.models.cart import CART_DATA_KEY, Cart
from common.a2a_message_builder import A2aMessageBuilder
from common.artifact_utils import find_canonical_objects
from google.adk.tools.tool_context import ToolContext
from roles.shopping_agent.remote_agents import merchant_agent_client

# HACKATHON (Pre-Sign Guard): el contexto del comercio de la compra en curso.
# Lo abre la primera búsqueda y lo cierra el fin de la compra: el recibo del
# pago o un corte del Guard (búsqueda o checkout bloqueados).
CONTEXTO_COMPRA_KEY = "guard_contexto_compra"


async def find_products(
    catalog_search: str,
    tool_context: ToolContext,
    debug_mode: bool = False,
) -> list[Cart]:
  """Calls the merchant agent to find products matching the user's search.

  Args:
    catalog_search: The user's catalog search query.
    tool_context: The context object for the tool.
    debug_mode: Whether to run in debug mode.

  Returns:
    A list of Cart objects (cart_id, item_label, amount, currency). If the
    user authorized new purchase parameters in the Pre-Sign Guard, a dict
    with the carts and those parameters (HACKATHON, see world/NOTICE.md).

  Raises:
    RuntimeError: If the merchant agent fails to find products.
  """
  risk_data = _collect_risk_data(tool_context)
  # HACKATHON (Pre-Sign Guard): otra búsqueda de la misma compra sigue en el
  # contexto del comercio de la anterior. El proxy del Guard agrupa la compra
  # por contexto: con uno nuevo por búsqueda, le volvía a preguntar el porqué
  # al usuario cada vez que el agente buscaba de nuevo.
  builder = A2aMessageBuilder()
  if tool_context.state.get(CONTEXTO_COMPRA_KEY):
    builder.set_context_id(tool_context.state[CONTEXTO_COMPRA_KEY])
  message = (
      builder
      .add_text("Find products that match the user's request.")
      .add_data("catalog_search", catalog_search)
      .add_data("risk_data", risk_data)
      .add_data("debug_mode", debug_mode)
      .add_data("shopping_agent_id", "trusted_shopping_agent")
      .build()
  )
  task = await merchant_agent_client.send_a2a_message(message)

  if task.status.state != "completed":
    # HACKATHON (Pre-Sign Guard): si el proxy del Guard cortó la conversación
    # (filtro de seguridad o consulta vencida), se devuelve su motivo en vez
    # de tirar error. Con el error, RetryingLlmAgent reintentaba la búsqueda
    # hasta 5 veces y cada reintento era una compra nueva para el Guard.
    veredicto = _veredicto_guard(task.artifacts)
    if veredicto is not None and veredicto.get("status") == "bloqueado":
      # La compra terminó: la próxima búsqueda es otra (en este contexto el
      # proxy cortaría todo lo que siga).
      tool_context.state[CONTEXTO_COMPRA_KEY] = None
      return (
          "Pre-Sign Guard blocked this conversation with the merchant before"
          f" any of its content reached you: {veredicto.get('motivo', '')}"
          " No products from this merchant can be offered. Do NOT call"
          " find_products again for this request. Tell the user the purchase"
          " was stopped for safety."
      )
    tool_context.state[CONTEXTO_COMPRA_KEY] = task.context_id  # el reintento sigue
    raise RuntimeError(f"Failed to find products: {task.status}")

  tool_context.state["shopping_context_id"] = task.context_id
  tool_context.state[CONTEXTO_COMPRA_KEY] = task.context_id  # HACKATHON
  carts = find_canonical_objects(task.artifacts, CART_DATA_KEY, Cart)
  tool_context.state["available_carts"] = [
      c.model_dump(mode="json") for c in carts
  ]
  # HACKATHON (Pre-Sign Guard, etapa 2): si el usuario aceptó una alternativa
  # en el dashboard del Guard, el proxy agrega los parámetros nuevos en un
  # artefacto aparte. Se le pasan al agente junto con los carts. Sin ese
  # artefacto la herramienta devuelve lo mismo que el sample.
  parametros = _parametros_autorizados(task.artifacts)
  if parametros is not None:
    tool_context.state["guard_parametros_autorizados"] = parametros
    return {
        "carts": carts,
        "parameters_authorized_by_the_user": parametros.get("texto"),
        "authorized_cart_id": parametros.get("cart_id"),
    }
  return carts


def _veredicto_guard(artifacts) -> dict | None:
  """El data part `guard.veredicto` del proxy del Guard, si vino."""
  for artifact in artifacts or []:
    for part in artifact.parts:
      data = getattr(part.root, "data", None)
      if isinstance(data, dict) and isinstance(data.get("guard.veredicto"), dict):
        return data["guard.veredicto"]
  return None


def _parametros_autorizados(artifacts) -> dict | None:
  """El data part `guard.parametros_autorizados` del proxy del Guard, si vino."""
  for artifact in artifacts or []:
    for part in artifact.parts:
      data = getattr(part.root, "data", None)
      if isinstance(data, dict) and isinstance(
          data.get("guard.parametros_autorizados"), dict
      ):
        return data["guard.parametros_autorizados"]
  return None


def select_cart(cart_id: str, tool_context: ToolContext) -> str:
  """Selects a cart by ID from the available carts."""
  available_carts = tool_context.state.get("available_carts", [])
  for cart in available_carts:
    if cart.get("cart_id") == cart_id:
      tool_context.state["chosen_cart_id"] = cart_id
      return f"Cart {cart_id} selected."
  return f"Cart {cart_id} not found."


def _collect_risk_data(tool_context: ToolContext) -> str:
  risk_data = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...fake_risk_data"
  tool_context.state["risk_data"] = risk_data
  return risk_data
