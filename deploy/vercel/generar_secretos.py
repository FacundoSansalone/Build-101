"""Genera los secretos del motor en Vercel, cada uno en su archivo. Nunca los imprime.

- **Clave del recibo** (P-256, PKCS8): `guard_signing_key.pem` y, para la
  variable de Vercel, `GUARD_SIGNING_KEY_PEM_B64.txt` (el PEM en base64, una
  línea) y `GUARD_SIGNING_KID.txt` (`guard-key-1` o el de `--kid`). La clave
  pública va a `guard_public_jwk.json` (no es secreta; es la de
  `/v1/public-key`). En Vercel la clave es una sola para todas las
  instancias: no se crea en disco al arrancar.
- **Clave de integrador** `psg_<kid>_<secreto>`: `clave_integrador_<id>.txt`
  (se le entrega al integrador; el smoke la lee con `KAIAI_CLAVE`) y
  `GATEWAY_CLAVES_JSON.json`, el registro para la variable
  `GATEWAY_CLAVES_JSON` (solo el sha256, como `proxy.claves nueva`).
- Con `--webhook-url`: el secreto `whsec_…` en `WEBHOOK_SECRETO_<ID>.txt` y
  el webhook en el registro (`secreto_env`).
- Con `--dominio <x>.ngrok-free.app`: `GATEWAY_COMERCIOS_JSON.json` con el
  comercio de la simulación detrás del túnel y un prefijo secreto de ruta en
  `TUNEL_PREFIJO.txt` (para `tunel_router.py --prefijo`; `--sin-prefijo` lo
  saca).
- `vercel.env`: todo lo de arriba que va al servidor, en formato `.env`, para
  `local_multi.py --env`. La clave del integrador **no** va (es del cliente).
- `--clave-lectura <id>` (va solo): una clave **de solo lectura** para un
  integrador que ya está en `GATEWAY_CLAVES_JSON.json` (la del panel web: GET
  en `/v1`, no sirve para comprar). Escribe la clave en
  `clave_lectura_<id>.txt` y agrega su entrada (`"permisos": ["lectura"]`)
  al registro y a `vercel.env`; lo demás del registro queda igual. Después
  hay que actualizar `GATEWAY_CLAVES_JSON` en Vercel y redesplegar.

Por defecto escribe en `.temp-db/vercel/` (ignorado por git y por
`.vercelignore`), con permisos 600, y no pisa nada salvo `--forzar`: perder la
clave del recibo invalida la clave pública que ya tienen los integradores.

    world/ap2/.venv/Scripts/python deploy/vercel/generar_secretos.py \\
        --integrador acme --dominio <tu-dominio>.ngrok-free.app
    world/ap2/.venv/Scripts/python deploy/vercel/generar_secretos.py --clave-lectura acme

Para cargar cada una en Vercel (lee el valor del archivo, sin pasar por la
historia del shell): `npx vercel env add GATEWAY_CLAVES_JSON production <
.temp-db/vercel/GATEWAY_CLAVES_JSON.json`. Ver deploy/vercel/README.md.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[2]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

CARPETA_POR_DEFECTO = RAIZ / ".temp-db" / "vercel"
KID_POR_DEFECTO = "guard-key-1"
_KID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_DOMINIO_RE = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class YaExiste(Exception):
    pass


@dataclass
class Escritor:
    carpeta: Path
    forzar: bool = False
    escritos: list[Path] = field(default_factory=list)

    def escribir(self, nombre: str, contenido: str | bytes) -> Path:
        """Crea el archivo con permisos 600 (sin `\\n` final: Vercel guarda el
        valor tal cual). Sin `--forzar`, falla si ya existe."""
        ruta = self.carpeta / nombre
        datos = contenido.encode("utf-8") if isinstance(contenido, str) else contenido
        banderas = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if self.forzar else os.O_EXCL)
        banderas |= getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(ruta, banderas, 0o600)
        except FileExistsError as e:
            raise YaExiste(str(ruta)) from e
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(datos)
        try:
            os.chmod(ruta, 0o600)
        except OSError:
            pass
        self.escritos.append(ruta)
        return ruta

    def existentes(self, nombres: list[str]) -> list[Path]:
        return [self.carpeta / n for n in nombres if (self.carpeta / n).exists()]

    def reemplazar(self, nombre: str, contenido: str) -> Path:
        """Pisa un archivo que ya existe a propósito (el registro, al sumarle
        una clave): escribe uno temporal con permisos 600 y lo cambia de un
        golpe, así nunca queda a medio escribir."""
        ruta = self.carpeta / nombre
        temporal = self.carpeta / f".{nombre}.{secrets.token_hex(4)}.tmp"
        banderas = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporal, banderas, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as archivo:
                archivo.write(contenido.encode("utf-8"))
            os.replace(temporal, ruta)
        except BaseException:
            temporal.unlink(missing_ok=True)
            raise
        try:
            os.chmod(ruta, 0o600)
        except OSError:
            pass
        self.escritos.append(ruta)
        return ruta


def clave_del_recibo(kid: str) -> tuple[bytes, dict[str, Any]]:
    """(PEM PKCS8 de la clave privada, JWK público)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    from guard.recibo import clave_publica_jwk

    clave = ec.generate_private_key(ec.SECP256R1())
    pem = clave.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    return pem, clave_publica_jwk(clave, kid)


