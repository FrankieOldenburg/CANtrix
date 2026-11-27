#!/usr/bin/env python3
"""CAN Trace Viewer.

Tkinter-Anwendung zur Analyse von CAN-Traces (Vector ASC und BLF, ASAM MDF4, PCAN-View TRC, BUSMASTER,
candump, SLCAN, CSV, PCAP/PCAPNG; die Leser außer ASC stehen in CAN_TraceFormats.py) zusammen mit einer CAN-Datenbasis (DBC, ARXML oder HTML-Message-Catalogue).
Datenbasis-Parser und gemeinsame Hilfsfunktionen kommen aus CAN_Common.py im selben Ordner.

Start: python3 CAN_TraceViewer.py [TRACE] [-d DATENBASIS]
Kompatibel mit Python 3.6.
"""
import argparse
import bisect
import collections
import copy
import datetime
import heapq
import io
import math
import os
import queue
import re
import struct
import sys
import threading
import time as clock
import zlib

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None

from CAN_Common import (C, MessageIndex, Theme, apply_appearance, database_id, decode_text, initial_geometry, is_big_endian,
                        js_number, js_str, normalize_id, parse_database, parse_dbc_environment, read_database_bytes,
                        signal_bit_numbers, window_geometry)
from CAN_Debugger import (DebuggerDialog, T32Poller, create_watch_database, normalize_settings,
                          variable_names, watch_environment_names)
from CAN_Language import language, load_language, set_text, tr
from CAN_TraceFormats import Rows, binary_parser, parse_asc_date, text_parser
from CAN_TraceWriters import WRITE_FORMATS, format_for_path, write_asc, write_trace

MAX_TRACE_SIZE = 200 * 1024 * 1024
GRAPH_COUNT = 5  # graph windows of the Trace Viewer (one is shown at a time)
PAGE_SIZE = 1000
LIVE_REFRESH_MS = 500  # how often the recorded frames are shown
MAX_LIVE_FRAMES = 100000  # older frames of a recording are dropped
TRACE_TYPES = ('.asc', '.blf', '.mf4', '.mdf', '.trc', '.log', '.txt', '.csv', '.pcap', '.pcapng', '.vsb', '.db')

CHECKED, UNCHECKED = '☑', '☐'
DASH = '—'

TRACE_COLUMNS = (
    ('reltime', 'Zeit [s]', 120, 'e'),  # only shown with the absolute time, which then is in the tree column
    ('channel', 'Kanal', 72, 'center'),
    ('id', 'ID', 90, 'w'),
    ('name', 'Nachricht', 230, 'w'),
    ('dlc', 'DLC', 45, 'center'),
    ('data', 'Daten', 360, 'w'),
    ('count', 'Anzahl', 65, 'e'),
    ('graph', 'Grafik', 55, 'center'),
    ('description', 'Beschreibung', 320, 'w'),  # only shown with the button "Beschreibung"
)
ABSOLUTE_WIDTH = 200
DATA_WIDTH_WITH_DESCRIPTION = 250  # width of the data column while the description is shown
RELATIVE_WIDTH = 140
SECTION_WINDOWS = {'trace': ('CAN Trace Viewer', '900x800'), 'graph': ('CAN Graph Viewer', '1000x700')}


# ---------------------------------------------------------------- helpers

def description_text(text):
    """Description from the database as one line for the table."""
    return ' '.join(str(text or '').split())


