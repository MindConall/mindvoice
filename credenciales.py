"""Almacén local de credenciales.

MindVoice necesita una clave de la API de Gemini. La vía histórica era definir
``GEMINI_API_KEY`` como variable de entorno del sistema antes de instalar nada,
lo cual es un muro infranqueable para quien no es desarrollador: no aparece en
la barra de tareas, no se hereda al abrir la app desde un acceso directo y no
se puede explicar en un README de "un clic".

Este módulo guarda la clave en el directorio de datos del usuario para que la
app funcione sin tocar variables de entorno. La **precedencia** es:

1. ``GEMINI_API_KEY`` del entorno (lo que ya venía siendo, y lo que gana: útil
   para pruebas, CI y usuarios avanzados).
2. ``MINDVOICE_API_KEY``, alias explícito por si el entorno ya está ocupado.
3. El almacén local cifrado de este módulo.

En Windows el almacén usa **DPAPI** (``CryptProtectData``) con el ámbito del
usuario actual: el archivo queda cifrado y, además, solo se puede descifrar
desde la misma cuenta de Windows que lo escribió. Copiar el ``secrets.json`` a
otra máquina no sirve de nada, que es justo lo que se quiere de un archivo de
credenciales. En Linux y macOS no hay DPAPI, así que se guarda cifrado con
``Fernet`` si ``cryptography`` está disponible y, si no, en claro con permisos
restrictivos y una advertencia en el log.

Nada de esto se sube al repositorio: el archivo está en ``.gitignore`` y, en
Windows, además resulta ilegible fuera de la cuenta del usuario.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Optional

from rutas import ensure_data_dir

logger = logging.getLogger(__name__)

# Entornos que se consultan antes que el almacén local.
ENV_KEYS = ("GEMINI_API_KEY", "MINDVOICE_API_KEY")

SECRETS_FILE = "secrets.json"

# Se usa el flag de entropía para que el cifrado quede atado al nombre de la
# app: aunque alguien copie el blob a otro programa suyo hecho con DPAPI, no
# descifra.
_DPAPI_ENTROPY = b"MindVoice::secrets.json::v1"


def secrets_path() -> Path:
    """Ruta del almacén local. Vive en el directorio de datos del usuario, que
    es lo único escribible cuando la app está instalada en "Program Files"."""
    return ensure_data_dir() / SECRETS_FILE


# ---------------------------------------------------------------------------
# DPAPI (Windows)
# ---------------------------------------------------------------------------

class _DataBlob(ctypes.Structure):
    """``DATA_BLOB`` de crypt32.dll."""

    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _dpapi_available() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def _dpapi_protect(data: bytes) -> Optional[str]:
    """Cifra con DPAPI (ámbito usuario). Devuelve base64 o ``None``."""
    if not _dpapi_available():
        return None
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src, entropy = _blob(data), _blob(_DPAPI_ENTROPY)
    out = _DataBlob()
    # CRYPTPROTECT_UI_FORBIDDEN: sin diálogo, falla si no hay usuario.
    if not crypt32.CryptProtectData(
        ctypes.byref(src), "MindVoice", ctypes.byref(entropy), None, None,
        0x01, ctypes.byref(out),
    ):
        logger.warning("DPAPI no disponible (%s); se guarda la clave sin cifrar.",
                       kernel32.GetLastError())
        return None
    try:
        return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(blob_b64: str) -> Optional[str]:
    """Descifra un blob de DPAPI. Devuelve ``None`` si falla."""
    if not _dpapi_available():
        return None
    try:
        raw = base64.b64decode(blob_b64)
    except Exception:  # noqa: BLE001
        return None
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src, entropy = _blob(raw), _blob(_DPAPI_ENTROPY)
    out = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(src), None, ctypes.byref(entropy), None, None, 0x01,
        ctypes.byref(out),
    ):
        logger.debug("DPAPI no pudo descifrar las credenciales (%s).",
                     kernel32.GetLastError())
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out.pbData)


def _fernet_available() -> bool:
    try:
        import cryptography.fernet  # noqa: F401
    except ImportError:
        return False
    return True


def _fernet_key() -> bytes:
    """Clave de Fernet derivada de una passphrase fija.

    NO es cifrado real contra un atacante local: la clave está en el código.
    Solo evita que la clave de Gemini aparezca en claro si alguien mira el
    archivo o lo abre sin querer en un editor. En Windows, que es la plataforma
    soportada, manda DPAPI y esto no se usa.
    """
    import hashlib

    digest = hashlib.sha256(_DPAPI_ENTROPY).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet_encrypt(plain: str) -> Optional[str]:
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    return Fernet(_fernet_key()).encrypt(plain.encode()).decode()


def _fernet_decrypt(blob: str) -> Optional[str]:
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    try:
        return Fernet(_fernet_key()).decrypt(blob.encode()).decode()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def load_api_key() -> str:
    """Devuelve la clave de Gemini disponible, o ``""`` si no hay ninguna.

    El entorno tiene prioridad sobre el almacén local para que un usuario
    avanzado pueda sobrescribir la clave guardada sin tocarla.
    """
    for name in ENV_KEYS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return _read_stored_key()


def _read_stored_key() -> str:
    path = secrets_path()
    if not path.exists():
        return ""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - un JSON corrupto no debe tumbar
        logger.warning("No se pudo leer el almacén de credenciales: %s", exc)
        return ""
    if not isinstance(stored, dict):
        return ""
    algo = stored.get("cipher", "")
    value = stored.get("key", "")
    if not isinstance(value, str) or not value:
        return ""
    if algo == "dpapi":
        return _dpapi_unprotect(value) or ""
    if algo == "fernet":
        return _fernet_decrypt(value) or ""
    # Sin cifrar: se acepta para no perder la clave si alguien la migró a mano,
    # pero se avisa por log porque es el estado menos seguro.
    if algo == "plain":
        logger.warning(
            "La clave de Gemini está guardada sin cifrar en %s. "
            "Bórrala y vuelve a configurarla para protegerla.", path)
        return value
    return ""


def save_api_key(key: str) -> bool:
    """Guarda la clave cifrada. Devuelve ``True`` si se escribió."""
    key = key.strip()
    if not key:
        return False
    path = secrets_path()
    payload = {"cipher": "", "key": key}

    blob = _dpapi_protect(key.encode("utf-8"))
    if blob:
        payload = {"cipher": "dpapi", "key": blob, "machine": os.environ.get(
            "COMPUTERNAME", ""), "user": os.environ.get("USERNAME", "")}
    elif _fernet_available():
        blob = _fernet_encrypt(key)
        if blob:
            payload = {"cipher": "fernet", "key": blob}
    else:
        payload["cipher"] = "plain"
        logger.warning(
            "Sin DPAPI ni 'cryptography': la clave se guardará sin cifrar en %s.",
            path,
        )

    staged = path.with_name(path.name + ".tmp")
    try:
        staged.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(staged, path)
        _restrict_permissions(path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo guardar la clave: %s", exc)
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def clear_api_key() -> None:
    """Borra la clave guardada (el entorno no se toca)."""
    path = secrets_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("No se pudo borrar el almacén de credenciales: %s", exc)


def _restrict_permissions(path: Path) -> None:
    """En POSIX, deja el archivo legible solo por el dueño."""
    if sys.platform == "win32":
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def store_location() -> str:
    """Ruta del almacén, para mostrarla en los ajustes y en los avisos."""
    return str(secrets_path())


# Alias corto para el resto del código.
def get_api_key() -> str:
    return load_api_key()
