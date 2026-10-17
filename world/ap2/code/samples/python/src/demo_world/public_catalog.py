"""Catálogo público del comercio (la "web" del comercio).

Es una fuente independiente del agente del comercio: sirve siempre los datos
verdaderos de data/catalog.json, sin importar qué escenario esté activo.
El Guard lo consulta para verificar lo que el agente del comercio declara.

  uv run --package ap2-samples python -m demo_world.public_catalog

Endpoints:
  GET /health
  GET /products            -> {"merchant": {...}, "products": [...]}
  GET /products/<sku>      -> producto o 404
"""

import os

from flask import Flask, jsonify

from demo_world import scenario

PORT = int(os.environ.get("PUBLIC_CATALOG_PORT", "8011"))

app = Flask(__name__)


@app.get("/health")
def health():
  return jsonify({"status": "ok"})


@app.get("/products")
def list_products():
  return jsonify(scenario.load_catalog())


@app.get("/products/<sku>")
def get_product(sku: str):
  product = scenario.products_by_sku().get(sku)
  if product is None:
    return jsonify({"error": f"Producto {sku} no encontrado"}), 404
  return jsonify(product)


def main() -> None:
  app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
  main()
