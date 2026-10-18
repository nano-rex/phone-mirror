"""Secure link handshake, pairing storage and the Wi-Fi app backend."""

import json
import os
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest

from phonemirror.link import backend as B
from phonemirror.link import pairing as P
from phonemirror.link.secure import (Channel, HandshakeError, client_handshake, derive_keys,
                                     hkdf_expand, server_handshake)


def run_pair(server_lookup, pair_id, psk):
    a, b = socket.socketpair()
    result = {}

    def server():
        try:
            result["server"] = server_handshake(b, server_lookup)
        except Exception as exc:
            result["server_error"] = exc
            b.close()

    t = threading.Thread(target=server)
    t.start()
    try:
        result["client"] = client_handshake(a, pair_id, psk)
    except Exception as exc:
        result["client_error"] = exc
        a.close()
    t.join(5)
    return a, b, result


class HandshakeTest(unittest.TestCase):
    def test_round_trip_and_records(self):
        pid, key = os.urandom(16), os.urandom(32)
        a, b, r = run_pair(lambda p: key if p == pid else None, pid, key)
        client = r["client"]
        server, got_pid = r["server"]
        self.assertEqual(got_pid, pid)
        client.send(b"\x01hello")
        self.assertEqual(server.recv(), b"\x01hello")
        # larger than the socket buffer, so send from another thread
        threading.Thread(target=server.send, args=(b"x" * 100000,)).start()
        self.assertEqual(client.recv(), b"x" * 100000)
        a.close(), b.close()

    def test_wrong_key_is_rejected_by_both_sides(self):
        pid = os.urandom(16)
        a, b, r = run_pair(lambda p: os.urandom(32), pid, os.urandom(32))
        self.assertIsInstance(r.get("client_error"), HandshakeError)
        self.assertIn("server_error", r)

    def test_unknown_pairing(self):
        a, b, r = run_pair(lambda p: None, os.urandom(16), os.urandom(32))
        self.assertIsInstance(r.get("server_error"), HandshakeError)
        self.assertIn("client_error", r)
        a.close()

    def test_tampered_record_closes(self):
        k1, k2 = os.urandom(32), os.urandom(32)
        a, b = socket.socketpair()
        sender, receiver = Channel(a, k1, k2), Channel(b, k2, k1)
        record = bytearray(sender.seal(b"secret"))
        record[-1] ^= 1
        a.sendall(bytes(record))
        with self.assertRaises(ConnectionError):
            receiver.recv()
        a.close(), b.close()

    def test_replayed_record_fails(self):
        k1, k2 = os.urandom(32), os.urandom(32)
        a, b = socket.socketpair()
        sender, receiver = Channel(a, k1, k2), Channel(b, k2, k1)
        record = sender.seal(b"once")
        a.sendall(record + record)
        self.assertEqual(receiver.recv(), b"once")
        with self.assertRaises(ConnectionError):
            receiver.recv()
        a.close(), b.close()

    def test_hkdf_matches_rfc5869_case_1(self):
        import hmac, hashlib
        ikm = bytes([0x0b] * 22)
        salt = bytes(range(13))
        info = bytes(range(0xf0, 0xfa))
        prk = hmac.new(salt, ikm, hashlib.sha256).digest()
        okm = hkdf_expand(prk, info, 42)
        self.assertEqual(okm.hex(), "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56"
                                    "ecc4c5bf34007208d5b887185865")

    def test_key_derivation_vector(self):
        # Fixed vector shared with the Android app's unit test (SecureLinkTest.kt)
        k_c2s, k_s2c = derive_keys(bytes(range(32)), bytes([7] * 32), b"transcript")
        self.assertEqual(k_c2s.hex(), DERIVE_C2S)
        self.assertEqual(k_s2c.hex(), DERIVE_S2C)


DERIVE_C2S = "344d3c27e2d2c2c4ac9078491538aaea3bf2d35f970d910e2b282d8f44c56276"
DERIVE_S2C = "3e7ad84fc4300481b22bab21ba15a9330fa6495c45f23195285cf06972315d41"


