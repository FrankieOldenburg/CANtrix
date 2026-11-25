#!/usr/bin/env python3
"""CAN Panel Viewer.

Liest Vector-Panels (.xvp, XML aus dem CANalyzer/CANoe Panel Designer) und zeigt sie als GUI.
Die Elemente sind mit Signalen der Datenbasis oder mit Umgebungsvariablen verknüpft
(SymbolConfiguration). Ihre Werte kommen entweder aus dem geladenen CAN-Trace zu einem wählbaren
Zeitpunkt (Zeitschieber, Abspielen oder Cursor des Trace Viewers) oder live aus dem Trace Recorder
(zuletzt empfangener Wert jedes Signals).

Unterstützte Elemente: GroupBox, Meter (Zeigerinstrument), NumericUpDown/TextBox (Zahlenanzeige),
ComboBox (Wertetabelle), LED (Farben aus SwitchValuesVTXml), ProgressBar/TrackBar, StaticText.
Andere Elemente erscheinen als Platzhalter mit ihrem Typ und, wenn verknüpft, ihrem Wert.

Start: python3 CAN_Panel.py [PANEL] [-d DATENBASIS] [-t TRACE] (normalerweise über CANtrix.py)
Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import argparse
import bisect
import math
import os
import re
import struct
import sys
import time
import xml.etree.ElementTree as ET

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    tk = None

from CAN_Common import C, Theme, apply_appearance, database_id, initial_geometry, normalize_id, rounded_rect, \
    window_geometry
from CAN_Language import load_language, set_text, tr
from CAN_TraceViewer import DASH, Frame, SignalDecoder, de_int, fmt, read_database, read_trace

PANEL_TYPES = ('.xvp',)
MAX_PANEL_SIZE = 20 * 1024 * 1024
MARGIN = 0  # space around the panel on the canvas (the panel starts at the edge)
MAX_SCALE = 1.5  # the layout grows (up to this factor) until all labels fit
SPEEDS = ('0.5×', '1×', '2×', '5×', '10×', '100×', '1000×', '∞')  # ∞ = unendlich: sofort bis zum Ende
DEFAULT_SPEED = '100×'
PLAY_INTERVAL = 40  # ms between two frames while playing

# Panel Designer type (last part of the .NET type name) -> kind of element drawn here
CONTROL_KINDS = {
    'GroupBoxControl': 'group',
    'MeterControl': 'meter', 'AnalogGaugeControl': 'meter',
    'NumericUpDownControl': 'numeric', 'TextBoxControl': 'numeric', 'InputOutputBoxControl': 'numeric',
    'ComboBoxControl': 'combo',
    'LEDControl': 'led',
    'ProgressBarControl': 'progress', 'TrackBarControl': 'progress',
    'StaticTextControl': 'text', 'LabelControl': 'text',
    'ButtonControl': 'button', 'PanelButtonControl': 'button',
    'CheckBoxControl': 'check', 'RadioButtonControl': 'radio',
    'PictureBoxControl': 'picture',
    'VectorTabControl': 'tab', 'VectorTabPage': 'page',
}

# .NET colour names whose X11 meaning (used by Tk) differs; all other names are passed to Tk.
NET_COLORS = {
    'gray': '#808080', 'grey': '#808080', 'green': '#008000', 'maroon': '#800000', 'purple': '#800080',
    'darkgray': '#a9a9a9', 'darkgrey': '#a9a9a9', 'lightgray': '#d3d3d3', 'lightgrey': '#d3d3d3',
}


# ---------------------------------------------------------------- parsing

def _pair(text, default):
    """'15, 25' -> (15, 25)."""
    numbers = re.findall(r'-?\d+', text or '')
    return (int(numbers[0]), int(numbers[1])) if len(numbers) >= 2 else default


def _number(text, default=None):
    try:
        value = float(str(text).strip().replace(',', '.'))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def parse_symbol(text):
    """SymbolConfiguration: '5;2;db;;Message;Signal;…' (2 = signal) or '5;16;db;;;Variable;…'
    (16 = environment variable). Returns None without a binding."""
    fields = (text or '').split(';')
    if len(fields) < 6 or not fields[5].strip():
        return None
    kind = {'2': 'signal', '16': 'env'}.get(fields[1].strip(), 'other')
    return {'kind': kind, 'database': fields[2], 'message': fields[4].strip(), 'name': fields[5].strip()}


def parse_switch_values(props):
    """LED states as a sorted list [(lower value, colour name)] from SwitchValuesVTXml."""
    text = props.get('SwitchValuesVTXml') or ''
    states = []
    if text.strip():
        try:
            root = ET.fromstring('<states>%s</states>' % text)
        except ET.ParseError:
            root = None
        if root is not None:
            value = None
            for element in root:
                if element.tag == 'RxValue':
                    value = _number(element.text)
                elif element.tag == 'Color' and value is not None:
                    states.append((value, (element.text or '').strip()))
                    value = None
    return sorted(states, key=lambda state: state[0])


def _parse_object(element, origin, depth, controls, tabs=()):
    type_name = element.get('Type', '').split(',')[0].rsplit('.', 1)[-1]
    props = {}
    for child in element:
        if child.tag == 'Property' and child.get('Name'):
            props[child.get('Name')] = child.text or ''
    x, y = _pair(props.get('Location'), (0, 0))
    width, height = _pair(props.get('Size'), (100, 23))
    x, y = origin[0] + x, origin[1] + y
    control = {
        'type': type_name, 'kind': CONTROL_KINDS.get(type_name, 'other'),
        'name': props.get('Name') or element.get('Name') or type_name,
        'title': element.get('ControlName') or '', 'x': x, 'y': y, 'w': width, 'h': height, 'depth': depth,
        'props': props, 'symbol': parse_symbol(props.get('SymbolConfiguration')),
        'tabs': tabs,  # ((tab control name, page number), ...): shown only while all these pages are selected
    }
    if control['kind'] == 'led':
        control['states'] = parse_switch_values(props)
    if control['kind'] == 'tab':
        control['pages'] = []
    controls.append(control)
    for child in element:
        if child.tag == 'Object':
            child_type = child.get('Type', '').split(',')[0].rsplit('.', 1)[-1]
            if control['kind'] == 'tab' and CONTROL_KINDS.get(child_type) == 'page':
                number = len(control['pages'])
                title = ''
                for prop in child:
                    if prop.tag == 'Property' and prop.get('Name') == 'Text':
                        title = prop.text or ''
                control['pages'].append(title or child.get('ControlName') or str(number + 1))
                _parse_object(child, (x, y), depth + 1, controls, tabs + ((control['name'], number),))
            else:
                _parse_object(child, (x, y), depth + 1, controls, tabs)


def parse_panel(data, source_name):
    """Vector panel (.xvp). Returns {'title', 'width', 'height', 'controls'}; controls are in drawing
    order (a group before its children) with absolute positions in panel pixels."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError(tr('"%s" ist keine gültige Panel-Datei (%s).') % (source_name, error))
    panel = root if root.tag == 'Object' else root.find('Object')
    if panel is None:
        raise ValueError(tr('In "%s" wurde kein Panel gefunden.') % source_name)
    props = dict((child.get('Name'), child.text or '') for child in panel if child.tag == 'Property')
    controls = []
    for child in panel:
        if child.tag == 'Object':
            _parse_object(child, (0, 0), 0, controls)
    if not controls:
        raise ValueError(tr('Das Panel "%s" enthält keine Elemente.') % source_name)
    width, height = _pair(props.get('Size'), (0, 0))
    width = max([width] + [control['x'] + control['w'] for control in controls])
    height = max([height] + [control['y'] + control['h'] for control in controls])
    title = panel.get('ControlName') or props.get('Name') or os.path.splitext(source_name)[0]
    return {'title': title, 'width': width, 'height': height, 'controls': controls}


