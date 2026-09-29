"""Reproducción de audio de baja latencia para la voz de Gemini.

Gemini devuelve PCM de 24 kHz (16 bits, mono). Su reproducción se encadena a
través de una cola acotada ``queue.Queue`` procesada por un hilo dedicado con
PyAudio en modo bloqueante, lo que garantiza un flujo continuo y sin
cortes aunque el bucle asíncrono se ocupe de enviar y recibir datos.

La cola es **acotada** (``_MAX_QUEUED_CHUNKS``) pero con un tope MUY generoso:
absorbe hasta ~96 s de audio, de modo que una respuesta larga (las ráfagas de
Gemini llegan más rápido de lo que el altavoz reproduce) se encola por
completo y se oye entera. Solo si el dispositivo de salida va realmente más
lento que el envío se usa el criterio **drop-oldest** (skip-ahead): se descarta
el fragmento MÁS ANTIGUO ya leído para dar sitio al recién llegado, así la voz
salta a la parte más reciente de la frase y NUNCA se corta la cola de la
respuesta (cortarla es lo que colgaba la voz). El final de cada turno siempre
se reproduce.

Incluye ``flush()`` para vaciar la cola al instante cuando el modelo es
interrumpido a mitad de una frase, ``reopen()`` para cambiar el dispositivo
de salida en caliente y ``set_volume()`` para ajustar el volumen del PCM.
"""

import array
import logging
import queue
import threading
import time
import unicodedata
from typing import List, Optional

import pyaudio

logger = logging.getLogger(__name__)

# Número máximo de fragmentos PCM en espera de reproducción. Acotar la cola
# evita que un dispositivo lento acumule retraso sin fin, pero un tope PEQUEÑO
# era el fallo: con 48 (~9,6 s) la ráfaga de las respuestas largas desbordaba
# la cola y se descartaba el audio RECIÉN LLEGADO, cortando el FINAL de las
# frases: la voz parecía "colgarse" a mitad del turno. Con 480 (~96 s) la cola
# absorbe cualquier respuesta entera de Gemini; y si aun así se llena, el
# descarte es drop-oldest (skip-ahead, ver ``write``): se salta audio ya leído
# para seguir con lo más reciente y terminar la frase, nunca se corta la cola.
# Como cada turno comienza con un ``flush()``, el búfer nunca arrastra audio a
# la frase siguiente.
_MAX_QUEUED_CHUNKS = 480

# Si la cola supera este nivel se registra una advertencia (con enfriamiento de
# 5 s): síntoma de que el dispositivo de salida procesa más lento que la red.
_WARN_QUEUED_CHUNKS = 160
_WARN_COOLDOWN_S = 5.0


def list_output_devices() -> List[dict]:
    """Devuelve los altavoces disponibles: índice, nombre, canales y host API."""
    pa = pyaudio.PyAudio()
    try:
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            channels = int(info.get("maxOutputChannels") or 0)
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


def _match_output_index(device_name: Optional[str]) -> Optional[int]:
    """Devuelve el índice del altavoz cuyo nombre coincide (o ``None``)."""
    if not device_name:
        return None
    wanted = unicodedata.normalize("NFC", device_name.lower())
    for device in list_output_devices():
        if unicodedata.normalize("NFC", device["name"].lower()) == wanted:
            return device["index"]
    return None


