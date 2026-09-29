"""Adquisición continua de voz desde el micrófono.

PyAudio es bloqueante, así que la lectura del micrófono vive en un hilo de
trabajo propio. Cada fragmento PCM (16 kHz, 16 bits, mono) se entrega a una
cola ``asyncio.Queue`` de forma segura para que el bucle asíncrono lo envíe
por el WebSocket sin bloquearse nunca.
"""

import asyncio
import logging
import struct
import threading
import unicodedata
from typing import List, Optional

import pyaudio

logger = logging.getLogger(__name__)

# Pista para la selección automática del micrófono (por nombre del dispositivo).
DEFAULT_MIC_HINT = "FaceCam"


def _norm_name(name: str) -> str:
    """Nombre normalizado para comparar: minúsculas, NFC y sin espacios finales."""
    return unicodedata.normalize("NFC", (name or "").strip().lower())


def _repair_mojibake(name: str) -> str:
    """Corrige nombres cuyo UTF-8 se guardó como Latin-1 ('MicrÃ³fono'->'Micrófono')."""
    if not name or "Ã" not in name and "©" not in name:
        return name
    try:
        repaired = name.encode("latin-1", errors="ignore").decode(
            "utf-8", errors="ignore"
        )
        if repaired and repaired != name:
            return repaired
    except Exception:  # noqa: BLE001 - se conserva el nombre original
        pass
    return name


def list_input_devices() -> List[dict]:
    """Devuelve los micrófonos disponibles: índice, nombre, canales y host API."""
    pa = pyaudio.PyAudio()
    try:
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            channels = int(info.get("maxInputChannels") or 0)
            if channels > 0:
                host = "???"
                try:
                    host = str(pa.get_host_api_info_by_index(int(info["hostApi"]))["name"])
                except Exception:
                    pass
                devices.append(
                    {
                        "index": i,
                        "name": str(info.get("name")),
                        "channels": channels,
                        "default_rate": int(info.get("defaultSampleRate") or 0),
                        "host": host.upper(),
                    }
                )
        return devices
    finally:
        pa.terminate()


def _host_priority(host: str) -> int:
    """Preferencia de host API para mic de webcam.

    En este equipo el endpoint DirectSound del mic de webcam entrega audio
    recortado/roto a tope de escala (que desconecta la sesión Live y hace que
    la voz parezca "no detectada"), mientras que el MME del mismo mic entrega
    señal limpia y real. Se prefiere MME, luego WASAPI y al final DS/WDM-KS;
    el filtro ``_is_dead`` descarta igualmente los endpoints mudos.
    """
    if "MME" in host:
        return 0
    if "WASAPI" in host:
        return 1
    if "DIRECTSOUND" in host:
        return 2
    return 3


def input_device_candidates(
    device_name: Optional[str], hint: str = DEFAULT_MIC_HINT
) -> List[int]:
    """Índices de micrófonos a probar, en orden de preferencia.

    - ``device_name``: primera opción por coincidencia exacta de nombre
      (lo que el usuario elige en Ajustes).
    - El **micrófono predeterminado del sistema**: segunda opción (el que
      Windows marca como entrada por defecto; suele ser el que da señal real).
    - ``hint``: a continuación, dispositivos cuyo nombre contenga la pista.
    Se ordenan por host API (MME antes que WASAPI) y luego por índice. No todos
    los índices de un mismo micrófono entregan señal: el abrir/medir decide.
    """
    devices = list_input_devices()
    selected_name: Optional[str] = None
    if device_name:
        selected_name = device_name
    else:
        try:
            pa = pyaudio.PyAudio()
            try:
                default = pa.get_default_input_device_info()
                selected_name = str(default.get("name"))
            except Exception:
                selected_name = None
            finally:
                pa.terminate()
        except Exception:
            selected_name = None

    candidates: List[dict] = []
    if selected_name:
        wanted = _norm_name(selected_name)
        candidates.extend(
            d for d in devices if _norm_name(d["name"]) == wanted
        )
        # Si el nombre guardado viene con mojibake de una versión antigua,
        # se intenta la variante reparada antes de rendirse a la pista.
        if not candidates:
            repaired = _repair_mojibake(selected_name)
            wanted_repaired = _norm_name(repaired)
            candidates.extend(
                d for d in devices if _norm_name(d["name"]) == wanted_repaired
            )
    matched = [c["index"] for c in candidates]
    hint_matches = [
        d for d in devices
        if hint and _norm_name(hint) in _norm_name(d["name"])
        and d["index"] not in matched
    ]
    candidates.extend(hint_matches)
    candidates.sort(key=lambda d: (_host_priority(d["host"]), d["index"]))
    return [d["index"] for d in candidates]


