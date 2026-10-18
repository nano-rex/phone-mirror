"""Runs AndroidBackend against a fake scrcpy server speaking the 5.0.1 protocol."""

import socket
import struct
import threading
import time
import unittest
from fractions import Fraction

from phonemirror.android import protocol as P
from phonemirror.android.client import AndroidBackend, VideoStream, parse_bit_rate, recv_exact

try:
    import av
    import numpy as np
except ImportError:  # pragma: no cover
    av = None

W, H = 64, 128


def encode_frames(count=5):
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = W, H, "yuv420p"
    enc.time_base = Fraction(1, 30)
    enc.options = {"preset": "ultrafast", "tune": "zerolatency"}
    rgb = np.zeros((H, W, 3), dtype=np.uint8)
    rgb[:, :, 0] = 255  # red
    packets = []
    for i in range(count):
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        packets += [bytes(p) for p in enc.encode(frame)]
    packets += [bytes(p) for p in enc.encode(None)]
    return packets


class FakeProcess:
    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class FakeServer:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(2)
        self.port = self.listener.getsockname()[1]
        self.control = None
        self.ready = threading.Event()

    def serve(self, packets):
        video, _ = self.listener.accept()
        video.sendall(b"\0")  # dummy byte on the first socket
        self.control, _ = self.listener.accept()
        self.ready.set()
        video.sendall(b"Fake Phone".ljust(P.DEVICE_NAME_FIELD_LENGTH, b"\0"))
        video.sendall(b"h264")
        video.sendall(b"\x80\x00\x00\x00" + struct.pack(">II", W, H))
        for i, data in enumerate(packets):
            video.sendall(struct.pack(">QI", i, len(data)) + data)
        self.video = video


class FakeAdb:
    def __init__(self, server):
        self.server = server
        self.serial = None
        self.forwarded = []

    def select_device(self):
        self.serial = "FAKE"
        return self.serial

    def push(self, local, remote):
        self.pushed = (local, remote)

    def forward(self, port, remote):
        self.forwarded.append((port, remote))

    def forward_remove(self, port):
        self.forwarded = [f for f in self.forwarded if f[0] != port]

    def popen(self, *args, **kwargs):
        self.command = args
        return FakeProcess()


@unittest.skipIf(av is None, "PyAV/numpy not installed")
class AndroidEndToEndTest(unittest.TestCase):
    def test_stream_decode_and_control(self):
        server = FakeServer()
        threading.Thread(target=server.serve, args=(encode_frames(),), daemon=True).start()

        backend = AndroidBackend(adb_path="adb-not-used")
        backend.adb = FakeAdb(server)
        backend.scid = 0x1234ABCD
        import phonemirror.android.client as client
        orig = client.free_port
        client.free_port = lambda: server.port
        try:
            backend.start()
        finally:
            client.free_port = orig

        self.assertEqual(backend.title, "Fake Phone - phonemirror")
        self.assertEqual(backend.adb.forwarded, [(server.port, "localabstract:scrcpy_1234abcd")])
        cmd = backend.adb.command
        self.assertIn("com.genymobile.scrcpy.Server", cmd)
        self.assertEqual(cmd[cmd.index("com.genymobile.scrcpy.Server") + 1], "5.0.1")
        self.assertIn("scid=1234abcd", cmd)

        deadline = time.monotonic() + 10
        frame = None
        while time.monotonic() < deadline and frame is None:
            frame = backend.latest_frame()
            time.sleep(0.02)
        self.assertIsNotNone(frame, backend.error)
        self.assertEqual((frame.width, frame.height), (W, H))
        r, g, b = frame.rgb[(H // 2 * W + W // 2) * 3:][:3]
        self.assertGreater(r, 200)
        self.assertLess(g, 60)

        server.ready.wait(5)
        backend.touch("down", 0.5, 0.25)
        msg = recv_exact(server.control, 32)
        self.assertEqual(msg, P.inject_touch(P.ACTION_DOWN, 32, 32, W, H, 1.0))
        backend.touch("up", 1.0, 1.0)  # clamped inside the frame
        msg = recv_exact(server.control, 32)
        self.assertEqual(msg, P.inject_touch(P.ACTION_UP, W - 1, H - 1, W, H, 0.0))
        backend.press("back")
        self.assertEqual(recv_exact(server.control, 4), b"\x04\x00\x04\x01")
        backend.text("hi")
        self.assertEqual(recv_exact(server.control, 7), b"\x01\x00\x00\x00\x02hi")

        server.control.sendall(b"\x00\x00\x00\x00\x06copied")
        deadline = time.monotonic() + 5
        text = None
        while text is None and time.monotonic() < deadline:
            text = backend.pop_clipboard()
            time.sleep(0.02)
        self.assertEqual(text, "copied")

        backend.close()
        self.assertEqual(backend.adb.forwarded, [])


class VideoStreamTest(unittest.TestCase):
    def test_config_packet_is_merged(self):
        a, b = socket.socketpair()
        a.sendall(b"\x80\x00\x00\x00" + struct.pack(">II", 10, 20))
        a.sendall(struct.pack(">QI", P.PACKET_FLAG_CONFIG, 3) + b"SPS")
        a.sendall(struct.pack(">QI", P.PACKET_FLAG_KEY_FRAME | 7, 2) + b"K1")
        a.sendall(struct.pack(">QI", 8, 2) + b"P1")
        a.close()
        events = []
        with self.assertRaises(ConnectionError):
            for event in VideoStream(b, "h264").events():
                events.append(event)
        self.assertEqual(events, [("session", (10, 20)),
                                  ("packet", (b"SPSK1", 7, True)),
                                  ("packet", (b"P1", 8, False))])
        b.close()

    def test_bit_rate(self):
        self.assertEqual(parse_bit_rate("8M"), 8_000_000)
        self.assertEqual(parse_bit_rate("500k"), 500_000)
        self.assertEqual(parse_bit_rate(2000000), 2_000_000)


if __name__ == "__main__":
    unittest.main()
