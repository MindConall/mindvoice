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
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_assistant as la  # noqa: E402
import memory.base as la_mem  # noqa: E402
from config import Settings  # noqa: E402
from memory.graph_backend import GraphBackend  # noqa: E402


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


def _correr_orden_local(asistente, texto: str) -> _SesionFalsa:
    """Como ``_correr_orden``, pero sin sustituir ``_local_action``.

    La razón de existir: el resto de tests sustituyen ``_local_action`` para aislar
    el envío del turno, y eso arrastra a las órdenes de memoria, que se resuelven
    DENTRO de ``_local_action``. Con el sustituto puesto no se puede comprobar lo
    que de verdad importa aquí, que es que una orden resuelta en local no lance
    una búsqueda web.
    """

    sesion = _SesionFalsa()
    cola: asyncio.Queue = asyncio.Queue()
    cola.put_nowait(texto)
    asistente._command_queue = cola

    async def scenario():
        with mock.patch.object(
            la.LiveAssistant, "_describe_screen",
            new=mock.AsyncMock(return_value=""),
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


class _IndiceFalso:
    """Grafo de mentira con el que se comprueba el *giro* de la respuesta.

    No se usa un ``GraphBackend`` real porque lo que interesa aquí es qué nota
    llega al prompt y qué se borra de la memoria, no la calidad de la búsqueda.
    Y evita que un test escriba en la memoria real del usuario.

    Filtra por palabras como los de verdad, porque si no el test pasa por
    casualidad: un doble que devuelve lo que sea no prueba que el motor
    pregunte por el tema correcto.
    """

    def __init__(self) -> None:
        self.recuerdos = [
            la_mem.MemoryEntry(id="m1", text="mi color favorito es el azul"),
            la_mem.MemoryEntry(id="m2", text="mi perro se llama Nube"),
        ]
        self.borrados: list[str] = []

    @staticmethod
    def _comunes(texto: str, consulta: str) -> bool:
        palabras_a = {p for p in re.findall(r"[a-záéíóúñ]{3,}", consulta.lower())}
        palabras_b = {p for p in re.findall(r"[a-záéíóúñ]{3,}", texto.lower())}
        return bool(palabras_a & palabras_b)

    def buscar(self, texto, limite=8):
        return [e for e in self.recuerdos if self._comunes(e.text, texto)][:limite]

    def forget_matching(self, texto, limite=8):
        victims = [e for e in self.recuerdos if self._comunes(e.text, texto)]
        self.recuerdos = [e for e in self.recuerdos if e not in victims]
        self.borrados = [e.text for e in victims]
        return self.borrados

    def block(self, query="", **kwargs):
        return "\n".join(f"- {e.text}" for e in self.recuerdos)

    def en_disputa(self):
        return []

    def marcar_disputa(self, *a, **k):
        return None

    def reset(self):
        return None


def _asistente_con_indice(indice):
    a = _assistant()
    a._memoria_indice = indice
    # La memoria de trabajo real escribiría en el disco del usuario; aquí solo
    # se necesita que exista para que ``registrar_giro`` no se quede en el aire.
    a._memoria_trabajo = None
    return a


class TestMemoriaHablada(unittest.TestCase):
    """Las órdenes de memoria tienen que funcionar sin gastar un turno de modelo.

    "Olvida X" y "qué recuerdas de Y" se resuelven con la memoria local. Si
    llegaran al modelo, la app gastaría una ida y vuelta por red para leer algo
    que ya tiene en el disco, y además podría inventarse una respuesta distinta
    de la que tiene guardada.
    """

    def test_olvidar_un_tema_borra_solo_ese_tema(self) -> None:
        indice = _IndiceFalso()
        a = _asistente_con_indice(indice)
        nota = a._accion_memoria("olvida mi perro")
        self.assertIsNotNone(nota, "la orden de olvidar no se reconoció")
        self.assertEqual(len(indice.borrados), 1)
        self.assertIn("perro", indice.borrados[0])
        # Lo demás sigue ahí: olvidar es por temas, no un reset.
        self.assertTrue(any("azul" in e.text for e in indice.recuerdos))
        self.assertIn("perro", nota)

    def test_olvidar_lo_que_no_hay_lo_dice(self) -> None:
        indice = _IndiceFalso()
        a = _asistente_con_indice(indice)
        nota = a._accion_memoria("olvida mi piscina")
        self.assertEqual(indice.borrados, [])
        self.assertIn("nada", nota.lower())

    def test_preguntar_por_un_tema_usa_lo_guardado(self) -> None:
        indice = _IndiceFalso()
        a = _asistente_con_indice(indice)
        nota = a._accion_memoria("qué recuerdas de mi perro")
        self.assertIsNotNone(nota)
        self.assertIn("Nube", nota)

    def test_preguntar_de_donde_sale_un_dato(self) -> None:
        indice = _IndiceFalso()
        a = _asistente_con_indice(indice)
        nota = a._accion_memoria("de dónde sabes mi color favorito")
        self.assertIsNotNone(nota)
        self.assertIn("lo dijiste tú", nota)
        self.assertIn("azul", nota)
        # Y NO debe haber caído en la rama de "¿qué recuerdas?": son dos
        # preguntas distintas y con respuestas distintas.
        self.assertNotIn("Lo que recuerdo", nota)

    def test_una_orden_normal_no_se_confunde_con_la_memoria(self) -> None:
        """El riesgo de tocar la ruta de órdenes: "pon un temporizador" no es memoria."""
        indice = _IndiceFalso()
        a = _asistente_con_indice(indice)
        for frase in (
            "pon un temporizador de diez minutos",
            "busca el tiempo en Madrid",
            "abre el bloc de notas",
        ):
            with self.subTest(frase=frase):
                self.assertIsNone(a._accion_memoria(frase))
        self.assertEqual(indice.borrados, [])

    def test_olvidar_llega_al_modelo_solo_para_que_lo_diga(self) -> None:
        """El turno se manda, pero vacío de web: la memoria no necesita internet.

        Ojo con lo que se espera aquí: la orden NO se consume en silencio. Se
        ejecuta en local y la nota viaja al modelo para que lo diga en voz alta,
        que es lo que hacen ya el temporizador y el portapapeles, y sin eso el
        usuario no sabe si le han hecho caso. Lo que sí tiene que ser cero es la
        búsqueda web: leer la memoria no es cosa de internet.
        """
        indice = _IndiceFalso()
        a = _asistente_con_indice(indice)
        with mock.patch.object(
            la.LiveAssistant, "_web_search", new=mock.AsyncMock(return_value="")
        ) as web:
            sesion = _correr_orden_local(a, "olvida mi perro")
        self.assertTrue(indice.borrados, "la orden local no se ejecutó")
        self.assertEqual(len(sesion.turnos), 1, "el modelo no llegó a confirmar la orden")
        textos = " ".join(
            getattr(p, "text", "") or "" for p in sesion.turnos[0][0].parts
        )
        self.assertIn("olvidado", textos)
        self.assertFalse(web.called, "una orden de memoria launched una búsqueda web")

    def test_olvidar_no_lanza_la_busqueda_en_paralelo(self) -> None:
        """La detección de intención web se salta si hay integración local.

        Es lo que evita que "olvida mi perro" abra el navegador a buscar
        "perro" mientras se borra.
        """
        indice = _IndiceFalso()
        a = _asistente_con_indice(indice)
        a._settings.web_search_enabled = True
        with mock.patch.object(
            la.LiveAssistant, "_web_query", return_value="perro"
        ) as consulta:
            with mock.patch.object(
                la.LiveAssistant, "_web_search", new=mock.AsyncMock(return_value="")
            ):
                _correr_orden_local(a, "olvida mi perro")
        self.assertFalse(consulta.called, "se buscó en internet una orden local")


class TestChoqueEnElTurno(unittest.TestCase):
    """Si lo que el usuario dice choca con lo guardado, el turno lo sabe."""

    def _grafo_real(self):
        """Grafo de verdad en un directorio temporal.

        Aquí sí hace falta el de verdad: ``detectar_chocque`` comprueba que es un
        ``GraphBackend`` antes de tocar nada, y ese filtro es intencionado (no se
        puede marcar disputa en algo que no tiene nodos). Un doble cualquiera no
        probaría el camino que se ejecuta en producción.
        """
        import shutil
        import tempfile

        import shutil
        import tempfile

        self._tmp = tempfile.mkdtemp(prefix="mv-choque-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        g = GraphBackend(self._tmp)
        g.remember("Tengo un perro que se llama Nube")
        return g

    def test_el_choque_llega_en_el_mismo_turno(self) -> None:
        """Detectar el choque DESPUÉS de enviar el turno no sirve de nada.

        El recuerdo nuevo se guarda después de mandar el turno, así que la nota
        tiene que salir antes: si espera un turno, el modelo ya ha respondido
        con la versión vieja y el aviso llega tarde.
        """
        a = _asistente_con_indice(self._grafo_real())
        nota = a._detectar_disputa("Tengo un perro que se llama Bigotes")
        self.assertIn("AVISO DE MEMORIA", nota)
        self.assertIn("Nube", nota)
        self.assertIn("pregunta", nota.lower())

    def test_sin_choque_no_hay_nota(self) -> None:
        a = _asistente_con_indice(self._grafo_real())
        self.assertEqual(a._detectar_disputa("pon un temporizador de cinco minutos"), "")

    def test_la_memoria_rota_no_tumba_el_turno(self) -> None:
        class Roto:
            def buscar(self, *a, **k):
                raise RuntimeError("fichero corrupto")

        a = _asistente_con_indice(Roto())
        self.assertEqual(a._detectar_disputa("mi perro se llama Bigotes"), "")

    def test_sin_grafo_no_hay_nota_pero_el_turno_sigue(self) -> None:
        """Sin índice no hay disputa que detectar, y eso no puede romper el turno.

        Se degrada en silencio a propósito: el aviso es una mejora, no una
        condición para poder hablar. Lo que no puede pasar es una excepción.
        """
        a = _asistente_con_indice(None)
        self.assertEqual(a._detectar_disputa("Tengo un perro que se llama Bigotes"), "")

    def test_una_correccion_cierra_el_turno_anterior_como_corregido(self) -> None:
        """``corrected`` es la señal que más cambia el comportamiento después."""
        a = _assistant()
        registradas = []

        async def scenario():
            a._pregunta_turno = "mi color favorito"
            a._ultima_pregunta = "mi color favorito"
            with mock.patch.object(
                a,
                "registrar_giro",
                side_effect=lambda p, r="useful", correccion="": registradas.append((p, r)),
            ):
                a._marcar_correccion("no, es el verde")

        _correr(scenario())
        self.assertEqual(len(registradas), 1, "la corrección no llegó a la reflexión")
        self.assertEqual(registradas[0][1], "corrected")

    def test_una_frase_normal_no_cierra_nada_como_corregido(self) -> None:
        a = _assistant()
        registradas = []

        async def scenario():
            a._pregunta_turno = "qué hora es"
            a._ultima_pregunta = "qué hora es"
            with mock.patch.object(
                a,
                "registrar_giro",
                side_effect=lambda p, r="useful", correccion="": registradas.append((p, r)),
            ):
                a._marcar_correccion("mañana tengo una reunión")

        _correr(scenario())
        self.assertEqual(registradas, [])

    def test_sin_turno_anterior_no_hay_que_corregir(self) -> None:
        a = _assistant()
        registradas = []

        async def scenario():
            with mock.patch.object(
                a, "registrar_giro", side_effect=lambda *a, **k: registradas.append(a)
            ):
                a._marcar_correccion("no, es el verde")

        _correr(scenario())
        self.assertEqual(registradas, [])

    def test_el_turno_cerrado_guarda_la_pregunta_para_poder_corregirla(self) -> None:
        """``_pregunta_turno`` se vacía al cerrar; sin copia no hay con qué comparar.

        Es el detalle que hace que la corrección funcione o no: si al vaciar la
        pregunta se pierde, el siguiente turno no tiene forma de saber a cuál
        de los anteriores se refiere el "no, es el verde".
        """
        a = _assistant()

        async def scenario():
            a._pregunta_turno = "mi color favorito"
            with mock.patch.object(a, "registrar_giro"):
                await a._end_turn(None, None)

        _correr(scenario())
        self.assertEqual(a._pregunta_turno, "")
        self.assertEqual(a._ultima_pregunta, "mi color favorito")


if __name__ == "__main__":
    unittest.main()