"""Identidad visual de MindVoice: logo robot 8-bit estilo vintage.

El logo se define por píxeles (matriz de texto) y se dibuja en código, sin
necesidad de descargar nada. Se usa:

- En el HUD (overlay) como encabezado, dibujado con Qt.
- Como icono de la ventana y del acceso directo del escritorio (``.ico``).
- Como ``.png`` de mayor resolución en ``assets/`` si se generan los ficheros.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Cuerpo del robot: '#' = verde menta, 'o' = ojos claros, '.' = transparente.
ROBOT_PIXELS = [
    "....#....",
    "....#....",
    "..#####..",
    ".##...##.",
    "##o###o##",
    "#########",
    "#..#.#..#",
    "..#####..",
    "...#.#...",
    "...#.#...",
]

BODY = (89, 217, 143)      # #59d98f (verde menta de la app)
EYE = (236, 240, 242)      # #eceef2 (blanco hueso)
TRANSPARENT = (0, 0, 0, 0)

# Tamaño base en píxeles del logo (alto del HUD, sin escalado).
LOGO_BASE = 44

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
LOGO_PNG = ASSETS_DIR / "mindvoice_logo.png"
LOGO_ICO = ASSETS_DIR / "mindvoice_logo.ico"
LOGO_BASE_PNG = ASSETS_DIR / "mindvoice_logo_base.png"


def pixel_row_colors(row: str) -> list:
    """Convierte una fila del mapa en colores RGBA (píxel a píxel)."""
    return [EYE if ch == "o" else BODY if ch == "#" else TRANSPARENT for ch in row]


def icon_canvas(size: int, scale: int | None = None) -> "Image.Image":
    """Dibuja el robot 8-bit centrado en un lienzo cuadrado ``size``.

    Cada celda del mapa ocupa ``size / 12`` píxeles (con margen), escalando con
    NEAREST para mantener el estilo pixel-art nítido en cualquier tamaño.
    """
    from PIL import Image

    rows = [pixel_row_colors(row) for row in ROBOT_PIXELS]
    height = len(rows)
    width = len(rows[0]) if rows else 0
    if not width:
        raise ValueError("Mapa del robot vacío")

    cell = max(1, round(size / 12))
    robot_w = cell * width
    robot_h = cell * height
    offset_x = (size - robot_w) // 2
    offset_y = (size - robot_h) // 2

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(rows):
        for x, rgba in enumerate(row):
            rgba = rgba if len(rgba) == 4 else (*rgba, 255)
            if rgba[3] != 0:
                for dy in range(cell):
                    for dx in range(cell):
                        px[offset_x + x * cell + dx, offset_y + y * cell + dy] = rgba
    return img


def generate_asset_files(icon_size: int = 256) -> None:
    """Genera ``assets/mindvoice_logo.png`` y ``.ico`` a partir de los píxeles.

    El ICO incluye tamaños estándar (16-256 px), cada uno pre-escalado con
    NEAREST para que el pixel-art se vea igual de nítido que en el HUD.
    Pillow guarda el PNG a ``icon_size`` (por defecto 256).
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow no está disponible; no se generan los iconos.")
        return

    try:
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        base = icon_canvas(icon_size)
        base.save(LOGO_PNG)
        base.save(LOGO_BASE_PNG)

        sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
        base.save(
            LOGO_ICO,
            format="ICO",
            sizes=[(s, s) for s in sizes],
        )
        logger.info("Iconos MindVoice generados en %s", ASSETS_DIR)
    except Exception as exc:  # noqa: BLE001 - no crítico
        logger.warning("No se pudieron generar los iconos: %s", exc)


if __name__ == "__main__":
    generate_asset_files()
    print("Logo generado:", LOGO_PNG, "y", LOGO_ICO)