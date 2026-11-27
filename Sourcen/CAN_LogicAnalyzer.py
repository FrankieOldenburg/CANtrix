"""Logic Analyzer: digitale und analoge Signale aufzeichnen und anzeigen (sigrok-pico oder USBtingo).

Das Fenster zeigt wie PulseView je Kanal eine Zeile. Mit dem sigrok-pico (`CAN_SigrokPico.py`, bis zu 21 digitale und
3 analoge Kanäle) ist es ein allgemeiner Logic Analyzer ohne CAN-Bezug. Beim USBtingo, der die CAN-RX-Leitung
aufzeichnet (`recording_start`/`recording_stop` des python-can-Plugins `python-can-usbtingo`), wird der Pegel als CAN
dekodiert: darunter die einzelnen Bits (Stuff-Bits blau) und die CAN-Felder (ID, Steuerbits, Datenbytes, CRC, EOF …).
Aufzeichnungen sind Sigrok-Dateien (`.sr`). Lesen und Dekodieren brauchen nur die Standardbibliothek (Python 3.6).
"""
import bisect
import contextlib
import math
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from array import array

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # pragma: no cover
    tk = None

from CAN_Common import C, Theme, apply_appearance, attach_dialog, draw_open_icon, draw_save_icon, icon_hue, \
    icon_line, initial_geometry, window_geometry
from CAN_Language import language, set_text, tr
from CAN_SigrokPico import ANALOG, DIGITAL, PicoRecording, analog_limit, list_ports, write_sigrok

BITRATES = [10000, 20000, 50000, 100000, 125000, 250000, 500000, 800000, 1000000]
DATA_BITRATES = [1000000, 2000000, 4000000, 5000000, 8000000]
SAMPLE_RATES = [1000000, 2000000, 5000000, 10000000, 20000000, 40000000]  # of the USBtingo: 120 MHz / 3 … 255
PICO_RATES = [10000, 50000, 100000, 160000, 240000, 400000, 500000] + SAMPLE_RATES + [60000000, 120000000]
SAMPLE_POINT = 0.75
DLC_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
MIN_SAMPLES_PER_BIT = 4
DEVICES = ('USBtingo', 'sigrok-pico')  # recording hardware
PICO_AUTO_RATE = 5000000  # "Auto" of the sigrok-pico without analog channels

_CWD_LOCK = threading.Lock()


# ---------------------------------------------------------------- Sigrok files

