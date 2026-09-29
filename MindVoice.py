"""Arranque de la instalación de MindVoice.

Este archivo es el punto de entrada real de la app instalada (es lo que lanzan
los accesos directos). Existe por un motivo concreto: el runtime de Python
embebido que se empaqueta **no** añade la carpeta del script a ``sys.path`` (a
diferencia de un Python normal, porque manda el archivo ``pythonXY._pth``). Sin
este ``import`` explícito, ``import overlay`` fallaría con ModuleNotFoundError
y la app no arrancaría con ningún mensaje útil.

También fija la codificación de la salida a UTF-8: en una consola española el
registro con acentos y emojis revienta con ``UnicodeEncodeError`` si la
consola está en cp850, que es el valor por defecto heredado en Windows.

Ejecuta el ``start_mindvoice.py`` del proyecto, que es el que se encarga de
arrancar el lanzador en segundo plano y mostrar el overlay.
"""

from __future__ import annotations

import os
import runpy
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# La app va primero: el runtime embebido no la añade por su cuenta.
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Todo lo que el usuario tiene por delante: que los logs y la salida de
# consola no mueran al escribir tildes o emojis.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        # No es una TextIOWrapper (redirección a otra cosa): se deja como está.
        pass

# La consola de Windows arranca en una página de códigos heredada (cp850 en
# español), donde cualquier tilde o emoji revienta la salida. Se pasa a UTF-8.
# Es cosmético y no debe romper nada si falla.
try:
    if os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
except Exception:  # noqa: BLE001
    pass


def main() -> int:
    entry = os.path.join(APP_DIR, "start_mindvoice.py")
    if not os.path.exists(entry):
        print(f"No se encuentra {entry}. La instalación está incompleta.",
              file=sys.stderr)
        return 1
    try:
        # start_mindvoice.py termina con sys.exit(main()), así que su código de
        # salida llega aquí como SystemExit y hay que propagarlo.
        runpy.run_path(entry, run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
