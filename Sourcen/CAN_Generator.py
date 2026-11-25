"""Generator: CAN-Nachrichten senden (Fenster "CAN Generator" im Analyzer).

Eine Liste von Einträgen (ID, Daten, optional Periode und Taste). Gesendet wird per Button, per Taste oder
zyklisch über die Verbindung der Aufzeichnung (Datenquelle Live, Start im Hauptfenster); der Analyzer setzt
dazu den Rückruf `on_send(identifier, data, extended)`, der einen Fehlertext oder None liefert.
Nur Standardbibliothek (Python 3.6).
"""
import re

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # pragma: no cover
    tk = None

from CAN_Common import (C, MessageIndex, Theme, database_id, initial_geometry, is_big_endian, normalize_id,
                        signal_bit_numbers, window_geometry)
from CAN_Language import set_text, tr
from CAN_TraceViewer import number_or

DEFAULT_PERIOD = 100  # ms
MIN_PERIOD = 1


# ---------------------------------------------------------------- helpers

def parse_identifier(text):
    """CAN ID from text ('0x123' or '123', hexadecimal). Returns (identifier, extended) or None."""
    text = text.strip()
    if text.lower().startswith('0x'):
        text = text[2:]
    if not re.fullmatch(r'[0-9a-fA-F]{1,8}', text):
        return None
    identifier = int(text, 16)
    if identifier > 0x1FFFFFFF:
        return None
    return identifier, identifier > 0x7FF


def parse_data(text):
    """Data bytes from hexadecimal text ('00 11 AA' or '0011AA'). Returns a list or None (invalid, > 64 bytes)."""
    digits = re.sub(r'[\s,;]|0x', '', text, flags=re.I)
    if len(digits) % 2 or not re.fullmatch(r'[0-9a-fA-F]*', digits) or len(digits) > 128:
        return None
    return [int(digits[index:index + 2], 16) for index in range(0, len(digits), 2)]


def data_text(data):
    return ' '.join('%02X' % byte for byte in data)


def signal_layout(signal):
    """(start, length, big_endian, signed, factor, offset) of a database signal or None if unusable."""
    try:
        start = int(str(signal.get('start_bit', '')).strip())
        length = int(str(signal.get('length', '')).strip())
    except ValueError:
        return None
    if start < 0 or not 1 <= length <= 64:
        return None
    return (start, length, is_big_endian(signal), signal.get('signedness') == 'signed',
            number_or(signal.get('factor'), 1.0), number_or(signal.get('offset'), 0.0))


