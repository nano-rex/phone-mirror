"""Android backend: runs the scrcpy server on the device over adb, decodes its
video stream with PyAV and sends input as scrcpy control messages."""

import os
import random
import socket
import threading
import time
from collections import deque

from ..adb import Adb
from ..backend import Backend
from . import protocol as P
from .keycodes import KEYCODES, metastate

DEVICE_SERVER_PATH = "/data/local/tmp/phonemirror-server.jar"
LOCAL_SERVER_PATH = os.path.join(os.path.dirname(__file__), P.SERVER_FILENAME)


def recv_exact(sock, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by the device")
        buf += chunk
    return bytes(buf)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def parse_bit_rate(value):
    """Accept 8000000, "8M" or "800K"."""
    value = str(value).strip().upper()
    scale = 1
    if value.endswith("M"):
        scale, value = 1_000_000, value[:-1]
    elif value.endswith("K"):
        scale, value = 1_000, value[:-1]
    return int(float(value) * scale)


class VideoStream:
    """Reads the scrcpy video socket and yields stream events.

    Events: ("session", (w, h)) and ("packet", (data, pts, key)). Config
    packets are merged into the following media packet, as scrcpy does for
    H.264/H.265.
    """

    def __init__(self, sock, codec):
        self.sock = sock
        self.codec = codec

    def events(self):
        merge = self.codec in ("h264", "hevc")
        pending_config = None
        while True:
            header = recv_exact(self.sock, P.PACKET_HEADER_SIZE)
            if P.is_session_header(header):
                yield "session", P.parse_session_header(header)
                continue
            pts, config, key, size = P.parse_packet_header(header)
            if size == 0:
                raise ConnectionError("invalid packet length 0")
            data = recv_exact(self.sock, size)
            if merge and config:
                pending_config = data
                continue
            if pending_config is not None:
                data = pending_config + data
                pending_config = None
            yield "packet", (data, pts, key)


class AndroidBackend(Backend):
    buttons = (("Back", "back"), ("Home", "home"), ("Recents", "recents"),
               ("Vol-", "volume_down"), ("Vol+", "volume_up"), ("Power", "power"),
               ("Rotate", "rotate"), ("Notif", "notifications"))

    def __init__(self, serial=None, max_size=1600, bit_rate="8M", max_fps=60,
                 codec="h264", control=True, stay_awake=False, show_touches=False,
                 turn_screen_off=False, power_off_on_close=False, connect=None,
                 server_path=None, adb_path=None):
        super().__init__()
        self.adb = Adb(serial, adb_path)
        self.connect_address = connect
        self.max_size = max_size
        self.bit_rate = parse_bit_rate(bit_rate)
        self.max_fps = max_fps
        self.codec = codec
        self.control = control
        self.stay_awake = stay_awake
        self.show_touches = show_touches
        self.turn_screen_off = turn_screen_off
        self.power_off_on_close = power_off_on_close
        self.server_path = server_path or LOCAL_SERVER_PATH
        self.scid = random.randrange(0, 0x7FFFFFFF)
        self.port = None
        self.process = None
        self.video_sock = None
        self.control_sock = None
        self.video_size = None  # (w, h) of the current capture session
        self._send_lock = threading.Lock()
        self._clipboard = deque()
        self._clipboard_seq = 0

    # lifecycle ----------------------------------------------------------
    def server_args(self):
        args = [
            f"scid={self.scid:08x}",
            "log_level=info",
            "audio=false",
            "tunnel_forward=true",
            f"video_codec={'h265' if self.codec == 'hevc' else self.codec}",
            f"video_bit_rate={self.bit_rate}",
        ]
        if self.max_size:
            args.append(f"max_size={self.max_size}")
        if self.max_fps:
            args.append(f"max_fps={self.max_fps}")
        if not self.control:
            args.append("control=false")
        if self.stay_awake:
            args.append("stay_awake=true")
        if self.show_touches:
            args.append("show_touches=true")
        if self.power_off_on_close:
            args.append("power_off_on_close=true")
        return args

    def start(self):
        if self.connect_address:
            self.adb.connect(self.connect_address)
            self.adb.serial = self.adb.serial or self.connect_address
        serial = self.adb.select_device()
        if not os.path.exists(self.server_path):
            raise FileNotFoundError(f"scrcpy server not found at {self.server_path}")
        self.adb.push(self.server_path, DEVICE_SERVER_PATH)

        self.port = free_port()
        self.adb.forward(self.port, f"localabstract:scrcpy_{self.scid:08x}")
        cmd = ["shell", f"CLASSPATH={DEVICE_SERVER_PATH}", "app_process", "/",
               "com.genymobile.scrcpy.Server", P.SERVER_VERSION] + self.server_args()
        self.process = self.adb.popen(*cmd)

        try:
            self._connect()
        except Exception:
            self.close()
            raise

        name = P.parse_device_name(recv_exact(self.video_sock, P.DEVICE_NAME_FIELD_LENGTH))
        self.title = f"{name or serial} - phonemirror"
        codec = P.parse_codec_id(recv_exact(self.video_sock, 4))
        threading.Thread(target=self._video_loop, args=(codec,), daemon=True).start()
        if self.control_sock:
            threading.Thread(target=self._device_msg_loop, daemon=True).start()
            if self.turn_screen_off:
                self.command("display_off")

    def _connect(self, attempts=100, delay=0.1):
        # With a forward tunnel, connect() succeeds as soon as adb listens; the
        # server sends a dummy byte once it really accepted the connection.
        for _ in range(attempts):
            if self.process.poll() is not None:
                raise ConnectionError("scrcpy server exited during startup (see output above)")
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                if sock.recv(1):
                    break
            except OSError:
                pass
            sock.close()
            time.sleep(delay)
        else:
            raise ConnectionError("could not connect to the scrcpy server on the device")
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.video_sock = sock
        if self.control:
            ctl = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            ctl.settimeout(None)
            ctl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.control_sock = ctl

    def close(self):
        if self.closed.is_set() and self.process is None:
            return
        super().close()
        for sock in (self.video_sock, self.control_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
        self.process = None
        if self.port:
            self.adb.forward_remove(self.port)
            self.port = None

    # threads ------------------------------------------------------------
    def _video_loop(self, codec):
        import av  # imported here so the protocol code works without PyAV

        decoder = None
        try:
            for kind, value in VideoStream(self.video_sock, codec).events():
                if kind == "session":
                    self.video_size = value
                    decoder = av.CodecContext.create(codec, "r")
                    continue
                data, pts, _key = value
                packet = av.Packet(data)
                packet.pts = pts
                for frame in decoder.decode(packet):
                    rgb = frame.to_ndarray(format="rgb24")
                    self._publish(frame.width, frame.height, rgb=rgb.tobytes())
        except Exception as exc:
            if not self.closed.is_set():
                self._fail(f"video stream ended: {exc}")
        self.closed.set()

    def _device_msg_loop(self):
        parser = P.DeviceMessageParser()
        try:
            while True:
                data = self.control_sock.recv(65536)
                if not data:
                    break
                for kind, value in parser.feed(data):
                    if kind == "clipboard":
                        self._clipboard.append(value)
        except (OSError, ValueError):
            pass

    # input --------------------------------------------------------------
    def _send(self, message):
        if not self.control_sock or self.closed.is_set():
            return
        try:
            with self._send_lock:
                self.control_sock.sendall(message)
        except OSError as exc:
            self._fail(f"control connection lost: {exc}")

    def _point(self, nx, ny):
        w, h = self.video_size
        x = min(max(int(nx * w), 0), w - 1)
        y = min(max(int(ny * h), 0), h - 1)
        return x, y, w, h

    def touch(self, action, nx, ny):
        if not self.video_size:
            return
        code = {"down": P.ACTION_DOWN, "move": P.ACTION_MOVE, "up": P.ACTION_UP}[action]
        x, y, w, h = self._point(nx, ny)
        pressure = 0.0 if action == "up" else 1.0
        self._send(P.inject_touch(code, x, y, w, h, pressure))

    def scroll(self, nx, ny, dx, dy):
        if not self.video_size:
            return
        x, y, w, h = self._point(nx, ny)
        self._send(P.inject_scroll(x, y, w, h, dx, dy))

    def press(self, name, shift=False, ctrl=False, alt=False):
        if name == "back":
            # BACK_OR_SCREEN_ON also wakes the screen when it is off
            self._send(P.back_or_screen_on(P.ACTION_DOWN))
            self._send(P.back_or_screen_on(P.ACTION_UP))
            return
        if name in ("rotate", "notifications", "settings", "collapse"):
            self.command(name)
            return
        keycode = KEYCODES.get(name)
        if keycode is None:
            return
        meta = metastate(shift, alt, ctrl)
        self._send(P.inject_keycode(P.ACTION_DOWN, keycode, 0, meta))
        self._send(P.inject_keycode(P.ACTION_UP, keycode, 0, meta))

    def text(self, text):
        while text:
            chunk = P.truncate_utf8(text, P.INJECT_TEXT_MAX_LENGTH).decode("utf-8")
            if not chunk:
                break
            self._send(P.inject_text(chunk))
            text = text[len(chunk):]

    def paste(self, text):
        self._clipboard_seq += 1
        self._send(P.set_clipboard(self._clipboard_seq, text, paste=True))

    def command(self, name):
        messages = {
            "notifications": P.expand_notification_panel,
            "settings": P.expand_settings_panel,
            "collapse": P.collapse_panels,
            "rotate": P.rotate_device,
            "display_off": lambda: P.set_display_power(False),
            "display_on": lambda: P.set_display_power(True),
            "copy": lambda: P.get_clipboard(P.COPY_KEY_COPY),
            "cut": lambda: P.get_clipboard(P.COPY_KEY_CUT),
        }
        if name in messages:
            self._send(messages[name]())

    def pop_clipboard(self):
        return self._clipboard.popleft() if self._clipboard else None
