"""Only environment secrets in CI; Windows DPAPI for the existing user's desktop key."""
import ctypes
import os
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def decrypt_dpapi(data: bytes) -> str:
    if os.name != "nt":
        return ""
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_int
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("本机密钥无法解密")
    try:
        return ctypes.string_at(target.data, target.size).decode()
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def api_key() -> str:
    value = os.getenv("ZHIPU_API_KEY", "") or os.getenv("REPO_LLM_API_KEY", "")
    if value:
        return value.strip()
    if os.name == "nt":
        p = Path(os.environ["LOCALAPPDATA"]) / "BrainsongTodayAgent" / "zhipu-key.dpapi"
        if p.exists():
            return decrypt_dpapi(p.read_bytes()).strip()
    return ""
