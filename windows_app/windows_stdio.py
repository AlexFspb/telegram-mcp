"""Use inherited MCP pipes in a Windows GUI executable without allocating a console."""

from __future__ import annotations

import io
import os
import struct
import sys
from pathlib import Path


def _inherited_stream(number: int, mode: str):
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = (wintypes.DWORD,)
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = (wintypes.HANDLE,)
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.GetStdHandle(number & 0xFFFFFFFF)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    # Pipes/files only. Never attach to a terminal or use a screen buffer.
    if kernel.GetFileType(handle) not in (1, 3):
        return None
    duplicate = wintypes.HANDLE()
    process = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
        raise ctypes.WinError(ctypes.get_last_error())
    flags = os.O_BINARY | (os.O_RDONLY if mode == "r" else os.O_WRONLY)
    try:
        descriptor = msvcrt.open_osfhandle(duplicate.value, flags)
    except BaseException:
        kernel.CloseHandle(duplicate)
        raise
    binary = os.fdopen(descriptor, mode + "b", buffering=0)
    return io.TextIOWrapper(
        binary,
        encoding="utf-8",
        errors="replace",
        newline=None if mode == "r" else "\n",
        write_through=True,
    )


def configure_standard_streams(require_protocol=False):
    if os.name != "nt":
        return
    missing = []
    for name, number, mode in (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w")):
        if getattr(sys, name) is not None:
            continue
        stream = _inherited_stream(number, mode)
        if stream is None:
            missing.append(name)
            stream = open(os.devnull, mode, encoding="utf-8")
        setattr(sys, name, stream)
    if require_protocol and any(name in missing for name in ("stdin", "stdout")):
        raise RuntimeError("MCP stdio requires inherited input/output pipes.")


def pe_subsystem(path: Path) -> int:
    with path.open("rb") as binary:
        if binary.read(2) != b"MZ":
            raise ValueError("Expected a Windows executable.")
        binary.seek(0x3C)
        offset = struct.unpack("<I", binary.read(4))[0]
        binary.seek(offset)
        if binary.read(4) != b"PE\0\0":
            raise ValueError("Invalid PE header.")
        binary.seek(offset + 24 + 68)
        return struct.unpack("<H", binary.read(2))[0]


def console_attached() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    return bool(kernel.GetConsoleWindow())
