// Los controladores de las páginas (DOM, sin tests propios): que se puedan
// cargar como módulos y que cada elemento que buscan exista en su HTML.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const STATIC = fileURLToPath(new URL("../../static/", import.meta.url));
const leer = (nombre) => readFileSync(join(STATIC, nombre), "utf-8");

test("los controladores son módulos sin errores de sintaxis", () => {
  const carpeta = mkdtempSync(join(tmpdir(), "kaiai-pagina-"));
  try {
    for (const nombre of ["demo.js", "demo-clasico.js", "app.js"]) {
      const copia = join(carpeta, nombre.replace(/\.js$/, ".mjs"));
      writeFileSync(copia, leer(nombre));
      execFileSync(process.execPath, ["--check", copia], { stdio: "pipe" });
    }
  } finally {
    rmSync(carpeta, { recursive: true, force: true });
  }
});

test("cada #id que usa demo.js está en demo.html", () => {
  const js = leer("demo.js");
  const html = leer("demo.html");
  const ids = new Set([...js.matchAll(/\$\("#([\w-]+)"\)/g)].map((m) => m[1]));
  assert.ok(ids.has("sugerencias") && ids.has("escenarios-vacio"), [...ids].join(", "));
  const faltan = [...ids].filter((id) => !html.includes(`id="${id}"`));
  assert.deepEqual(faltan, []);
});