def decode_bmp(data):
    """Uncompressed Windows BMP (1, 4, 8, 24, 32 bit) -> (width, height, [rows of (r, g, b) tuples], top row first)."""
    if data[:2] != b'BM' or len(data) < 54:
        raise ValueError('no BMP')
    offset, header = struct.unpack('<II', data[10:18])
    width, height, planes, bits, compression = struct.unpack('<iiHHI', data[18:34])
    if compression not in (0, 3) or bits not in (1, 4, 8, 24, 32) or width <= 0 or height == 0:
        raise ValueError('unsupported BMP')
    top_down = height < 0
    height = abs(height)
    palette = []
    if bits <= 8:
        count = struct.unpack('<I', data[46:50])[0] or (1 << bits)
        start = 14 + header
        palette = [(data[start + 4 * i + 2], data[start + 4 * i + 1], data[start + 4 * i])
                   for i in range(count) if start + 4 * i + 3 < len(data)]
    stride = ((width * bits + 31) // 32) * 4
    rows = []
    for row in range(height):
        base = offset + (row if top_down else height - 1 - row) * stride
        line = data[base:base + stride]
        pixels = []
        for x in range(width):
            if bits == 24:
                pixels.append((line[3 * x + 2], line[3 * x + 1], line[3 * x]))
            elif bits == 32:
                pixels.append((line[4 * x + 2], line[4 * x + 1], line[4 * x]))
            else:
                per = 8 // bits
                value = (line[x // per] >> (8 - bits * (x % per + 1))) & ((1 << bits) - 1)
                pixels.append(palette[value] if value < len(palette) else (0, 0, 0))
        rows.append(pixels)
    return width, height, rows


def bmp_ppm(data, width, height):
    """PPM data of a BMP scaled (nearest neighbour) to width x height pixels, for tk.PhotoImage."""
    source_w, source_h, rows = decode_bmp(data)
    width, height = max(1, int(width)), max(1, int(height))
    out = bytearray(b'P6 %d %d 255\n' % (width, height))
    for y in range(height):
        row = rows[min(source_h - 1, y * source_h // height)]
        for x in range(width):
            out.extend(row[min(source_w - 1, x * source_w // width)])
    return bytes(out)


def read_panel(path, progress=None):
    """Read a panel file (for CANtrix.py; progress is not needed, panels are small)."""
    name = os.path.basename(path)
    if os.path.splitext(name)[1].lower() not in PANEL_TYPES:
        raise ValueError(tr('Bitte eine Panel-Datei (.xvp) wählen.'))
    if os.path.getsize(path) > MAX_PANEL_SIZE:
        raise ValueError(tr('Die Panel-Datei ist größer als 20 MB.'))
    with open(path, 'rb') as handle:
        panel = parse_panel(handle.read(), name)
    panel.update({'name': name, 'path': path})
    return panel


def bound_count(panel):
    return sum(1 for control in panel['controls'] if control['symbol'])


# ---------------------------------------------------------------- values over time

class ValueSource(object):
    """Values of one signal or environment variable over time: times, numbers (or None) and texts
    from the value table. reason: why there are no values (not in the database / trace)."""
    __slots__ = ('label', 'unit', 'times', 'values', 'texts', 'reason')

    def __init__(self, label, unit=''):
        self.label, self.unit = label, unit
        self.times, self.values, self.texts = [], [], []
        self.reason = ''

    def at(self, moment):
        """(value, text) of the last sample at or before moment; None before the first sample."""
        index = bisect.bisect_right(self.times, moment) - 1
        if index < 0:
            return None
        return self.values[index], self.texts[index]


def messages_by_name(messages, channel=None):
    """Message per name. With several databases the one of the selected CAN channel wins, then one for all
    channels, then the first."""
    ranked = {}
    for order, message in enumerate(messages):
        own = message.get('channel')
        rank = 0 if channel is not None and own == channel else 1 if not own else 2
        best = ranked.get(message['name'])
        if best is None or (rank, order) < best[0]:
            ranked[message['name']] = ((rank, order), message)
    return dict((name, item[1]) for name, item in ranked.items())


def build_sources(controls, database, trace, channel):
    """One ValueSource per bound control (index -> ValueSource)."""
    messages = (database or {}).get('messages') or []
    environment = (database or {}).get('environment') or {}
    by_name = messages_by_name(messages, channel)
    frames_by_id = {}
    for frame in (trace or {}).get('frames') or []:
        if channel is None or frame.channel == channel:
            frames_by_id.setdefault(normalize_id(frame.id), []).append(frame)
    trace_env = (trace or {}).get('environment') or {}
    cache, sources = {}, {}
    for index, control in enumerate(controls):
        symbol = control['symbol']
        if not symbol:
            continue
        key = (symbol['kind'], symbol['message'], symbol['name'])
        if key not in cache:
            cache[key] = _signal_source(symbol, by_name, frames_by_id, trace) if symbol['kind'] == 'signal' \
                else _env_source(symbol, environment, trace_env, trace)
        sources[index] = cache[key]
    return sources


def _signal_source(symbol, by_name, frames_by_id, trace):
    source = ValueSource('%s.%s' % (symbol['message'], symbol['name']))
    message = by_name.get(symbol['message'])
    signal = next((item for item in message['signals'] if item['name'] == symbol['name']), None) \
        if message else None
    if signal is None:
        source.reason = tr('nicht in der Datenbasis')
        return source
    source.unit = signal.get('unit') or ''
    decoder = SignalDecoder(signal)
    own_channel = message.get('channel')  # database assigned to one CAN channel: only its frames
    for frame in frames_by_id.get(database_id(message), ()):
        if own_channel and frame.channel != own_channel:
            continue
        decoded = decoder.decode(frame)
        if decoded is not None and math.isfinite(decoded[1]):
            source.times.append(frame.time)
            source.values.append(decoded[1])
            source.texts.append(decoded[2])
    _sort(source)
    if not source.times:
        source.reason = tr('nicht im Trace') if trace else tr('kein Trace geladen')
    return source


def _env_source(symbol, environment, trace_env, trace):
    source = ValueSource(tr('%s (Umgebungsvariable)') % symbol['name'])
    variable = environment.get(symbol['name']) or {}
    source.unit = variable.get('unit') or ''
    names = variable.get('values') or {}
    for moment, text in trace_env.get(symbol['name'], ()):
        value = _number(text)
        source.times.append(moment)
        source.values.append(value)
        source.texts.append(names.get(value, '') if value is not None else text)
    _sort(source)
    if not source.times:
        source.reason = tr('nicht im Trace') if trace else tr('kein Trace geladen')
    return source


def live_decoders(controls, database):
    """Decoders for live frames: {CAN ID: [(control index, SignalDecoder), ...]} for all bound signals."""
    by_name = messages_by_name((database or {}).get('messages') or [])
    decoders, cache = {}, {}
    for index, control in enumerate(controls):
        symbol = control['symbol']
        if not symbol or symbol['kind'] != 'signal':
            continue
        message = by_name.get(symbol['message'])
        signal = next((item for item in message['signals'] if item['name'] == symbol['name']), None) \
            if message else None
        identifier = database_id(message) if signal is not None else None
        if identifier is None:
            continue
        if id(signal) not in cache:
            cache[id(signal)] = SignalDecoder(signal)
        decoders.setdefault(identifier, []).append((index, cache[id(signal)]))
    return decoders


def live_frame(message):
    """Frame (as in a trace) from a python-can message of the Trace Recorder."""
    return Frame(message.timestamp, str(getattr(message, 'channel', '') or ''), 'Rx', message.arbitration_id, '',
                 bytes(message.data), 0)


def _sort(source):
    times = source.times
    if any(times[i] > times[i + 1] for i in range(len(times) - 1)):
        order = sorted(range(len(times)), key=times.__getitem__)
        source.times = [times[i] for i in order]
        source.values = [source.values[i] for i in order]
        source.texts = [source.texts[i] for i in order]


def format_value(value, decimals=None):
    if value is None:
        return DASH
    if decimals is not None:
        return '%.*f' % (decimals, value)
    return fmt(value)


# ---------------------------------------------------------------- GUI

if tk is not None:

    class PanelViewerApp(object):
        def __init__(self, root, settings=None, sources=None, channel=None, always_fit=False):
            """root: Tk or Toplevel. settings: window settings (see window_settings). sources: loaded files
            {'panel', 'database', 'trace'} (from CANtrix.py or main). channel: CAN channel ('1', …)
            or None for all channels."""
            self.root = root
            self.settings = dict(settings or {})
            self.panel = self.database = self.trace = None
            self.channel = channel
            self.sources = {}  # control index -> ValueSource
            self.live_map = {}  # CAN ID -> [(control index, SignalDecoder)], see live_decoders
            self.live_values = {}  # control index -> (value, text) last received live
            self.live_env_values = {}  # environment variable name -> last live sample
            self.live_count = 0  # live frames received since the recorder started
            self.scale = 1.0
            self.boxes = []  # (x0, y0, x1, y1, control index) on the canvas, for the mouse
            self.hover = None
            self.always_fit = always_fit  # best window size at every start and every loaded panel (ignores the saved one)
            self.tab_pages = {}  # tab control name -> selected page number
            self.images = {}  # (file, width, height) -> PhotoImage or None
            self.t0 = self.t1 = 0.0
            self.moment = 0.0
            self._play_job = self._render_job = None
            self.on_playing_changed = None  # callback(): playing started or stopped (buttons in the main window)
            self._play_last = None
            self.on_time_changed = None  # callback(): time or range changed (time slider in the main window)
            self.attach_menu = None  # callback(window), set by CANtrix.py

            self.theme = Theme(root)
            self.footer = tk.StringVar()
            set_text(self.footer, 'Bereit')
            self.subtitle = tk.StringVar()
            self.file_var = tk.StringVar()
            self.time_var = tk.StringVar(value=DASH)
            # Time text, speed and "follow the cursor" live in the main window (CANtrix.py replaces these).
            self.speed_var = tk.StringVar(value=DEFAULT_SPEED)
            self.follow_var = tk.BooleanVar(value=True)
            self.mode = tk.StringVar(value='trace')  # 'trace' or 'live', chosen in the main window (set_source)

            root.title('CAN Panel Viewer')
            root.minsize(520, 360)
            self._build_layout()
            root.protocol('WM_DELETE_WINDOW', self.close)
            if isinstance(root, tk.Tk) and root.tk.call('tk', 'windowingsystem') == 'aqua':
                root.createcommand('tk::mac::Quit', self.close)
            root.bind('<space>', lambda event: self.toggle_play())
            self.set_sources(sources or {})
            self._apply_mode()
            if self.always_fit:
                root.geometry(self.fitting_geometry())
                if getattr(root, 'docked', False):
                    root.after_idle(self.fit_window)
            else:
                root.geometry(initial_geometry(root, self.settings, self.fitting_geometry()))
            self._ready = True  # later panels resize the window (fit_window)

        def fitting_geometry(self):
            """Window size that shows the whole panel (as far as the screen allows)."""
            if not self.panel:
                return '980x620'
            self.root.update_idletasks()
            extra_h = self.root.winfo_reqheight() - self.canvas.winfo_reqheight()
            # Card padding, borders and scroll bar beside the canvas; the toolbar may need more width.
            _, _, panel_w, panel_h = self.extent()
            width = max(int(panel_w * self.scale + 2 * MARGIN + 50), self.root.winfo_reqwidth())
            height = int(panel_h * self.scale + 2 * MARGIN + extra_h)
            return '%dx%d' % (min(max(width, 760), self.root.winfo_screenwidth() - 40),
                              min(height, self.root.winfo_screenheight() - 80))

        # ------------------------------------------------------------ layout

        def _build_layout(self):
            theme = self.theme
            # Docked: the title bar of the pane shows the panel name (_set_window_title), so no label here.
            theme.header(self.root, 'CAN Panel Viewer',
                         subtitle_variable=None if getattr(self.root, 'docked', False) else self.subtitle)
            theme.footer(self.root, self.footer)
            # Card without a title row (the window title already names the panel).
            pane = tk.Frame(self.root, background=C.SURFACE, highlightthickness=1, highlightbackground=C.LINE,
                            highlightcolor=C.LINE)
            pane.pack(fill=tk.BOTH, expand=True)

            area = ttk.Frame(pane)
            area.pack(fill=tk.BOTH, expand=True)
            canvas = self.canvas = tk.Canvas(area, background=C.SURFACE, highlightthickness=0)
            vscroll = ttk.Scrollbar(area, orient=tk.VERTICAL, command=canvas.yview)
            hscroll = ttk.Scrollbar(area, orient=tk.HORIZONTAL, command=canvas.xview)
            canvas.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
            canvas.grid(row=0, column=0, sticky='nsew')
            vscroll.grid(row=0, column=1, sticky='ns')
            hscroll.grid(row=1, column=0, sticky='ew')
            area.rowconfigure(0, weight=1)
            area.columnconfigure(0, weight=1)
            canvas.bind('<Configure>', lambda event: self.schedule_render())
            canvas.bind('<Motion>', self.on_motion)
            canvas.bind('<Button-1>', self.on_click)
            canvas.bind('<Leave>', lambda event: self.set_hover(None))

        # ------------------------------------------------------------ data

        def set_sources(self, result):
            """Use loaded files: {'panel', 'database', 'trace'} (keys are optional)."""
            if 'panel' in result:
                self.panel = result['panel']
                self.tab_pages = {}
                self.scale = self._fit_scale()
                self.live_values = {}
            if 'database' in result:
                self.database = result['database']
            if 'trace' in result:
                self.trace = result['trace']
            self.rebuild(reset_time='trace' in result)
            if 'panel' in result and self.panel and getattr(self, '_ready', False):
                self.root.after_idle(self.fit_window)

        def fit_window(self):
            """Resize the window (or the pane when docked) so that the loaded panel fits."""
            if not self.panel:
                return
            self.root.update_idletasks()
            width, height = [int(value) for value in self.fitting_geometry().split('+')[0].split('x')]
            pane = getattr(self.root, 'pane', None)
            if getattr(self.root, 'docked', False) and pane is not None:
                x, y, old_w, old_h = pane.geo
                pane.set_geometry(x, y, width + pane.winfo_width() - self.root.winfo_width(),
                                  height + pane.winfo_height() - self.root.winfo_height())
            else:
                self.root.geometry('%dx%d' % (width, height))

        def set_database(self, database):
            self.set_sources({'database': database})

        def set_channel(self, channel):
            if channel != self.channel:
                self.channel = channel
                self.rebuild()

        def rebuild(self, reset_time=False):
            controls = self.panel['controls'] if self.panel else []
            self.sources = build_sources(controls, self.database, self.trace, self.channel)
            self.live_map = live_decoders(controls, self.database)
            times = [source.times[k] for source in self.sources.values() if source.times for k in (0, -1)]
            frames = (self.trace or {}).get('frames') or []
            if frames:
                times.extend((frames[0].time, frames[-1].time))
            self.t0, self.t1 = (min(times), max(times)) if times else (0.0, 0.0)
            if reset_time:
                self.stop()
                self.moment = self.t0
            self.set_time(min(max(self.moment, self.t0), self.t1))
            if self.panel:
                self.subtitle.set(self.panel['title'])
                self.file_var.set(self.panel['name'])
                self._set_window_title(self.panel['title'])
            else:
                set_text(self.subtitle, 'Kein Panel geladen')
                self.file_var.set('')
                self._set_window_title(tr('Kein Panel geladen'))
            self.show_summary()

        def show_summary(self):
            if not self.panel:
                set_text(self.footer, 'Bereit')
                return
            bound = len(self.sources)
            count = len(self.panel['controls'])
            if self.live():
                with_values = sum(self.sample(index) is not None for index in self.sources)
                if not self.live_count and not self.live_env_values:
                    set_text(self.footer, '%s Elemente · %s verknüpft · noch keine Live-Daten – Live-Aufzeichnung starten',
                             lambda: de_int(count), lambda: de_int(bound))
                    return
                template = '%s Elemente · %s verknüpft · %s mit Live-Werten'
            else:
                with_values = sum(1 for source in self.sources.values() if source.times)
                template = '%s Elemente · %s verknüpft · %s mit Werten im Trace'
            set_text(self.footer, template, lambda: de_int(count), lambda: de_int(bound), lambda: de_int(with_values))

        # ------------------------------------------------------------ live values (Trace Recorder)

        def live(self):
            return self.mode.get() == 'live'

        def set_source(self, value):
            """'trace' or 'live': where the values come from (the switch is in the main window)."""
            self.mode.set('live' if value == 'live' else 'trace')
            self._apply_mode()

        def _apply_mode(self):
            """Trace: time slider, playing and cursor; live: these are locked."""
            live = self.live()
            if live:
                self.stop()
            state = ['disabled'] if live else ['!disabled']
            self.show_time()
            self.render()

        def live_frames(self, messages, reset=False):
            """Messages received by the Trace Recorder (python-can), passed on by CANtrix.py.
            reset: the recording started again, forget the old values. Without a loaded trace the
            window switches to live by itself."""
            if reset:
                self.live_values = {}
                self.live_env_values.clear()
                self.live_count = 0
            for message in messages or ():
                self.live_count += 1
                targets = self.live_map.get(normalize_id(message.arbitration_id))
                if not targets:
                    continue
                frame = live_frame(message)
                for index, decoder in targets:
                    decoded = decoder.decode(frame)
                    if decoded is not None and math.isfinite(decoded[1]):
                        self.live_values[index] = (decoded[1], decoded[2])
            if self.live():
                self.show_time()
                self.schedule_render()

        def live_environment(self, samples):
            if samples:
                self.live_env_values.update(samples)
                if self.live():
                    self.show_time()
                    self.schedule_render()

        def sample(self, index):
            """(value, text) of a control at the chosen time or live; None without a value."""
            if self.live():
                symbol = self.panel['controls'][index]['symbol']
                if symbol and symbol['kind'] == 'env':
                    name = symbol['name']
                    if name in self.live_env_values:
                        text = self.live_env_values[name]
                        value = _number(text)
                        variable = ((self.database or {}).get('environment') or {}).get(name) or {}
                        return value, (variable.get('values') or {}).get(value, '') if value is not None else text
                return self.live_values.get(index)
            source = self.sources.get(index)
            return source.at(self.moment) if source is not None else None

        # ------------------------------------------------------------ time and playing

        def set_time(self, moment):
            self.moment = moment
            self.show_time()
            if self.on_time_changed is not None:
                self.on_time_changed()  # time slider in the main window
            self.render()

        def show_time(self):
            if self.live():
                count = self.live_count
                set_text(self.time_var, 'Live · %s Frames', lambda: de_int(count))
            else:
                self.time_var.set('%.3f / %.3f s' % (self.moment, self.t1) if self.t1 > self.t0 else DASH)

        def user_time(self, value):
            """Time slider of the main window moved."""
            self.stop()
            self.set_time(float(value))

        def set_cursor(self, moment):
            """Cursor of the Trace Viewer was set (CANtrix.py); followed if the box is ticked."""
            if moment is None or not self.follow_var.get() or self.live():
                return
            self.stop()
            self.set_time(min(max(moment, self.t0), self.t1))

        def speed(self):
            if self.speed_var.get() == '∞':
                return float('inf')
            return _number(self.speed_var.get().rstrip('×'), 1.0)

        def toggle_play(self):
            if self._play_job is not None:
                self.stop()
            else:
                self.play()

        def playing(self):
            return self._play_job is not None

        def can_play(self):
            return self.t1 > self.t0 and not self.live()

        def play(self):
            """Play the trace from its beginning (Start in the main window, space bar)."""
            if self._play_job is not None or not self.can_play():
                return
            self.set_time(self.t0)  # playing always starts at the beginning
            self._play_last = time.monotonic()
            self._play_job = self.root.after(PLAY_INTERVAL, self._play_step)
            self._playing_changed()

        def _playing_changed(self):
            if self.on_playing_changed is not None:
                self.on_playing_changed()

        def _play_step(self):
            now = time.monotonic()
            speed = self.speed()
            moment = float('inf') if speed == float('inf') else self.moment + (now - self._play_last) * speed
            self._play_last = now
            if moment >= self.t1:
                self._play_job = None
                self.stop()
                self.set_time(self.t1)
                return
            self.set_time(moment)
            self._play_job = self.root.after(PLAY_INTERVAL, self._play_step)

        def stop(self):
            if self._play_job is not None:
                self.root.after_cancel(self._play_job)
                self._play_job = None
            self._playing_changed()

        # ------------------------------------------------------------ drawing

        def _fit_scale(self):
            """Grow the layout until the labels of the panel fit (Tk fonts are wider than Windows fonts)."""
            scale = 1.0
            font, heading = self.theme.font_label, self.theme.font_heading
            for control in self.panel['controls']:
                props = control['props']
                if control['kind'] in ('numeric', 'combo') and props.get('DisplayLabel', 'Left') in ('Left', 'Right') \
                        and props.get('DescriptionText'):
                    room = _pair(props.get('DescriptionSize'), (control['w'] // 2, 0))[0]
                    needed = font.measure(props['DescriptionText']) + 8
                elif control['kind'] == 'text':
                    room, needed = control['w'], font.measure(props.get('Text', ''))
                elif control['kind'] == 'group':
                    room, needed = control['w'] - 24, heading.measure(props.get('Text') or control['title'])
                else:
                    continue
                if room > 0:
                    scale = max(scale, float(needed) / room)
            return min(scale, MAX_SCALE)

        def extent(self):
            """(x, y, width, height) of the area the controls cover; the panel is drawn from its top left corner."""
            controls = self.panel['controls'] if self.panel else []
            if not controls:
                return 0, 0, self.panel['width'] if self.panel else 0, self.panel['height'] if self.panel else 0
            x0 = min(control['x'] for control in controls)
            y0 = min(control['y'] for control in controls)
            x1 = max(control['x'] + control['w'] for control in controls)
            y1 = max(control['y'] + control['h'] for control in controls)
            return x0, y0, x1 - x0, y1 - y0

        def schedule_render(self):
            if self._render_job is None:
                self._render_job = self.root.after_idle(self._render_now)

        def _render_now(self):
            self._render_job = None
            self.render()

        def render(self):
            canvas = self.canvas
            canvas.delete('all')
            self.boxes = []
            width, height = canvas.winfo_width(), canvas.winfo_height()
            if not self.panel:
                canvas.configure(scrollregion=(0, 0, width, height))
                canvas.create_text(width / 2, height / 2, fill=C.MUTED, font=self.theme.font_text,
                                   text=tr('Kein Panel geladen'))
                return
            s = self.scale
            x0, y0, panel_w, panel_h = self.extent()
            panel_w, panel_h = panel_w * s, panel_h * s
            # Starts at the left and top edge (the free space of the panel file around its controls is left out).
            ox = MARGIN - x0 * s
            oy = MARGIN - y0 * s
            canvas.configure(scrollregion=(0, 0, max(width, panel_w + 2 * MARGIN), max(height, panel_h + 2 * MARGIN)))
            for index, control in enumerate(self.panel['controls']):
                if not self.is_visible(control):
                    continue
                box = (ox + control['x'] * s, oy + control['y'] * s,
                       ox + (control['x'] + control['w']) * s, oy + (control['y'] + control['h']) * s)
                self.boxes.append(box + (index,))
                source = self.sources.get(index)
                sample = self.sample(index) if source is not None else None
                draw = getattr(self, '_draw_' + control['kind'])
                draw(canvas, control, box, sample, source)
            self.show_hover()

        def is_visible(self, control):
            return all(self.tab_pages.get(name, 0) == number for name, number in control.get('tabs', ()))

        def tab_headers(self, control, box):
            """[(page number, x0, x1)] of the tab headers of a tab control (canvas coordinates)."""
            font, x, result = self.theme.font_label, box[0] + 4 * self.scale, []
            for number, title in enumerate(control['pages']):
                width = font.measure(title) + 20
                result.append((number, x, x + width))
                x += width + 4
            return result

        def _draw_tab(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            top = y0 + 22 * self.scale  # the pages start 22 px below the tab control
            rounded_rect(canvas, x0, top, x1, y1, 10, fill=C.SURFACE_ALT, outline=C.LINE)
            selected = self.tab_pages.get(control['name'], 0)
            for number, hx0, hx1 in self.tab_headers(control, box):
                active = number == selected
                rounded_rect(canvas, hx0, y0 + 2, hx1, top - 3, 8, fill=C.ACCENT if active else C.SURFACE,
                             outline=C.ACCENT if active else C.LINE)
                canvas.create_text((hx0 + hx1) / 2, (y0 + 2 + top - 3) / 2, text=control['pages'][number],
                                   fill=C.SURFACE if active else C.MUTED, font=self.theme.font_label)

        def _draw_page(self, canvas, control, box, sample, source):
            pass

        def _caption(self, canvas, control, box, left):
            text = control['props'].get('Text', '')
            canvas.create_text(left, (box[1] + box[3]) / 2, anchor=tk.W, fill=C.INK, font=self.theme.font_label,
                               text=self.fit_text(self.theme.font_label, text, box[2] - left))

        def _draw_button(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            rounded_rect(canvas, x0, y0, x1, y1, 6, fill=C.SURFACE, outline=C.LINE_STRONG)
            text = control['props'].get('Text', '')
            canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, fill=C.INK, font=self.theme.font_label,
                               text=self.fit_text(self.theme.font_label, text, x1 - x0 - 6))

        def _draw_check(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            size = min(14, y1 - y0)
            cy = (y0 + y1) / 2
            on = self._value(sample)
            rounded_rect(canvas, x0, cy - size / 2, x0 + size, cy + size / 2, 3,
                         fill=C.ACCENT if on else C.SURFACE, outline=C.ACCENT if on else C.LINE_STRONG)
            if on:
                canvas.create_line(x0 + size * 0.25, cy, x0 + size * 0.42, cy + size * 0.22, x0 + size * 0.78,
                                   cy - size * 0.25, fill=C.SURFACE, width=2)
            self._caption(canvas, control, box, x0 + size + 6)

        def _draw_radio(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            size = min(14, y1 - y0)
            cy = (y0 + y1) / 2
            value = self._value(sample)
            wanted = _number(control['props'].get('SwitchValue'))
            on = value is not None and wanted is not None and abs(value - wanted) < 1e-9
            canvas.create_oval(x0, cy - size / 2, x0 + size, cy + size / 2, fill=C.SURFACE,
                               outline=C.ACCENT if on else C.LINE_STRONG)
            if on:
                canvas.create_oval(x0 + 3.5, cy - size / 2 + 3.5, x0 + size - 3.5, cy + size / 2 - 3.5,
                                   fill=C.ACCENT, outline='')
            self._caption(canvas, control, box, x0 + size + 6)

        def _draw_picture(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            name = control['props'].get('ImageFile', '')
            image = self.picture(name, x1 - x0, y1 - y0)
            if image is not None:
                canvas.create_image(x0, y0, image=image, anchor=tk.NW)
                return
            canvas.create_rectangle(x0, y0, x1, y1, outline=C.LINE_STRONG, dash=(4, 3))
            canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, fill=C.SUBTLE, font=self.theme.font_axis,
                               text=self.fit_text(self.theme.font_axis, name, x1 - x0 - 8))

        def picture(self, name, width, height):
            """Image file of a picture box (next to the panel file; BMP, otherwise whatever Tk reads) or None."""
            key = (name, int(width), int(height))
            if key not in self.images:
                image = None
                path = os.path.join(os.path.dirname(self.panel.get('path') or ''), name.replace('\\', os.sep))
                try:
                    with open(path, 'rb') as handle:
                        data = handle.read()
                    if data[:2] == b'BM':
                        image = tk.PhotoImage(data=bmp_ppm(data, width, height), format='ppm')
                    else:
                        image = tk.PhotoImage(file=path)
                except (OSError, ValueError, tk.TclError, IndexError):
                    image = None
                self.images[key] = image
            return self.images[key]

        def on_click(self, event):
            if not self.panel:
                return
            x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
            for x0, y0, x1, y1, index in self.boxes:
                control = self.panel['controls'][index]
                if control['kind'] != 'tab' or not (x0 <= x <= x1 and y0 <= y <= y0 + 22 * self.scale):
                    continue
                for number, hx0, hx1 in self.tab_headers(control, (x0, y0, x1, y1)):
                    if hx0 <= x <= hx1:
                        self.tab_pages[control['name']] = number
                        self.render()
                        return

        def _value(self, sample):
            return sample[0] if sample is not None else None

        def _label_and_field(self, canvas, control, box):
            """Draw the description of a numeric field or selection box; returns the field rectangle."""
            props, s = control['props'], self.scale
            x0, y0, x1, y1 = box
            text = props.get('DescriptionText', '')
            position = props.get('DisplayLabel', 'Left' if text else 'Hide')
            if not text or position == 'Hide':
                return box
            room = _pair(props.get('DescriptionSize'), (control['w'] // 2, 0))[0] * s
            font = self.theme.font_label
            if position == 'Top':
                canvas.create_text(x0, y0 - 2, text=text, anchor=tk.SW, fill=C.MUTED, font=font)
                return box
            if position == 'Right':
                canvas.create_text(x1 - room + 6, (y0 + y1) / 2, text=text, anchor=tk.W, fill=C.MUTED, font=font)
                return x0, y0, x1 - room, y1
            canvas.create_text(x0, (y0 + y1) / 2, text=text, anchor=tk.W, fill=C.MUTED, font=font)
            return x0 + room, y0, x1, y1

        def _field(self, canvas, rect):
            rounded_rect(canvas, rect[0], rect[1], rect[2], rect[3], 6, fill=C.SURFACE_ALT, outline=C.LINE_STRONG)

        def _draw_group(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            rounded_rect(canvas, x0, y0, x1, y1, 10, fill=C.SURFACE_ALT, outline=C.LINE)
            title = control['props'].get('Text') or control['title']
            canvas.create_text(x0 + 12, y0 + 13, text=self.fit_text(self.theme.font_heading, title, x1 - x0 - 24),
                               anchor=tk.W, fill=C.MUTED, font=self.theme.font_heading)

        def _draw_text(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            canvas.create_text(x0, (y0 + y1) / 2, text=control['props'].get('Text', ''), anchor=tk.W, fill=C.INK,
                               font=self.theme.font_label)

        def _draw_numeric(self, canvas, control, box, sample, source):
            rect = self._label_and_field(canvas, control, box)
            self._field(canvas, rect)
            decimals = control['props'].get('DecimalPlaces')
            decimals = int(decimals) if decimals and decimals.strip().isdigit() else None
            text = format_value(self._value(sample), decimals)
            canvas.create_text(rect[2] - 8, (rect[1] + rect[3]) / 2, text=text, anchor=tk.E,
                               fill=C.INK if sample is not None else C.SUBTLE, font=self.theme.font_mono)

        def _draw_combo(self, canvas, control, box, sample, source):
            rect = self._label_and_field(canvas, control, box)
            self._field(canvas, rect)
            x0, y0, x1, y1 = rect
            cy = (y0 + y1) / 2
            canvas.create_line(x1 - 18, cy - 2, x1 - 14, cy + 2, x1 - 10, cy - 2, fill=C.MUTED, width=1.5,
                               capstyle=tk.ROUND, joinstyle=tk.ROUND)
            if sample is None:
                text = DASH
            else:
                text = sample[1] or format_value(sample[0])
            canvas.create_text(x0 + 8, cy, text=self.fit_text(self.theme.font_text, text, x1 - x0 - 34), anchor=tk.W,
                               fill=C.INK if sample is not None else C.SUBTLE, font=self.theme.font_text)

        def _draw_led(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            size = min(x1 - x0, y1 - y0)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            color = None
            value = self._value(sample)
            if value is not None:
                states = control.get('states') or [(0.0, ''), (1.0, '')]
                chosen = None
                for lower, name in states:
                    if value >= lower:
                        chosen = name
                color = self.led_color(chosen) if chosen else (C.SUCCESS if value else None)
            r = size / 2.0
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=C.SURFACE, outline=C.LINE_STRONG)
            inner = max(2, r - 3)
            canvas.create_oval(cx - inner, cy - inner, cx + inner, cy + inner, fill=color or C.LINE, outline='')

        def led_color(self, name):
            """Colour of a Panel Designer colour name ('Red', 'LimeGreen', '#ff0000', '255, 0, 0')."""
            name = (name or '').strip()
            numbers = re.findall(r'\d+', name)
            if ',' in name and len(numbers) in (3, 4):
                return '#%02x%02x%02x' % tuple(min(255, int(n)) for n in numbers[-3:])
            if name.lower() in NET_COLORS:
                return NET_COLORS[name.lower()]
            try:
                red, green, blue = self.canvas.winfo_rgb(name)
            except tk.TclError:
                return None
            return '#%02x%02x%02x' % (red // 257, green // 257, blue // 257)

        def _draw_progress(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            props = control['props']
            low, high = _number(props.get('Minimum'), 0.0), _number(props.get('Maximum'), 100.0)
            radius = min(6, (y1 - y0) / 2)
            rounded_rect(canvas, x0, y0, x1, y1, radius, fill=C.GRID, outline=C.LINE)
            value = self._value(sample)
            share = None
            if value is not None and high > low:
                share = max(0.0, min(1.0, (value - low) / (high - low)))
                if share > 0:
                    rounded_rect(canvas, x0, y0, x0 + max(2 * radius, share * (x1 - x0)), y1, radius,
                                 fill=C.ACCENT, outline='')
            if str(props.get('ShowPercentage')).lower() == 'true':
                text = '%d %%' % round(100 * share) if share is not None else DASH
                ink = C.ON_ACCENT if share is not None and share >= 0.5 else C.INK
                canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=text, fill=ink, font=self.theme.font_label)

        def _draw_meter(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            props = control['props']
            low, high = _number(props.get('ScaleMin'), 0.0), _number(props.get('ScaleMax'), 100.0)
            if high <= low:
                high = low + 1
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2 + 4
            r = min(x1 - x0, y1 - y0) / 2 - 6
            start, sweep = 225.0, 270.0  # degrees, counter-clockwise from 3 o'clock

            def angle(value):
                return math.radians(start - sweep * (min(max(value, low), high) - low) / (high - low))

            def point(value, radius):
                a = angle(value)
                return cx + radius * math.cos(a), cy - radius * math.sin(a)

            canvas.create_oval(x0 + 1, y0 + 1, x1 - 1, y1 - 1, fill=C.SURFACE, outline=C.LINE)
            track = (cx - r + 6, cy - r + 6, cx + r - 6, cy + r - 6)
            canvas.create_arc(*track, start=start - sweep, extent=sweep, style=tk.ARC, outline=C.GRID, width=6)
            value = self._value(sample)
            if value is not None:
                extent = sweep * (min(max(value, low), high) - low) / (high - low)
                if extent > 0:
                    canvas.create_arc(*track, start=start, extent=-extent, style=tk.ARC, outline=C.ACCENT, width=6)
            for spacing, length, color in ((_number(props.get('MinorTickSpacing')), 5, C.LINE_STRONG),
                                           (_number(props.get('MajorTickSpacing')), 9, C.MUTED)):
                if not spacing or spacing <= 0 or (high - low) / spacing > 200:
                    continue
                step = 0
                while low + step * spacing <= high + 1e-9:
                    tick = low + step * spacing
                    canvas.create_line(point(tick, r - 12), point(tick, r - 12 - length), fill=color, width=1.5)
                    if length == 9:
                        # Inside the ticks; wide labels move further in, so they never touch a tick.
                        label, font = fmt(tick, 6), self.theme.font_axis
                        a = angle(tick)
                        inset = (font.measure(label) / 2.0 * abs(math.cos(a))
                                 + font.metrics('linespace') / 2.0 * abs(math.sin(a)))
                        label_x, label_y = point(tick, r - 12 - length - 4 - inset)
                        canvas.create_text(label_x, label_y, text=label, fill=C.MUTED, font=font)
                    step += 1
            needle = value if value is not None else low
            canvas.create_line((cx, cy), point(needle, r - 16), fill=C.DANGER if value is not None else C.SUBTLE,
                               width=2.5, capstyle=tk.ROUND)
            canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=C.INK, outline='')
            text = format_value(value, 0 if high - low >= 100 else None)
            if value is not None and source is not None and source.unit:
                text += ' ' + source.unit
            canvas.create_text(cx, cy + r * 0.62, text=text, fill=C.INK, font=self.theme.font_title)
            description = props.get('DescriptionText', '')
            if description:
                canvas.create_text(cx, cy + r * 0.86, fill=C.MUTED, font=self.theme.font_axis,
                                   text=self.fit_text(self.theme.font_axis, description, r * 1.3))

        def _draw_other(self, canvas, control, box, sample, source):
            x0, y0, x1, y1 = box
            canvas.create_rectangle(x0, y0, x1, y1, outline=C.LINE_STRONG, dash=(4, 3))
            text = control['type'].replace('Control', '')
            if source is not None:
                text += ': ' + (sample[1] or format_value(sample[0]) if sample is not None else DASH)
            canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, fill=C.SUBTLE, font=self.theme.font_axis,
                               text=self.fit_text(self.theme.font_axis, text, x1 - x0 - 8))

        @staticmethod
        def fit_text(font, text, width):
            """Text shortened with '…' to fit width pixels."""
            if width <= 0 or font.measure(text) <= width:
                return text
            while text and font.measure(text + '…') > width:
                text = text[:-1]
            return text + '…'

        # ------------------------------------------------------------ mouse

        def on_motion(self, event):
            x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
            found = None
            for x0, y0, x1, y1, index in reversed(self.boxes):  # topmost (last drawn) first
                if x0 <= x <= x1 and y0 <= y <= y1 and self.panel['controls'][index]['kind'] not in ('group', 'tab', 'page'):
                    found = index
                    break
            self.set_hover(found)

        def set_hover(self, index):
            if index != self.hover:
                self.hover = index
                self.show_hover()

        def show_hover(self):
            """Footer: binding and value of the element under the mouse, otherwise the summary."""
            if self.hover is None or not self.panel or self.hover >= len(self.panel['controls']):
                self.show_summary()
                return
            control = self.panel['controls'][self.hover]
            source = self.sources.get(self.hover)
            if source is None:
                set_text(self.footer, '%s · nicht verknüpft', control['name'])
                return
            sample = self.sample(self.hover)
            if sample is not None:
                value = format_value(sample[0]) + (' ' + source.unit if source.unit else '')
                if sample[1]:
                    value = '%s (%s)' % (sample[1], value)
            elif self.live():
                symbol = control['symbol']
                value = (tr('nicht live verfügbar') if symbol['kind'] != 'signal'
                         else source.reason if source.reason == tr('nicht in der Datenbasis')
                         else tr('noch keine Live-Daten'))
            else:
                value = source.reason or DASH
            set_text(self.footer, '%s · %s = %s', control['name'], source.label, value)

        # ------------------------------------------------------------ analyser hooks

        def window_settings(self):
            return {'geometry': window_geometry(self.root)}

        def theme_changed(self):
            self.render()

        def _set_window_title(self, name):
            """Window title / title bar of the docked pane: "Panel Viewer - <name of the panel>"."""
            if getattr(self.root, 'docked', False):
                self.root.title('Panel Viewer - %s' % name)
            else:
                self.root.title('CAN Panel Viewer - %s' % name)

        def language_changed(self):
            self.rebuild()

        def close(self):
            self.stop()
            if self._render_job is not None:
                self.root.after_cancel(self._render_job)
            self.root.destroy()


def run_gui(panel_path=None, database_path=None, trace_path=None):
    if tk is None:
        print(tr('Fehler: tkinter ist in dieser Python-Installation nicht verfügbar.'), file=sys.stderr)
        return 1
    sources, errors = {}, []
    for kind, path, reader in (('panel', panel_path, read_panel), ('database', database_path, read_database),
                               ('trace', trace_path, read_trace)):
        if path:
            try:
                sources[kind] = reader(os.path.abspath(path))
            except (OSError, ValueError) as error:
                errors.append('%s: %s' % (os.path.basename(path), error))
    root = tk.Tk()
    apply_appearance(root)
    app = PanelViewerApp(root, sources=sources)
    if errors:
        set_text(app.footer, 'Laden fehlgeschlagen: %s', errors[0])
    root.mainloop()
    return 0


def main(argv=None):
    load_language()
    parser = argparse.ArgumentParser(description=tr('Vector-Panels (.xvp) mit Werten aus einem CAN-Trace anzeigen.'))
    parser.add_argument('panel', nargs='?', help=tr('Panel-Datei (.xvp)'))
    parser.add_argument('-d', '--database', help=tr('CAN-Datenbasis (DBC, ARXML, HTML), die beim Start geladen wird'))
    parser.add_argument('-t', '--trace', help=tr('Trace-Datei (ASC, TRC, LOG, TXT), die beim Start geladen wird'))
    args = parser.parse_args(argv)
    return run_gui(args.panel, args.database, args.trace)


if __name__ == '__main__':
    sys.exit(main())