class MicrophoneCapture:
    """Lector de micrófono en un hilo trabajador, alimentando un asyncio.Queue."""

    def __init__(
        self,
        rate: int = 16000,
        chunk_ms: int = 200,
        device_name: Optional[str] = None,
    ) -> None:
        self._rate = rate
        self._chunk_frames = int(rate * chunk_ms / 1000)
        self._device_name = device_name
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self._queue: asyncio.Queue = None
        self._loop: asyncio.AbstractEventLoop = None
        self._thread: threading.Thread = None
        self._running = False

    @property
    def rate(self) -> int:
        """Frecuencia de muestreo de la captura."""
        return self._rate

    def _try_open(self, index: Optional[int]):
        """Abre el flujo para un índice; devuelve ``None`` si no es posible."""
        kwargs = dict(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk_frames,
        )
        try:
            if index is not None:
                return self._pa.open(input_device_index=index, **kwargs)
            return self._pa.open(**kwargs)
        except OSError:
            return None

    def _is_dead(self, stream) -> bool:
        """True si el dispositivo entrega silencio digital (endpoint mudo).

        Se desechan los primeros milisegundos (pre-roll del stream, que algunos
        mic de webcam entregan en silencio) y se mide el máximo de ~0,5 s; un
        endpoint vivo siempre muestra algo de ruido ambiente.
        """
        peak = 0
        frames = int(self._rate * 0.1)  # 100 ms por lectura
        for _ in range(5):
            try:
                data = stream.read(frames, exception_on_overflow=False)
                if len(data) % 2:
                    data = data[:-1]
                peak = max(
                    peak,
                    max(
                        (abs(sample) for sample in struct.unpack(
                            "<%dh" % (len(data) // 2), data
                        )),
                        default=0,
                    ),
                )
            except OSError:
                return True
        return peak < 4

    def _pick_stream(self) -> None:
        """Elige el primer dispositivo que abra y no entregue silencio muerto."""
        for idx in input_device_candidates(self._device_name):
            stream = self._try_open(idx)
            if stream is None:
                continue
            if self._is_dead(stream):
                name = ""
                try:
                    name = str(self._pa.get_device_info_by_index(idx)["name"])
                except Exception:
                    pass
                logger.debug("Endpoint '%s' (%s): sin señal, se descarta", idx, name)
                try:
                    stream.close()
                except OSError:
                    pass
                continue
            self._stream = stream
            self._opened = idx
            logger.info("Micrófono en índice %d (host operable)", idx)
            return
        self._stream = self._try_open(None)  # respaldo: predeterminado
        self._opened = None

    def start(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        """Abre el flujo de micrófono y lanza el hilo que llena ``queue``."""
        self._queue = queue
        self._loop = loop
        self._opened: Optional[int] = None
        self._pick_stream()
        if self._stream is None:
            self._pa.terminate()
            raise OSError("Ningún micrófono disponible (¿sin dispositivo de entrada?)")
        device = self._pa.get_device_info_by_index(
            self._opened
            if self._opened is not None
            else int(self._pa.get_default_input_device_info()["index"])
        )
        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, name="mic-reader", daemon=True
        )
        self._thread.start()
        logger.info(
            "Micrófono activo (%.1f kHz, fragmentos de %d ms, '%s')",
            self._rate / 1000,
            int(self._chunk_frames * 1000 / self._rate),
            device.get("name"),
        )

    def _read_loop(self) -> None:
        """Bucle bloqueante: lee PCM del micrófono y alimenta la cola asíncrona."""
        while self._running:
            try:
                data = self._stream.read(self._chunk_frames, exception_on_overflow=False)
            except OSError:
                # La mayoría de tarjetas de sonido generan desbordamientos
                # puntuales; los ignoramos y seguimos con el siguiente bloque.
                logger.debug("Desbordamiento de búfer de audio ignorado")
                continue
            if self._running and self._loop is not None:
                try:
                    self._loop.call_soon_threadsafe(self._push_to_queue, data)
                except RuntimeError:
                    # El bucle asíncrono ya no acepta trabajo (apagado en curso).
                    pass

    def _push_to_queue(self, data: bytes) -> None:
        """Encola un fragmento desde el hilo del bucle; descarta si está llena."""
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            # La cola está llena (la red va más lenta que el micrófono):
            # descartamos el fragmento para mantener el tiempo real.
            pass

    def close(self) -> None:
        """Detiene el hilo del micrófono y libera los recursos de audio."""
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        self._pa.terminate()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Micrófono cerrado")