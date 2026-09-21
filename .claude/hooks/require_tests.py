#!/usr/bin/env python3
"""Hook `Stop`: impide terminar un turno que cambio codigo sin tocar tests.

POR QUE EXISTE
Este repo llego a 9.444 lineas y 390 funciones con CERO tests. El resultado,
medido: la app se rompio cuatro veces en una semana y en dos de ellas se
perdieron minutos de reunion reales. Un test que no es obligatorio no se
escribe, asi que la obligacion va por mecanismo y no por buena intencion.

POR QUE EN `Stop` Y NO EN `PreToolUse`
Un hook de `PreToolUse` solo ve UNA edicion y no puede saber si el test se
escribio dos acciones antes. `Stop` corre cuando el agente va a terminar el
turno, asi que ve el turno completo via `git diff`.

POR QUE EN PYTHON Y NO EN BASH
`jq` no esta garantizado en Windows y `bash` tampoco en la maquina de un
companero. Python si: es una app Python.

QUE NO PUEDE HACER (limitacion honesta)
Solo ve lo que hace Claude Code. Si alguien edita un .py en VS Code y
commitea, este hook no se entera. Para eso estan las otras dos capas:
.pre-commit-config.yaml y el workflow de GitHub Actions.

Salida 2 = bloqueo. Ningun modo de permisos puede saltarselo.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Scripts de diagnostico manual: no forman parte de la app y no piden test.
# Misma lista que el `omit` de coverage en pyproject.toml.
EXENTOS = ('diagnostico.py', '_merge_wavs.py')
EXENTOS_PREFIJO = ('check_',)


def _git(*args) -> list[str]:
    try:
        out = subprocess.run(
            ['git', *args], cwd=REPO, capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _cambiados() -> list[str]:
    """Ficheros tocados en el arbol de trabajo: modificados, en staging y
    nuevos sin seguimiento."""
    vistos = dict.fromkeys(
        _git('diff', '--name-only', 'HEAD')
        + _git('diff', '--cached', '--name-only')
        + _git('ls-files', '--others', '--exclude-standard')
    )
    return list(vistos)


def _en_staging() -> list[str]:
    """Solo lo que va a entrar en el commit. Lo usa la capa de pre-commit."""
    return list(dict.fromkeys(_git('diff', '--cached', '--name-only')))


def _es_codigo_de_app(ruta: str) -> bool:
    if not ruta.endswith('.py'):
        return False
    if ruta.startswith(('tests/', '.claude/')):
        return False
    nombre = ruta.rsplit('/', 1)[-1]
    if nombre in EXENTOS or nombre.startswith(EXENTOS_PREFIJO):
        return False
    return True


def _revisar(cambiados: list[str], codigo_de_bloqueo: int, sufijo: str) -> int:
    if not cambiados:
        return 0

    codigo = sorted(f for f in cambiados if _es_codigo_de_app(f))
    tests = sorted(f for f in cambiados if f.startswith('tests/'))
    if not codigo or tests:
        return 0

    listado = '\n'.join(f'  - {f}' for f in codigo)
    print(
        "BLOQUEADO: has cambiado codigo de la app sin tocar ningun test.\n\n"
        f"Ficheros de codigo modificados:\n{listado}\n\n"
        "Anade o actualiza los tests que cubran el cambio en tests/, y\n"
        "comprueba que fallan si se anula el arreglo (un test que pasa con\n"
        "y sin el cambio no prueba nada).\n\n"
        "  pytest -m unit\n"
        f"{sufijo}",
        file=sys.stderr,
    )
    return codigo_de_bloqueo


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Modo pre-commit: mira solo lo que va a entrar en el commit y devuelve 1,
    # que es lo que espera el framework. Misma logica, un solo sitio.
    if '--staged' in argv:
        return _revisar(
            _en_staging(), 1,
            "\nSi de verdad no necesita test, salta la comprobacion una vez\n"
            "con: git commit --no-verify",
        )

    try:
        entrada = json.load(sys.stdin)
    except Exception:
        entrada = {}

    # Anti-bucle: si ya se bloqueo y el agente sigue sin resolverlo, no
    # insistir indefinidamente. Claude Code tambien corta por su cuenta.
    if entrada.get('stop_hook_active'):
        return 0

    return _revisar(
        _cambiados(), 2,
        "\nSi el cambio de verdad no necesita test (por ejemplo, solo\n"
        "comentarios), dilo explicitamente en la respuesta y vuelve a\n"
        "intentarlo: este hook no insiste dos veces seguidas.",
    )


if __name__ == '__main__':
    sys.exit(main())
