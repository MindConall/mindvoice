"""Tests de la capa de acento QML del HUD (Fase 5).

La Fase 5 se decidió con números (ver el docstring de ``ui/accento.py``): la
isla QML gana al shader porque es testeable en ``offscreen`` y no revienta por
un detalle de API. Este fichero comprueba justo eso, en tres capas:

1. **El QML y el contrato**, leídos del fuente: que el acento va embebido (sin
   fichero de datos que el instalador pueda olvidar) y que la isla NO se
   construye en ``__init__`` (el lazy de verdad empieza aquí).
2. **El cableado del HUD**, leído de ``overlay.py``: cuándo se carga, que va
   detrás del panel, y que se suelta al cerrar.
3. **La isla de verdad**, sobre ``offscreen``: que carga, que pinta tinta, que
   ``reposar`` no la desmonta y que ``apagar`` la suelta.
"""

import ctypes
import os
import pathlib
import re
import tempfile
import unittest

RAIZ = pathlib.Path(__file__).resolve().parent
FUENTE_ACENTO = (RAIZ / "ui" / "accento.py").read_text(encoding="utf-8")
FUENTE_OVERLAY = (RAIZ / "overlay.py").read_text(encoding="utf-8")


# ======================================================================
# Contrato del módulo, leído del fuente (sin Qt)
# ======================================================================
class TestContratoDelAcento(unittest.TestCase):
    def test_el_qml_va_embebido_y_no_como_fichero_del_repo(self) -> None:
        """El instalador tiene una lista EXPLÍCITA de ficheros (build_release.ps1).

        Un ``.qml`` suelto es un fichero que el paquete puede olvidar sin que
        nadie se entere hasta que el acento no aparece. Embebido en el ``.py``,
        no hay nada que copiar aparte.
        """
        self.assertIn("QML_ACENTO =", FUENTE_ACENTO)
        self.assertNotIn(".qml\"", FUENTE_ACENTO.split("QML_ACENTO")[0])
        # El QML solo se ESCRIBE (al directorio de datos), nunca se lee de un
        # fichero del repo que el paquete pudiera no llevar.
        self.assertIn("write_text", FUENTE_ACENTO)
        self.assertNotIn("setSource(QUrl.fromLocalFile(RAIZ", FUENTE_ACENTO)

    def test_el_qml_expone_las_dos_propiedades_que_maneja_python(self) -> None:
        self.assertIn("property color acento", FUENTE_ACENTO)
        self.assertIn("property bool activo", FUENTE_ACENTO)

    def test_la_isla_no_se_construye_en_el_constructor(self) -> None:
        """Si el QQuickWidget se creara en ``__init__``, no sería lazy."""
        init = _cuerpo_clase(FUENTE_ACENTO, "HaloAcento", "__init__")
        self.assertNotIn("QQuickWidget(", init, "el constructor ya crea la isla QML")
        self.assertIn("self._vista = None", init)

    def test_el_qml_no_pide_modulos_externos(self) -> None:
        """``Qt5Compat.GraphicalEffects`` no siempre está; el QML no lo usa."""
        qml = FUENTE_ACENTO.split('QML_ACENTO = """')[1].split('"""')[0]
        self.assertNotIn("import Qt5Compat", qml)
        self.assertIn("import QtQuick", qml)


# ======================================================================
# Cableado en el HUD, leído del fuente
# ======================================================================
class TestCableadoDelHalo(unittest.TestCase):
    def test_el_estado_del_motor_mueve_el_acento(self) -> None:
        estado = _metodo_overlay("_on_assistant_state")
        self.assertIn("self._refrescar_halo(", estado)

    def test_en_reposo_no_se_carga_el_motor_qml(self) -> None:
        """La promesa de la Fase 5: el cold start no paga QML."""
        refresco = _metodo_overlay("_refrescar_halo")
        self.assertIn("reposar()", refresco)
        # Los estados que encienden el acento, todos presentes.
        for etiqueta in ("escuchando", "procesando", "hablando", "reconectando"):
            self.assertIn(etiqueta, refresco)

    def test_el_anillo_va_detras_del_panel(self) -> None:
        """``raise_()`` lo pondría encima y taparía el texto: es ``stackUnder``."""
        colocar = _metodo_overlay("_acomodar_halo")
        self.assertIn("stackUnder(self.panel)", colocar)
        self.assertNotIn("raise_()", colocar)

    def test_el_panel_al_cambiar_recoloca_el_anillo(self) -> None:
        self.assertIn("self._acomodar_halo()", _metodo_overlay("_relayout_panel"))

    def test_cerrar_el_hud_suelta_la_isla(self) -> None:
        cierre = _metodo_overlay("closeEvent")
        self.assertIn("self._halo.apagar()", cierre)

    def test_la_geometria_del_anillo_va_con_margen(self) -> None:
        """El anillo asoma por el borde: se ajusta unos píxeles por fuera."""
        self.assertRegex(FUENTE_ACENTO, r"adjusted\(-\d+, -\d+, \d+, \d+\)")


