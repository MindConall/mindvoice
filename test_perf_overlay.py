"""Tests del panel de diagnóstico del HUD (Fase 4).

La Fase 0 dejó la instrumentación escrita y probada, pero nadie podía ver las
cifras sin relanzar la app con ``MINDVOICE_PERF`` y abrir el JSON. La Fase 4 lo
cierra con un panel dentro del HUD (Ctrl+Shift+D) y, sobre todo, con la
condición que la hace aceptable: **cerrado no cuesta nada**. No hay timer
corriendo, no hay hueco reservado en el layout y no se enciende nada.

Los medidores se comprueban sin Qt (contrato puro) y el panel en sí se
comprueba con un HUD real, porque lo que importa aquí es que el ciclo de
abrir/cerrar no deje nada colgando.
"""

import ctypes
import importlib
import os
import pathlib
import re
import unittest
from unittest import mock

RAIZ = pathlib.Path(__file__).resolve().parent


class RelojFalso:
    """Un ``perf_counter`` que solo avanza cuando se le dice.

    Las métricas de fluidez son razones y ventanas de tiempo: con el reloj real
    dos llamadas seguidas dan dos cifras distintas y no hay nada que comparar.
    """

    def __init__(self) -> None:
        self.t = 1000.0

    def avanza(self, segundos: float) -> None:
        self.t += segundos

    def __call__(self) -> float:
        return self.t


# ======================================================================
# perf_instr: encendido en caliente
# ======================================================================
class TestEncendidoEnCaliente(unittest.TestCase):
    """``PERF.encender()`` es lo que cumple la promesa del módulo."""

    def setUp(self) -> None:
        for var in ("MINDVOICE_PERF", "MINDVOICE_PERF_OUT"):
            os.environ.pop(var, None)
        import perf_instr

        self.mod = importlib.reload(perf_instr)
        self.p = self.mod.Perf()

    def tearDown(self) -> None:
        # La selección forzada es estado de módulo: si un test la deja puesta,
        # el singleton del proceso se queda midiendo sin querer.
        self.p.apagar()
        os.environ.pop("MINDVOICE_PERF", None)

    def test_enciende_sin_variable_de_entorno(self) -> None:
        """Lo que no se podía: pedir métricas sin relanzar la app."""
        self.assertFalse(self.p.enabled, "arrancó encendida sin pedirlo")
        self.p.encender()
        self.assertTrue(self.p.enabled)
        self.assertTrue(self.mod._wanted("overlay"))
        self.assertTrue(self.mod._wanted("prompt"))

    def test_apagado_deja_de_recoger(self) -> None:
        self.p.encender()
        self.p.note_prompt(100, 200, 1)
        turnos = self.p.snapshot()["prompt_turnos"]
        self.p.apagar()
        self.p.note_prompt(100, 200, 1)
        self.assertEqual(
            self.p.snapshot()["prompt_turnos"],
            turnos,
            "sigue recogiendo prompt despues de apagar()",
        )
        self.assertFalse(self.p.enabled)

    def test_apagar_no_le_pisa_al_entorno(self) -> None:
        """Si el proceso se lanzó medido, el panel no puede desmedirlo.

        Cerrar el panel devuelve el mando a ``MINDVOICE_PERF``. Si el
        arranque pidió métricas, esas siguen: son de quien lanzó la app, no
        del panel.
        """
        os.environ["MINDVOICE_PERF"] = "1"
        self.addCleanup(os.environ.pop, "MINDVOICE_PERF", None)
        p = self.mod.Perf()
        p.encender()
        p.apagar()
        self.assertTrue(p.enabled, "apagar() desmedro lo que pidio el entorno")

    def test_encender_de_medidores_concretos(self) -> None:
        """Se puede pedir una sola magnitud, que es lo que hace falta al medir."""
        self.p.encender(["overlay"])
        self.p.note_prompt(100, 200, 1)
        self.assertNotIn(
            "prompt_turnos",
            self.p.snapshot(),
            "midio el prompt sin que se lo pidieran",
        )
        self.assertTrue(self.mod._wanted("overlay"))

    def test_los_medidores_que_existen_son_los_conocidos(self) -> None:
        """Si se añade un medidor, ``_MEDIDORES`` tiene que crecer con él.

        El panel enciende ``_MEDIDORES`` por defecto. Un medidor nuevo que se
        quede fuera sería un dead end: existiría, se podría pedir por entorno y
        el panel no lo enseñaría nunca.
        """
        src = (RAIZ / "perf_instr.py").read_text(encoding="utf-8")
        medidores = {
            m
            for m in re.findall(r'_wanted\("(\w+)"\)', src)
        }
        declarados = set(self.mod._MEDIDORES)
        self.assertTrue(medidores, "no encuentro ningun medidor en perf_instr")
        self.assertEqual(
            medidores - declarados,
            set(),
            f"medidores que el panel no enciende: {sorted(medidores - declarados)}",
        )