def bit_plan(start, length, big_endian):
    """[(byte, bit, shift)]: where bit `shift` of the raw value lies."""
    bits = signal_bit_numbers(start, length, big_endian)
    if big_endian:
        return [(bit // 8, bit % 8, length - 1 - index) for index, bit in enumerate(bits)]
    return [(bit // 8, bit % 8, index) for index, bit in enumerate(bits)]


def signal_bytes(signal):
    """Set of the data byte numbers a database signal occupies (empty if unusable)."""
    layout = signal_layout(signal)
    if layout is None:
        return set()
    return set(entry[0] for entry in bit_plan(layout[0], layout[1], layout[2]))


def parse_value(text):
    """Number of an input field: decimal (comma allowed) or hexadecimal with the prefix 0x; ValueError if invalid."""
    text = text.strip().split(' – ')[0].strip()  # "value – name" of the value table
    sign = -1 if text.startswith('-') else 1
    body = text.lstrip('+-').strip()
    if body.lower().startswith('0x'):
        return sign * int(body[2:], 16)
    return float(text.replace(',', '.'))


def decode_signal(signal, data):
    """Physical value of a signal in the data bytes, None if the data is too short or the signal unusable."""
    layout = signal_layout(signal)
    if layout is None:
        return None
    start, length, big_endian, signed, factor, offset = layout
    plan = bit_plan(start, length, big_endian)
    if max(entry[0] for entry in plan) >= len(data):
        return None
    raw = 0
    for byte, bit, shift in plan:
        raw |= ((data[byte] >> bit) & 1) << shift
    if signed and raw >> (length - 1):
        raw -= 1 << length
    return raw * factor + offset


def encode_signal(signal, data, physical):
    """Write the physical value of a signal into the data bytes (a list, extended if too short).
    Returns False if the signal is unusable."""
    layout = signal_layout(signal)
    if layout is None:
        return False
    start, length, big_endian, signed, factor, offset = layout
    raw = int(round((physical - offset) / (factor or 1.0)))
    low, high = (-(1 << (length - 1)), (1 << (length - 1)) - 1) if signed else (0, (1 << length) - 1)
    raw = max(low, min(high, raw)) & ((1 << length) - 1)
    plan = bit_plan(start, length, big_endian)
    while len(data) <= max(entry[0] for entry in plan):
        data.append(0)
    for byte, bit, shift in plan:
        if (raw >> shift) & 1:
            data[byte] |= 1 << bit
        else:
            data[byte] &= ~(1 << bit) & 0xFF
    return True


def normalize_entries(entries):
    """Entries of a configuration: [{'id', 'data', 'period', 'key'}] (invalid ones are dropped)."""
    result = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get('id')
        data = parse_data(str(entry.get('data') or ''))
        if not isinstance(identifier, int) or isinstance(identifier, bool) or not 0 <= identifier <= 0x1FFFFFFF \
                or data is None:
            continue
        period = entry.get('period')
        period = period if isinstance(period, int) and not isinstance(period, bool) and period >= MIN_PERIOD \
            else DEFAULT_PERIOD
        result.append({'id': identifier, 'data': data_text(data), 'period': period,
                       'key': str(entry.get('key') or '')})
    return result


# ---------------------------------------------------------------- GUI

if tk is not None:

    class SignalDialog(object):
        """Edit the signal values of a message (physical values, value tables as a selection)."""

        def __init__(self, parent, message, data, on_apply):
            self.message, self.on_apply = message, on_apply
            self.data = list(data)
            self.window = window = tk.Toplevel(parent)
            window.title(tr('Signalwerte') + ' – ' + str(message['name']))
            window.transient(parent)
            window.configure(background=C.SURFACE)
            outer = ttk.Frame(window, padding=12)
            outer.pack(fill=tk.BOTH, expand=True)
            self.bytes_frame = ttk.Frame(outer)
            self.bytes_frame.pack(side=tk.TOP, fill=tk.X, pady=(0, 8))
            body = ttk.Frame(outer)
            body.pack(fill=tk.BOTH, expand=True)
            self.byte_labels = []
            self.active = None
            canvas_height = min(420, 34 * max(1, len(message['signals'])) + 10)
            canvas = tk.Canvas(body, background=C.SURFACE, highlightthickness=0, height=canvas_height, width=480)
            scroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            inner = ttk.Frame(canvas)
            canvas.create_window(0, 0, window=inner, anchor=tk.NW)
            inner.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
            self.fields = []
            for row, signal in enumerate(message['signals']):
                ttk.Label(inner, text=signal['name']).grid(row=row, column=0, sticky=tk.W, padx=(0, 10), pady=2)
                variable = tk.StringVar()
                values = [item for item in signal.get('values') or []]
                names = ['%s – %s' % (item.get('value', ''), item.get('name', '')) for item in values]
                if names:
                    widget = ttk.Combobox(inner, textvariable=variable, values=names, width=28)
                else:
                    widget = ttk.Entry(inner, textvariable=variable, width=18)
                widget.grid(row=row, column=1, sticky=tk.W, pady=2)
                ttk.Label(inner, text=signal.get('unit') or '', foreground=C.MUTED).grid(
                    row=row, column=2, sticky=tk.W, padx=6)
                current = decode_signal(signal, self.data)
                variable.set('' if current is None else ('%g' % current))
                self.fields.append((signal, variable))
                widget.bind('<FocusIn>', lambda event, signal=signal: self.activate(signal), add='+')
                variable.trace_add('write', lambda *args, signal=signal: self.activate(signal))
            self.activate(None)
            buttons = ttk.Frame(window, padding=(12, 0, 12, 12))
            buttons.pack(fill=tk.X)
            ttk.Label(buttons, text=tr('Hexadezimal mit 0x eingeben, z. B. 0x1F'), foreground=C.MUTED).pack(
                side=tk.LEFT)
            ttk.Button(buttons, text=tr('Abbrechen'), command=window.destroy).pack(side=tk.RIGHT, padx=(6, 0))
            ttk.Button(buttons, text=tr('Übernehmen'), style='Accent.TButton', command=self.apply).pack(side=tk.RIGHT)
            window.bind('<Escape>', lambda event: window.destroy())

        def current_data(self):
            data = list(self.data)
            for signal, variable in self.fields:
                try:
                    encode_signal(signal, data, parse_value(variable.get()))
                except ValueError:
                    continue
            return data

        def activate(self, signal):
            """Show the data bytes; those of the signal being edited are colored."""
            if signal is not None:
                self.active = signal
            data = self.current_data()
            marked = signal_bytes(self.active) if self.active is not None else set()
            if len(self.byte_labels) != len(data):
                for label in self.byte_labels:
                    label.destroy()
                self.byte_labels = []
                per_row = (len(data) + 1) // 2
                for index in range(len(data)):
                    row, column = divmod(index, per_row)
                    label = tk.Label(self.bytes_frame, width=3, borderwidth=1, relief=tk.SOLID, padx=0, pady=0)
                    label.grid(row=row, column=column + 1, padx=0)
                    self.byte_labels.append(label)
                    if column in (0, per_row - 1) or index == len(data) - 1:
                        tk.Label(self.bytes_frame, text=str(index), foreground=C.MUTED, background=C.SURFACE,
                                 pady=0).grid(row=row, column=0 if column == 0 else column + 2, padx=3)
            for index, label in enumerate(self.byte_labels):
                on = index in marked
                label.configure(text='%02X' % data[index], background=C.ACCENT if on else C.SURFACE,
                                foreground=C.ON_ACCENT if on else C.INK)

        def apply(self):
            data = self.current_data()
            self.on_apply(data)
            self.window.destroy()

    class MessageDialog(object):
        """Choose messages of the database to add (search, multiple selection)."""

        def __init__(self, parent, messages, on_choose, on_empty):
            self.messages, self.on_choose = messages, on_choose
            self.window = window = tk.Toplevel(parent)
            window.title(tr('Nachricht hinzufügen'))
            window.transient(parent)
            window.geometry('420x480')
            window.configure(background=C.SURFACE)
            body = ttk.Frame(window, padding=12)
            body.pack(fill=tk.BOTH, expand=True)
            self.search = tk.StringVar()
            entry = ttk.Entry(body, textvariable=self.search)
            entry.pack(fill=tk.X)
            entry.focus_set()
            self.search.trace_add('write', lambda *args: self.fill())
            frame = ttk.Frame(body)
            frame.pack(fill=tk.BOTH, expand=True, pady=8)
            self.listbox = tk.Listbox(frame, selectmode=tk.EXTENDED, activestyle='none', background=C.SURFACE,
                                      foreground=C.INK, selectbackground=C.ACCENT_SELECT,
                                      selectforeground=C.ACCENT_INK, highlightthickness=0, borderwidth=0)
            scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.listbox.yview)
            self.listbox.configure(yscrollcommand=scroll.set)
            self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.listbox.bind('<Double-Button-1>', lambda event: self.choose())
            buttons = ttk.Frame(body)
            buttons.pack(fill=tk.X)
            ttk.Button(buttons, text=tr('Leere Nachricht'), command=lambda: (window.destroy(), on_empty())).pack(
                side=tk.LEFT)
            ttk.Button(buttons, text=tr('Abbrechen'), command=window.destroy).pack(side=tk.RIGHT, padx=(6, 0))
            ttk.Button(buttons, text=tr('Hinzufügen'), style='Accent.TButton', command=self.choose).pack(side=tk.RIGHT)
            window.bind('<Escape>', lambda event: window.destroy())
            window.bind('<Return>', lambda event: self.choose())
            self.shown = []
            self.fill()

        def fill(self):
            words = self.search.get().lower().split()
            self.shown = []
            self.listbox.delete(0, tk.END)
            for message in self.messages:
                identifier = database_id(message)
                if identifier is None:
                    continue
                text = '%s (0x%X)' % (message['name'], identifier)
                if all(word in text.lower() for word in words):
                    self.shown.append(message)
                    self.listbox.insert(tk.END, text)

        def choose(self):
            chosen = [self.shown[index] for index in self.listbox.curselection()]
            if not chosen:
                return
            self.window.destroy()
            self.on_choose(chosen)

    class Entry(object):
        """One row of the list."""

        def __init__(self, identifier, data, period=DEFAULT_PERIOD, key=''):
            self.id_var = tk.StringVar(value='0x%X' % identifier)
            self.data_var = tk.StringVar(value=data_text(data))
            self.period_var = tk.StringVar(value=str(period))
            self.key_var = tk.StringVar(value=key)
            self.cyclic_var = tk.BooleanVar(value=False)
            self.name_var = tk.StringVar()
            self.job = None
            self.frame = None
            self.sent = 0

    class GeneratorApp(object):
        def __init__(self, root, settings=None, database=None, entries=None):
            """root: Tk or Toplevel. settings: window settings. database: loaded database. entries: see
            normalize_entries."""
            self.root = root
            self.settings = dict(settings or {})
            self.messages = []
            self.index = MessageIndex([])
            self.entries = []
            self.on_send = None  # callback(identifier, data, extended) -> error text or None (CANtrix.py)
            self.status = tk.StringVar()
            self.theme = Theme(root)
            self._build_layout()
            root.title('CAN Generator')
            root.minsize(860, 320)
            root.geometry(initial_geometry(root, self.settings, '980x460'))
            root.bind('<Key>', self.on_key)
            self.set_database(database)
            for item in normalize_entries(entries):
                self.add_entry(item['id'], parse_data(item['data']), item['period'], item['key'], show=False)
            self._layout_rows()
            set_text(self.status, 'Bereit')

        # ------------------------------------------------------------ layout

        def _build_layout(self):
            theme = self.theme
            theme.header(self.root, 'CAN Generator', tr('Nachrichten senden'))
            theme.footer(self.root, self.status)
            body = tk.Frame(self.root, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            pane, bar = theme.pane(body, tr('Nachrichten'))
            pane.pack(fill=tk.BOTH, expand=True)
            ttk.Button(bar, text=tr('Nachricht hinzufügen'), command=self.add_blank).pack(side=tk.LEFT, padx=4, pady=2)
            ttk.Button(bar, text=tr('Zyklisches Senden beenden'), command=self.stop_all).pack(
                side=tk.LEFT, padx=4, pady=2)
            hint = ttk.Label(
                pane, foreground=C.MUTED, padding=(12, 6, 12, 2), wraplength=800,
                text=tr('ID und Daten hexadezimal. Gesendet wird nur während der Aufzeichnung (Datenquelle Live, '
                        'Start). Taste: Zeichen oder Name wie F5 oder space.'))
            hint.pack(side=tk.TOP, fill=tk.X)
            hint.bind('<Configure>', lambda event: hint.configure(wraplength=max(200, event.width - 24)))
            area = ttk.Frame(pane)
            area.pack(fill=tk.BOTH, expand=True)
            canvas = self.canvas = tk.Canvas(area, background=C.SURFACE, highlightthickness=0)
            scroll = ttk.Scrollbar(area, orient=tk.VERTICAL, command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.rows = ttk.Frame(canvas)
            self.rows_id = canvas.create_window(0, 0, window=self.rows, anchor=tk.NW)
            self.rows.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>', lambda event: canvas.itemconfigure(self.rows_id, width=event.width))
            canvas.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units'))
        # Titles of the columns; headers and rows share one grid, so the titles stand above their fields.
        COLUMNS = ('ID', 'Name', 'Daten', 'Zyklisch', 'Periode', 'Taste')

        def _layout_rows(self):
            """Header and rows in one grid."""
            for widget in self.rows.winfo_children():
                widget.destroy()
            for entry in self.entries:
                entry.frame = None
            if not self.entries:
                self.empty = ttk.Label(self.rows, foreground=C.MUTED, padding=16,
                                       text=tr('Keine Nachrichten. „Nachricht hinzufügen“ oder Rechtsklick auf eine '
                                               'Zeile der Trace-Tabelle.'))
                self.empty.grid(row=0, column=0, sticky=tk.W)
                return
            self.rows.columnconfigure(2, weight=1)
            for column, title in enumerate(self.COLUMNS):
                ttk.Label(self.rows, text=tr(title), foreground=C.MUTED).grid(
                    row=0, column=column, sticky=tk.W, padx=(8 if column == 0 else 4, 4), pady=(8, 2))
            for number, entry in enumerate(self.entries, 1):
                self._build_row(entry, number)

        def _build_row(self, entry, row):
            entry.frame = True
            grid = dict(row=row, pady=2, padx=4)
            ttk.Entry(self.rows, textvariable=entry.id_var, width=11).grid(column=0, sticky=tk.W, **dict(grid, padx=(8, 4)))
            ttk.Label(self.rows, textvariable=entry.name_var, width=16).grid(column=1, sticky=tk.W, **grid)
            ttk.Entry(self.rows, textvariable=entry.data_var, width=26).grid(column=2, sticky=tk.EW, **grid)
            ttk.Checkbutton(self.rows, variable=entry.cyclic_var, command=lambda: self.on_cyclic(entry)).grid(
                column=3, sticky=tk.W, **dict(grid, padx=(14, 4)))
            period = ttk.Frame(self.rows)
            period.grid(column=4, sticky=tk.W, **grid)
            ttk.Entry(period, textvariable=entry.period_var, width=6).pack(side=tk.LEFT)
            ttk.Label(period, text='ms', foreground=C.MUTED).pack(side=tk.LEFT, padx=(4, 0))
            ttk.Entry(self.rows, textvariable=entry.key_var, width=6).grid(column=5, sticky=tk.W, **grid)
            buttons = ttk.Frame(self.rows)
            buttons.grid(column=6, sticky=tk.E, **dict(grid, padx=(4, 8)))
            ttk.Button(buttons, text=tr('Signale…'), command=lambda: self.edit_signals(entry)).pack(side=tk.LEFT)
            ttk.Button(buttons, text=tr('Senden'), style='Accent.TButton', command=lambda: self.send(entry)).pack(
                side=tk.LEFT, padx=4)
            ttk.Button(buttons, text='✕', width=3, command=lambda: self.remove_entry(entry)).pack(side=tk.LEFT)
            entry.id_var.trace_add('write', lambda *args: self.update_name(entry))
            self.update_name(entry)

        # ------------------------------------------------------------ data

        def set_database(self, database):
            self.messages = (database or {}).get('messages') or []
            self.index = MessageIndex(self.messages)
            for entry in self.entries:
                self.update_name(entry)

        def message_of(self, entry):
            parsed = parse_identifier(entry.id_var.get())
            return self.index.find_any(parsed[0]) if parsed else None

        def update_name(self, entry):
            message = self.message_of(entry)
            entry.name_var.set(message['name'] if message else '')

        def add_blank(self):
            """Button "Nachricht hinzufügen": choose a message of the database, without a database an empty one."""
            messages = [message for message in self.messages if database_id(message) is not None]
            if messages:
                MessageDialog(self.root, messages, self.add_messages, lambda: self.add_entry(0, [0] * 8))
            else:
                self.add_entry(0, [0] * 8)

        def add_messages(self, messages):
            """Entries for database messages: their ID and data bytes set to 0."""
            for message in messages:
                try:
                    length = int(str(message.get('length') or 8))
                except ValueError:
                    length = 8
                self.add_entry(database_id(message), [0] * max(0, min(length, 64)), show=False)
            self._layout_rows()
            set_text(self.status, 'Nachrichten hinzugefügt: %s', str(len(messages)))

        def add_entry(self, identifier, data, period=DEFAULT_PERIOD, key='', show=True):
            """New entry (also from the trace table: ID and data of the frame)."""
            entry = Entry(normalize_id(identifier), list(data), period, key)
            self.entries.append(entry)
            if show:
                self._layout_rows()
                set_text(self.status, 'Nachricht hinzugefügt: 0x%s', '%X' % entry_id(entry))
            return entry

        def remove_entry(self, entry):
            self._cancel(entry)
            if entry in self.entries:
                self.entries.remove(entry)
            self._layout_rows()

        def collect_entries(self):
            """Entries for the configuration (cyclic sending is not saved: nothing starts on its own)."""
            result = []
            for entry in self.entries:
                parsed = parse_identifier(entry.id_var.get())
                data = parse_data(entry.data_var.get())
                if parsed is None or data is None:
                    continue
                period = number_or(entry.period_var.get(), DEFAULT_PERIOD)
                result.append({'id': parsed[0], 'data': data_text(data),
                               'period': max(MIN_PERIOD, int(period)), 'key': entry.key_var.get().strip()})
            return result

        # ------------------------------------------------------------ signals

        def edit_signals(self, entry):
            message = self.message_of(entry)
            data = parse_data(entry.data_var.get())
            if message is None or not message['signals']:
                set_text(self.status, 'Für diese ID gibt es keine Signale in der Datenbasis.')
                return
            if data is None:
                set_text(self.status, 'Ungültige Daten (hexadezimal, Bytes)')
                return
            try:
                length = int(str(message.get('length') or 0))
            except ValueError:
                length = 0
            data = data + [0] * (length - len(data))
            SignalDialog(self.root, message, data, lambda new: entry.data_var.set(data_text(new)))

        # ------------------------------------------------------------ sending

        def _values(self, entry):
            """Arguments for on_send, or None (with a message in the status line)."""
            parsed = parse_identifier(entry.id_var.get())
            if parsed is None:
                set_text(self.status, 'Ungültige ID (hexadezimal)')
                return None
            data = parse_data(entry.data_var.get())
            if data is None:
                set_text(self.status, 'Ungültige Daten (hexadezimal, Bytes)')
                return None
            identifier, extended = parsed
            return identifier, data, extended

        def send(self, entry):
            """Send an entry once; returns True if it was sent."""
            values = self._values(entry)
            if values is None or self.on_send is None:
                return False
            error = self.on_send(*values)
            if error:
                self.status.set(error)
                return False
            entry.sent += 1
            set_text(self.status, 'Gesendet: 0x%s (%s×)', '%X' % values[0], str(entry.sent))
            return True

        def on_key(self, event):
            if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
                return  # typing in a field
            names = set(name.lower() for name in (event.keysym, event.char) if name and name.isprintable())
            for entry in list(self.entries):
                key = entry.key_var.get().strip().lower()
                if key and key in names:
                    self.send(entry)

        def on_cyclic(self, entry):
            self._cancel(entry)
            if entry.cyclic_var.get():
                if self._values(entry) is None:
                    entry.cyclic_var.set(False)
                    return
                self._cycle(entry)

        def _cycle(self, entry):
            entry.job = None
            if not entry.cyclic_var.get() or entry not in self.entries:
                return
            if not self.send(entry):
                entry.cyclic_var.set(False)  # stop on an error (e.g. not connected)
                return
            period = max(MIN_PERIOD, int(number_or(entry.period_var.get(), DEFAULT_PERIOD)))
            entry.job = self.root.after(period, lambda: self._cycle(entry))

        def _cancel(self, entry):
            if entry.job is not None:
                try:
                    self.root.after_cancel(entry.job)
                except tk.TclError:
                    pass
                entry.job = None

        def stop_all(self):
            for entry in self.entries:
                self._cancel(entry)
                entry.cyclic_var.set(False)

        # ------------------------------------------------------------ window

        def window_settings(self):
            return {'geometry': window_geometry(self.root)}

        def theme_changed(self):
            pass

        def language_changed(self):
            self._layout_rows()

        def close(self):
            self.stop_all()
            self.root.destroy()

    def entry_id(entry):
        parsed = parse_identifier(entry.id_var.get())
        return parsed[0] if parsed else 0
