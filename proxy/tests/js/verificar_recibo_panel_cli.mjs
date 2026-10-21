// Para proxy/tests/test_panel.py: verifica como el panel del cliente un recibo
// firmado por el motor. La explicación da el id del recibo, la compra trae el
// JWS (reciboParaVerificar) y recibo.mjs (el archivo que sirve /panel/recibo.mjs)
// lo verifica contra la clave pública.
//   node verificar_recibo_panel_cli.mjs <entrada.json>
// entrada: {"explicacion": <GET …/explicacion>, "compra": <GET /v1/compras/{id}>,
//           "publica": <GET /v1/public-key>}
// salida (stdout): {"ok": bool, "motivo": str, "id": <id del recibo a verificar>}
import { readFileSync } from "node:fs";

import { reciboParaVerificar, textoVerificacion } from "../../panel/logica.mjs";
import { verificarRecibo } from "../../consola/recibo.mjs";

const entrada = JSON.parse(readFileSync(process.argv[2], "utf-8"));
const recibo = reciboParaVerificar(entrada.compra, entrada.explicacion.recibo);
const resultado = recibo
  ? await verificarRecibo(recibo, entrada.publica, globalThis.crypto.subtle)
  : { ok: false, motivo: "sin recibo" };
process.stdout.write(JSON.stringify({
  ok: resultado.ok,
  motivo: textoVerificacion(resultado).texto,
  id: recibo?.id ?? null,
}));