def registro_integradores(
    integrador: str, nombre: str | None, comercios: list[str], webhook_url: str | None
) -> tuple[dict[str, Any], str, str | None, str | None]:
    """(registro para GATEWAY_CLAVES_JSON, clave psg_, nombre de la variable
    del secreto del webhook, secreto del webhook)."""
    from proxy.claves import PREFIJO_SECRETO_WEBHOOK, generar_clave

    clave, kid, sha = generar_clave()
    item: dict[str, Any] = {
        "id": integrador,
        "nombre": nombre or integrador,
        "claves": [{"kid": kid, "sha256": sha, "creada": date.today().isoformat(), "activa": True}],
        "comercios": comercios,
    }
    secreto_env = secreto = None
    if webhook_url:
        secreto_env = nombre_secreto_webhook(integrador)
        secreto = PREFIJO_SECRETO_WEBHOOK + secrets.token_urlsafe(32)
        item["webhook"] = {"url": webhook_url, "secreto_env": secreto_env}
    return {"integradores": [item]}, clave, secreto_env, secreto


class RegistroInvalido(ValueError):
    """El GATEWAY_CLAVES_JSON.json que hay no sirve (el mensaje no trae hashes)."""


def agregar_clave_lectura(registro: Any, integrador: str) -> tuple[dict[str, Any], str]:
    """(registro con una clave de solo lectura más para `integrador`, clave
    psg_). No toca nada más del registro. `RegistroInvalido` si el registro
    no sirve o el integrador no está."""
    import copy

    from proxy.claves import PERMISO_LECTURA, generar_clave

    validar_registro(registro)
    nuevo = copy.deepcopy(registro)
    item = next((i for i in nuevo["integradores"] if i.get("id") == integrador), None)
    if item is None:
        raise RegistroInvalido(f"el integrador {integrador} no está en el registro")
    clave, kid, sha = generar_clave()
    item.setdefault("claves", []).append({
        "kid": kid, "sha256": sha, "creada": date.today().isoformat(), "activa": True,
        "permisos": [PERMISO_LECTURA],
    })
    validar_registro(nuevo)
    return nuevo, clave


def validar_registro(registro: Any) -> None:
    """Lo que validaría el motor al arrancar (`RegistroIntegradores`), sin los
    secretos de los webhooks: esos están en Vercel, no acá; cada `secreto_env`
    se cubre con un valor de relleno."""
    from proxy.claves import MIN_SECRETO_WEBHOOK, RegistroIntegradores
    from proxy.tipos_gateway import ConfigInvalida

    relleno: dict[str, str] = {}
    if isinstance(registro, dict) and isinstance(registro.get("integradores"), list):
        for item in registro["integradores"]:
            webhook = item.get("webhook") if isinstance(item, dict) else None
            if isinstance(webhook, dict) and isinstance(webhook.get("secreto_env"), str):
                relleno[webhook["secreto_env"]] = "x" * MIN_SECRETO_WEBHOOK
    try:
        RegistroIntegradores.desde_dict(registro, relleno)
    except ConfigInvalida as e:
        raise RegistroInvalido(str(e)) from None


