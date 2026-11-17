#!/usr/bin/python3
"""Desk Mode daemon: HDMI desktop + the CardputerZero's own keyboard.

* Starts the Raspberry Pi desktop (labwc) on the HDMI output when HDMI is
  plugged in (AUTOSTART), as the desktop user's own systemd unit
  cz-desk-mode-desktop.service. No LightDM and no root: the desktop user is
  in the video and input groups, and libseat's "noop" backend opens the
  devices directly. Only the HDMI GPU (card0) is used; the built-in LCD
  stays with the launcher. (LightDM's greeter also starts a second PipeWire
  that takes the sound card away from the launcher, which hangs it.)

* Hands the built-in keyboard to the HDMI desktop on request. The stock udev
  rules put that keyboard on its own seat so only the launcher sees it. To
  hand it over, this daemon grabs it (EVIOCGRAB, so the launcher stops
  getting keys) and replays the keys on a virtual uinput keyboard, which the
  desktop picks up like any USB keyboard. The Sym layer sends non-standard
  key codes (# is 39, which a desktop reads as ;), so those are translated
  to the standard key plus Shift. Ctrl+Alt+Esc, unplugging HDMI or closing
  the desktop gives the keyboard back.

* When the desktop starts it asks, on the built-in LCD, where the keyboard
  should go (KEYBOARD_ON_START=ask). While the question or the "keyboard is
  on the HDMI display" notice is up, the launcher is paused (SIGSTOP) so it
  does not draw over it, and its frame is put back afterwards.

Settings: /etc/default/cz-desk-mode (package defaults), then
/var/lib/cz-desk-mode/settings (the Desk Mode app). The app asks for actions
by writing /var/lib/cz-desk-mode/request.
"""

import array
import ctypes
import fcntl
import glob
import json
import mmap
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time

DEFAULTS_FILE = '/etc/default/cz-desk-mode'
STATE_DIR = os.environ.get('CZ_DESK_MODE_STATE', '/var/lib/cz-desk-mode')  # override for tests
SETTINGS_FILE = STATE_DIR + '/settings'
REQUEST_FILE = STATE_DIR + '/request'
STATUS_FILE = STATE_DIR + '/status'        # read by the app
DEFAULTS = {'AUTOSTART': '1', 'KEYBOARD_ON_START': 'ask'}

DESKTOP_UNIT = 'cz-desk-mode-desktop.service'
DESKTOP_ENV = {
    'LIBSEAT_BACKEND': 'noop',              # open DRM/input directly, no logind seat
    'WLR_BACKENDS': 'drm,libinput',
    'WLR_DRM_DEVICES': '/dev/dri/card0',    # HDMI only; card1 is the built-in LCD
    'XDG_SESSION_TYPE': 'wayland',
    'XDG_CURRENT_DESKTOP': 'labwc:wlroots',
    'XDG_SESSION_DESKTOP': 'rpd-labwc',
    'DESKTOP_SESSION': 'rpd-labwc',
}

KEYBOARD = '/dev/input/cardputer-zero-internal'
FRAMEBUFFER = '/dev/fb_lcd'
BACKLIGHT = '/sys/class/backlight/backlight/brightness'
LAUNCHER_PATH = '/usr/share/APPLaunch/bin/M5CardputerZero-APPLaunch'
LAUNCHER_CONFIG = os.path.expanduser('~/.config/cardputerzero/config.json')
SETTLE_SECONDS = 2          # HDMI must stay connected this long before the desktop starts
START_TRIES = 3             # labwc fails ("unable to create allocator") if HDMI bounces as it starts
POPUP_TIMEOUT = 30          # s; no answer keeps the keyboard on the Cardputer
NOTICE_SECONDS = 8          # then the LCD backlight goes off while handed over

EVENT = struct.Struct('llHHi')
EV_SYN, EV_KEY = 0, 1
KEY_ESC, KEY_ENTER, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT = 1, 28, 29, 42, 56
KEY_Y, KEY_N, KEY_Z, KEY_C, KEY_LEFT, KEY_RIGHT = 21, 49, 44, 46, 105, 106
EVIOCGRAB = 0x40044590
UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x5501, 0x5502

