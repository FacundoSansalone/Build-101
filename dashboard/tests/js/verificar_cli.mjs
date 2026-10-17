// Ayudante para el test cruzado Python → navegador (dashboard/tests/test_server.py):
// lee {recibo, clave} de un archivo JSON y escribe el resultado de verificarRecibo().
import { readFileSync } from "node:fs";
import { verificarRecibo } from "../../static/lib/jose.js";

const { recibo, clave } = JSON.parse(readFileSync(process.argv[2], "utf-8"));
const resultado = await verificarRecibo(recibo, clave);
process.stdout.write(JSON.stringify({ ok: resultado.ok, exacto: resultado.exacto ?? null, pasos: resultado.pasos }));
