#!/usr/bin/env python3
"""Lanza los tests unitarios con el interprete CORRECTO, para pre-commit.

pre-commit con `language: system` ejecuta `python` tal y como este en el PATH
del shell. Si el commit se hace sin el venv activado, eso es el Python del
sistema, que no tiene las dependencias de la app, y el commit falla con un
ModuleNotFoundError confuso en vez de con el resultado de los tests.

Este envoltorio prefiere el interprete del venv del repo y, si no lo
encuentra, avisa de forma clara en vez de dar un error incomprensible.

El prefijo _ evita que pytest lo recoja como fichero de tests.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CANDIDATOS = (
    REPO / '.venv' / 'Scripts' / 'python.exe',      # Windows
    REPO / '.venv' / 'bin' / 'python',              # por si acaso
    REPO / 'venv' / 'Scripts' / 'python.exe',
)


def _interprete() -> str:
    for c in CANDIDATOS:
        if c.exists():
            return str(c)
    return sys.executable


def main() -> int:
    py = _interprete()
    comprobacion = subprocess.run(
        [py, '-c', 'import numpy, soundfile, psutil, dotenv'],
        capture_output=True, text=True,
    )
    if comprobacion.returncode != 0:
        print(
            f"No se pueden correr los tests: a {py} le faltan dependencias.\n"
            f"  {comprobacion.stderr.strip().splitlines()[-1] if comprobacion.stderr else ''}\n\n"
            "Instalalas y vuelve a intentarlo:\n"
            "  pip install -r requirements.txt -r requirements-dev.txt",
            file=sys.stderr,
        )
        return 1

    return subprocess.run([py, '-m', 'pytest', '-m', 'unit', '-q'], cwd=REPO).returncode


if __name__ == '__main__':
    sys.exit(main())
