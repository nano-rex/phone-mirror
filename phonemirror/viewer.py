"""pygame window that shows backend frames and turns mouse/keyboard input into
backend calls."""

import io
import sys

import pygame

TOOLBAR_HEIGHT = 34
BG = (18, 18, 20)
BAR_BG = (34, 35, 39)
BUTTON_BG = (52, 54, 60)
BUTTON_HOVER = (74, 77, 86)
TEXT = (225, 227, 232)
MUTED = (150, 154, 162)

# pygame key -> backend key name, for keys that do not produce text
SPECIAL_KEYS = {
    pygame.K_RETURN: "enter",
    pygame.K_KP_ENTER: "enter",
    pygame.K_BACKSPACE: "backspace",
    pygame.K_DELETE: "delete",
    pygame.K_TAB: "tab",
    pygame.K_ESCAPE: "escape",
    pygame.K_UP: "up",
    pygame.K_DOWN: "down",
    pygame.K_LEFT: "left",
    pygame.K_RIGHT: "right",
    pygame.K_HOME: "move_home",
    pygame.K_END: "move_end",
    pygame.K_PAGEUP: "page_up",
    pygame.K_PAGEDOWN: "page_down",
    pygame.K_INSERT: "insert",
}

# Alt+<key> shortcuts (Option on macOS)
SHORTCUTS = {
    pygame.K_h: "home",
    pygame.K_b: "back",
    pygame.K_s: "recents",
    pygame.K_p: "power",
    pygame.K_m: "menu",
    pygame.K_UP: "volume_up",
    pygame.K_DOWN: "volume_down",
    pygame.K_n: "notifications",
    pygame.K_r: "rotate",
}

HELP = """Mouse: left = touch/drag, right = back, middle = home, wheel = scroll
Alt+H home  Alt+B back  Alt+S recents  Alt+P power  Alt+Up/Down volume
Alt+N notifications (Alt+Shift+N close)  Alt+R rotate  Alt+O screen off
(Alt+Shift+O on)  Alt+F fullscreen  Alt+W fit window  Ctrl+V paste PC clipboard"""


