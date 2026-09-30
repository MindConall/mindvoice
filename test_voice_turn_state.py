"""Ciclo de vida del turno de voz: turnos PTT seguidos y turnos que fallan.

Estas pruebas son herméticas: no abren micrófono (PyAudio), ni red, ni disco.
Sustituyen ``MicrophoneCapture`` y la sesión del servidor por dobles que
registran exactamente qué manda el motor, y luego comprueban el protocolo del
turno de voz:

* cada turno abre (``activity_start``) y cierra (``activity_end`` +
  ``audio_stream_end``) su propia ventana de actividad;
* varios turnos consecutivos funcionan, que es el síntoma que reportaba el
  usuario ("solo me escucha el primer turno");
* un fallo de envío no deja el estado a medias;
* y, sobre todo, ``_voice_replay_tries`` se rearma al terminar cada turno, que
  es lo que evita que el watchdog se quede sin reenvío de voz para el resto de
  la sesión en cuanto lo usa una vez.
"""

import asyncio
import contextlib
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_assistant as la  # noqa: E402
from config import Settings  # noqa: E402
from hotkeys import HotkeyController  # noqa: E402


class _SesionFalsa:
    """Doble de la sesión Live: anota el protocolo, sin red."""

    def __init__(self) -> None:
        self.actividad: list = []  # 'start' / 'end' en orden de llegada
        self.audio = 0             # fragmentos de audio enviados
        self.cierres = 0           # audio_stream_end
        self.fallar_en_audio = None  # nº de audio a partir del cual se rompe

    async def send_realtime_input(
        self,
        audio=None,
        activity_start=None,
        activity_end=None,
        audio_stream_end: bool = False,
    ) -> None:
        if activity_start is not None:
            self.actividad.append("start")
            return
        if activity_end is not None:
            self.actividad.append("end")
            return
        if audio is not None:
            if (
                self.fallar_en_audio is not None
                and self.audio >= self.fallar_en_audio
            ):
                raise RuntimeError("sesión caída (simulada)")
            self.audio += 1
            return
        if audio_stream_end:
            self.cierres += 1


