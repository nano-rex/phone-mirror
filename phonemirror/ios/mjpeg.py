"""Split an MJPEG (multipart/x-mixed-replace) byte stream into JPEG images."""

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


class MjpegParser:
    """Finds JPEG images by their start/end markers.

    Part headers and boundaries between images are skipped, so this works for
    WebDriverAgent's MJPEG server and for other multipart encoders alike.
    """

    def __init__(self, max_buffer=16 * 1024 * 1024):
        self._buf = bytearray()
        self._max = max_buffer

    def feed(self, data):
        self._buf += data
        images = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                # keep a possible half marker
                del self._buf[:max(len(self._buf) - 1, 0)]
                break
            end = self._buf.find(EOI, start + 2)
            if end < 0:
                if start:
                    del self._buf[:start]
                if len(self._buf) > self._max:
                    self._buf.clear()
                break
            images.append(bytes(self._buf[start:end + 2]))
            del self._buf[:end + 2]
        return images


def jpeg_size(data):
    """Return (width, height) from a JPEG's SOF segment, or None."""
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = (data[i + 2] << 8) | data[i + 3]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return width, height
        i += 2 + length
    return None
