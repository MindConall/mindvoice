"""Regresión del NameError que la Fase 0 introdujo en el envío de órdenes.

El fallo: ``_perf.note_prompt`` mide ``note`` y ``web_note`` al final del turno,
pero esas dos variables solo se asignaban dentro de un ``if`` (descripción de
pantalla no vacía / búsqueda web realizada). En el camino normal de una orden de
texto —sin visión y sin búsqueda— no existían, y la lectura lanzaba NameError.

Lo que hacía eso especialmente malo: el NameError caía en el ``except`` del
propio turno, que registra "envío interrumpido" y hace ``continue``. La orden
del usuario no se enviaba nunca y no había ninguna señal visible: el HUD se
quedaba como si nada. Es decir, escribir en el HUD dejaba de funcionar, en
silencio.

Estas pruebas ejecutan el bucle real de órdenes de texto y comprueban que la
orden llega al servidor. No leen el código: lo ejecutan.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_assistant as la  # noqa: E402
from config import Settings  # noqa: E402


class _SesionFalsa:
    """Doble de la sesión Live: guarda lo que se le manda, sin red."""

    def __init__(self) -> None:
        self.turnos: list = []

    async def send_client_content(self, turns=None, turn_complete=False):
        self.turnos.append(turns)

    async def send_realtime_input(self, **kwargs):
        return None

    async def close(self):
        return None


def _assistant(**ajustes):
    """Assistant mínimo, sin micrófono ni red, con la visión apagada."""
    s = Settings()
    s.screen_enabled = False
    s.web_search_enabled = False
    s.web_smart_detect = False
    for k, v in ajustes.items():
        setattr(s, k, v)
    with mock.patch.object(la, "MicrophoneCapture"), \
         mock.patch.object(la, "AudioPlayer"):
        return la.LiveAssistant(
            settings=s,
            hotkeys=None,
            on_text=lambda t: None,
            on_meta=lambda t: None,
        )


def _correr(coro):
    """Ejecuta una corrutina en un event loop propio y lo CIERRA.

    La forma directa, ``asyncio.new_event_loop().run_until_complete(...)``,
    deja el loop abierto y Python avisa con ``ResourceWarning: unclosed event
    loop`` al recogerlos. Parece inocuo, pero un test que ensucia la salida de
    todos los demás acaba normalizándose: es la vía por la que un fallo real
    deja de verse.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def _correr_orden(asistente, texto: str) -> _SesionFalsa:
    """Ejecuta un turno de texto completo y devuelve la sesión registrada.

    ``quit_event`` NO se fija antes: el bucle lo comprueba al entrar (L2262) y
    otra vez nada más sacar la orden (L2264), así que puesto antes no llega a
    procesar nada. Se fija con un temporizador de un solo disparo que se dispara
    después de que la orden ya haya salido de la cola.
    """
    sesion = _SesionFalsa()
    # El bucle lee ``self._command_queue``, no una cola local: hay que
    # inyectarle la suya o falla con AttributeError sobre None.
    cola: asyncio.Queue = asyncio.Queue()
    cola.put_nowait(texto)
    asistente._command_queue = cola

    async def scenario():
        with mock.patch.object(
            la.LiveAssistant, "_describe_screen",
            new=mock.AsyncMock(return_value=""),
        ), mock.patch.object(
            la.LiveAssistant, "_local_action", return_value=None
        ), mock.patch.object(
            la.LiveAssistant, "_build_memory_block", return_value=""
        ), mock.patch.object(
            la.LiveAssistant, "_remember"
        ), mock.patch.object(
            la.LiveAssistant, "_set_state"
        ), mock.patch.object(
            la.LiveAssistant, "_stamp_turn_begin"
        ):
            # Para detener el bucle no basta con poner quit_event: se queda
            # bloqueado en `await queue.get()`. Hay que poner la bandera Y
            # despertar la cola con un elemento extra; el bucle lo saca, ve la
            # bandera y sale.
            def _parar():
                asistente.quit_event.set()
                cola.put_nowait("__parar__")

            asyncio.get_running_loop().call_later(0.3, _parar)
            await asyncio.wait_for(
                asistente._send_command_loop(sesion, None, None),
                timeout=10,
            )

    _correr(scenario())
    return sesion


