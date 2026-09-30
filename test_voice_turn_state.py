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


class _MicroQueSeCalla(_MicroFalso):
    """Habla un momento y luego se calla: reproduce "pulsas, hablas y esperas".

    Los trozos mudos van a cero, muy por debajo de cualquier gate, así que el
    motor los descarta y ``last_loud_ts`` deja de avanzar. Es la escena que el
    usuario reportaba: turno abierto, habla, y nada que cierre el turno.
    """

    def __init__(self, trozos_con_voz: int = 3, **kw) -> None:
        super().__init__(**kw)
        self.trozos_con_voz = int(trozos_con_voz)

    async def _alimento(self) -> None:
        voz = struct.pack(
            "<%dh" % self.muestras, *([3000, -3000] * (self.muestras // 2))
        )
        mudo = struct.pack("<%dh" % self.muestras, *([0] * self.muestras))
        while not self.cerrado:
            for _ in range(max(1, self.trozos_con_voz)):
                if self.cerrado:
                    return
                await self._cola.put(voz)
                await asyncio.sleep(0.005)
            self.trozos_con_voz = 0
            while not self.cerrado:
                await self._cola.put(mudo)
                await asyncio.sleep(0.005)


def _asistente(ajustes=None):
    """``LiveAssistant`` sin memoria en disco (no toca archivos del usuario)."""
    with mock.patch.object(
        la.LiveAssistant, "_load_memory", lambda self: []
    ), mock.patch.object(
        la.LiveAssistant, "_load_permanent", lambda self: []
    ):
        return la.LiveAssistant(ajustes or Settings(), HotkeyController())


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

    # -- cierre por silencio (regresión del 30/09) ------------------------
    async def test_en_toggle_el_turno_se_cierra_al_callarse(self) -> None:
        """Pulsas, hablas, te callas: el turno se envía SIN segunda pulsación.

        Regresión del bug reportado: con ``voice_manual_vad`` el segmentador de
        silencio estaba desactivado siempre, así que en modo alterno el turno
        solo lo cerraba la segunda pulsación. El usuario pulsaba una vez, veía
        el micrófono abierto, hablaba y no recibía respuesta nunca; el HUD
        además decía "suelta el botón", que en modo alterno no hace nada.
        """
        sesion = _SesionFalsa()
        asis = self._preparar(sesion)
        # Umbral corto para que la prueba no tarde 2,5 s.
        asis._settings.voice_toggle_silence = 0.1
        # Turno abierto por el usuario (pulsación), no escucha continua.
        asis._voice_toggle = True
        asis._voice_event.set()
        with mock.patch.object(la, "MicrophoneCapture", _MicroQueSeCalla):
            tarea = asyncio.create_task(asis._voice_loop(sesion, None, None))
            try:
                # Nadie toca ``_voice_event``: el cierre tiene que venir solo.
                # ``audio_stream_end`` es lo que entrega el turno al modelo; el
                # ``activity_end`` llega después, al salir del bucle, porque el
                # micrófono sigue abierto escuchando la siguiente frase.
                await self._esperar(
                    lambda: sesion.cierres == 1,
                    "el turno NO se envió solo al callarse: hace falta una "
                    "segunda pulsación para que salga algo",
                    timeout=8.0,
                )
            finally:
                asis.quit_event.set()
                tarea.cancel()
                with contextlib.suppress(BaseException):
                    await tarea
        self.assertTrue(asis._awaiting_turn, "el turno no quedó esperando respuesta")
        self.assertIsNotNone(asis._voice_replay)

    async def test_en_escucha_continua_el_silencio_no_cierra(self) -> None:
        """Escucha continua + VAD manual: hablar indefinido sigue siendo legal.

        El auto-cierre es solo para turnos que abrió una pulsación. Si se
        extendiera a la escucha continua, una pausa al pensar cortaría la frase
        a mitad, que es justo lo que el VAD manual existe para evitar.
        """
        sesion = _SesionFalsa()
        asis = self._preparar(sesion)
        asis._settings.voice_toggle_silence = 0.1
        asis._voice_toggle = False  # escucha continua
        asis._voice_event.set()
        with mock.patch.object(la, "MicrophoneCapture", _MicroQueSeCalla):
            tarea = asyncio.create_task(asis._voice_loop(sesion, None, None))
            try:
                await self._esperar(lambda: sesion.audio > 0, "no llegó audio")
                # Silencio prolongado: el turno debe seguir abierto.
                await asyncio.sleep(0.6)
                # La ventana se abre al empezar el turno; lo que no debe
                # aparecer es el 'end' que cerraría la frase.
                self.assertEqual(
                    0,
                    sesion.actividad.count("end"),
                    "la escucha continua se cerró sola: una pausa al pensar "
                    "cortaría la frase",
                )
                self.assertEqual(0, sesion.cierres)
                self.assertTrue(asis._voice_event.is_set())
            finally:
                asis.quit_event.set()
                tarea.cancel()
                with contextlib.suppress(BaseException):
                    await tarea

    async def test_el_silencio_por_defecto_no_es_tan_brusco(self) -> None:
        """El umbral de toggle (2,5 s) es bastante más ancho que el de 1,0 s.

        El segmentador de 1 s del VAD automático se descartó porque cortaba
        frases a mitad al pensar. El valor por defecto de toggle no puede ser
        ese: tiene que dar margen a una pausa natural.
        """
        ajustes = Settings()
        self.assertTrue(ajustes.voice_manual_vad)
        self.assertGreater(ajustes.voice_toggle_silence, 1.5)
        self.assertLess(ajustes.voice_toggle_silence, 6.0)

    async def test_silencio_cero_devuelve_el_dos_pulsaciones(self) -> None:
        """``voice_toggle_silence = 0`` deja el cierre solo en la 2ª pulsación."""
        sesion = _SesionFalsa()
        asis = self._preparar(sesion)
        asis._settings.voice_toggle_silence = 0.0
        asis._voice_toggle = True
        asis._voice_event.set()
        with mock.patch.object(la, "MicrophoneCapture", _MicroQueSeCalla):
            tarea = asyncio.create_task(asis._voice_loop(sesion, None, None))
            try:
                await self._esperar(lambda: sesion.audio > 0, "no llegó audio")
                await asyncio.sleep(0.6)
                self.assertEqual(
                    0,
                    sesion.actividad.count("end"),
                    "con silencio=0 el turno NO debe cerrarse solo",
                )
                self.assertEqual(0, sesion.cierres)
                self.assertTrue(asis._voice_event.is_set())
            finally:
                asis.quit_event.set()
                tarea.cancel()
                with contextlib.suppress(BaseException):
                    await tarea


class TestAvisoDelMicrofono(unittest.TestCase):
    """El HUD tiene que decir qué hacer para cerrar el turno.

    Importa ``overlay``, que arrastra PyQt6, así que ``QT_QPA_PLATFORM`` se pone
    a ``offscreen`` antes de nada: sin ventana, sin tocar la pantalla.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import overlay  # noqa: PLC0415 - necesita el env de arriba primero

        cls._hint = staticmethod(overlay.OverlayHud._voice_hint)

    @staticmethod
    def _settings(modo: str, silencio: float) -> Settings:
        s = Settings()
        s.mute_mode = modo
        s.voice_toggle_silence = silencio
        return s

    def _texto(self, modo: str, silencio: float) -> str:
        return self._hint(type("X", (), {"_settings": self._settings(modo, silencio)})())

    def test_en_alterno_no_dice_suelta_el_boton(self) -> None:
        """En modo alterno, soltar NO cierra el turno: el aviso era falso.

        Era la causa de la confusión del usuario: el HUD ponía "suelta el
        botón para responder" y, si pulsaba una vez y esperaba, no pasaba nada
        sin explicación. En alterno el botón se suelta sin efecto.
        """
        texto = self._texto("toggle", 2.5)
        self.assertNotIn(
            "suelta el bot",
            texto.lower(),
            "en modo alterno soltar la tecla no cierra el turno",
        )
        self.assertIn("Escuchando", texto)

    def test_en_alterno_explica_como_se_manda(self) -> None:
        """Con auto-cierre: dice que se manda sola y cuánto tarda."""
        texto = self._texto("toggle", 2.5)
        self.assertIn("2.5", texto)
        self.assertIn("pulsa", texto.lower())

    def test_sin_auto_cierre_pide_la_segunda_pulsacion(self) -> None:
        """Con ``voice_toggle_silence = 0`` solo cierra la 2ª pulsación."""
        texto = self._texto("toggle", 0.0)
        self.assertIn("pulsa de nuevo", texto.lower())
        # Sin auto-cierre no debe prometer que se manda sola.
        self.assertNotIn("sola", texto.lower())

    def test_en_push_to_talk_sigue_diciendo_suelta(self) -> None:
        """En PTT soltar sí cierra: el aviso original era correcto ahí."""
        texto = self._texto("push_to_talk", 2.5)
        self.assertIn("suelta", texto.lower())


if __name__ == "__main__":
    unittest.main()