"""IosBackend against a fake WebDriverAgent HTTP API and MJPEG server."""

import io
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from phonemirror.ios.mjpeg import MjpegParser, jpeg_size
from phonemirror.ios.wda import IosBackend, gesture_to_points

try:
    import pygame
except ImportError:  # pragma: no cover
    pygame = None


def make_jpeg(w, h):
    surf = pygame.Surface((w, h))
    surf.fill((0, 0, 255))
    buf = io.BytesIO()
    pygame.image.save(surf, buf, "frame.jpg")
    return buf.getvalue()


class FakeWda(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def reply(self, value):
        body = json.dumps({"value": value, "sessionId": "S1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.calls.append(("GET", self.path, None))
        if self.path == "/status":
            self.reply({"ready": True, "device": "iphone"})
        elif self.path == "/session/S1/window/size":
            self.reply({"width": 390, "height": 844})
        elif self.path == "/wda/locked":
            self.reply(False)
        else:
            self.reply(None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"null")
        self.calls.append(("POST", self.path, body))
        if self.path == "/session":
            self.reply({"sessionId": "S1", "capabilities": {}})
        else:
            self.reply(None)


class FakeMjpeg(BaseHTTPRequestHandler):
    image = b""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=--BoundaryString")
        self.end_headers()
        try:
            for _ in range(50):
                self.wfile.write(b"--BoundaryString\r\nContent-type: image/jpeg\r\n"
                                 + f"Content-Length: {len(self.image)}\r\n\r\n".encode()
                                 + self.image + b"\r\n")
                self.wfile.flush()
                time.sleep(0.02)
        except OSError:
            pass


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class MjpegParserTest(unittest.TestCase):
    def test_split_images_across_chunks(self):
        img1 = b"\xff\xd8abc\xff\xd9"
        img2 = b"\xff\xd8defg\xff\xd9"
        stream = b"--b\r\nContent-Length: 7\r\n\r\n" + img1 + b"\r\n--b\r\n\r\n" + img2
        parser = MjpegParser()
        out = []
        for i in range(0, len(stream), 3):
            out += parser.feed(stream[i:i + 3])
        self.assertEqual(out, [img1, img2])

    @unittest.skipIf(pygame is None, "pygame not installed")
    def test_jpeg_size(self):
        self.assertEqual(jpeg_size(make_jpeg(30, 70)), (30, 70))


class GestureTest(unittest.TestCase):
    def test_tap_hold_and_path(self):
        size = (400, 800)
        self.assertEqual(gesture_to_points([(0, 0.5, 0.5), (0.1, 0.505, 0.5)], size),
                         ("tap", 200, 400))
        self.assertEqual(gesture_to_points([(0, 0.5, 0.5), (1.0, 0.5, 0.5)], size),
                         ("hold", 200, 400, 1.0))
        kind, path = gesture_to_points([(0, 0.5, 0.9), (0.1, 0.5, 0.5), (0.2, 0.5, 0.1)], size)
        self.assertEqual(kind, "path")
        self.assertEqual(path[0], (200, 720, 0))
        self.assertEqual(path[-1], (200, 80, 100))


@unittest.skipIf(pygame is None, "pygame not installed")
class IosBackendTest(unittest.TestCase):
    def test_session_frames_and_input(self):
        FakeWda.calls = []
        FakeMjpeg.image = make_jpeg(195, 422)
        wda = serve(FakeWda)
        mjpeg = serve(FakeMjpeg)
        backend = IosBackend(wda_url=f"http://127.0.0.1:{wda.server_port}",
                             mjpeg_url=f"http://127.0.0.1:{mjpeg.server_port}", forward=False)
        try:
            backend.start()
            self.assertEqual(backend.title, "iphone - phonemirror")
            self.assertEqual(backend.size_pt, (390.0, 844.0))

            deadline = time.monotonic() + 5
            frame = None
            while frame is None and time.monotonic() < deadline:
                frame = backend.latest_frame()
                time.sleep(0.02)
            self.assertIsNotNone(frame)
            self.assertEqual((frame.width, frame.height), (195, 422))

            backend.touch("down", 0.5, 0.5)
            backend.touch("up", 0.5, 0.5)
            backend.press("home")
            backend.press("volume_up")
            backend.text("hey")
            backend.press("backspace")
            backend.scroll(0.5, 0.5, 0, 1)
            time.sleep(0.8)
        finally:
            backend.close()
            wda.shutdown()
            mjpeg.shutdown()

        posts = [(path, body) for method, path, body in FakeWda.calls if method == "POST"]
        self.assertIn(("/session/S1/appium/settings", {"settings": {
            "mjpegServerFramerate": 30, "mjpegScalingFactor": 50,
            "mjpegServerScreenshotQuality": 60}}), posts)
        self.assertIn(("/session/S1/wda/tap", {"x": 195.0, "y": 422.0}), posts)
        self.assertIn(("/wda/homescreen", {}), posts)
        self.assertIn(("/session/S1/wda/pressButton", {"name": "volumeUp"}), posts)
        self.assertIn(("/session/S1/wda/keys", {"value": ["h", "e", "y"]}), posts)
        self.assertIn(("/session/S1/wda/keys", {"value": ["\b"]}), posts)
        actions = [b for p, b in posts if p == "/session/S1/actions"]
        self.assertEqual(len(actions), 1)
        steps = actions[0]["actions"][0]["actions"]
        self.assertEqual(steps[0], {"type": "pointerMove", "duration": 0, "x": 195.0, "y": 422.0})
        self.assertEqual(steps[2]["y"], 422.0 + 80.0)  # wheel away: finger moves down


if __name__ == "__main__":
    unittest.main()