def _con_linea_env(texto: str, nombre: str, valor: str) -> str:
    """`texto` (.env) con la línea `nombre=` cambiada por la nueva (o agregada)."""
    nueva = _linea_env(nombre, valor)
    lineas = texto.splitlines()
    indices = [n for n, linea in enumerate(lineas) if linea.startswith(f"{nombre}=")]
    if indices:
        lineas[indices[0]] = nueva
        lineas = [linea for n, linea in enumerate(lineas) if n not in indices[1:]]
    else:
        lineas.append(nueva)
    return "\n".join(lineas) + "\n"


def nombre_secreto_webhook(integrador: str) -> str:
    """La variable de entorno con el secreto del webhook (`secreto_env`)."""
    return "WEBHOOK_SECRETO_" + re.sub(r"[^A-Z0-9]", "_", integrador.upper())


def registro_comercios(dominio: str, prefijo: str | None, comercio: str) -> dict[str, Any]:
    """El comercio de la simulación detrás del túnel (`tunel_router.py`)."""
    base = f"https://{dominio}" + (f"/{prefijo}" if prefijo else "")
    return {
        "por_defecto": comercio,
        "comercios": [{
            "id": comercio,
            "nombre": "Simulación (comercio general detrás del túnel)",
            "a2a_url": f"{base}/a2a/merchant_agent",
            "catalogo_url": base,
            "merchant_ids": ["merchant_1"],
            "respaldo_fixture": False,
        }],
    }


def _linea_env(nombre: str, valor: str) -> str:
    if "\n" in valor or "\r" in valor:
        raise ValueError(f"{nombre} no puede tener saltos de línea")
    return f"{nombre}={valor}"


def _compacto(datos: Any) -> str:
    return json.dumps(datos, ensure_ascii=False, separators=(",", ":"))


def _enmascarar(clave: str) -> str:
    """`psg_<kid>_…`: el kid es público; el secreto no se muestra."""
    prefijo, _, resto = clave.partition("_")
    kid = resto.partition("_")[0]
    return f"{prefijo}_{kid}_…"


