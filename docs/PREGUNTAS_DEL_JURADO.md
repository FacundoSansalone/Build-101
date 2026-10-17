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

No lo sabemos. Detectamos cuando lo que dice no coincide con lo que publica
(su propio catálogo público), y declaramos lo que no se puede verificar.

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

No. No buscamos inyecciones: el agente que decide nunca ve el texto libre del
vendedor, así que la inyección no tiene por dónde entrar. En el escenario C no
hay inyección y el Guard igual detecta el sobreprecio.
