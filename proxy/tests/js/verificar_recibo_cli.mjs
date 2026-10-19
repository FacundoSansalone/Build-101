// Para proxy/tests/test_consola.py: verifica con la lógica de la consola un
// recibo firmado en Python (guard.recibo.firmar_recibo).
//   node verificar_recibo_cli.mjs <entrada.json>
// entrada: {"recibo": {"id", "jws", "kid"}, "publica": <GET /v1/public-key>}
// salida (stdout): {"ok": bool, "motivo": str, "id": <payload.id>}
import { readFileSync } from "node:fs";

import { verificarRecibo } from "../../consola/logica.mjs";

const entrada = JSON.parse(readFileSync(process.argv[2], "utf-8"));
const resultado = await verificarRecibo(entrada.recibo, entrada.publica, globalThis.crypto.subtle);
process.stdout.write(JSON.stringify({
  ok: resultado.ok,
  motivo: resultado.motivo,
  id: resultado.payload?.id ?? null,
}));
