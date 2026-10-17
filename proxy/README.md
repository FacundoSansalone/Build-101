# proxy/ (Dev 3)

Proxy A2A en el puerto 8010, entre el Shopping Agent y el Merchant Agent (8001). Llama al Guard y nunca modifica objetos firmados.

El Shopping Agent se apunta al proxy con `MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent`.
