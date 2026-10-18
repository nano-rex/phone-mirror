"""Backend for the phonemirror Android app over Wi-Fi (no adb needed)."""

import json
import socket
import struct
import threading

from ..backend import Backend
from . import pairing as pairing_mod
from .secure import HandshakeError, server_handshake

# phone -> computer
MSG_INFO = 0x01
MSG_VIDEO_SESSION = 0x02
MSG_VIDEO_PACKET = 0x03
MSG_STATUS = 0x05
# computer -> phone
MSG_TOUCH = 0x10
MSG_SCROLL = 0x11
MSG_KEY = 0x12
MSG_TEXT = 0x13
MSG_CLIPBOARD = 0x14

FLAG_CONFIG = 1
FLAG_KEY = 2
TOUCH_ACTIONS = {"down": 0, "up": 1, "move": 2}
HANDSHAKE_TIMEOUT = 10


def touch_message(action, nx, ny):
    return struct.pack(">BBff", MSG_TOUCH, TOUCH_ACTIONS[action], nx, ny)


def scroll_message(nx, ny, dx, dy):
    return struct.pack(">Bffff", MSG_SCROLL, nx, ny, dx, dy)


class AppBackend(Backend):
    buttons = (("Back", "back"), ("Home", "home"), ("Recents", "recents"),
               ("Vol-", "volume_down"), ("Vol+", "volume_up"), ("Lock", "power"),
               ("Notif", "notifications"))

    def __init__(self, port=pairing_mod.DEFAULT_PORT, store=None, advertise=True, bind="0.0.0.0"):
        super().__init__()
        self.port = port
        self.bind = bind
        self.store = store or pairing_mod.PairingStore()
        self.advertise = advertise
        self.listener = None
        self.channel = None
        self.video_size = None
        self.control_available = False
        self.status = ""
        self._session_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._zeroconf = None
        self._new_pending()
        self.title = "phonemirror - waiting for phone"

    # pairing ------------------------------------------------------------
    def _new_pending(self):
        self.pending = pairing_mod.new_pairing()
        hosts = pairing_mod.local_addresses()
        self.qr_uri = pairing_mod.pairing_uri(self.pending, self.store.pc_id,
                                              pairing_mod.computer_name(), hosts, self.port)

    def _lookup(self, pair_id):
        known = self.store.get(pair_id)
        if known:
            return known.key
        if pair_id == self.pending.pair_id:
            return self.pending.key
        return None

    def waiting_lines(self):
        lines = []
        if self.status:
            lines.append(self.status)
        if self.store.pairings():
            lines.append("Open phonemirror on the phone and tap Start mirroring.")
            lines.append("New phone? Tap \"Scan the code\" in its phonemirror app.")
        else:
            lines.append("Open the phonemirror app on the phone,")
            lines.append("tap \"Scan the code\" and point it here.")
        return lines

    # lifecycle ----------------------------------------------------------
    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: refuse port sharing
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind, self.port))
        except OSError as exc:
            sock.close()
            raise OSError(f"cannot listen on port {self.port} ({exc}). Is phonemirror "
                          "already running?") from None
        sock.listen(4)
        self.listener = sock
        self.port = sock.getsockname()[1]
        self._new_pending()
        threading.Thread(target=self._accept_loop, daemon=True).start()
        if self.advertise:
            self._start_mdns()

    def _start_mdns(self):
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            return
        try:
            addresses = [socket.inet_aton(a) for a in pairing_mod.local_addresses()]
            name = pairing_mod.computer_name()
            info = ServiceInfo("_phonemirror._tcp.local.",
                               f"{name}-{self.store.pc_id[:6]}._phonemirror._tcp.local.",
                               addresses=addresses, port=self.port,
                               properties={"pc": self.store.pc_id, "v": "1"})
            zc = Zeroconf()
            zc.register_service(info)
            self._zeroconf = (zc, info)
        except Exception:
            self._zeroconf = None  # discovery is a convenience; stored addresses still work

    def close(self):
        super().close()
        if self.listener:
            try:
                self.listener.close()
            except OSError:
                pass
        if self.channel:
            try:
                self.channel.sock.close()
            except OSError:
                pass
        if self._zeroconf:
            zc, info = self._zeroconf
            try:
                zc.unregister_service(info)
                zc.close()
            except Exception:
                pass
            self._zeroconf = None

    # networking ---------------------------------------------------------
    def _accept_loop(self):
        while not self.closed.is_set():
            try:
                conn, addr = self.listener.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn, addr):
        conn.settimeout(HANDSHAKE_TIMEOUT)
        try:
            channel, pair_id = server_handshake(conn, self._lookup)
        except (HandshakeError, ConnectionError, OSError, ValueError) as exc:
            if isinstance(exc, HandshakeError):
                self.status = f"Rejected a connection from {addr[0]}: {exc}"
            conn.close()
            return
        if not self._session_lock.acquire(blocking=False):
            conn.close()  # one phone at a time
            return
        try:
            if pair_id == self.pending.pair_id:
                self.store.add(self.pending)
                self._new_pending()
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.channel = channel
            self.status = ""
            self._session(channel, pair_id)
        except (ConnectionError, OSError, ValueError) as exc:
            if not self.closed.is_set():
                self.status = f"Phone disconnected ({exc}). Tap Start mirroring to reconnect."
        finally:
            self.channel = None
            self.video_size = None
            with self._frame_lock:
                self._frame = None
            self.title = "phonemirror - waiting for phone"
            conn.close()
            self._session_lock.release()

    def _session(self, channel, pair_id):
        import av

        decoder = None
        pending_config = None
        while not self.closed.is_set():
            msg = channel.recv()
            kind, payload = msg[0], msg[1:]
            if kind == MSG_INFO:
                info = json.loads(payload.decode("utf-8"))
                name = str(info.get("name") or info.get("model") or "Android phone")[:64]
                self.title = f"{name} - phonemirror"
                self.control_available = bool(info.get("control"))
                self.store.set_name(pair_id, name)
            elif kind == MSG_STATUS:
                info = json.loads(payload.decode("utf-8"))
                self.control_available = bool(info.get("control", self.control_available))
                self.status = str(info.get("message", ""))[:200]
            elif kind == MSG_VIDEO_SESSION:
                self.video_size = struct.unpack(">II", payload[:8])
                decoder = av.CodecContext.create("h264", "r")
                pending_config = None
            elif kind == MSG_VIDEO_PACKET and decoder is not None:
                flags, pts = struct.unpack(">BQ", payload[:9])
                data = payload[9:]
                if flags & FLAG_CONFIG:
                    pending_config = data
                    continue
                if pending_config is not None:
                    data = pending_config + data
                    pending_config = None
                packet = av.Packet(data)
                packet.pts = pts
                try:
                    frames = decoder.decode(packet)
                except av.error.InvalidDataError:
                    continue
                for frame in frames:
                    rgb = frame.to_ndarray(format="rgb24")
                    self._publish(frame.width, frame.height, rgb=rgb.tobytes())

    def _send(self, message):
        channel = self.channel
        if channel is None:
            return
        try:
            with self._send_lock:
                channel.send(message)
        except OSError:
            pass

    # input --------------------------------------------------------------
    def touch(self, action, nx, ny):
        self._send(touch_message(action, nx, ny))

    def scroll(self, nx, ny, dx, dy):
        self._send(scroll_message(nx, ny, dx, dy))

    def press(self, name, shift=False, ctrl=False, alt=False):
        self._send(bytes([MSG_KEY]) + name.encode("utf-8"))

    def text(self, text):
        self._send(bytes([MSG_TEXT]) + text.encode("utf-8"))

    def paste(self, text):
        self._send(bytes([MSG_CLIPBOARD, 1]) + text.encode("utf-8"))

    def command(self, name):
        if name in ("notifications", "settings", "collapse"):
            self.press(name)
