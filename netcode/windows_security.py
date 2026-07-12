"""Windows machine-local secret protection for the Local Connector.

The Windows service runs as SYSTEM, so DPAPI uses machine scope while NTFS ACLs
limit access to SYSTEM and local administrators.  No secret-encryption key is
stored in the package or sent to the control plane.
"""

from __future__ import annotations

import os


CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4


def _require_windows() -> None:
    if os.name != "nt":
        raise OSError("Windows DPAPI is available only on Windows")


def protect_machine(data: bytes) -> bytes:
    """Encrypt bytes using machine-scoped Windows DPAPI."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source_buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    protected = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    flags = CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE
    if not crypt32.CryptProtectData(
        ctypes.byref(source), None, None, None, None, flags, ctypes.byref(protected)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        kernel32.LocalFree(protected.pbData)


def unprotect_machine(data: bytes) -> bytes:
    """Decrypt machine-scoped Windows DPAPI bytes."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source_buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    clear = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(clear)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(clear.pbData, clear.cbData)
    finally:
        kernel32.LocalFree(clear.pbData)
