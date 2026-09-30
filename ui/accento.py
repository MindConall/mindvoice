"""Capa de acento animada del HUD, dibujada con QML y cargada en perezoso.

La Fase 5 tenía que decidir cómo se dibuja el brillo del panel. Se midieron las
tres rutas sobre un prototipo mínimo del tamaño real del HUD (416x390, Radeon
RX 580):

- **QSS** (lo de antes): construye en 2-3 ms y repinta en 0,6-1,1 ms por
  cuadro. Cero dependencias nuevas.
- **Shader GL 4.1** (``QOpenGLWidget``): en régimen es tan barato como QML, pero
  se descartó con datos: en la plataforma ``offscreen`` con la que corre TODA la
  suite, ``initializeGL`` y ``paintGL`` no llegan a ejecutarse nunca, así que el
  fondo sería intesteable; usar la API equivocada de PyQt6 (``QOpenGLFunctions``
  no existe; es ``QOpenGLFunctions_4_1_Core``) aborta el proceso con
  ``0xC0000409`` y sin traza; y en *core profile* sin VAO dibuja nada EN
  SILENCIO, sin error.
- **Isla QML** (``QQuickWidget``): repinta tan barato como el shader
  (0,07-0,19 ms), SÍ se ejecuta en ``offscreen`` (por eso es testeable) y no
  revienta por un detalle de API. Su precio es el arranque del motor QML: unos
  70 ms en caliente y ~230 ms en frío, porque la primera vez no hay caché de
  disco.

Ese precio de arranque es lo que decide el diseño de este módulo. El motor QML
**no** se paga al arrancar la app: se paga la primera vez que hay algo que
animar, o sea cuando el motor entra en un estado que merece el acento
(escuchando/procesando/hablando). Un usuario que abre el HUD y no usa el motor
no paga nunca ese coste. El fondo base del panel sigue siendo el QSS de
siempre; esto es solo la capa de encima, así que el arranque en frío queda
como estaba.

Dos consecuencias de haber cargado QML a mano, escritas para no repetirlas:

- **El QML va como cadena, no como fichero del repo.** Así el instalador no
  necesita copiar un dato nuevo (su lista de ficheros es explícita) y no hay
  forma de que el paquete salga sin el ``.qml``. La cadena se escribe una vez,
  ya en caliente, en el directorio de datos del usuario, que sí es escribible.
- **Nada de esto puede tumbar el HUD.** Si QtQuick no está, si el motor QML
  falla o si el fichero no se puede escribir, se registra y se sigue: el HUD
  funciona sin acento, que es exactamente como funcionaba antes de la Fase 5.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from . import tokens

logger = logging.getLogger(__name__)

# El acento va como cadena por lo dicho arriba. Es QML de Qt6 puro: un rectángulo
# redondeado con un borde que respira. No toca layout, no pide ficheros externos
# y no usa módulos de efectos (``Qt5Compat.GraphicalEffects`` no siempre está).
#
# ``acento`` y ``activo`` son las dos propiedades que maneja Python: el color lo
# pone el estado del motor y ``activo`` enciende o apaga la respiración. La
# animación vive en QML, no en un timer de Python: el motor tiene su propio
# reloj y así no se añade un temporizador más al HUD.
QML_ACENTO = """import QtQuick

