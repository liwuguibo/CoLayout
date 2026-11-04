"""Shared writable-LPP presentation for canvas creation tools."""

from PySide6.QtCore import QSignalBlocker

from .regions.query import metals


def populate_layers(combo, profile, previous, *, metal_only=False):
    allowed = set(metals(profile)) if metal_only else None
    with QSignalBlocker(combo):
        combo.clear()
        for layer, purpose in profile.writable_lpps:
            if allowed is not None and (purpose != 'drawing' or layer not in allowed):
                continue
            combo.addItem(layer + ' / ' + purpose, (layer, purpose))
        index = combo.findData(previous or profile.editor_defaults.get('layer'))
        if index >= 0:
            combo.setCurrentIndex(index)