class PairingTest(unittest.TestCase):
    def test_uri_round_trip(self):
        p = P.new_pairing()
        uri = P.pairing_uri(p, "ab" * 16, "My PC & co", ["192.168.1.5", "10.0.0.2"], 27199)
        parsed = P.parse_pairing_uri(uri)
        self.assertEqual(parsed["pairing"].pair_id, p.pair_id)
        self.assertEqual(parsed["pairing"].key, p.key)
        self.assertEqual(parsed["name"], "My PC & co")
        self.assertEqual(parsed["hosts"], ["192.168.1.5", "10.0.0.2"])
        with self.assertRaises(ValueError):
            P.parse_pairing_uri("https://example.com/pair?v=1")

    def test_store_is_private_and_persistent(self):
        with tempfile.TemporaryDirectory() as d:
            store = P.PairingStore(d)
            p = P.new_pairing()
            store.add(p)
            store.set_name(p.pair_id, "Pixel")
            again = P.PairingStore(d)
            self.assertEqual(again.get(p.pair_id).key, p.key)
            self.assertEqual(again.get(p.pair_id).name, "Pixel")
            self.assertEqual(again.pc_id, store.pc_id)
            if sys.platform != "win32":
                mode = stat.S_IMODE(os.stat(store.path).st_mode)
                self.assertEqual(mode, 0o600)
            self.assertTrue(again.remove(p.pair_id))
            self.assertIsNone(P.PairingStore(d).get(p.pair_id))


try:
    import av  # noqa: F401
    from tests.test_android_stream import encode_frames
except ImportError:  # pragma: no cover
    av = None


@unittest.skipIf(av is None, "PyAV not installed")
class AppBackendTest(unittest.TestCase):
    def test_pair_stream_and_control(self):
        with tempfile.TemporaryDirectory() as d:
            store = P.PairingStore(d)
            backend = B.AppBackend(port=0, store=store, advertise=False, bind="127.0.0.1")
            backend.start()
            try:
                info = P.parse_pairing_uri(backend.qr_uri)
                self.assertEqual(info["port"], backend.port)
                pairing = info["pairing"]

                sock = socket.create_connection(("127.0.0.1", backend.port))
                ch = client_handshake(sock, pairing.pair_id, pairing.key)
                ch.send(bytes([B.MSG_INFO]) + json.dumps({"name": "Pixel 9", "control": True}).encode())
                ch.send(bytes([B.MSG_VIDEO_SESSION]) + struct.pack(">II", 64, 128))
                for i, data in enumerate(encode_frames()):
                    ch.send(bytes([B.MSG_VIDEO_PACKET]) + struct.pack(">BQ", 0, i) + data)

                deadline = time.monotonic() + 10
                while backend.latest_frame() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                frame = backend.latest_frame()
                self.assertIsNotNone(frame)
                self.assertEqual((frame.width, frame.height), (64, 128))
                self.assertEqual(backend.title, "Pixel 9 - phonemirror")
                # the QR pairing became a stored pairing, and a fresh QR replaced it
                self.assertEqual(store.get(pairing.pair_id).name, "Pixel 9")
                self.assertNotEqual(P.parse_pairing_uri(backend.qr_uri)["pairing"].pair_id,
                                    pairing.pair_id)

                backend.touch("down", 0.25, 0.5)
                self.assertEqual(ch.recv(), struct.pack(">BBff", B.MSG_TOUCH, 0, 0.25, 0.5))
                backend.press("home")
                self.assertEqual(ch.recv(), b"\x12home")
                backend.text("hé")
                self.assertEqual(ch.recv(), b"\x13" + "hé".encode())
                backend.paste("clip")
                self.assertEqual(ch.recv(), b"\x14\x01clip")

                sock.close()
                deadline = time.monotonic() + 5
                while backend.latest_frame() is not None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIsNone(backend.latest_frame())  # back to the waiting screen

                # a phone with an unknown pairing is refused
                sock = socket.create_connection(("127.0.0.1", backend.port))
                with self.assertRaises(ConnectionError):
                    client_handshake(sock, os.urandom(16), os.urandom(32))
                sock.close()
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