# ======================================================================
# La isla de verdad, en offscreen
# ======================================================================
try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QWidget

    _app = QApplication.instance() or QApplication([])
    import overlay
    from config import Settings
    from ui.accento import HaloAcento

    _TIENE_QT = True
except Exception as _exc:  # noqa: BLE001
    overlay = None  # type: ignore[assignment]
    _TIENE_QT = False
    _FALLO_QT = _exc


def _datos_temporales() -> None:
    """Apunta los datos de usuario a un temporal para no ensuciar el real.

    Es el proceso de pruebas, así que no hace falta restaurar la variable: el
    directorio temporal desaparece con la máquina de pruebas.
    """
    if os.environ.get("MINDVOICE_DATA_DIR", "").startswith(tempfile.gettempdir()):
        return
    os.environ["MINDVOICE_DATA_DIR"] = tempfile.mkdtemp(prefix="mindvoice-accento-")


@unittest.skipUnless(_TIENE_QT, "no hay PyQt6: se prueba el cableado sobre el texto")
class TestIslaQmlEnOffscreen(unittest.TestCase):
    """El acento tiene que funcionar sin GPU, que es como corre la suite."""

    @classmethod
    def setUpClass(cls) -> None:
        _datos_temporales()

    def setUp(self) -> None:
        self.caja = QWidget()
        self.caja.resize(520, 440)
        self.caja.show()
        _app.processEvents()

    def tearDown(self) -> None:
        self.caja.hide()

    def test_nace_sin_isla_y_sin_pintar(self) -> None:
        halo = HaloAcento(self.caja)
        self.assertFalse(halo.cargada, "la isla QML se construyo al nacer")
        self.assertFalse(halo.isVisible())

    def test_asegurar_construye_la_isla(self) -> None:
        halo = HaloAcento(self.caja)
        self.assertTrue(halo.asegurar(), "no se pudo cargar el QML en offscreen")
        self.assertTrue(halo.cargada)
        self.assertIsNotNone(halo._vista.rootObject())

    def test_animar_enciende_y_pinta(self) -> None:
        from PyQt6.QtCore import QRect

        halo = HaloAcento(self.caja)
        halo.sincronizar_geometria(QRect(40, 20, 320, 360))
        halo.animar("hablando", "#59d98f")
        # El anillo entra con un fundido (Behavior sobre opacity): hay que darle
        # tiempo a que la animación avance antes de capturar, o se graba a
        # opacidad 0 y parece que no pinta.
        _esperar(300)
        raiz = halo._vista.rootObject()
        self.assertTrue(raiz.property("activo"))
        self.assertTrue(halo.isVisible(), "animar no mostro el halo")
        # Que pinte de verdad: la vista QQuickWidget sí se puede capturar.
        img = halo._vista.grab().toImage()
        tinta = sum(
            1
            for x in range(0, img.width(), 3)
            for y in range(0, img.height(), 3)
            if img.pixelColor(x, y).alpha() > 40
        )
        self.assertGreater(tinta, 20, "el QML cargo pero no pinto nada")

    def test_reposar_apaga_sin_desmontar(self) -> None:
        halo = HaloAcento(self.caja)
        halo.animar("hablando", "#59d98f")
        halo.reposar()
        self.assertFalse(halo._vista.rootObject().property("activo"))
        self.assertFalse(halo.isVisible())
        self.assertTrue(halo.cargada, "reposar desmonto la isla: deberia ser barato")

    def test_apagar_suelta_la_isla(self) -> None:
        halo = HaloAcento(self.caja)
        halo.animar("hablando", "#59d98f")
        halo.apagar()
        self.assertFalse(halo.cargada)

    def test_reposar_sin_isla_no_la_construye(self) -> None:
        """Un HUD que nunca anima no debe pagar el motor QML por un reposar."""
        halo = HaloAcento(self.caja)
        halo.reposar()
        self.assertFalse(halo.cargada, "reposar construyo la isla sin necesidad")