# ======================================================================
# perf_instr: el resumen que se lee en pantalla
# ======================================================================
class TestResumenParaElPanel(unittest.TestCase):
    """El texto del panel sale del ``snapshot``, no de campos sueltos."""

    def setUp(self) -> None:
        for var in ("MINDVOICE_PERF", "MINDVOICE_PERF_OUT"):
            os.environ.pop(var, None)
        import perf_instr

        self.mod = importlib.reload(perf_instr)
        self.p = self.mod.Perf()

    def tearDown(self) -> None:
        self.p.apagar()
        os.environ.pop("MINDVOICE_PERF", None)

    def test_apagada_lo_dice_en_vez_de_enseñar_ceros(self) -> None:
        """Un HUD sano no tiene por qué ir a 0/s: es que no se está midiendo."""
        lineas = self.p.resumen()
        self.assertTrue(lineas)
        self.assertIn("apagada", lineas[0])
        self.assertTrue(
            any("sin ciclos" in l for l in lineas),
            f"con la instrumentacion apagada deberia decir que no mide: {lineas}",
        )

    def test_encendida_no_miente_aboutra_nada(self) -> None:
        self.p.encender()
        for _ in range(4):
            self.p.ui_cycle_start()
            self.p.ui_cycle_end()
        self.p.note_drop(3)
        self.p.note_prompt(320, 900, 2)
        texto = "\n".join(self.p.resumen())
        self.assertNotIn("apagada", texto)
        self.assertIn("volcado", texto)
        self.assertIn("cola descartada 3", texto)
        self.assertIn("4 ciclos", texto)
        self.assertIn("1.2k car", texto, "no suma memoria + pantalla del turno")
        self.assertIn("~ 305 tok", texto)

    def test_las_cifras_del_texto_son_las_del_informe(self) -> None:
        """Cada número del panel tiene que existir en el JSON.

        Es la razón de leer del ``snapshot``: si alguien compusiera una línea a
        mano con un dato que el informe no tiene, el panel estaría enseñando
        algo que nadie puede comprobar. El reloj va congelado porque los
        ciclos por segundo cambian entre dos llamadas seguidas y aquí se
        comparan dos.
        """
        reloj = RelojFalso()
        with mock.patch.object(self.mod.time, "perf_counter", reloj):
            self.p.encender()
            for _ in range(3):
                self.p.ui_cycle_start()
                reloj.avanza(0.060)
                self.p.ui_cycle_end()
            d = self.p.snapshot()
            lineas = self.p.resumen()
        numeros = _numeros(d)
        self.assertTrue(lineas)
        for linea in lineas:
            # Los umbrales del rótulo ("tirones >100 ms") son etiquetas, no
            # datos: se quitan antes de buscar cifras que tengan que existir
            # en el informe.
            limpio = re.sub(r">\s*\d+(\.\d+)?\s*ms", ">umbral", linea)
            for cifra in re.findall(r"\d+(?:\.\d+)?", limpio):
                # Tolerancia de 0.06 porque el panel redondea a un decimal y el
                # informe a dos, y las medias del panel salen ya redondeadas.
                self.assertTrue(
                    any(abs(n - float(cifra)) < 0.06 for n in numeros),
                    f"la cifra {cifra} de '{linea}' no sale de {d}",
                )

    def test_un_ciclo_largo_cuenta_como_tiron(self) -> None:
        """La cifra que el ojo nota: un turno sin refrescar.

        El hueco se mide entre dos volcados seguidos, así que el reloj avanza
        ANTES de cada ``ui_cycle_start``: es el temporizador de 60 ms que se
        retrasa cuando el motor está ocupado.
        """
        reloj = RelojFalso()
        with mock.patch.object(self.mod.time, "perf_counter", reloj):
            self.p.encender()
            for salto in (0.060, 0.060, 0.400, 0.900, 0.060):
                reloj.avanza(salto)
                self.p.ui_cycle_start()
                self.p.ui_cycle_end()
            texto = "\n".join(self.p.resumen())
        self.assertIn(">100 ms 2", texto, texto)
        self.assertIn(">500 ms 1", texto, texto)

    def test_un_solo_turno_no_pone_turnos_en_plural(self) -> None:
        self.p.encender()
        self.p.note_prompt(10, 10)
        texto = "\n".join(self.p.resumen())
        self.assertIn("1 turno", texto)
        self.assertNotIn("1 turnos", texto)

    def test_corto_no_crece_mas_de_una_linea(self) -> None:
        self.assertEqual(self.mod._corto(1.25), "1.2")
        self.assertEqual(self.mod._corto(999), "999")
        self.assertEqual(self.mod._corto(1234), "1.2k")