class _MicroFalso:
    """Sustituto de ``MicrophoneCapture``: inyecta voz alta, sin PyAudio."""

    instancias: list = []

    def __init__(self, rate=16000, chunk_ms=200, device_name=None) -> None:
        self.rate = int(rate)
        self.muestras = max(2, int(self.rate * int(chunk_ms) / 1000))
        self.cerrado = False
        self._cola = None
        self._tarea = None
        _MicroFalso.instancias.append(self)

    def start(self, chunk_queue, loop) -> None:
        self._cola = chunk_queue
        self._tarea = loop.create_task(self._alimento())

    async def _alimento(self) -> None:
        # Tono alterno de ±3000: RMS 3000, muy por encima de cualquier gate
        # (el techo del suelo es 80 → gate <= 160), así que el motor lo acepta.
        voz = struct.pack(
            "<%dh" % self.muestras, *([3000, -3000] * (self.muestras // 2))
        )
        while not self.cerrado:
            await self._cola.put(voz)
            await asyncio.sleep(0.005)

    def close(self) -> None:
        self.cerrado = True
        if self._tarea is not None:
            self._tarea.cancel()
            self._tarea = None


def _asistente():
    """``LiveAssistant`` sin memoria en disco (no toca archivos del usuario)."""
    with mock.patch.object(
        la.LiveAssistant, "_load_memory", lambda self: []
    ), mock.patch.object(
        la.LiveAssistant, "_load_permanent", lambda self: []
    ):
        return la.LiveAssistant(Settings(), HotkeyController())


class TestTurnoDeVoz(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    def setUp(self) -> None:
        _MicroFalso.instancias = []
        patcher = mock.patch.object(la, "MicrophoneCapture", _MicroFalso)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- utilidades ----------------------------------------------------
    async def _esperar(self, cond, mensaje: str, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        fin = loop.time() + timeout
        while loop.time() < fin:
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail(mensaje)

    def _preparar(self, sesion):
        asis = _asistente()
        # ``_run_session`` es quien crea el evento de voz y guarda la sesión;
        # aquí se reproducen a mano para no abrir la red.
        asis._voice_event = asyncio.Event()
        asis._session = sesion
        return asis

    # -- protocolo del turno -------------------------------------------
    async def test_tres_turnos_ptt_seguidos(self) -> None:
        """Tres turnos de voz encadenados: el 2º y el 3º también se atienden."""
        sesion = _SesionFalsa()
        asis = self._preparar(sesion)
        tarea = asyncio.create_task(asis._voice_loop(sesion, None, None))
        try:
            for turno in (1, 2, 3):
                with self.subTest(turno=turno):
                    # Flag de reenvío limpio al empezar cada turno.
                    self.assertEqual(
                        0,
                        asis._voice_replay_tries,
                        f"el contador quedó consumido al abrir el turno {turno}",
                    )
                    enviados = sesion.audio
                    asis._voice_event.set()
                    await self._esperar(
                        lambda: sesion.audio > enviados,
                        f"turno {turno}: no llegó audio al servidor",
                    )
                    # Suelte del PTT: cierra la ventana de actividad y el turno.
                    asis._voice_event.clear()
                    await self._esperar(
                        lambda: sesion.actividad.count("end") == turno,
                        f"turno {turno}: no se cerró la ventana de actividad",
                    )
                    self.assertEqual(
                        turno,
                        sesion.actividad.count("start"),
                        f"turno {turno}: falta abrir la ventana de actividad",
                    )
                    self.assertEqual(
                        turno,
                        sesion.cierres,
                        f"turno {turno}: falta audio_stream_end",
                    )
                    self.assertTrue(asis._awaiting_turn)
                    self.assertIsNotNone(
                        asis._voice_replay,
                        f"turno {turno}: no quedó audio guardado para el reenvío",
                    )
                    # El modelo contesta y el turno se cierra de verdad.
                    await asis._end_turn(sesion)
                    self.assertIsNone(asis._voice_replay)
                    self.assertEqual(0, asis._voice_replay_tries)
        finally:
            asis.quit_event.set()
            tarea.cancel()
            with contextlib.suppress(BaseException):
                await tarea

        # Un micrófono nuevo por turno, y todos cerrados (nada se queda abierto).
        self.assertEqual(3, len(_MicroFalso.instancias))
        self.assertTrue(all(m.cerrado for m in _MicroFalso.instancias))

    async def test_voz_tras_escuchar_tres_turnos_sigue_abriendo_actividad(self) -> None:
        """La ventana de actividad se abre en CADA turno, no solo en el primero."""
        sesion = _SesionFalsa()
        asis = self._preparar(sesion)
        tarea = asyncio.create_task(asis._voice_loop(sesion, None, None))
        try:
            for turno in (1, 2, 3):
                enviados = sesion.audio
                asis._voice_event.set()
                await self._esperar(
                    lambda: sesion.audio > enviados,
                    f"turno {turno}: no llegó audio",
                )
                asis._voice_event.clear()
                await self._esperar(
                    lambda: sesion.actividad.count("end") == turno,
                    f"turno {turno}: no cerró la actividad",
                )
                await asis._end_turn(sesion)
        finally:
            asis.quit_event.set()
            tarea.cancel()
            with contextlib.suppress(BaseException):
                await tarea
        self.assertEqual(3, sesion.actividad.count("start"))
        # El 'start' precede siempre a su 'end': nunca se solapan ventanas.
        for indice, marca in enumerate(sesion.actividad):
            self.assertEqual(
                "start" if indice % 2 == 0 else "end",
                marca,
                "las ventanas de actividad se solaparon",
            )

    # -- el flag de reenvío (causa raíz del bug) ------------------------
    async def test_contador_se_rearma_al_terminar_el_turno(self) -> None:
        """Tras un reenvío, el turno siguiente vuelve a tener su intento."""
        asis = self._preparar(_SesionFalsa())
        asis._voice_replay = b"\x00\x01" * 200
        asis._voice_replay_tries = 1
        await asis._end_turn(_SesionFalsa())
        self.assertIsNone(asis._voice_replay)
        self.assertEqual(0, asis._voice_replay_tries)
        # Precondición del watchdog (live_assistant.py:4967).
        self.assertLess(asis._voice_replay_tries, 1)

    async def test_contador_se_rearma_al_abandonar_el_turno(self) -> None:
        """``_clear_turn_state`` (turno abandonado) también rearma el contador."""
        asis = self._preparar(_SesionFalsa())
        asis._voice_replay_tries = 1
        asis._responding = True
        asis._awaiting_turn = True
        asis._clear_turn_state()
        self.assertEqual(0, asis._voice_replay_tries)
        self.assertFalse(asis._responding)
        self.assertFalse(asis._awaiting_turn)

    # -- turno simulado que falla --------------------------------------
    async def test_fallo_de_envio_no_deja_el_turno_a_medias(self) -> None:
        """Si el envío se rompe, el turno se limpia y la voz vuelve a servir."""
        sesion = _SesionFalsa()
        asis = self._preparar(sesion)
        sesion.fallar_en_audio = 0  # se rompe en el primer fragmento
        tarea = asyncio.create_task(asis._voice_loop(sesion, None, None))
        try:
            asis._voice_event.set()
            await self._esperar(
                lambda: not asis._voice_event.is_set(),
                "el fallo de envío no cerró el micrófono",
            )
            self.assertFalse(asis._awaiting_turn)
            self.assertEqual(0, asis._voice_replay_tries)
            self.assertFalse(asis._voice_sending)
            # La ventana de actividad SÍ se abrió (va antes del primer audio,
            # así que es lo correcto), pero el turno nunca llegó a cerrarse:
            # ni 'end' ni audio_stream_end.
            self.assertEqual(["start"], sesion.actividad)
            self.assertEqual(0, sesion.cierres)

            # Sesión sana: el siguiente turno se atiende con normalidad.
            sesion.fallar_en_audio = None
            asis._voice_event.set()
            await self._esperar(
                lambda: sesion.audio > 0, "tras el fallo no se vuelve a enviar audio"
            )
            asis._voice_event.clear()
            await self._esperar(
                lambda: sesion.actividad.count("end") == 1,
                "tras el fallo no se cierra la ventana de actividad",
            )
            self.assertEqual(1, sesion.cierres)
            self.assertTrue(asis._awaiting_turn)
        finally:
            asis.quit_event.set()
            tarea.cancel()
            with contextlib.suppress(BaseException):
                await tarea

    async def test_turno_rechazado_tambien_rearma_el_contador(self) -> None:
        """Un ``turn_complete`` de rechazo tampoco gasta el reenvío."""
        asis = self._preparar(_SesionFalsa())
        asis._voice_replay = b"\x00\x01" * 200
        asis._voice_replay_tries = 1
        await asis._end_turn(_SesionFalsa(), reason="RESPONSE_REJECTED")
        self.assertEqual(0, asis._voice_replay_tries)


if __name__ == "__main__":
    unittest.main()