"""iOS backend: mirrors WebDriverAgent's MJPEG stream and sends input through
its HTTP API. WebDriverAgent must already be running on the device (see
README) with its ports 8100 and 9100 reachable from this computer."""

import base64
import json
import math
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..backend import Backend
from .mjpeg import MjpegParser, jpeg_size

TAP_MAX_DISTANCE = 10.0   # points
TAP_MAX_DURATION = 0.35   # seconds
SCROLL_STEP = 80.0        # points per wheel step
SCROLL_IDLE = 0.15        # seconds of wheel silence before the swipe is sent

# Characters XCTest's typeText understands as special keys
SPECIAL_KEYS = {
    "enter": "\n",
    "backspace": "\b",
    "tab": "\t",
    "escape": "\x1b",
    "delete": "\x7f",
    "space": " ",
    "up": "",
    "down": "",
    "left": "",
    "right": "",
}


class WdaError(RuntimeError):
    pass


class WdaClient:
    def __init__(self, url="http://127.0.0.1:8100", timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session_id = None

    def request(self, method, path, body=None, session=True, timeout=None):
        if session:
            if not self.session_id:
                self.create_session()
            path = f"/session/{self.session_id}{path}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise WdaError(f"{method} {path}: HTTP {exc.code} {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise WdaError(f"cannot reach WebDriverAgent at {self.url}: {exc}") from None
        if not payload:
            return None
        result = json.loads(payload)
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, dict) and "error" in value:
            raise WdaError(f"{method} {path}: {value.get('message') or value['error']}")
        return value

    def status(self, timeout=None):
        return self.request("GET", "/status", session=False, timeout=timeout)

    def create_session(self):
        value = self.request("POST", "/session", {"capabilities": {"alwaysMatch": {}}},
                             session=False)
        self.session_id = value["sessionId"]
        return self.session_id

    def window_size(self):
        value = self.request("GET", "/window/size")
        return float(value["width"]), float(value["height"])

    def update_settings(self, **settings):
        return self.request("POST", "/appium/settings", {"settings": settings})

    def tap(self, x, y):
        self.request("POST", "/wda/tap", {"x": x, "y": y})

    def touch_and_hold(self, x, y, duration):
        self.request("POST", "/wda/touchAndHold", {"x": x, "y": y, "duration": duration})

    def perform_path(self, points):
        """points: [(x, y, ms_since_previous), ...] for one finger."""
        x0, y0, _ = points[0]
        steps = [{"type": "pointerMove", "duration": 0, "x": x0, "y": y0},
                 {"type": "pointerDown", "button": 0}]
        for x, y, ms in points[1:]:
            steps.append({"type": "pointerMove", "duration": int(ms), "x": x, "y": y})
        steps.append({"type": "pointerUp", "button": 0})
        self.request("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger1",
            "parameters": {"pointerType": "touch"}, "actions": steps}]})

    def keys(self, text):
        self.request("POST", "/wda/keys", {"value": list(text)})

    def press_button(self, name):
        self.request("POST", "/wda/pressButton", {"name": name})

    def home(self):
        self.request("POST", "/wda/homescreen", {}, session=False)

    def locked(self):
        return bool(self.request("GET", "/wda/locked", session=False))

    def lock(self):
        self.request("POST", "/wda/lock", {}, session=False)

    def unlock(self):
        self.request("POST", "/wda/unlock", {}, session=False)

    def set_pasteboard(self, text):
        self.request("POST", "/wda/setPasteboard", {
            "content": base64.b64encode(text.encode()).decode(), "contentType": "plaintext"},
            session=False)


