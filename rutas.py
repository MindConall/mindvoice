"""Rutas de datos de usuario de MindVoice.

La app guarda memoria, alarmas, preferencias y transcripciones. Históricamente
esos archivos vivían *junto al código* (``memory.json`` en la raíz del
proyecto). Eso funciona en una copia de desarrollo, pero rompe en cuanto la app
se instala en un sitio de solo lectura:

- El instalador deja la app en ``C:\\Program Files``, y un usuario normal no
  puede escribir ahí. Los ``open(..., "w")`` fallarían con ``PermissionError``
  y la memoria se perdería en silencio (ya están envueltos en
  ``try/except``, así que no hay crash, pero tampoco hay persistencia).
- El código tampoco está garantizado en la máquina del usuario.

Este módulo centraliza la decisión: los datos mutables van a un directorio
específico del usuario y la instalación queda libre de permisos de escritura.

- Windows: ``%LOCALAPPDATA%\\MindVoice``
- Linux/macOS: ``~/.local/share/mindvoice``

Cada ruta resuelve con este orden:

1. Si existe el override de entorno ``MINDVOICE_DATA_DIR``, se respeta (lo usan
   las pruebas y los builds de CI).
2. Si el directorio de datos del usuario ya tiene el archivo, se usa tal cual.
3. Si el archivo solo existe en la raíz del código (instalación antigua o copia
   de desarrollo), se **migra** al directorio de usuario. La copia solo ocurre
   la primera vez y nunca pisa un archivo que ya exista en el destino.

Importar este módulo **no** crea el directorio en disco: se hace bajo demanda
en ``ensure_data_dir()``, para que importar ``config`` siga siendo inocuo.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

APP_NAME = "mindvoice"

ENV_DATA_DIR = "MINDVOICE_DATA_DIR"


def app_dir() -> Path:
    """Carpeta que contiene el código de la app (la del repo o la del install)."""
    return Path(__file__).resolve().parent


def user_data_dir(create: bool = False) -> Path:
    """Directorio de datos de usuario, donde se escribe todo lo mutable.

    Windows usa ``%LOCALAPPDATA%\\MindVoice`` (con la clave que el usuario
    tiene permisos garantizados); en Linux/macOS, ``~/.local/share/mindvoice``.
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        base = Path(override).expanduser()
    elif sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            r"~\AppData\Local"
        )
        base = Path(root) / "MindVoice"
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
            "~/.local/share"
        )
        base = Path(root).expanduser() / APP_NAME
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base


def ensure_data_dir() -> Path:
    """Crea el directorio de datos si hace falta y lo devuelve."""
    return user_data_dir(create=True)


def data_file(name: str, migrate: bool = True) -> Path:
    """Ruta de un archivo de datos, migrándolo desde la raíz si es necesario.

    ``name`` es un nombre simple (no una ruta con separadores).
    """
    if not migrate:
        return app_dir() / name

    destination = user_data_dir() / name
    if destination.exists():
        return destination

    legacy = app_dir() / name
    if legacy.exists():
        moved = _migrate(legacy, destination)
        if moved is not None:
            return moved
    return destination


def _migrate(legacy: Path, destination: Path) -> Optional[Path]:
    """Copia ``legacy`` a ``destination``. Devuelve el destino o ``None``.

    Nunca pisa un archivo existente: si el destino apareció entre medias (dos
    procesos arrancando a la vez), se descarta la copia y se gana el archivo
    que ya estaba. Cualquier error se registra y se ignora: que la migración
    falle no puede impedir que la app arranque.
    """
    try:
        ensure_data_dir()
        staged = destination.with_name(destination.name + ".migrando")
        shutil.copy2(legacy, staged)
        os.replace(staged, destination)
        logger.info(
            "Migrado %s al directorio de datos del usuario (%s)",
            legacy.name,
            destination.parent,
        )
        return destination
    except FileExistsError:
        return destination if destination.exists() else None
    except Exception as exc:  # noqa: BLE001 - la migración es best-effort
        logger.warning("No se pudo migrar %s: %s", legacy.name, exc)
        try:
            legacy.with_name(legacy.name + ".migrando").unlink(missing_ok=True)
        except OSError:
            pass
        return None


def logs_dir(create: bool = True) -> Path:
    """Carpeta de registros: ``<datos>/logs``.

    Los registros vivían junto al código (``launcher.log`` en la raíz del
    proyecto). En una instalación en "Program Files" eso no es escribible, y
    como el lanzador configura ``logging.FileHandler`` en ese arranque, la app
    moría antes de mostrar nada. Aquí se degradan los registros a la carpeta
    temporal en vez de impedir que la app arranque.
    """
    try:
        base = ensure_data_dir() / "logs"
        if create:
            base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError as exc:
        logger.warning(
            "No se pudo preparar la carpeta de registros (%s); se usa la temporal.",
            exc,
        )
        fallback = Path(tempfile.gettempdir()) / "mindvoice-logs"
        if create:
            fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def log_file(name: str) -> Path:
    """Ruta completa de un archivo de registro dentro de :func:`logs_dir`."""
    return logs_dir() / name


def is_writable(path: Path) -> bool:
    """¿Se puede escribir realmente en ``path``? (comprobación real)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.with_name(path.name + ".escritura")
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def python_exe(windowless: bool = False) -> str:
    """Intérprete con el que lanzar los procesos hijos de la app.

    Antes esto estaba fijado a ``<proyecto>/.venv/Scripts/python.exe``, lo cual
    solo es correcto en una copia de desarrollo: la instalación empaquetada no
    lleva ``.venv``, así que el lanzador y el overlay arrancaban con una ruta
    inexistente y morían sin mensaje.

    Orden de preferencia:

    1. El ``.venv`` del proyecto, si existe (desarrollo con entorno virtual).
    2. El intérprete que ya está ejecutando este proceso, que en la
       instalación empaquetada es el runtime embebido que viene junto al código.
    """
    suffix = "pythonw.exe" if windowless else "python.exe"
    venv_script = app_dir() / ".venv" / "Scripts" / suffix
    if venv_script.exists():
        return str(venv_script)
    current = Path(sys.executable)
    if windowless and current.name.lower() != "pythonw.exe":
        # Se invocó con python.exe: se busca el pythonw hermano, que existe en
        # cualquier instalación de Python de Windows y evita una consola
        # pegada al escritorio en los procesos en segundo plano.
        sibling = current.with_name("pythonw.exe")
        if sibling.exists():
            return str(sibling)
    return str(current)
