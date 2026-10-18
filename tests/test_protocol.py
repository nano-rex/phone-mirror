import struct
import unittest

from phonemirror.android import protocol as P
from phonemirror.android.keycodes import KEYCODES, metastate


class ControlMessageTest(unittest.TestCase):
    """Expected bytes come from scrcpy's app/tests/test_control_msg_serialize.c."""

    def test_inject_keycode(self):
        msg = P.inject_keycode(P.ACTION_UP, KEYCODES["enter"], 5, 0x41)
        self.assertEqual(msg, bytes([0, 0x01, 0, 0, 0, 0x42, 0, 0, 0, 5, 0, 0, 0, 0x41]))

    def test_inject_text(self):
        self.assertEqual(P.inject_text("hello, world!"), b"\x01\x00\x00\x00\x0dhello, world!")

    def test_inject_text_long_is_truncated(self):
        msg = P.inject_text("a" * 400)
        self.assertEqual(len(msg), 5 + 300)
        self.assertEqual(msg[1:5], b"\x00\x00\x01\x2c")

    def test_utf8_truncation_keeps_characters_whole(self):
        data = P.truncate_utf8("é" * 200, 301)  # 2 bytes each
        self.assertEqual(len(data), 300)
        data.decode("utf-8")

    def test_inject_touch(self):
        msg = P.inject_touch(P.ACTION_DOWN, 100, 200, 1080, 1920, 1.0,
                             pointer_id=0x1234567887654321, action_button=1, buttons=1)
        expected = bytes([
            2, 0x00,
            0x12, 0x34, 0x56, 0x78, 0x87, 0x65, 0x43, 0x21,
            0x00, 0x00, 0x00, 0x64, 0x00, 0x00, 0x00, 0xc8,
            0x04, 0x38, 0x07, 0x80,
            0xff, 0xff,
            0x00, 0x00, 0x00, 0x01,
            0x00, 0x00, 0x00, 0x01,
        ])
        self.assertEqual(msg, expected)

    def test_inject_scroll(self):
        msg = P.inject_scroll(260, 1026, 1080, 1920, 16, -16, buttons=1)
        expected = bytes([
            3,
            0x00, 0x00, 0x01, 0x04, 0x00, 0x00, 0x04, 0x02,
            0x04, 0x38, 0x07, 0x80,
            0x7F, 0xFF,
            0x80, 0x00,
            0x00, 0x00, 0x00, 0x01,
        ])
        self.assertEqual(msg, expected)

    def test_simple_messages(self):
        self.assertEqual(P.back_or_screen_on(P.ACTION_UP), b"\x04\x01")
        self.assertEqual(P.expand_notification_panel(), b"\x05")
        self.assertEqual(P.expand_settings_panel(), b"\x06")
        self.assertEqual(P.collapse_panels(), b"\x07")
        self.assertEqual(P.get_clipboard(P.COPY_KEY_COPY), b"\x08\x01")
        self.assertEqual(P.set_display_power(True), b"\x0a\x01")
        self.assertEqual(P.rotate_device(), b"\x0b")

    def test_set_clipboard(self):
        msg = P.set_clipboard(0x0102030405060708, "hello, world!", paste=True)
        expected = (b"\x09" + bytes([1, 2, 3, 4, 5, 6, 7, 8]) + b"\x01"
                    + b"\x00\x00\x00\x0d" + b"hello, world!")
        self.assertEqual(msg, expected)

    def test_metastate(self):
        self.assertEqual(metastate(shift=True), 0x41)
        self.assertEqual(metastate(ctrl=True), 0x3000)


class DeviceMessageTest(unittest.TestCase):
    def test_messages_split_across_reads(self):
        data = (b"\x00\x00\x00\x00\x03ABC" + b"\x01" + struct.pack(">Q", 42)
                + b"\x02\x00\x07\x00\x02xy")
        parser = P.DeviceMessageParser()
        out = []
        for i in range(len(data)):
            out += parser.feed(data[i:i + 1])
        self.assertEqual(out, [("clipboard", "ABC"), ("ack_clipboard", 42),
                               ("uhid_output", (7, b"xy"))])

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            P.DeviceMessageParser().feed(b"\x09")


class StreamHeaderTest(unittest.TestCase):
    def test_session_header(self):
        header = b"\x80\x00\x00\x00" + struct.pack(">II", 1080, 2400)
        self.assertTrue(P.is_session_header(header))
        self.assertEqual(P.parse_session_header(header), (1080, 2400))

    def test_media_header(self):
        config = struct.pack(">QI", P.PACKET_FLAG_CONFIG, 30)
        self.assertFalse(P.is_session_header(config))
        self.assertEqual(P.parse_packet_header(config), (None, True, False, 30))
        key = struct.pack(">QI", P.PACKET_FLAG_KEY_FRAME | 123456, 999)
        self.assertEqual(P.parse_packet_header(key), (123456, False, True, 999))

    def test_codec_id(self):
        self.assertEqual(P.parse_codec_id(b"h264"), "h264")
        self.assertEqual(P.parse_codec_id(b"h265"), "hevc")
        with self.assertRaises(ConnectionError):
            P.parse_codec_id(b"\x00\x00\x00\x01")

    def test_device_name(self):
        self.assertEqual(P.parse_device_name(b"Pixel 9\0" + b"\0" * 56), "Pixel 9")


if __name__ == "__main__":
    unittest.main()