def gesture_to_points(samples, size_pt):
    """Turn [(t, nx, ny), ...] into a WDA gesture.

    Returns ("tap", x, y), ("hold", x, y, seconds) or ("path", [(x, y, ms), ...]).
    """
    w, h = size_pt
    pts = [(t, nx * w, ny * h) for t, nx, ny in samples]
    t0, x0, y0 = pts[0]
    t1, x1, y1 = pts[-1]
    travel = max(math.hypot(x - x0, y - y0) for _, x, y in pts)
    duration = t1 - t0
    if travel <= TAP_MAX_DISTANCE:
        if duration <= TAP_MAX_DURATION:
            return ("tap", x0, y0)
        return ("hold", x0, y0, round(duration, 2))
    path = [(x0, y0, 0)]
    last_t = t0
    for t, x, y in pts[1:]:
        path.append((round(x, 1), round(y, 1), max(int((t - last_t) * 1000), 10)))
        last_t = t
    return ("path", path)


def _start_forwarders(udid, ports):
    """Start iproxy (libimobiledevice) or go-ios port forwarding if available."""
    procs = []
    iproxy = shutil.which("iproxy")
    go_ios = shutil.which("ios")
    for local, remote in ports:
        if iproxy:
            cmd = [iproxy, f"{local}:{remote}"] + (["-u", udid] if udid else [])
        elif go_ios:
            cmd = [go_ios, "forward", str(local), str(remote)] + ([f"--udid={udid}"] if udid else [])
        else:
            break
        procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return procs