def read_sigrok(path):
    """Reads a Sigrok session file (`.sr`, a zip): returns {'rate': Hz, 'digital': [(name, levels as bytes with 0/1
    per sample)], 'analog': [(name, array 'f' of volts)]}."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if 'metadata' not in names:
            raise ValueError(tr('Keine Sigrok-Datei (Eintrag "metadata" fehlt).'))
        meta = archive.read('metadata').decode('utf-8', 'replace')
        match = re.search(r'^samplerate=\s*([\d.]+)\s*([kMG]?)Hz', meta, re.M | re.I)
        if not match:
            raise ValueError(tr('Die Sigrok-Datei enthält keine Abtastrate.'))
        rate = float(match.group(1)) * {'': 1, 'k': 1e3, 'm': 1e6, 'g': 1e9}[match.group(2).lower()]
        unit = int((re.search(r'^unitsize=\s*(\d+)', meta, re.M) or [0, 1])[1]) or 1
        capture = (re.search(r'^capturefile=\s*(\S+)', meta, re.M) or [0, 'logic-1'])[1]

        def chunks(prefix):
            found = [(re.fullmatch(re.escape(prefix) + r'-(\d+)', name), name) for name in names]
            found = sorted((int(match.group(1)), name) for match, name in found if match)
            return b''.join(archive.read(name) for _, name in found)

        probes = [(int(number) - 1, name) for number, name in re.findall(r'^probe(\d+)=\s*(.*?)\s*$', meta, re.M)]
        data = chunks(capture)
        if data and not probes:
            probes = [(0, 'CAN')]
        digital = []
        for bit, name in sorted(probes):
            if data and bit // 8 < unit:
                table = bytes((value >> (bit % 8)) & 1 for value in range(256))
                digital.append((name, data[bit // 8::unit].translate(table)))
        analog = []
        for number, name in sorted((int(a), b) for a, b in re.findall(r'^analog(\d+)=\s*(.*?)\s*$', meta, re.M)):
            raw = chunks('analog-1-%d' % number)
            values = array('f')
            values.frombytes(raw[:len(raw) // 4 * 4])
            if sys.byteorder != 'little':
                values.byteswap()
            if values:
                analog.append((name, values))
        if not digital and not analog:
            raise ValueError(tr('Die Sigrok-Datei enthält keine Messdaten.'))
    return {'rate': int(round(rate)), 'digital': digital, 'analog': analog}


def split_name(name):
    """A channel name written with a label ('D2 SCL') gives ('D2', 'SCL'), any other name (name, '')."""
    match = re.match(r'^(D\d+|A\d) +(\S.*)$', name)
    return (match.group(1), match.group(2)) if match else (name, '')


def find_edges(levels):
    """Indices of the samples at which the level changes (array of unsigned)."""
    edges = array('I')
    if not levels:
        return edges
    level, pos, find = levels[0], 0, levels.find
    while True:
        pos = find(1 - level, pos)
        if pos < 0:
            return edges
        edges.append(pos)
        level = 1 - level


# ---------------------------------------------------------------- CAN decoder

class EndOfData(Exception):
    pass


class FrameError(Exception):
    pass


class Decoder(object):
    """Decodes classical CAN and CAN FD (bit rate switch) from the sampled levels of the CAN RX line.

    Result: starts/ends (sample index of each bit), flags (bit 0 value, bit 1 stuff bit, bit 2 data phase),
    fields (start, end, label, detail, kind) and frames (start, end, text)."""

    def __init__(self, levels, rate, nominal, data_rate=None, sample_point=SAMPLE_POINT, cancel=None,
                 progress=None):
        self.d = levels
        self.n = len(levels)
        self.tb = rate / float(nominal)
        self.tbd = rate / float(data_rate or nominal)
        self.sp = sample_point
        self.cancel = cancel
        self.progress = progress
        self.starts, self.ends, self.flags = array('I'), array('I'), bytearray()
        self.fields, self.frames = [], []
        self._fields = []  # (first bit, last bit, label, detail, kind) while decoding
        self.start, self.prev, self.prev_pt, self.fast, self.last = 0.0, 1, -1, False, 1
        self.bit_start = 0.0
        self.run_val, self.run_len = 1, 0

    # --- bits
    def raw(self):
        """Next bit (stuff bits included), synchronised to the edge before it."""
        tb = self.tbd if self.fast else self.tb
        d, s = self.d, self.start
        pt = int(s + tb * self.sp)
        if pt >= self.n:
            raise EndOfData()
        v = d[pt]
        if v != self.prev:
            j = d.find(v, self.prev_pt + 1, pt + 1)
            if j >= 0:
                s = float(j)
                pt = int(s + tb * self.sp)
                if pt >= self.n:
                    raise EndOfData()
                v = d[pt]
        self.prev, self.prev_pt, self.last = v, pt, v
        self.bit_start = s
        self.start = s + tb
        self.starts.append(int(s))
        self.ends.append(int(s + tb))
        self.flags.append(v | (4 if self.fast else 0))
        return v

    def bit(self):
        """Next data bit with dynamic destuffing; returns (value, index)."""
        while True:
            v = self.raw()
            index = len(self.flags) - 1
            if self.run_len == 5:  # a stuff bit
                self.flags[index] |= 2
                if v == self.run_val:
                    raise FrameError(tr('Stuff-Fehler'))
                self.run_val, self.run_len = v, 1
                continue
            if v == self.run_val:
                self.run_len += 1
            else:
                self.run_val, self.run_len = v, 1
            return v, index

    def trailing_stuff(self):
        """A stuff bit after the last bit of a stuffed region (when the last five bits were equal)."""
        if self.run_len == 5:
            v = self.raw()
            self.flags[-1] |= 2
            if v == self.run_val:
                raise FrameError(tr('Stuff-Fehler'))

    def fixed_stuff(self):
        before = self.last
        v = self.raw()
        self.flags[-1] |= 2
        if v == before:
            raise FrameError(tr('Stuff-Fehler (festes Stuff-Bit)'))

    def take(self, count):
        value, first, index = 0, None, -1
        for _ in range(count):
            v, index = self.bit()
            if first is None:
                first = index
            value = (value << 1) | v
        return value, first, index

    def field(self, first, last, label, detail, kind):
        self._fields.append((first, last, label, detail, kind))

    # --- frame
    def frame(self, sof):
        """Decodes the frame whose start bit begins at sample sof; returns the sample after its end."""
        self.start, self.prev, self.prev_pt, self.fast, self.last = float(sof), 1, sof - 1, False, 1
        first_bit = len(self.flags)
        self.raw()
        self.run_val, self.run_len = 0, 1
        self.field(first_bit, first_bit, 'SOF', tr('Start of Frame'), 'ctrl')
        ident, a, b = self.take(11)
        self.field(a, b, 'ID: %d (0x%X)' % (ident, ident), tr('Identifier: %d (0x%X)') % (ident, ident), 'id')
        x1, i1, _ = self.take(1)  # RTR (classic) or SRR (extended); in CAN FD RRS
        ide, i2, _ = self.take(1)
        brs = low = 0
        if not ide:  # base format
            fd, i3, _ = self.take(1)  # FDF, in classical CAN the reserved bit r0
            self.field(i1, i1, 'RRS' if fd else 'RTR', ('RRS: %d' if fd else 'RTR: %d') % x1, 'flag')
            self.field(i2, i2, 'IDE', tr('IDE: 0 (Standard-ID)'), 'flag')
            self.field(i3, i3, 'FDF' if fd else 'r0', 'FDF: 1 (CAN FD)' if fd else 'r0: 0', 'flag')
            rtr = x1
            identifier = ident
        else:  # extended format
            self.field(i1, i1, 'SRR', 'SRR: %d' % x1, 'flag')
            self.field(i2, i2, 'IDE', tr('IDE: 1 (Extended-ID)'), 'flag')
            low, a, b = self.take(18)
            identifier = (ident << 18) | low
            self.field(a, b, 'Full ID: %d (0x%X)' % (identifier, identifier),
                       tr('Full ID (29 Bit): %d (0x%X)') % (identifier, identifier), 'fullid')
            rtr, i3, _ = self.take(1)  # RTR, in CAN FD r1
            fd, i4, _ = self.take(1)  # FDF, in classical CAN r1
            self.field(i3, i3, 'r1' if fd else 'RTR', ('r1: %d' if fd else 'RTR: %d') % rtr, 'flag')
            self.field(i4, i4, 'FDF' if fd else 'r1', 'FDF: 1 (CAN FD)' if fd else 'r1: 0', 'flag')
            if not fd:
                v, a, b = self.take(1)
                self.field(a, b, 'r0', 'r0: %d' % v, 'flag')
        if fd:
            v, a, b = self.take(1)
            self.field(a, b, 'r0', 'r0: %d' % v, 'flag')
            brs, a, b = self.take(1)
            self.field(a, b, 'BRS', tr('BRS: 1 (Datenphase schneller)') if brs else 'BRS: 0', 'flag')
            if brs:  # the data phase begins at the sample point of the BRS bit
                self.fast = True
                self.start = self.bit_start + self.tb * self.sp + self.tbd * (1 - self.sp)
                self.ends[-1] = int(self.start)
            esi, a, b = self.take(1)
            self.field(a, b, 'ESI', 'ESI: %d' % esi, 'flag')
        dlc, a, b = self.take(4)
        length = DLC_LENGTHS[dlc] if fd else min(dlc, 8)
        if not fd and rtr:
            length = 0
        self.field(a, b, 'DLC: %d' % dlc, tr('DLC: %d (%d Datenbytes)') % (dlc, length), 'dlc')
        data = []
        for number in range(length):
            value, a, b = self.take(8)
            data.append(value)
            self.field(a, b, 'DB', tr('Datenbyte %d: 0x%02X') % (number, value), 'data')
        if fd:  # stuff count and CRC with fixed stuff bits (one before every group of four bits)
            self.trailing_stuff()
            crc_bits = 17 if length <= 16 else 21
            values, indices = [], []
            for position in range(4 + crc_bits):
                if position % 4 == 0:
                    self.fixed_stuff()
                values.append(self.raw())
                indices.append(len(self.flags) - 1)
            count = (values[0] << 2) | (values[1] << 1) | values[2]
            count ^= (count >> 1) ^ (count >> 2)  # Gray code
            self.field(indices[0], indices[3], 'SC: %d' % count,
                       tr('Stuff-Zähler: %d (Parität %d)') % (count, values[3]), 'stuffcount')
            crc = 0
            for v in values[4:]:
                crc = (crc << 1) | v
            self.field(indices[4], indices[-1], 'CRC-%d' % crc_bits, tr('CRC-%d: 0x%X') % (crc_bits, crc), 'crc')
        else:
            crc, a, b = self.take(15)
            self.field(a, b, 'CRC-15', tr('CRC-15: 0x%X') % crc, 'crc')
            self.trailing_stuff()
        v = self.raw()
        index = len(self.flags) - 1
        self.field(index, index, 'CRC-D', tr('CRC-Delimiter'), 'ctrl')
        if v != 1:
            raise FrameError(tr('Form-Fehler: CRC-Delimiter'))
        if self.fast:  # back to the nominal bit rate
            self.fast = False
            self.start = self.bit_start + self.tbd * self.sp + self.tb * (1 - self.sp)
            self.ends[-1] = int(self.start)
        v = self.raw()
        index = len(self.flags) - 1
        self.field(index, index, 'ACK', tr('ACK-Slot: dominant (bestätigt)') if v == 0
                   else tr('ACK-Slot: rezessiv (nicht bestätigt)'), 'ctrl')
        v = self.raw()
        index = len(self.flags) - 1
        self.field(index, index, 'ACK-D', tr('ACK-Delimiter'), 'ctrl')
        if v != 1:
            raise FrameError(tr('Form-Fehler: ACK-Delimiter'))
        first = len(self.flags)
        for _ in range(7):
            if self.raw() != 1:
                raise FrameError(tr('Form-Fehler: EOF'))
        self.field(first, len(self.flags) - 1, 'EOF', tr('End of Frame'), 'eof')
        text = '0x%X%s%s: %s' % (identifier, ' FD' if fd else '', ' BRS' if brs else '',
                                 ' '.join('%02X' % v for v in data))
        self.frames.append((int(self.starts[first_bit]), int(self.ends[-1]), text))
        return int(self.start)

    def flush_fields(self):
        for first, last, label, detail, kind in self._fields:
            first = max(0, min(first, len(self.starts) - 1))
            last = max(first, min(last, len(self.starts) - 1))
            self.fields.append((int(self.starts[first]), int(self.ends[last]), label, detail, kind))
        self._fields = []

    # --- search
    def resync(self, pos):
        """Position after a stretch of at least 7 recessive bit times (idle bus) at or after pos."""
        d, need = self.d, int(7 * self.tb)
        while pos < self.n:
            zero = d.find(0, pos)
            if zero < 0:
                return self.n
            one = d.find(1, zero)
            if one < 0:
                return self.n
            nxt = d.find(0, one)
            if (self.n if nxt < 0 else nxt) - one >= need:
                return one + need
            pos = one
        return self.n

    def run(self):
        d, pos = self.d, 0
        if self.n < 2:
            return
        if d[0] == 0:
            pos = self.resync(0)
        count = 0
        while pos < self.n:
            if self.cancel is not None and self.cancel():
                return
            sof = d.find(0, pos)
            if sof < 0:
                break
            try:
                pos = self.frame(sof)
            except EndOfData:
                break
            except FrameError as error:
                index = len(self.flags) - 1
                self.field(index, index, tr('Fehler'), str(error), 'error')
                pos = self.resync(int(self.start))
            self.flush_fields_safe()
            count += 1
            if self.progress is not None and count % 50 == 0:
                self.progress(pos / float(self.n))
        self.flush_fields_safe()

    def flush_fields_safe(self):
        if self._fields:
            self.flush_fields()


def decode(levels, rate, nominal, data_rate=None, cancel=None, progress=None):
    """Decodes the levels; returns a dict (starts, ends, flags, fields, frames, field_starts) or raises ValueError."""
    if rate / float(nominal) < MIN_SAMPLES_PER_BIT or (data_rate and rate / float(data_rate) < MIN_SAMPLES_PER_BIT):
        raise ValueError(tr('Die Abtastrate (%s MHz) ist für diese Bitrate zu niedrig (mindestens %d Abtastwerte '
                            'je Bit).') % ('%g' % (rate / 1e6), MIN_SAMPLES_PER_BIT))
    decoder = Decoder(levels, rate, nominal, data_rate, cancel=cancel, progress=progress)
    decoder.run()
    return {'starts': decoder.starts, 'ends': decoder.ends, 'flags': decoder.flags, 'fields': decoder.fields,
            'frames': decoder.frames, 'field_starts': [field[0] for field in decoder.fields]}


# ---------------------------------------------------------------- time axis

def nice_step(minimum):
    """Smallest 1, 2 or 5 times a power of ten that is at least minimum."""
    if minimum <= 0:
        return 1.0
    exponent = math.floor(math.log10(minimum))
    for factor in (1, 2, 5, 10):
        step = factor * 10 ** exponent
        if step >= minimum * 0.999:
            return step


def time_unit(step):
    """(factor, name) of the unit in which ticks of this step are shown."""
    if step < 1e-6:
        return 1e-9, 'ns'
    if step < 1e-3:
        return 1e-6, 'µs'
    if step < 1:
        return 1e-3, 'ms'
    return 1.0, 's'


def number_text(value, decimals):
    text = '%.*f' % (decimals, value)
    return text.replace('.', ',') if language() == 'de' else text


def time_text(seconds):
    """Time with a sensible unit, e.g. '340,50 µs'."""
    factor, unit = time_unit(abs(seconds) or 1e-3)
    return '%s %s' % (number_text(seconds / factor, 2), unit)


# ---------------------------------------------------------------- icons

def draw_record_icon(canvas, color):
    """Red dot: start recording."""
    canvas.create_oval(9, 9, 21, 21, fill=C.DANGER, outline=C.DANGER, tags=('icon',))


def draw_stop_square_icon(canvas, color):
    canvas.create_rectangle(9, 9, 21, 21, fill=C.DANGER, outline=C.DANGER, width=1.5, tags=('icon',))


def draw_zoom_icon(plus):
    def draw(canvas, color):
        blue = icon_hue(5)
        canvas.create_oval(8, 8, 19, 19, outline=blue, width=1.5, tags=('icon',))
        canvas.create_line(18, 18, 23, 23, **icon_line(blue, 2))
        canvas.create_line(11, 13.5, 16, 13.5, **icon_line(blue))
        if plus:
            canvas.create_line(13.5, 11, 13.5, 16, **icon_line(blue))
    return draw


def draw_fit_icon(canvas, color):
    line = icon_line(icon_hue(5), 1.5)
    canvas.create_line(8, 9, 8, 21, **line)
    canvas.create_line(22, 9, 22, 21, **line)
    canvas.create_line(11, 15, 19, 15, **line)
    canvas.create_line(11, 15, 14, 12, 11, 15, 14, 18, **line)
    canvas.create_line(19, 15, 16, 12, 19, 15, 16, 18, **line)


# ---------------------------------------------------------------- window

LABEL_WIDTH = 140  # left column with the channel names, wider for long labels
LABEL_WIDTH_MAX = 320
RULER_HEIGHT = 30
WAVE_HEIGHT = 64
ROW_HEIGHT = 34
CHANNEL_HEIGHT = 32  # digital channel when there are several
ANALOG_HEIGHT = 60
KIND_COLORS = {'id': 6, 'fullid': 4, 'data': 2, 'crc': 1, 'eof': 3, 'flag': 5, 'dlc': 7, 'ctrl': 7,
               'stuffcount': 5}
DARK_TEXT = '#111827'


@contextlib.contextmanager
def working_directory(path):
    """The plugin writes its temporary sample file into the current directory; this keeps it out of the project."""
    with _CWD_LOCK:
        old = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(old)


if tk is not None:

    class LogicApp(object):
        def __init__(self, root, settings=None, connection=None):
            """root: Tk, Toplevel or DockFrame. settings: window settings. connection: settings of the recorder
            (bit rates). get_bus (set by the analyzer) returns the open USBtingo of a running recording or None."""
            self.root = root
            self.settings = dict(settings or {})
            connection = connection or {}
            self.get_bus = None
            self.capture = None  # dict: path, rate, levels, edges, name
            self.decoded = None
            self.view_start, self.spp = 0.0, 1.0
            self.recording = None  # dict while recording: bus, owned, folder, path, started
            self.busy = False
            self.temp_folder = None
            self.queue = queue.Queue()
            self.job = 0
            self.poll_job = None
            self.decode_job = None
            self.timer_job = None
            self.hover = None
            self._fresh = True
            self.drag = None
            self.status = tk.StringVar()
            self.theme = Theme(root)
            saved = lambda key, default: self.settings.get(key, default)
            self.bitrate = tk.StringVar(value=str(saved('bitrate', connection.get('bitrate', 500000))))
            self.fd = tk.BooleanVar(value=bool(saved('fd', connection.get('fd', False))))
            self.data_bitrate = tk.StringVar(value=str(saved('data_bitrate', connection.get('data_bitrate', 2000000))))
            self.sample_rate = tk.StringVar(value=str(saved('sample_rate', 'Auto')))
            self.connection = dict(connection)
            device = saved('device', DEVICES[0])
            self.device = tk.StringVar(value=device if device in DEVICES else DEVICES[0])
            self.port = tk.StringVar(value=str(saved('port', 'Auto')))
            channels = saved('channels', [saved('input', DIGITAL[0])])
            if not isinstance(channels, list):
                channels = []
            self.channels = dict((name, tk.BooleanVar(value=name in channels)) for name in ANALOG + DIGITAL)
            if not self.selected_channels():
                self.channels[DIGITAL[0]].set(True)
            self.input = tk.StringVar(value=str(saved('input', DIGITAL[0])))  # channel decoded as CAN
            labels = saved('labels', {})
            labels = labels if isinstance(labels, dict) else {}
            self.labels = dict((name, tk.StringVar(value=str(labels.get(name, '')))) for name in ANALOG + DIGITAL)
            self.label_width = LABEL_WIDTH
            self.label_job = None
            self.channel_text = tk.StringVar()
            self.channel_window = None
            self.top = 0  # vertical scroll position (pixels)
            self._build_layout()
            for variable in self.labels.values():
                variable.trace_add('write', self._label_changed)
            root.title('Logic Analyzer')
            root.minsize(760, 340)
            root.geometry(initial_geometry(root, self.settings, '1100x430'))
            set_text(self.status, 'Bereit')
            self._update_buttons()

        # ------------------------------------------------------------ layout

        def _build_layout(self):
            theme = self.theme
            theme.header(self.root, 'Logic Analyzer', tr('Signale aufzeichnen und anzeigen'))
            theme.footer(self.root, self.status)
            body = tk.Frame(self.root, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            card, bar = theme.pane(body, tr('Aufzeichnung'))
            card.pack(fill=tk.BOTH, expand=True)
            buttons = (
                ('open', draw_open_icon, self.open_file, tr('Aufzeichnung öffnen… (Sigrok-Datei .sr)')),
                ('save', draw_save_icon, self.save_file, tr('Aufzeichnung speichern unter…')),
                ('record', draw_record_icon, self.start_recording, tr('Aufzeichnung starten')),
                ('stop', draw_stop_square_icon, self.stop_recording, tr('Aufzeichnung beenden')),
                ('zoom_in', draw_zoom_icon(True), lambda: self.zoom(0.5), tr('Hineinzoomen')),
                ('zoom_out', draw_zoom_icon(False), lambda: self.zoom(2.0), tr('Herauszoomen')),
                ('fit', draw_fit_icon, self.fit, tr('Alles anzeigen')),
            )
            self.buttons = {}
            for key, draw, command, tip in buttons:
                button = theme.icon_button(bar, draw, command, tip)
                button.pack(side=tk.LEFT, padx=(8 if key in ('open', 'zoom_in') else 2, 2), pady=3)
                self.buttons[key] = button
            source = ttk.Frame(card, padding=(12, 8, 12, 0))
            source.pack(side=tk.TOP, fill=tk.X)
            ttk.Label(source, text=tr('Gerät:')).pack(side=tk.LEFT)
            self.device_box = ttk.Combobox(source, textvariable=self.device, values=DEVICES, state='readonly',
                                           width=12)
            self.device_box.pack(side=tk.LEFT, padx=(4, 12))
            self.device_box.bind('<<ComboboxSelected>>', lambda event: self._device_changed())
            self.pico_row = ttk.Frame(source)  # only for the sigrok-pico
            ttk.Label(self.pico_row, text=tr('Anschluss:')).pack(side=tk.LEFT)
            self.port_box = ttk.Combobox(self.pico_row, textvariable=self.port, width=24,
                                         postcommand=self._update_ports)
            self.port_box.pack(side=tk.LEFT, padx=(4, 12))
            self.channel_button = ttk.Button(self.pico_row, text=tr('Kanäle…'), command=self.choose_channels)
            self.channel_button.pack(side=tk.LEFT)
            ttk.Label(self.pico_row, textvariable=self.channel_text).pack(side=tk.LEFT, padx=(6, 12))
            self.input_label = ttk.Label(source, text=tr('CAN-Kanal:'))
            self.input_box = ttk.Combobox(source, textvariable=self.input, state='readonly', width=6)
            self.input_box.bind('<<ComboboxSelected>>', lambda event: self._can_changed())
            settings = ttk.Frame(card, padding=(12, 8, 12, 4))
            settings.pack(side=tk.TOP, fill=tk.X)
            self.can_row = ttk.Frame(settings)  # decoding, only for the USBtingo
            self.can_row.pack(side=tk.LEFT)
            ttk.Label(self.can_row, text=tr('Bitrate:')).pack(side=tk.LEFT)
            bitrate = ttk.Combobox(self.can_row, textvariable=self.bitrate, values=BITRATES, width=9)
            bitrate.pack(side=tk.LEFT, padx=(4, 12))
            ttk.Checkbutton(self.can_row, text='CAN FD', variable=self.fd, command=self._settings_changed).pack(
                side=tk.LEFT, padx=(0, 4))
            ttk.Label(self.can_row, text=tr('Datenrate:')).pack(side=tk.LEFT, padx=(8, 0))
            data = self.data_box = ttk.Combobox(self.can_row, textvariable=self.data_bitrate, values=DATA_BITRATES,
                                                width=9)
            data.pack(side=tk.LEFT, padx=(4, 12))
            self.rate_label = ttk.Label(settings, text=tr('Abtastrate (Aufzeichnung):'))
            self.rate_label.pack(side=tk.LEFT)
            rate = self.rate_box = ttk.Combobox(settings, textvariable=self.sample_rate, width=10)
            rate.pack(side=tk.LEFT, padx=(4, 0))
            for box in (bitrate, data, rate):
                box.bind('<<ComboboxSelected>>', lambda event: self._settings_changed())
                box.bind('<Return>', lambda event: self._settings_changed())
                box.bind('<FocusOut>', lambda event: self._settings_changed())
            self._update_data_box()
            self._update_device()
            area = tk.Frame(card, background=C.SURFACE)
            area.pack(fill=tk.BOTH, expand=True, padx=1, pady=(4, 1))
            self.scroll = ttk.Scrollbar(area, orient=tk.HORIZONTAL, command=self._on_scrollbar)
            self.scroll.pack(side=tk.BOTTOM, fill=tk.X)
            self.vscroll = ttk.Scrollbar(area, orient=tk.VERTICAL, command=self._on_vscrollbar)
            self.vscroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.canvas = tk.Canvas(area, background=C.SURFACE, highlightthickness=0, height=240)
            self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self.font = tkfont.Font(font=theme.font_axis)
            canvas = self.canvas
            canvas.bind('<Configure>', lambda event: self.render())
            canvas.bind('<MouseWheel>', self._on_wheel)
            canvas.bind('<Shift-MouseWheel>', self._on_pan_wheel)
            canvas.bind('<Button-4>', lambda event: self.zoom(0.8, event.x))
            canvas.bind('<Button-5>', lambda event: self.zoom(1.25, event.x))
            canvas.bind('<ButtonPress-1>', self._on_press)
            canvas.bind('<B1-Motion>', self._on_drag)
            canvas.bind('<ButtonRelease-1>', lambda event: setattr(self, 'drag', None))
            canvas.bind('<Double-Button-1>', self._on_double)
            canvas.bind('<Motion>', self._on_motion)
            canvas.bind('<Leave>', lambda event: self._show_hover(None))

        def _update_device(self):
            pico = self.device.get() == 'sigrok-pico'
            if pico:
                self.pico_row.pack(side=tk.LEFT, after=self.device_box)
            else:
                self.pico_row.pack_forget()
                self._close_channels()
            if pico:
                self.can_row.pack_forget()
            else:
                self.can_row.pack(side=tk.LEFT, padx=(0, 8), before=self.rate_label)
            self.rate_box.configure(values=['Auto'] + (PICO_RATES if pico else SAMPLE_RATES))
            self._update_channels()

        def decoding(self):
            """CAN is decoded only for the USBtingo; the sigrok-pico is a general logic analyzer."""
            return self.device.get() != 'sigrok-pico'

        def _device_changed(self):
            self._update_device()
            self.decoded = None
            if self.capture is not None:
                self._use_can_channel()
                self._start_decode()
            self.render()

        def label_of(self, name):
            """Label the user gave a channel of the sigrok-pico, else ''."""
            if self.decoding() or name not in self.labels:
                return ''
            return self.labels[name].get().strip()

        def _label_changed(self, *args):
            if self.label_job is None:
                self.label_job = self.root.after(200, self._show_labels)

        def _show_labels(self):
            self.label_job = None
            self.render()

        def selected_channels(self):
            return [name for name in ANALOG + DIGITAL if self.channels[name].get()]

        def _can_choices(self):
            """Channels of the capture that can be decoded as CAN."""
            if self.capture is None or not self.decoding():
                return []
            return [name for name, _, _ in self.capture['digital']]

        def can_channel(self):
            """Name of the channel of the capture that is decoded as CAN, or None."""
            names = self._can_choices()
            if not names:
                return None
            if self.input.get() in names:
                return self.input.get()
            return next((name for name in names if name.upper() == 'CAN'), names[0])

        def _update_channels(self):
            """Text of the channel selection and the choice of the CAN channel."""
            parts = []
            for group in (ANALOG, DIGITAL):
                chosen = [name for name in group if self.channels[name].get()]
                if len(chosen) > 2 and group.index(chosen[-1]) - group.index(chosen[0]) == len(chosen) - 1:
                    parts.append('%s–%s' % (chosen[0], chosen[-1]))
                elif len(chosen) > 4:
                    parts.append('%d × %s' % (len(chosen), group[0][0]))
                else:
                    parts.extend(chosen)
            self.channel_text.set(', '.join(parts))
            names = self._can_choices()
            self.input_box.configure(values=names)
            if self.can_channel() is not None:
                self.input.set(self.can_channel())
            if len(names) > 1:  # a file with several channels
                self.input_label.pack(side=tk.LEFT)
                self.input_box.pack(side=tk.LEFT, padx=(4, 0))
            else:
                self.input_label.pack_forget()
                self.input_box.pack_forget()

        def _can_changed(self):
            if self.capture is not None:
                self._use_can_channel()
                self.decoded = None
                self.render()
                self._start_decode()

        def _use_can_channel(self):
            """Take over levels and edges of the CAN channel into the capture."""
            name = self.can_channel()
            found = next((entry for entry in self.capture['digital'] if entry[0] == name), None)
            self.capture['can'] = name
            self.capture['levels'], self.capture['edges'] = found[1:] if found else (None, None)

        # ------------------------------------------------------------ channel selection

        def choose_channels(self):
            if self.channel_window is not None:
                self.channel_window.lift()
                self.channel_window.focus_force()
                return
            window = self.channel_window = tk.Toplevel(self.root)
            apply_appearance(window)
            window.title(tr('Kanalauswahl'))
            window.resizable(False, False)
            attach_dialog(window, self.root)
            theme = Theme(window)
            body = tk.Frame(window, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            for title, group, rows in ((tr('Analog'), ANALOG, 3), (tr('Digital'), DIGITAL, 7)):
                card, bar = theme.pane(body, title)
                card.pack(fill=tk.X, pady=(0, 10))
                for text, value in ((tr('Keine'), False), (tr('Alle'), True)):
                    ttk.Button(bar, text=text, command=lambda group=group, value=value:
                               self._set_channels(group, value)).pack(side=tk.RIGHT, padx=2)
                grid = ttk.Frame(card, padding=(12, 8))
                grid.pack(fill=tk.X)
                for index, name in enumerate(group):  # one column after the other: check mark and label
                    row, column = index % rows, 2 * (index // rows)
                    ttk.Checkbutton(grid, text=name, width=4, variable=self.channels[name],
                                    command=self._channels_changed).grid(row=row, column=column, sticky=tk.W, pady=2)
                    ttk.Entry(grid, textvariable=self.labels[name], width=14).grid(row=row, column=column + 1,
                                                                                   padx=(0, 16), pady=2)
            ttk.Label(body, text=tr('Das Textfeld neben einem Kanal ist seine Bezeichnung.')).pack(anchor=tk.W)
            ttk.Label(body, text=tr('Analog: höchstens 400 / 240 / 160 kHz bei 1 / 2 / 3 Kanälen.')).pack(anchor=tk.W)
            ttk.Button(body, text=tr('Schließen'), command=self._close_channels).pack(anchor=tk.E, pady=(8, 0))
            window.protocol('WM_DELETE_WINDOW', self._close_channels)
            window.bind('<Escape>', lambda event: self._close_channels())

        def _set_channels(self, group, value):
            for name in group:
                self.channels[name].set(value)
            self._channels_changed()

        def _channels_changed(self):
            self._update_channels()
            if self.capture is None:
                self.render()  # the empty rows show the selected channels

        def _close_channels(self):
            if self.channel_window is not None:
                self.channel_window.destroy()
                self.channel_window = None

        def _update_ports(self):
            self.port_box.configure(values=['Auto'] + list_ports())

        def _settings_changed(self):
            self._update_data_box()
            if self.capture is not None:
                if self.decode_job is not None:
                    self.root.after_cancel(self.decode_job)
                self.decode_job = self.root.after(250, self._start_decode)

        def _update_data_box(self):
            self.data_box.configure(state='normal' if self.fd.get() else 'disabled')

        def _update_buttons(self):
            recording = self.recording is not None
            idle = not recording and not self.busy
            for key, enabled in (('open', idle), ('save', idle and self.capture is not None),
                                 ('record', idle), ('stop', recording and not self.busy),
                                 ('zoom_in', self.capture is not None), ('zoom_out', self.capture is not None),
                                 ('fit', self.capture is not None)):
                self.buttons[key].state(['!disabled' if enabled else 'disabled'])
            self.device_box.configure(state='readonly' if idle else 'disabled')
            self.port_box.configure(state='normal' if idle else 'disabled')
            self.input_box.configure(state='readonly' if idle else 'disabled')
            self.channel_button.state(['!disabled' if idle else 'disabled'])
            if not idle:
                self._close_channels()

        # ------------------------------------------------------------ rates

        def rates(self):
            """(nominal, data) bit rate from the fields; ValueError for invalid input."""
            nominal = int(self.bitrate.get())
            data = int(self.data_bitrate.get()) if self.fd.get() else None
            if nominal <= 0 or (data is not None and data <= 0):
                raise ValueError
            return nominal, data

        def sampling_rate(self):
            """Rate for recording: None = automatic (10 x bit rate)."""
            text = self.sample_rate.get().strip()
            return None if text.lower() in ('', 'auto') else int(text)

        def set_connection(self, connection):
            """The recorder settings changed: take over bit rates."""
            self.connection = dict(connection)
            self.bitrate.set(str(connection.get('bitrate', self.bitrate.get())))
            self.fd.set(bool(connection.get('fd', False)))
            self.data_bitrate.set(str(connection.get('data_bitrate', self.data_bitrate.get())))
            self._settings_changed()

        # ------------------------------------------------------------ files and decoding

        def open_file(self):
            path = filedialog.askopenfilename(parent=self.root, title=tr('Aufzeichnung öffnen'),
                                              filetypes=[(tr('Sigrok-Aufzeichnung'), '*.sr'),
                                                         (tr('Alle Dateien'), '*')])
            if path:
                self.load(path)

        def save_file(self):
            if self.capture is None:
                return
            name = os.path.basename(self.capture['path'])
            path = filedialog.asksaveasfilename(parent=self.root, title=tr('Aufzeichnung speichern unter…'),
                                                initialfile=name, defaultextension='.sr',
                                                filetypes=[(tr('Sigrok-Aufzeichnung'), '*.sr')])
            if not path:
                return
            try:
                if self.decoding():
                    shutil.copyfile(self.capture['path'], path)
                else:  # written anew: with the current labels
                    full = lambda name: ('%s %s' % (name, self.label_of(name))).strip()
                    write_sigrok(path, self.capture['rate'],
                                 [(full(name), levels) for name, levels, _ in self.capture['digital']],
                                 [(full(name), values) for name, values in self.capture['analog']])
            except OSError as error:
                messagebox.showerror('Logic Analyzer', str(error), parent=self.root)
                return
            set_text(self.status, 'Gespeichert: %s', path)

        def load(self, path):
            """Read a Sigrok file and decode it (in the background)."""
            self.job += 1
            job = self.job
            self.busy = True
            self._update_buttons()
            set_text(self.status, 'Wird geladen: %s', path)

            def work():
                try:
                    data = read_sigrok(path)
                    labels, digital, analog = {}, [], []
                    for full, levels in data['digital']:
                        name, labels[name] = split_name(full)
                        digital.append((name, levels, find_edges(levels)))
                    for full, values in data['analog']:
                        name, labels[name] = split_name(full)
                        analog.append((name, values))
                    capture = {'path': path, 'rate': data['rate'], 'digital': digital, 'analog': analog,
                               'labels': labels,
                               'samples': max(len(entry[1]) for entry in digital + analog),
                               'name': os.path.basename(path)}
                    self.queue.put(('capture', job, capture))
                except Exception as error:
                    self.queue.put(('error', job, str(error)))
            threading.Thread(target=work, daemon=True).start()
            self._poll()

        def _start_decode(self):
            self.decode_job = None
            if self.capture is None:
                return
            if self.capture['levels'] is None:  # analog channels only
                self.busy = False
                self._update_buttons()
                self._capture_status()
                return
            try:
                nominal, data = self.rates()
            except ValueError:
                set_text(self.status, 'Bitrate und Datenrate müssen positive ganze Zahlen sein.')
                return
            self.job += 1
            job = self.job
            capture = self.capture
            self.busy = True
            self._update_buttons()
            set_text(self.status, 'Dekodiere …')

            def work():
                try:
                    result = decode(capture['levels'], capture['rate'], nominal, data,
                                    cancel=lambda: job != self.job,
                                    progress=lambda share: self.queue.put(('progress', job, share)))
                    self.queue.put(('decoded', job, result))
                except Exception as error:
                    self.queue.put(('decode_error', job, str(error)))
            threading.Thread(target=work, daemon=True).start()
            self._poll()

        def _poll(self):
            if self.poll_job is not None:
                return
            self.poll_job = self.root.after(60, self._poll_now)

        def _poll_now(self):
            self.poll_job = None
            pending = True
            while True:
                try:
                    kind, job, payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                if kind == 'record_started':
                    self._recording_started(payload)
                    continue
                if kind == 'record_error':
                    self.busy = False
                    self._update_buttons()
                    set_text(self.status, 'Aufzeichnung fehlgeschlagen: %s', payload)
                    messagebox.showerror('Logic Analyzer', payload, parent=self.root)
                    continue
                if kind == 'record_stopped':
                    self.busy = False
                    self.recording = None
                    self._update_buttons()
                    if payload.get('error'):
                        shutil.rmtree(payload['folder'], ignore_errors=True)
                        set_text(self.status, 'Aufzeichnung fehlgeschlagen: %s', payload['error'])
                        messagebox.showerror('Logic Analyzer', payload['error'], parent=self.root)
                    else:
                        self._forget_temp()
                        self.temp_folder = payload['folder']
                        self.load(payload['path'])
                        if payload.get('notes'):
                            messagebox.showwarning('Logic Analyzer', '\n\n'.join(payload['notes']),
                                                   parent=self.root)
                    continue
                if job != self.job:
                    continue
                if kind == 'progress':
                    set_text(self.status, 'Dekodiere … %s %%', lambda share=payload: '%d' % round(share * 100))
                elif kind == 'capture':
                    self.capture = payload
                    self._fresh = True
                    self.decoded = None
                    self.top = 0
                    if any(payload['labels'].values()):  # the file brings its labels
                        for name, label in payload['labels'].items():
                            if name in self.labels:
                                self.labels[name].set(label)
                    self._update_channels()
                    self._use_can_channel()
                    self.fit()
                    self.busy = False
                    self._start_decode()
                elif kind == 'decoded':
                    self.decoded = payload
                    self.busy = False
                    self._update_buttons()
                    self._capture_status()
                    self._fit_first_frame()
                    self.render()
                elif kind == 'decode_error':
                    self.decoded = None
                    self.busy = False
                    self._update_buttons()
                    set_text(self.status, '%s', payload)
                    self.render()
                elif kind == 'error':
                    self.busy = False
                    self._update_buttons()
                    set_text(self.status, '%s', payload)
                    messagebox.showerror('Logic Analyzer', payload, parent=self.root)
            if self.busy or self.recording is not None:
                self.poll_job = self.root.after(60, self._poll_now)

        def _capture_status(self):
            capture = self.capture
            seconds = capture['samples'] / float(capture['rate'])
            samples, rate = '{:,}'.format(capture['samples']), '%g' % (capture['rate'] / 1e6)
            if self.decoded is None:
                set_text(self.status, '%s: %s Abtastwerte, %s MHz, %s', capture['name'], samples, rate,
                         time_text(seconds))
            else:
                set_text(self.status, '%s: %s Frames, %s Abtastwerte, %s MHz, %s', capture['name'],
                         '%d' % len(self.decoded['frames']), samples, rate, time_text(seconds))

        def _fit_first_frame(self):
            """After loading: show the first frame (otherwise everything stays)."""
            if getattr(self, '_fresh', True) and self.decoded and self.decoded['frames']:
                start, end, _ = self.decoded['frames'][0]
                width = max(1, self._plot_width())
                span = max(end - start, 10) * 1.1
                self.spp = max(span / width, 1 / 32.0)
                self.view_start = max(0.0, start - span * 0.04)
            self._fresh = False

        # ------------------------------------------------------------ recording

        def start_recording(self):
            if self.recording is not None or self.busy:
                return
            try:
                nominal, data = self.rates() if self.decoding() else (None, None)
                sampling = self.sampling_rate()
            except ValueError:
                messagebox.showerror(tr('Eingabefehler'), tr('Bitrate, Datenrate und Abtastrate müssen positive '
                                                             'ganze Zahlen sein.'), parent=self.root)
                return
            if sampling is not None and sampling <= 0:
                return
            if not self.decoding():
                self._start_pico(sampling)
                return
            bus = self.get_bus() if self.get_bus is not None else None
            connection = dict(self.connection)
            connection.update({'interface': 'usbtingo', 'channel': 0, 'bitrate': nominal, 'fd': bool(data),
                               'data_bitrate': data or nominal})
            if not isinstance(connection.get('serial'), str):
                connection['serial'] = None  # a Vector serial number does not identify the USBtingo
            self.busy = True
            self._update_buttons()
            set_text(self.status, 'Aufzeichnung wird gestartet …')

            def work():
                owned, started = False, None
                try:
                    if bus is None:
                        from CAN_TraceRecorder import open_bus
                        started = open_bus(connection)
                        owned = True
                    else:
                        started = bus
                    if not hasattr(started, 'recording_start'):
                        raise RuntimeError(tr('Die Logikaufzeichnung braucht einen USBtingo '
                                              '(python-can-usbtingo).'))
                    folder = tempfile.mkdtemp(prefix='canlogic')
                    path = os.path.join(folder, 'usbtingo_capture.sr')
                    try:
                        with working_directory(folder):
                            started.recording_start(path, sampling)
                    except Exception:
                        shutil.rmtree(folder, ignore_errors=True)
                        raise
                    self.queue.put(('record_started', 0, {'bus': started, 'owned': owned, 'folder': folder,
                                                          'path': path, 'started': time.time()}))
                except Exception as error:
                    if owned and started is not None:
                        try:
                            started.shutdown()
                        except Exception:
                            pass
                    self.queue.put(('record_error', 0, str(error) or error.__class__.__name__))
            threading.Thread(target=work, daemon=True).start()
            self._poll()

        def _start_pico(self, sampling):
            channels = self.selected_channels()
            if not channels:
                messagebox.showerror('Logic Analyzer', tr('Es ist kein Kanal gewählt.'), parent=self.root)
                return
            port = self.port.get().strip()
            port = None if port.lower() in ('', 'auto') else port
            rate = sampling or PICO_AUTO_RATE
            notes = []
            count = len([name for name in channels if name in ANALOG])
            if count and rate > analog_limit(count):
                if sampling:
                    notes.append(tr('Mit %d analogen Kanälen sind höchstens %d kHz möglich.')
                                 % (count, analog_limit(count) // 1000))
                rate = analog_limit(count)
            self.capture, self.decoded = None, None
            self._update_channels()
            self.render()
            self.busy = True
            self._update_buttons()
            set_text(self.status, 'Aufzeichnung wird gestartet …')

            def work():
                folder = None
                try:
                    folder = tempfile.mkdtemp(prefix='canlogic')
                    self.queue.put(('record_started', 0, {
                        'pico': PicoRecording(port, channels, rate), 'notes': notes, 'folder': folder,
                        'path': os.path.join(folder, 'sigrok_pico_capture.sr'), 'started': time.time()}))
                except Exception as error:
                    if folder:
                        shutil.rmtree(folder, ignore_errors=True)
                    self.queue.put(('record_error', 0, str(error) or error.__class__.__name__))
            threading.Thread(target=work, daemon=True).start()
            self._poll()

        def _recording_started(self, info):
            self.busy = False
            self.recording = info
            info['draining'] = info.get('owned', False)
            if info.get('owned'):  # nobody reads the CAN frames of an own connection: drop them
                def drain():
                    while info['draining']:
                        try:
                            info['bus'].recv(timeout=0.1)
                        except Exception:
                            time.sleep(0.1)
                threading.Thread(target=drain, daemon=True).start()
            self._update_buttons()
            self._tick()

        def _tick(self):
            self.timer_job = None
            if self.recording is None:
                return
            pico = self.recording.get('pico')
            if pico is not None and pico.finished():  # overflow or device removed
                self.stop_recording()
                return
            seconds = int(time.time() - self.recording['started'])
            set_text(self.status, 'Aufzeichnung läuft … %s s', '%d' % seconds)
            self.timer_job = self.root.after(500, self._tick)

        def stop_recording(self):
            info = self.recording
            if info is None or self.busy:
                return
            self.busy = True
            self._update_buttons()
            set_text(self.status, 'Aufzeichnung wird beendet …')
            threading.Thread(target=self._stop_work, args=(info,), daemon=True).start()
            self._poll()

        def _stop_work(self, info):
            result = {'folder': info['folder'], 'path': info['path']}
            try:
                if 'pico' in info:
                    result['notes'] = info['notes'] + info['pico'].stop(info['path'])
                else:
                    with working_directory(info['folder']):
                        info['bus'].recording_stop()
            except Exception as error:
                result['error'] = str(error) or error.__class__.__name__
            finally:
                info['draining'] = False
                if info.get('owned'):
                    try:
                        info['bus'].shutdown()
                    except Exception:
                        pass
            self.queue.put(('record_stopped', 0, result))

        def _forget_temp(self):
            if self.temp_folder:
                shutil.rmtree(self.temp_folder, ignore_errors=True)
                self.temp_folder = None

        # ------------------------------------------------------------ view

        def _plot_width(self):
            return max(1, self.canvas.winfo_width() - self.label_width - 8)

        def _total(self):
            return self.capture['samples'] if self.capture is not None else 0

        def _limit(self):
            """Keep the view inside the capture."""
            total, width = self._total(), self._plot_width()
            if total <= 0:
                self.view_start, self.spp = 0.0, 1.0
                return
            self.spp = min(max(self.spp, 1 / 32.0), max(total / float(width), 1 / 32.0))
            span = self.spp * width
            self.view_start = min(max(self.view_start, 0.0), max(0.0, total - span))

        def fit(self):
            if self.capture is None:
                return
            self.view_start = 0.0
            self.spp = self._total() / float(self._plot_width())
            self.render()

        def zoom(self, factor, x=None):
            """factor < 1 zooms in. x: canvas position that stays fixed (default: the middle)."""
            if self.capture is None:
                return
            width = self._plot_width()
            x = width / 2.0 if x is None else min(max(x - self.label_width, 0), width)
            anchor = self.view_start + x * self.spp
            self.spp *= factor
            self._limit()
            self.view_start = anchor - x * self.spp
            self.render()

        def _on_vscrollbar(self, action, *args):
            height, content = self._heights()
            if action == 'moveto':
                self.top = float(args[0]) * content
            elif action == 'scroll':
                self.top += int(args[0]) * (height * 0.9 if args[1] == 'pages' else CHANNEL_HEIGHT)
            self.render()

        def _on_wheel(self, event):
            delta = event.delta
            if delta and event.x < self.label_width:  # over the channel names: scroll the channels
                self.top -= CHANNEL_HEIGHT if delta > 0 else -CHANNEL_HEIGHT
                self.render()
            elif delta:
                self.zoom(0.8 if delta > 0 else 1.25, event.x)

        def _on_pan_wheel(self, event):
            self.view_start -= (1 if event.delta > 0 else -1) * self._plot_width() * self.spp * 0.1
            self.render()

        def _on_press(self, event):
            self.drag = (event.x, self.view_start)

        def _on_drag(self, event):
            if self.drag is None:
                return
            self.view_start = self.drag[1] - (event.x - self.drag[0]) * self.spp
            self.render()

        def _on_scrollbar(self, action, *args):
            if self.capture is None:
                return
            width = self._plot_width()
            span = self.spp * width
            if action == 'moveto':
                self.view_start = float(args[0]) * self._total()
            elif action == 'scroll':
                self.view_start += int(args[0]) * (span * 0.9 if args[1] == 'pages' else span * 0.1)
            self.render()

        def _on_double(self, event):
            """Double click on a field or bit: zoom to it."""
            found = self._item_at(event.x, event.y)
            if found is None:
                return
            start, end = found[0], found[1]
            width = self._plot_width()
            span = max(end - start, 4) * 1.4
            self.spp = max(span / width, 1 / 32.0)
            self.view_start = start - (span - (end - start)) / 2.0
            self._limit()
            self.render()

        # ------------------------------------------------------------ hover

        def _rows(self):
            """Rows below the ruler: [(kind, name, data, top, bottom)] in canvas coordinates (scrolled).
            kind: 'analog', 'digital', 'bits', 'fields'."""
            if self.capture is None and self.decoding():
                items = [('digital', 'CAN', None, WAVE_HEIGHT), ('bits', 'CAN: Bits', None, ROW_HEIGHT),
                         ('fields', 'CAN: Fields', None, ROW_HEIGHT)]
            elif self.capture is None:
                items = [('analog' if name in ANALOG else 'digital', name, None,
                          ANALOG_HEIGHT if name in ANALOG else CHANNEL_HEIGHT) for name in self.selected_channels()]
            else:
                items = [('analog', name, values, ANALOG_HEIGHT) for name, values in self.capture['analog']]
                single = len(self.capture['digital']) == 1 and not items
                for name, levels, edges in self.capture['digital']:
                    items.append(('digital', name, (levels, edges), WAVE_HEIGHT if single else CHANNEL_HEIGHT))
                    if name == self.capture['can']:
                        items.append(('bits', 'CAN: Bits', None, ROW_HEIGHT))
                        items.append(('fields', 'CAN: Fields', None, ROW_HEIGHT))
            rows, top = [], RULER_HEIGHT - int(self.top)
            for kind, name, data, height in items:
                rows.append((kind, name, data, top, top + height))
                top += height
            return rows

        def _heights(self):
            """(visible height, height of all rows) below the ruler."""
            rows = self._rows()
            return max(1, self.canvas.winfo_height() - RULER_HEIGHT), (rows[-1][4] - rows[0][3]) if rows else 0

        def _item_at(self, x, y):
            """(start, end, text) of the bit, field or analog value under the pointer, or None."""
            if self.capture is None or x < self.label_width or y < RULER_HEIGHT:
                return None
            sample = self.view_start + (x - self.label_width) * self.spp
            decoded = self.decoded
            row = next((row for row in self._rows() if row[3] <= y < row[4]), None)
            if row is None:
                return None
            if row[0] == 'analog':
                index = int(sample)
                if 0 <= index < len(row[2]):
                    name = ('%s %s' % (row[1], self.label_of(row[1]))).strip()
                    return index, index + 1, '%s: %s V' % (name, number_text(row[2][index], 2))
            elif row[0] == 'bits' and decoded is not None:
                starts = decoded['starts']
                index = bisect.bisect_right(starts, sample) - 1
                if 0 <= index < len(starts) and sample < decoded['ends'][index]:
                    flag = decoded['flags'][index]
                    kind = tr('Stuff-Bit') if flag & 2 else tr('Bit')
                    return (starts[index], decoded['ends'][index],
                            '%s: %d%s' % (kind, flag & 1, ' (%s)' % tr('Datenphase') if flag & 4 else ''))
            elif row[0] == 'fields' and decoded is not None:
                fields = decoded['fields']
                index = bisect.bisect_right(decoded['field_starts'], sample) - 1
                if 0 <= index < len(fields) and sample < fields[index][1]:
                    return fields[index][0], fields[index][1], fields[index][3]
            return None

        def _on_motion(self, event):
            self._show_hover(self._item_at(event.x, event.y), event.x)

        def _show_hover(self, item, x=0):
            if item == self.hover:
                return
            self.hover = item
            if item is None:
                if self.capture is not None and not self.busy and self.recording is None:
                    self._capture_status()
                return
            rate = self.capture['rate']
            self.status.set('%s · %s' % (item[2], time_text(item[0] / float(rate))))

        # ------------------------------------------------------------ drawing

        def theme_changed(self):
            self.render()

        def language_changed(self):
            self.render()

        def _update_label_width(self):
            names = [row[1] for row in self._rows() if row[0] in ('analog', 'digital')]
            longest = max([self.font.measure(self.label_of(name)) for name in names] + [0])
            # 70 pixels name tag, behind the label the voltage scale of analog channels
            self.label_width = min(LABEL_WIDTH_MAX, max(LABEL_WIDTH, 70 + longest + 52))

        def _fitting(self, text, width):
            """text, shortened with '…' to at most width pixels."""
            if self.font.measure(text) <= width:
                return text
            while text and self.font.measure(text + '…') > width:
                text = text[:-1]
            return text + '…'

        def render(self):
            canvas = self.canvas
            canvas.delete('all')
            width = canvas.winfo_width()
            if width < 50:
                return
            self._update_label_width()
            self._limit()
            height, content = self._heights()
            content = max(1, content)  # no rows without a selected channel
            self.top = max(0, min(self.top, content - height))
            self.vscroll.set(self.top / float(content), min(1.0, (self.top + height) / float(content)))
            rows = self._rows()
            bottom = max([canvas.winfo_height()] + [row[4] for row in rows[-1:]])
            font = self.theme.font_axis
            if self.capture is not None:
                self._draw_grid(width, bottom)
            number = 0
            for kind, name, data, top, end in rows:
                if end < RULER_HEIGHT or top > canvas.winfo_height():
                    number += kind in ('analog', 'digital')
                    continue
                middle = (top + end) / 2.0
                if kind in ('analog', 'digital'):
                    if len(rows) == 1 or (self.decoding() and (self.capture is None or (
                            len(self.capture['digital']) == 1 and not self.capture['analog']))):
                        fill, ink = C.INK, C.SURFACE
                    else:
                        fill, ink = C.SERIES_COLORS[number % len(C.SERIES_COLORS)], DARK_TEXT
                    number += 1
                    canvas.create_rectangle(14, middle - 10, 62, middle + 10, fill=fill, outline='')
                    canvas.create_text(38, middle, text=name, fill=ink, font=font)
                    label = self.label_of(name)
                    if label:
                        canvas.create_text(70, middle, text=self._fitting(label, self.label_width - 122), fill=C.INK,
                                           font=font, anchor=tk.W)
                else:
                    canvas.create_text(14, middle, text='▸ ' + name, fill=C.INK, font=font, anchor=tk.W)
                if self.capture is None:
                    continue
                if kind == 'digital':
                    self._draw_wave(width, (top, end), *data)
                elif kind == 'analog':
                    self._draw_analog(width, (top, end), data)
                elif kind == 'bits' and self.decoded is not None:
                    self._draw_bits(width, (top, end))
                elif kind == 'fields' and self.decoded is not None:
                    self._draw_fields(width, (top, end))
            canvas.create_line(self.label_width - 4, 0, self.label_width - 4, bottom, fill=C.LINE)
            if self.capture is None:
                canvas.create_text(self.label_width + 20, RULER_HEIGHT + 30, anchor=tk.W, fill=C.MUTED,
                                   text=tr('Keine Aufzeichnung. Mit dem roten Punkt aufzeichnen oder eine Sigrok-Datei '
                                           '(.sr) öffnen.'), font=font)
            else:
                self._draw_ruler(width)  # last: it covers rows scrolled below it
            self._update_scrollbar()

        def _update_scrollbar(self):
            total = self._total()
            if total <= 0:
                self.scroll.set(0, 1)
                return
            span = self._plot_width() * self.spp
            self.scroll.set(self.view_start / total, min(1.0, (self.view_start + span) / total))

        def _x(self, sample):
            return self.label_width + (sample - self.view_start) / self.spp

        def _ticks(self):
            """(step, tick times in seconds) of the time axis."""
            rate = float(self.capture['rate'])
            first, last = self.view_start / rate, (self.view_start + self._plot_width() * self.spp) / rate
            step = nice_step(110 * self.spp / rate)
            tick = math.ceil(first / step - 1e-9)
            ticks = []
            while tick * step <= last:
                ticks.append(tick * step)
                tick += 1
            return step, ticks

        def _draw_grid(self, width, bottom):
            for seconds in self._ticks()[1]:
                x = self._x(seconds * self.capture['rate'])
                self.canvas.create_line(x, RULER_HEIGHT, x, bottom, fill=C.GRID)

        def _draw_ruler(self, width):
            canvas, rate = self.canvas, float(self.capture['rate'])
            font = self.theme.font_axis
            step, ticks = self._ticks()
            factor, unit = time_unit(step)
            decimals = max(0, int(-math.floor(math.log10(step / factor) + 1e-9)))
            canvas.create_rectangle(0, 0, width, RULER_HEIGHT, fill=C.SURFACE, outline='')
            canvas.create_line(self.label_width - 4, 0, self.label_width - 4, RULER_HEIGHT, fill=C.LINE)
            for seconds in ticks:
                x = self._x(seconds * rate)
                canvas.create_line(x, RULER_HEIGHT - 8, x, RULER_HEIGHT, fill=C.MUTED)
                canvas.create_text(x, RULER_HEIGHT - 12, text='+' + number_text(seconds / factor, decimals) + ' ' + unit,
                                   fill=C.MUTED, font=font, anchor=tk.S)
            canvas.create_line(self.label_width, RULER_HEIGHT, width, RULER_HEIGHT, fill=C.LINE)

        def _draw_wave(self, width, row, levels, edges):
            canvas = self.canvas
            top, bottom = row
            margin = min(10, (bottom - top) // 5)
            high, low = top + margin, bottom - margin
            color = C.INK
            a = self.view_start
            b = a + self._plot_width() * self.spp
            lo, hi = bisect.bisect_left(edges, int(a) + 1), bisect.bisect_left(edges, int(b) + 1)
            canvas.create_rectangle(self.label_width, top + 2, width - 4, bottom - 2, fill=C.APP_BG, outline='')
            if hi - lo > 2 * self._plot_width():  # too dense: only mark where the level changes
                for x in range(self.label_width, width - 4):
                    sample = a + (x - self.label_width) * self.spp
                    index = bisect.bisect_left(edges, int(sample))
                    if index < len(edges) and edges[index] < sample + self.spp:
                        canvas.create_line(x, high, x, low, fill=color)
                return
            level = levels[min(int(a), len(levels) - 1)]
            x = float(self.label_width)
            points = [x, high if level else low]
            for index in range(lo, hi):
                edge_x = self._x(edges[index])
                points += [edge_x, high if level else low]
                level = 1 - level
                points += [edge_x, high if level else low]
            points += [width - 4, high if level else low]
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=1.5)

        def _draw_analog(self, width, row, values):
            """Voltage over time, 0 … 3.3 V (more if the file contains it)."""
            canvas = self.canvas
            top, bottom = row
            canvas.create_rectangle(self.label_width, top + 2, width - 4, bottom - 2, fill=C.APP_BG, outline='')
            if 'range' not in self.capture:
                self.capture['range'] = {}
            if id(values) not in self.capture['range']:
                self.capture['range'][id(values)] = (min(0.0, min(values)), max(3.3, max(values)))
            low, high = self.capture['range'][id(values)]
            scale = (bottom - top - 12) / (high - low)
            y = lambda volts: bottom - 6 - (volts - low) * scale
            canvas.create_text(self.label_width - 10, bottom - 6, text='%g V' % round(low, 1), fill=C.MUTED,
                               font=self.theme.font_axis, anchor=tk.SE)
            canvas.create_text(self.label_width - 10, top + 6, text='%g V' % round(high, 1), fill=C.MUTED,
                               font=self.theme.font_axis, anchor=tk.NE)
            points = []
            if self.spp > 2:  # several samples per pixel: smallest and largest value of each column
                for x in range(self.label_width, width - 4):
                    start = int(self.view_start + (x - self.label_width) * self.spp)
                    part = values[start:int(start + self.spp) + 1]
                    if part:
                        points += [x, y(min(part)), x, y(max(part))]
            else:
                end = min(len(values), int(self.view_start + self._plot_width() * self.spp) + 2)
                for index in range(max(0, int(self.view_start)), end):
                    points += [max(self.label_width, min(width - 4, self._x(index))), y(values[index])]
            if len(points) >= 4:
                canvas.create_line(*points, fill=C.INK, width=1)

        def _hexagon(self, x0, x1, top, bottom, fill, text, text_color, outline=''):
            canvas = self.canvas
            point = min(5.0, (x1 - x0) / 2.0)
            middle = (top + bottom) / 2.0
            canvas.create_polygon(x0, middle, x0 + point, top, x1 - point, top, x1, middle, x1 - point, bottom,
                                  x0 + point, bottom, fill=fill, outline=outline or fill)
            if text and self.font.measure(text) + 2 * point + 4 <= x1 - x0:
                canvas.create_text((x0 + x1) / 2.0, middle, text=text, fill=text_color, font=self.theme.font_axis)

        def _visible(self, starts, ends_at):
            """Index range of the items overlapping the view."""
            a = self.view_start
            b = a + self._plot_width() * self.spp
            first = max(0, bisect.bisect_right(starts, a) - 1)
            last = bisect.bisect_right(starts, b)
            return first, last

        def _draw_bits(self, width, row):
            decoded = self.decoded
            top, bottom = row[0] + 4, row[1] - 4
            starts, ends, flags = decoded['starts'], decoded['ends'], decoded['flags']
            first, last = self._visible(starts, ends)
            if last > first and (ends[first] - starts[first]) / self.spp < 3:  # too small for single bits
                self.canvas.create_line(self.label_width, (top + bottom) / 2, width - 4, (top + bottom) / 2, fill=C.LINE)
                return
            normal, stuff = C.SERIES_COLORS[6], C.SERIES_COLORS[0]
            for index in range(first, last):
                x0, x1 = self._x(starts[index]), self._x(ends[index])
                if x1 - x0 < 1.0:
                    continue
                flag = flags[index]
                if flag & 2:
                    self._hexagon(x0 + 0.5, x1 - 0.5, top, bottom, stuff, str(flag & 1), C.ON_ACCENT)
                else:
                    self._hexagon(x0 + 0.5, x1 - 0.5, top, bottom, normal, str(flag & 1), DARK_TEXT)

        def _draw_fields(self, width, row):
            decoded = self.decoded
            top, bottom = row[0] + 4, row[1] - 4
            fields = decoded['fields']
            first, last = self._visible(decoded['field_starts'], None)
            for index in range(first, min(last, len(fields))):
                start, end, label, detail, kind = fields[index]
                x0, x1 = self._x(start), self._x(end)
                if x1 < self.label_width or x1 - x0 < 1.0:
                    continue
                x0 = max(x0, self.label_width)
                if kind == 'error':
                    fill, ink = C.DANGER, C.ON_ACCENT
                else:
                    fill, ink = C.SERIES_COLORS[KIND_COLORS.get(kind, 7)], DARK_TEXT
                self._hexagon(x0 + 0.5, x1 - 0.5, top, bottom, fill, label, ink)

        # ------------------------------------------------------------ window

        def window_settings(self):
            return {'geometry': window_geometry(self.root), 'bitrate': self.bitrate.get(), 'fd': self.fd.get(),
                    'data_bitrate': self.data_bitrate.get(), 'sample_rate': self.sample_rate.get(),
                    'device': self.device.get(), 'port': self.port.get(), 'input': self.input.get(),
                    'channels': self.selected_channels(),
                    'labels': dict((name, variable.get().strip()) for name, variable in self.labels.items()
                                   if variable.get().strip())}

        def close(self):
            self.job += 1  # stops a running decoder
            if self.recording is not None and not self.busy:
                threading.Thread(target=self._stop_work, args=(self.recording,), daemon=True).start()
            for job in (self.poll_job, self.decode_job, self.timer_job, self.label_job):
                if job is not None:
                    try:
                        self.root.after_cancel(job)
                    except tk.TclError:
                        pass
            if self.recording is None:
                self._forget_temp()
            self.root.destroy()
