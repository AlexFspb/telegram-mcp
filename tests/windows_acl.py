"""Read a Windows DACL directly, without PowerShell modules or screen access."""

import ctypes
from ctypes import wintypes


def read_acl(path):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        pointer,
        pointer,
        ctypes.POINTER(pointer),
        pointer,
        ctypes.POINTER(pointer),
    )
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = (
        pointer,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi.GetAce.argtypes = (pointer, wintypes.DWORD, ctypes.POINTER(pointer))
    advapi.ConvertSidToStringSidW.argtypes = (pointer, ctypes.POINTER(wintypes.LPWSTR))
    kernel.LocalFree.argtypes = (pointer,)
    kernel.LocalFree.restype = pointer
    descriptor, dacl = pointer(), pointer()
    status = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if status:
        raise ctypes.WinError(status)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # ACL header: revision, padding, size, AceCount, padding.
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        identities = []
        for index in range(count):
            ace = pointer()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            assert ctypes.c_ubyte.from_address(ace.value).value == 0  # ACCESS_ALLOWED_ACE
            text = wintypes.LPWSTR()
            if not advapi.ConvertSidToStringSidW(pointer(ace.value + 8), ctypes.byref(text)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                identities.append(text.value)
            finally:
                kernel.LocalFree(ctypes.cast(text, pointer))
        return {"protected": bool(control.value & 0x1000), "ids": identities}
    finally:
        kernel.LocalFree(descriptor)
