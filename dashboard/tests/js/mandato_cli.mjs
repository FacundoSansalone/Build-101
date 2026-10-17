// Ayudante para test_server.py: decodifica un SD-JWT (mandato de AP2) con el
// mismo código que usa el navegador y escribe los claims divulgados.
import { readFileSync } from "node:fs";
import { decodificarSdJwt, resolverSdJwt } from "../../static/lib/jose.js";

const token = readFileSync(process.argv[2], "utf-8").trim();
const cadena = decodificarSdJwt(token);
const ultimo = cadena[cadena.length - 1];
const { resuelto, sinUsar } = await resolverSdJwt(ultimo.emisor.payload, ultimo.divulgaciones);
process.stdout.write(JSON.stringify({ header: ultimo.emisor.header, claims: resuelto, sinUsar }));
