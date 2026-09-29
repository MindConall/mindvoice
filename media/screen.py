"""Captura de la pantalla principal a bajo coste de ancho de banda.

Toma una instantánea de la pantalla con ``mss``, la reduce a una resolución
razonable (máximo ``max_size`` píxeles en el lado más largo), conservando la
proporción, y la codifica en JPEG para minimizar el tamaño de cada fotograma
que viaja por el WebSocket.

FASE 3 (rendimiento y economía de tokens):

- **Detección de cambios**: antes de reencodificar, se muestrea un *preview*
  en miniatura (un byte por píxel de cada ``_probe_step`` muestreado, con
  slicing de bytes a nivel C, sin pasar por PIL) y se compara con el anterior.
  Si la pantalla no cambió de forma significativa, ``capture_if_changed()``
  devuelve ``None`` y el llamador no envía nada a Gemini: se ahorra el re-encode
  JPEG completo (CPU) y el consumo de la cuota de la API (tokens).
- **Rate limiting**: ``capture_if_changed()`` no captura más a menudo que
  ``min_interval`` (derivado de ``screen_fps``); si el llamador insiste dentro
  de la ventana devuelve ``None`` (no reenvía una imagen vieja como si fuera
  actual). Para un fotograma SIEMPRE fresco se usa ``capture()``, que ignora el
  rate limiting: es la llamada de cada nuevo turno (texto o voz).
"""

import io
import logging
import time
from typing import Optional

import mss
from PIL import Image

logger = logging.getLogger(__name__)

try:  # PIL >= 10
    RESAMPLE_ALIAS = getattr(Image, "Resampling", Image)
    LANCZOS = RESAMPLE_ALIAS.LANCZOS
except AttributeError:  # pragma: no cover - PIL antiguo
    LANCZOS = Image.LANCZOS

# Cada cuántos píxeles se muestrea el preview de comparación. Un valor de 8 da
# un preview minúsculo (p. ej. 480×270 para un monitor 4K) que se compara en
# milisegundos.
_PROBE_STEP = 8
# Cambio mínimo por muestra (0-255) para considerar que la pantalla varió de
# forma significativa. Por debajo, se considera estática y no se reenvía.
_DIFF_THRESHOLD = 2.0


class ScreenCapture:
    """Captura de pantalla → bytes JPEG listos para enviar a Gemini."""

    def __init__(
        self,
        monitor: int = 1,
        max_size: int = 1024,
        quality: int = 85,
        min_interval: float = 1.0,
    ) -> None:
        self._max_size = max_size
        self._quality = quality
        self._min_interval = max(0.0, float(min_interval))
        self._sct = mss.mss()
        # ``monitors[0]`` es la pantalla virtual compuesta; ``monitors[1]`` la
        # pantalla principal. Guardamos el dict del monitor ya resuelto.
        self._monitor = self._sct.monitors[monitor]
        self._w = int(self._monitor["width"])
        self._h = int(self._monitor["height"])
        self._preview: Optional[bytes] = None
        self._jpeg_cache: Optional[bytes] = None
        self._last_grab_ts = 0.0
        logger.info("Captura de pantalla preparada (monitor %d)", monitor)

    @property
    def resolution(self):
        """Resolución de pantalla (ancho, alto)."""
        return self._w, self._h

    def _sample_preview(self, raw: bytes) -> bytes:
        """Preview en miniatura (1 byte/píxel muestreado) sin pasar por PIL.

        ``raw`` es el BGRA completo; se impone la fila de un canal (bytes 0, 4,
        8, … de cada línea) con slicing C nativo, así el coste es mínimo.
        """
        step = _PROBE_STEP
        row_bytes = self._w * 4
        lines = []
        for y in range(0, self._h, step):
            start = y * row_bytes
            lines.append(raw[start : start + row_bytes : step * 4])
        return b"".join(lines)

    def _changed(self, raw: bytes) -> bool:
        """``True`` si la pantalla cambió lo bastante respecto a la anterior."""
        preview = self._sample_preview(raw)
        old = self._preview
        self._preview = preview
        if old is None or len(old) != len(preview):
            return True
        total = 0
        count = 0
        for a, b in zip(old, preview):
            total += abs(a - b)
            count += 1
        if count == 0:
            return False
        return (total / count) > _DIFF_THRESHOLD

    def _encode_jpeg(self, img: Image.Image) -> bytes:
        """Reduce la imagen y la devuelve codificada como JPEG."""
        img.thumbnail((self._max_size, self._max_size), LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._quality, optimize=True)
        return buf.getvalue()

    def _encode_from_raw(self, shot, raw: bytes) -> bytes:
        """Construye la imagen RGB y la codifica; además rellena la caché."""
        # mss ofrece píxeles en BGRA; los convertimos a RGB en el propio
        # constructor de PIL para no copiar la imagen dos veces.
        img = Image.frombytes("RGB", shot.size, raw, "raw", "BGRX")
        jpeg = self._encode_jpeg(img)
        self._jpeg_cache = jpeg
        return jpeg

    def capture(self) -> bytes:
        """Captura SIEMPRE un fotograma fresco y devuelve su JPEG.

        Se usa al inicio de un turno nuevo (una orden de texto o de voz) donde
        interesa la imagen más reciente aunque la pantalla no haya cambiado.

        Raises:
            mss.ScreenShotError: si el monitor deja de estar disponible
                (p. ej. al cambiar la resolución de pantalla).
        """
        self._last_grab_ts = time.monotonic()
        shot = self._sct.grab(self._monitor)
        raw = shot.bgra
        self._changed(raw)  # refresca la baseline del preview en silencio
        return self._encode_from_raw(shot, raw)

    def capture_if_changed(self) -> Optional[bytes]:
        """JPEG solo si la pantalla cambió de forma significativa.

        Devuelve ``None`` cuando la pantalla está estática (el llamador salta
        ese envío) o se está dentro de la ventana de rate limiting. Sin cambios
        no se reencodifica ni se gasta cuota de tokens de Gemini. En pantallas
        vivas (videojuegos) el coste es el de siempre: un JPEG por llamada.
        """
        now = time.monotonic()
        if now - self._last_grab_ts < self._min_interval:
            # Rate limiting (screen_fps): no se vuelve a llamar a mss, pero
            # TAMPOCO se devuelve la caché antigua como si fuera un fotograma
            # actual. Reenviar un JPEG viejo hacía que el modelo "viera" una
            # pantalla pretérita (p. ej. el escritorio con iconos) mientras el
            # usuario miraba otra cosa. Un fotograma fresco se obtiene con
            # capture() (que no aplica rate limiting).
            return None
        self._last_grab_ts = now
        shot = self._sct.grab(self._monitor)
        raw = shot.bgra
        if not self._changed(raw):
            return None
        return self._encode_from_raw(shot, raw)

    def close(self) -> None:
        """Libera los recursos de ``mss``."""
        self._sct.close()