# Sym layer (linux,keymap1 in the CardputerZero overlay) -> (standard key, shift).
# These codes never come from the base or Fn layers, so the mapping is safe.
SYM = {
    26: (2, 1), 27: (3, 1), 39: (4, 1), 40: (5, 1), 41: (6, 1),     # ! @ # $ %
    43: (7, 1), 51: (8, 1), 52: (9, 1), 53: (10, 1), 94: (11, 1),   # ^ & * ( )
    55: (41, 1), 69: (41, 0), 70: (12, 1), 71: (12, 0),             # ~ ` _ -
    72: (13, 1), 73: (13, 0), 74: (26, 0), 75: (27, 0),             # + = [ ]
    76: (26, 1), 77: (27, 1), 79: (39, 0), 80: (39, 1),             # { } ; :
    81: (40, 0), 82: (40, 1), 83: (51, 1), 85: (52, 1),             # ' " < >
    86: (43, 0), 89: (43, 1), 90: (51, 0), 91: (52, 0),             # \ | , .
    92: (53, 0), 93: (53, 1),                                       # / ?
}


def log(*a):
    print(*a, flush=True)


def read(path, default=''):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def parse_settings(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            out[key.strip()] = value.strip().strip('"' + "'")
    return out


def load_config():
    config = dict(DEFAULTS)
    for path in (DEFAULTS_FILE, SETTINGS_FILE):
        config.update({k: v for k, v in parse_settings(read(path)).items() if k in DEFAULTS})
    return config


ENV = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}', LANG='C.UTF-8')


def run(args, timeout=20):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=ENV)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        log('command failed:', ' '.join(args), e)
        return -1, ''


# ------------------------------------------------------------------ desktop

def hdmi_connected():
    return any(read(p) == 'connected' for p in glob.glob('/sys/class/drm/card*-HDMI-A-*/status'))


def desktop_active():
    return run(['systemctl', '--user', 'is-active', DESKTOP_UNIT])[1] == 'active'


PANEL_INI = os.path.expanduser('~/.config/wf-panel-pi/wf-panel-pi.ini')
PANEL_SYSTEM_INI = '/etc/xdg/wf-panel-pi/wf-panel-pi.ini'
PANEL_DONE = STATE_DIR + '/panel-updater-removed'


def panel_keys(path):
    """{key: value} of the [panel] section of a wf-panel-pi ini file."""
    out, section = {}, None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('['):
                    section = line
                elif section == '[panel]' and '=' in line:
                    key, _, value = line.partition('=')
                    out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def panel_without_updater():
    """Once: take the update checker off the desktop's panel. It asks PackageKit
    to refresh when the panel starts, and without a login session (this
    desktop has none) polkit wants the password for that every time. Its
    'interval' setting does not stop that first check. The user's panel
    file overrides the system one key by key, so the widget lists are
    written there, the system's minus 'updater'. Done only once, so adding
    it back in the panel's settings sticks."""
    if os.path.exists(PANEL_DONE):
        return
    effective = panel_keys(PANEL_SYSTEM_INI)
    effective.update(panel_keys(PANEL_INI))
    fixed = {k: ' '.join(w for w in effective[k].split() if w != 'updater')
             for k in ('widgets_left', 'widgets_right')
             if 'updater' in effective.get(k, '').split()}
    if fixed:
        try:
            try:
                with open(PANEL_INI) as f:
                    lines = f.read().splitlines()
            except FileNotFoundError:
                lines = []
            section, done = None, set()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('['):
                    section = stripped
                elif section == '[panel]' and stripped.partition('=')[0].strip() in fixed:
                    key = stripped.partition('=')[0].strip()
                    lines[i] = f'{key}={fixed[key]}'
                    done.add(key)
            missing = [f'{k}={v}' for k, v in fixed.items() if k not in done]
            if missing:
                if '[panel]' in (l.strip() for l in lines):
                    at = [l.strip() for l in lines].index('[panel]') + 1
                    lines[at:at] = missing
                else:
                    lines = ['[panel]'] + missing + ([''] + lines if lines else [])
            os.makedirs(os.path.dirname(PANEL_INI), exist_ok=True)
            tmp = PANEL_INI + '.cz-desk-mode'
            with open(tmp, 'w') as f:
                f.write('\n'.join(lines) + '\n')
            os.replace(tmp, PANEL_INI)
            log('panel: update checker removed (it asks for a password on this desktop)')
        except OSError as e:
            log('panel:', e)
            return
    try:
        open(PANEL_DONE, 'w').close()
    except OSError:
        pass


