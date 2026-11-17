#!/usr/bin/python3
"""Desk Mode: APPLaunch app for cz-desk-mode.service.

Starts or closes the HDMI desktop, hands the built-in keyboard to it, and
changes the daemon's settings. Draws straight to the LCD framebuffer with PIL
and reads the internal keyboard, like the other external APPLaunch apps. No
root: settings go to /var/lib/cz-desk-mode/settings and actions to
/var/lib/cz-desk-mode/request, both owned by the desktop user; the daemon picks
them up within a second.
"""
import argparse
import array
import ctypes
import fcntl
import json
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont

HOME = Path.home()
STATE_DIR = HOME / '.config/cz-desk-mode'
DEFAULTS_FILE = Path('/etc/default/cz-desk-mode')
DAEMON_DIR = Path(os.environ.get('CZ_DESK_MODE_STATE', '/var/lib/cz-desk-mode'))  # override for tests
SETTINGS_FILE = DAEMON_DIR / 'settings'
REQUEST_FILE = DAEMON_DIR / 'request'
STATUS_FILE = DAEMON_DIR / 'status'
UNIT = 'cz-desk-mode.service'
DESKTOP_UNIT = 'cz-desk-mode-desktop.service'
KEYBOARD = '/dev/input/cardputer-zero-internal'
DEJAVU = '/usr/share/fonts/truetype/dejavu/'
FONT, FONT_BOLD = DEJAVU + 'DejaVuSans.ttf', DEJAVU + 'DejaVuSans-Bold.ttf'
FONT_TITLE = '/usr/share/APPLaunch/share/font/Montserrat-Bold.ttf'
BG, FG, MUTED, FAINT, DIM = '#000000', '#ffffff', '#a7b0bb', '#7d8793', '#4b535d'
ROW, RULE = '#1c232b', '#2a3139'
BLUE, GREEN, YELLOW, RED = '#4fb3ff', '#33e06a', '#ffcc1a', '#ff5a52'

DEFAULTS = {'AUTOSTART': '1', 'KEYBOARD_ON_START': 'ask'}
ROWS = ['DESKTOP', 'KEYBOARD', 'AUTOSTART', 'KEYBOARD_ON_START']
LABELS = {'DESKTOP': 'HDMI desktop', 'KEYBOARD': 'Keyboard', 'AUTOSTART': 'Start on plug-in',
          'KEYBOARD_ON_START': 'Keyboard on start'}
ON_START = ['ask', 'desktop', 'cardputer']
ON_START_TEXT = {'ask': 'Ask', 'desktop': 'HDMI display', 'cardputer': 'Cardputer'}


