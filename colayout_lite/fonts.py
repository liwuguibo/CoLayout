"""Select the existing UI font or an installed Chinese fallback."""
from functools import lru_cache
import sys
from PySide6.QtGui import QFont, QFontDatabase


@lru_cache(maxsize=1)
def ui_family():
    if sys.platform == 'win32':
        return 'Microsoft YaHei UI'
    available = set(QFontDatabase.families())
    for family in ('Noto Sans CJK SC', 'Source Han Sans SC', 'WenQuanYi Micro Hei',
                   'Microsoft YaHei UI'):
        if family in available:
            return family
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def ui_font(point_size=None, *, pixel_size=None):
    font = QFont(ui_family())
    if point_size is not None:
        font.setPointSize(point_size)
    if pixel_size is not None:
        font.setPixelSize(pixel_size)
    return font
