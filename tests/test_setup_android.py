"""`phonemirror setup android` against a fake adb that simulates the phone's settings."""

import contextlib
import io
import tempfile
import unittest

from phonemirror import setup_android as S
from phonemirror.adb import AdbError
from phonemirror.link import pairing as P

OTHER = "com.example.reader/com.example.reader.ReaderService"


class FakeAdb:
    def __init__(self, enabled="null", install_result="Success"):
        self.serial = "FAKE"
        self.secure = {"enabled_accessibility_services": enabled, "accessibility_enabled": "0"}
        self.install_result = install_result
        self.commands = []

    def devices(self):
        return [("FAKE", "device", "Pixel")]

    def run(self, *args, check=True, timeout=30):
        self.commands.append(args)
        if args[:4] == ("shell", "settings", "get", "secure"):
            return self.secure.get(args[4], "null") + "\n"
        if args[:4] == ("shell", "settings", "put", "secure"):
            self.secure[args[4]] = args[5]
            return ""
        if args[:4] == ("shell", "settings", "delete", "secure"):
            self.secure.pop(args[4], None)
            return ""
        if args[0] == "install":
            return self.install_result
        if args[:3] == ("shell", "dumpsys", "package"):
            return ""
        return ""


class SetupAndroidTest(unittest.TestCase):
    def quiet(self):
        return contextlib.redirect_stdout(io.StringIO())

    def test_enable_keeps_other_services(self):
        adb = FakeAdb(enabled=OTHER)
        with self.quiet():
            S.enable_control(adb)
        self.assertEqual(adb.secure["enabled_accessibility_services"], f"{OTHER}:{S.SERVICE}")
        self.assertEqual(adb.secure["accessibility_enabled"], "1")

    def test_enable_is_idempotent(self):
        adb = FakeAdb(enabled=f"{OTHER}:{S.SERVICE_SHORT}")
        with self.quiet():
            S.enable_control(adb)
        self.assertFalse(any(c[:3] == ("shell", "settings", "put") for c in adb.commands))

    def test_enable_from_empty(self):
        adb = FakeAdb(enabled="null")
        with self.quiet():
            S.enable_control(adb)
        self.assertEqual(adb.secure["enabled_accessibility_services"], S.SERVICE)

    def test_disable_only_removes_ours(self):
        adb = FakeAdb(enabled=f"{OTHER}:{S.SERVICE}")
        with self.quiet():
            S.disable_control(adb)
        self.assertEqual(adb.secure["enabled_accessibility_services"], OTHER)
        self.assertEqual(adb.secure["accessibility_enabled"], "0")  # untouched from the fake's start
        adb = FakeAdb(enabled=S.SERVICE)
        adb.secure["accessibility_enabled"] = "1"
        with self.quiet():
            S.disable_control(adb)
        self.assertNotIn("enabled_accessibility_services", adb.secure)
        self.assertEqual(adb.secure["accessibility_enabled"], "0")

    def test_install_errors(self):
        with self.assertRaises(AdbError):
            S.install(FakeAdb(install_result="Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]"), "x.apk")
        with self.quiet():
            S.install(FakeAdb(install_result="Failure [INSTALL_FAILED_VERSION_DOWNGRADE]"), "x.apk")

    def test_pairing_is_stored_and_sent(self):
        adb = FakeAdb()
        with tempfile.TemporaryDirectory() as d, self.quiet():
            store = P.PairingStore(d)
            S.send_pairing(adb, store, 27199)
            (stored,) = store.pairings()
        start = [c for c in adb.commands if c[:3] == ("shell", "am", "start")][0]
        uri = start[-1]
        self.assertTrue(uri.startswith("'phonemirror://pair?") and uri.endswith("'"))
        parsed = P.parse_pairing_uri(uri.strip("'"))
        self.assertEqual(parsed["pairing"].key, stored.key)
        self.assertIn(f"{S.PACKAGE}.PAIR", start)


if __name__ == "__main__":
    unittest.main()
