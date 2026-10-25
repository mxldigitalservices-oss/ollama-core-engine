"""CLI: python3 -m apikeys generate --name app --prefix sk --ttl 86400   (requiere APIKEYS_PEPPER)."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .keys import JsonFileStore, KeyManager


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m apikeys")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="crea una API key (se muestra una sola vez)")
    g.add_argument("--name", required=True)
    g.add_argument("--prefix", default="sk")
    g.add_argument("--ttl", type=float, default=None, help="segundos de vida (omitir = sin vencimiento)")
    g.add_argument("--scopes", default="", help="alcances separados por comas")
    g.add_argument("--store", default=os.getenv("APIKEYS_STORE", ""), help="JSON donde se guardan SOLO los hashes")
    args = ap.parse_args(argv)
    pepper = os.getenv("APIKEYS_PEPPER", "")
    if len(pepper) < 16:
        print("Define APIKEYS_PEPPER (mínimo 16 caracteres, ej. openssl rand -hex 32)", file=sys.stderr)
        return 2
    if not args.store:
        print("Indica --store (o APIKEYS_STORE): sin almacén no podrás verificar la clave después", file=sys.stderr)
        return 2
    manager = KeyManager(pepper, JsonFileStore(args.store))
    key = manager.create(args.name, prefix=args.prefix, ttl=args.ttl,
                         scopes=tuple(s for s in args.scopes.split(",") if s))
    print(json.dumps({"key_id": key.key_id, "token": key.token, "persistido": bool(args.store)}, ensure_ascii=False))
    print("Guarda el token ahora: no se puede recuperar.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
