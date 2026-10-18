"""Drives the pygame viewer headless (SDL dummy video driver) with a fake backend."""

import os
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:
    import pygame
    from phonemirror import viewer as V
except ImportError:  # pragma: no cover
    pygame = None

from phonemirror.backend import Backend


class RecordingBackend(Backend):
    buttons = (("Back", "back"), ("Home", "home"))

    def __init__(self):
        super().__init__()
        self.calls = []

    def touch(self, action, nx, ny):
        self.calls.append(("touch", action, round(nx, 2), round(ny, 2)))

    def scroll(self, nx, ny, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def press(self, name, shift=False, ctrl=False, alt=False):
        self.calls.append(("press", name, ctrl))

    def text(self, text):
        self.calls.append(("text", text))

    def command(self, name):
        self.calls.append(("command", name))


@unittest.skipIf(pygame is None, "pygame not installed")
class ViewerTest(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.backend = RecordingBackend()
        self.viewer = V.Viewer(self.backend)
        self.viewer.font = pygame.font.Font(None, 20)
        self.viewer.set_mode((200, 400 + V.TOOLBAR_HEIGHT))
        self.viewer.clipboard = V.Clipboard()
        self.viewer.sized_to_frame = True  # keep the test window size
        # 100x200 frame -> fills the 200x400 content area at 2x
        self.backend._publish(100, 200, rgb=bytes([10, 200, 30]) * (100 * 200))
        self.viewer.update_frame()
        self.viewer.layout()
        self.viewer.draw()

    def tearDown(self):
        pygame.quit()

    def send(self, type_, **attrs):
        self.viewer.handle(pygame.event.Event(type_, **attrs))

    def test_frame_is_drawn(self):
        self.assertEqual(self.viewer.content, pygame.Rect(0, 0, 200, 400))
        self.assertEqual(self.viewer.screen.get_at((100, 200))[:3], (10, 200, 30))

    def test_drag_maps_to_normalised_touch(self):
        self.send(pygame.MOUSEBUTTONDOWN, button=1, pos=(50, 100))
        self.send(pygame.MOUSEMOTION, pos=(100, 300), rel=(50, 200), buttons=(1, 0, 0))
        self.send(pygame.MOUSEBUTTONUP, button=1, pos=(500, -20))
        self.assertEqual(self.backend.calls, [("touch", "down", 0.25, 0.25),
                                              ("touch", "move", 0.5, 0.75),
                                              ("touch", "up", 1.0, 0.0)])

    def test_toolbar_and_mouse_buttons(self):
        rect = self.viewer.buttons[1][0]
        self.send(pygame.MOUSEBUTTONDOWN, button=1, pos=rect.center)
        self.send(pygame.MOUSEBUTTONDOWN, button=3, pos=(10, 10))
        self.assertEqual(self.backend.calls, [("press", "home", False), ("press", "back", False)])

    def test_keys(self):
        self.send(pygame.KEYDOWN, key=pygame.K_h, mod=pygame.KMOD_LALT, unicode="h", scancode=0)
        self.send(pygame.TEXTINPUT, text="h")  # swallowed: used as a shortcut
        self.send(pygame.KEYUP, key=pygame.K_h, mod=0, unicode="", scancode=0)
        self.send(pygame.TEXTINPUT, text="x")
        self.send(pygame.KEYDOWN, key=pygame.K_RETURN, mod=0, unicode="\r", scancode=0)
        self.send(pygame.KEYDOWN, key=pygame.K_a, mod=pygame.KMOD_LCTRL, unicode="", scancode=0)
        self.send(pygame.KEYDOWN, key=pygame.K_c, mod=pygame.KMOD_LCTRL, unicode="", scancode=0)
        self.send(pygame.KEYDOWN, key=pygame.K_o, mod=pygame.KMOD_LALT, unicode="", scancode=0)
        # AltGr (reported as RAlt+LCtrl on Windows) must still type text
        self.send(pygame.KEYDOWN, key=pygame.K_q, mod=pygame.KMOD_RALT | pygame.KMOD_LCTRL,
                  unicode="@", scancode=0)
        self.send(pygame.TEXTINPUT, text="@")
        self.assertEqual(self.backend.calls, [
            ("press", "home", False), ("text", "x"), ("press", "enter", False),
            ("press", "a", True), ("command", "copy"), ("command", "display_off"),
            ("text", "@")])

    def test_fit_rect(self):
        self.assertEqual(V.fit_rect(100, 200, 400, 400), pygame.Rect(100, 0, 200, 400))
        self.assertIsNone(V.normalise((5, 5), pygame.Rect(10, 10, 10, 10), clamp=False))


if __name__ == "__main__":
    unittest.main()