def fit_rect(content_w, content_h, area_w, area_h):
    """Largest rect with the content's aspect ratio centred in the area."""
    if content_w <= 0 or content_h <= 0 or area_w <= 0 or area_h <= 0:
        return pygame.Rect(0, 0, 0, 0)
    scale = min(area_w / content_w, area_h / content_h)
    w = max(int(content_w * scale), 1)
    h = max(int(content_h * scale), 1)
    return pygame.Rect((area_w - w) // 2, (area_h - h) // 2, w, h)


def normalise(pos, rect, clamp=True):
    """Window position -> (nx, ny) in 0..1, or None when outside the rect."""
    if rect.w == 0 or rect.h == 0:
        return None
    nx = (pos[0] - rect.x) / rect.w
    ny = (pos[1] - rect.y) / rect.h
    if clamp:
        return min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
    if 0 <= nx < 1 and 0 <= ny < 1:
        return nx, ny
    return None


class Clipboard:
    def __init__(self):
        self.ok = False
        try:
            import pygame.scrap as scrap
            scrap.init()
            self.scrap = scrap
            self.ok = hasattr(scrap, "get_text")
        except Exception:
            self.scrap = None

    def get(self):
        if self.ok:
            try:
                return self.scrap.get_text() or ""
            except Exception:
                pass
        return ""

    def put(self, text):
        if self.ok:
            try:
                self.scrap.put_text(text)
            except Exception:
                pass


class Viewer:
    def __init__(self, backend, toolbar=True, fullscreen=False):
        self.backend = backend
        self.toolbar = toolbar and bool(backend.buttons)
        self.fullscreen = fullscreen
        self.frame_seq = None
        self.frame_surface = None
        self.frame_size = None
        self.scaled = None
        self.content = pygame.Rect(0, 0, 0, 0)
        self.dragging = False
        self.buttons = []
        self.sized_to_frame = False
        self.suppress_text = False  # set when a shortcut consumed the key

    # layout -------------------------------------------------------------
    def bar_height(self):
        return TOOLBAR_HEIGHT if self.toolbar else 0

    def layout(self):
        w, h = self.screen.get_size()
        area_h = h - self.bar_height()
        if self.frame_size:
            self.content = fit_rect(*self.frame_size, w, area_h)
        self.scaled = None
        self.buttons = []
        if self.toolbar:
            n = len(self.backend.buttons)
            bw = w / n
            for i, (label, name) in enumerate(self.backend.buttons):
                rect = pygame.Rect(int(i * bw) + 2, area_h + 3, int(bw) - 4, TOOLBAR_HEIGHT - 6)
                self.buttons.append((rect, label, name))

    def window_size_for(self, fw, fh):
        desktop_w, desktop_h = pygame.display.get_desktop_sizes()[0]
        max_w, max_h = desktop_w * 0.9, desktop_h * 0.85 - self.bar_height()
        scale = min(max_w / fw, max_h / fh, 1.0)
        return max(int(fw * scale), 200), max(int(fh * scale), 200) + self.bar_height()

    def set_mode(self, size):
        flags = pygame.FULLSCREEN if self.fullscreen else pygame.RESIZABLE
        self.screen = pygame.display.set_mode((0, 0) if self.fullscreen else size, flags)
        self.layout()

    # main loop ----------------------------------------------------------
    def run(self):
        pygame.init()
        self.caption = self.backend.title
        pygame.display.set_caption(self.caption)
        self.font = pygame.font.Font(None, 20)
        self.set_mode((420, 860))
        self.clipboard = Clipboard()
        pygame.key.start_text_input()
        pygame.key.set_repeat(400, 40)
        clock = pygame.time.Clock()
        print(HELP)
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                else:
                    self.handle(event)
            self.update_frame()
            if self.backend.title != self.caption:
                self.caption = self.backend.title
                pygame.display.set_caption(self.caption)
            text = self.backend.pop_clipboard()
            if text is not None:
                self.clipboard.put(text)
            self.draw()
            clock.tick(120)
        pygame.quit()
        return 1 if self.backend.error else 0

    def update_frame(self):
        frame = self.backend.latest_frame()
        if frame is None:
            if self.frame_surface is not None:  # stream ended; show the waiting screen
                self.frame_surface = self.scaled = None
                self.frame_seq = None
            return
        if frame.seq == self.frame_seq:
            return
        self.frame_seq = frame.seq
        try:
            if frame.rgb is not None:
                surf = pygame.image.frombuffer(frame.rgb, (frame.width, frame.height), "RGB")
            else:
                surf = pygame.image.load(io.BytesIO(frame.jpeg), "frame.jpg")
        except (pygame.error, ValueError):
            return
        self.frame_surface = surf
        size = surf.get_size()
        if size != self.frame_size:
            self.frame_size = size
            if not self.sized_to_frame and not self.fullscreen:
                self.sized_to_frame = True
                self.set_mode(self.window_size_for(*size))
            self.layout()
        self.scaled = None

    def draw(self):
        self.screen.fill(BG)
        if self.frame_surface is not None and self.content.w:
            if self.scaled is None:
                self.scaled = pygame.transform.smoothscale(self.frame_surface, self.content.size)
            self.screen.blit(self.scaled, self.content.topleft)
        else:
            self.draw_waiting()
        if self.backend.error:
            self.center_text(f"Disconnected: {self.backend.error}", y_offset=30)
        if self.toolbar:
            w, h = self.screen.get_size()
            pygame.draw.rect(self.screen, BAR_BG, (0, h - TOOLBAR_HEIGHT, w, TOOLBAR_HEIGHT))
            mouse = pygame.mouse.get_pos()
            for rect, label, _ in self.buttons:
                color = BUTTON_HOVER if rect.collidepoint(mouse) else BUTTON_BG
                pygame.draw.rect(self.screen, color, rect, border_radius=6)
                img = self.font.render(label, True, TEXT)
                self.screen.blit(img, img.get_rect(center=rect.center))
        pygame.display.flip()

    def draw_waiting(self):
        w, h = self.screen.get_size()
        area_h = h - self.bar_height()
        lines = self.backend.waiting_lines()
        line_h = self.font.get_linesize() + 2
        uri = self.backend.qr_uri
        qr = self.qr_surface(uri, min(w - 40, area_h - 60 - line_h * len(lines))) if uri else None
        total = (qr.get_height() + 16 if qr else 0) + line_h * len(lines)
        y = max((area_h - total) // 2, 8)
        if qr:
            self.screen.blit(qr, qr.get_rect(midtop=(w // 2, y)))
            y += qr.get_height() + 16
        for line in lines:
            img = self.font.render(line, True, MUTED)
            self.screen.blit(img, img.get_rect(midtop=(w // 2, y)))
            y += line_h

    def qr_surface(self, uri, size):
        """Render a QR code for uri, cached per (uri, size)."""
        if size < 80:
            return None
        key = (uri, size)
        if getattr(self, "_qr_key", None) != key:
            try:
                import segno
            except ImportError:
                return None
            matrix = [list(row) for row in segno.make(uri, error="m").matrix]
            border = 4
            n = len(matrix) + 2 * border
            module = max(size // n, 1)
            surf = pygame.Surface((n * module, n * module))
            surf.fill((255, 255, 255))
            for r, row in enumerate(matrix):
                for c, dark in enumerate(row):
                    if dark:
                        surf.fill((0, 0, 0), ((c + border) * module, (r + border) * module,
                                              module, module))
            self._qr_key, self._qr = key, surf
        return self._qr

    def center_text(self, text, y_offset=0):
        w, h = self.screen.get_size()
        img = self.font.render(text, True, MUTED)
        self.screen.blit(img, img.get_rect(center=(w // 2, (h - self.bar_height()) // 2 + y_offset)))

    # input --------------------------------------------------------------
    def handle(self, event):
        b = self.backend
        if event.type in (pygame.VIDEORESIZE, pygame.WINDOWSIZECHANGED):
            self.layout()
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:
                for rect, _, name in self.buttons:
                    if rect.collidepoint(event.pos):
                        b.press(name)
                        return
                pos = normalise(event.pos, self.content, clamp=False)
                if pos:
                    self.dragging = True
                    b.touch("down", *pos)
            elif event.button == 3:
                b.press("back")
            elif event.button == 2:
                b.press("home")
        elif event.type == pygame.MOUSEMOTION and self.dragging:
            b.touch("move", *normalise(event.pos, self.content))
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1 and self.dragging:
            self.dragging = False
            b.touch("up", *normalise(event.pos, self.content))
        elif event.type == pygame.MOUSEWHEEL:
            pos = normalise(pygame.mouse.get_pos(), self.content, clamp=False)
            if pos:
                dx = getattr(event, "precise_x", event.x)
                dy = getattr(event, "precise_y", event.y)
                if getattr(event, "flipped", False):
                    dx, dy = -dx, -dy
                b.scroll(*pos, dx, dy)
        elif event.type == pygame.TEXTINPUT:
            if self.suppress_text:
                self.suppress_text = False
            else:
                b.text(event.text)
        elif event.type == pygame.KEYDOWN:
            self.suppress_text = self.handle_key(event)
        elif event.type == pygame.KEYUP:
            # text for a shortcut arrives between its KEYDOWN and KEYUP
            self.suppress_text = False

    def handle_key(self, event):
        """Handle a key press; return True when it was used as a shortcut."""
        b = self.backend
        mods = event.mod
        shift = bool(mods & pygame.KMOD_SHIFT)
        # Only left Alt is the shortcut key: right Alt is AltGr on many layouts
        # (and reported with Ctrl on Windows), which must keep typing text.
        altgr = bool(mods & pygame.KMOD_RALT)
        alt = bool(mods & pygame.KMOD_LALT)
        # Cmd on macOS plays the role of Ctrl for clipboard shortcuts
        ctrl = (bool(mods & pygame.KMOD_CTRL) and not altgr) or (
            sys.platform == "darwin" and bool(mods & pygame.KMOD_META))
        key = event.key
        if alt and not ctrl:
            if key == pygame.K_n and shift:
                b.command("collapse")
            elif key == pygame.K_o:
                b.command("display_on" if shift else "display_off")
            elif key == pygame.K_f:
                self.fullscreen = not self.fullscreen
                self.set_mode(self.window_size_for(*self.frame_size) if self.frame_size else (420, 860))
            elif key == pygame.K_w and self.frame_size and not self.fullscreen:
                self.set_mode(self.window_size_for(*self.frame_size))
            elif key in SHORTCUTS:
                b.press(SHORTCUTS[key])
            return True
        if ctrl:
            name = pygame.key.name(key)
            if key == pygame.K_v:
                text = self.clipboard.get()
                if text:
                    b.paste(text)
            elif key == pygame.K_c:
                b.command("copy")
            elif key == pygame.K_x:
                b.command("cut")
            elif len(name) == 1 and name.isalnum():
                b.press(name, shift=shift, ctrl=True)
            elif key in SPECIAL_KEYS:
                b.press(SPECIAL_KEYS[key], shift=shift, ctrl=True)
            return True
        if key in SPECIAL_KEYS:
            b.press(SPECIAL_KEYS[key], shift=shift)
        return False
