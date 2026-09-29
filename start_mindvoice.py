"""Abre MindVoice (acceso directo del escritorio).

Arranca el lanzador en segundo plano (propietario del atajo global) si no
está en marcha y a continuación muestra el overlay. Si el overlay ya estaba
abierto, un nuevo doble clic lo muestra/oculta.

Uso::

    pythonw start_mindvoice.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from overlay import LAUNCHER_MUTEX, _mutex_exists
from rutas import python_exe

import firstrun

PROJECT_DIR = Path(__file__).resolve().parent
# El intérprete se resuelve en tiempo de ejecución: en desarrollo es el .venv
# del proyecto y en la instalación empaquetada el runtime embebido. Fijar la
# ruta a .venv aquí hacía que el lanzador arrancara con una ruta inexistente.
PYTHON = python_exe(windowless=False)
PYTHONW = python_exe(windowless=True)
CREATE_NO_WINDOW = 0x08000000
LOGO_ICO = str(PROJECT_DIR / "assets" / "mindvoice_logo.ico")


def _launcher_running() -> bool:
    try:
        return _mutex_exists(LAUNCHER_MUTEX)
    except Exception:  # noqa: BLE001 - si el mutex falla, asumir que no corre
        return False


def _refresh_shortcut(folder: Path, name: str) -> None:
    """Crea o actualiza un acceso directo con el icono 8-bit del robot.

    ``.lnk`` que abre ``pythonw start_mindvoice.py`` y usa
    ``assets/mindvoice_logo.ico`` como icono de aplicación (así el logo de
    Python no aparece en el Escritorio / Menú Inicio / barra de tareas).
    Idempotente: se vuelve a crear en cada arranque para corregir accesos
    directos antiguos que apuntaban a python.exe sin icono propio.

    Sin dependencias: usa ``WScript.Shell`` vía PowerShell (COM).
    """
    link = folder / (name + ".lnk")
    icon = LOGO_ICO if os.path.exists(LOGO_ICO) else PYTHONW
    script = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut({str(link)!r});"
        f"$s.TargetPath = {PYTHONW!r};"
        f"$s.WorkingDirectory = {str(PROJECT_DIR)!r};"
        f"$s.Arguments = {chr(34) + str(PROJECT_DIR / 'start_mindvoice.py') + chr(34)!r};"
        f"$s.Description = 'MindVoice: asistente de IA por voz';"
        f"$s.IconLocation = {icon + ',0'!r};"
        "$s.Save()"
    )
    try:
        subprocess.run(
            [
                os.environ.get("MINDVOICE_POWERSHELL", "powershell"),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            cwd=str(PROJECT_DIR),
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - el acceso directo es cosmético
        print(f"aviso: no se pudo crear el acceso directo ({exc})", file=sys.stderr)


def _refresh_shortcuts() -> None:
    """Refresca los accesos directos del Escritorio y del Menú Inicio."""
    desktop = Path(os.path.expandvars(r"%USERPROFILE%\Desktop"))
    if not desktop.exists():
        desktop = Path(os.path.expandvars(r"%PUBLIC%\Desktop"))
    start_menu = Path(
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")
    )
    for folder in (desktop, start_menu):
        if folder.exists():
            _refresh_shortcut(folder, "MindVoice")


def main() -> int:
    # Primera ejecución: si no hay clave de Gemini, se pregunta con un diálogo
    # y se guarda cifrada. Así el acceso directo del escritorio funciona con
    # doble clic sin que nadie tenga que tocar variables de entorno.
    if not firstrun.asegurar_clave():
        print(
            "Sin clave de la API de Gemini no se puede hablar con el modelo. "
            "Vuelve a abrir MindVoice para configurarla.",
            file=sys.stderr,
        )
        return 2
    if not _launcher_running():
        subprocess.Popen(
            [PYTHONW, str(PROJECT_DIR / "launcher.py")],
            cwd=str(PROJECT_DIR),
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
        for _ in range(50):  # espera al launcher (hasta ~5 s)
            if _launcher_running():
                break
            time.sleep(0.1)
        if not _launcher_running():
            print("aviso: el lanzador en segundo plano no arrancó.", file=sys.stderr)
            return 1
    _refresh_shortcuts()
    subprocess.call(
        [PYTHONW, str(PROJECT_DIR / "launcher.py"), "--press"],
        cwd=str(PROJECT_DIR),
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())