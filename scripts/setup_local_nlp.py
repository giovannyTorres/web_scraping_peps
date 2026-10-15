#!/usr/bin/env python3
"""Explicit GitHub-only model acquisition; application runtime never calls this."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import urllib.request

URL = "https://github.com/explosion/spacy-models/releases/download/es_core_news_sm-3.8.0/es_core_news_sm-3.8.0-py3-none-any.whl"
SHA256 = "e451a83d6df79b87e9eed0cb553f03e99e36a3bab18a7b79f0dcfd1fdf875e12"
FILENAME = "es_core_news_sm-3.8.0-py3-none-any.whl"
SIZE_BYTES = 12884212


def verify(path: Path) -> None:
    actual_size = path.stat().st_size
    if actual_size != SIZE_BYTES:
        raise ValueError(f"Tamaño inválido para {path.name}: {actual_size}; esperado {SIZE_BYTES}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != SHA256:
        raise ValueError(f"SHA256 inválido para {path.name}: {actual}; esperado {SHA256}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--download", action="store_true", help="Descarga explícita desde release oficial de GitHub.")
    source.add_argument("--wheel", type=Path, help="Usa un wheel local, sin ninguna red.")
    parser.add_argument("--directory", type=Path, default=Path.home() / ".cache" / "peps-models")
    parser.add_argument("--install", action="store_true", help="Instala solo el wheel verificado en este Python; dependencias se instalan por separado.")
    args = parser.parse_args()
    print("Modelo oficial es_core_news_sm 3.8.0: GNU GPL 3.0; fuentes AnCora GPL3, WikiNER CC BY4.0.")
    print("Uso comercial permitido bajo sus condiciones. Consulte docs/NLP_LOCAL.md antes de redistribuir.")
    path = args.wheel
    try:
        if args.download:
            args.directory.mkdir(parents=True, exist_ok=True)
            path = args.directory / FILENAME
            if not path.exists():
                temporary = path.with_suffix(".download")
                try:
                    with urllib.request.urlopen(URL, timeout=60) as response, temporary.open("wb") as output:
                        received = 0
                        while chunk := response.read(min(1024 * 1024, SIZE_BYTES - received + 1)):
                            received += len(chunk)
                            if received > SIZE_BYTES:
                                raise ValueError(f"Descarga excede {SIZE_BYTES} bytes; se cancela antes de instalar.")
                            output.write(chunk)
                    verify(temporary)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        verify(path)
        print(f"Verificado: {path} sha256={SHA256}")
        if args.install:
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", str(path)], check=True)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"No instalado: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
