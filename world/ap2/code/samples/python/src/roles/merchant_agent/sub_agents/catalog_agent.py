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

"""A sub-agent that offers items from its 'catalog'.

Returns Cart objects for browsing. The checkout JWT is created later
when the user selects a cart (via the ``create_checkout`` tool).

HACKATHON (Pre-Sign Guard): el sample original generaba 3 productos con un LLM
en cada búsqueda. Lo reemplazamos por un catálogo fijo por escenario
(demo_world.scenario) para que el benchmark sea reproducible y para que el
comercio pueda comportarse como atacante (inyección, precios inflados, datos
falsos). El resto del flujo AP2 (checkout firmado, mandatos, pago) no cambia.
"""

import logging

from typing import Any

from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import DataPart, Part, Task, TextPart
from ap2.models.cart import CART_DATA_KEY, Cart
from common import message_utils
from demo_world import scenario as demo_scenario
from pydantic import ValidationError

from .. import storage


async def find_items_workflow(
    data_parts: list[dict[str, Any]],
    updater: TaskUpdater,
    current_task: Task | None,
) -> None:
  """Find products.

  Use this tool only when the request says 'find products' or contains
  'catalog_search'. Searches the catalog and returns Cart objects.

  Args:
    data_parts: The data parts from the message.
    updater: The task updater to report status.
    current_task: The current task object.
  """
  catalog_search = message_utils.find_data_part("catalog_search", data_parts)
  if not catalog_search:
    error_message = updater.new_agent_message(
        parts=[Part(root=TextPart(text="Missing catalog_search."))]
    )
    await updater.failed(message=error_message)
    return

  try:
    scenario_name = demo_scenario.active_scenario(data_parts)
    offers = demo_scenario.build_offers(scenario_name, catalog_search, contexto_id=updater.context_id)
    logging.info(
        "demo_world: scenario=%s search=%r offers=%s",
        scenario_name,
        catalog_search,
        [o["sku"] for o in offers],
    )

    for offer in offers:
      cart = Cart(
          cart_id=offer["cart_id"],
          item_label=offer["item_label"],
          amount=offer["amount"],
          currency=offer["currency"],
      )
      storage.set_cart_data(
          cart.cart_id, {**cart.model_dump(), "sku": offer["sku"]}
      )
      await updater.add_artifact(
          [Part(root=DataPart(data={CART_DATA_KEY: cart.model_dump()}))]
      )

    # Campos estructurados tal como los declara el comercio (pueden mentir).
    await updater.add_artifact([
        Part(
            root=DataPart(
                data={
                    demo_scenario.OFFER_DETAILS_KEY: (
                        demo_scenario.offer_details_payload(offers)
                    ),
                    demo_scenario.SCENARIO_DATA_KEY: scenario_name,
                }
            )
        )
    ])

    risk_data = _collect_risk_data(updater)
    await updater.add_artifact([
        Part(root=DataPart(data={"risk_data": risk_data})),
    ])
    await updater.complete()
  except (ValidationError, ValueError) as e:
    error_message = updater.new_agent_message(
        parts=[Part(root=TextPart(text=f"Invalid product list: {e}"))]
    )
    await updater.failed(message=error_message)
    return


def _collect_risk_data(updater: TaskUpdater) -> str:
  risk_data = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...fake_risk_data"
  storage.set_risk_data(updater.context_id, risk_data)
  return risk_data