def _avisar_si_no_ignorada(carpeta: Path) -> None:
    try:
        relativa = carpeta.resolve().relative_to(RAIZ)
    except ValueError:
        return  # fuera del repo: no se sube
    if relativa.parts[:1] != (".temp-db",):
        print(f"AVISO: {relativa} no está bajo .temp-db/: revisá que .gitignore y "
              ".vercelignore la ignoren antes de commitear o desplegar.", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Genera los secretos del motor para Vercel (clave del recibo, clave de "
        "integrador y registros) en archivos. No imprime ningún secreto."
    )
    p.add_argument("--carpeta", type=Path, default=CARPETA_POR_DEFECTO,
                   help="dónde escribir (.temp-db/vercel/, ignorada por git)")
    p.add_argument("--kid", default=KID_POR_DEFECTO, help=f"kid de la clave del recibo ({KID_POR_DEFECTO})")
    p.add_argument("--integrador", default="demo", help="id del integrador (demo)")
    p.add_argument("--nombre", default=None, help="nombre visible del integrador")
    p.add_argument("--comercio", action="append", default=None,
                   help="comercio que puede usar (se repite; merchant_1)")
    p.add_argument("--webhook-url", default=None, help="URL https del webhook del integrador")
    p.add_argument("--dominio", default=None,
                   help="dominio del túnel (<x>.ngrok-free.app): arma GATEWAY_COMERCIOS_JSON")
    p.add_argument("--sin-prefijo", action="store_true",
                   help="con --dominio: sin prefijo secreto en las rutas del túnel")
    p.add_argument("--sin-recibo", action="store_true", help="no generar la clave del recibo")
    p.add_argument("--sin-integrador", action="store_true", help="no generar la clave de integrador")
    p.add_argument("--clave-lectura", metavar="INTEGRADOR", default=None,
                   help="solo esto: una clave de solo lectura (panel web) para un integrador que ya "
                   "está en GATEWAY_CLAVES_JSON.json; la agrega al registro y a vercel.env")
    p.add_argument("--forzar", action="store_true",
                   help="pisar archivos existentes (¡la clave del recibo cambia!; con "
                   "--clave-lectura, otra clave de lectura más)")
    return p


# Opciones que no tienen sentido con --clave-lectura (no genera nada de eso).
_SIN_CLAVE_LECTURA = (("dominio", "--dominio"), ("webhook_url", "--webhook-url"),
                      ("comercio", "--comercio"), ("nombre", "--nombre"))


def _clave_lectura(args: argparse.Namespace) -> int:
    """`--clave-lectura <id>`: suma una clave de solo lectura al registro que
    ya está en la carpeta. Valida todo antes de escribir; la clave va primero
    a su archivo y después el registro (si algo se corta en el medio, queda a
    lo sumo una clave sin entrada, que no sirve para nada)."""
    integrador = args.clave_lectura
    usadas = [opcion for atributo, opcion in _SIN_CLAVE_LECTURA if getattr(args, atributo)]
    if usadas:
        print(f"--clave-lectura va solo (con --carpeta y --forzar): sacá {', '.join(usadas)}",
              file=sys.stderr)
        return 2
    if not _ID_RE.fullmatch(integrador):
        print("--clave-lectura: minúsculas, dígitos, '_' o '-', hasta 40", file=sys.stderr)
        return 2
    carpeta = args.carpeta if args.carpeta.is_absolute() else Path.cwd() / args.carpeta
    registro_path = carpeta / "GATEWAY_CLAVES_JSON.json"
    try:
        registro = json.loads(registro_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"No está {registro_path}: generá primero el integrador "
              "(sin --clave-lectura).", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"{registro_path.name} no se puede leer como JSON ({type(e).__name__})",
              file=sys.stderr)
        return 2
    try:
        nuevo, clave = agregar_clave_lectura(registro, integrador)
    except RegistroInvalido as e:
        print(f"{registro_path.name}: {e}", file=sys.stderr)
        return 2

    _avisar_si_no_ignorada(carpeta)
    escritor = Escritor(carpeta, forzar=args.forzar)
    nombre_clave = f"clave_lectura_{integrador}.txt"
    if not args.forzar and escritor.existentes([nombre_clave]):
        print(f"Ya existe {carpeta / nombre_clave} (no piso nada). Con --forzar se suma otra "
              "clave de lectura; la anterior sigue activa en el registro hasta que la des de "
              "baja (\"activa\": false).", file=sys.stderr)
        return 1
    compacto = _compacto(nuevo)
    escritor.escribir(nombre_clave, clave)
    escritor.reemplazar(registro_path.name, compacto)
    entorno_path = carpeta / "vercel.env"
    if entorno_path.exists():
        texto = entorno_path.read_text(encoding="utf-8")
        escritor.reemplazar("vercel.env", _con_linea_env(texto, "GATEWAY_CLAVES_JSON", compacto))

    print(f"Listo. Clave de solo lectura {_enmascarar(clave)} para el integrador {integrador} "
          "(GET en /v1 para el panel; no sirve para comprar ni para A2A).")
    print(f"Archivos (permisos 600) en {carpeta}:")
    for ruta in escritor.escritos:
        print(f"  {ruta.name}")
    print("Ningún secreto se imprimió. Para que valga en Vercel, actualizá la variable y "
          "redesplegá (las claves se leen al arrancar; cambia producción, confirmalo antes):")
    print(f"  npx vercel env add GATEWAY_CLAVES_JSON production --force < {registro_path}")
    print("  npx vercel deploy --prod")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.clave_lectura is not None:
        return _clave_lectura(args)
    comercios = args.comercio or ["merchant_1"]
    if not _KID_RE.fullmatch(args.kid):
        print("--kid: hasta 64 caracteres de [A-Za-z0-9._-]", file=sys.stderr)
        return 2
    if not _ID_RE.fullmatch(args.integrador) or not all(_ID_RE.fullmatch(c) for c in comercios):
        print("--integrador y --comercio: minúsculas, dígitos, '_' o '-', hasta 40", file=sys.stderr)
        return 2
    dominio = (args.dominio or "").strip().lower().removeprefix("https://").rstrip("/") or None
    if dominio is not None and not _DOMINIO_RE.fullmatch(dominio):
        print("--dominio: solo el nombre (p. ej. mi-kaiai.ngrok-free.app), sin esquema ni ruta",
              file=sys.stderr)
        return 2
    if args.webhook_url and not args.webhook_url.startswith("https://"):
        print("--webhook-url tiene que ser https://", file=sys.stderr)
        return 2

    carpeta = args.carpeta if args.carpeta.is_absolute() else Path.cwd() / args.carpeta
    carpeta.mkdir(parents=True, exist_ok=True)
    _avisar_si_no_ignorada(carpeta)
    escritor = Escritor(carpeta, forzar=args.forzar)

    nombres: list[str] = ["vercel.env"]
    if not args.sin_recibo:
        nombres += ["guard_signing_key.pem", "GUARD_SIGNING_KEY_PEM_B64.txt",
                    "GUARD_SIGNING_KID.txt", "guard_public_jwk.json"]
    if not args.sin_integrador:
        nombres += [f"clave_integrador_{args.integrador}.txt", "GATEWAY_CLAVES_JSON.json"]
        if args.webhook_url:
            nombres.append(f"{nombre_secreto_webhook(args.integrador)}.txt")
    if dominio:
        nombres += ["GATEWAY_COMERCIOS_JSON.json"] + ([] if args.sin_prefijo else ["TUNEL_PREFIJO.txt"])
    if not args.forzar:
        ya = escritor.existentes(nombres)
        if ya:
            print("Ya existen (no piso nada; --forzar para regenerar):", file=sys.stderr)
            for ruta in ya:
                print(f"  {ruta}", file=sys.stderr)
            return 1

    entorno: list[str] = ["# Generado por deploy/vercel/generar_secretos.py. SECRETO: no lo subas."]
    resumen: list[str] = []
    if not args.sin_recibo:
        pem, jwk = clave_del_recibo(args.kid)
        b64 = base64.b64encode(pem).decode("ascii")
        escritor.escribir("guard_signing_key.pem", pem)
        escritor.escribir("GUARD_SIGNING_KEY_PEM_B64.txt", b64)
        escritor.escribir("GUARD_SIGNING_KID.txt", args.kid)
        escritor.escribir("guard_public_jwk.json", json.dumps(jwk, indent=2) + "\n")
        entorno += [_linea_env("GUARD_SIGNING_KEY_PEM_B64", b64), _linea_env("GUARD_SIGNING_KID", args.kid)]
        resumen.append(f"clave del recibo P-256, kid {args.kid}")
    if not args.sin_integrador:
        registro, clave, secreto_env, secreto = registro_integradores(
            args.integrador, args.nombre, comercios, args.webhook_url
        )
        escritor.escribir(f"clave_integrador_{args.integrador}.txt", clave)
        escritor.escribir("GATEWAY_CLAVES_JSON.json", _compacto(registro))
        entorno.append(_linea_env("GATEWAY_CLAVES_JSON", _compacto(registro)))
        resumen.append(f"integrador {args.integrador}: clave {_enmascarar(clave)} "
                       f"(comercios {', '.join(comercios)})")
        if secreto_env and secreto:
            escritor.escribir(f"{secreto_env}.txt", secreto)
            entorno.append(_linea_env(secreto_env, secreto))
            resumen.append(f"secreto del webhook en la variable {secreto_env}")
    if dominio:
        prefijo = None if args.sin_prefijo else secrets.token_urlsafe(18)
        if prefijo:
            escritor.escribir("TUNEL_PREFIJO.txt", prefijo)
        comercio = comercios[0]
        registro_c = _compacto(registro_comercios(dominio, prefijo, comercio))
        escritor.escribir("GATEWAY_COMERCIOS_JSON.json", registro_c)
        entorno.append(_linea_env("GATEWAY_COMERCIOS_JSON", registro_c))
        resumen.append(f"comercio {comercio} detrás de https://{dominio}"
                       + (" con prefijo secreto" if prefijo else " (sin prefijo)"))
    escritor.escribir("vercel.env", "\n".join(entorno) + "\n")

    print("Listo. " + "; ".join(resumen) + ".")
    print(f"Archivos (permisos 600) en {carpeta}:")
    for ruta in escritor.escritos:
        print(f"  {ruta.name}")
    print("Ningún secreto se imprimió. Cargalos en Vercel con "
          "`npx vercel env add <NOMBRE> production < <archivo>` (deploy/vercel/README.md).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except YaExiste as e:
        print(f"ya existe {e} (--forzar para pisarlo)", file=sys.stderr)
        sys.exit(1)
