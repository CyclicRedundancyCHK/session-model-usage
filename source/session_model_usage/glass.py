"""Shared glass preferences and background blur for Qt's layered windows."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path

from .platform_win import state_directory


@dataclass(frozen=True)
class GlassSettings:
    enabled: bool = True
    opacity: int = 40


def read_glass_settings(folder: Path | None = None) -> GlassSettings:
    enabled, opacity = True, 40
    try:
        with ((folder or state_directory()) / "settings.ini").open("rb") as stream:
            content = stream.read(65_537)
        if len(content) > 65_536:
            return GlassSettings()
        for line in content.decode("utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if not separator:
                continue
            key, value = key.strip().casefold(), value.strip().casefold()
            if key == "glassenabled" and value in ("true", "false"):
                enabled = value == "true"
            elif key == "glassopacitypercent":
                try:
                    number = int(value)
                    if 5 <= number <= 95:
                        opacity = number
                except ValueError:
                    pass
    except (OSError, UnicodeError):
        pass
    return GlassSettings(enabled, opacity)


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                ("gradient", ctypes.c_uint32), ("animation", ctypes.c_int)]


class _CompositionData(ctypes.Structure):
    _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p),
                ("size", ctypes.c_size_t)]


def apply_glass(hwnd: int, settings: GlassSettings) -> str:
    """Use a system-rounded backdrop, with Qt supplying tint and content.

    A window region does not clip the accent blur of a layered Qt window.
    Without native rounding, retain alpha transparency instead of rectangular blur.
    """
    if os.name != "nt" or not hwnd:
        return "unavailable"
    rounded = False
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                             ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        # Match the panel's 8-DIP outer radius and let DWM clip its own backdrop.
        corner = ctypes.c_int(2)
        rounded = dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner)) == 0
        border = ctypes.c_uint32(0xFFFFFFFE)
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
        # Clear a previous system-backdrop fallback before changing materials.
        backdrop = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
    except (OSError, AttributeError):
        pass
    try:
        user = ctypes.WinDLL("user32")
        user.SetWindowCompositionAttribute.argtypes = [wintypes.HWND, ctypes.POINTER(_CompositionData)]
        user.SetWindowCompositionAttribute.restype = wintypes.BOOL
        policy = _AccentPolicy(3 if settings.enabled and rounded else 0, 0, 0, 0)
        data = _CompositionData(19, ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p), ctypes.sizeof(policy))
        if user.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)):
            if rounded:
                return "rounded-blur-behind" if settings.enabled else "rounded-disabled"
            return "alpha-only" if settings.enabled else "disabled"
    except (OSError, AttributeError):
        pass
    # Retain the Qt alpha surface if blur is unavailable. An opaque system
    # backdrop would bring back the rejected dark panel.
    return "unavailable"