class IosBackend(Backend):
    buttons = (("Home", "home"), ("Vol-", "volume_down"), ("Vol+", "volume_up"),
               ("Lock", "power"))

    def __init__(self, wda_url="http://127.0.0.1:8100", mjpeg_url=None, udid=None,
                 forward=True, fps=30, scale=50, quality=60):
        super().__init__()
        self.wda = WdaClient(wda_url)
        parsed = urllib.parse.urlsplit(wda_url)
        self.mjpeg_url = mjpeg_url or f"{parsed.scheme}://{parsed.hostname}:9100"
        self.udid = udid
        self.forward = forward and parsed.hostname in ("127.0.0.1", "localhost")
        self.fps = fps
        self.scale = scale
        self.quality = quality
        self.size_pt = None
        self._forwarders = []
        self._jobs = queue.Queue()
        self._gesture = None
        self._scroll = None  # [nx, ny, dx, dy, last_time]
        self._scroll_lock = threading.Lock()

    def start(self):
        try:
            self.wda.status(timeout=3)
        except WdaError:
            if not self.forward:
                raise
            self._forwarders = _start_forwarders(
                self.udid, [(urllib.parse.urlsplit(self.wda.url).port or 8100, 8100),
                            (urllib.parse.urlsplit(self.mjpeg_url).port or 9100, 9100)])
            if not self._forwarders:
                raise WdaError(
                    f"WebDriverAgent is not reachable at {self.wda.url} and neither iproxy "
                    "nor go-ios was found to forward its ports. See README (iOS setup).")
            self._wait_for_wda()
        status = self.wda.status() or {}
        device = (status.get("device") or "").replace("_", " ")
        self.title = f"{device or 'iOS device'} - phonemirror"
        self.wda.create_session()
        try:
            self.wda.update_settings(mjpegServerFramerate=self.fps, mjpegScalingFactor=self.scale,
                                     mjpegServerScreenshotQuality=self.quality)
        except WdaError:
            pass  # older WDA builds: keep their defaults
        self.size_pt = self.wda.window_size()
        threading.Thread(target=self._mjpeg_loop, daemon=True).start()
        threading.Thread(target=self._job_loop, daemon=True).start()
        threading.Thread(target=self._scroll_loop, daemon=True).start()

    def _wait_for_wda(self, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.wda.status(timeout=2)
                return
            except WdaError:
                if time.monotonic() > deadline:
                    self.close()
                    raise WdaError(
                        f"WebDriverAgent did not answer at {self.wda.url}. Is it running on "
                        "the device? See README (iOS setup).") from None
                time.sleep(0.5)

    def close(self):
        super().close()
        for proc in self._forwarders:
            proc.terminate()
        self._forwarders = []

    # threads ------------------------------------------------------------
    def _mjpeg_loop(self):
        while not self.closed.is_set():
            try:
                with urllib.request.urlopen(self.mjpeg_url, timeout=10) as resp:
                    parser = MjpegParser()
                    while not self.closed.is_set():
                        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                        if not chunk:
                            break
                        for image in parser.feed(chunk):
                            size = jpeg_size(image)
                            if size:
                                self._check_orientation(size)
                                self._publish(size[0], size[1], jpeg=image)
            except OSError:
                pass
            if not self.closed.is_set():
                time.sleep(1)  # WDA restarts the MJPEG server on rotation

    def _check_orientation(self, size):
        if self.size_pt and (size[0] > size[1]) != (self.size_pt[0] > self.size_pt[1]):
            self._jobs.put(("refresh_size",))

    def _job_loop(self):
        while not self.closed.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_job(job)
            except WdaError as exc:
                print(f"phonemirror: {exc}")

    def _run_job(self, job):
        kind = job[0]
        if kind == "refresh_size":
            self.size_pt = self.wda.window_size()
        elif kind == "gesture":
            g = gesture_to_points(job[1], self.size_pt)
            if g[0] == "tap":
                self.wda.tap(g[1], g[2])
            elif g[0] == "hold":
                self.wda.touch_and_hold(g[1], g[2], g[3])
            else:
                self.wda.perform_path(g[1])
        elif kind == "call":
            job[1](*job[2:])

    def _scroll_loop(self):
        while not self.closed.is_set():
            time.sleep(0.05)
            with self._scroll_lock:
                s = self._scroll
                if not s or time.monotonic() - s[4] < SCROLL_IDLE:
                    continue
                self._scroll = None
            w, h = self.size_pt
            x, y = s[0] * w, s[1] * h
            # wheel away from the user (dy > 0) reveals content above: finger
            # moves down; wheel right (dx > 0) reveals content on the right
            tx = min(max(x - s[2] * SCROLL_STEP, 1), w - 1)
            ty = min(max(y + s[3] * SCROLL_STEP, 1), h - 1)
            self._jobs.put(("call", self.wda.perform_path, [(x, y, 0), (tx, ty, 250)]))

    # input --------------------------------------------------------------
    def touch(self, action, nx, ny):
        now = time.monotonic()
        if action == "down":
            self._gesture = [(now, nx, ny)]
        elif self._gesture is not None:
            if action == "move":
                if now - self._gesture[-1][0] >= 0.03:
                    self._gesture.append((now, nx, ny))
            else:
                self._gesture.append((now, nx, ny))
                self._jobs.put(("gesture", self._gesture))
                self._gesture = None

    def scroll(self, nx, ny, dx, dy):
        with self._scroll_lock:
            if self._scroll is None:
                self._scroll = [nx, ny, 0.0, 0.0, 0.0]
            self._scroll[2] += dx
            self._scroll[3] += dy
            self._scroll[4] = time.monotonic()

    def press(self, name, shift=False, ctrl=False, alt=False):
        if name == "home":
            self._jobs.put(("call", self.wda.home))
        elif name in ("volume_up", "volume_down"):
            self._jobs.put(("call", self.wda.press_button,
                            "volumeUp" if name == "volume_up" else "volumeDown"))
        elif name == "power":
            self._jobs.put(("call", self._toggle_lock))
        elif name in SPECIAL_KEYS:
            self._jobs.put(("call", self.wda.keys, SPECIAL_KEYS[name]))

    def _toggle_lock(self):
        if self.wda.locked():
            self.wda.unlock()
        else:
            self.wda.lock()

    def text(self, text):
        self._jobs.put(("call", self.wda.keys, text))

    def paste(self, text):
        # Typing is what reliably reaches the focused field; also update the
        # device pasteboard so the text can be pasted again on the phone.
        self._jobs.put(("call", self.wda.set_pasteboard, text))
        self._jobs.put(("call", self.wda.keys, text))
