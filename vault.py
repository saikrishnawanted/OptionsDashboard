"""Windows DPAPI or Linux Fernet credential encryption; never plaintext secrets."""
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise ValueError("Credential storage requires Windows DPAPI.")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    operation = library.CryptUnprotectData if decrypt else library.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError("Windows could not unlock the saved credentials for this account.")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(result.data)


class Vault:
    def __init__(self, path: Path):
        self.path = path

    def save(self, values):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self.encrypt(json.dumps(values).encode())
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(self.path)

    def read(self):
        if not self.path.exists():
            return {}
        raw = self.path.read_bytes()
        return json.loads(crypt(raw, decrypt=True) if os.name == 'nt' else self.cipher(create=False).decrypt(raw))

    def encrypt(self, raw):
        return crypt(raw) if os.name == 'nt' else self.cipher(create=True).encrypt(raw)

    def cipher(self, create=False):
        from cryptography.fernet import Fernet
        key_path = self.path.parent / 'vault.key'
        if create and not key_path.exists():
            # Exclusive creation prevents concurrent setup from replacing an existing key.
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as target:
                target.write(Fernet.generate_key())
        if key_path.stat().st_mode & 0o077:
            raise ValueError('Linux vault.key must be readable only by its owner (chmod 600).')
        return Fernet(key_path.read_bytes())