class TestOrdenDeTextoSinContexto(unittest.TestCase):
    """La orden de texto tiene que llegar aunque no haya nada que anotar."""

    def test_una_orden_sin_vision_ni_web_se_envia(self) -> None:
        """Este es el camino de la everyday: escribir y que conteste."""
        a = _assistant()
        sesion = _correr_orden(a, "¿qué hora es?")
        self.assertEqual(
            len(sesion.turnos),
            1,
            "la orden no llegó al servidor: se perdió en el camino",
        )

    def test_la_orden_llega_con_su_texto(self) -> None:
        a = _assistant()
        sesion = _correr_orden(a, "hola MindVoice")
        self.assertTrue(sesion.turnos, "no se envió ninguna orden")
        partes = sesion.turnos[0][0].parts
        textos = " ".join(getattr(p, "text", "") or "" for p in partes)
        self.assertIn("hola MindVoice", textos)

    def test_tres_ordenes_seguidas_todas_llegan(self) -> None:
        """Con valores que persisten entre iteraciones el bug se escondía."""
        a = _assistant()
        sesion = _SesionFalsa()
        cola: asyncio.Queue = asyncio.Queue()
        for t in ("primera", "segunda", "tercera"):
            cola.put_nowait(t)
        a._command_queue = cola

        async def scenario():
            with mock.patch.object(
                la.LiveAssistant, "_describe_screen",
                new=mock.AsyncMock(return_value=""),
            ), mock.patch.object(
                la.LiveAssistant, "_local_action", return_value=None
            ), mock.patch.object(
                la.LiveAssistant, "_build_memory_block", return_value=""
            ), mock.patch.object(
                la.LiveAssistant, "_remember"
            ), mock.patch.object(
                la.LiveAssistant, "_set_state"
            ), mock.patch.object(
                la.LiveAssistant, "_stamp_turn_begin"
            ):
                def _parar():
                    a.quit_event.set()
                    cola.put_nowait("__parar__")

                asyncio.get_running_loop().call_later(0.3, _parar)
                await asyncio.wait_for(
                    a._send_command_loop(sesion, None, None), timeout=10
                )

        _correr(scenario())
        self.assertEqual(
            len(sesion.turnos), 3,
            "alguna de las tres órdenes se perdió",
        )

    def test_la_instrumentacion_no_rompe_el_turno(self) -> None:
        """La métrica es un adorno: si falla, el turno va igual."""
        a = _assistant()
        with mock.patch.object(
            la._perf, "note_prompt", side_effect=RuntimeError("boom")
        ):
            sesion = _correr_orden(a, "sigue funcionando")
        self.assertTrue(
            sesion.turnos,
            "una métrica rota tumbó el turno: la instrumentación no puede "
            "ser lo que corta la conversación",
        )


class TestMemoriaDelTurno(unittest.TestCase):
    """La memoria relevante a la pregunta tiene que llegar al turno."""

    def _asistente_con_grafo(self):
        """Assistant con el bloque de memoria False en vez de grafo real."""
        a = _assistant()
        return a

    def test_la_memoria_relevante_se_anade_al_turno(self) -> None:
        """El bloque por pregunta se inyecta como parte del turno."""
        a = self._asistente_con_grafo()
        sesion = _SesionFalsa()
        cola: asyncio.Queue = asyncio.Queue()
        cola.put_nowait("¿qué hice con el proyecto?")
        a._command_queue = cola

        async def scenario():
            with mock.patch.object(
                la.LiveAssistant, "_local_action", return_value=None
            ), mock.patch.object(
                la.LiveAssistant, "_remember"
            ), mock.patch.object(
                la.LiveAssistant, "_set_state"
            ), mock.patch.object(
                la.LiveAssistant, "_stamp_turn_begin"
            ), mock.patch.object(
                la.LiveAssistant,
                "_build_memory_block",
                side_effect=lambda q="": (
                    "recuerdos del proyecto" if q else "x" * 400
                ),
            ):
                def _parar():
                    a.quit_event.set()
                    cola.put_nowait("__parar__")

                asyncio.get_running_loop().call_later(0.3, _parar)
                await asyncio.wait_for(
                    a._send_command_loop(sesion, None, None), timeout=10
                )

        _correr(scenario())
        textos = " ".join(
            getattr(p, "text", "") or "" for p in sesion.turnos[0][0].parts
        )
        self.assertIn(
            "recuerdos del proyecto",
            textos,
            "la memoria relevante a la pregunta no llegó al turno",
        )

    def test_no_se_anade_si_es_todo_la_memoria(self) -> None:
        """Si no se recorta nada, no se repite el bloque entero."""
        a = _assistant()
        sesion = _SesionFalsa()
        cola: asyncio.Queue = asyncio.Queue()
        cola.put_nowait("hola")
        a._command_queue = cola

        async def scenario():
            with mock.patch.object(
                la.LiveAssistant, "_local_action", return_value=None
            ), mock.patch.object(
                la.LiveAssistant, "_remember"
            ), mock.patch.object(
                la.LiveAssistant, "_set_state"
            ), mock.patch.object(
                la.LiveAssistant, "_stamp_turn_begin"
            ), mock.patch.object(
                la.LiveAssistant, "_build_memory_block", return_value="igual"
            ):
                def _parar():
                    a.quit_event.set()
                    cola.put_nowait("__parar__")

                asyncio.get_running_loop().call_later(0.3, _parar)
                await asyncio.wait_for(
                    a._send_command_loop(sesion, None, None), timeout=10
                )

        _correr(scenario())
        textos = " ".join(
            getattr(p, "text", "") or "" for p in sesion.turnos[0][0].parts
        )
        self.assertNotIn("Recuerdos relacionados", textos)

    def test_el_turno_se_registra_al_cerrarse(self) -> None:
        """``registrar_giro`` tiene que llamarse al terminar el turno."""
        a = _assistant()
        registradas = []

        async def scenario():
            a._pregunta_turno = "¿me recuerdas el dog's name?"
            with mock.patch.object(
                a, "registrar_giro",
                side_effect=lambda p, r="useful", c="": registradas.append((p, r)),
            ):
                await a._end_turn(None, None)

        _correr(scenario())
        self.assertTrue(registradas, "el turno no se registró en la memoria de trabajo")
        self.assertIn("dog", registradas[0][0])

    def test_tras_registrar_el_turno_se_limpia_la_pregunta(self) -> None:
        """La pregunta de un turno no puede quedarse para el siguiente."""
        a = _assistant()

        async def scenario():
            a._pregunta_turno = "primera"
            with mock.patch.object(a, "registrar_giro"):
                await a._end_turn(None, None)

        _correr(scenario())
        self.assertEqual(a._pregunta_turno, "")


if __name__ == "__main__":
    unittest.main()