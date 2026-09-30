"""Memoria de MindVoice: plano por defecto, grafo cuando se puede.

Importar este paquete nunca debe lanzar. Si algo falla al construir el grafo, se
avisa y el sistema sigue con la memoria plana de siempre.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from .base import (  # noqa: E402
    CONTESTED,
    EXTRACTED,
    INFERRED,
    MemoryBackend,
    MemoryEntry,
    ahora_iso,
    autoetiquetas,
    normalizar,
    palabras,
    recortar,
)

__all__ = [
    "CONTESTED",
    "EXTRACTED",
    "INFERRED",
    "MemoryBackend",
    "MemoryEntry",
    "FlatBackend",
    "GraphBackend",
    "ahora_iso",
    "autoetiquetas",
    "get_backend",
    "migrar",
    "normalizar",
    "palabras",
    "recortar",
]


def __getattr__(name):
    """Carga los backends bajo demanda.

    Así ``import memory`` no arrastra el grafo ni toca el disco cuando solo se
    usan las utilidades de texto.
    """
    if name == "FlatBackend":
        from .flat_backend import FlatBackend

        return FlatBackend
    if name == "get_backend":
        from .migrate import get_backend

        return get_backend
    if name == "GraphBackend":
        from .graph_backend import GraphBackend

        return GraphBackend
    if name == "migrar":
        from .migrate import migrar

        return migrar
    raise AttributeError(f"el módulo memory no expone {name!r}")