@unittest.skipUnless(_TIENE_QT, "no hay PyQt6: se prueba el cableado sobre el texto")
class TestHaloEnElHudReal(unittest.TestCase):
    """Un solo HUD para la clase por el mutex de instancia única."""

    @classmethod
    def setUpClass(cls) -> None:
        _datos_temporales()
        s = Settings()
        s.overlay_show_on_start = False
        cls.w = overlay.OverlayHud(s)
        assert cls.w._single_ok, "no se pudo construir el HUD de pruebas"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.w._halo.apagar()
        cls.w.hide()
        if cls.w._single_mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(cls.w._single_mutex))
            cls.w._single_mutex = 0

    def setUp(self) -> None:
        self.w._halo.apagar()
        self.w._halo_estado = ""
        self.w._on_assistant_state("IDLE")

    def tearDown(self) -> None:
        self.w._halo.apagar()
        self.w._halo_estado = ""
        self.w._cerrando = False
        self.w.hide()

    def test_el_hud_arranca_sin_pagar_qml(self) -> None:
        self.assertIsNotNone(self.w._halo)
        self.assertFalse(self.w._halo.cargada, "el HUD arranca con el motor QML pagado")

    def test_un_estado_activo_enciende_el_acento(self) -> None:
        self.w.show()
        _app.processEvents()
        self.w._on_assistant_state("SPEAKING")
        _app.processEvents()
        self.assertTrue(self.w._halo.cargada, "el acento no se cargo al hablar")
        self.assertTrue(self.w._halo.isVisible())
        self.assertEqual(self.w._halo_estado, "hablando")

    def test_volver_a_reposo_apaga_pero_no_desmonta(self) -> None:
        self.w.show()
        _app.processEvents()
        self.w._on_assistant_state("PROCESSING")
        self.w._on_assistant_state("IDLE")
        _app.processEvents()
        self.assertFalse(self.w._halo.isVisible())
        self.assertTrue(self.w._halo.cargada, "IDLE desmonto la isla")

    def test_el_acento_va_detras_del_panel_y_a_su_medida(self) -> None:
        self.w.show()
        _app.processEvents()
        self.w._on_assistant_state("LISTENING")
        _app.processEvents()
        panel = self.w.panel.geometry()
        halo = self.w._halo.geometry()
        self.assertEqual(halo.left(), panel.left() - 6)
        self.assertEqual(halo.top(), panel.top() - 6)
        self.assertEqual(halo.width(), panel.width() + 12)
        self.assertEqual(halo.height(), panel.height() + 12)


# ======================================================================
# utilidades para leer el fuente
# ======================================================================
def _esperar(ms: int) -> None:
    """Deja correr el bucle de eventos ``ms`` milisegundos (avanza animaciones QML)."""
    from PyQt6.QtTest import QTest

    QTest.qWait(ms)


def _cuerpo_clase(fuente: str, clase: str, metodo: str) -> str:
    """Cuerpo de un método ``def metodo(self...)`` de una clase del fuente."""
    bloque = fuente.split(f"class {clase}")[1]
    m = re.search(rf"def {re.escape(metodo)}\(self.*?(?=\n    def |\Z)", bloque, re.S)
    assert m is not None, f"no encuentro {clase}.{metodo}"
    return m.group(0)


def _metodo_overlay(nombre: str) -> str:
    m = re.search(
        rf"def {re.escape(nombre)}\(self.*?(?=\n    def |\Z)", FUENTE_OVERLAY, re.S
    )
    assert m is not None, f"no encuentro el metodo {nombre} en overlay.py"
    return m.group(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
