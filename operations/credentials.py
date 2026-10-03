"""当前 Windows 用户的 AI 凭据存储。 / AI credential storage bound to the current Windows user."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import tempfile

from django.conf import settings


KEY_FILENAME = "ai-key.dpapi"
ENV_KEY_NAME = "QINGLIAN_AI_API_KEY"
FILE_MARKER = b"QINGLIAN-DPAPI-1\x00"
PAYLOAD_MARKER = b"QINGLIAN-AI-KEY-1\x00"
MAX_STORED_BYTES = 64 * 1024

ERROR_MESSAGES = {
    "unsupported_platform": "当前系统不支持本机加密保存，请改用 QINGLIAN_AI_API_KEY 环境变量。",
    "invalid_key": "AI 访问凭据格式无效，请重新填写。",
    "encrypt_failed": "无法安全加密 AI 凭据，未保存明文。",
    "decrypt_failed": "无法解密本机 AI 凭据，请由原 Windows 用户使用或重新配置。",
    "invalid_store": "本机 AI 凭据文件无效或已损坏，请重新配置。",
    "storage_failed": "无法访问本机 AI 凭据存储，请检查本地文件权限。",
}


class CredentialError(Exception):
    """不携带系统异常或凭据原文。 / Never include raw system errors or credential values."""

    def __init__(self, code):
        self.code = code if code in ERROR_MESSAGES else "storage_failed"
        self.message = ERROR_MESSAGES[self.code]
        super().__init__(self.message)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _is_windows():
    return sys.platform == "win32"


def _validate_key(key):
    if (
        not isinstance(key, str)
        or not key
        or len(key) > 4096
        or any(ord(char) < 33 or ord(char) > 126 for char in key)
    ):
        raise CredentialError("invalid_key")
    return key


def _key_path(data_dir=None):
    try:
        directory = Path(settings.DATA_DIR if data_dir is None else data_dir)
        path = directory / KEY_FILENAME
        if path.is_symlink():
            raise CredentialError("storage_failed")
        return path
    except (OSError, TypeError, ValueError):
        raise CredentialError("storage_failed") from None


def _dpapi_transform(data, *, protect):
    if not _is_windows():
        raise CredentialError("unsupported_platform")
    error_code = "encrypt_failed" if protect else "decrypt_failed"
    input_buffer = ctypes.create_string_buffer(data, len(data))
    input_blob = _DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    local_free = None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p if protect else ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        # 仅当前用户、禁止交互；不使用 LOCAL_MACHINE 标志。 / Current user only, no UI, never LOCAL_MACHINE.
        succeeded = function(
            ctypes.byref(input_blob), None, None, None, None, 0x01, ctypes.byref(output_blob)
        )
        if not succeeded or not output_blob.pbData or not 0 < output_blob.cbData <= MAX_STORED_BYTES:
            raise CredentialError(error_code)
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    except CredentialError:
        raise
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        raise CredentialError(error_code) from None
    finally:
        ctypes.memset(input_buffer, 0, len(data))
        if output_blob.pbData and local_free is not None:
            # 在释放 DPAPI 返回缓冲区前清空内容。 / Wipe the DPAPI output buffer before freeing it.
            if 0 < output_blob.cbData <= MAX_STORED_BYTES:
                ctypes.memset(output_blob.pbData, 0, output_blob.cbData)
            local_free(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def save_key(key, *, data_dir=None):
    """原子保存 DPAPI 密文，不保存明文副本。 / Atomically save DPAPI ciphertext with no plaintext file."""
    if not _is_windows():
        raise CredentialError("unsupported_platform")
    validated = _validate_key(key)
    encrypted = _dpapi_transform(PAYLOAD_MARKER + validated.encode("ascii"), protect=True)
    path = _key_path(data_dir)
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件也只包含密文，替换失败时保留旧凭据。 / The temporary file is encrypted; failed replacement preserves the old key.
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".ai-key-", suffix=".tmp", mode="wb", delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(FILE_MARKER + encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except (OSError, ValueError):
        raise CredentialError("storage_failed") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # 失败残留仅为密文，不将文件路径或系统错误写进日志。 / Any leftover is ciphertext; do not log paths or raw errors.
                pass


def get_key(*, data_dir=None):
    """优先环境变量；没有本机文件时返回空串。 / Prefer the environment; return empty when no stored file exists."""
    environment_key = os.environ.get(ENV_KEY_NAME, "")
    if environment_key:
        return _validate_key(environment_key)
    path = _key_path(data_dir)
    try:
        with path.open("rb") as stream:
            stored = stream.read(MAX_STORED_BYTES + len(FILE_MARKER) + 1)
    except FileNotFoundError:
        return ""
    except OSError:
        raise CredentialError("storage_failed") from None
    if not _is_windows():
        raise CredentialError("unsupported_platform")
    if (
        not stored.startswith(FILE_MARKER)
        or len(stored) <= len(FILE_MARKER)
        or len(stored) > MAX_STORED_BYTES + len(FILE_MARKER)
    ):
        raise CredentialError("invalid_store")
    decoded = _dpapi_transform(stored[len(FILE_MARKER):], protect=False)
    if not decoded.startswith(PAYLOAD_MARKER):
        raise CredentialError("invalid_store")
    try:
        key = decoded[len(PAYLOAD_MARKER):].decode("ascii")
    except UnicodeError:
        raise CredentialError("invalid_store") from None
    return _validate_key(key)


def clear_key(*, data_dir=None):
    """只清除指定存储的密文文件，不触碰环境变量。 / Remove only this store's ciphertext, never the environment variable."""
    path = _key_path(data_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise CredentialError("storage_failed") from None