def _numeros(d: dict) -> list[float]:
    """Todos los números de un informe, para comparar con los del panel."""
    out: list[float] = []

    def _bajar(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            out.append(float(v))
        elif isinstance(v, dict):
            for x in v.values():
                _bajar(x)
        elif isinstance(v, list):
            for x in v:
                _bajar(x)

    _bajar(d)
    return out


# ======================================================================
# overlay: cableado que un test con ventana no puede ver
# ======================================================================
class TestCableadoDelPanel(unittest.TestCase):
    """Se lee el código, no se levanta Qt.

    Son invariantes que importan cuando el HUD no se puede construir (otra
    instancia, CI sin pantalla) y que no se ven en la ventana: que el timer
    nazca parado, que el atajo sea un atajo y no un keyPressEvent, y que al
    cerrar la ventana el timer pare.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = (RAIZ / "overlay.py").read_text(encoding="utf-8")

    def test_el_panel_nace_oculto(self) -> None:
        """Oculto con ``setVisible(False)``, que además le quita el hueco.

        Un ``QWidget`` con ``isHidden()`` pero visible para el layout
        reservaría sitio: el HUD de todos los días mediría y se vería distinto
        por tener un panel de diagnóstico que nadie pidió.
        """
        self.assertIn("self._perf_box.setVisible(False)", self.src)

    def test_el_timer_no_arranca_al_construir(self) -> None:
        """Solo ``_toggle_perf`` puede arrancarlo.

        Si el panel nace con su timer corriendo, cada app paga un tick por
        segundo para no mirar nada. Se comprueba que el bloque de creation no
        llama a ``start()``.
        """
        m = re.search(
            r"self\._perf_timer = QTimer\(self\).*?(?=\n\n)", self.src, re.S
        )
        self.assertIsNotNone(m, "no encuentro la creacion del timer de diagnostico")
        self.assertNotIn(
            ".start()",
            m.group(0),
            "el timer de diagnostico arranca al construir el HUD: cuesta un "
            "tick por segundo con el panel cerrado",
        )

    def test_el_arranque_y_la_parada_solo_caben_en_el_toggle(self) -> None:
        metodo =         self._cuerpo("_toggle_perf")
        self.assertIn("self._perf_timer.start()", metodo)
        self.assertIn("self._perf_timer.stop()", metodo)
        # Encender y apagar la instrumentación van atados al panel: abrirlo
        # mide, cerrarlo devuelve el mando al entorno.
        self.assertIn("_perf.encender()", metodo)
        self.assertIn("_perf.apagar()", metodo)
        # Y el tamaño del panel cambia, así que el layout hay que invalidarlo.
        self.assertIn(
            "self._relayout_panel()",
            metodo,
            "el toggle cambia el alto del panel sin invalidar el layout",
        )

    def test_el_tick_no_pinta_si_el_texto_no_cambio(self) -> None:
        """Un ``setText`` con lo mismo de contenido igual repinta el panel."""
        metodo =         self._cuerpo("_perf_tick")
        self.assertIn("if texto != self._perf_texto:", metodo)
        self.assertLess(
            metodo.index("if texto != self._perf_texto:"),
            metodo.index("self._perf_lbl.setText(texto)"),
            "el setText tiene que estar dentro de la comparacion",
        )

    def test_el_tick_no_hace_nada_con_el_panel_cerrado(self) -> None:
        self.assertIn("not self._perf_visible",         self._cuerpo("_perf_tick"))

    def test_el_atajo_es_un_shortcut_no_un_keypressevent(self) -> None:
        """El foco al abrir el HUD está en el campo de órdenes.

        Un ``keyPressEvent`` de la ventana no lo vería: la tecla se la come el
        ``QLineEdit``. Por eso es un ``QShortcut``.
        """
        self.assertIn('QShortcut(QKeySequence("Ctrl+Shift+D")', self.src)
        self.assertIn("Qt.ShortcutContext.WindowShortcut", self.src)
        self.assertIn("self._perf_shortcut.activated.connect", self.src)

    def test_cerrar_la_ventana_para_el_timer(self) -> None:
        cierre = self._cuerpo("closeEvent")
        self.assertIn("self._perf_timer.stop()", cierre)

    def test_ocultar_el_hud_para_el_tick(self) -> None:
        """Con la ventana oculta no se ve el panel: no hay que medirlo."""
        ocultar = self._cuerpo("hide_overlay")
        self.assertIn("self._perf_timer.stop()", ocultar)
        salir = self._cuerpo("show_overlay")
        self.assertIn("self._perf_timer.start()", salir)
        self.assertIn("self._perf_tick()", salir)

    def test_ocultar_el_hud_no_cierra_el_panel(self) -> None:
        """El flag no se toca: al volver, el panel sigue abierto como estaba."""
        ocultar = self._cuerpo("hide_overlay")
        self.assertNotIn(
            "self._perf_visible =",
            ocultar,
            "ocultar el HUD no debe cambiar _perf_visible: eso cerraria el "
            "panel al esconder el HUD, sin que el usuario lo pidiera",
        )

    def test_el_tick_no_es_barato_de_por_si(self) -> None:
        """1 s es legible y barato; 60 ms sería un panel que se recalcula solo."""
        m = re.search(r"_PERF_TICK_MS = (\d+)", self.src)
        self.assertIsNotNone(m, "no encuentro el intervalo del panel")
        self.assertGreaterEqual(
            int(m.group(1)),
            250,
            "el panel de diagnostico se refresca demasiado a menudo",
        )

    def _cuerpo(self, nombre: str) -> str:
        """El cuerpo de un método del HUD, leído del fichero.

        Se casa en ``def NOMBRE(self`` y no en ``def NOMBRE(self)`` porque
        algunos métodos (``closeEvent``, ``keyPressEvent``) reciben el evento
        como segundo argumento.
        """
        m = re.search(
            rf"def {re.escape(nombre)}\(self.*?(?=\n    def |\Z)", self.src, re.S
        )
        self.assertIsNotNone(m, f"no encuentro el metodo {nombre}")
        return m.group(0)


# ======================================================================
# El panel de verdad, en un HUD de verdad
# ======================================================================
try:
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    import overlay
    from config import Settings

    _TIENE_QT = True
except Exception as _exc:  # noqa: BLE001 - sin PyQt6 el resto de tests corre igual
    overlay = None  # type: ignore[assignment]
    _TIENE_QT = False
    _FALLO_QT = _exc


@unittest.skipUnless(_TIENE_QT, "no hay PyQt6: se prueba el cableado sobre el texto")
class TestPanelDeDiagnosticoEnElHud(unittest.TestCase):
    """El ciclo abrir/cerrar sobre el HUD real.

    Un SOLO HUD para toda la clase por el mutex de instancia única: el
    segundo no llegaría a construir el panel.
    """

    @classmethod
    def setUpClass(cls) -> None:
        s = Settings()
        s.overlay_show_on_start = False
        cls.w = overlay.OverlayHud(s)
        assert cls.w._single_ok, "no se pudo construir el HUD de pruebas"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.w.hide()
        # Libera el mutex: CreateMutexW lo suelta solo al morir el proceso, y
        # el proceso de pruebas sigue vivo.
        if cls.w._single_mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(cls.w._single_mutex))
            cls.w._single_mutex = 0

    def setUp(self) -> None:
        # Cada test parte de cerrado y sin instrumentacion.
        if self.w._perf_visible:
            self.w._toggle_perf()
        overlay._perf.apagar()
        self.w._perf_texto = ""
        self.w._perf_lbl.setText("")

    def tearDown(self) -> None:
        if self.w._perf_visible:
            self.w._toggle_perf()
        if self.w._anim_panel is not None:
            self.w._anim_panel.stop()
            self.w._anim_panel = None
        self.w._cerrando = False
        self.w.hide()
        overlay._perf.apagar()

    def test_arranca_apagado_y_sin_encender_nada(self) -> None:
        self.assertFalse(self.w._perf_visible)
        # ``isHidden`` y no ``isVisible``: este HUD no está en pantalla, y en
        # Qt un hijo de una ventana oculta responde False a ``isVisible``
        # aunque nadie lo haya escondido. Lo que se comprueba aquí es el
        # estado que el propio panel se da a sí mismo.
        self.assertTrue(
            self.w._perf_box.isHidden(), "el panel se ve al arrancar el HUD"
        )
        self.assertFalse(self.w._perf_timer.isActive(), "el timer corre al arrancar")
        self.assertFalse(
            overlay._perf.enabled,
            "se puso a medir sin que nadie lo pidiera",
        )

    def test_abrir_muestra_cifras_y_arranca_el_reloj(self) -> None:
        self.w._toggle_perf()
        self.assertFalse(self.w._perf_box.isHidden(), "abrir no enseñó el panel")
        self.assertTrue(self.w._perf_timer.isActive())
        self.assertTrue(overlay._perf.enabled, "abrir el panel no encendio las metricas")
        self.assertTrue(self.w._perf_lbl.text(), "el panel abrio en blanco")
        # Y ya mide: el readout sale de las cifras reales, no de un texto fijo.
        self.w._perf_tick()
        self.assertIn("motor:", self.w._perf_lbl.text())
        self.assertIn("HUD", self.w._perf_lbl.text())

    def test_cerrar_devuelve_el_mando_al_entorno(self) -> None:
        self.w._toggle_perf()
        self.w._toggle_perf()
        self.assertTrue(self.w._perf_box.isHidden())
        self.assertFalse(self.w._perf_timer.isActive())
        self.assertFalse(overlay._perf.enabled)

    def test_veinte_ciclos_no_dejan_nada_colgando(self) -> None:
        """La prueba de la Fase 4: abrir y cerrar no puede tener deriva."""
        # Con la ventana en pantalla el alto del panel es el de verdad, que es
        # el que reserva hueco en el layout.
        self.w.show()
        app = QApplication.instance()
        app.processEvents()
        base = self.w.panel.size()
        for _ in range(20):
            self.w._toggle_perf()
        app.processEvents()
        self.assertFalse(self.w._perf_visible)
        self.assertFalse(self.w._perf_timer.isActive())
        self.assertEqual(
            self.w.panel.size(),
            base,
            "el panel quedo mas alto tras 20 ciclos: el layout reserva el hueco",
        )
        # Y que abrir sí lo haga más alto: si no, lo anterior no probaría nada.
        self.w._toggle_perf()
        app.processEvents()
        self.assertGreater(
            self.w.panel.size().height(),
            base.height(),
            "el panel deberia crecer al abrirse",
        )
        self.w._toggle_perf()
        app.processEvents()
        self.assertEqual(self.w.panel.size(), base)
        self.w.hide()

    def test_ocultar_y_mostrar_el_hud_reanuda_el_tick(self) -> None:
        """Con el HUD oculto no se ve el panel, así que no se mide."""
        self.w.show_overlay()
        self.w._toggle_perf()
        self.assertTrue(self.w._perf_timer.isActive())
        self.w.hide_overlay()
        self.assertFalse(
            self.w._perf_timer.isActive(),
            "el tick sigue pidiendo snapshots con el HUD oculto",
        )
        self.assertTrue(
            self.w._perf_visible,
            "ocultar el HUD cerro el panel: al volver tendria que seguir abierto",
        )
        self.w.show_overlay()
        self.assertTrue(
            self.w._perf_timer.isActive(),
            "el panel no reanudo su tick al volver el HUD",
        )

    def test_el_tick_ignorado_con_el_panel_cerrado(self) -> None:
        self.w._perf_tick()
        self.assertEqual(
            self.w._perf_lbl.text(),
            "",
            "un tick con el panel cerrado escribio en el label",
        )

    def test_no_reescribe_el_label_si_el_texto_no_cambio(self) -> None:
        self.w._toggle_perf()
        self.w._perf_tick()
        self.assertTrue(self.w._perf_texto)
        original = self.w._perf_lbl.text()
        # Se rompe el texto interno: si el tick no compara, lo reescribe.
        self.w._perf_texto = "NO ES EL MISMO"
        self.w._perf_tick()
        self.assertEqual(
            self.w._perf_lbl.text(),
            original,
            "el tick reescribio el label sin que cambiera nada",
        )

    def test_el_atajo_de_verdad_abre_el_panel(self) -> None:
        """El atajo dispara el toggle de verdad, no un keyPressEvent."""
        self.w._perf_shortcut.activated.emit()
        self.assertTrue(
            self.w._perf_visible,
            "Ctrl+Shift+D no llego al toggle (QShortcut mal conectado)",
        )
        self.w._perf_shortcut.activated.emit()
        self.assertFalse(self.w._perf_visible)

    def test_el_atajo_cabe_en_el_texto_del_panel(self) -> None:
        self.assertIn("Ctrl+Shift+D", self.w._perf_lbl.toolTip())


if __name__ == "__main__":
    unittest.main()
