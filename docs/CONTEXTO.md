# Contexto: por qué existe Pre-Sign Guard

> Para quien llega sin saber nada del proyecto. Cómo está construido:
> [ARQUITECTURA.md](ARQUITECTURA.md). Cómo trabajamos: [PLAN.md](PLAN.md).

## 1. La idea y el problema

### De dónde venimos

La visión inicial era más amplia. En un futuro agéntico, los agentes de
distintas organizaciones van a negociar y comprar entre sí: el **agente A**
compra en nombre de un cliente y el **agente B** vende en nombre de otra
organización. Queríamos que esa conversación fuera fiable, segura y sin
alucinaciones.

Un "firewall genérico entre agentes" ya existe en desarrollo (ver la
[sección 2](#2-lo-que-ya-existe)), así que acotamos: nos enfocamos en
**pagos hechos por agentes con AP2**.

### El nicho, en una definición

**Pre-Sign Guard** (nombre de trabajo) protege **la integridad de las decisiones
de compra de agentes que pagan con AP2**. Garantiza que lo que el usuario firma
refleje lo que quería y se base en datos verificados, en el tramo que va **desde
el pedido del usuario hasta la firma**.

| Pregunta | Respuesta |
|---|---|
| ¿Qué amenaza cubre? | Todo lo que hace que el agente decida mal antes de la firma: inyección de prompt, información falsa del comercio o error propio del agente. |
| ¿Dónde actúa? | Del lado del comprador, antes de la firma, dentro de los límites del mandato. |
| ¿Cómo? | Datos estructurados, verificación independiente y un recibo de la decisión. |

### Lo que NO es

**No es "un detector de prompt injection".** Esa categoría está saturada y,
además, contradice el diseño: el Guard no detecta inyecciones, las vuelve
irrelevantes porque el agente que decide nunca ve el texto libre del vendedor.
La inyección es el mejor **ejemplo** y la demo, no la definición del producto.

### Cliente objetivo

Agentes de compras **B2B (procurement)**:

- montos más altos;
- áreas de compliance y auditoría que valoran un control independiente;
- compras repetidas a muchos proveedores;
- suelen usar agentes de terceros (SAP, Salesforce) que no pueden reentrenar.

### Frases para el pitch

- "AP2 frena al agente cuando se pasa de los límites. Nosotros lo frenamos
  cuando lo manipulan sin pasarse."
- "AP2 firma lo que el agente decide. Nosotros garantizamos que esa decisión no
  fue manipulada."
- "No detectamos ataques. Garantizamos que tu agente de compras decide con
  datos verificados, aunque lo ataquen."
- "No sabemos si B miente. Sabemos cuándo lo que dice no coincide con lo que
  publica."

## 2. Lo que ya existe

### Los protocolos

**A2A (Agent2Agent).** Protocolo abierto impulsado por Google, hoy bajo la
Linux Foundation, para que agentes de distintas organizaciones se comuniquen.
Resuelve transporte y formato (Agent Cards, tareas, mensajes, artefactos) y
autenticación. **No** resuelve la confianza en el contenido de los mensajes.

**AP2 (Agent Payments Protocol).** Extensión de A2A para pagos hechos por
agentes. Google lo anunció en septiembre de 2025 y lo donó a la FIDO Alliance
en abril de 2026. Licencia Apache 2.0. Se basa en **mandatos** firmados
criptográficamente: el usuario firma qué autoriza y el comercio firma su
oferta. Prueba **qué se autorizó**, no si la decisión fue correcta.

La documentación oficial de seguridad de AP2 dice que **asume que prevenir la
inyección de prompt es inviable**: trata a todo LLM como potencial atacante y
limita el daño verificando las restricciones del mandato.

**Hallazgo en el código del sample (v0.2).** Las restricciones de AP2
(`AmountRange`, `AllowedPayees`, `Budget`, etc., en `ap2.sdk.constraints`) se
aplican a mandatos **abiertos**, es decir, en el flujo **sin usuario
presente**. En el flujo con usuario presente, el usuario firma directamente.
Para la demo, el chequeo de tope se hace con el **evaluador real del SDK**, en
[`demo_world/ap2_check.py`](../world/ap2/code/samples/python/src/demo_world/ap2_check.py).

### Papers y trabajos relevantes

**Whispers of Wealth** (arXiv 2601.22569, U. de Georgia y Kennesaw State).
Agente de compras sobre AP2 con Gemini 2.5 Flash y ADK.
- El ataque *Branded Whisper* (instrucción escondida en la descripción del
  producto) manipuló el ranking el **100%** de las veces; en 6 de 10 corridas
  el producto elegido contradecía restricciones del usuario.
- El ataque *Vault Whisper* expuso datos de otra cuenta en el **20%**.
- Ninguna firma se rompió. Conclusión: la criptografía asegura la ejecución, no
  la decisión.
- Recomiendan filtrar el contenido externo y validar las acciones sensibles
  **fuera** del LLM.

**Beyond the Mandate** (arXiv 2608.23858, Ben-Gurion e Intuit, agosto 2026).
Análisis sistemático de AP2 v0.2: 48 amenazas, 8 de riesgo alto. Construyen un
**escáner** que revisa la configuración de un despliegue. Misma conclusión: una
firma válida no garantiza que la transacción refleje la intención si el
contexto previo fue manipulado.

**PCAT / AIP-Bench** (arXiv 2607.21824, Ariel University, julio 2026). Sidecar
HTTP open source y determinístico que frena ataques **estructurales** (firmas,
identidad, pagos atómicos) en AP2 y otras plataformas. **Deja explícitamente
fuera de alcance los ataques semánticos** (inyección por descripciones
envenenadas): es exactamente nuestro hueco. Su estudio de modelos: los baratos
(GPT-4o-mini, Gemini Flash, Mistral) obedecieron instrucciones de pago
inyectadas el 99–100% de las veces, GPT-4o el 68%, y Claude Sonnet y Gemini
Pro el 0%.

**Mastercard Verifiable Intent** (marzo 2026, con Google, open source). Registro
a prueba de manipulación de lo que el usuario autorizó y de la interacción que
llevó a la compra. Se parece a nuestro recibo, pero no aborda la inyección ni
verifica que la compra cumpla las preferencias.

**Firewalls A2A genéricos** (por ejemplo *A2A Firewall* en GitHub, gateways
como el de TrueFoundry o agentgateway, el framework A2AS). Gobernanza general,
sin chequeos de precios ni de intención contra carrito.

**KaliCart.** Etiqueta el origen de cada dato del catálogo (declarado o
extraído). No filtra y trabaja del lado del índice, no del comprador. La idea
de etiquetar el origen la reutilizamos en el chequeo de evidencia.

### Quién cubre qué

| Capa | Quién |
|---|---|
| Autorización y firmas | AP2, Mastercard Verifiable Intent |
| Ataques estructurales en tiempo real | PCAT |
| Revisión de configuración | Escáner de *Beyond the Mandate* |
| Firewall genérico entre agentes | A2A Firewall, gateways, A2AS |
| **Manipulación semántica del comprador, dentro del mandato, en tiempo real** | **Nadie, en lo que encontramos → nosotros** |

> El campo se mueve rápido y la búsqueda no fue exhaustiva. En la presentación
> lo decimos así, tal cual.

## 3. Posicionamiento

- **Construimos sobre AP2, sin modificarlo.** El Guard usa AP2 en todo el flujo,
  pero no cambia el protocolo. `world/ap2/` es una copia del sample oficial con
  cambios mínimos, listados en [`world/NOTICE.md`](../world/NOTICE.md).
- **Adopción unilateral.** El Guard es un proxy del lado de A. B no se entera
  ni tiene que cooperar. "Protección desde el día uno, sin pedirle permiso a
  nadie."
- **Complementario a PCAT.** PCAT cubre la capa estructural; nosotros, la
  semántica.
- **Alineado con la filosofía de AP2.** AP2 asume que el LLM puede ser
  manipulado; por eso las decisiones críticas del Guard son código
  determinístico.

## Referencias

- *Whispers of Wealth: A Systematic Red-Teaming Study of AP2*. arXiv 2601.22569.
- *Beyond the Mandate: A Systematic Security Analysis of AP2*. arXiv 2608.23858.
- *Protocol-Level Attacks on Agentic Commerce Platforms (PCAT, AIP-Bench)*.
  arXiv 2607.21824.
- AP2, *Security and Privacy Considerations*. ap2-protocol.org.
- AP2, repo oficial: <https://github.com/google-agentic-commerce/AP2> (commit
  copiado en `world/ap2/UPSTREAM_COMMIT`).
- Mastercard Verifiable Intent (marzo 2026).
- A2A Firewall, agentgateway, gateway de TrueFoundry, A2AS.
- KaliCart.
