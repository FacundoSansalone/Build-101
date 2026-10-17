# contracts/ (los tres)

Modelos de datos compartidos (Pydantic v2) en [`models.py`](models.py):
`Ficha`, `Requisito`, `Oferta`, `Evidencia`, `OfertasVistas`, `Politica`,
`Chequeo`, `Veredicto`, `Recibo`, `Evento` y el protocolo `Evaluador` (la firma
de `evaluar()`).

Acordados en el paso 0. Las decisiones están en
[`CLAUDE.md`](../CLAUDE.md#decisiones). **No se cambian sin acordarlo entre los
tres.**

```python
from contracts import Ficha, OfertasVistas, Veredicto
```

Tests: `PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest contracts` desde la
raíz del repo.
