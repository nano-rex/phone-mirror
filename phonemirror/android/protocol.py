"""Wire format of the scrcpy server protocol (server version 5.0.1).

Only pure encoding/decoding lives here so it can be tested without a device.
Layouts follow scrcpy's app/src/control_msg.c, app/src/device_msg.c and
app/src/demuxer.c.
"""

import struct

SERVER_VERSION = "5.0.1"
SERVER_FILENAME = f"scrcpy-server-v{SERVER_VERSION}"
SERVER_SHA256 = "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"

DEVICE_NAME_FIELD_LENGTH = 64
PACKET_HEADER_SIZE = 12
INJECT_TEXT_MAX_LENGTH = 300

# Control message types (client -> device)
TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_BACK_OR_SCREEN_ON = 4
TYPE_EXPAND_NOTIFICATION_PANEL = 5
TYPE_EXPAND_SETTINGS_PANEL = 6
TYPE_COLLAPSE_PANELS = 7
TYPE_GET_CLIPBOARD = 8
TYPE_SET_CLIPBOARD = 9
TYPE_SET_DISPLAY_POWER = 10
TYPE_ROTATE_DEVICE = 11

# Device message types (device -> client)
DEVICE_MSG_CLIPBOARD = 0
DEVICE_MSG_ACK_CLIPBOARD = 1
DEVICE_MSG_UHID_OUTPUT = 2

# Android KeyEvent / MotionEvent actions
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

COPY_KEY_NONE = 0
COPY_KEY_COPY = 1
COPY_KEY_CUT = 2

POINTER_ID_MOUSE = 0xFFFFFFFFFFFFFFFF
POINTER_ID_GENERIC_FINGER = 0xFFFFFFFFFFFFFFFE

PACKET_FLAG_CONFIG = 1 << 62
PACKET_FLAG_KEY_FRAME = 1 << 61
PACKET_PTS_MASK = PACKET_FLAG_KEY_FRAME - 1

CODEC_IDS = {
    0x68323634: "h264",
    0x68323635: "hevc",
    0x00617631: "av1",
    0x00767038: "vp8",
    0x00767039: "vp9",
}


def float_to_u16fp(value):
    value = min(max(value, 0.0), 1.0)
    return min(int(value * 0x10000), 0xFFFF)


def float_to_i16fp(value):
    value = min(max(value, -1.0), 1.0)
    return max(min(int(value * 0x8000), 0x7FFF), -0x8000)


def truncate_utf8(text, max_len):
    """Encode text as UTF-8 and cut it at a character boundary."""
    data = text.encode("utf-8")
    if len(data) <= max_len:
        return data
    end = max_len
    # step back over continuation bytes (10xxxxxx)
    while end > 0 and (data[end] & 0xC0) == 0x80:
        end -= 1
    return data[:end]


def _string(text, max_len):
    data = truncate_utf8(text, max_len)
    return struct.pack(">I", len(data)) + data


def inject_keycode(action, keycode, repeat=0, metastate=0):
    return struct.pack(">BBIII", TYPE_INJECT_KEYCODE, action, keycode, repeat, metastate)


def inject_text(text):
    return bytes([TYPE_INJECT_TEXT]) + _string(text, INJECT_TEXT_MAX_LENGTH)


def inject_touch(action, x, y, width, height, pressure=1.0,
                 pointer_id=POINTER_ID_GENERIC_FINGER, action_button=0, buttons=0):
    return struct.pack(
        ">BBQiiHHHII", TYPE_INJECT_TOUCH_EVENT, action, pointer_id, x, y,
        width, height, float_to_u16fp(pressure), action_button, buttons)


def inject_scroll(x, y, width, height, hscroll, vscroll, buttons=0):
    """hscroll/vscroll are in the range [-16, 16], like the scrcpy client."""
    return struct.pack(
        ">BiiHHhhI", TYPE_INJECT_SCROLL_EVENT, x, y, width, height,
        float_to_i16fp(hscroll / 16), float_to_i16fp(vscroll / 16), buttons)


def back_or_screen_on(action):
    return bytes([TYPE_BACK_OR_SCREEN_ON, action])


def expand_notification_panel():
    return bytes([TYPE_EXPAND_NOTIFICATION_PANEL])


def expand_settings_panel():
    return bytes([TYPE_EXPAND_SETTINGS_PANEL])


def collapse_panels():
    return bytes([TYPE_COLLAPSE_PANELS])


def get_clipboard(copy_key=COPY_KEY_NONE):
    return bytes([TYPE_GET_CLIPBOARD, copy_key])


def set_clipboard(sequence, text, paste):
    # type (1) + sequence (8) + paste (1) + length (4)
    max_len = (1 << 18) - 14
    return struct.pack(">BQB", TYPE_SET_CLIPBOARD, sequence, int(bool(paste))) + _string(text, max_len)


def set_display_power(on):
    return bytes([TYPE_SET_DISPLAY_POWER, int(bool(on))])


def rotate_device():
    return bytes([TYPE_ROTATE_DEVICE])


class DeviceMessageParser:
    """Incremental parser for messages the device sends on the control socket."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf += data
        messages = []
        while self._buf:
            kind = self._buf[0]
            if kind == DEVICE_MSG_CLIPBOARD:
                if len(self._buf) < 5:
                    break
                (length,) = struct.unpack_from(">I", self._buf, 1)
                if len(self._buf) < 5 + length:
                    break
                text = bytes(self._buf[5:5 + length]).decode("utf-8", "replace")
                messages.append(("clipboard", text))
                del self._buf[:5 + length]
            elif kind == DEVICE_MSG_ACK_CLIPBOARD:
                if len(self._buf) < 9:
                    break
                (sequence,) = struct.unpack_from(">Q", self._buf, 1)
                messages.append(("ack_clipboard", sequence))
                del self._buf[:9]
            elif kind == DEVICE_MSG_UHID_OUTPUT:
                if len(self._buf) < 5:
                    break
                uhid_id, size = struct.unpack_from(">HH", self._buf, 1)
                if len(self._buf) < 5 + size:
                    break
                messages.append(("uhid_output", (uhid_id, bytes(self._buf[5:5 + size]))))
                del self._buf[:5 + size]
            else:
                raise ValueError(f"unknown device message type {kind}")
        return messages


def parse_device_name(data):
    return data.split(b"\0", 1)[0].decode("utf-8", "replace")


def parse_codec_id(data):
    (raw,) = struct.unpack(">I", data)
    if raw == 0:
        raise ConnectionError("video stream disabled by the device")
    if raw == 1:
        raise ConnectionError("video stream configuration error on the device")
    try:
        return CODEC_IDS[raw]
    except KeyError:
        raise ValueError(f"unknown codec id 0x{raw:08x}") from None


def is_session_header(header):
    return bool(header[0] & 0x80)


def parse_session_header(header):
    """Return (width, height) from a 12-byte session packet."""
    width, height = struct.unpack_from(">II", header, 4)
    return width, height


def parse_packet_header(header):
    """Return (pts or None, is_config, is_key_frame, size) for a media packet."""
    pts_flags, size = struct.unpack(">QI", header)
    config = bool(pts_flags & PACKET_FLAG_CONFIG)
    key = bool(pts_flags & PACKET_FLAG_KEY_FRAME)
    pts = None if config else pts_flags & PACKET_PTS_MASK
    return pts, config, key, size
