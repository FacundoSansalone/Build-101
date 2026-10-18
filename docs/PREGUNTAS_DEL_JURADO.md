# Preguntas del jurado

> Objeciones esperables y cómo las respondemos. Contexto en
> [CONTEXTO.md](CONTEXTO.md); números en [DEMO.md](DEMO.md).

### "¿Por qué no entrenar mejor al agente?"

- Un modelo es probabilístico: 99% de resistencia a escala sigue siendo muchas
  compras manipuladas.
- AP2 mismo asume que la inyección es inviable de prevenir. La certeza sale de
  la arquitectura, no del entrenamiento.
- Muchas empresas compran agentes de terceros que no pueden reentrenar, y los
  modelos cambian cada pocos meses.
- El que decide no puede ser el que se controla (separación de funciones).
- Vemos ataques de todos los clientes, no de uno.
- Permite usar modelos baratos de forma segura.

### "¿Por qué no usar un modelo mejor?"

Cuesta mucho más por transacción, un 0% en un test no es garantía y el Guard
funciona con cualquier modelo. (Dato de PCAT / AIP-Bench: los modelos baratos
obedecieron instrucciones de pago inyectadas el 99–100% de las veces.)

### "¿Google no lo va a resolver?"

AP2 declara el problema fuera del alcance del protocolo. Una capa neutral
funciona con agentes de cualquier proveedor. Modelo open core: el Guard básico
abierto; la inteligencia compartida, la reputación y los recibos de auditoría,
pagos.

### "¿PCAT no hace esto?"

No. PCAT deja explícitamente fuera los ataques semánticos. Somos
complementarios: PCAT cubre la capa estructural, nosotros la semántica.

### "¿Cómo sabés si B miente?"

No lo sabemos. Detectamos cuando lo que dice no coincide con una fuente que
no controla: la ficha del fabricante por GTIN y, si lo publica, su propio
catálogo público. Lo que no se puede verificar se declara y, si es un
requisito obligatorio, no se aprueba solo: se escala al humano.

### "¿B se entera?"

No. El Guard está del lado de A. Adopción unilateral: no hace falta que el
comercio coopere.

### "¿Modifican AP2?"

No. Lo usamos tal cual; los únicos cambios son al sample de demo y están
documentados en [`world/NOTICE.md`](../world/NOTICE.md).

### "¿Qué pasa si el Guard se equivoca?"

Lo dudoso nunca se aprueba en silencio: se escala al humano con la evidencia.
Solo se bloquea lo que viola reglas explícitas (un obligatorio de la ficha, el
mandato o la política).

### "¿No es otro detector de prompt injection?"

No es solo eso. Tenemos un filtro que detecta (heurísticas y juicios
tipados con probabilidad) y corta la conversación antes de que el texto
hostil llegue a cualquier agente: es defensa en profundidad. Pero **la
garantía no depende de detectar**: el Guard verifica la compra antes de la
firma contra lo que autorizó el usuario y contra fuentes que el vendedor no
controla, sin leer su texto libre. En `notebook_dato_falso` no hay inyección
(solo un dato falso), el filtro no tiene nada que ver y el Guard igual
bloquea; en C, igual escala el sobreprecio.

### "¿Y si el filtro se equivoca?"

- **Si deja pasar un ataque**, el Guard igual verifica antes de la firma: la
  seguridad no depende del filtro.
- **Si corta a un proveedor honesto**, se pierde esa negociación, pero no se
  compra nada que no se autorizó. Lo medimos: 0 de 12 honestos cortados y
  14 de 14 ataques cortados, en 26 casos de una corrida que escribimos
  nosotros (muestra chica y optimista). En la prueba real apareció un falso
  positivo (el eco de nuestro propio pedido) y lo corregimos.
- **Si el filtro falla o no responde**, bloquea: nunca aprueba en silencio.

### "¿Qué hace el modelo y qué el código?"

Los modelos entienden y proponen: el filtro juzga si un texto intenta
manipular, el intake entiende el pedido (con probabilidades: si duda,
repregunta) y el detector propone alternativas. El usuario autoriza: confirma
la ficha y acepta o rechaza cada cambio. El código verifica: el Guard es
determinístico, no usa LLM y nunca lee el texto del vendedor.