class AudioPlayer:
    """Reproductor de PCM en un hilo propio con cola de búfer."""

    def __init__(
        self,
        rate: int = 24000,
        channels: int = 1,
        device_name: Optional[str] = None,
    ) -> None:
        self._rate = rate
        self._channels = channels
        self._device_name = device_name
        # Dispositivo REALMENTE en uso: puede diferir del elegido si se tuvo que
        # caer al predeterminado (ver ``_open_stream``).
        self._active_device: Optional[str] = None
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=_MAX_QUEUED_CHUNKS)
        self._overruns = 0
        self._last_warn_ts = 0.0
        self._stop = threading.Event()
        self._pa = pyaudio.PyAudio()
        self._volume = 1.0
        self._stream = self._open_stream()
        self._thread = threading.Thread(
            target=self._play_loop, name="audio-player", daemon=True
        )
        self._thread.start()
        logger.info("Reproductor de audio activo (%.1f kHz)", rate / 1000)

    def _open_stream(self):
        """Abre el flujo de salida (predeterminado o el dispositivo elegido).

        Si el dispositivo elegido no se puede abrir se CAE AL PREDETERMINADO
        en vez de propagar el error. Una preferencia de audio no puede tumbar
        el motor: si la apertura falla, ``live_assistant`` aborta con
        ``sin_audio`` y el watchdog reinicia en bucle un motor que no puede
        tener audio nunca. Casos reales medidos en este equipo:

        - WDM-KS (``Auriculares ()``, ``Speakers``) -> "Unanticipated host
          error" (-9999) a CUALQUIER frecuencia, incluso la nativa.
        - WASAPI -> "Invalid sample rate" (-9997) a 24000 Hz, que es la
          frecuencia nativa de Gemini; solo abre a 48000.

        A 24 kHz solo son utilizables MME y DirectSound. Si tampoco se puede
        abrir el predeterminado, el error se propaga (no hay audio posible).
        """
        kwargs = dict(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._rate,
            output=True,
        )
        index = _match_output_index(self._device_name)
        if index is not None:
            try:
                stream = self._pa.open(output_device_index=index, **kwargs)
            except Exception as exc:  # noqa: BLE001 - se reintenta sin indice
                logger.warning(
                    "No se pudo abrir la salida elegida (%s): %s. Se usa la "
                    "predeterminada de Windows.",
                    self._device_name,
                    exc,
                )
            else:
                self._active_device = self._device_name
                return stream
        self._active_device = None
        return self._pa.open(**kwargs)

    def reopen(
        self,
        device_name: Optional[str] = None,
    ) -> None:
        """Reabre el reproductor con otro dispositivo de salida (en caliente).

        Detiene el hilo de reproducción actual, cierra el stream y abre uno
        nuevo con el dispositivo solicitado (o el predeterminado). El volumen
        configurado se conserva.
        """
        if self._stop.is_set():
            return
        old_thread = self._thread
        self._stop.set()
        self.flush()
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=2.0)
        try:
            self._stream.close()
        except OSError:
            pass
        self._device_name = device_name
        self._stream = self._open_stream()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._play_loop, name="audio-player", daemon=True
        )
        self._thread.start()
        logger.info(
            "Reproductor reiniciado con salida: %s",
            self._active_device or "predeterminada de Windows",
        )

    def restart(self) -> None:
        """Reabre el stream con el MISMO dispositivo (hard reset de salida).

        Usado por el watchdog cuando varios turnos consecutivos se estancan:
        recrea el flujo de PyAudio por si el altavoz/tarjeta de sonido quedó
        retenido en un estado erróneo. El volumen configurado se conserva.
        """
        self.reopen(self._device_name)

    def _play_loop(self) -> None:
        """Bucle bloqueante que escribe cada fragmento PCM en el altavoz."""
        while not self._stop.is_set():
            try:
                data = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._stream.write(self._apply_gain(data))
            except OSError:
                logger.warning("No se pudo escribir audio de salida")
                continue
            now = time.monotonic()
            if self._queue.qsize() > _WARN_QUEUED_CHUNKS and (
                now - self._last_warn_ts
            ) >= _WARN_COOLDOWN_S:
                self._last_warn_ts = now
                logger.warning(
                    "Cola de reproducción acumulada (%d fragmentos): la salida "
                    "va más lenta que lo que envía Gemini",
                    self._queue.qsize(),
                )

    def _apply_gain(self, data: bytes) -> bytes:
        """Aplica el volumen al PCM 16 bits (no-op si está al 100%)."""
        volume = self._volume
        sample_bytes = len(data) - (len(data) % 2)
        if volume >= 0.999 and volume <= 1.001 or sample_bytes == 0:
            return data
        samples = array.array("h")
        samples.frombytes(data[:sample_bytes])
        volume = max(0.0, min(1.5, volume))
        for i in range(len(samples)):
            samples[i] = max(-32768, min(32767, int(samples[i] * volume)))
        tail = data[sample_bytes:]
        return samples.tobytes() + tail

    def write(self, data: bytes) -> None:
        """Encola un fragmento PCM para reproducirlo lo antes posible.

        La cola es acotada y de descarte del MÁS ANTIGUO (skip-ahead): si está
        llena se suelta el fragmento ya leído para dar sitio al recién llegado,
        de forma no bloqueante (este método corre en el bucle asyncio y nunca
        debe esperar a que el hilo de reproducción consuma). Descartar el
        nuevo cortaba SIEMPRE la cola/final de la frase (la voz se oía cortada
        o "colgada"); con skip-ahead la voz avanza hacia la parte más reciente
        y la respuesta termina, como mucho, saltándose audio intermedio.
        """
        if not data:
            return
        q = self._queue
        if q.full():
            # Cola llena: se descarta el fragmento MÁS ANTIGUO (audio ya leído
            # o irrecuperable por el retraso) en favor del más reciente. El
            # tope grande hace esto rarísimo; es la red de seguridad para que
            # una ráfaga enorme nunca deje la frase sin final.
            self._overruns += 1
            try:
                q.get_nowait()
            except queue.Empty:
                return
        try:
            q.put_nowait(data)
        except queue.Full:  # carrera residual: se pierde este fragmento
            self._overruns += 1

    @property
    def overrun_count(self) -> int:
        """Salto(s) por cola llena (skip-ahead; diagnóstico de rendimiento)."""
        return self._overruns

    def flush(self) -> None:
        """Vacía el búfer de reproducción (útil al interrumpir al modelo)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def set_volume(self, volume: float) -> None:
        """Ajusta el volumen del PCM reproducido (0.0-1.5, seguro por hilos)."""
        self._volume = max(0.0, min(1.5, float(volume)))

    def close(self) -> None:
        """Detiene el hilo de reproducción y libera los recursos de audio."""
        self._stop.set()
        self.flush()
        try:
            self._stream.stop_stream()
            self._stream.close()
        except OSError:
            pass
        self._pa.terminate()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Reproductor de audio cerrado")