def desktop_up(seconds=3.0):
    """True if the desktop is still running after `seconds` (labwc fails fast)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not desktop_active():
            return False
        time.sleep(0.25)
    return desktop_active()


def desktop_start():
    if desktop_active():
        return True
    panel_without_updater()
    session = '/usr/bin/labwc-pi' if os.path.exists('/usr/bin/labwc-pi') else '/usr/bin/labwc'
    args = ['systemd-run', '--user', '--unit=' + DESKTOP_UNIT, '--collect',
            '-p', 'KillMode=mixed', '-p', 'Description=Desk Mode HDMI desktop']
    args += [f'--setenv={k}={v}' for k, v in DESKTOP_ENV.items()]
    # The user manager may carry WAYLAND_DISPLAY/DISPLAY from an old session;
    # with them set, labwc would try to run nested instead of on the GPU.
    args += ['/usr/bin/env', '-u', 'WAYLAND_DISPLAY', '-u', 'DISPLAY', session]
    rc, out = run(args)
    log('desktop start:', 'ok' if rc == 0 else f'failed ({rc}) {out}')
    return rc == 0


def desktop_stop():
    rc, _ = run(['systemctl', '--user', 'stop', DESKTOP_UNIT], timeout=40)
    log('desktop stop:', 'ok' if rc == 0 else f'failed ({rc})')


# ----------------------------------------------------------------- launcher

def launcher_pid():
    for exe in glob.glob('/proc/[0-9]*/exe'):
        try:
            if os.readlink(exe) == LAUNCHER_PATH:
                return int(exe.split('/')[2])
        except OSError:
            pass
    return None


def children(pid):
    out = []
    for f in glob.glob(f'/proc/{pid}/task/*/children'):
        out += [int(c) for c in read(f).split()]
    return out


def proc_state(pid):
    stat = read(f'/proc/{pid}/stat')
    return stat.rsplit(')', 1)[1].split()[0] if ')' in stat else ''


def launcher_brightness():
    try:
        with open(LAUNCHER_CONFIG) as f:
            return max(10, int(json.load(f)['brightness']))
    except (OSError, ValueError, KeyError, TypeError):
        return 60


class LcdHold:
    """Pause the launcher and own the LCD; put everything back on release.

    Battery Saver may resume the launcher (it wakes it when the backlight
    comes on), so keep() re-pauses it and redraws whenever that happens. A
    launcher resumed while asleep turns the backlight off again, so keep()
    also puts the backlight back while the screen should be lit (`lit`)."""

    def __init__(self):
        self.pid = launcher_pid()
        # An app open in the launcher draws on the LCD too: pause it as well.
        self.apps = children(self.pid) if self.pid else []
        self.fb = None
        self.saved = None
        self.backlight = read(BACKLIGHT, '')
        self.pause()
        time.sleep(0.15)   # let an in-flight SPI frame finish
        try:
            self.fb = Framebuffer()
            self.saved = self.fb.snapshot()
        except Exception as e:
            log('framebuffer:', e)
        self.lit = self.backlight if self.backlight not in ('', '0') else str(launcher_brightness())
        set_backlight(self.lit)

    def pause(self):
        for pid in self.apps:
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError:
                pass
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGSTOP)
            except OSError:
                self.pid = None

    def keep(self):
        """True if the launcher had been resumed behind our back (redraw needed)."""
        resumed = bool(self.pid and proc_state(self.pid) not in ('T', 't'))
        if resumed:
            self.pause()
        if self.lit and read(BACKLIGHT, '') == '0':
            set_backlight(self.lit)
        return resumed

    def dark(self):
        """Turn the screen off and keep it off."""
        self.lit = None
        set_backlight(0)

    def draw(self, image):
        if self.fb:
            self.fb.draw(image)

    def release(self, backlight=None):
        if self.fb:
            if self.saved:
                self.fb.restore(self.saved)
            self.fb.close()
            self.fb = None
        if backlight is not None:
            set_backlight(backlight)
        elif self.backlight:
            set_backlight(self.backlight)
        for pid in self.apps + ([self.pid] if self.pid else []):
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass


def set_backlight(value):
    try:
        with open(BACKLIGHT, 'w') as f:
            f.write(str(value))
    except OSError as e:
        log('backlight:', e)


# -------------------------------------------------------------- framebuffer

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
    W, H = 320, 170

    def __init__(self):
        self.fd = os.open(FRAMEBUFFER if os.path.exists(FRAMEBUFFER) else '/dev/fb0', os.O_RDWR)
        self.var, self.fix = VarInfo(), FixInfo()
        fcntl.ioctl(self.fd, 0x4600, self.var)
        fcntl.ioctl(self.fd, 0x4602, self.fix)
        v = self.var
        if (v.xres, v.yres, v.bits_per_pixel) != (self.W, self.H, 16):
            os.close(self.fd)
            raise RuntimeError('unsupported LCD framebuffer')
        self.memory = mmap.mmap(self.fd, self.fix.smem_len, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def _rows(self):
        for y in range(self.H):
            yield y, (y + self.var.yoffset) * self.fix.line_length + self.var.xoffset * 2

    def snapshot(self):
        return [bytes(self.memory[s:s + self.W * 2]) for _, s in self._rows()]

    def restore(self, rows):
        for y, s in self._rows():
            self.memory[s:s + self.W * 2] = rows[y]

    def draw(self, image):
        raw = array.array('H', (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                                for r, g, b in image.getdata())).tobytes()
        for y, s in self._rows():
            self.memory[s:s + self.W * 2] = raw[y * self.W * 2:(y + 1) * self.W * 2]

    def close(self):
        self.memory.close()
        os.close(self.fd)


# --------------------------------------------------------------- LCD screens

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT_TITLE = '/usr/share/APPLaunch/share/font/Montserrat-Bold.ttf'
BG, FG, MUTED, FAINT, ROW, RULE, BLUE = '#000000', '#ffffff', '#a7b0bb', '#7d8793', '#1c232b', '#2a3139', '#4fb3ff'


def fonts():
    from PIL import ImageFont

    def load(path, size):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            return ImageFont.truetype(FONT, size)
    return load(FONT_TITLE, 17), load(FONT, 13), load(FONT_BOLD, 14), load(FONT, 11)


def screen_ask(choice, seconds_left):
    from PIL import Image, ImageDraw
    title, font, bold, small = fonts()
    im = Image.new('RGB', (320, 170), BG)
    d = ImageDraw.Draw(im)
    d.text((10, 6), 'HDMI desktop is on', font=title, fill=FG)
    d.text((10, 32), 'Use this keyboard on the HDMI display?', font=font, fill=MUTED)
    for i, (label, sub) in enumerate((('HDMI display', 'Ctrl+Alt+Esc gives it back'),
                                      ('Keep here', 'for the launcher'))):
        y = 56 + i * 38
        if i == choice:
            d.rounded_rectangle((8, y, 311, y + 34), radius=5, fill=ROW)
            d.rounded_rectangle((8, y + 6, 10, y + 28), radius=1, fill=BLUE)
        d.text((18, y + 2), label, font=bold, fill=FG)
        d.text((18, y + 19), sub, font=small, fill=FAINT)
    d.line((8, 146, 312, 146), fill=RULE)
    x = 10
    for keys, action in (('F/X', 'Choose'), ('ENTER', 'OK'), ('ESC', 'Keep here')):
        d.text((x, 152), keys, font=small, fill=FG)
        x += d.textlength(keys + ' ', font=small)
        d.text((x, 152), action, font=small, fill=FAINT)
        x += d.textlength(action, font=small) + 14
    left = f'{seconds_left}s'
    d.text((310 - d.textlength(left, font=small), 152), left, font=small, fill=FAINT)
    return im


def screen_handed_over():
    from PIL import Image, ImageDraw
    title, font, bold, small = fonts()
    im = Image.new('RGB', (320, 170), BG)
    d = ImageDraw.Draw(im)
    d.text((10, 6), 'Keyboard on HDMI', font=title, fill=FG)
    d.text((10, 40), 'Typing goes to the HDMI desktop.', font=font, fill=MUTED)
    d.text((10, 70), 'Back to the Cardputer:', font=font, fill=MUTED)
    d.rounded_rectangle((10, 92, 160, 116), radius=5, fill=ROW)
    d.text((20, 96), 'Ctrl + Alt + Esc', font=bold, fill=BLUE)
    d.text((10, 128), 'The screen turns off in a moment.', font=small, fill=FAINT)
    return im


# ----------------------------------------------------------------- keyboard

def grab(fd, on):
    fcntl.ioctl(fd, EVIOCGRAB, 1 if on else 0)


def wake_screen():
    """Wake the launcher if the LCD is dark, the way a key press would: tap Shift
    on the built-in keyboard. Battery Saver resumes the launcher on that key,
    and the launcher lights the screen and redraws itself. Turning the
    backlight on directly would show whatever stale frame is in the LCD
    (black after a sleep) until the next key. Shift alone does nothing in the
    launcher. Only works while nobody grabs the keyboard."""
    if read(BACKLIGHT, '') != '0':
        return
    try:
        fd = os.open(KEYBOARD, os.O_WRONLY)
    except OSError as e:
        log('wake screen:', e)
        set_backlight(launcher_brightness())
        return
    try:
        for value in (1, 0):
            os.write(fd, EVENT.pack(0, 0, EV_KEY, KEY_LEFTSHIFT, value))
            os.write(fd, EVENT.pack(0, 0, EV_SYN, 0, 0))
            time.sleep(0.05)
        log('screen woken')
    except OSError as e:
        log('wake screen:', e)
    finally:
        os.close(fd)


def flush_keys(fd):
    """Drop what queued up while nobody read the keyboard. The fd stays open
    between handovers, so it holds every key pressed in the launcher since;
    without this an old Esc answers the question the moment it appears."""
    try:
        while os.read(fd, EVENT.size * 64):
            pass
    except BlockingIOError:
        pass


def key_events(fd):
    """(code, value) for every EV_KEY event readable now."""
    try:
        data = os.read(fd, EVENT.size * 64)
    except BlockingIOError:
        return []
    out = []
    for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
        _, _, etype, code, value = EVENT.unpack_from(data, off)
        if etype == EV_KEY:
            out.append((code, value))
    return out


class VirtualKeyboard:
    """A uinput keyboard on the default seat, so the HDMI desktop sees it."""

    def __init__(self):
        self.fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        for code in range(1, 256):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        name = b'Desk Mode CardputerZero keyboard'
        setup = struct.pack('HHHH80sI', 0x06, 0x0c5d, 0x0001, 1, name, 0)  # BUS_VIRTUAL
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.down = set()

    def emit(self, code, value):
        os.write(self.fd, EVENT.pack(0, 0, EV_KEY, code, value) + EVENT.pack(0, 0, EV_SYN, 0, 0))
        if value:
            self.down.add(code)
        else:
            self.down.discard(code)

    def release_all(self):
        for code in list(self.down):
            self.emit(code, 0)

    def close(self):
        try:
            self.release_all()
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


class Handover:
    """The built-in keyboard, grabbed and replayed into the HDMI desktop."""

    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.virtual = VirtualKeyboard()
        try:
            grab(keyboard, True)
        except OSError:
            self.virtual.close()   # the caller logs the failure
            raise
        flush_keys(keyboard)     # not replay old launcher keys into the desktop
        self.held = set()        # physical keys down
        self.shifted = {}        # sym code -> we pressed Shift for it
        self.lcd = LcdHold()
        self.lcd.draw(screen_handed_over())
        self.dark_at = time.monotonic() + NOTICE_SECONDS
        log('keyboard -> HDMI desktop')

    def pump(self):
        """Forward pending keys; returns True when the user asked for the keyboard back."""
        for code, value in key_events(self.keyboard):
            if value == 2:          # autorepeat: the desktop repeats keys itself
                continue
            if value == 1:
                self.held.add(code)
                if code == KEY_ESC and {KEY_LEFTCTRL, KEY_LEFTALT} <= self.held:
                    return True
            else:
                self.held.discard(code)
            target, shift = SYM.get(code, (code, 0))
            if value == 1 and shift and KEY_LEFTSHIFT not in self.virtual.down:
                self.virtual.emit(KEY_LEFTSHIFT, 1)
                self.shifted[code] = True
            self.virtual.emit(target, value)
            if value == 0 and self.shifted.pop(code, False):
                self.virtual.emit(KEY_LEFTSHIFT, 0)
        return False

    def tick(self):
        if self.lcd.keep() and time.monotonic() < self.dark_at:
            self.lcd.draw(screen_handed_over())
        if self.dark_at and time.monotonic() >= self.dark_at:
            self.dark_at = 0
            self.lcd.dark()    # saves power; Battery Saver then idles the launcher

    def close(self, why):
        try:
            self.virtual.close()
        except OSError as e:
            log('virtual keyboard:', e)
        try:
            grab(self.keyboard, False)
        except OSError:
            pass
        # Put the launcher back as it was, then make sure the screen is on: the
        # keyboard is back, so the user is about to use the Cardputer.
        self.lcd.release()
        log('keyboard -> Cardputer:', why)
        if why != 'exit':
            wake_screen()


def ask_keyboard(keyboard):
    """Ask on the LCD where the keyboard should go. True = HDMI desktop."""
    try:
        grab(keyboard, True)    # the answer must not reach the paused launcher
    except OSError as e:        # EBUSY: something else holds the keyboard
        log('keyboard question skipped:', e)
        return False
    flush_keys(keyboard)
    lcd = LcdHold()
    choice, answer, why = 0, None, 'answered'
    deadline = time.monotonic() + POPUP_TIMEOUT
    shown = None
    try:
        while answer is None:
            left = int(deadline - time.monotonic()) + 1
            if left <= 0 or not hdmi_connected():
                answer, why = False, 'no answer' if left <= 0 else 'HDMI unplugged'
                break
            if lcd.keep() or shown != (choice, left):
                lcd.draw(screen_ask(choice, left))
                shown = (choice, left)
            # Short: Battery Saver resumes the launcher when the backlight comes
            # on, and keep() must re-pause it before it draws over the question.
            r, _, _ = select.select([keyboard], [], [], 0.1)
            if not r:
                continue
            for code, value in key_events(keyboard):
                if value != 1:
                    continue
                if code in (33, 103, KEY_LEFT, KEY_Z, 17):       # F, Up, Left, Z, W
                    choice = 0
                elif code in (45, 108, KEY_RIGHT, KEY_C, 31):    # X, Down, Right, C, S
                    choice = 1
                elif code == KEY_ENTER:
                    answer = choice == 0
                elif code == KEY_Y:
                    answer = True
                elif code in (KEY_ESC, KEY_N):
                    answer = False
    finally:
        # Let go of the keys still held (Enter), so the launcher sees no half press.
        time.sleep(0.2)
        flush_keys(keyboard)
        grab(keyboard, False)
        lcd.release()
    log('keyboard question:', 'HDMI display' if answer else 'keep here', f'({why})')
    if not answer and why == 'answered':
        wake_screen()   # the user is at the Cardputer; on unplug the main loop wakes it
    return answer


# --------------------------------------------------------------------- main

def uevent_socket():
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 15)   # NETLINK_KOBJECT_UEVENT
    s.bind((0, 1))
    s.setblocking(False)
    return s


def drm_changed(sock):
    changed = False
    while True:
        try:
            payload = sock.recv(65536)
        except BlockingIOError:
            return changed
        if b'SUBSYSTEM=drm' in payload.split(b'\0'):
            changed = True


def write_status(**fields):
    try:
        tmp = STATUS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(fields, f)
        os.replace(tmp, STATUS_FILE)
    except OSError as e:
        log('status:', e)


def take_request():
    try:
        with open(REQUEST_FILE) as f:
            request = f.read().strip()
        os.unlink(REQUEST_FILE)
        return request
    except OSError:
        return None


def main():
    def on_term(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)
    keyboard = os.open(KEYBOARD, os.O_RDONLY | os.O_NONBLOCK)
    uevents = uevent_socket()
    config = load_config()
    stamp = None
    handover = None
    hdmi = hdmi_connected()
    active = desktop_active()
    log(f'started: hdmi={hdmi} desktop={active} config={config}')
    just_started = False   # the desktop came up or HDMI came back: ask about the keyboard
    start_at, tries = None, 0   # when to (re)try starting the desktop
    if hdmi and not active and config['AUTOSTART'] == '1':
        start_at = 0.0
    next_check = 0.0
    try:
        while True:
            fds = [uevents] + ([keyboard] if handover else [])
            # While the handover notice shows, poll fast enough that keep()
            # re-pauses a launcher Battery Saver resumed before it draws much.
            r, _, _ = select.select(fds, [], [], 0.2 if handover and handover.dark_at else 1.0)
            now = time.monotonic()
            if handover and keyboard in r and handover.pump():
                handover.close('Ctrl+Alt+Esc')
                handover = None
            if handover:
                handover.tick()

            new_stamp = [os.path.getmtime(p) if os.path.exists(p) else None for p in (DEFAULTS_FILE, SETTINGS_FILE)]
            if new_stamp != stamp:
                stamp, config = new_stamp, load_config()

            # The uevent only wakes us early; the connector status is read every
            # second, so a missed or merged uevent cannot hide a plug or unplug.
            if uevents in r:
                drm_changed(uevents)
            now_hdmi = hdmi_connected()
            if now_hdmi != hdmi:
                hdmi = now_hdmi
                log('HDMI', 'connected' if hdmi else 'disconnected')
                if hdmi:
                    if desktop_active():
                        # Still up from before (Battery Saver waits before closing
                        # it): the keyboard went back on unplug, so ask again.
                        just_started = not handover
                    elif config['AUTOSTART'] == '1':
                        # HDMI often bounces right after plug-in; let it settle.
                        start_at, tries = now + SETTLE_SECONDS, 0
                if not hdmi:
                    start_at = None
                    if handover:
                        handover.close('HDMI unplugged')   # wakes the screen
                        handover = None
                    else:
                        wake_screen()   # unplugged: the Cardputer is in use again

            request = take_request()
            if request == 'desktop-start' and not desktop_active():
                start_at, tries = now, 0
            elif request == 'desktop-stop':
                if handover:
                    handover.close('desktop closing')
                    handover = None
                desktop_stop()
            elif request == 'keyboard-desktop' and not handover:
                if desktop_active():
                    time.sleep(1.0)   # the app has just quit; let the launcher redraw first
                    try:
                        handover = Handover(keyboard)
                    except OSError as e:
                        log('keyboard handover failed:', e)
            elif request == 'keyboard-cardputer' and handover:
                handover.close('app')
                handover = None

            if start_at is not None and now >= start_at:
                start_at = None
                if hdmi_connected() and not desktop_active():
                    tries += 1
                    if desktop_start() and desktop_up():
                        just_started = True
                    elif tries < START_TRIES and hdmi_connected():
                        log(f'desktop did not stay up (try {tries}), trying again')
                        start_at = time.monotonic() + SETTLE_SECONDS
                    else:
                        log('desktop did not start; plug HDMI in again or start it from the app')

            if just_started:
                just_started = False
                time.sleep(1.0)        # the launcher may still be busy with the app that asked
                choice = config['KEYBOARD_ON_START']
                if not (hdmi_connected() and desktop_active()):
                    pass               # gone again; the next plug-in or start asks
                elif choice == 'desktop' or (choice == 'ask' and ask_keyboard(keyboard)):
                    try:
                        handover = Handover(keyboard)
                    except OSError as e:
                        log('keyboard handover failed:', e)

            if now >= next_check or request:
                next_check = now + 5
                active = desktop_active()
                if handover and not active:
                    handover.close('desktop closed')
                    handover = None
                write_status(hdmi=hdmi_connected(), desktop=active, keyboard='desktop' if handover else 'cardputer')
    finally:
        if handover:
            handover.close('exit')
        write_status(hdmi=hdmi_connected(), desktop=desktop_active(), keyboard='cardputer')


if __name__ == '__main__':
    sys.exit(main())
