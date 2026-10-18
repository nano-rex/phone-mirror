"""Interface shared by the Android and iOS backends and the viewer."""

import threading
from dataclasses import dataclass


@dataclass
class Frame:
    seq: int
    width: int
    height: int
    rgb: bytes = None   # packed RGB24, width * height * 3 bytes
    jpeg: bytes = None  # encoded JPEG (iOS MJPEG stream)


class Backend:
    """Base class. Coordinates passed in are normalised to 0..1 of the frame."""

    title = "phonemirror"
    # Toolbar buttons the backend can handle, as (label, key name) pairs.
    buttons = ()
    # Pairing link to show as a QR code while no video is available.
    qr_uri = None

    def __init__(self):
        self._frame = None
        self._frame_lock = threading.Lock()
        self._seq = 0
        self.error = None
        self.closed = threading.Event()

    # frames -------------------------------------------------------------
    def _publish(self, width, height, rgb=None, jpeg=None):
        with self._frame_lock:
            self._seq += 1
            self._frame = Frame(self._seq, width, height, rgb, jpeg)

    def latest_frame(self):
        with self._frame_lock:
            return self._frame

    def waiting_lines(self):
        """Text shown in the window while no video is available."""
        return ["Waiting for video..."]

    def _fail(self, message):
        if self.error is None:
            self.error = message
        self.closed.set()

    # lifecycle ----------------------------------------------------------
    def start(self):
        raise NotImplementedError

    def close(self):
        self.closed.set()

    # input --------------------------------------------------------------
    def touch(self, action, nx, ny):
        """action is "down", "move" or "up"."""

    def scroll(self, nx, ny, dx, dy):
        """dx/dy are wheel steps; positive dy scrolls content up (wheel away)."""

    def press(self, name, shift=False, ctrl=False, alt=False):
        """Press and release a named key (see android/keycodes.py for names)."""

    def text(self, text):
        """Type printable text."""

    def paste(self, text):
        """Put text on the device clipboard and paste it."""

    def command(self, name):
        """Device action without a key: notifications, settings, collapse,
        rotate, display_off, display_on, copy, cut."""

    def pop_clipboard(self):
        """Return new device clipboard text, if the device reported one."""
        return None