def absolute_time_text(start, time):
    """Absolute time 'dd.mm.yyyy hh:mm:ss.xxx' of a trace time (seconds, also negative) after start."""
    if start is None:
        return DASH
    try:
        when = start + datetime.timedelta(microseconds=round(time * 1e6))
    except OverflowError:
        return DASH
    return '%02d.%02d.%04d %02d:%02d:%02d.%03d' % (when.day, when.month, when.year, when.hour, when.minute,
                                                    when.second, when.microsecond // 1000)


def fmt(value, digits=4):
    """Number with `digits` significant digits, like JavaScript's toPrecision."""
    if value is None or not math.isfinite(value):
        return DASH
    return js_str(float('%.*g' % (digits, value)))


def de_int(number):
    """Integer with thousands separators: '.' in German, ',' in English."""
    text = '{:,}'.format(number)
    return text if language() == 'en' else text.replace(',', '.')


def trace_channels(frames):
    """CAN channels used in a trace, sorted (channel numbers as strings, e.g. ['1', '2'])."""
    return sorted(set(frame.channel for frame in frames), key=natural_key)


def natural_key(text):
    return [(0, int(part), '') if part.isdigit() else (1, 0, part.casefold())
            for part in re.split(r'(\d+)', text) if part]


def signal_key(message, signal):
    return '%s.%s' % (message['name'], signal['name'])


ENV_PREFIX = 'EV:'  # key of an environment variable in the graph, e.g. 'EV:IO_VN1600_1_AIN'
IO_NAME_RE = re.compile(r'^IO_|(?:^|_)[DA](?:IN|OUT)\d*$', re.I)  # digital/analog I/O, e.g. of a VN1600


def env_key(name):
    return ENV_PREFIX + name


def is_io_variable(name):
    """Digital or analog input/output (e.g. IO_VN1600_1_DIN0, IO_VN1600_1_AIN) rather than another variable."""
    return bool(IO_NAME_RE.search(name))


def number_or(text, default):
    if text is None or str(text).strip() == '':
        return default
    value = js_number(text)
    return value if math.isfinite(value) else default


def finite_pair(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            pair = [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            return None
        if all(math.isfinite(item) for item in pair):
            return pair
    return None


# ---------------------------------------------------------------- trace parsing

class Frame(object):
    __slots__ = ('time', 'channel', 'direction', 'id', 'trace_name', 'data', 'line',
                 'message', 'name', 'count', '_int', 'fd')

    def __init__(self, time, channel, direction, identifier, trace_name, data, line, fd=False):
        self.fd = fd  # CAN FD frame (also with up to 8 bytes), as far as the source says so
        self.time = time
        self.channel = channel
        self.direction = direction
        self.id = identifier
        self.trace_name = trace_name
        self.data = data
        self.line = line
        self.message = None
        self.name = ''
        self.count = 1
        self._int = None

    def as_int(self):
        if self._int is None:
            self._int = int.from_bytes(self.data, 'little')
        return self._int

    def channel_key(self):
        return '%s:%d' % (self.channel, self.id)

    is_env = False
    is_error = False
    is_debug = False


class ErrorEvent(object):
    """CAN error frame as a row of the trace table (it belongs to a channel, but has no ID)."""
    __slots__ = ('time', 'channel', 'text', 'index', 'count', 'message', 'name')
    is_env = False
    is_error = True
    is_debug = False

    def __init__(self, time, channel, text, index):
        self.time, self.channel, self.text, self.index = time, channel, text, index
        self.count = 1
        self.message = None
        self.name = 'CAN Error'


def error_events(errors):
    """ErrorEvents of the error frames [(time, channel, text)], sorted by time."""
    return [ErrorEvent(time, channel, text, index) for index, (time, channel, text)
            in enumerate(sorted(errors or (), key=lambda error: error[0]))]


class EnvEvent(object):
    """Value of an environment variable (e.g. digital/analog I/O of a VN1600) as a row of the trace table."""
    __slots__ = ('time', 'name', 'value', 'index', 'count', 'channel', 'message')
    is_env = True
    is_error = False
    is_debug = False

    def __init__(self, time, name, value, index):
        self.time, self.name, self.value, self.index = time, name, value, index
        self.count = 1
        self.channel = ''  # environment variables belong to no CAN channel
        self.message = None


class DebugEvent(object):
    """Value of a variable read from a debugger (TRACE32) as a row of the trace table."""
    __slots__ = ('time', 'name', 'value', 'index', 'count', 'channel', 'message')
    is_env = False
    is_error = False
    is_debug = True

    def __init__(self, time, name, value, index):
        self.time, self.name, self.value, self.index = time, name, value, index
        self.count = 1
        self.channel = ''
        self.message = None


def debug_events(values):
    """DebugEvents of the read values [(time, name, text)], sorted by time."""
    return [DebugEvent(time, name, value, index) for index, (time, name, value)
            in enumerate(sorted(values or (), key=lambda item: item[0]))]


def environment_events(environment):
    """EnvEvents of all environment variables ({name: [(time, text)]}), sorted by time."""
    events = [EnvEvent(time, name, value, index) for name, values in (environment or {}).items()
              for index, (time, value) in enumerate(values)]
    events.sort(key=lambda event: event.time)
    return events


def env_value_text(variable, value):
    """Text of an environment variable value with its value table text ('Active (1)') or unit ('12.44 V')."""
    variable = variable or {}
    try:
        number = float(value)
    except ValueError:
        number = None
    name = (variable.get('values') or {}).get(number) if number is not None else None
    if name:
        return '%s (%s)' % (name, value)
    unit = variable.get('unit') or ''
    return '%s %s' % (value, unit) if unit else value


ASC_FD_RE = re.compile(
    r'^\s*(-?\d+(?:\.\d+)?)\s+(?:CANFD|CAN)\s+(\d+)\s+(Rx|Tx)\s+([0-9a-f]+)x?(.*?)'
    r'\s+(\d+)\s+(\d+)\s+([0-9a-f])\s+(\d+)\s+((?:[0-9a-f]{2}\s*)+)', re.I)
ASC_CLASSIC_RE = re.compile(
    r'^\s*(-?\d+(?:\.\d+)?)\s+(?:CAN\s+)?(\d+)\s+([0-9a-f]+)x?\s+(Rx|Tx)\s+[dDrR]\s+(\d+)\s+((?:[0-9a-f]{2}\s*)+)', re.I)
ASC_COMPACT_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s+(?:CANFD|CAN)\s+(\d+)\s+(Rx|Tx)\s+([0-9a-f]+)x?\s+(.+)$', re.I)
COMPACT_DATA_RE = re.compile(r'(?:^|\s)(?:d|f)\s+(\d+)\s+((?:[0-9a-f]{2}\s*)+)', re.I)
# CAN error frame of a Vector ASC trace, e.g. "2.153270 1  ErrorFrame ECC: 10100010"
ASC_FD_LINE_RE = re.compile(r'^\s*\S+\s+CANFD\s', re.I)  # a frame on a CANFD line is a CAN FD frame
ASC_ERROR_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s+(?:CANFD\s+|CAN\s+)?(\d+)\s+(?:(?:Rx|Tx)\s+)?'
                          r'Error\s?Frame\b\s*(.*?)\s*$', re.I)
STATUS_RE = re.compile(r'\b(?:error|warning|status:|overload|bus\s+off)\b', re.I)
ENV_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s+([A-Za-z_][\w.]*)\s*:=\s*(.+)$')
# Value of an environment variable in a Vector ASC trace, e.g. "0.001000 ev IO_VN1600_1_AIN = 12.44"
EV_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s+ev\s+([A-Za-z_][\w.:]*)\s*=\s*(.*?)\s*$', re.I)
TIME_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)')


def hex_bytes(text, count):
    values = []
    for token in text.split():
        if len(values) >= count or len(token) != 2:
            break
        values.append(int(token, 16))
    return bytes(values)


def frames_from_rows(rows):
    """Frames from the rows (time, channel, direction, id, data, line, fd) of CAN_TraceFormats; the result
    keeps the start of measurement (start)."""
    frames = Rows(Frame(time, channel, direction, identifier, '', data, line, fd)
                  for time, channel, direction, identifier, data, line, fd in rows)
    frames.start = getattr(rows, 'start', None)
    frames.errors = list(getattr(rows, 'errors', ()))
    return frames


def parse_trace(source, source_name, progress=None):
    """Return (frames, status_count, recent_status_lines, environment). frames.start: start of
    measurement (naive local datetime, from the "date" line of ASC) or None. environment: values of
    environment variables, {name: [(time, value text), ...]}. progress(fraction): optional, called now
    and then with the share of lines already read (0..1). Negative times are allowed."""
    frames = Rows()
    environment = {}
    status_count = 0
    recent = collections.deque(maxlen=4)
    lines = source.splitlines()
    other = text_parser(lines)
    if other is not None:  # PCAN-View, BUSMASTER, candump, SLCAN or CSV
        frames = frames_from_rows(other[1](lines, progress))
        if not frames:
            raise ValueError(tr('Keine CAN-Frames in "%s" (%s) gefunden.') % (source_name, other[0]))
        return frames, 0, [], environment
    step = max(1, len(lines) // 200)
    for number, line in enumerate(lines, 1):
        if progress and number % step == 0:
            progress(float(number) / len(lines))
        frame = None
        match = ASC_FD_RE.match(line)
        if match:
            frame = Frame(float(match.group(1)), match.group(2), match.group(3), int(match.group(4), 16),
                          match.group(5).strip(), hex_bytes(match.group(10), int(match.group(9))), number)
        else:
            match = ASC_CLASSIC_RE.match(line)
            if match:
                frame = Frame(float(match.group(1)), match.group(2), match.group(4), int(match.group(3), 16),
                              '', hex_bytes(match.group(6), int(match.group(5))), number)
            else:
                match = ASC_COMPACT_RE.match(line)
                data = COMPACT_DATA_RE.search(match.group(5)) if match else None
                if data:
                    frame = Frame(float(match.group(1)), match.group(2), match.group(3), int(match.group(4), 16),
                                  '', hex_bytes(data.group(2), int(data.group(1))), number)
        if frame is not None and frame.data:
            frame.fd = ASC_FD_LINE_RE.match(line) is not None
            frames.append(frame)
            continue
        error = ASC_ERROR_RE.match(line)
        if error:
            frames.errors.append((float(error.group(1)), error.group(2),
                                  'Error Frame' + (' ' + error.group(3) if error.group(3) else '')))
            continue
        if frames.start is None and not frames and line.lstrip()[:5].lower() in ('date ', 'begin'):
            frames.start = parse_asc_date(line)
        ev = EV_RE.match(line)
        if ev:
            environment.setdefault(ev.group(2), []).append((float(ev.group(1)), ev.group(3)))
            continue
        if STATUS_RE.search(line):
            time = TIME_RE.match(line)
            status_count += 1
            recent.append('%s%s' % ('%s s · ' % time.group(1) if time else '', line.strip()))
        env = ENV_RE.match(line)
        if env:
            status_count += 1
            recent.append(tr('%s s · %s := %s (Umgebungsvariable)') % env.groups())
            environment.setdefault(env.group(2), []).append((float(env.group(1)), env.group(3).strip()))
    if not frames and not environment:
        raise ValueError(tr('Keine CAN-Datenzeilen in "%s" erkannt. Unterstützt werden Vector ASC und BLF, '
                            'ASAM MDF4, PCAN-View TRC, BUSMASTER, candump, SLCAN, CSV und PCAP/PCAPNG.')
                         % source_name)
    return frames, status_count, list(recent), environment


# ---------------------------------------------------------------- signal decoding

class SignalDecoder(object):
    """Extracts one signal from frame data (Intel and Motorola bit order)."""

    def __init__(self, signal):
        self.valid = False
        try:
            start = int(str(signal.get('start_bit', '')).strip())
            length = int(str(signal.get('length', '')).strip())
        except ValueError:
            return
        if start < 0 or not 1 <= length <= 64:
            return
        self.valid = True
        self.length = length
        self.signed = signal.get('signedness') == 'signed'
        self.factor = number_or(signal.get('factor'), 1.0)
        self.offset = number_or(signal.get('offset'), 0.0)
        self.values = {}
        for item in signal.get('values') or []:
            value = js_number(item.get('value', ''))
            if math.isfinite(value):
                self.values.setdefault(value, item.get('name', ''))
        if is_big_endian(signal):
            # Motorola: the first bit is the MSB of the raw value.
            plan = [(bit // 8, bit % 8, length - 1 - offset)
                    for offset, bit in enumerate(signal_bit_numbers(start, length, True))]
            self.plan = plan
            self.need = max(entry[0] for entry in plan) + 1
        else:
            self.plan = None
            self.start = start
            self.mask = (1 << length) - 1
            self.need = (start + length - 1) // 8 + 1

    def decode(self, frame):
        """Return (raw, physical, status) or None if the frame is too short."""
        if not self.valid or len(frame.data) < self.need:
            return None
        if self.plan is None:
            raw = (frame.as_int() >> self.start) & self.mask
        else:
            data = frame.data
            raw = 0
            for byte_index, bit_index, shift in self.plan:
                raw |= ((data[byte_index] >> bit_index) & 1) << shift
        if self.signed and raw >> (self.length - 1):
            raw -= 1 << self.length
        return raw, raw * self.factor + self.offset, self.values.get(float(raw), '')

    def hex_text(self, decoded):
        """' (0x7A)': the raw value in hex (two's complement for negative raw values), '' without a value."""
        if decoded is None:
            return ''
        return ' (0x%0*X)' % ((self.length + 3) // 4, decoded[0] & ((1 << self.length) - 1))

    @staticmethod
    def text(decoded):
        if decoded is None:
            return DASH
        raw, physical, status = decoded
        return '%s (%s)' % (status, fmt(physical)) if status else fmt(physical)


class Series(object):
    __slots__ = ('key', 'name', 'message', 'unit', 'xs', 'ys', 'status')

    def __init__(self, key, name, message, unit):
        self.key, self.name, self.message, self.unit = key, name, message, unit
        self.xs, self.ys, self.status = [], [], []


# ---------------------------------------------------------------- loading (worker thread)

def read_database(path, progress=None):
    """progress(fraction): optional, called now and then with the share already done (0..1)."""
    name = os.path.basename(path)
    data = read_database_bytes(path)
    database = {'name': name, 'path': path, 'messages': parse_database(data, name, progress)}
    if name.lower().endswith('.dbc'):
        database['environment'] = parse_dbc_environment(decode_text(data))  # EV_ (e.g. VN1600 I/O)
    return database


def read_trace(path, progress=None):
    """progress(fraction): optional, called now and then with the share already done (0..1)."""
    name = os.path.basename(path)
    if os.path.getsize(path) > MAX_TRACE_SIZE:
        raise ValueError(tr('Die Trace-Datei ist größer als 200 MB.'))
    with open(path, 'rb') as handle:
        data = handle.read()
    if progress:
        progress(0.1)  # reading the file is about a tenth of the work
    share = progress and (lambda value: progress(0.1 + 0.9 * value))
    binary = binary_parser(data)
    if binary is not None:  # BLF, MDF4, PCAP, PCAPNG
        try:
            rows = binary[1](data, share)
            frames = frames_from_rows(rows)
        except (struct.error, zlib.error, IndexError) as error:
            raise ValueError(tr('Die %s-Datei "%s" ist beschädigt (%s).') % (binary[0], name, error))
        environment = dict(getattr(rows, 'environment', None) or {})
        if not frames and not environment:
            raise ValueError(tr('Keine CAN-Frames in "%s" (%s) gefunden.') % (name, binary[0]))
        status_count, recent = 0, []
    else:
        frames, status_count, recent, environment = parse_trace(decode_text(data), name, share)
    return {'name': name, 'path': path, 'frames': frames, 'status_count': status_count, 'recent': recent,
            'errors': list(getattr(frames, 'errors', ())),
            'environment': environment, 'start': getattr(frames, 'start', None)}


def load_sources(request):
    """Run in a worker thread. request keys: database, trace (file paths).

    The result has 'database'/'trace' set to a loaded dict; keys are missing when the source
    stays unchanged.
    """
    result = {}
    if request.get('database'):
        result['database'] = read_database(request['database'])
    if request.get('trace'):
        result['trace'] = read_trace(request['trace'])
    return result


# ---------------------------------------------------------------- GUI

if tk is not None:

    class TraceViewerApp(object):
        def __init__(self, root, database_path=None, trace_path=None, settings=None, view=None, sources=None,
                     channel=None, section='trace'):
            """root: Tk or Toplevel of the first section ('trace' = trace table, 'graph' = graph); the other one
            is added with add_section. Both share the data, the selection and the cursors. settings: window settings (see window_settings). view: view state
            (see collect_view). sources: already loaded files as returned by load_sources (used by
            CANtrix.py); database_path/trace_path are only for the stand-alone start.
            channel: CAN channel to show (e.g. '1'), None for all channels (see set_channel)."""
            self.hosts = {}  # section ('trace', 'graph') -> its window
            self._last_root = root
            self.settings = dict(settings or {})
            self.results = queue.Queue()
            self.busy = False

            # data
            self.database_name = ''
            self.database_path = None
            self.trace_name = ''
            self.trace_path = None
            self.messages = []
            self.frames = []  # all frames of the trace
            self.trace_start = None  # start of measurement (naive local datetime) for the absolute time
            self.environment = {}  # values of environment variables, {name: [(time, text)]}
            self.errors = []  # CAN error frames, [(time, channel, text)]
            self.trace_parts = []
            self.db_environment = {}  # environment variables of the database (EV_): unit, value table
            self.trace_rows = []  # rows of the chronological trace table: frames and EnvEvents by time
            self.overview_rows = []
            self.channel = channel
            self.visible_frames = []  # frames of the selected channel (all without one), see rebuild
            self.overview_frames = []
            self.status_count = 0
            self.status_recent = []
            self.frames_by_message = {}
            self.signal_lookup = {}
            self.trace_signals = []
            self.series_cache = {}
            self.decoders = {}
            self.filter_cache = None

            # view state
            self.mode = tk.StringVar(value='chronological')
            self.absolute_time = tk.BooleanVar(value=False)  # column with the absolute time (off by default)
            self.show_environment = tk.BooleanVar(value=False)  # rows of environment variables (off by default)
            self.show_errors = tk.BooleanVar(value=False)  # rows of CAN error frames (off by default)
            self.show_debugger = tk.BooleanVar(value=False)  # rows of debugger variables (TRACE32, live only)
            self.debugger_settings = normalize_settings(None)
            self._debug_environment_names = {}
            self.on_watch_database = None
            self.config_name = lambda: ''  # name of the configuration, for the generated database
            self.on_live_environment = None
            self.debug_values = []  # [(time, name, text)] of the shown trace
            self._debug_queue = queue.Queue()
            self._debug_poller = None
            self._debug_state = None
            self._debug_bar = None
            self.debugger_status = tk.StringVar()
            self._debug_wall0 = None
            self._debug_offset = None  # time stamp of the CAN driver minus the clock: common time base
            self.show_description = tk.BooleanVar(value=False)  # column with the description of the database
            self.zoom_axis = tk.StringVar(value='x')  # axis zoomed by mouse wheel and dragging: 'x' (time) or 'y'
            self.second_cursor = tk.BooleanVar(value=False)  # 2nd cursor with differences to the 1st one
            self.cursor2_time = None
            self.cursor_info = tk.StringVar()  # times of both cursors and their difference, below the graph
            self.trace_filter = tk.StringVar()
            self.range_start = self.range_end = None  # selected time range (set by the main window); None = all
            self._all_environment, self._all_errors = {}, []  # without the range
            self.signal_filter = tk.StringVar()
            self.selected = []
            self.trace_page = 0
            self.expanded = set()
            self.hidden = set()  # message ('id:<id>') and variable ('ev:<name>') keys left out of the trace table
            self.x_range = self.y_range = self.cursor_time = None
            # Up to GRAPH_COUNT graph windows, only one is shown. The state of the shown one lives in the
            # attributes above; self.graphs holds the saved state of every window (see _store_graph).
            self.trace_index = 0  # likewise for the trace windows (see _store_trace)
            self.traces = [self._new_trace() for _ in range(GRAPH_COUNT)]
            self.trace_choice = tk.StringVar(value='1')
            self.graph_index = 0
            self.graphs = [self._new_graph() for _ in range(GRAPH_COUNT)]
            self.graph_choice = tk.StringVar(value='1')
            self.trace_tree = self.plot = self.cursor_info_label = None  # widgets of the sections
            self.picker_window = self.picker_tree = None  # signal selection window (open_picker)
            self.picker_count = tk.StringVar()
            self.filter_window = self.filter_tree = None  # message selection window (open_filter_picker)
            self.filter_count = tk.StringVar()
            self.filter_search = tk.StringVar()
            self.filter_search.trace_add('write', lambda *args: self.render_filter_options())
            self.filter_names = {}  # key -> text shown in the message selection
            self.picker_names = {}  # key -> name shown in the signal selection
            self.picker_open = set()  # groups opened by the user (all groups start closed)
            self.signal_groups = []
            self.attach_menu = None  # callback(window): shows the main menu in a window (CANtrix.py)
            self.geometries = {}  # section -> saved window geometry
            self.on_generator_add = None  # callback(identifier, data): right click on a frame (CANtrix.py)
            self.on_cursor_changed = None  # callback(time) after a click set the cursor, e.g. for the Panel Viewer
            # Source: 'trace' = the loaded trace, 'live' = the recording of the Trace Recorder (CAN_TraceRecorder,
            # built on first use because it needs python-can). Live shows the recorded frames in the same trace
            # table and graph; _loaded_trace is the trace of the files, _live_trace the recording.
            self.source = tk.StringVar(value='trace')
            self._applied_source = 'trace'
            self.recorder = None
            # Position of the playing in the Panel Viewer (set by CANtrix.py): trace table and graph show only what
            # lies at or before it; None = everything.
            self.position = None
            self.position_clips = False  # playing: show only up to the position; otherwise the position is a cursor
            self._position_job = None
            self._row_times = None  # (rows, times) of the rows shown in the trace table, for the position
            self.on_source_changed = None  # callback(source) when the window had to go back to 'trace'
            self.on_live = None  # callback(messages, reset=False) with the received messages, e.g. for the Panel Viewer
            self._connection = None  # connection settings of the recorder (set by CANtrix.py)
            self._loaded_trace = None
            self._live_trace = {'name': 'live', 'path': None, 'frames': [], 'environment': {}, 'errors': [],
                                'start': None, 'debugger': []}
            self._live_t0 = None  # time stamp of the first message (the recording starts at 0 s)
            self._live_line = 0
            self._live_dirty = False
            self._live_job = None
            self._live_range = None  # x range set by the follow mode: the graph follows while it is unchanged
            self._live_last_page = 0
            self._live_signature = None
            self.on_connection_status = None  # callback(text or None): connection status of the recording (status overview)
            self.on_running_changed = None  # callback(running): recording started or stopped (Start/Stop in the main window)
            self.on_live_recorded = None  # callback(frames): frames just added to the recording (live plugins)
            self.on_live_cleared = None  # callback(): the recording is forgotten, its time starts again

            self.row_frames = {}
            self.signal_rows = {}
            self.plot_bounds = None
            self.drag = None
            self._plot_job = self._filter_job = None

            self.footer = tk.StringVar()
            set_text(self.footer, 'Bereit')
            self.trace_count = tk.StringVar()
            set_text(self.trace_count, '%s Frames', '0')
            self.page_label = tk.StringVar(value='0 / 0')

            self.theme = Theme(root)
            self.font_axis = self.theme.font_axis
            self.font_label = self.theme.font_label
            self.font_empty = self.theme.font_text
            self.font_title = self.theme.font_title

            self.add_section(section, root, self.settings)
            if isinstance(root, tk.Tk):
                root.protocol('WM_DELETE_WINDOW', self.close_all)
                if root.tk.call('tk', 'windowingsystem') == 'aqua':
                    root.createcommand('tk::mac::Quit', self.close_all)

            self.trace_filter.trace_add('write', lambda *args: self.on_trace_filter())
            self.signal_filter.trace_add('write', lambda *args: self.render_signal_options())
            self._poll_job = root.after(60, self.poll_results)

            if sources:
                self.set_sources(sources, view)
            else:
                if view is not None:
                    self.apply_view(view)
                self.rebuild()
            if database_path or trace_path:
                request = {}
                if database_path:
                    request['database'] = os.path.abspath(database_path)
                if trace_path:
                    request['trace'] = os.path.abspath(trace_path)
                done = self.on_files_loaded if view is None else (
                    lambda result: self.on_view_files_loaded(result, view))
                self.start_job(('Lade Dateien…',), request, done)

        # ------------------------------------------------------------ window & layout

        def _pane(self, parent, title):
            return self.theme.pane(parent, title)

        @property
        def root(self):
            """A window of the viewer that is still open (for dialogs and timers)."""
            for section in ('trace', 'graph'):
                if section in self.hosts:
                    return self.hosts[section]
            return self._last_root

        def add_section(self, section, host, settings=None):
            """Show a section ('trace' or 'graph') in a window (Tk, Toplevel or DockFrame). settings: its window
            settings (see window_settings)."""
            if section in self.hosts:
                return
            settings = settings or {}
            for key in ('picker_geometry', 'filter_geometry'):
                if settings.get(key):
                    self.settings[key] = settings[key]
            if settings.get('geometry'):
                self.geometries[section] = settings['geometry']
            title, size = SECTION_WINDOWS[section]
            host.title(title)
            host.geometry(initial_geometry(host, {'geometry': settings.get('geometry')}, size))
            host.minsize(560, 400)
            self.hosts[section] = host
            self._last_root = host
            self._build_layout(section, host)
            if len(self.hosts) > 1:  # added later: show what the data already holds
                if section == 'trace':
                    self.render_trace()
                    self._apply_time_columns()
                else:
                    self._update_cursor_info()
                    self.schedule_plot()

        def remove_section(self, section):
            """The window of a section is closed. The last one ends the viewer (recording, timers)."""
            host = self.hosts.pop(section, None)
            if host is None:
                return
            self._last_root = host
            if section == 'trace':
                self.close_filter_picker()
                self._debug_bar = None
                self.trace_tree = None
            else:
                self.close_picker()
                self.plot = self.cursor_info_label = None
            if not self.hosts:
                self.shutdown()

        def close_all(self):
            """Quit (stand-alone): close all windows."""
            for section in list(self.hosts):
                host = self.hosts.get(section)
                self.remove_section(section)
                if host is not None:
                    try:
                        host.destroy()
                    except tk.TclError:
                        pass

        @property
        def connection(self):
            return self._connection

        @connection.setter
        def connection(self, value):
            self._connection = value
            if self.recorder is not None and value is not None:
                self.recorder.connection = dict(value)

        def _build_layout(self, section, host):
            self.theme.header(host, SECTION_WINDOWS[section][0])
            self.theme.footer(host, self.footer)
            body = tk.Frame(host, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            if section == 'trace':
                self._trace_body = body
                self._debug_bar = tk.Frame(host, background=C.SURFACE)
                self._debug_dot = tk.Canvas(self._debug_bar, width=8, height=8, background=C.SURFACE,
                                            highlightthickness=0)
                self._debug_dot.create_oval(1, 1, 7, 7, outline='', tags=('status',))
                self._debug_dot.pack(side=tk.LEFT, padx=(14, 0))
                tk.Label(self._debug_bar, textvariable=self.debugger_status, background=C.SURFACE,
                         foreground=C.INK, font=self.font_label, anchor=tk.W).pack(side=tk.LEFT, padx=6, pady=3)
                self._render_debug_status()
                self._build_trace_pane(body)
                self.trace_pane.pack(fill=tk.BOTH, expand=True)
            else:
                self._build_graph_pane(body)
                self.graph_pane.pack(fill=tk.BOTH, expand=True)

        # ------------------------------------------------------------ live (Trace Recorder)

        def live(self):
            return self.source.get() == 'live'

        def set_source(self, value):
            """'trace' or 'live' (the switch is in the main window)."""
            self.source.set('live' if value == 'live' else 'trace')
            if self.source.get() != self._applied_source:
                self._apply_source()

        def _set_trace(self, trace):
            """Make a trace (dict as in load_traces) the shown one."""
            trace = trace or {}
            self.trace_name = trace.get('name', '')
            self.trace_path = trace.get('path')
            if trace.get('frames', []) is not self.frames:
                self.range_start = self.range_end = None
            self.frames = trace.get('frames', [])
            # the lists of a recording keep their identity (they grow while recording), also when empty
            self._all_environment = trace['environment'] if trace.get('environment') is not None else {}
            self._all_errors = trace['errors'] if trace.get('errors') is not None else []
            self._apply_range_data()
            self.trace_parts = trace.get('traces') or []  # the single traces of a combined trace
            self.trace_start = trace.get('start')
            self.debug_values = trace['debugger'] if trace.get('debugger') is not None else []
            self.status_count = trace.get('status_count', 0)
            self.status_recent = trace.get('recent', [])

        def _apply_source(self):
            """Trace: the loaded trace; live: the recording (same trace table and graph), started and stopped from the main window."""
            self._applied_source = self.source.get()
            if self.live():
                if self.recorder is None:
                    try:
                        from CAN_TraceRecorder import Recorder
                    except ImportError as error:
                        self.source.set('trace')
                        self._applied_source = 'trace'
                        if self.on_source_changed is not None:
                            self.on_source_changed('trace')
                        messagebox.showerror('CAN Trace Viewer', tr('Der Trace Recorder benötigt python-can (%s).\n\n'
                                                                    'Installation: pip3 install --user python-can')
                                             % error, parent=self.root)
                        return
                    recorder = self.recorder = Recorder(self.root, self.footer, self._live_data)
                    recorder.on_live = self._live_messages
                    recorder.on_running = self._live_running
                    recorder.on_status = self._connection_status
                    if self._connection is not None:
                        recorder.connection = dict(self._connection)
                self._set_trace(self._live_trace)
                self._live_last_page = 0
                self._live_range = None
                if self._live_job is None:
                    self._live_job = self.root.after(LIVE_REFRESH_MS, self._live_tick)
            else:
                if self.recorder is not None:
                    self.recorder.stop()
                if self._live_job is not None:
                    self.root.after_cancel(self._live_job)
                    self._live_job = None
                self._set_trace(self._loaded_trace)
            self._reset_graphs()
            self._reset_traces()
            self.rebuild()
            self.fit_x()
            self._update_clear_button()
            if self.live():
                self._live_range = list(self.x_range) if self.x_range else None
                if not self.recorder.running():
                    set_text(self.footer, 'Nicht verbunden')

        def clear_live(self):
            """Button "Löschen": forget the recording shown (also while recording; the time starts again at 0)."""
            if not self.live():
                return
            self._clear_recording()
            self.fit_x()
            self._live_range = list(self.x_range) if self.x_range else None
            if self.recorder is not None and not self.recorder.running():
                set_text(self.footer, 'Nicht verbunden')

        def _update_clear_button(self):
            button = getattr(self, 'clear_button', None)
            if button is not None:
                try:
                    button.state(['!disabled'] if self.live() else ['disabled'])
                except tk.TclError:
                    pass

        def start_live(self):
            """Start (main window): forget the last recording and record again."""
            self._clear_recording()
            self.recorder.start()
            self._update_debugger()

        def _clear_recording(self):
            if self.on_live_cleared is not None:
                self.on_live_cleared()
            data = self._live_trace
            del data['frames'][:]
            data['environment'].clear()
            del data['errors'][:]
            del data['debugger'][:]
            data['start'] = self.trace_start = datetime.datetime.now()
            self._live_t0 = None
            self._live_line = 0
            self._live_range = None
            self._live_last_page = 0
            self._reset_graphs()
            self._reset_traces()
            self.rebuild()
            self._debug_wall0 = clock.time()
            self._debug_offset = None

        def stop_live(self):
            self.recorder.stop()
            self._live_refresh()  # the last messages

        def _connection_status(self, text):
            if self.on_connection_status is not None:
                self.on_connection_status(text)

        def _live_running(self, running):
            if not running:
                self._stop_debugger()
            if self.on_running_changed is not None:
                self.on_running_changed(running)

        def _live_channel(self, message):
            if (self._connection or {}).get('interface') == 'usbtingo':
                return '1'  # one channel per device
            channel = getattr(message, 'channel', None)
            if isinstance(channel, int) and not isinstance(channel, bool):
                return str(channel + 1)  # python-can counts from 0, traces from 1
            connection = self._connection or {}
            return str(connection['channel'] + 1) if isinstance(connection.get('channel'), int) else ''

        def recording(self):
            """(frames, dropped) of the running recording, None while nothing is recorded."""
            if not self.live() or self.recorder is None or not self.recorder.running():
                return None
            frames = list(self._live_trace['frames'])
            return frames, self._live_line - len(frames)

        def _live_data(self, messages):
            """Messages of the recorder (GUI thread): append them to the recording; the display follows in
            _live_tick."""
            data = self._live_trace
            frames = data['frames']
            added = []
            for message in messages:
                stamp = message.timestamp or 0.0
                if self._live_t0 is None:
                    self._live_t0 = stamp
                moment = stamp - self._live_t0
                channel = self._live_channel(message)
                if message.is_error_frame:
                    data['errors'].append((moment, channel, 'Error Frame'))
                    continue
                self._live_line += 1
                added.append(Frame(moment, channel, 'Rx' if getattr(message, 'is_rx', True) else 'Tx',
                                   message.arbitration_id, '', bytes(message.data), self._live_line,
                                   bool(getattr(message, 'is_fd', False))))
            frames.extend(added)
            if added and self.on_live_recorded is not None:
                self.on_live_recorded(added)
            if len(frames) > MAX_LIVE_FRAMES:
                del frames[:len(frames) - MAX_LIVE_FRAMES]
            if messages and messages[-1].timestamp:
                self._debug_offset = messages[-1].timestamp - clock.time()
            self._live_dirty = True

        def _update_debugger(self):
            """Start or stop reading the debugger variables: only while recording and with the check mark."""
            wanted = self.live() and self.recorder is not None and self.recorder.running() \
                and self.show_debugger.get() and variable_names(self.debugger_settings)
            if wanted and self._debug_poller is None:
                self._set_debug_status('idle', 'TRACE32 verbindet …')
                self._debug_poller = T32Poller(self.debugger_settings, self._debug_queue)
                self._debug_poller.start()
            elif not wanted:
                self._stop_debugger()

        def _stop_debugger(self):
            if self._debug_poller is not None:
                self._debug_poller.stop()
                self._drain_debugger()
                self._debug_poller = None
                self._debug_queue = queue.Queue()  # ignore messages from the previous worker
                self._set_debug_status(None)

        def _set_debug_status(self, state, message='', *args):
            self._debug_state = state
            set_text(self.debugger_status, message, *args)
            self._render_debug_status()

        def _render_debug_status(self):
            if self._debug_bar is None:
                return
            if self._debug_state is None:
                self._debug_bar.pack_forget()
                return
            color = {'connected': C.SUCCESS, 'error': C.DANGER}.get(self._debug_state, C.SUBTLE)
            self._debug_dot.itemconfigure('status', fill=color)
            self._debug_bar.pack(side=tk.BOTTOM, before=self._trace_body, fill=tk.X)

        def _debug_time(self, wall):
            """Trace time of a clock time: same time base as the recorded frames."""
            if self._debug_offset is not None and self._live_t0 is not None:
                return wall + self._debug_offset - self._live_t0
            return wall - (self._debug_wall0 or wall)

        def _drain_debugger(self):
            """Take the values of the debugger thread over into the recording (GUI thread)."""
            changed = False
            latest = {}
            while True:
                try:
                    item = self._debug_queue.get_nowait()
                except queue.Empty:
                    break
                if item[0] == 'status':
                    self._set_debug_status(*item[1:])
                elif self.live():
                    _, timestamp, symbol, value, valid = item
                    moment = self._debug_time(timestamp)
                    self._live_trace['debugger'].append((moment, symbol, value))
                    name = self._debug_environment_names.get(symbol)
                    if name and valid:
                        text = {'True': '1', 'False': '0'}.get(value, ' '.join(value.splitlines()))
                        self._live_trace['environment'].setdefault(name, []).append((moment, text))
                        latest[name] = text
                    changed = True
            if latest and self.on_live_environment is not None:
                self.on_live_environment(latest)
            if changed:
                self._live_dirty = True

        def configure_debugger(self):
            dialog = DebuggerDialog(self.root, self.debugger_settings)
            if dialog.result is None:
                return
            names = watch_environment_names(dialog.result)
            old_path = self.debugger_settings.get('environment_database')
            try:
                path = create_watch_database(names, config_name=self.config_name())
            except (OSError, ValueError) as error:
                messagebox.showerror(tr('TRACE32-Variablen'),
                                     tr('TRACE32-Datenbasis konnte nicht erstellt werden: %s') % error,
                                     parent=self.root)
                return
            self._stop_debugger()
            self.debugger_settings = dialog.result
            self._debug_environment_names = names
            if path:
                self.debugger_settings['environment_database'] = path
                self.show_debugger.set(True)
                self.show_environment.set(True)
            if self.on_watch_database is not None and (path or old_path):
                self.on_watch_database(old_path, path)
            self._update_debugger()
            if path:
                self.on_row_kinds()

        def live_environment_values(self):
            return {name: values[-1][1] for name, values in self._live_trace['environment'].items() if values}

        def _live_tick(self):
            self._live_job = None
            if not self.live():
                return
            self._drain_debugger()
            if self._live_dirty:
                self._live_refresh()
            self._live_job = self.root.after(LIVE_REFRESH_MS, self._live_tick)

        def _live_refresh(self):
            """Show the newly recorded frames: tables and graph, following the end while the user has not
            paged back or zoomed."""
            self._live_dirty = False
            follow_page = self.trace_page >= self._live_last_page
            follow_graph = self.x_range is None or self.x_range == self._live_range
            if follow_page:
                self.trace_page = 10 ** 9  # render_trace limits it to the last page
            self.rebuild(live=True)
            self._live_last_page = self.trace_page
            if follow_page and self.mode.get() == 'chronological' and self.trace_tree is not None:
                self.trace_tree.yview_moveto(1.0)
            if follow_graph:
                self.fit_x()
                self._live_range = list(self.x_range) if self.x_range else None

        def _live_messages(self, messages, reset=False):
            if self.on_live is not None:
                self.on_live(messages, reset)

        def _hint(self, parent, text, padding):
            """Muted usage hint on its own line; wraps when the area is narrow."""
            label = ttk.Label(parent, text=text, foreground=C.MUTED, font=self.font_axis, padding=padding)
            label.pack(side=tk.TOP, fill=tk.X)
            label.bind('<Configure>', lambda event: label.configure(
                wraplength=max(100, event.width - padding[0] - padding[2])))
            return label

        def _build_trace_pane(self, parent):
            pane, bar = self._pane(parent, 'Trace')
            self.trace_pane = pane
            self.theme.bar_label(bar, self.trace_count)
            ttk.Label(bar, text=tr('Fenster')).pack(side=tk.LEFT, padx=(12, 4), pady=2)
            self.trace_combo = ttk.Combobox(bar, textvariable=self.trace_choice, state='readonly', width=3,
                                            values=[str(number + 1) for number in range(GRAPH_COUNT)])
            self.trace_combo.pack(side=tk.LEFT, pady=2)
            self.trace_combo.bind('<<ComboboxSelected>>', lambda event: self.select_trace(
                int(self.trace_choice.get()) - 1))
            # Fixed position = one row per message (overview), otherwise chronological.
            ttk.Checkbutton(bar, style='Toolbutton', text=tr('Feste Position'), variable=self.mode, onvalue='overview',
                            offvalue='chronological', command=self.on_mode).pack(side=tk.RIGHT, padx=4, pady=2)
            ttk.Checkbutton(bar, style='Toolbutton', text=tr('Uhrzeit'), variable=self.absolute_time,
                            command=self.on_absolute_time).pack(side=tk.RIGHT, padx=(4, 8), pady=2)
            ttk.Checkbutton(bar, style='Toolbutton', text=tr('Beschreibung'), variable=self.show_description,
                            command=self._apply_time_columns).pack(side=tk.RIGHT, padx=4, pady=2)
            ttk.Checkbutton(bar, style='Toolbutton', text=tr('TRACE32'), variable=self.show_debugger,
                            command=self.on_debugger).pack(side=tk.RIGHT, padx=4, pady=2)
            ttk.Checkbutton(bar, style='Toolbutton', text=tr('CAN-Fehler'), variable=self.show_errors,
                            command=self.on_row_kinds).pack(side=tk.RIGHT, padx=4, pady=2)
            ttk.Checkbutton(bar, style='Toolbutton', text=tr('Umgebungsvariablen'), variable=self.show_environment,
                            command=self.on_row_kinds).pack(side=tk.RIGHT, padx=4, pady=2)

            toolbar = ttk.Frame(pane, padding=(12, 8))
            toolbar.pack(side=tk.TOP, fill=tk.X)
            ttk.Button(toolbar, text=tr('Speichern unter…'), command=self.save_trace_as).pack(side=tk.LEFT)
            self.clear_button = ttk.Button(toolbar, text=tr('Löschen'), command=self.clear_live)
            self.clear_button.pack(side=tk.LEFT, padx=(6, 0))
            self._update_clear_button()
            # Page buttons are packed before the filter, so a narrow pane shrinks the filter, not them.
            self.next_button = ttk.Button(toolbar, text='›', width=2, command=lambda: self.change_page(1))
            self.next_button.pack(side=tk.RIGHT)
            ttk.Label(toolbar, textvariable=self.page_label, foreground=C.MUTED).pack(side=tk.RIGHT, padx=4)
            self.prev_button = ttk.Button(toolbar, text='‹', width=2, command=lambda: self.change_page(-1))
            self.prev_button.pack(side=tk.RIGHT, padx=(8, 0))
            ttk.Button(toolbar, text='Filter', command=self.open_filter_picker).pack(side=tk.LEFT, padx=(16, 6))
            ttk.Entry(toolbar, textvariable=self.trace_filter, width=8).pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._hint(pane, tr('Doppelklick auf Nachricht: Signale · Doppelklick auf Signal oder Klick in '
                                'Spalte „Grafik“: Grafik an/aus'), (12, 0, 12, 6))

            frame = ttk.Frame(pane, padding=(4, 0, 0, 4))
            frame.pack(fill=tk.BOTH, expand=True)
            tree = self.trace_tree = ttk.Treeview(frame, columns=[c[0] for c in TRACE_COLUMNS],
                                                  selectmode='browse')
            tree.column('#0', width=RELATIVE_WIDTH, minwidth=90, stretch=False)
            for name, title, width, anchor in TRACE_COLUMNS:
                tree.heading(name, text=tr(title), anchor=anchor)
                tree.column(name, width=width, minwidth=30, anchor=anchor, stretch=name == 'data')
            tree.column('description', minwidth=120)
            tree.bind('<Configure>', lambda event: self._distribute_columns(), add='+')
            self._apply_time_columns()
            tree.tag_configure('signal', background=C.SURFACE_ALT)
            tree.tag_configure('unknown', foreground=C.MUTED)
            tree.tag_configure('environment', foreground=C.ACCENT_HOVER)
            tree.tag_configure('debugger', foreground=C.SUCCESS)
            tree.tag_configure('error', foreground=C.DANGER)
            vscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
            hscroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
            tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
            tree.grid(row=0, column=0, sticky='nsew')
            vscroll.grid(row=0, column=1, sticky='ns')
            hscroll.grid(row=1, column=0, sticky='ew')
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            tree.bind('<<TreeviewOpen>>', self.on_trace_open)
            tree.bind('<<TreeviewClose>>', self.on_trace_close)
            tree.bind('<Button-1>', self.on_trace_click)
            tree.bind('<Double-Button-1>', self.on_trace_double_click)
            tree.bind('<space>', self.on_trace_space)
            # Trace context menu (macOS: Button-2 (Tk 8.6), Button-3 (Tk 9) or
            # Control+click, others: Button-3).
            right_buttons = ('<Button-2>', '<Button-3>', '<Control-Button-1>') \
                if tree.tk.call('tk', 'windowingsystem') == 'aqua' else ('<Button-3>',)
            for sequence in right_buttons:
                tree.bind(sequence, self.on_trace_right_click)

        def _build_graph_pane(self, parent):
            pane, bar = self._pane(parent, tr('Grafik'))
            self.graph_pane = pane
            self.picker_button = ttk.Button(bar, text=tr('Signalauswahl'), command=self.open_picker)
            self.picker_button.pack(side=tk.LEFT, padx=4, pady=2)
            ttk.Label(bar, text=tr('Fenster')).pack(side=tk.LEFT, padx=(12, 4), pady=2)
            self.graph_combo = ttk.Combobox(bar, textvariable=self.graph_choice, state='readonly', width=3,
                                            values=[str(number + 1) for number in range(GRAPH_COUNT)])
            self.graph_combo.pack(side=tk.LEFT, pady=2)
            self.graph_combo.bind('<<ComboboxSelected>>', lambda event: self.select_graph(
                int(self.graph_choice.get()) - 1))

            area = ttk.Frame(pane)
            area.pack(fill=tk.BOTH, expand=True)
            toolbar = ttk.Frame(area, padding=(8, 10, 8, 6))
            toolbar.pack(side=tk.TOP, fill=tk.X)
            ttk.Button(toolbar, text='Fit X', command=self.fit_x).pack(side=tk.LEFT)
            ttk.Button(toolbar, text='Fit Y', command=self.fit_y).pack(side=tk.LEFT, padx=6)
            ttk.Button(toolbar, text='Fit all', command=self.reset_zoom).pack(side=tk.LEFT)
            for value, text in (('x', 'Zoom X'), ('y', 'Zoom Y')):
                ttk.Radiobutton(toolbar, text=text, value=value, variable=self.zoom_axis,
                                style='Toolbutton').pack(side=tk.LEFT, padx=(16 if value == 'x' else 0, 0))
            ttk.Checkbutton(toolbar, text=tr('2. Cursor'), variable=self.second_cursor, style='Toolbutton',
                            command=self.on_second_cursor).pack(side=tk.LEFT, padx=(16, 0))
            self._hint(area, tr('Klick: Cursor (mit 2. Cursor: der nähere) · Rechtsklick: 2. Cursor · Mausrad und '
                                'Ziehen: Zoom in der gewählten Achse · Shift+Mausrad: andere Achse'), (8, 0, 8, 6))
            self.cursor_info_label = ttk.Label(area, textvariable=self.cursor_info, padding=(12, 4, 12, 8))
            canvas_frame = ttk.Frame(area)
            canvas_frame.pack(fill=tk.BOTH, expand=True)
            plot = self.plot = tk.Canvas(canvas_frame, background=C.SURFACE, highlightthickness=0,
                                         cursor='crosshair')
            plot_scroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=plot.yview)
            plot.configure(yscrollcommand=plot_scroll.set)
            plot.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            plot_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            plot.bind('<Configure>', lambda event: self.schedule_plot())
            plot.bind('<ButtonPress-1>', self.on_plot_press)
            plot.bind('<B1-Motion>', self.on_plot_drag)
            plot.bind('<ButtonRelease-1>', self.on_plot_release)
            # Right click sets the 2nd cursor (macOS: Button-2 or Control+click, others: Button-3).
            right_buttons = ('<Button-2>', '<Button-3>', '<Control-Button-1>') \
                if plot.tk.call('tk', 'windowingsystem') == 'aqua' else ('<Button-3>',)
            for sequence in right_buttons:
                plot.bind(sequence, self.on_plot_right_click)
            plot.bind('<MouseWheel>', self.on_plot_wheel)
            plot.bind('<Button-4>', lambda event: self.zoom_plot(event, -0.15))
            plot.bind('<Button-5>', lambda event: self.zoom_plot(event, 0.15))
            try:
                plot.bind('<TouchpadScroll>', self.on_plot_touchpad)  # Tk 9 trackpads
            except tk.TclError:
                pass

        # ------------------------------------------------------------ background jobs

        def start_job(self, message, request, done, work=load_sources, failure='Laden fehlgeschlagen: %s'):
            """message: (template, *args) for the footer. work(request) runs in a worker thread, done(result)
            afterwards in the GUI thread; an error is shown with the footer template failure."""
            if self.busy:
                self.set_footer('Bitte warten, es wird noch geladen…')
                return
            self.busy = True
            self.set_footer(*message)
            self.root.configure(cursor='watch')

            def run():
                try:
                    outcome = (True, work(request))
                except Exception as error:  # reported in the GUI
                    outcome = (False, error)
                self.results.put((done, outcome, failure))
            threading.Thread(target=run, daemon=True).start()

        def range_active(self):
            return self.range_start is not None and self.range_end is not None

        def _clip_time(self, time):
            return not self.range_active() or self.range_start <= time <= self.range_end

        def _apply_range_data(self):
            """environment and errors: those of the selected range (all of them without one; a recording keeps
            the identity of its lists)."""
            if not self.range_active():
                self.environment, self.errors = self._all_environment, self._all_errors
                return
            self.environment = dict((name, [value for value in values if self._clip_time(value[0])])
                                    for name, values in self._all_environment.items())
            self.errors = [error for error in self._all_errors if self._clip_time(error[0])]

        def set_range(self, start, end):
            """Show only the time range start..end (seconds; None = the whole trace) in table and graph."""
            if end is None or start is None:
                start = end = None
            if (start, end) == (self.range_start, self.range_end):
                return
            self.range_start, self.range_end = start, end
            self._apply_range_data()
            self._reset_traces()
            self.rebuild()
            self.fit_x()

        def save_trace_as(self):
            """Save the whole trace (all frames of all channels; with several traces the combined one) in a
            format chosen in the save dialog."""
            frames = self.frames
            if not frames and not any(self._all_environment.values()):
                self.set_footer('Kein Trace geladen.')
                return
            if self.busy:
                self.set_footer('Bitte warten, es wird noch geladen…')
                return
            folder = os.path.dirname(self.trace_path) if self.trace_path else os.getcwd()
            stem = datetime.datetime.now().strftime('CAN Trace - %d-%m-%Y - %H_%M_%S')
            file_type = tk.StringVar(master=self.root)
            path = filedialog.asksaveasfilename(
                parent=self.root, title=tr('Trace speichern unter'), initialdir=folder, initialfile=stem + '.asc',
                filetypes=[('%s (*%s)' % (text, extension), '*' + extension)
                           for key, text, extension in WRITE_FORMATS], typevariable=file_type)
            if not path:
                return
            key = format_for_path(path, file_type.get())
            if key is None:
                self.set_footer('Unbekanntes Dateiformat: %s', os.path.basename(path))
                return
            extension = next(ending for name, text, ending in WRITE_FORMATS if name == key)
            if not os.path.splitext(path)[1]:
                path += extension
            name = next(text for name, text, ending in WRITE_FORMATS if name == key)
            export = self._trace_export_data(frames)
            if export is None:
                return
            if not export[0] and key != 'asc':
                self.set_footer('Umgebungsvariablen ohne CAN-Frames können nur als ASC gespeichert werden.')
                return
            self.start_job(('Wird gespeichert: %s', path), export,
                           lambda result: self.set_footer('Trace gespeichert (%s, %s Frames): %s', name,
                                                          lambda: de_int(len(export[0])), path),
                           work=lambda data: write_trace(path, key, *data),
                           failure='Speichern fehlgeschlagen: %s')

        def _trace_export_data(self, frames):
            # Environment variables and CAN errors are only written to ASC.
            environment = {name: list(values) for name, values in self._all_environment.items()}
            errors = list(self._all_errors)
            start = self.trace_start.timestamp() if self.trace_start else clock.time()
            if not self.range_active():
                frames = list(frames)
            else:  # only the selected range, shifted to start at 0 (the measurement start moves with it)
                begin, end = self.range_start, self.range_end
                frames = [copy.copy(frame) for frame in frames if begin <= frame.time <= end]
                for frame in frames:
                    frame.time -= begin
                environment = dict((name, [(time - begin, text) for time, text in values if begin <= time <= end])
                                   for name, values in environment.items())
                errors = [(time - begin, channel, text) for time, channel, text in errors if begin <= time <= end]
                start += begin
                if not frames and not any(environment.values()):
                    self.set_footer('Der gewählte Bereich enthält keine Daten.')
                    return
            return frames, environment, start, errors

        def copy_trace_to_clipboard(self):
            if self.busy:
                self.set_footer('Bitte warten, es wird noch geladen…')
                return
            if not self.frames and not any(self._all_environment.values()):
                self.set_footer('Kein Trace geladen.')
                return
            export = self._trace_export_data(self.frames)
            if export is None:
                return

            def format_asc(data):
                with io.StringIO() as handle:
                    write_asc(handle, *data)
                    return handle.getvalue()

            def copy_to_clipboard(text):
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(text)
                except tk.TclError as error:
                    self.report_error(error, 'Kopieren fehlgeschlagen: %s')
                    return
                self.set_footer('Trace in die Zwischenablage kopiert (%s Frames).', de_int(len(export[0])))

            self.start_job(('Trace wird kopiert…',), export, copy_to_clipboard, work=format_asc,
                           failure='Kopieren fehlgeschlagen: %s')

        def poll_results(self):
            try:
                while True:
                    done, (ok, value), failure = self.results.get_nowait()
                    self.busy = False
                    self.root.configure(cursor='')
                    if ok:
                        done(value)
                    else:
                        self.report_error(value, failure)
            except queue.Empty:
                pass
            self._poll_job = self.root.after(60, self.poll_results)

        def report_error(self, error, failure='Laden fehlgeschlagen: %s'):
            text = str(error) or error.__class__.__name__
            if isinstance(error, OSError) and getattr(error, 'strerror', None):
                text = '%s: %s' % (error.strerror, error.filename or '')
            self.set_footer(failure, text)

        def apply_sources(self, result):
            if 'database' in result:
                database = result['database'] or {}
                self.database_name = database.get('name', '')
                self.database_path = database.get('path')
                self.messages = database.get('messages', [])
                self.db_environment = database.get('environment') or {}
            if 'trace' in result:
                self._loaded_trace = result['trace'] or {}
                if not self.live():  # live shows the recording; the trace comes back with the switch to Trace
                    self._set_trace(self._loaded_trace)

        @staticmethod
        def _new_trace():
            return {'mode': 'chronological', 'absoluteTime': False, 'showEnvironment': False, 'showErrors': False,
                    'showDescription': False, 'showDebugger': False, 'tracePage': 0, 'traceFilter': '', 'expanded': [], 'hidden': []}

        def _store_trace(self):
            """Save the state of the shown trace window into self.traces."""
            self.traces[self.trace_index] = {
                'mode': self.mode.get(), 'absoluteTime': self.absolute_time.get(),
                'showEnvironment': self.show_environment.get(), 'showErrors': self.show_errors.get(),
                'showDescription': self.show_description.get(), 'showDebugger': self.show_debugger.get(), 'tracePage': self.trace_page, 'traceFilter': self.trace_filter.get(),
                'expanded': sorted(self.expanded), 'hidden': sorted(self.hidden)}

        def _load_trace(self, state):
            """Make a saved trace state the shown one (without redrawing)."""
            self.mode.set('overview' if state.get('mode') == 'overview' else 'chronological')
            self.absolute_time.set(state.get('absoluteTime') is True)
            self.show_environment.set(state.get('showEnvironment') is True)
            self.show_errors.set(state.get('showErrors') is True)
            self.show_description.set(state.get('showDescription') is True)
            self.show_debugger.set(state.get('showDebugger') is True)
            page = state.get('tracePage')
            self.trace_page = page if isinstance(page, int) and page >= 0 else 0
            text = str(state.get('traceFilter') or '')
            if self.trace_filter.get() != text:
                self.trace_filter.set(text)
            expanded = state.get('expanded')
            self.expanded = set(item for item in expanded if isinstance(item, str)) \
                if isinstance(expanded, list) else set()
            hidden = state.get('hidden')
            self.hidden = set(item for item in hidden if isinstance(item, str)) \
                if isinstance(hidden, list) else set()
            self.filter_cache = None
            self.trace_choice.set(str(self.trace_index + 1))

        def select_trace(self, index):
            """Show trace window index (0-based) instead of the current one."""
            if not 0 <= index < GRAPH_COUNT:
                return
            if index == self.trace_index:
                self.trace_choice.set(str(index + 1))
                return
            self._store_trace()
            self.trace_index = index
            self._load_trace(self.traces[index])
            self._apply_time_columns()
            if getattr(self, '_last', None) is not None:
                self._build_rows()
            self.render_trace()
            self.render_filter_options()
            if self.trace_tree is not None:
                self.trace_tree.yview_moveto(0)

        def _reset_traces(self):
            """New trace or channel: first page, everything closed in every window."""
            self._store_trace()
            for state in self.traces:
                state.update(tracePage=0, expanded=[], hidden=[])
            self.trace_page = 0
            self.expanded.clear()
            self.hidden.clear()

        @staticmethod
        def _new_graph():
            return {'selectedSignals': [], 'xRange': None, 'yRange': None, 'cursorTime': None,
                    'secondCursor': False, 'cursor2Time': None, 'zoomAxis': 'x'}

        def _store_graph(self):
            """Save the state of the shown graph window into self.graphs."""
            self.graphs[self.graph_index] = {
                'selectedSignals': list(self.selected), 'xRange': self.x_range, 'yRange': self.y_range,
                'cursorTime': self.cursor_time, 'secondCursor': self.second_cursor.get(),
                'cursor2Time': self.cursor2_time, 'zoomAxis': self.zoom_axis.get()}

        def _load_graph(self, state):
            """Make a saved graph state the shown one (without redrawing)."""
            def number(value):
                return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None
            selected = state.get('selectedSignals')
            self.selected = [item for item in selected if isinstance(item, str)] \
                if isinstance(selected, list) else []
            self.x_range = finite_pair(state.get('xRange'))
            self.y_range = finite_pair(state.get('yRange'))
            self.cursor_time = number(state.get('cursorTime'))
            self.cursor2_time = number(state.get('cursor2Time'))
            self.second_cursor.set(state.get('secondCursor') is True)
            self.zoom_axis.set('y' if state.get('zoomAxis') == 'y' else 'x')
            self.graph_choice.set(str(self.graph_index + 1))

        def select_graph(self, index):
            """Show graph window index (0-based) instead of the current one."""
            if not 0 <= index < GRAPH_COUNT:
                return
            if index == self.graph_index:
                self.graph_choice.set(str(index + 1))
                return
            self._store_graph()
            self.graph_index = index
            self._load_graph(self.graphs[index])
            self._update_cursor_info()
            self.render_trace()
            self.render_signal_options()
            if self.x_range is None and self.frames:
                self.fit_x()
            else:
                self.schedule_plot()
            if self.on_cursor_changed is not None and self.cursor_time is not None:
                self.on_cursor_changed(self.cursor_time)

        def _reset_graphs(self, signals=False):
            """New files: clear zoom and cursors (and with signals the signal selection) of every window."""
            self._store_graph()
            for state in self.graphs:
                if signals:
                    state['selectedSignals'] = []
                state.update(xRange=None, yRange=None)
                if not signals:
                    state.update(cursorTime=None, cursor2Time=None)
            self._load_graph(self.graphs[self.graph_index])

        def on_files_loaded(self, result):
            """Newly opened files: reset the view."""
            self.apply_sources(result)
            if 'database' in result:
                self._reset_graphs(signals=True)
            if 'trace' in result:
                self._reset_graphs()
                self._reset_traces()
            self.rebuild()
            if 'trace' in result:
                self.fit_x()
            if 'database' in result and 'trace' not in result:
                count = len(self.messages)
                self.set_footer('%s Datenbank-Nachrichten geladen', lambda: de_int(count))

        def set_sources(self, result, view=None):
            """Show files loaded by the caller (result as returned by load_sources). Without a view
            the view is reset for the new files, otherwise the given view is restored."""
            if view is None:
                self.on_files_loaded(result)
            else:
                self.on_view_files_loaded(result, view)

        def on_view_files_loaded(self, result, view):
            """Files from a saved configuration: restore the saved view."""
            self.apply_sources(result)
            self.apply_view(view)
            self.rebuild()
            self.set_footer('Dateien geladen')

        # ------------------------------------------------------------ view state & window settings

        def collect_view(self):
            return {
                'mode': self.mode.get(), 'selectedSignals': list(self.selected),
                'xRange': self.x_range, 'yRange': self.y_range, 'cursorTime': self.cursor_time,
                'tracePage': self.trace_page, 'traceFilter': self.trace_filter.get(),
                'signalFilter': self.signal_filter.get(),
                'absoluteTime': self.absolute_time.get(), 'zoomAxis': self.zoom_axis.get(),
                'showEnvironment': self.show_environment.get(), 'showErrors': self.show_errors.get(),
                'showDescription': self.show_description.get(), 'showDebugger': self.show_debugger.get(),
                'debugger': self.debugger_settings,
                'secondCursor': self.second_cursor.get(), 'cursor2Time': self.cursor2_time,
                'traceIndex': self.trace_index, 'traces': self._traces_for_save(),
                'graphIndex': self.graph_index, 'graphs': self._graphs_for_save(),
            }

        def _traces_for_save(self):
            self._store_trace()
            # opened messages are not saved: after loading, all messages are closed
            return [dict((key, value) for key, value in state.items() if key != 'expanded')
                    for state in self.traces]

        def _graphs_for_save(self):
            self._store_graph()
            return [dict(state) for state in self.graphs]

        def apply_view(self, view):
            self.mode.set('overview' if view.get('mode') == 'overview' else 'chronological')
            self.absolute_time.set(view.get('absoluteTime') is True)
            self.show_environment.set(view.get('showEnvironment') is True)
            self.show_errors.set(view.get('showErrors') is True)
            self.show_description.set(view.get('showDescription') is True)
            self.show_debugger.set(view.get('showDebugger') is True)
            self.debugger_settings = normalize_settings(view.get('debugger'))
            self._debug_environment_names = watch_environment_names(self.debugger_settings)
            self.zoom_axis.set('y' if view.get('zoomAxis') == 'y' else 'x')
            cursor2 = view.get('cursor2Time')
            self.cursor2_time = float(cursor2) if isinstance(cursor2, (int, float)) and math.isfinite(cursor2) \
                else None
            self.second_cursor.set(view.get('secondCursor') is True)
            self._update_cursor_info()
            self._apply_time_columns()
            page = view.get('tracePage')
            self.trace_page = page if isinstance(page, int) and page >= 0 else 0
            self.trace_filter.set(str(view.get('traceFilter') or ''))
            self.signal_filter.set(str(view.get('signalFilter') or ''))
            self.expanded = set()
            traces = view.get('traces')
            self.traces = [self._new_trace() for _ in range(GRAPH_COUNT)]
            if isinstance(traces, list) and traces and all(isinstance(item, dict) for item in traces):
                for index, state in enumerate(traces[:GRAPH_COUNT]):
                    self.traces[index] = dict(state, expanded=[])
                index = view.get('traceIndex')
                self.trace_index = index if isinstance(index, int) and 0 <= index < GRAPH_COUNT else 0
                self._load_trace(self.traces[self.trace_index])
                self._apply_time_columns()
            else:  # older configuration: one trace window (the values set above)
                self.trace_index = 0
            graphs = view.get('graphs')
            if isinstance(graphs, list) and graphs and all(isinstance(item, dict) for item in graphs):
                self.graphs = [self._new_graph() for _ in range(GRAPH_COUNT)]
                for index, state in enumerate(graphs[:GRAPH_COUNT]):
                    self.graphs[index] = dict(state)
                index = view.get('graphIndex')
                self.graph_index = index if isinstance(index, int) and 0 <= index < GRAPH_COUNT else 0
            else:  # older configuration: one graph
                self.graphs = [self._new_graph() for _ in range(GRAPH_COUNT)]
                self.graph_index = 0
                self.graphs[0] = dict((key, view.get(key)) for key in self.graphs[0] if key in view)
            self._load_graph(self.graphs[self.graph_index])
            self._update_cursor_info()

        def window_settings(self, section=None):
            """Window geometry (of the section's window; picker windows belong to their section)."""
            section = section if section in self.hosts else next(iter(self.hosts), 'trace')
            settings = {}
            if section in self.hosts:
                settings['geometry'] = window_geometry(self.hosts[section])
            elif self.geometries.get(section):
                settings['geometry'] = self.geometries[section]
            if section == 'graph':
                self._remember_picker_geometry()
                if self.settings.get('picker_geometry'):
                    settings['picker_geometry'] = self.settings['picker_geometry']
            else:
                self._remember_filter_geometry()
                if self.settings.get('filter_geometry'):
                    settings['filter_geometry'] = self.settings['filter_geometry']
            return settings

        def shutdown(self):
            """Last window closed: stop the recording and the timers."""
            self._stop_debugger()
            if self.recorder is not None:
                self.recorder.close()
            if self._live_job is not None:
                self.root.after_cancel(self._live_job)
            for job in (self._poll_job, self._plot_job, self._filter_job, self._position_job):
                if job is not None:
                    try:
                        self.root.after_cancel(job)
                    except tk.TclError:
                        pass
            self._live_job = self._poll_job = self._plot_job = self._filter_job = self._position_job = None

        def set_footer(self, template, *args):
            """Status line: German template (translated with tr) and its arguments."""
            set_text(self.footer, template, *args)

        def set_channel(self, channel, redraw=True):
            """Show only the frames of one CAN channel (e.g. '1'); None shows all channels."""
            if channel == self.channel:
                return
            self.channel = channel
            self._reset_traces()
            if redraw:
                self.rebuild()
                self.fit_x()

        def language_changed(self):
            """Redraw what the window builds itself (labels and buttons are translated by set_language)."""
            self.render_trace()
            self.render_filter_options()
            self.render_signal_options()
            self.schedule_plot()

        # ------------------------------------------------------------ analysis

        def rebuild(self, live=False):
            """Analyse the frames and fill the tables. live: refresh while recording; the selection windows and the
            status line are only touched when messages or variables are new."""
            index = MessageIndex(self.messages)  # several databases: per CAN channel (see combine_databases)
            counts, last = {}, {}
            by_message = {}
            frames = self.frames
            if self.range_active():
                frames = [frame for frame in frames if self.range_start <= frame.time <= self.range_end]
            frames = self.visible_frames = (frames if self.channel is None
                                            else [frame for frame in frames if frame.channel == self.channel])
            for frame in frames:
                message = index.find(frame.id, frame.channel)
                frame.message = message
                frame.name = message['name'] if message else (frame.trace_name or 'ID 0x%X' % frame.id)
                key = frame.channel_key()
                counts[key] = counts.get(key, 0) + 1
                last[key] = frame
                if message is not None:
                    by_message.setdefault(id(message), []).append(frame)
            for frame in frames:
                frame.count = counts[frame.channel_key()]
            self.overview_frames = sorted(last.values(), key=lambda frame: natural_key(frame.name))
            self.frames_by_message = by_message
            self._last = last
            self._build_rows()

            self.signal_lookup = {}
            self.trace_signals = []
            # Groups of the signal selection: (group id, title, [(key, name, unit)]); messages first, then
            # the digital/analog I/O and the other environment variables.
            self.signal_groups = []
            seen_messages = set()
            for frame in sorted(last.values(), key=lambda frame: natural_key(frame.name)):
                message = frame.message
                if message is None or id(message) in seen_messages:
                    continue
                seen_messages.add(id(message))
                members = []
                for signal in message['signals']:
                    key = signal_key(message, signal)
                    if key not in self.signal_lookup:
                        self.signal_lookup[key] = (message, signal)
                        unit = signal.get('unit') or ''
                        self.trace_signals.append((key, unit))
                        members.append((key, signal['name'], unit))
                if members:
                    members.sort(key=lambda member: natural_key(member[1]))
                    title = '%s (%s)' % (message['name'], '0x%X' % normalize_id(frame.id))
                    self.signal_groups.append(('msg:' + message['name'], title, members))
            env_groups = {'io': [], 'env': []}
            for name in sorted(self.environment, key=natural_key):
                unit = (self.db_environment.get(name) or {}).get('unit') or ''
                self.trace_signals.append((env_key(name), unit))
                env_groups['io' if is_io_variable(name) else 'env'].append((env_key(name), name, unit))
            if env_groups['io']:
                self.signal_groups.append(('group:io', 'Digitale und analoge Signale', env_groups['io']))
            if env_groups['env']:
                self.signal_groups.append(('group:env', 'Umgebungsvariablen', env_groups['env']))
            self.series_cache = {}
            self.decoders = {}
            self.filter_cache = None

            signature = (frozenset(last), frozenset(self.environment))
            unchanged = live and signature == self._live_signature
            self._live_signature = signature
            self.render_trace()
            if not unchanged:
                self.render_filter_options()
                self.render_signal_options()
            self.schedule_plot()
            if live:
                pass
            elif self.frames:
                count = len(frames)
                self.set_footer('%s Trace-Frames analysiert', lambda: de_int(count))
            else:
                self.set_footer('Bereit')

        def _build_rows(self):
            """Rows of the trace table and the overview: the frames of the channel plus, when switched on,
            the values of environment variables (no CAN channel) and CAN error frames."""
            if not self.frames and self.environment and not self.live() and not self.show_environment.get():
                self.show_environment.set(True)  # a trace of signals only (e.g. MDF4 signal logging): show them
            frames, last = self.visible_frames, self._last
            events = environment_events(self.environment) if self.show_environment.get() else []
            errors = [error for error in error_events(self.errors)
                      if self.channel is None or error.channel == self.channel] if self.show_errors.get() else []
            debug = debug_events(self.debug_values) if self.show_debugger.get() and self.live() else []
            sources = [source for source in (events, errors, debug, frames) if source]
            self.trace_rows = list(heapq.merge(*sources, key=lambda row: row.time)) if len(sources) > 1 \
                else (sources[0] if sources else frames)
            latest = {}
            for event in events:
                latest[event.name] = event
            for event in latest.values():
                event.count = len(self.environment[event.name])
            latest_debug = {}
            for event in debug:
                latest_debug[event.name] = event
                event.count = 0
            for event in debug:
                latest_debug[event.name].count += 1
            latest_errors = {}
            for error in errors:
                latest_errors[error.channel] = error
                error.count = 0
            for error in errors:
                latest_errors[error.channel].count += 1
            self.overview_rows = sorted(list(last.values()) + list(latest.values()) + list(latest_errors.values()) + list(latest_debug.values()),
                                        key=lambda row: natural_key(row.name))
            self.filter_cache = None

        def on_row_kinds(self):
            """Environment variables or CAN errors switched on or off in the trace table."""
            self._build_rows()
            self.trace_page = 0
            self.render_trace()

        def on_debugger(self):
            """Debugger variables switched on or off: rows, and the reading while recording."""
            if self.show_debugger.get() and not variable_names(self.debugger_settings):
                self.configure_debugger()  # first: ask for the variables
                if not variable_names(self.debugger_settings):
                    self.show_debugger.set(False)
            self._update_debugger()
            self.on_row_kinds()

        def decoder(self, signal):
            decoder = self.decoders.get(id(signal))
            if decoder is None:
                decoder = self.decoders[id(signal)] = SignalDecoder(signal)
            return decoder

        def series(self, key):
            if key in self.series_cache:
                return self.series_cache[key]
            entry = self.signal_lookup.get(key)
            result = None
            if key.startswith(ENV_PREFIX) and key[len(ENV_PREFIX):] in self.environment:
                result = self._env_series(key, key[len(ENV_PREFIX):])
            elif entry is not None:
                message, signal = entry
                decoder = self.decoder(signal)
                result = Series(key, signal['name'], message['name'], signal.get('unit') or '')
                xs, ys, status = result.xs, result.ys, result.status
                for frame in self.frames_by_message.get(id(message), ()):
                    value = decoder.decode(frame)
                    if value is not None and math.isfinite(value[1]):
                        xs.append(frame.time)
                        ys.append(value[1])
                        status.append(value[2])
                if any(xs[i] > xs[i + 1] for i in range(len(xs) - 1)):
                    order = sorted(range(len(xs)), key=xs.__getitem__)
                    result.xs = [xs[i] for i in order]
                    result.ys = [ys[i] for i in order]
                    result.status = [status[i] for i in order]
            self.series_cache[key] = result
            return result

        def _env_series(self, key, name):
            """Series of an environment variable; values that are no numbers are left out."""
            variable = self.db_environment.get(name) or {}
            texts = variable.get('values') or {}
            result = Series(key, name, '', variable.get('unit') or '')  # no message: the label is the name
            points = []
            for time, value in self.environment.get(name, ()):
                try:
                    number = float(value)
                except ValueError:
                    continue
                if math.isfinite(number):
                    points.append((time, number, texts.get(number, '')))
            points.sort(key=lambda point: point[0])
            result.xs = [point[0] for point in points]
            result.ys = [point[1] for point in points]
            result.status = [point[2] for point in points]
            return result

        # ------------------------------------------------------------ trace table

        def filtered_frames(self):
            query = self.trace_filter.get().strip().casefold()
            mode = self.mode.get()
            cache_key = (mode, query, id(self.trace_rows), frozenset(self.hidden))
            if self.filter_cache is not None and self.filter_cache[0] == cache_key:
                return self.filter_cache[1]
            rows = self.trace_rows if mode == 'chronological' else self.overview_rows
            if self.hidden:
                rows = [row for row in rows if self.row_choice_key(row) not in self.hidden]
            if query:
                rows = [row for row in rows if query in self.search_text(row)]
            self.filter_cache = (cache_key, rows)
            return rows

        @staticmethod
        def row_choice_key(row):
            """Key of a row in the message selection (None for CAN errors, they are not selectable)."""
            if row.is_env:
                return 'ev:%s' % row.name
            if row.is_debug:
                return 'dbg:%s' % row.name
            if row.is_error:
                return None
            return 'id:%d' % row.id

        @staticmethod
        def search_text(row):
            if row.is_env:
                return ('%s ev' % row.name).casefold()
            if row.is_debug:
                return ('%s dbg t32 trace32 %s' % (row.name, row.value)).casefold()
            if row.is_error:
                return ('err error can error %s %s' % (row.text, row.channel)).casefold()
            return ('%s %x %s' % (row.name, row.id, row.channel)).casefold()

        def _rows_until(self, rows, position):
            """The rows (sorted by time) at or before the position."""
            if self._row_times is None or self._row_times[0] is not rows:
                self._row_times = (rows, [row.time for row in rows])
            return rows[:bisect.bisect_right(self._row_times[1], position)]

        def set_position(self, position, clip=False):
            """Time position of the Panel Viewer in seconds (None: none). While playing (clip) the trace table
            follows it to the last page and the graph shows the curves only up to it. Otherwise (time slider
            moved) nothing is hidden: the graph cursor moves there and the trace table jumps to that time."""
            clip = bool(clip and position is not None)
            if (position, clip) == (self.position, self.position_clips):
                return
            self.position, self.position_clips = position, clip
            if self._position_job is None:
                self._position_job = self.root.after(100, self._apply_position)

        def _apply_position(self):
            self._position_job = None
            if self.live():
                return
            position = self.position
            chronological = self.mode.get() == 'chronological'
            jump = None
            if position is not None:
                self.cursor_time = position  # the cursor of the graph (without reporting it back)
                if chronological and self.position_clips:
                    self.trace_page = 10 ** 9  # render_trace limits it to the last page
                elif chronological:
                    rows = self.filtered_frames()
                    if rows:
                        self._rows_until(rows, position)  # fills the times
                        jump = min(bisect.bisect_left(self._row_times[1], position), len(rows) - 1)
                        self.trace_page = jump // PAGE_SIZE
            self.render_trace()
            if position is not None and chronological and self.trace_tree is not None:
                if jump is None:
                    self.trace_tree.yview_moveto(1.0)
                else:
                    key = self.row_key(self._row_times[0][jump], jump)
                    if self.trace_tree.exists(key):
                        self.trace_tree.see(key)
                        self.trace_tree.selection_set(key)
                        self._center_row(key)
            self.schedule_plot()

        def _center_row(self, key):
            """Scroll the table so that the row lies in the middle of the visible area (as far as possible)."""
            tree = self.trace_tree
            tree.update_idletasks()
            box = tree.bbox(key)
            rows = tree.get_children()
            if not box or not box[3] or key not in rows:
                return
            visible = tree.winfo_height() / float(box[3]) - 1  # minus the header
            first = rows.index(key) - visible / 2.0 + 0.5
            tree.yview_moveto(max(0.0, first / len(rows)))

        def _clip_series(self, series):
            """Copy of a series up to the position of the playing."""
            count = bisect.bisect_right(series.xs, self.position)
            if count >= len(series.xs):
                return series
            clipped = Series(series.key, series.name, series.message, series.unit)
            clipped.xs, clipped.ys, clipped.status = series.xs[:count], series.ys[:count], series.status[:count]
            return clipped

        def row_key(self, frame, index):
            mode = self.mode.get()
            if frame.is_env:
                return '%s:ev:%s:%d' % (mode, frame.name, frame.index if mode == 'chronological' else index)
            if frame.is_debug:
                return '%s:dbg:%s:%d' % (mode, frame.name, frame.index if mode == 'chronological' else index)
            if frame.is_error:
                return '%s:err:%s:%d' % (mode, frame.channel, frame.index if mode == 'chronological' else index)
            return '%s:%s:%d:%d' % (mode, frame.channel, frame.id,
                                    frame.line if mode == 'chronological' else index)

        def render_trace(self):
            tree = self.trace_tree
            if tree is None:  # no trace window
                return
            tree.delete(*tree.get_children())
            self.row_frames = {}
            self.signal_rows = {}
            overview = self.mode.get() == 'overview'
            filtered = self.filtered_frames()
            if self.position is not None and self.position_clips and not overview and not self.live():
                filtered = self._rows_until(filtered, self.position)  # playing: only up to the position
            total = len(filtered)
            # frames or messages, without the rows of environment variables and errors
            counted = sum(1 for row in filtered if not row.is_env and not row.is_error and not row.is_debug)
            if not self.frames and self.environment:  # signals only: count their values (or variables)
                set_text(self.trace_count, '%s Variablen' if overview else '%s Werte', de_int(total))
            elif self.channel is None:
                set_text(self.trace_count, '%s Nachrichten' if overview else '%s Frames', de_int(counted))
            else:
                set_text(self.trace_count, '%s Nachrichten · Kanal %s' if overview else '%s Frames · Kanal %s',
                         de_int(counted), self.channel)
            tree.heading('count', text=tr('Anzahl') if overview else '')
            page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            self.trace_page = max(0, min(self.trace_page, page_count - 1))
            first = self.trace_page * PAGE_SIZE
            visible = filtered[first:first + PAGE_SIZE]
            self.page_label.set('%s–%s / %s' % (de_int(first + 1), de_int(first + len(visible)), de_int(total))
                                if total else '0 / 0')
            self.prev_button.state(['disabled'] if self.trace_page == 0 else ['!disabled'])
            self.next_button.state(['disabled'] if self.trace_page >= page_count - 1 else ['!disabled'])
            for offset, frame in enumerate(visible):
                key = self.row_key(frame, first + offset)
                time_text, relative = self.time_texts(frame)
                if frame.is_env:
                    skey = env_key(frame.name)
                    self.signal_rows[key] = skey  # click in the column "Grafik" / double click: graph on/off
                    tree.insert('', tk.END, iid=key, text=time_text, tags=('environment',), values=(
                        relative, '', 'EV', frame.name, '',
                        env_value_text(self.db_environment.get(frame.name), frame.value),
                        frame.count if overview else '', CHECKED if skey in self.selected else UNCHECKED,
                        description_text((self.db_environment.get(frame.name) or {}).get('description'))))
                    continue
                if frame.is_debug:
                    tree.insert('', tk.END, iid=key, text=time_text, tags=('debugger',), values=(
                        relative, '', 'T32', frame.name, '', frame.value, frame.count if overview else '', '', ''))
                    continue
                if frame.is_error:
                    tree.insert('', tk.END, iid=key, text=time_text, tags=('error',), values=(
                        relative, frame.channel, 'ERR', frame.name, '', frame.text,
                        frame.count if overview else '', '', ''))
                    continue
                self.row_frames[key] = frame
                tree.insert('', tk.END, iid=key, text=time_text, values=(
                    relative, frame.channel, '0x%X' % frame.id, frame.name, len(frame.data),
                    ' '.join('%02X' % byte for byte in frame.data), frame.count if overview else '', '',
                    description_text(frame.message.get('description') if frame.message else '')),
                    tags=() if frame.message else ('unknown',))
                if frame.message is not None and frame.message['signals']:
                    if key in self.expanded:
                        self.fill_signal_rows(key)
                        tree.item(key, open=True)
                    else:
                        tree.insert(key, tk.END, iid=key + '|placeholder', text='')
            if not total:
                tree.insert('', tk.END, iid='__empty__', text='', values=(
                    '', '', '', tr('Keine passenden Trace-Nachrichten.'), '', '', '', '', ''), tags=('unknown',))

        def fill_signal_rows(self, key):
            tree = self.trace_tree
            frame = self.row_frames[key]
            tree.delete(*tree.get_children(key))
            for index, signal in enumerate(frame.message['signals']):
                skey = signal_key(frame.message, signal)
                decoded = self.decoder(signal).decode(frame)
                unit = signal.get('unit') or ''
                value = SignalDecoder.text(decoded)
                iid = '%s|%d' % (key, index)
                self.signal_rows[iid] = skey
                tree.insert(key, tk.END, iid=iid, text='', tags=('signal',), values=(
                    '', '', 'Bit %s|%s' % (signal.get('start_bit', ''), signal.get('length', '')), signal['name'], '',
                    value + (' ' + unit if unit and decoded is not None else '') + self.decoder(signal).hex_text(decoded), '',
                    CHECKED if skey in self.selected else UNCHECKED, description_text(signal.get('description'))))

        def on_trace_open(self, event):
            key = self.trace_tree.focus()
            if key in self.row_frames:
                self.fill_signal_rows(key)
                self.expanded.add(key)

        def on_trace_close(self, event):
            key = self.trace_tree.focus()
            if key in self.expanded:
                self.expanded.discard(key)

        def _column_at(self, x):
            """Name of the trace table column at x ('' for the tree column)."""
            column = self.trace_tree.identify_column(x)
            if not column or column == '#0':
                return ''
            try:
                return self.trace_tree.column(column, 'id')
            except tk.TclError:
                return ''

        def on_trace_click(self, event):
            tree = self.trace_tree
            row = tree.identify_row(event.y)
            if row in self.signal_rows and self._column_at(event.x) == 'graph':
                tree.selection_set(row)
                self.toggle_signal(self.signal_rows[row])
                return 'break'
            return None

        def on_trace_double_click(self, event):
            tree = self.trace_tree
            row = tree.identify_row(event.y)
            if row in self.signal_rows:
                if self._column_at(event.x) != 'graph':
                    self.toggle_signal(self.signal_rows[row])
                return 'break'
            return None

        def on_trace_right_click(self, event):
            tree = self.trace_tree
            row = tree.identify_row(event.y)
            frame = self.row_frames.get(row) or self.row_frames.get(tree.parent(row)) if row else None
            if row:
                tree.selection_set(row)
            menu = tk.Menu(tree, tearoff=False)
            menu.add_command(label=tr('In Zwischenablage kopieren'), command=self.copy_trace_to_clipboard,
                             state='normal' if not self.busy and (self.frames or any(self._all_environment.values())) else 'disabled')
            if frame is not None and self.on_generator_add is not None:
                menu.add_command(label=tr('Zum Generator hinzufügen'),
                                 command=lambda: self.on_generator_add(frame.id, list(frame.data)))
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        def on_trace_space(self, event):
            row = self.trace_tree.focus()
            if row in self.signal_rows:
                self.toggle_signal(self.signal_rows[row])
            elif row in self.row_frames and self.trace_tree.get_children(row):
                self.trace_tree.item(row, open=not self.trace_tree.item(row, 'open'))
                if self.trace_tree.item(row, 'open'):
                    self.on_trace_open(event)
                else:
                    self.on_trace_close(event)
            return 'break'

        def on_trace_filter(self):
            if self._filter_job is not None:
                self.root.after_cancel(self._filter_job)

            def apply():
                self._filter_job = None
                self.trace_page = 0
                self.render_trace()
            self._filter_job = self.root.after(250, apply)

        def _apply_time_columns(self):
            """Absolute time on: it takes the tree column (leftmost), the relative time moves to 'reltime'.
            The column 'description' is only shown with the button "Beschreibung"."""
            tree = self.trace_tree
            if tree is None:
                return
            columns = [column[0] for column in TRACE_COLUMNS]
            if not self.show_description.get():
                columns.remove('description')
            # The free width goes to the description while it is shown, otherwise to the data.
            tree.column('data', stretch=not self.show_description.get())
            tree.column('description', stretch=self.show_description.get())
            if self.absolute_time.get():
                tree.heading('#0', text=tr('Uhrzeit'), anchor=tk.W)
                tree.column('#0', width=ABSOLUTE_WIDTH)
                tree.configure(displaycolumns=columns)
            else:
                tree.heading('#0', text=tr('Zeit [s]'), anchor=tk.W)
                tree.column('#0', width=RELATIVE_WIDTH)
                tree.configure(displaycolumns=[name for name in columns if name != 'reltime'])
            self._distribute_columns()

        def _distribute_columns(self):
            """The free width goes to the column 'data', with the description to 'description'. Treeview only
            distributes it when the widget is resized, so it is set here when columns are shown or hidden."""
            tree = self.trace_tree
            if tree is None or tree.winfo_width() < 50:
                return
            shown = list(tree['displaycolumns'])
            stretch = 'description' if 'description' in shown else 'data'
            base = dict((column[0], column[2]) for column in TRACE_COLUMNS)
            if stretch == 'description':
                tree.column('data', width=DATA_WIDTH_WITH_DESCRIPTION)
            fixed = int(tree.column('#0', 'width')) + sum(
                int(tree.column(name, 'width')) for name in shown if name != stretch)
            minimum = 120 if stretch == 'description' else base['data']
            tree.column(stretch, width=max(minimum, tree.winfo_width() - fixed - 20))

        def on_absolute_time(self):
            self._apply_time_columns()
            self.render_trace()
            if self.absolute_time.get() and self.frames and self.trace_start is None:
                self.set_footer('Der Trace enthält keine Startzeit, die absolute Zeit ist unbekannt.')

        def time_texts(self, frame):
            """(text of the tree column, text of the column 'reltime') of a frame."""
            relative = '%.6f' % frame.time
            if self.absolute_time.get():
                return absolute_time_text(self.trace_start, frame.time), relative
            return relative, ''

        def on_mode(self):
            self.trace_page = 0
            self.render_trace()

        def change_page(self, step):
            self.trace_page += step
            self.render_trace()
            if self.trace_tree is not None:
                self.trace_tree.yview_moveto(0)

        # ------------------------------------------------------------ signal selection

        def open_picker(self):
            """Open the signal selection window (or bring it to the front)."""
            if self.picker_window is not None:
                self.picker_window.deiconify()
                self.picker_window.lift()
                self.picker_window.focus_force()
                return
            window = self.picker_window = tk.Toplevel(self.root)
            apply_appearance(window)
            window.title(tr('CAN Graph Viewer – Signalauswahl'))
            window.geometry(initial_geometry(window, {'geometry': self.settings.get('picker_geometry')},
                                             '420x640'))
            window.minsize(300, 300)
            theme = Theme(window)
            theme.header(window, tr('Signalauswahl'), tr('Signale für die Grafik auswählen'))
            theme.footer(window, self.picker_count)
            body = tk.Frame(window, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            pane, _ = theme.pane(body, tr('Signale'))
            pane.pack(fill=tk.BOTH, expand=True)
            search = ttk.Frame(pane, padding=(12, 10, 12, 8))
            search.pack(side=tk.TOP, fill=tk.X)
            ttk.Label(search, text='Search').pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(search, text=tr('Zurücksetzen'), command=self.reset_signals).pack(side=tk.RIGHT, padx=(8, 0))
            entry = ttk.Entry(search, textvariable=self.signal_filter)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            list_frame = ttk.Frame(pane, padding=(4, 0, 0, 4))
            list_frame.pack(fill=tk.BOTH, expand=True)
            # Groups (messages, digital/analog I/O, environment variables) as parents; a signal row shows its
            # check mark in front of the name.
            tree = self.picker_tree = ttk.Treeview(list_frame, columns=('unit',), show='tree headings',
                                                   selectmode='none')
            tree.heading('#0', text='Signal', anchor=tk.W)
            tree.heading('unit', text=tr('Einheit'), anchor=tk.W)
            tree.column('#0', width=300, minwidth=120, stretch=True)
            tree.column('unit', width=70, minwidth=40, stretch=False)
            tree.tag_configure('empty', foreground=C.MUTED)
            tree.tag_configure('group', font=theme.font_heading)
            tree.bind('<<TreeviewOpen>>', lambda event: self._picker_group_toggled(False))
            tree.bind('<<TreeviewClose>>', lambda event: self._picker_group_toggled(True))
            scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            tree.bind('<Button-1>', self.on_picker_click)
            window.protocol('WM_DELETE_WINDOW', self.close_picker)
            window.bind('<Escape>', lambda event: self.close_picker())
            if self.attach_menu is not None:
                self.attach_menu(window)
            self.render_signal_options()
            entry.focus_set()

        def close_picker(self):
            if self.picker_window is None:
                return
            self._remember_picker_geometry()
            window, self.picker_window, self.picker_tree = self.picker_window, None, None
            window.destroy()

        def _remember_picker_geometry(self):
            if self.picker_window is not None:
                try:
                    self.settings['picker_geometry'] = window_geometry(self.picker_window)
                except tk.TclError:
                    pass

        def _update_picker_count(self):
            total = len(self.trace_signals)
            shown = sum(1 for key, _ in self.trace_signals if key in self.selected)
            set_text(self.picker_count, '%s von %s Signalen in der Grafik', lambda: de_int(shown),
                     lambda: de_int(total))

        def render_signal_options(self):
            tree = self.picker_tree
            if tree is None:
                return
            self._update_picker_count()
            tree.delete(*tree.get_children())
            self.picker_names = {}
            if not self.trace_signals:
                text = tr('Keine passenden Datenbasis-Signale im Trace gefunden.' if self.frames
                          else 'Lade Trace und Datenbasis.')
                tree.insert('', tk.END, iid='__empty__', text=text, values=('',), tags=('empty',))
                return
            query = self.signal_filter.get().strip().casefold()
            count = 0
            for group, title, members in self.signal_groups:
                shown = [member for member in members
                         if not query or query in ('%s %s' % (member[0], tr(title))).casefold()]
                if not shown:
                    continue
                # The search opens all groups with hits; otherwise groups start closed and stay as the user left them.
                tree.insert('', tk.END, iid=group, text=tr(title), values=('',), tags=('group',),
                            open=bool(query) or group in self.picker_open)
                for key, name, unit in shown:
                    self.picker_names[key] = name
                    tree.insert(group, tk.END, iid=key, text=self._picker_text(key), values=(unit,))
                    count += 1
            if not count:
                tree.insert('', tk.END, iid='__empty__', text=tr('Keine Treffer.'), values=('',), tags=('empty',))

        def _picker_text(self, key):
            return '%s  %s' % (CHECKED if key in self.selected else UNCHECKED, self.picker_names.get(key, key))

        def _picker_group_toggled(self, closed):
            tree = self.picker_tree
            group = tree.focus() if tree is not None else ''
            if not group or group not in dict((item[0], 1) for item in self.signal_groups):
                return
            if self.signal_filter.get().strip():
                return  # opened by the search, not by the user
            if closed:
                self.picker_open.discard(group)
            else:
                self.picker_open.add(group)

        # ------------------------------------------------------------ message selection

        def open_filter_picker(self):
            """Open the message selection window (or bring it to the front)."""
            if self.filter_window is not None:
                self.filter_window.deiconify()
                self.filter_window.lift()
                self.filter_window.focus_force()
                return
            window = self.filter_window = tk.Toplevel(self.root)
            apply_appearance(window)
            window.title(tr('CAN Trace Viewer – Nachrichtenauswahl'))
            window.geometry(initial_geometry(window, {'geometry': self.settings.get('filter_geometry')}, '420x560'))
            window.minsize(300, 300)
            theme = Theme(window)
            theme.header(window, tr('Nachrichtenauswahl'), tr('Nachrichten für die Trace-Tabelle auswählen'))
            theme.footer(window, self.filter_count)
            body = tk.Frame(window, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            pane, _ = theme.pane(body, tr('Nachrichten'))
            pane.pack(fill=tk.BOTH, expand=True)
            search = ttk.Frame(pane, padding=(12, 10, 12, 8))
            search.pack(side=tk.TOP, fill=tk.X)
            ttk.Label(search, text='Search').pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(search, text=tr('Keine'), command=lambda: self.set_all_choices(False)).pack(
                side=tk.RIGHT, padx=(6, 0))
            ttk.Button(search, text=tr('Alle'), command=lambda: self.set_all_choices(True)).pack(
                side=tk.RIGHT, padx=(8, 0))
            entry = ttk.Entry(search, textvariable=self.filter_search)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            list_frame = ttk.Frame(pane, padding=(4, 0, 0, 4))
            list_frame.pack(fill=tk.BOTH, expand=True)
            tree = self.filter_tree = ttk.Treeview(list_frame, show='tree', selectmode='none')
            tree.column('#0', width=300, minwidth=120, stretch=True)
            tree.tag_configure('empty', foreground=C.MUTED)
            tree.tag_configure('group', font=theme.font_heading)
            scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            tree.bind('<Button-1>', self.on_filter_click)
            window.protocol('WM_DELETE_WINDOW', self.close_filter_picker)
            window.bind('<Escape>', lambda event: self.close_filter_picker())
            if self.attach_menu is not None:
                self.attach_menu(window)
            self.render_filter_options()
            entry.focus_set()

        def close_filter_picker(self):
            if self.filter_window is None:
                return
            self._remember_filter_geometry()
            window, self.filter_window, self.filter_tree = self.filter_window, None, None
            window.destroy()

        def _remember_filter_geometry(self):
            if self.filter_window is not None:
                try:
                    self.settings['filter_geometry'] = window_geometry(self.filter_window)
                except tk.TclError:
                    pass

        def filter_options(self):
            """[(group title, [(key, text)])]: the messages and variables of the trace."""
            names = {}
            for frame in self.visible_frames:
                names.setdefault(frame.id, frame.name)
            messages = [('id:%d' % identifier, '%s  (0x%X)' % (name, identifier))
                        for identifier, name in sorted(names.items(), key=lambda item: natural_key(item[1]))]
            variables = [('ev:%s' % name, name) for name in sorted(self.environment, key=natural_key)]
            return [('Nachrichten', messages), ('Umgebungsvariablen', variables)]

        def render_filter_options(self):
            tree = self.filter_tree
            if tree is None:
                return
            tree.delete(*tree.get_children())
            self.filter_names = {}
            groups = self.filter_options()
            total = sum(len(items) for _, items in groups)
            shown = sum(1 for _, items in groups for key, _ in items if key not in self.hidden)
            set_text(self.filter_count, '%s von %s Nachrichten angezeigt', lambda: de_int(shown),
                     lambda: de_int(total))
            if not total:
                tree.insert('', tk.END, iid='__empty__', tags=('empty',),
                            text=tr('Lade Trace und Datenbasis.'))
                return
            query = self.filter_search.get().strip().casefold()
            count = 0
            for index, (title, items) in enumerate(groups):
                found = [(key, text) for key, text in items if not query or query in text.casefold()]
                if not found:
                    continue
                tree.insert('', tk.END, iid='group:%d' % index, text=tr(title), tags=('group',), open=True)
                for key, text in found:
                    self.filter_names[key] = text
                    tree.insert('group:%d' % index, tk.END, iid=key, text=self._filter_text(key))
                    count += 1
            if not count:
                tree.insert('', tk.END, iid='__empty__', text=tr('Keine Treffer.'), tags=('empty',))

        def _filter_text(self, key):
            return '%s  %s' % (UNCHECKED if key in self.hidden else CHECKED, self.filter_names.get(key, key))

        def on_filter_click(self, event):
            tree = self.filter_tree
            row = tree.identify_row(event.y)
            if row in self.filter_names:
                self.set_choice(row, row in self.hidden)
            return 'break'

        def set_choice(self, key, visible):
            if visible:
                self.hidden.discard(key)
            else:
                self.hidden.add(key)
            self._choices_changed()

        def set_all_choices(self, visible):
            """All messages shown (or hidden); with a search only the found ones."""
            keys = list(self.filter_names) if self.filter_window is not None and self.filter_search.get().strip() \
                else [key for _, items in self.filter_options() for key, _ in items]
            for key in keys:
                if visible:
                    self.hidden.discard(key)
                else:
                    self.hidden.add(key)
            self._choices_changed()

        def _choices_changed(self):
            self.filter_cache = None
            self.trace_page = 0
            self.render_trace()
            self.trace_tree.yview_moveto(0)
            self.render_filter_options()

        def reset_signals(self):
            """Remove all signals from the graph."""
            for key in list(self.selected):
                self.set_signal_visibility(key, False)

        def on_picker_click(self, event):
            tree = self.picker_tree
            if tree.identify_region(event.x, event.y) == 'heading':
                return None
            row = tree.identify_row(event.y)
            if row in self.picker_names:
                self.toggle_signal(row)
                return 'break'
            if row and tree.get_children(row):  # group: open/close with a click anywhere in its row
                tree.focus(row)
                tree.item(row, open=not tree.item(row, 'open'))
                self._picker_group_toggled(not tree.item(row, 'open'))
                return 'break'
            return 'break'

        def toggle_signal(self, key):
            self.set_signal_visibility(key, key not in self.selected)

        def set_signal_visibility(self, key, visible):
            if visible and key not in self.selected:
                self.selected.append(key)
            elif not visible and key in self.selected:
                self.selected.remove(key)
            mark = CHECKED if visible else UNCHECKED
            for iid, skey in self.signal_rows.items():
                if skey == key and self.trace_tree is not None and self.trace_tree.exists(iid):
                    self.trace_tree.set(iid, 'graph', mark)
            if self.picker_tree is not None and self.picker_tree.exists(key):
                self.picker_tree.item(key, text=self._picker_text(key))
            if self.picker_tree is not None:
                self._update_picker_count()
            self.schedule_plot()

        # ------------------------------------------------------------ plot

        def theme_changed(self):
            """Redraw theme-dependent colors."""
            self._render_debug_status()
            self.schedule_plot()

        def visible_series(self):
            result = []
            for key in self.selected:
                series = self.series(key)
                if series is not None and series.xs:
                    result.append(series)
            return result

        def fit_x(self):
            times = [value for series in self.visible_series() for value in (series.xs[0], series.xs[-1])]
            if not times and self.visible_frames:
                frames = self.visible_frames
                times = [min(frame.time for frame in frames), max(frame.time for frame in frames)]
            if times:
                self.x_range = [min(times), max(times)]
                if self.x_range[0] == self.x_range[1]:
                    self.x_range[1] += 1
                if self.live():
                    self._live_range = list(self.x_range)  # the graph follows the recording again
            self.schedule_plot()

        def fit_y(self):
            self.y_range = None
            self.schedule_plot()

        def reset_zoom(self):
            self.x_range = self.y_range = None
            self.schedule_plot()

        def schedule_plot(self):
            if self._plot_job is None and self.plot is not None:
                self._plot_job = self.root.after_idle(self.render_plot)

        @staticmethod
        def fit_text(font, text, width):
            if width <= 0:
                return ''
            if font.measure(text) <= width:
                return text
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if font.measure(text[:middle] + '…') <= width:
                    low = middle
                else:
                    high = middle - 1
            return text[:low] + '…' if low else ''

        def render_plot(self):
            self._plot_job = None
            canvas = self.plot
            if canvas is None:  # no graph window
                return
            self._update_cursor_info()  # line below the graph follows every change of the cursors
            canvas.delete('all')
            self.plot_bounds = None
            width, view_height = canvas.winfo_width(), canvas.winfo_height()
            if width < 50 or view_height < 20:
                return
            series_list = self.visible_series()
            height = max(120, view_height, len(series_list) * 100)
            canvas.configure(scrollregion=(0, 0, width, height))
            if not series_list:
                canvas.create_text(width / 2, max(view_height / 2, 60), fill=C.MUTED, font=self.font_empty,
                                   text=tr('Über „Signalauswahl“ Signale auswählen, um den Verlauf anzuzeigen' if self.frames
                                           else 'Trace und Datenbasis laden'))
                return
            left, right, top, bottom = 132, 18, 8, 30
            plot_width, plot_height = width - left - right, height - top - bottom
            if plot_width < 20:
                return
            full_x = [min(s.xs[0] for s in series_list), max(s.xs[-1] for s in series_list)]
            if full_x[0] == full_x[1]:
                full_x[1] = full_x[0] + 1
            x0, x1 = self.x_range or full_x
            span = (x1 - x0) or 1.0
            if self.position is not None and self.position_clips and not self.live():  # playing: only up to it
                series_list = [self._clip_series(series) for series in series_list]

            def px(value):
                return left + (value - x0) / span * plot_width
            cursor_x = (px(self.cursor_time) if self.cursor_time is not None and x0 <= self.cursor_time <= x1
                        else None)
            second = self.second_cursor.get() and self.cursor2_time is not None
            cursor2_x = px(self.cursor2_time) if second and x0 <= self.cursor2_time <= x1 else None
            y_window = (self.y_range if self.y_range and 0 <= self.y_range[0] < self.y_range[1] <= 1
                        else [0.0, 1.0])
            lane_height = plot_height / len(series_list)

            for step in range(6):
                xx = left + step * plot_width / 5
                canvas.create_line(xx, top, xx, height - bottom, fill=C.GRID)
                canvas.create_text(xx, height - bottom + 16, text=fmt(x0 + step * span / 5, 6), anchor=tk.S,
                                   fill=C.MUTED, font=self.font_axis)
            canvas.create_text(left + plot_width / 2, height - 3, text=tr('Zeit [s]'), anchor=tk.S, fill=C.MUTED,
                               font=self.font_axis)
            if cursor_x is not None:
                canvas.create_line(cursor_x, top, cursor_x, height - bottom, fill=C.DANGER, width=1.5, dash=(5, 4))
            if cursor2_x is not None:
                canvas.create_line(cursor2_x, top, cursor2_x, height - bottom, fill=C.WARNING, width=1.5,
                                   dash=(5, 4))

            lanes = []
            for index, series in enumerate(series_list):
                color = C.SERIES_COLORS[index % len(C.SERIES_COLORS)]
                lane_top = top + index * lane_height
                lane_bottom = lane_top + lane_height
                lanes.append((lane_top, lane_bottom))
                i0 = bisect.bisect_left(series.xs, x0)
                i1 = bisect.bisect_right(series.xs, x1)
                # The sample before the visible range sets the value at the left edge (step display).
                start = max(0, i0 - 1)
                ys = series.ys[start:i1]
                low, high = (min(ys), max(ys)) if ys else (0.0, 1.0)
                if low == high:
                    low, high = low - 1, high + 1
                padding = (high - low) * 0.08
                low, high = low - padding, high + padding
                vy0 = low + (high - low) * y_window[0]
                vy1 = low + (high - low) * y_window[1]
                vspan = (vy1 - vy0) or 1.0

                def py(value, lane_top=lane_top, vy1=vy1, vspan=vspan):
                    return lane_top + (vy1 - value) / vspan * lane_height

                # value axis
                labels = {}
                has_status = False
                for value, status in zip(ys, series.status[start:i1]):
                    if vy0 <= value <= vy1:
                        if status:
                            has_status = True
                            labels[value] = status
                        elif value not in labels:
                            labels[value] = ''
                ticks = sorted(labels.items())
                discrete = has_status or len(ticks) <= 5
                if len(ticks) > 5:
                    ticks = [ticks[int(step * (len(ticks) - 1) / 4 + 0.5)] for step in range(5)]
                if discrete and ticks:
                    for value, label in ticks:
                        yy = py(value)
                        canvas.create_line(left, yy, width - right, yy, fill=C.GRID)
                        canvas.create_text(left - 6, max(lane_top + 10, min(lane_bottom - 2, yy + 3)), anchor=tk.SE,
                                           text=self.fit_text(self.font_axis, label or fmt(value), left - 14),
                                           fill=C.MUTED, font=self.font_axis)
                else:
                    for tick, yy in enumerate((lane_top, lane_top + lane_height / 2, lane_bottom)):
                        canvas.create_line(left, yy, width - right, yy, fill=C.GRID)
                        value = vy1 - tick * vspan / 2
                        canvas.create_text(left - 6, yy + (10 if tick == 0 else -3 if tick == 2 else 3),
                                           anchor=tk.SE, text=fmt(value), fill=C.MUTED, font=self.font_axis)
                label = '%s%s%s' % (series.message + '.' if series.message else '', series.name,
                                    ' [%s]' % series.unit if series.unit else '')
                canvas.create_text(left + 6, lane_top + 14, anchor=tk.SW, fill=color, font=self.font_label,
                                   text=self.fit_text(self.font_label, label, plot_width - 12))

                self._draw_series(canvas, series, start, i1, px, py, vy0, vy1, plot_width, color, x0, x1)

                unit = ' ' + series.unit if series.unit else ''
                marks = []  # (x, sample index, outline color, extra text)
                if cursor_x is not None and i1 > start:
                    # value that holds at the cursor: the last sample at or before it
                    marks.append((cursor_x, max(start, bisect.bisect_right(series.xs, self.cursor_time, start, i1) - 1),
                                  C.DANGER, ''))
                if cursor2_x is not None and i1 > start:
                    k2 = max(start, bisect.bisect_right(series.xs, self.cursor2_time, start, i1) - 1)
                    k1 = self.value_at(series, self.cursor_time)
                    extra = ''
                    if k1 is not None:  # difference to the value at the 1st cursor (also outside the view)
                        diff = series.ys[k2] - series.ys[k1]
                        extra = ' (Δ %s%s%s)' % ('+' if diff > 0 else '', fmt(diff), unit)
                    marks.append((cursor2_x, k2, C.WARNING, extra))
                for mark_x, k, outline, extra in marks:
                    value = series.ys[k]
                    if vy0 <= value <= vy1:
                        cursor_y = py(value)
                        canvas.create_oval(mark_x - 4, cursor_y - 4, mark_x + 4, cursor_y + 4,
                                           fill=C.SURFACE, outline=outline, width=1.5)
                        text = (series.status[k] or '%s%s' % (fmt(value), unit)) + extra
                        text = self.fit_text(self.font_label, text, max(1, plot_width - 16))
                        text_width = self.font_label.measure(text)
                        label_x = mark_x + 7
                        if label_x + text_width + 6 > width - right:
                            label_x = mark_x - text_width - 10
                        label_x = max(left + 2, min(label_x, width - right - text_width - 4))
                        label_y = max(lane_top + 13, min(lane_bottom - 4, cursor_y + 3))
                        canvas.create_rectangle(label_x, label_y - 12, label_x + text_width + 6, label_y + 2,
                                                fill=C.SURFACE, outline='')
                        canvas.create_text(label_x + 3, label_y, anchor=tk.SW, text=text, fill=color,
                                           font=self.font_label)

            self.plot_bounds = {'left': left, 'top': top, 'plot_width': plot_width, 'plot_height': plot_height,
                                'x': (x0, x1), 'full_x': full_x, 'lanes': lanes, 'y_window': list(y_window)}

        @staticmethod
        def _draw_series(canvas, series, i0, i1, px, py, vy0, vy1, plot_width, color, x0, x1):
            """Draw the series as steps: a value holds until the next sample, the line only goes up or
            down where the value changes. Dense data is reduced to first/min/max/last per pixel column."""
            dense = (i1 - i0) > 3 * plot_width
            xs, ys = series.xs, series.ys
            segment = []
            segments = [segment]
            bucket = None  # [column, first, low, high, last] with (x, y) pixel tuples

            def add(point):
                if segment and segment[-1] != point[1]:
                    segment.extend((point[0], segment[-1]))  # hold the previous value up to this sample
                segment.extend(point)

            def flush():
                if bucket is not None:
                    _, first, low_point, high_point, last_point = bucket
                    for point in (first, low_point, high_point, last_point):
                        add(point)
            for i in range(i0, i1):
                value = ys[i]
                if not vy0 <= value <= vy1:
                    if dense:
                        flush()
                        bucket = None
                    segment = []
                    segments.append(segment)
                    continue
                point = (px(max(xs[i], x0)), py(value))
                if not dense:
                    add(point)
                    continue
                column = int(point[0])
                if bucket is None or bucket[0] != column:
                    flush()
                    bucket = [column, point, point, point, point]
                else:
                    if point[1] < bucket[2][1]:
                        bucket[2] = point
                    if point[1] > bucket[3][1]:
                        bucket[3] = point
                    bucket[4] = point
            if dense:
                flush()
            if segment and i1 < len(xs):
                segment.extend((px(x1), segment[-1]))  # the last value holds until the next sample
            for coords in segments:
                if len(coords) >= 4:
                    canvas.create_line(*coords, fill=color, width=1.5)

        def lane_at(self, y):
            bounds = self.plot_bounds
            count = len(bounds['lanes'])
            lane = int((y - bounds['top']) // (bounds['plot_height'] / count))
            return max(0, min(count - 1, lane))

        def on_plot_press(self, event):
            if not self.plot_bounds:
                return
            x, y = self.plot.canvasx(event.x), self.plot.canvasy(event.y)
            self.drag = {'x0': x, 'y0': y, 'x1': x, 'y1': y, 'lane': self.lane_at(y), 'rect': None}

        def on_plot_drag(self, event):
            if not self.drag:
                return
            drag = self.drag
            drag['x1'], drag['y1'] = self.plot.canvasx(event.x), self.plot.canvasy(event.y)
            bounds = self.plot_bounds
            if self.zoom_axis.get() == 'y':  # band across the plot within the lane
                lane_top, lane_bottom = bounds['lanes'][drag['lane']]
                coords = (bounds['left'], max(lane_top, min(drag['y0'], lane_bottom)),
                          bounds['left'] + bounds['plot_width'], max(lane_top, min(drag['y1'], lane_bottom)))
            else:  # band over the full height
                coords = (drag['x0'], bounds['top'], drag['x1'], bounds['top'] + bounds['plot_height'])
            if drag['rect'] is None:
                drag['rect'] = self.plot.create_rectangle(*coords, outline=C.ACCENT, dash=(3, 2), width=1)
            else:
                self.plot.coords(drag['rect'], *coords)

        def on_plot_release(self, event):
            drag, bounds = self.drag, self.plot_bounds
            self.drag = None
            if not drag or not bounds:
                return
            if drag['rect'] is not None:
                self.plot.delete(drag['rect'])
            x_lo, x_hi = sorted((drag['x0'], drag['x1']))
            y_lo, y_hi = sorted((drag['y0'], drag['y1']))
            left, plot_width = bounds['left'], bounds['plot_width']
            x0, x1 = bounds['x']
            zoom_x = self.zoom_axis.get() != 'y'  # dragging zooms only the selected axis
            if (x_hi - x_lo <= 8) if zoom_x else (y_hi - y_lo <= 8):
                if (left <= drag['x0'] <= left + plot_width
                        and bounds['top'] <= drag['y0'] <= bounds['top'] + bounds['plot_height']):
                    time = x0 + (drag['x0'] - left) / plot_width * (x1 - x0)
                    # With the 2nd cursor a click moves the nearer one of the two.
                    if (self.second_cursor.get() and self.cursor2_time is not None and self.cursor_time is not None
                            and abs(time - self.cursor2_time) < abs(time - self.cursor_time)):
                        self.set_cursor2(time)
                    else:
                        self.cursor_time = time
                        self._update_cursor_info()
                        self.schedule_plot()
                        if self.on_cursor_changed is not None:
                            self.on_cursor_changed(self.cursor_time)
                return
            if zoom_x:
                self.x_range = [x0 + (x_lo - left) / plot_width * (x1 - x0),
                                x0 + (x_hi - left) / plot_width * (x1 - x0)]
            else:
                lane_top, lane_bottom = bounds['lanes'][drag['lane']]
                w0, w1 = bounds['y_window']
                lower = w0 + (lane_bottom - min(y_hi, lane_bottom)) / (lane_bottom - lane_top) * (w1 - w0)
                upper = w0 + (lane_bottom - max(y_lo, lane_top)) / (lane_bottom - lane_top) * (w1 - w0)
                if upper > lower:
                    self.y_range = [max(0.0, lower), min(1.0, upper)]
            self.schedule_plot()

        def on_plot_right_click(self, event):
            """Right click (macOS also Control+click): set the 2nd cursor there and switch it on."""
            bounds = self.plot_bounds
            if not bounds:
                return 'break'
            x = self.plot.canvasx(event.x)
            if not bounds['left'] <= x <= bounds['left'] + bounds['plot_width']:
                return 'break'
            x0, x1 = bounds['x']
            self.second_cursor.set(True)
            self.set_cursor2(x0 + (x - bounds['left']) / bounds['plot_width'] * (x1 - x0))
            return 'break'

        def set_cursor2(self, time):
            self.cursor2_time = time
            self._update_cursor_info()
            self.schedule_plot()

        def on_second_cursor(self):
            """Button "2. Cursor": on places it next to the 1st cursor (both inside the visible range)."""
            if self.second_cursor.get():
                bounds = self.plot_bounds
                x0, x1 = bounds['x'] if bounds else (self.x_range or (0.0, 1.0))
                span = (x1 - x0) or 1.0
                if self.cursor_time is None or not x0 <= self.cursor_time <= x1:
                    self.cursor_time = x0 + span / 3
                    if self.on_cursor_changed is not None:
                        self.on_cursor_changed(self.cursor_time)
                if self.cursor2_time is None or not x0 <= self.cursor2_time <= x1:
                    self.cursor2_time = min(x1, self.cursor_time + span / 5)
                    if self.cursor2_time == self.cursor_time:
                        self.cursor2_time = max(x0, self.cursor_time - span / 5)
            self._update_cursor_info()
            self.schedule_plot()

        def _update_cursor_info(self):
            """Line below the graph: time of the cursor, with the 2nd cursor both times and Δt."""
            label = self.cursor_info_label
            if label is None:
                return
            first, second = self.cursor_time, self.cursor2_time
            if first is not None and self.second_cursor.get() and second is not None:
                set_text(self.cursor_info, 'Cursor 1: %s s · Cursor 2: %s s · Δt = %s s', '%.6f' % first,
                         '%.6f' % second, '%+.6f' % (second - first))
            elif first is not None:
                set_text(self.cursor_info, 'Cursor: %s s', '%.6f' % first)
            else:
                if label.winfo_manager():
                    label.pack_forget()
                return
            if not label.winfo_manager():
                label.pack(side=tk.BOTTOM, fill=tk.X, before=self.plot.master)

        @staticmethod
        def value_at(series, time):
            """Index of the sample that holds at time (last one at or before it) or None."""
            if time is None or not series.xs:
                return None
            k = bisect.bisect_right(series.xs, time) - 1
            return k if k >= 0 else None

        def on_plot_wheel(self, event):
            if event.delta:
                self.zoom_plot(event, -0.15 if event.delta > 0 else 0.15)

        def on_plot_touchpad(self, event):
            try:
                dx, dy = (float(value) for value in
                          self.root.tk.splitlist(self.root.tk.call('tk::PreciseScrollDeltas', event.delta)))
            except (tk.TclError, ValueError):
                return
            delta = dy if dy else dx
            if delta:
                self.zoom_plot(event, max(-0.15, min(0.15, -delta * 0.01)))

        def zoom_plot(self, event, amount):
            """amount > 0 zooms out, < 0 zooms in. Zooms the axis selected with "Zoom X"/"Zoom Y", with Shift
            the other one."""
            bounds = self.plot_bounds
            if not bounds:
                return
            factor = math.exp(amount)
            x, y = self.plot.canvasx(event.x), self.plot.canvasy(event.y)
            change_y = (self.zoom_axis.get() == 'y') != bool(event.state & 0x1)
            if change_y:
                axis = bounds['y_window']
                lane_top, lane_bottom = bounds['lanes'][self.lane_at(y)]
                anchor = axis[1] - (y - lane_top) / (lane_bottom - lane_top) * (axis[1] - axis[0])
            else:
                axis = self.x_range or bounds['full_x']
                anchor = axis[0] + (x - bounds['left']) / bounds['plot_width'] * (axis[1] - axis[0])
            new = [anchor - (anchor - axis[0]) * factor, anchor + (axis[1] - anchor) * factor]
            if new[1] - new[0] <= 0:
                return
            if change_y:
                self.y_range = [max(0.0, new[0]), min(1.0, new[1])]
                if self.y_range[1] <= self.y_range[0]:
                    self.y_range = list(axis)
            else:
                self.x_range = new
            self.schedule_plot()


if tk is not None:

    class SectionWindow(object):
        """One window of the TraceViewerApp (section 'trace' or 'graph') as a window of the analyzer."""

        def __init__(self, engine, section):
            self.engine, self.section = engine, section

        def window_settings(self):
            return self.engine.window_settings(self.section)

        def theme_changed(self):
            self.engine.theme_changed()

        def language_changed(self):
            self.engine.language_changed()

        def close(self):
            """Close the window; the last one ends the viewer."""
            host = self.engine.hosts.get(self.section)
            self.engine.remove_section(self.section)
            if host is not None:
                host.destroy()



def run_gui(database_path=None, trace_path=None):
    if tk is None:
        print(tr('Fehler: tkinter ist in dieser Python-Installation nicht verfügbar.'), file=sys.stderr)
        return 1
    root = tk.Tk()
    apply_appearance(root)
    app = TraceViewerApp(root, database_path, trace_path)
    graph = tk.Toplevel(root)
    apply_appearance(graph)
    app.add_section('graph', graph)
    graph.protocol('WM_DELETE_WINDOW', lambda: app.remove_section('graph') or graph.destroy())
    root.mainloop()
    return 0


def main(argv=None):
    load_language()
    parser = argparse.ArgumentParser(description=tr('CAN-Traces mit einer CAN-Datenbasis analysieren.'))
    parser.add_argument('trace', nargs='?', help=tr('Trace-Datei (ASC, TRC, LOG, TXT), die beim Start geladen wird'))
    parser.add_argument('-d', '--database', help=tr('CAN-Datenbasis (DBC, ARXML, HTML), die beim Start geladen wird'))
    args = parser.parse_args(argv)
    return run_gui(args.database, args.trace)


if __name__ == '__main__':
    sys.exit(main())
