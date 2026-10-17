"""Qué frena AP2 por sí solo, usando el evaluador real del SDK.

En el flujo sin usuario presente, el usuario firma un mandato de pago ABIERTO
con restricciones (por ejemplo, AmountRange con un máximo). Antes de pagar, el
SDK de AP2 verifica que el mandato CERRADO cumpla esas restricciones con
ap2.sdk.constraints.check_payment_constraints.

Este módulo arma esos dos mandatos para una oferta y devuelve las violaciones
que encuentra el propio SDK. Así, "AP2 lo frena" en la demo está demostrado
con el código de AP2 y no con una regla nuestra.
"""

from __future__ import annotations

from typing import Any

from ap2.sdk.constraints import check_payment_constraints
from ap2.sdk.generated.open_payment_mandate import (
    AllowedPayees,
    AmountRange,
    OpenPaymentMandate,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.utils import ec_key_to_jwk
from cryptography.hazmat.primitives.asymmetric import ec

from demo_world import scenario

_AGENT_KEY = ec.generate_private_key(ec.SECP256R1())


def _cents(amount: float) -> int:
  return int(round(amount * 100))


def _merchant() -> Merchant:
  m = scenario.load_catalog()["merchant"]
  return Merchant(id=m["id"], name=m["name"], website=m["website"])


def open_payment_mandate() -> OpenPaymentMandate:
  """El mandato abierto que firmaría el usuario a partir del intent."""
  hard = scenario.intent()["hard"]
  return OpenPaymentMandate(
      constraints=[
          AmountRange(max=_cents(hard["max_price"]), currency=hard["currency"]),
          AllowedPayees(allowed=[_merchant()]),
      ],
      cnf={
          "jwk": ec_key_to_jwk(_AGENT_KEY.public_key()).model_dump(
              exclude_none=True
          )
      },
  )


def closed_payment_mandate(offer: dict[str, Any]) -> PaymentMandate:
  """El mandato cerrado que armaría el agente para pagar esta oferta."""
  return PaymentMandate(
      transaction_id=f"tx_{offer['cart_id']}",
      payee=_merchant(),
      payment_amount=Amount(
          amount=_cents(offer["amount"]), currency=offer["currency"]
      ),
      payment_instrument=PaymentInstrument(id="pi-demo", type="credit"),
  )


def ap2_violations(offer: dict[str, Any]) -> list[str]:
  """Violaciones que detecta el SDK de AP2 para esta oferta (vacío = pasa)."""
  return check_payment_constraints(
      open_payment_mandate(), closed_payment_mandate(offer)
  )
