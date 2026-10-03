"""仅在临时目录中使用虚构密钥。 / Use fictional keys exclusively inside temporary directories."""

import os
from pathlib import Path
import sys
import tempfile
import traceback
from unittest import TestCase, skipUnless
from unittest.mock import patch

from django.test import override_settings

from .credentials import (
    CredentialError,
    ENV_KEY_NAME,
    FILE_MARKER,
    KEY_FILENAME,
    MAX_STORED_BYTES,
    PAYLOAD_MARKER,
    clear_key,
    get_key,
    save_key,
)


FICTIONAL_KEY = "fictional-dpapi-test-key-never-an-account-key"


class CredentialTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qinglian-credential-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key_path = self.root / KEY_FILENAME
        self.setting_override = override_settings(DATA_DIR=self.root)
        self.setting_override.enable()
        self.addCleanup(self.setting_override.disable)
        self.environment_override = patch.dict(os.environ, {ENV_KEY_NAME: ""})
        self.environment_override.start()
        self.addCleanup(self.environment_override.stop)

    def assert_safe(self, error, code):
        self.assertEqual(error.code, code)
        self.assertNotIn(FICTIONAL_KEY, str(error))
        self.assertNotIn(FICTIONAL_KEY, repr(error))
        self.assertNotIn(FICTIONAL_KEY, "".join(traceback.format_exception(error)))

    def test_missing_store_returns_empty_without_creating_files(self):
        self.assertEqual(get_key(), "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_environment_takes_priority_without_reading_local_file(self):
        self.key_path.write_bytes(b"unreadable-fictional-ciphertext")
        with patch.dict(os.environ, {ENV_KEY_NAME: FICTIONAL_KEY}), patch(
            "operations.credentials._dpapi_transform"
        ) as transform:
            self.assertEqual(get_key(), FICTIONAL_KEY)
            transform.assert_not_called()

    def test_environment_works_on_nonwindows(self):
        with patch.dict(os.environ, {ENV_KEY_NAME: FICTIONAL_KEY}), patch(
            "operations.credentials._is_windows", return_value=False
        ):
            self.assertEqual(get_key(), FICTIONAL_KEY)
            self.assertFalse(self.key_path.exists())

    def test_nonwindows_save_refuses_plaintext_fallback(self):
        with patch("operations.credentials._is_windows", return_value=False):
            with self.assertRaises(CredentialError) as caught:
                save_key(FICTIONAL_KEY)
        self.assert_safe(caught.exception, "unsupported_platform")
        self.assertFalse(self.key_path.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_nonwindows_cannot_decrypt_windows_store(self):
        self.key_path.write_bytes(FILE_MARKER + b"fictional-encrypted-data")
        with patch("operations.credentials._is_windows", return_value=False):
            with self.assertRaises(CredentialError) as caught:
                get_key()
        self.assert_safe(caught.exception, "unsupported_platform")

    def test_invalid_credentials_are_not_written(self):
        with patch("operations.credentials._is_windows", return_value=True):
            for key in ("", None, "a" * 4097, FICTIONAL_KEY + "\n", "不合法的凭据"):
                with self.subTest(key_type=type(key).__name__):
                    with self.assertRaises(CredentialError) as caught:
                        save_key(key)
                    self.assert_safe(caught.exception, "invalid_key")
        self.assertFalse(self.key_path.exists())

    def test_invalid_environment_key_is_not_silently_used(self):
        with patch.dict(os.environ, {ENV_KEY_NAME: FICTIONAL_KEY + "\n"}):
            with self.assertRaises(CredentialError) as caught:
                get_key()
        self.assert_safe(caught.exception, "invalid_key")

    def test_clear_only_removes_ciphertext_and_keeps_environment(self):
        self.key_path.write_bytes(FILE_MARKER + b"fictional-encrypted-data")
        unrelated = self.root / "unrelated-test-note.txt"
        unrelated.write_text("keep", encoding="utf-8")
        with patch.dict(os.environ, {ENV_KEY_NAME: FICTIONAL_KEY}):
            self.assertTrue(clear_key())
            self.assertEqual(get_key(), FICTIONAL_KEY)
        self.assertFalse(self.key_path.exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(clear_key())

    def test_explicit_directory_does_not_touch_default_store(self):
        self.key_path.write_bytes(b"default-store-untouched")
        explicit = self.root / "explicit"
        explicit.mkdir()
        self.assertEqual(get_key(data_dir=explicit), "")
        self.assertFalse(clear_key(data_dir=explicit))
        self.assertEqual(self.key_path.read_bytes(), b"default-store-untouched")

    def test_invalid_file_header_and_oversized_file_are_rejected(self):
        for stored in (b"raw-plaintext-not-accepted", FILE_MARKER, FILE_MARKER + b"x" * (MAX_STORED_BYTES + 1)):
            with self.subTest(size=len(stored)):
                self.key_path.write_bytes(stored)
                with patch("operations.credentials._is_windows", return_value=True):
                    with self.assertRaises(CredentialError) as caught:
                        get_key()
                self.assert_safe(caught.exception, "invalid_store")

    def test_decrypted_payload_requires_application_marker(self):
        self.key_path.write_bytes(FILE_MARKER + b"fictional-encrypted-data")
        with patch("operations.credentials._is_windows", return_value=True), patch(
            "operations.credentials._dpapi_transform", return_value=b"wrong-payload-format"
        ):
            with self.assertRaises(CredentialError) as caught:
                get_key()
        self.assert_safe(caught.exception, "invalid_store")

    def test_decrypted_payload_requires_valid_encoding(self):
        self.key_path.write_bytes(FILE_MARKER + b"fictional-encrypted-data")
        with patch("operations.credentials._is_windows", return_value=True), patch(
            "operations.credentials._dpapi_transform", return_value=PAYLOAD_MARKER + b"\xff"
        ):
            with self.assertRaises(CredentialError) as caught:
                get_key()
        self.assert_safe(caught.exception, "invalid_store")

    def test_failed_atomic_replace_preserves_old_store(self):
        previous = FILE_MARKER + b"previous-fictional-ciphertext"
        self.key_path.write_bytes(previous)
        with patch("operations.credentials._is_windows", return_value=True), patch(
            "operations.credentials._dpapi_transform", return_value=b"new-fictional-ciphertext"
        ), patch("operations.credentials.os.replace", side_effect=OSError(FICTIONAL_KEY)):
            with self.assertRaises(CredentialError) as caught:
                save_key(FICTIONAL_KEY)
        self.assert_safe(caught.exception, "storage_failed")
        self.assertEqual(self.key_path.read_bytes(), previous)
        self.assertEqual(list(self.root.iterdir()), [self.key_path])

    def test_raw_read_errors_are_not_exposed(self):
        with patch("pathlib.Path.open", side_effect=OSError(FICTIONAL_KEY)):
            with self.assertRaises(CredentialError) as caught:
                get_key()
        self.assert_safe(caught.exception, "storage_failed")

    def test_symlink_target_is_rejected(self):
        with patch("pathlib.Path.is_symlink", return_value=True):
            with self.assertRaises(CredentialError) as caught:
                get_key()
        self.assert_safe(caught.exception, "storage_failed")

    @skipUnless(sys.platform == "win32", "DPAPI 仅在 Windows 本机验证 / DPAPI requires Windows")
    def test_real_dpapi_roundtrip_uses_only_temporary_ciphertext(self):
        save_key(FICTIONAL_KEY)
        stored = self.key_path.read_bytes()
        self.assertTrue(stored.startswith(FILE_MARKER))
        self.assertNotIn(FICTIONAL_KEY.encode(), stored)
        self.assertEqual(get_key(), FICTIONAL_KEY)
        self.assertEqual(list(self.root.iterdir()), [self.key_path])

    @skipUnless(sys.platform == "win32", "DPAPI 仅在 Windows 本机验证 / DPAPI requires Windows")
    def test_replacing_key_keeps_only_latest_ciphertext(self):
        save_key(FICTIONAL_KEY)
        first_ciphertext = self.key_path.read_bytes()
        replacement = "fictional-replacement-test-key"
        save_key(replacement)
        self.assertNotEqual(self.key_path.read_bytes(), first_ciphertext)
        self.assertEqual(get_key(), replacement)
        self.assertNotIn(replacement.encode(), self.key_path.read_bytes())

    @skipUnless(sys.platform == "win32", "DPAPI 仅在 Windows 本机验证 / DPAPI requires Windows")
    def test_corrupted_dpapi_data_is_reported_safely(self):
        self.key_path.write_bytes(FILE_MARKER + b"not-a-real-DPAPI-record")
        with self.assertRaises(CredentialError) as caught:
            get_key()
        self.assert_safe(caught.exception, "decrypt_failed")