Item {
    id: raiz
    property color acento: "#7ee0ff"
    property bool activo: false

    Rectangle {
        id: anillo
        anchors.fill: parent
        anchors.margins: 6
        radius: 18
        color: "transparent"
        border.width: 2
        border.color: raiz.acento
        opacity: 0.0

        Behavior on opacity {
            NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
        }

        SequentialAnimation on scale {
            running: raiz.activo
            loops: Animation.Infinite
            NumberAnimation { from: 1.0; to: 1.012; duration: 900;
                               easing.type: Easing.InOutQuad }
            NumberAnimation { from: 1.012; to: 1.0; duration: 900;
                               easing.type: Easing.InOutQuad }
        }

        states: State {
            name: "encendido"
            when: raiz.activo
            PropertyChanges { target: anillo; opacity: 0.9 }
        }
    }
}
"""

# Nombre del fichero que se materializa en el directorio de datos. Es estable a
# propósito: el motor QML cachea el bytecode compilado por URL, así que una ruta
# que no cambie entre arranques aprovecha la caché (los ~70 ms en caliente en
# vez de los ~230 ms en frío).
_NOMBRE_QML = "accento_hud.qml"


def _escribir_qml() -> Path | None:
    """Deja el QML en el directorio de datos y devuelve su ruta, o ``None``.

    Se escribe solo si falta o si su contenido cambió (una versión nueva del
    HUD puede traer otro acento). Cualquier fallo de escritura no es fatal: sin
    acento el HUD sigue siendo el mismo de antes.
    """
    try:
        from rutas import ensure_data_dir

        destino = ensure_data_dir() / _NOMBRE_QML
        try:
            if destino.read_text(encoding="utf-8") == QML_ACENTO:
                return destino
        except OSError:
            pass  # no existe todavía o no se puede leer: se escribe igual
        destino.write_text(QML_ACENTO, encoding="utf-8")
        return destino
    except Exception as exc:  # noqa: BLE001 - el acento es cosmético
        logger.warning("No se pudo preparar el QML del acento: %s", exc)
        return None


class HaloAcento(QWidget):
    """Anillo que respira alrededor del panel, dibujado con una isla QML.

    Nace SIN isla QML: en ``__init__`` no se importa QtQuick ni se crea nada. La
    isla se construye la primera vez que se pide ``animar()`` (lazy de verdad,
    no solo diferido). Mientras nadie lo pida, este widget es un ``QWidget``
    vacío que no cuesta nada.

    Es un hijo de la ventana y va POR DEBAJO del panel, un poco más grande, para
    que el anillo asome por los bordes redondeados sin tapar el texto.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vista = None  # QQuickWidget, None hasta el primer ``animar()``
        self._estado = ""
        self._fallo_dicho = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hide()

    @property
    def cargada(self) -> bool:
        """¿Se pagó ya el arranque del motor QML?"""
        return self._vista is not None

    @property
    def estado(self) -> str:
        return self._estado

    def asegurar(self) -> bool:
        """Construye la isla QML la primera vez. Idempotente.

        Devuelve si el acento quedó utilizable. Un ``False`` no es un error del
        HUD: es el HUD de antes, sin acento.
        """
        if self._vista is not None:
            return True
        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtQuickWidgets import QQuickWidget

            ruta = _escribir_qml()
            if ruta is None:
                return False
            vista = QQuickWidget(self)
            vista.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
            # La vista se mete en un layout SIN márgenes sobre este widget: así
            # sigue su tamaño sola. Es la parte que se me escapó la primera vez:
            # la isla nace DESPUÉS de colocar el halo (el lazy la retrasa hasta
            # el primer ``animar``), y con la geometría puesta a mano nacía 0x0
            # e invisible. Con el layout no hay que acordarse de redimensionarla.
            capa = QVBoxLayout(self)
            capa.setContentsMargins(0, 0, 0, 0)
            capa.addWidget(vista)
            # Sin fondo propio: lo que no dibuja el QML queda transparente y
            # deja ver el QSS del panel que tiene debajo.
            from PyQt6.QtGui import QColor

            vista.setClearColor(QColor(0, 0, 0, 0))
            vista.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            vista.setSource(QUrl.fromLocalFile(str(ruta)))
            if vista.status() == QQuickWidget.Status.Error:
                errores = "; ".join(e.toString() for e in vista.errors())
                raise RuntimeError(errores or "QML inválido")
            self._vista = vista
            self._aplicar()
            return True
        except Exception as exc:  # noqa: BLE001 - el acento nunca es crítico
            if not self._fallo_dicho:
                logger.warning("El acento QML no está disponible: %s", exc)
                self._fallo_dicho = True
            self._vista = None
            return False

    def animar(self, estado: str, color: str | None = None) -> None:
        """Enciende el acento para un estado del motor, cargando la isla si hace falta."""
        self._estado = estado
        if not self.asegurar():
            return
        if color is not None:
            self._vista.rootObject().setProperty("acento", color)
        self._vista.rootObject().setProperty("activo", bool(color))
        self._aplicar()

    def reposar(self) -> None:
        """Apaga la respiración sin desmontar la isla (barato de volver).

        No se llama a ``asegurar()``: si nunca hubo acento, no tiene sentido
        pagar el motor QML solo para apagarlo.
        """
        self._estado = ""
        if self._vista is None:
            return
        self._vista.rootObject().setProperty("activo", False)
        self._aplicar()

    def sincronizar_geometria(self, rect) -> None:
        """Coloca el anillo alrededor del panel. ``rect`` está en coordenadas del padre.

        Solo se coloca ESTE widget: la vista QML va en un layout sin márgenes, así
        que se redimensiona con él. Antes se le puso la geometría a la vista a
        mano, y como la isla nace después de colocar el halo, se quedaba en 0x0.
        """
        try:
            from PyQt6.QtCore import QRect

            self.setGeometry(QRect(rect).adjusted(-6, -6, 6, 6))
        except Exception as exc:  # noqa: BLE001 - cosmético
            logger.debug("No se pudo colocar el acento: %s", exc)

    def _aplicar(self) -> None:
        """Hace que el anillo se vea o se oculte según haya estado.

        No se llama a ``raise_()`` a propósito: el anillo tiene que quedar POR
        DEBAJO del panel (es un halo alrededor, no una capa que tape el texto).
        Quien lo coloca llama antes a ``stackUnder(panel)``.
        """
        self.setVisible(bool(self._estado))

    def apagar(self) -> None:
        """Suelta la isla QML del todo (cierre del HUD).

        Sin esto, el motor QML seguiría con su animación viva contra una ventana
        que se está destruyendo.
        """
        if self._vista is not None:
            try:
                self._vista.setParent(None)
                self._vista.deleteLater()
            except RuntimeError:
                pass
            self._vista = None
        self._estado = ""
