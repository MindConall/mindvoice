"""Instrumentación de rendimiento del HUD. Solo lectura: no cambia el comportamiento.

Vive en su propio módulo para que `overlay.py` solo tenga que llamarlo, y todo
queda apagado salvo que se active por variable de entorno o desde el HUD.

Se activa con ``MINDVOICE_PERF=1`` (o ``MINDVOICE_PERF=overlay,prompt`` para
elegir qué se mide), o en caliente desde el propio HUD con
``PERF.encender()``, que es lo que hace el panel de diagnóstico (Ctrl+Shift+D).
Escribir el informe: con ``MINDVOICE_PERF_OUT`` se indica un fichero JSON; si no,
se deja en el directorio de datos de la app.

Qué mide:

* **FPS del HUD.** No se puede contar en un ``paintEvent`` porque el HUD no tiene
  ninguno: es un árbol de widgets de stock y Qt repinta por su cuenta. Lo que sí
  existe y sí va al ritmo del refresco visible es ``_drain_ui_queue``, el timer
  que vuelca el estado del motor en pantalla. Se cuenta cuántas veces se completa
  un ciclo de volcado por segundo y se reportan los tramos más largos sin
  refrescar, que es donde se nota un tirón en la UI.
* **Latencia de arranque.** Cuánto se tarda desde el ``import`` del módulo hasta
  que el HUD está construido y visible.
* **Tamaño del prompt por turno.** Caracteres de la nota de memoria y del
  contexto de pantalla que se inyectan al modelo, más una estimación de tokens.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Iterable

# Muestreo de duración de los ciclos de volcado de la UI. Con 600 muestras se
# cubre un minuto largo sin comer memoria.
_UI_SAMPLES = 600
# Tamaño de ventana para las medias móviles del prompt.
_PROMPT_WINDOW = 50

# Una estimación de tokens: los providers usan ~4 caracteres por token. No es
# exacta, pero sirve para comparar tamaños entre turnos y fases, que es lo que
# buscamos. Para el número bueno hay que usar el contador del modelo.
_CHARS_PER_TOKEN = 4.0

# Medidores que se encienden a la vez. ``all`` los activa todos de una.
_MEDIDORES = ("overlay", "prompt")

# Selección de medidores impuesta desde la app (``PERF.encender()``). ``None``
# significa "manda la variable de entorno"; un conjunto significa que el HUD
# tiene el mando. Vive fuera de la instancia para que ``_wanted`` siga siendo
# una función sin estado que no necesita conocer al singleton.
_SELECCION: set[str] | None = None


def _env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("", "0", "false", "no", "off")


def _wanted(name: str) -> bool:
    """``True`` si este medidor está habilitado.

    Manda ``MINDVOICE_PERF`` salvo que alguien haya llamado a ``encender()``:
    así el panel de diagnóstico puede pedir las métricas sin que el usuario
    tenga que reiniciar la app con variables de entorno.
    """
    if _SELECCION is not None:
        return "all" in _SELECCION or name in _SELECCION
    sel = os.environ.get("MINDVOICE_PERF", "").strip().lower()
    if sel in ("", "0", "false", "no", "off"):
        return False
    if sel in ("1", "true", "yes", "on", "all"):
        return True
    return name in (s.strip() for s in sel.split(","))


def _corto(n: float) -> str:
    """Un número que quepa en un panel estrecho: 1234 se lee ``1.2k``."""
    n = float(n)
    if abs(n) >= 1000.0:
        return "%.1fk" % (n / 1000.0)
    if abs(n) >= 100.0:
        return "%.0f" % n
    return "%.1f" % n


class Perf:
    """Recolector de métricas del HUD. Una instancia por proceso."""

    def __init__(self) -> None:
        self.enabled = _env_flag("MINDVOICE_PERF")
        self._lock = threading.Lock()
        self._t0 = time.perf_counter()

        # Arranque de la UI.
        self._ui_built_ms: float | None = None
        self._ui_visible_ms: float | None = None

        # Ciclos de volcado de la UI (proxy de FPS).
        self._ui_cycles = 0
        self._ui_gaps = deque(maxlen=_UI_SAMPLES)
        self._last_ui: float | None = None
        # Arranque de la ventana de muestreo de ciclos. Se fija en el primer
        # ciclo, no al importar el módulo: el tiempo de import no cuenta.
        self._ui_window_start: float | None = None
        self._ui_busiest_ms = 0.0
        self._ui_total_ms = 0.0

        # Volcado a pantalla (latencias).
        self._drops = 0
        self._drain_s = 0
        self._drain_ms = deque(maxlen=_PROMPT_WINDOW)

        # Tamaño del prompt.
        self._prompt_chars = deque(maxlen=_PROMPT_WINDOW)
        self._screen_chars = deque(maxlen=_PROMPT_WINDOW)
        self._prompt_notes = 0
        self._last_prompt: dict | None = None

    # ------------------------------------------------------------------
    # Arranque
    # ------------------------------------------------------------------
    def mark_ui_built(self) -> None:
        """El HUD ya construyó su árbol de widgets."""
        with self._lock:
            if self._ui_built_ms is None:
                self._ui_built_ms = (time.perf_counter() - self._t0) * 1000.0
                self._log("UI construida en %.1f ms" % self._ui_built_ms)

    def mark_ui_visible(self) -> None:
        """El HUD ya está visible en pantalla."""
        with self._lock:
            if self._ui_visible_ms is None:
                self._ui_visible_ms = (time.perf_counter() - self._t0) * 1000.0
                self._log("UI visible en %.1f ms" % self._ui_visible_ms)

    # ------------------------------------------------------------------
    # FPS / fluidez de la UI
    # ------------------------------------------------------------------
    def ui_cycle_start(self) -> None:
        """Marca el inicio de un ciclo de volcado de la UI."""
        if not self.enabled or not _wanted("overlay"):
            return
        now = time.perf_counter()
        with self._lock:
            self._drain_s = now
            if self._ui_window_start is None:
                self._ui_window_start = now
            if self._last_ui is not None:
                gap = (now - self._last_ui) * 1000.0
                self._ui_gaps.append(gap)
                if gap > self._ui_busiest_ms:
                    self._ui_busiest_ms = gap
            self._last_ui = now

    def ui_cycle_end(self) -> None:
        """Marca el final de un ciclo de volcado y cuenta lo que se pintó."""
        if not self.enabled or not _wanted("overlay"):
            return
        with self._lock:
            if self._drain_s:
                self._drain_ms.append((time.perf_counter() - self._drain_s) * 1000.0)
                self._drain_s = 0.0
            self._ui_cycles += 1

    def note_drop(self, n: int = 1) -> None:
        """Anota mensajes de UI que se descartaron por saturación de la cola."""
        with self._lock:
            self._drops += n

    # ------------------------------------------------------------------
    # Tamaño del prompt
    # ------------------------------------------------------------------
    def note_prompt(self, memory_chars: int, screen_chars: int, notes: int = 0) -> None:
        """Registra el tamaño de lo que se inyecta al modelo en un turno."""
        if not self.enabled or not _wanted("prompt"):
            return
        with self._lock:
            self._prompt_chars.append(int(memory_chars))
            self._screen_chars.append(int(screen_chars))
            self._prompt_notes = int(notes)
            total = int(memory_chars) + int(screen_chars)
            self._last_prompt = {
                "memoria_chars": int(memory_chars),
                "pantalla_chars": int(screen_chars),
                "total_chars": total,
                "total_tokens_aprox": round(total / _CHARS_PER_TOKEN, 1),
                "notas_visuales": int(notes),
            }
            self._log("prompt: %s" % self._last_prompt)

    # ------------------------------------------------------------------
    # Encendido en caliente
    # ------------------------------------------------------------------
    def encender(self, medidores: Iterable[str] = _MEDIDORES) -> None:
        """Pide las métricas desde la app, sin variable de entorno.

        Es lo que cumple la promesa del módulo ("se activa ... o desde el
        HUD"): el panel de diagnóstico lo llama al abrirse. Con el
        arranque ya en marcha, lo único que cambia es que a partir de ahora
        se anotan ciclos, volcados y tamaños de prompt; no se puede
        "recuperar" lo anterior.
        """
        global _SELECCION
        with self._lock:
            self.enabled = True
            _SELECCION = {str(m).strip().lower() for m in medidores} or {"all"}
            self._log("instrumentación encendida en caliente: %s" % sorted(_SELECCION))

    def apagar(self) -> None:
        """Devuelve el mando a ``MINDVOICE_PERF``.

        No apaga lo que pidió el arranque: si el proceso se lanzó con
        ``MINDVOICE_PERF=1``, las métricas siguen porque el entorno manda
        sobre el panel. Lo que sí se para es lo que el panel encendió.
        """
        global _SELECCION
        with self._lock:
            _SELECCION = None
            self.enabled = _env_flag("MINDVOICE_PERF")

    # ------------------------------------------------------------------
    # Informe
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Métricas de este momento, sin escribir nada en disco."""
        with self._lock:
            gaps = list(self._ui_gaps)
            drains = list(self._drain_ms)
            prompts = list(self._prompt_chars)
            screens = list(self._screen_chars)
            out: dict = {
                "habilitado": self.enabled,
                "segundos_desde_import": round(time.perf_counter() - self._t0, 2),
            }
            if self._ui_built_ms is not None:
                out["ui_construida_ms"] = round(self._ui_built_ms, 1)
            if self._ui_visible_ms is not None:
                out["ui_visible_ms"] = round(self._ui_visible_ms, 1)

            out["ui_ciclos"] = self._ui_cycles
            out["ui_mensajes_descartados"] = self._drops
            if gaps:
                # Los ciclos por segundo se miden sobre TODO el tiempo que el
                # HUD lleva muestréando, no desde el último ciclo: si no, un
                # snapshot tomado justo tras un ciclo da un valor sin sentido.
                ventana_s = (time.perf_counter() - self._ui_window_start) if self._ui_window_start else 0.0
                media = sum(gaps) / len(gaps)
                out["ui_ciclo_medio_ms"] = round(media, 2)
                out["ui_ciclo_max_ms"] = round(max(gaps), 2)
                out["ui_ciclos_por_segundo_teorico"] = round(1000.0 / media, 1) if media else None
                # Un tramo largo entre ciclos es un turno en el que la UI se
                # quedó sin refrescar: es lo que el ojo percibe como tirón.
                out["ui_tramos_sobre_100ms"] = sum(1 for g in gaps if g > 100.0)
                out["ui_tramos_sobre_500ms"] = sum(1 for g in gaps if g > 500.0)
                if ventana_s > 0:
                    out["ui_ciclos_por_segundo_real"] = round(self._ui_cycles / ventana_s, 2)
                    out["ui_ventana_segundos"] = round(ventana_s, 2)
            if drains:
                out["ui_volcado_medio_ms"] = round(sum(drains) / len(drains), 3)
                out["ui_volcado_max_ms"] = round(max(drains), 3)

            if prompts:
                n = len(prompts)
                out["prompt_turnos"] = n
                out["memoria_chars_medio"] = round(sum(prompts) / n, 1)
                out["memoria_chars_max"] = max(prompts)
            if screens:
                n = len(screens)
                out["pantalla_chars_medio"] = round(sum(screens) / n, 1)
                out["pantalla_chars_max"] = max(screens)
            if prompts or screens:
                # El divisor es el número de TURNOS, no el de muestras: memoria
                # y pantalla se guardan en dos colas y una por turno, así que
                # sumar sus longitudes dividía por el doble y salía la mitad
                # justo del tamaño del prompt, que es la cifra que de verdad
                # importa para no reventar la ventana de contexto.
                tot = sum(prompts) + sum(screens)
                turnos_prompt = max(len(prompts), len(screens))
                out["prompt_total_chars_medio"] = (
                    round(tot / turnos_prompt, 1) if turnos_prompt else 0
                )
                out["prompt_tokens_aprox_medio"] = (
                    round(tot / turnos_prompt / _CHARS_PER_TOKEN, 1) if turnos_prompt else 0
                )
            if self._last_prompt:
                out["ultimo_turno"] = self._last_prompt
            return out

    def resumen(self) -> list[str]:
        """El informe de ``snapshot`` en tres líneas para un panel estrecho.

        Se arma leyendo el ``snapshot`` y no los campos sueltos a propósito:
        así el texto del HUD no puede enseñar una cifra que el JSON del
        informe no tenga. Si un medidor está apagado o aún no tiene datos, su
        línea no aparece en vez de enseñar ceros falsos.
        """
        d = self.snapshot()
        lineas: list[str] = []

        # 1) Fluidez: cada cuánto riega la UI y cuánto tarda cada riego.
        if "ui_ciclos_por_segundo_real" in d:
            linea = "HUD %.1f/s · ciclo %.1f ms (máx %.1f)" % (
                d["ui_ciclos_por_segundo_real"],
                d["ui_ciclo_medio_ms"],
                d["ui_ciclo_max_ms"],
            )
        else:
            linea = "HUD sin ciclos medidos"
        if "ui_volcado_medio_ms" in d:
            linea += " · volcado %.2f ms (máx %.2f)" % (
                d["ui_volcado_medio_ms"],
                d["ui_volcado_max_ms"],
            )
        lineas.append(linea)

        # 2) Tirones y saturación: lo que el ojo nota como "se ha parado".
        tirones = "tirones >100 ms %d · >500 ms %d" % (
            d.get("ui_tramos_sobre_100ms", 0),
            d.get("ui_tramos_sobre_500ms", 0),
        )
        lineas.append(
            "%s · cola descartada %d · %d ciclos" % (
                tirones,
                d.get("ui_mensajes_descartados", 0),
                d.get("ui_ciclos", 0),
            )
        )

        # 3) Arranque y prompt: cuánto costó entrar y cuánto se le inyecta.
        arranque = []
        if "ui_construida_ms" in d:
            arranque.append("construida %.0f ms" % d["ui_construida_ms"])
        if "ui_visible_ms" in d:
            arranque.append("visible %.0f ms" % d["ui_visible_ms"])
        if arranque:
            lineas.append("arranque: " + " · ".join(arranque))
        if "prompt_turnos" in d:
            turnos = d["prompt_turnos"]
            lineas.append(
                "prompt %s car (mem %s / pantalla %s) ~ %s tok · %d %s"
                % (
                    _corto(d.get("prompt_total_chars_medio", 0)),
                    _corto(d.get("memoria_chars_medio", 0)),
                    _corto(d.get("pantalla_chars_medio", 0)),
                    _corto(d.get("prompt_tokens_aprox_medio", 0)),
                    turnos,
                    "turno" if turnos == 1 else "turnos",
                )
            )

        if not d.get("habilitado"):
            # El panel enseña el caso "arrancado sin instrumentación": sin
            # esto, un HUD que se ve normal dejaría pensar que va a 0/s.
            lineas.insert(0, "instrumentación apagada (MINDVOICE_PERF)")
        return lineas

    def save(self, path: str | None = None) -> str:
        """Escribe el informe en JSON y devuelve la ruta usada."""
        data = self.snapshot()
        dest = path or os.environ.get("MINDVOICE_PERF_OUT") or self._default_path()
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - la instrumentación nunca rompe la app
            return ""
        return dest

    @staticmethod
    def _default_path() -> str:
        try:
            from rutas import ensure_data_dir

            return os.path.join(ensure_data_dir(), "perf-hud.json")
        except Exception:  # noqa: BLE001 - si no hay rutas, al temp
            import tempfile

            return os.path.join(tempfile.gettempdir(), "mindvoice-perf-hud.json")

    def _log(self, msg: str) -> None:
        """Log por el logger estándar, para no depender del logger del HUD."""
        try:
            import logging

            logging.getLogger("mindvoice.perf").info(msg)
        except Exception:  # noqa: BLE001
            pass


# Instancia única del proceso. Importarla es barato aunque esté apagada.
PERF = Perf()


def enabled() -> bool:
    """``True`` si hay que recoger métricas."""
    return PERF.enabled