def read(path, default=''):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def systemctl(*args, user=False):
    try:
        r = subprocess.run(['/usr/bin/systemctl'] + (['--user'] if user else []) + list(args),
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def parse_settings(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            out[key.strip()] = value.strip().strip('"\'')
    return out


def load_config():
    config = dict(DEFAULTS)
    for path in (DEFAULTS_FILE, SETTINGS_FILE):
        config.update({k: v for k, v in parse_settings(read(path)).items() if k in config})
    return config


def save_setting(key, value):
    current = {k: v for k, v in parse_settings(read(SETTINGS_FILE)).items() if k in DEFAULTS}
    current[key] = value
    body = '# Written by the Desk Mode app; overrides /etc/default/cz-desk-mode.\n'
    body += ''.join(f'{k}={v}\n' for k, v in sorted(current.items()))
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + '.tmp')
    tmp.write_text(body)
    os.replace(tmp, SETTINGS_FILE)


def request(action):
    tmp = REQUEST_FILE.with_name('request.tmp')
    tmp.write_text(action + '\n')
    os.replace(tmp, REQUEST_FILE)


def status():
    try:
        daemon = json.loads(read(STATUS_FILE, '{}'))
    except ValueError:
        daemon = {}
    hdmi = any(read(p) == 'connected' for p in Path('/sys/class/drm').glob('card*-HDMI-A-*/status'))
    return {
        'config': load_config(),
        'service': systemctl('show', UNIT, '-p', 'ActiveState', '--value'),
        'hdmi': hdmi,
        'desktop': systemctl('is-active', DESKTOP_UNIT, user=True) == 'active',
        'keyboard': daemon.get('keyboard', 'cardputer'),
    }


class Bitfield(ctypes.Structure):
    _fields_ = [('offset', ctypes.c_uint32), ('length', ctypes.c_uint32), ('msb_right', ctypes.c_uint32)]


class VarInfo(ctypes.Structure):
    _fields_ = ([(n, ctypes.c_uint32) for n in ('xres', 'yres', 'xres_virtual', 'yres_virtual', 'xoffset', 'yoffset', 'bits_per_pixel', 'grayscale')]
                + [(n, Bitfield) for n in ('red', 'green', 'blue', 'transp')]
                + [(n, ctypes.c_uint32) for n in ('nonstd', 'activate', 'height', 'width', 'accel_flags', 'pixclock', 'left_margin', 'right_margin', 'upper_margin', 'lower_margin', 'hsync_len', 'vsync_len', 'sync', 'vmode', 'rotate', 'colorspace')]
                + [('reserved', ctypes.c_uint32 * 4)])


class FixInfo(ctypes.Structure):
    _fields_ = [('id', ctypes.c_char * 16), ('smem_start', ctypes.c_ulong), ('smem_len', ctypes.c_uint32), ('type', ctypes.c_uint32), ('type_aux', ctypes.c_uint32), ('visual', ctypes.c_uint32), ('xpanstep', ctypes.c_uint16), ('ypanstep', ctypes.c_uint16), ('ywrapstep', ctypes.c_uint16), ('line_length', ctypes.c_uint32), ('mmio_start', ctypes.c_ulong), ('mmio_len', ctypes.c_uint32), ('accel', ctypes.c_uint32), ('capabilities', ctypes.c_uint16), ('reserved', ctypes.c_uint16 * 2)]


class Framebuffer:
    def __init__(self):
        path = '/dev/fb_lcd' if os.path.exists('/dev/fb_lcd') else '/dev/fb0'
        self.fd = os.open(path, os.O_RDWR)
        self.var, self.fix = VarInfo(), FixInfo()
        fcntl.ioctl(self.fd, 0x4600, self.var)
        fcntl.ioctl(self.fd, 0x4602, self.fix)
        v = self.var
        if (v.xres, v.yres, v.bits_per_pixel) != (320, 170, 16):
            os.close(self.fd)
            raise RuntimeError('Unsupported LCD framebuffer format')
        self.memory = mmap.mmap(self.fd, self.fix.smem_len, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        self.previous = None

    def draw(self, image):
        data = image.tobytes()
        if data == self.previous:
            return
        self.previous = data
        raw = array.array('H', (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3) for r, g, b in image.getdata())).tobytes()
        for y in range(170):
            start = (y + self.var.yoffset) * self.fix.line_length + self.var.xoffset * 2
            self.memory[start:start + 640] = raw[y * 640:(y + 1) * 640]

    def close(self):
        self.memory.close()
        os.close(self.fd)


class App:
    def __init__(self, state=None):
        self.state = state or status()
        self.selected = 0
        self.details = False
        self.confirm = False
        self.running = True
        self.message, self.message_colour, self.message_until = '', MUTED, 0

        def load(path, size):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                return ImageFont.truetype(FONT, size)
        self.title = load(FONT_TITLE, 17)
        self.font = load(FONT, 14)
        self.bold = load(FONT_BOLD, 14)
        self.small = load(FONT, 11)

    def say(self, text, colour=MUTED):
        self.message, self.message_colour, self.message_until = text, colour, time.monotonic() + 4

    def value(self, key):
        s, c = self.state, self.state['config']
        if key == 'DESKTOP':
            return ('Running', GREEN) if s['desktop'] else ('Off', MUTED)
        if key == 'KEYBOARD':
            return ('HDMI display', BLUE) if s['keyboard'] == 'desktop' else ('Cardputer', FG)
        if key == 'AUTOSTART':
            return ('On', GREEN) if c['AUTOSTART'] == '1' else ('Off', MUTED)
        return ON_START_TEXT.get(c['KEYBOARD_ON_START'], 'Ask'), FG

    def hint(self, key):
        if key == 'DESKTOP':
            return 'plugged in' if self.state['hdmi'] else 'no HDMI'
        return ''

    def render(self):
        im = Image.new('RGB', (320, 170), BG)
        d = ImageDraw.Draw(im)
        d.text((10, 4), 'Desk Mode', font=self.title, fill=FG)
        ok = self.state['service'] == 'active'
        d.ellipse((11, 29, 17, 35), fill=(GREEN if self.state['hdmi'] else MUTED) if ok else RED)
        line = ('HDMI connected' if self.state['hdmi'] else 'HDMI not connected') if ok else 'cz-desk-mode is not running'
        d.text((22, 25), line, font=self.small, fill=MUTED)

        if self.confirm:
            d.text((12, 50), 'Close the HDMI desktop?', font=self.title, fill=FG)
            d.text((12, 80), 'Save your desktop work first:', font=self.font, fill=MUTED)
            d.text((12, 99), 'its open apps will be closed.', font=self.font, fill=MUTED)
            footer = [('ENTER', 'Close desktop'), ('ESC', 'Cancel')]
        elif self.details:
            lines = [('Plug in HDMI to get the Pi desktop.', FG),
                     ('Hand it this keyboard from here or', MUTED),
                     ('when asked; Ctrl+Alt+Esc gives it', MUTED),
                     ('back to the Cardputer, and so does', MUTED),
                     ('unplugging HDMI or closing the desktop.', MUTED)]
            for i, (text, colour) in enumerate(lines):
                d.text((12, 46 + i * 19), text, font=self.font if i == 0 else self.small, fill=colour)
            footer = [('ESC', 'Back')]
        else:
            for i, key in enumerate(ROWS):
                y = 45 + 24 * i
                if i == self.selected:
                    d.rounded_rectangle((6, y - 2, 312, y + 19), radius=4, fill=ROW)
                    d.rounded_rectangle((6, y + 1, 8, y + 16), radius=1, fill=BLUE)
                d.text((14, y), LABELS[key], font=self.font, fill=FG)
                hint = self.hint(key)
                if hint:
                    d.text((20 + d.textlength(LABELS[key], font=self.font), y + 3), hint, font=self.small, fill=FAINT)
                text, colour = self.value(key)
                d.text((304 - d.textlength(text, font=self.bold), y), text, font=self.bold, fill=colour)
            footer = [('F/X', 'Move'), ('ENTER', 'Change'), ('I', 'Info'), ('ESC', 'Exit')]

        d.line((8, 146, 312, 146), fill=RULE)
        if self.message and time.monotonic() < self.message_until:
            d.text((10, 152), self.message, font=self.small, fill=self.message_colour)
        else:
            x = 10
            for keys, action in footer:
                d.text((x, 152), keys, font=self.small, fill=FG)
                x += d.textlength(keys + ' ', font=self.small)
                d.text((x, 152), action, font=self.small, fill=FAINT)
                x += d.textlength(action, font=self.small) + 14
        return im

    def act(self, step):
        key = ROWS[self.selected]
        s = self.state
        if s['service'] != 'active':
            self.say('cz-desk-mode is not running', RED)
            return
        try:
            if key == 'DESKTOP' and step == 0:
                if s['desktop']:
                    self.confirm = True
                elif not s['hdmi']:
                    self.say('Plug in an HDMI display first', YELLOW)
                else:
                    request('desktop-start')
                    self.running = False   # the daemon may ask about the keyboard on the LCD
            elif key == 'KEYBOARD' and step == 0:
                if s['keyboard'] == 'desktop':
                    request('keyboard-cardputer')
                elif not s['desktop']:
                    self.say('Start the HDMI desktop first', YELLOW)
                else:
                    request('keyboard-desktop')
                    self.running = False   # the LCD goes to the "keyboard on HDMI" notice
            elif key == 'AUTOSTART':
                save_setting('AUTOSTART', '0' if s['config']['AUTOSTART'] == '1' else '1')
                self.say('Saved', GREEN)
            elif key == 'KEYBOARD_ON_START':
                current = s['config']['KEYBOARD_ON_START']
                index = ON_START.index(current) if current in ON_START else 0
                save_setting('KEYBOARD_ON_START', ON_START[(index + (step or 1)) % len(ON_START)])
                self.say('Saved', GREEN)
        except OSError as error:
            print(key, '->', error, flush=True)
            self.say('Failed. See ~/.config/cz-desk-mode/app.log', RED)
        self.state = status()

    def key(self, code):
        code = {33: 103, 45: 108, 44: 105, 46: 106}.get(code, code)   # F/X/Z/C as arrows
        if code in (1, 41):  # Esc, or bare backtick
            if self.details or self.confirm:
                self.details = self.confirm = False
            else:
                self.running = False
            return
        if self.confirm:
            if code == 28:
                self.confirm = False
                request('desktop-stop')
                self.say('Closing the desktop…', YELLOW)
            return
        if code == 23:  # I
            self.details = not self.details
            return
        if self.details:
            return
        if code in (103, 17, 39):
            self.selected = (self.selected - 1) % len(ROWS)
        elif code in (108, 31, 52):
            self.selected = (self.selected + 1) % len(ROWS)
        elif code in (28, 57):
            self.act(0)
        elif code in (105, 30, 51):
            self.act(-1)
        elif code in (106, 32, 53):
            self.act(1)


def run():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / 'app.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    app = App()
    frame = Framebuffer()
    keyboard = os.open(KEYBOARD, os.O_RDONLY | os.O_NONBLOCK)
    event = struct.Struct('llHHi')
    last = 0.0
    signal.signal(signal.SIGTERM, lambda *_: setattr(app, 'running', False))
    signal.signal(signal.SIGINT, lambda *_: setattr(app, 'running', False))
    try:
        while app.running:
            now = time.monotonic()
            if now - last >= 2:
                app.state = status()
                last = now
            frame.draw(app.render())
            ready, _, _ = select.select([keyboard], [], [], 0.5)
            if ready:
                data = os.read(keyboard, event.size * 64)
                for offset in range(0, len(data) - event.size + 1, event.size):
                    _, _, kind, code, value = event.unpack_from(data, offset)
                    if kind == 1 and value == 1 and app.running:
                        app.key(code)
    finally:
        os.close(keyboard)
        frame.close()
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--preview', metavar='PNG')
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(), indent=2))
    elif args.preview:
        App().render().save(args.preview)
    else:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / 'app.log', 'w', buffering=1) as log:
            sys.stdout = sys.stderr = log
            try:
                run()
            except Exception:
                import traceback
                traceback.print_exc()
                raise SystemExit(1)
