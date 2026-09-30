"""Comprueba que todos los .py del proyecto siguen compilando.

Existe porque el editor de parches se comió la sangría de la primera línea de un
bloque en dos ocasiones durante la Fase 2, y un error de sangría en
``live_assistant.py`` (6 000 líneas) no sale hasta que algo lo importa. Un
``ast.parse`` por fichero lo dice al momento y en un segundo.

    python tools/comprobar_sangria.py              # todo el proyecto
    python tools/comprobar_sangria.py memory/*.py  # solo lo que le pases

Falla con código 1 si algo no compila, para poder ponerlo en CI.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# El runtime embebido y el volcado monolítico no son código nuestro: se excluyen
# para que el informe diga algo útil.
EXCLUIDOS = {".venv", "__pycache__", "staging", "installer", "~"}


def revisar(ruta: Path) -> str | None:
    """Mensaje de error si el fichero no compila; ``None`` si está bien."""
    try:
        fuente = ruta.read_text(encoding="utf-8")
    except OSError as exc:
        return f"{ruta}: no se pudo leer ({exc})"
    try:
        ast.parse(fuente, filename=str(ruta))
    except SyntaxError as exc:
        return f"{ruta}:{exc.lineno}: {exc.msg}"
    return None


def _por_defecto(raiz: Path) -> list[Path]:
    return sorted(
        p
        for p in raiz.rglob("*.py")
        if not (EXCLUIDOS & set(p.parts))
        and p.name != "mindvoice_codigo_completo.py"
    )


def main(argv: list[str]) -> int:
    raiz = Path(__file__).resolve().parent.parent
    rutas = [Path(a) for a in argv] if argv else _por_defecto(raiz)
    fallos = [f for f in (revisar(r) for r in rutas) if f]
    for fallo in fallos:
        print(fallo)
    print(f"{len(rutas)} fichero(s) revisados, {len(fallos)} problema(s).")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
