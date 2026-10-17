"""La Agent Card que sirve el proxy apunta al proxy: el cliente no lo saltea."""

from __future__ import annotations

import httpx
import pytest
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.client_task_manager import ClientTaskManager
from a2a.types import AgentCard, DataPart, Message, Part, Role, TextPart

from proxy.tests.fake_world import (
    MERCHANT_HOST,
    PROXY_PUBLIC_URL,
    FakeMerchant,
    RuteoPorHost,
    card_del_comercio,
)

pytestmark = pytest.mark.anyio

CARD_PATH = "/a2a/merchant_agent/.well-known/agent-card.json"


async def test_card_reescribe_url_e_interfaces(cliente: httpx.AsyncClient):
    resp = await cliente.get(CARD_PATH)

    assert resp.status_code == 200
    card = resp.json()
    assert card["url"] == PROXY_PUBLIC_URL
    assert [i["url"] for i in card["additionalInterfaces"]] == [PROXY_PUBLIC_URL]
    # El resto de la card queda igual que la del comercio.
    original = card_del_comercio()
    for clave in ("name", "capabilities", "skills", "protocolVersion"):
        assert card[clave] == original[clave]
    AgentCard.model_validate(card)


async def test_card_comercio_caido_da_502(armar_proxy):
    app = armar_proxy(transport=RuteoPorHost({}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010"
    ) as c:
        resp = await c.get(CARD_PATH)
    assert resp.status_code == 502


async def test_cliente_a2a_del_sdk_habla_con_el_proxy(
    proxy_app, merchant: FakeMerchant
):
    """Mismo camino que PaymentRemoteA2aClient: resolver la card y mandar.

    El transporte del cliente solo conoce al proxy (localhost:8010): si la
    card siguiera apuntando al comercio, la llamada fallaría.
    """
    transporte = RuteoPorHost({"localhost:8010": proxy_app})
    async with httpx.AsyncClient(transport=transporte, timeout=10) as http:
        card = await A2ACardResolver(
            httpx_client=http, base_url=PROXY_PUBLIC_URL
        ).get_agent_card()
        assert card.url == PROXY_PUBLIC_URL
        http.headers["X-A2A-Extensions"] = (
            "https://github.com/google-agentic-commerce/ap2/v1"
        )
        client = ClientFactory(ClientConfig(httpx_client=http)).create(card)
        message = Message(
            message_id="m-1",
            role=Role.agent,
            parts=[
                Part(root=TextPart(text="Find products.")),
                Part(root=DataPart(data={"catalog_search": "nike"})),
                Part(root=DataPart(data={"demo_scenario": "B"})),
            ],
        )
        manager = ClientTaskManager()
        async for event in client.send_message(message):
            if isinstance(event, tuple):
                event = event[0]
            await manager.process(event)
        task = manager.get_task()

    assert task is not None and task.status.state == "completed"
    assert len(merchant.recibidos) == 1  # llegó al comercio a través del proxy
