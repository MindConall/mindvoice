"""Backend plano: los dos JSON de siempre.

Es el backend de referencia. Su ``block()`` tiene que producir exactamente lo
mismo que producía ``LiveAssistant._build_memory_block`` antes de la Fase 1,
porque es la promesa de "sin grafo, todo igual que antes".
"""

from __future__ import annotations

import json
import logging
import os

from .base import (
    BLOCK_BUDGET,
    ENTRY_MAX_CHARS,
    EXTRACTED,
    MemoryBackend,
    MemoryEntry,
    ahora_iso,
    normalizar,
    recortar,
)

logger = logging.getLogger(__name__)

# Tope de entradas en cada capa. Son los mismos números que usa el bloque plano,
# extraídos tal cual para no cambiar el comportamiento por accidente.
MAX_BRIEF = 24
MAX_PERMANENT = 30
PERMANENT_MAX_CHARS = 600
BRIEF_HEADING = "[Breve (conversación reciente)]"
PERMANENT_HEADING = "[A largo plazo (hechos persistentes resumidos)]"


class FlatBackend(MemoryBackend):
    """Memoria en listas: ``memory.json`` (breve) y ``memory-long.json`` (larga)."""

    nombre = "plano"

    def __init__(self, brief_path: str, permanent_path: str) -> None:
        self._brief_path = brief_path
        self._permanent_path = permanent_path
        self._brief: list[MemoryEntry] = []
        self._permanent: list[MemoryEntry] = []
        self._seq = 0
        self.cargar()

    # ------------------------------------------------------------------ carga
    def cargar(self) -> None:
        """Relee los dos archivos. Nunca lanza: si están corruptos, vacíos."""
        self._brief = self._leer_breve()
        self._permanent = self._leer_permanente()

    def _leer_breve(self) -> list[MemoryEntry]:
        """Aplica los mismos filtros de validación que el cargador antiguo."""
        try:
            with open(self._brief_path, encoding="utf-8") as fh:
                guardada = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.debug("Memoria breve no disponible: %s", exc)
            return []
        memoria: list[MemoryEntry] = []
        for entrada in guardada if isinstance(guardada, list) else []:
            if (
                isinstance(entrada, dict)
                and entrada.get("role") in ("user", "assistant")
                and isinstance(entrada.get("text"), str)
                and entrada["text"].strip()
            ):
                self._seq += 1
                memoria.append(
                    MemoryEntry(
                        id=f"b{self._seq}",
                        text=entrada["text"].strip(),
                        role=entrada["role"],
                        kind="brief",
                        created_at=entrada.get("created_at") or ahora_iso(),
                    )
                )
        return memoria[-MAX_BRIEF:]

    def _leer_permanente(self) -> list[MemoryEntry]:
        try:
            with open(self._permanent_path, encoding="utf-8") as fh:
                guardados = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.debug("Memoria a largo plazo no disponible: %s", exc)
            guardados = []
        if not isinstance(guardados, list):
            logger.debug("Memoria a largo plazo con formato inesperado; se ignora.")
            return []
        resumenes: list[MemoryEntry] = []
        for i, texto in enumerate(guardados):
            if isinstance(texto, str) and texto.strip():
                self._seq += 1
                resumenes.append(
                    MemoryEntry(
                        id=f"p{i}",
                        text=texto.strip()[:PERMANENT_MAX_CHARS],
                        role="system",
                        kind="permanent",
                        importance=0.7,
                        confidence=EXTRACTED,
                        created_at=ahora_iso(),
                    )
                )
        return resumenes[-MAX_PERMANENT:]

    # ------------------------------------------------------------ persistencia
    def _guardar_breve(self) -> None:
        """Escribe solo ``role`` y ``text``: el formato que ya consume el resto."""
        try:
            with open(self._brief_path, "w", encoding="utf-8") as fh:
                json.dump(
                    [{"role": e.role, "text": e.text} for e in self._brief[-MAX_BRIEF:]],
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
        except (OSError, TypeError) as exc:
            logger.warning("No se pudo guardar la memoria: %s", exc)

    def _guardar_permanente(self) -> None:
        try:
            with open(self._permanent_path, "w", encoding="utf-8") as fh:
                json.dump(
                    [e.text[:PERMANENT_MAX_CHARS] for e in self._permanent[-MAX_PERMANENT:]],
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
        except (OSError, TypeError) as exc:
            logger.warning("No se pudo guardar la memoria a largo plazo: %s", exc)

    # -------------------------------------------------------------- escritura
    def remember(self, text: str, role: str = "user", **campos) -> MemoryEntry | None:
        texto = (text or "").strip()
        if not texto:
            return None
        self._seq += 1
        entrada = MemoryEntry(
            id=f"b{self._seq}",
            text=texto,
            role=role,
            kind="brief",
            importance=float(campos.get("importance", 0.5)),
            confidence=campos.get("confidence", EXTRACTED),
            tags=list(campos.get("tags") or []),
        )
        self._brief.append(entrada)
        self._evict()
        self._guardar_breve()
        return entrada

    def _evict(self) -> list[MemoryEntry]:
        """Saca lo más viejo por arriba y lo devuelve para archivar."""
        if len(self._brief) <= MAX_BRIEF:
            return []
        sobrante = len(self._brief) - MAX_BRIEF
        salidas = self._brief[:sobrante]
        del self._brief[:sobrante]
        self._guardar_breve()
        return salidas

    def archivar(self, entradas) -> None:
        """Añade resúmenes a la memoria a largo plazo."""
        anadidas = False
        for e in entradas or []:
            texto = (getattr(e, "text", None) or "").strip()[:PERMANENT_MAX_CHARS]
            if not texto:
                continue
            self._permanent.append(
                MemoryEntry(
                    id=f"p{len(self._permanent)}",
                    text=texto,
                    role="system",
                    kind="permanent",
                    importance=0.7,
                    confidence=EXTRACTED,
                )
            )
            anadidas = True
        if anadidas:
            self._permanent = self._permanent[-MAX_PERMANENT:]
            self._guardar_permanente()

    def forget(self, entry_id: str) -> bool:
        antes = len(self._brief) + len(self._permanent)
        self._brief = [e for e in self._brief if e.id != entry_id]
        self._permanent = [e for e in self._permanent if e.id != entry_id]
        if len(self._brief) + len(self._permanent) == antes:
            return False
        self._guardar_breve()
        self._guardar_permanente()
        return True

    def reset(self) -> bool:
        """Borra la breve y conserva la larga, como el comando de siempre."""
        self._brief = []
        self._guardar_breve()
        return True

    def export(self) -> str:
        return json.dumps(
            {
                "backend": self.nombre,
                "brief": [e.a_dict() for e in self._brief],
                "permanent": [e.a_dict() for e in self._permanent],
            },
            ensure_ascii=False,
            indent=2,
        )

    # -------------------------------------------------------------- lectura
    def retrieve(
        self, query: str = "", limite: int = 8, presupuesto: int = BLOCK_BUDGET
    ) -> list[MemoryEntry]:
        """El plano no sabe buscar: devuelve lo último, como siempre se hizo.

        La diferencia con el grafo está en que aquí el filtro por relevance no
        existe, y por eso manda la misma ventana de siempre.
        """
        candidatos = self._permanent[-limite:] + self._brief[-limite:]
        return candidatos[:limite]

    def block(self, query: str = "", presupuesto: int = BLOCK_BUDGET) -> str:
        """Reproduce el bloque original, sección por sección.

        El recorte por presupuesto se hace sobre las líneas de la sección breve,
        como en el código original, y el orden de secciones es larga -> breve.
        """
        secciones: list[str] = []

        lineas_larga = []
        for resumen in self._permanent:
            texto = (
                resumen.text
                if len(resumen.text) <= PERMANENT_MAX_CHARS
                else resumen.text[:PERMANENT_MAX_CHARS] + "…"
            )
            lineas_larga.append(f"- {texto}")
        if lineas_larga:
            secciones.append(PERMANENT_HEADING + "\n" + "\n".join(lineas_larga))

        if self._brief:
            lineas = []
            for entrada in self._brief:
                texto = entrada.text
                if len(texto) > ENTRY_MAX_CHARS:
                    texto = texto[:ENTRY_MAX_CHARS] + "…"
                quien = "usuario" if entrada.role == "user" else "MindVoice"
                lineas.append(f"- {quien}: {texto}")
            while len("\n".join(lineas)) > presupuesto and len(lineas) > 1:
                lineas.pop(0)
            secciones.append(BRIEF_HEADING + "\n" + "\n".join(lineas))

        return "\n\n".join(secciones)