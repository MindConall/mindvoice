"""Elección del backend y migración de los datos que ya había.

La decisión tiene que ser boring: si algo va mal, plano. Un fallo al construir
el grafo no puede impedir que MindVoice arranque y responda.
"""

from __future__ import annotations

import logging
import os

from .base import (
    ENV_BACKEND,
    ENV_GRAPH_DIR,
    MemoryBackend,
    ahora_iso,
    backend_forzado,
)
from .flat_backend import FlatBackend
from .graph_backend import GRAPH_FILENAME, GraphBackend

logger = logging.getLogger(__name__)

# Subcarpeta dentro del directorio de datos del usuario. No se usa el repo:
# cuando la app está instalada en "Program Files" el repo ni siquiera se puede
# escribir, que es justo el motivo por el que la memoria ya vive en
# %LOCALAPPDATA% desde el commit 0a76c69.
SUBCARPETA = "mindvoice-memory"


def directorio_grafo(data_dir: str) -> str:
    """Ruta del grafo de memoria, con override por entorno para las pruebas."""
    forzado = (os.environ.get(ENV_GRAPH_DIR) or "").strip()
    return forzado or os.path.join(data_dir, SUBCARPETA)


def grafo_disponible(data_dir: str) -> bool:
    """¿Se puede usar el grafo? Comprueba escritura sin tocar nada."""
    ruta = directorio_grafo(data_dir)
    try:
        os.makedirs(ruta, exist_ok=True)
        prueba = os.path.join(ruta, ".escribible")
        with open(prueba, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(prueba)
    except OSError as exc:
        logger.info("El grafo de memoria no está disponible (%s).", exc)
        return False
    return os.path.isdir(ruta)


def get_backend(data_dir: str, brief_path: str, permanent_path: str) -> MemoryBackend:
    """Devuelve el backend activo.

    El orden importa: una petición explícita del entorno se respeta siempre; si
    no la hay, se usa el grafo cuando está disponible y se cae al plano en
    cualquier excepción, avisando una sola vez.
    """
    forzado = backend_forzado()
    if forzado == "flat":
        logger.info("Memoria: backend plano forzado por %s.", ENV_BACKEND)
        return FlatBackend(brief_path, permanent_path)
    if forzado == "graph":
        # Aunque se fuerce el grafo, si no se puede construir se cae al plano:
        # "forzado" no puede significar "roto".
        try:
            backend = GraphBackend(directorio_grafo(data_dir))
            logger.info("Memoria: backend grafo forzado por %s.", ENV_BACKEND)
            return backend
        except Exception as exc:  # noqa: BLE001 - la memoria no puede tumbar la app
            logger.warning(
                "Se pidió el grafo pero no se pudo construir (%s); se usa el plano.",
                exc,
            )
            return FlatBackend(brief_path, permanent_path)

    try:
        if grafo_disponible(data_dir):
            backend = GraphBackend(directorio_grafo(data_dir))
            logger.info(
                "Memoria: backend grafo en %s (%s).",
                directorio_grafo(data_dir),
                os.path.join(directorio_grafo(data_dir), GRAPH_FILENAME),
            )
            return backend
        logger.info(
            "Memoria: %s no está disponible; se usa el backend plano de siempre.",
            ENV_GRAPH_DIR or "el directorio de datos",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo preparar el grafo de memoria (%s); se usa el plano.", exc)
    return FlatBackend(brief_path, permanent_path)


# Marca en el grafo para no migrar dos veces. Vive en el propio grafo, así que
# si alguien borra el grafo vuelve a migrar (que es lo correcto).
_MIGRATION_MARK = "migrated_from_flat_v1"


def _ya_migrado(grafo: dict) -> bool:
    return bool(grafo.get(_MIGRATION_MARK))


def migrar(
    backend: MemoryBackend, brief_path: str, permanent_path: str
) -> dict:
    """Copia la memoria plana al grafo, una vez.

    No borra los JSON: siguen siendo la copia de seguridad y permiten volver al
    plano con ``MINDVOICE_MEMORY=flat`` sin haber perdido nada.

    Devuelve un recuento para el log y para poder mostrarlo en las pruebas.
    """
    if isinstance(backend, FlatBackend):
        return {"estado": "plano", "insertados": 0, "motivo": "el backend activo es plano"}
    if _ya_migrado(backend._grafo):
        return {"estado": "sin cambios", "insertados": 0, "motivo": "ya migrado"}

    plano = FlatBackend(brief_path, permanent_path)
    insertados = 0
    for entrada in plano._brief:
        if backend.remember(entrada.text, role=entrada.role, kind="brief") is not None:
            insertados += 1
    for entrada in plano._permanent:
        if (
            backend.remember(
                entrada.text, role="system", kind="permanent", importance=0.7
            )
            is not None
        ):
            insertados += 1

    with backend._lock:
        backend._grafo[_MIGRATION_MARK] = {
            "cuando": ahora_iso(),
            "insertados": insertados,
            "origen": [os.path.basename(brief_path), os.path.basename(permanent_path)],
        }
        backend._persistir()

    logger.info(
        "Memoria migrada al grafo: %d recuerdos desde los JSON planos "
        "(los archivos originales se conservan).",
        insertados,
    )
    return {"estado": "migrado", "insertados": insertados}