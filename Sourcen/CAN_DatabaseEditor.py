#!/usr/bin/env python
"""CAN Database Editor.

Shows DBC, ARXML or HTML message catalogue files in a tkinter window.
Optionally pass a database file to open it on start.
Compatible with Python 3.6.
"""
import argparse
import colorsys
import os
import queue
import re
import shutil
import sys
import threading

try:
    import tkinter as tk
    from tkinter import filedialog, ttk, messagebox
except ImportError:
    tk = None

from CAN_Common import (C, Theme, apply_appearance, as_whole, combine_databases, decode_text, display, draw_new_icon,
                        draw_open_icon, draw_save_icon, ecu_names, icon_hue, icon_line, icon_page, initial_geometry, is_big_endian, js_str, normalize_id, ordered_unique, parse_database,
                        parse_dbc_environment, read_database_bytes, rounded_rect, signal_bit_numbers,
                        window_geometry)
from CAN_Language import etr
from CAN_ArxmlWriter import export_arxml
from CAN_DbcEditor import EMPTY_DBC, DbcEditError, DbcText, export_dbc, raw_id, split_names

# ---------------------------------------------------------------- GUI

SIGNAL_COLUMNS = [
    ('name', 'Signal', 170), ('start_bit', 'Start bit', 70), ('length', 'Length', 60),
    ('description', 'Description', 320), ('values', 'Values', 280),
    ('factor', 'Factor', 90), ('offset', 'Offset', 90), ('minimum', 'Minimum', 90),
    ('maximum', 'Maximum', 90), ('unit', 'Unit', 70), ('computation', 'Calculation', 140),
    ('multiplex', 'Multiplexing', 90), ('receivers', 'Receivers', 170),
    ('data_type', 'Data type', 130), ('byte_order', 'Byte order', 100), ('signedness', 'Signedness', 90),
]
ENV_COLUMNS = [
    ('name', 'Variable', 190), ('type', 'Type', 80), ('minimum', 'Minimum', 90), ('maximum', 'Maximum', 90),
    ('unit', 'Unit', 70), ('initial', 'Initial value', 100), ('description', 'Description', 320),
    ('values', 'Values', 220), ('access', 'Access', 110), ('nodes', 'Access nodes', 220),
]
ENV_GROUP = 'Environment variables'


def difference_colors():
    return {'added': C.SUCCESS, 'removed': C.DANGER, 'changed': '#60a5fa' if C.dark else '#1d4ed8'}

BYTE_ORDERS = ('Little endian (Intel)', 'Big endian (Motorola)')
SIGNEDNESS = ('unsigned', 'signed')
ENV_TYPES = ('integer', 'float', 'string', 'data')
ENV_ACCESS = ('unrestricted', 'read', 'write', 'read/write')
TITLE = 'CAN Database Editor'


# ---- Icons of the buttons (drawn in a 30 x 30 area, see Theme.icon_button; New/Open/Save come from CAN_Common)

def _plus(canvas, x, y, color=None):
    """Small plus (a badge for 'new')."""
    line = icon_line(color or C.SUCCESS, 2)
    canvas.create_line(x - 3, y, x + 3, y, **line)
    canvas.create_line(x, y - 3, x, y + 3, **line)


def draw_save_as_icon(canvas, color):
    """Floppy disk with a pencil badge."""
    blue = icon_hue(5)
    canvas.create_polygon(7, 7, 18, 7, 21, 10, 21, 21, 7, 21, fill=C.SURFACE, outline=blue, width=1.5,
                          joinstyle='round', tags=('icon',))
    canvas.create_rectangle(10, 7, 17, 11, fill=blue, outline=blue, tags=('icon',))
    canvas.create_line(10, 21, 10, 15, 18, 15, 18, 21, **icon_line(blue))
    _plus(canvas, 22, 22)


def draw_export_arxml_icon(canvas, color):
    """Document with angle brackets."""
    icon_page(canvas, C.MUTED)
    line = icon_line(icon_hue(5))
    canvas.create_line(13.5, 14.5, 11, 17, 13.5, 19.5, **line)
    canvas.create_line(16.5, 14.5, 19, 17, 16.5, 19.5, **line)


def _arrow(canvas, direction):
    """Curved arrow for undo (direction -1, tip left) and redo (+1, tip right)."""
    def x(value):
        return value if direction < 0 else 30 - value
    line = icon_line(icon_hue(2), 2)
    canvas.create_line(x(13), 9, x(9), 13, x(13), 17, **line)
    canvas.create_line(x(9), 13, x(17), 13, x(21), 16, x(21), 18, x(17), 21, x(12), 21, **line)


def draw_undo_icon(canvas, color):
    _arrow(canvas, -1)


def draw_redo_icon(canvas, color):
    _arrow(canvas, 1)


def draw_expand_icon(canvas, color):
    """Two chevrons pointing down: expand all."""
    line = icon_line(C.MUTED)
    canvas.create_line(10, 9, 15, 14, 20, 9, **line)
    canvas.create_line(10, 16, 15, 21, 20, 16, **line)


def draw_collapse_icon(canvas, color):
    """Two chevrons pointing up: collapse all."""
    line = icon_line(C.MUTED)
    canvas.create_line(10, 14, 15, 9, 20, 14, **line)
    canvas.create_line(10, 21, 15, 16, 20, 21, **line)


def draw_new_message_icon(canvas, color):
    """Message frame (header and data cells) with a plus."""
    line = icon_line(icon_hue(3))
    canvas.create_rectangle(7, 9, 21, 20, outline=icon_hue(3), width=1.5, tags=('icon',))
    canvas.create_line(7, 13, 21, 13, **line)
    canvas.create_line(14, 13, 14, 20, **line)
    _plus(canvas, 22, 21)


def draw_new_variable_icon(canvas, color):
    """Environment variable (rhombus) with a plus."""
    canvas.create_polygon(14, 7, 21, 14, 14, 21, 7, 14, fill=C.SURFACE, outline=icon_hue(4), width=1.5,
                          joinstyle='round', tags=('icon',))
    _plus(canvas, 22, 22)


def draw_new_signal_icon(canvas, color):
    """Signal curve with a plus."""
    canvas.create_line(7, 19, 10, 19, 10, 11, 15, 11, 15, 17, 19, 17, **icon_line(icon_hue(1)))
    _plus(canvas, 22, 22)


def draw_edit_icon(canvas, color):
    """Pencil."""
    ink = icon_hue(2)
    canvas.create_polygon(8, 22, 9, 17, 19, 7, 23, 11, 13, 21, fill=C.SURFACE, outline=ink, width=1.5,
                          joinstyle='round', tags=('icon',))
    canvas.create_line(17, 9, 21, 13, **icon_line(ink))


def draw_duplicate_icon(canvas, color):
    """Two overlapping sheets."""
    canvas.create_rectangle(7, 7, 18, 19, outline=C.MUTED, width=1.5, tags=('icon',))
    canvas.create_rectangle(12, 11, 23, 23, fill=C.SURFACE, outline=icon_hue(5), width=1.5, tags=('icon',))


def draw_delete_icon(canvas, color):
    """Waste bin."""
    line = icon_line(C.DANGER)
    canvas.create_line(8, 10, 22, 10, **line)
    canvas.create_line(12, 10, 12, 8, 18, 8, 18, 10, **line)
    canvas.create_line(10, 10, 11, 22, 19, 22, 20, 10, **line)
    canvas.create_line(14, 13, 14, 19, **line)
    canvas.create_line(16, 13, 16, 19, **line)
BUS_PADDING, BUS_GAP, BUS_MIN_NODE_WIDTH, BUS_NODE_HEIGHT = 20, 6, 56, 26
BUS_Y = 19 + BUS_NODE_HEIGHT + 44
BUS_LOWER_Y = BUS_Y + 44
BUS_GROUP_HEIGHT = BUS_LOWER_Y + BUS_NODE_HEIGHT + 19


def signal_color(index):
    hue = (index * 137.508 % 360) / 360.0
    red, green, blue = colorsys.hls_to_rgb(hue, 0.32 if C.dark else 0.86, 0.55 if C.dark else 0.70)
    return '#%02x%02x%02x' % (int(red * 255), int(green * 255), int(blue * 255))


def signal_bits(signals):
    bits = {}
    for index, signal in enumerate(signals):
        start = as_whole(signal.get('start_bit'))
        length = as_whole(signal.get('length'))
        if start is None or start < 0 or length is None or length < 1:
            continue
        for bit in signal_bit_numbers(start, length, is_big_endian(signal)):
            bits.setdefault(bit, []).append(index)
    return bits


def byte_count_for(message, bits):
    highest = max(bits) if bits else -1
    declared = re.match(r'\s*([-+]?\d+)', str(message['length']))
    declared_bytes = int(declared.group(1)) if declared else 0
    return max(declared_bytes if declared_bytes > 0 else 0, (highest + 8) // 8)


def _count(number):
    text = format(number, ',')
    return text.replace(',', '.') if etr('Cancel') != 'Cancel' else text


def bus_label():
    return etr('Bus not specified')


def build_bus_groups(messages):
    participants = {}
    for message in messages:
        bus = message['bus'].strip() or bus_label()
        senders, receivers = participants.setdefault(bus, ([], []))
        senders.extend(ecu_names(message['senders']))
        receivers.extend(ecu_names(message['receivers']))

    groups = []
    for bus, (senders, receivers) in participants.items():
        nodes = ordered_unique(senders + receivers)
        widths = [max(BUS_MIN_NODE_WIDTH, len(name) * 6 + 14) for name in nodes]
        centers = []
        for index, width in enumerate(widths):
            if index >= 2:
                centers.append(centers[index - 2] + (widths[index - 2] + width) / 2.0 + BUS_GAP)
            else:
                centers.append(BUS_PADDING + width / 2.0)
        extent = max([center + width / 2.0 for center, width in zip(centers, widths)] or [0])
        groups.append({'bus': bus, 'nodes': list(zip(nodes, widths, centers)),
                       'width': max(280, extent) + BUS_PADDING})
    return groups


def value_lines(signal):
    """One text per value of the signal's value table ("0 = Park"), sorted by value."""
    def order(item):
        try:
            return (0, float(item['value']), '')
        except ValueError:
            return (1, 0.0, item['value'])
    return ['%s = %s' % (item['value'], item['name']) for item in sorted(signal['values'], key=order)]


def cell_text(signal, key):
    if key == 'values':
        return '\n'.join(value_lines(signal))
    return display(signal.get(key))


def env_cell_text(variable, key):
    """Text of an environment variable (parse_dbc_environment) in the column key; values sorted by value."""
    if key == 'values':
        return display('\n'.join('%s = %s' % (js_str(value), text)
                                  for value, text in sorted((variable.get('values') or {}).items())))
    return display(variable.get(key))


def parse_value_lines(text):
    """Value table typed as lines "0 = Off" -> [('0', 'Off'), ...]."""
    pairs = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = re.match(r'^\s*(-?\d+)\s*[=:]?\s*(.*?)\s*$', line)
        if not match or not match.group(2):
            raise DbcEditError(etr('Line %d of the values must look like "0 = Off".') % number)
        pairs.append((match.group(1), match.group(2)))
    return pairs


def bit_order(bit, big_endian):
    """Position of a bit in the order in which a signal runs through the bits: Intel signals count up,
    Motorola signals run through every byte from bit 7 to bit 0. (The mapping is its own inverse.)"""
    return (bit // 8) * 8 + 7 - bit % 8 if big_endian else bit


def id_input_text(message):
    """CAN ID of a message as shown in the edit dialog (hexadecimal, without the extended flag)."""
    number = raw_id(message)
    return '0x%X' % normalize_id(int(number)) if number else ''


def load_database(path):
    """Stand-alone start: database file as {'name', 'path', 'messages', 'environment'}."""
    name = os.path.basename(path)
    data = read_database_bytes(path)
    database = {'name': name, 'path': path, 'messages': parse_database(data, name)}
    if name.lower().endswith('.dbc'):
        database['environment'] = parse_dbc_environment(decode_text(data))
    return database


if tk is not None:
    class FormDialog(object):
        """Modal input form. fields: dicts with key, label, kind ('entry', 'combo' or 'text'), value,
        values (choices of a combo), height (lines of a text) and hint (small note below). On OK
        apply(values) gets {key: text}; it returns an error text (the dialog stays open) or None."""

        def __init__(self, parent, theme, title, fields, apply, ok_text='OK', width=44):
            self.apply = apply
            self.variables, self.texts = {}, {}
            window = self.window = tk.Toplevel(parent)
            apply_appearance(window)
            window.title(title)
            window.transient(parent)
            window.resizable(False, False)
            card, _ = theme.pane(window, title)
            card.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            body = ttk.Frame(card, padding=(16, 12, 16, 16))
            body.pack(fill=tk.BOTH, expand=True)
            body.columnconfigure(1, weight=1)
            first, row = None, 0
            for field in fields:
                kind = field.get('kind', 'entry')
                ttk.Label(body, text=field['label']).grid(row=row, column=0, sticky='ne' if kind == 'text' else 'w',
                                                          padx=(0, 12), pady=3)
                if kind == 'text':
                    widget = tk.Text(body, width=width, height=field.get('height', 4), wrap='word',
                                     font=theme.font_text, background=C.SURFACE, foreground=C.INK,
                                     insertbackground=C.INK, relief=tk.FLAT, highlightthickness=1,
                                     highlightbackground=C.LINE_STRONG, highlightcolor=C.ACCENT, padx=6, pady=4)
                    widget.insert('1.0', field.get('value', ''))
                    self.texts[field['key']] = widget
                else:
                    variable = self.variables[field['key']] = tk.StringVar(value=field.get('value', ''))
                    if kind == 'combo':
                        widget = ttk.Combobox(body, textvariable=variable, values=field['values'],
                                              state='readonly', width=width - 3)
                    else:
                        widget = ttk.Entry(body, textvariable=variable, width=width)
                widget.grid(row=row, column=1, sticky='ew', pady=3)
                if first is None:
                    first = widget
                row += 1
                if field.get('hint'):
                    ttk.Label(body, text=field['hint'], foreground=C.MUTED, font=theme.font_axis).grid(
                        row=row, column=1, sticky='w')
                    row += 1
            buttons = ttk.Frame(body, padding=(0, 14, 0, 0))
            buttons.grid(row=row, column=0, columnspan=2, sticky='ew')
            ttk.Button(buttons, text=etr('Cancel'), command=window.destroy).pack(side=tk.RIGHT)
            ttk.Button(buttons, text=ok_text, style='Accent.TButton', command=self.ok).pack(side=tk.RIGHT, padx=6)
            window.bind('<Return>', self.on_return)
            window.bind('<Escape>', lambda event: window.destroy())
            window.update_idletasks()
            x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_reqwidth()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_reqheight()) // 3
            window.geometry('+%d+%d' % (max(0, x), max(0, y)))
            if first is not None:
                first.focus_set()
            window.grab_set()

        def on_return(self, event):
            if not isinstance(event.widget, tk.Text):  # in a text, Return starts a new line
                self.ok()

        def values(self):
            values = dict((key, variable.get()) for key, variable in self.variables.items())
            values.update((key, widget.get('1.0', 'end-1c')) for key, widget in self.texts.items())
            return values

        def ok(self):
            error = self.apply(self.values())
            if error:
                messagebox.showerror(self.window.title(), error, parent=self.window)
            else:
                self.window.destroy()


    class SignalTable(object):
        """Signal table drawn on canvases: texts wrap within their column, every value of the value
        table gets its own line, and every signal row has a frame. The heading stays at the top and
        column widths can be dragged at the heading's separators."""

        PAD_X, PAD_Y, GRIP = 8, 6, 4

        def __init__(self, master, theme, columns, on_select, on_activate=None):
            self.theme = theme
            self.on_activate = on_activate
            self.columns = [key for key, _, _ in columns]
            self.headings = [etr(heading) for _, heading, _ in columns]
            self.widths = [width for _, _, width in columns]
            self.on_select = on_select
            self._saved_widths = {}  # column keys -> widths, for set_columns
            self.rows = []  # [[signal texts per column], ...]
            self.row_differences = {}
            self.row_items = []  # (y0, y1, background item, text items) per row
            self.selected = None
            self._drag = None

            frame = self.frame = ttk.Frame(master, padding=(4, 0, 0, 4))
            heading_height = theme.font_heading.metrics('linespace') + 2 * self.PAD_Y + 2
            self.header = tk.Canvas(frame, height=heading_height, background=C.SURFACE_ALT,
                                    highlightthickness=0, borderwidth=0)
            self.body = tk.Canvas(frame, background=C.SURFACE, highlightthickness=0, borderwidth=0,
                                  xscrollincrement=20, yscrollincrement=20)
            self.header.configure(xscrollincrement=20)
            scroll_y = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.body.yview)
            scroll_x = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.xview)
            self.body.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
            self.header.grid(row=0, column=0, sticky='ew')
            self.body.grid(row=1, column=0, sticky='nsew')
            scroll_y.grid(row=1, column=1, sticky='ns')
            scroll_x.grid(row=2, column=0, sticky='ew')
            frame.rowconfigure(1, weight=1)
            frame.columnconfigure(0, weight=1)

            self.body.bind('<Button-1>', self.on_click)
            self.body.bind('<Double-Button-1>', self.on_double_click)
            self.header.bind('<Motion>', self.on_header_motion)
            self.header.bind('<Leave>', lambda event: self.header.configure(cursor=''))
            self.header.bind('<Button-1>', self.on_header_press)
            self.header.bind('<B1-Motion>', self.on_header_drag)
            self.header.bind('<ButtonRelease-1>', self.on_header_release)
            for widget in (self.body, self.header):
                widget.bind('<MouseWheel>', self.on_wheel)
                widget.bind('<Shift-MouseWheel>', lambda event: self.on_wheel(event, horizontal=True))
                widget.bind('<Button-4>', lambda event: self.body.yview_scroll(-1, 'units'))
                widget.bind('<Button-5>', lambda event: self.body.yview_scroll(1, 'units'))
                try:  # Tk 9 on macOS: touchpad gestures come as <TouchpadScroll>, not <MouseWheel>
                    widget.bind('<TouchpadScroll>', self.on_touchpad)
                except tk.TclError:
                    pass
            self._touch = [0.0, 0.0]  # touchpad movement not scrolled yet (x, y in pixels)
            self.draw()

        def pack(self, **options):
            self.frame.pack(**options)

        # data

        def set_rows(self, rows, selected=None, differences=None):
            """rows: list of signals, each a list of texts in column order."""
            self.rows = rows
            self.row_differences = differences or {}
            self.selected = selected if selected is not None and 0 <= selected < len(rows) else None
            self.body.yview_moveto(0)
            self.draw()
            if self.selected is not None:
                self.see(self.selected)

        def clear(self):
            self.set_rows([])

        def retitle(self, columns):
            """Headings in the current language (after a language switch)."""
            self.headings = [etr(heading) for _, heading, _ in columns]
            self.draw_header()

        def set_columns(self, columns):
            """Switch to other columns (e.g. environment variables); widths are kept per column set."""
            keys = [key for key, _, _ in columns]
            if keys == self.columns:
                return
            self._saved_widths[tuple(self.columns)] = list(self.widths)
            self.columns = keys
            self.headings = [etr(heading) for _, heading, _ in columns]
            self.widths = list(self._saved_widths.get(tuple(keys)) or [width for _, _, width in columns])
            self.rows, self.selected = [], None
            self.row_differences = {}
            self.body.xview_moveto(0)
            self.header.xview_moveto(0)
            self.draw()

        # drawing

        def total_width(self):
            return sum(self.widths)

        def draw(self):
            self.draw_header()
            self.draw_body()

        def draw_header(self):
            canvas = self.header
            canvas.delete('all')
            height = int(canvas.cget('height'))
            x = 0
            for heading, width in zip(self.headings, self.widths):
                canvas.create_text(x + self.PAD_X, height / 2.0, anchor=tk.W, text=heading, fill=C.MUTED,
                                   font=self.theme.font_heading, width=max(1, width - 2 * self.PAD_X))
                x += width
                canvas.create_line(x - 1, 4, x - 1, height - 4, fill=C.LINE)
            canvas.create_line(0, height - 1, x, height - 1, fill=C.LINE)
            canvas.configure(scrollregion=(0, 0, x, height))

        def draw_body(self):
            canvas = self.body
            canvas.delete('all')
            self.row_items = []
            font = self.theme.font_text
            total = self.total_width()
            y = 0
            colors = difference_colors()
            for index, texts in enumerate(self.rows):
                ink = colors.get(self.row_differences.get(index), C.INK)
                background = canvas.create_rectangle(0, y, total, y, width=0, fill=C.SURFACE)
                items = []
                bottom = y + font.metrics('linespace')
                x = 0
                for text, width in zip(texts, self.widths):
                    item = canvas.create_text(x + self.PAD_X, y + self.PAD_Y, anchor=tk.NW, text=text,
                                              fill=ink, font=font, width=max(1, width - 2 * self.PAD_X))
                    items.append(item)
                    bottom = max(bottom, canvas.bbox(item)[3])
                    x += width
                y1 = bottom + self.PAD_Y
                canvas.coords(background, 0, y, total, y1)
                self.row_items.append((y, y1, background, items))
                y = y1
                # frame of the row: separators between the cells and a line below the signal
                x = 0
                for width in self.widths:
                    x += width
                    canvas.create_line(x - 1, self.row_items[-1][0], x - 1, y, fill=C.LINE)
                canvas.create_line(0, y - 1, total, y - 1, fill=C.LINE_STRONG)
            canvas.configure(scrollregion=(0, 0, total, max(y, 1)))
            if self.selected is not None:
                self._paint(self.selected, True)

        def _paint(self, index, selected):
            if index is None or not 0 <= index < len(self.row_items):
                return
            _, _, background, items = self.row_items[index]
            self.body.itemconfigure(background, fill=C.ACCENT_SELECT if selected else C.SURFACE)
            ink = difference_colors().get(self.row_differences.get(index), C.ACCENT_INK if selected else C.INK)
            for item in items:
                self.body.itemconfigure(item, fill=ink)

        # selection and scrolling

        def select(self, index):
            self._paint(self.selected, False)
            self.selected = index
            self._paint(index, True)

        def see(self, index):
            if not 0 <= index < len(self.row_items):
                return
            y0, y1 = self.row_items[index][:2]
            total = self.row_items[-1][1] or 1
            top, bottom = (fraction * total for fraction in self.body.yview())
            if y0 < top:
                self.body.yview_moveto(y0 / float(total))
            elif y1 > bottom:
                # bottom of the row at the bottom of the view, but never hide the top of the row
                self.body.yview_moveto(min(y0, max(0.0, y1 - (bottom - top))) / float(total))

        def on_click(self, event):
            y = self.body.canvasy(event.y)
            for index, (y0, y1, _, _) in enumerate(self.row_items):
                if y0 <= y < y1:
                    if index != self.selected:
                        self.select(index)
                        self.on_select(index)
                    return

        def on_double_click(self, event):
            if self.on_activate is not None and self.selected is not None:
                y = self.body.canvasy(event.y)
                y0, y1 = self.row_items[self.selected][:2]
                if y0 <= y < y1:
                    self.on_activate(self.selected)

        def xview(self, *args):
            self.body.xview(*args)
            self.header.xview_moveto(self.body.xview()[0])

        def on_wheel(self, event, horizontal=False):
            delta = event.delta
            steps = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
            if horizontal:
                self.body.xview_scroll(steps, 'units')
                self.header.xview_moveto(self.body.xview()[0])
            else:
                self.body.yview_scroll(steps, 'units')
            return 'break'

        def on_touchpad(self, event):
            try:
                dx, dy = (float(value) for value in
                          self.body.tk.splitlist(self.body.tk.call('tk::PreciseScrollDeltas', event.delta)))
            except (tk.TclError, ValueError):
                return 'break'
            self._touch[0] -= dx
            self._touch[1] -= dy
            step = float(self.body.cget('yscrollincrement')) or 20.0
            columns, rows = int(self._touch[0] / step), int(self._touch[1] / step)
            if columns:
                self._touch[0] -= columns * step
                self.body.xview_scroll(columns, 'units')
                self.header.xview_moveto(self.body.xview()[0])
            if rows:
                self._touch[1] -= rows * step
                self.body.yview_scroll(rows, 'units')
            return 'break'

        # column widths

        def _grip(self, event):
            """Index of the column whose right edge is under the mouse, or None."""
            x = self.header.canvasx(event.x)
            edge = 0
            for index, width in enumerate(self.widths):
                edge += width
                if abs(x - edge) <= self.GRIP:
                    return index
            return None

        def on_header_motion(self, event):
            if self._drag is None:
                self.header.configure(cursor='sb_h_double_arrow' if self._grip(event) is not None else '')

        def on_header_press(self, event):
            index = self._grip(event)
            if index is not None:
                self._drag = (index, self.header.canvasx(event.x), self.widths[index])

        def on_header_drag(self, event):
            if self._drag is None:
                return
            index, start, width = self._drag
            self.widths[index] = max(40, int(width + self.header.canvasx(event.x) - start))
            self.draw_header()

        def on_header_release(self, event):
            if self._drag is not None:
                self._drag = None
                self.draw_body()
                self.header.xview_moveto(self.body.xview()[0])


if tk is not None:
    class ViewerApp(object):
        def __init__(self, root, initial_path=None, settings=None, database=None):
            """root: Tk or Toplevel. settings: window settings (see window_settings). database: already
            loaded database {'name', 'path', 'messages'} from CANtrix.py; initial_path is only for
            the stand-alone start."""
            self.root = root
            self.messages = []
            self.parts = []  # one per database file: name, path, channel, messages, environment, source, ...
            self.message_part = []  # part index of every message of self.messages
            self.grouped = False
            self.environment_groups = []  # [(database name, channel, [variables sorted by name], part index)]
            self.current_env = None  # (group index, variable index or None) while variables are shown
            self.env_items = {}
            self.iid_keys, self.key_iids = {}, {}  # tree item <-> key of what it shows (see _current_key)
            self.undo_stack, self.redo_stack = [], []  # (part index, text before, text after)
            self.on_new = None  # called with the path of a new empty DBC file (the analyser adds it)
            self.on_open = None  # called without arguments: the analyser opens its database window
            self.on_saved = None  # called with the paths of the saved files (the analyser reloads them)
            self.bit_preview = None  # (signal index, start bit, length) while a signal is dragged
            self._bit_drag = None
            self._backed_up = set()
            self.edit_info = tk.StringVar(value='')
            self.multi_info = tk.StringVar(value='')  # hint (bold) while several databases are loaded
            self.database_name = ''
            self.bus_groups = []
            self.current = None
            self.tree_items = {}
            self.bit_hits = []
            self._bit_resize_job = None
            self.results = queue.Queue()
            self.status = tk.StringVar(value=etr('No database loaded. Open one in the CANtrix main window.'))
            self.counts = tk.StringVar(value=etr('No database loaded'))
            self.title_var = tk.StringVar(value=etr('Signals'))
            self.subtitle_var = tk.StringVar(value='')
            self.description_var = tk.StringVar(value='')
            self.bit_info = tk.StringVar(value='')
            self.search = tk.StringVar()
            self.only_differences = tk.BooleanVar(value=False)  # show only what differs between two databases

            self.settings = dict(settings or {})
            root.title('CAN Database Editor')
            root.geometry(initial_geometry(root, self.settings, '1300x820'))
            root.minsize(900, 600)
            self.theme = Theme(root)
            self._build_frame()
            self._build_body()
            root.protocol('WM_DELETE_WINDOW', self.request_close)
            if isinstance(root, tk.Tk) and root.tk.call('tk', 'windowingsystem') == 'aqua':
                root.createcommand('tk::mac::Quit', self.request_close)
            self._bind_shortcuts()
            self.populate_tree()
            self.draw_bit_layout()
            self.draw_bus_structure()
            self._poll_job = self.root.after(100, self.poll_results)
            self._update_edit_state()
            if database:
                self.set_database(database)
            elif initial_path:
                self.start_load(initial_path)

        def window_settings(self):
            """Window geometry and pane sizes."""
            return {'geometry': window_geometry(self.root), 'tree_width': self.body.sashpos(0),
                    'table_height': self.split.sashpos(0)}

        def close(self):
            self.root.after_cancel(self._poll_job)
            self.root.destroy()

        def language_changed(self):
            """Redraw everything the language switch cannot reach by replacing widget texts."""
            columns = ENV_COLUMNS if self.current_env is not None else SIGNAL_COLUMNS
            self.table.retitle(columns)
            if self.parts:
                self.status.set(etr('Loaded %s') % self.database_name)
                self._refresh(self._current_key())
            else:
                self.status.set(etr('No database loaded. Open one in the CANtrix main window.'))
                self.counts.set(etr('No database loaded'))

        def confirm_close(self):
            """False when the user cancels because of unsaved changes (the analyser asks before closing)."""
            dirty = [part['name'] for part in self.parts if self._dirty(part)]
            if not dirty:
                return True
            answer = messagebox.askyesnocancel(TITLE, etr('Save the changes to %s?') % ', '.join(dirty),
                                               parent=self.root)
            if answer is None:
                return False
            return self.save_all() if answer else True

        def request_close(self):
            if self.confirm_close():
                self.close()

        def _build_frame(self):
            self.theme.header(self.root, TITLE, subtitle_variable=self.counts)
            self._build_toolbar()
            footer = self.theme.footer(self.root, self.status)
            self.theme.footer_label(footer, self.bit_info)

        def _build_toolbar(self):
            bar = tk.Frame(self.root, background=C.SURFACE)
            bar.pack(side=tk.TOP, fill=tk.X)
            inner = tk.Frame(bar, background=C.SURFACE)
            inner.pack(side=tk.TOP, fill=tk.X, padx=16, pady=6)
            tk.Frame(bar, background=C.LINE, height=1).pack(side=tk.TOP, fill=tk.X)
            self.edit_buttons = {}
            for index, (key, draw, text, command) in enumerate((
                    ('new', draw_new_icon, etr('New'), self.new_database),
                    ('open', draw_open_icon, etr('Load'), self.open_database),
                    ('save', draw_save_icon, etr('Save'), self.save_all),
                    ('save_copy', draw_save_as_icon, etr('Save as DBC…'), self.save_copy),
                    ('export_arxml', draw_export_arxml_icon, etr('Save as ARXML…'), lambda: self.save_copy('arxml')),
                    ('undo', draw_undo_icon, etr('Undo'), self.undo),
                    ('redo', draw_redo_icon, etr('Redo'), self.redo))):
                self._icon_button(inner, key, draw, text, command, padx=(12 if index in (2, 5) else 0, 6))
            tk.Label(inner, textvariable=self.edit_info, background=C.SURFACE, foreground=C.MUTED,
                     font=self.theme.font_label).pack(side=tk.LEFT, padx=(8, 0))
            tk.Label(inner, textvariable=self.multi_info, background=C.SURFACE, foreground=C.INK,
                     font=self.theme.font_heading).pack(side=tk.LEFT, padx=(8, 0))

        def _icon_button(self, parent, key, draw, tooltip, command, padx=(0, 6)):
            """Symbol button (instead of a text button); registered in edit_buttons for the enabled state."""
            button = self.theme.icon_button(parent, draw, command, tooltip)
            button.pack(side=tk.LEFT, padx=padx)
            self.edit_buttons[key] = button
            return button

        def _bind_shortcuts(self):
            modifier = 'Command' if self.root.tk.call('tk', 'windowingsystem') == 'aqua' else 'Control'
            self.root.bind('<%s-z>' % modifier, lambda event: self.undo())
            self.root.bind('<%s-Z>' % modifier, lambda event: self.redo())
            self.root.bind('<%s-y>' % modifier, lambda event: self.redo())

        def _build_body(self):
            theme = self.theme
            body = self.body = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            left, _ = theme.pane(body, etr('Messages'))
            split = self.split = ttk.Panedwindow(body, orient=tk.VERTICAL)
            body.add(left, weight=1)
            body.add(split, weight=3)
            # The right pane requests more width than the window has (fixed table columns),
            # which would squeeze the message tree to zero width. Place the sash explicitly,
            # using the width from the saved window settings when available.
            def place_sash(event):
                body.unbind('<Configure>')
                saved = self.settings.get('tree_width')
                width = saved if isinstance(saved, int) else event.width // 4
                body.sashpos(0, max(200, min(width, event.width - 400)))
            body.bind('<Configure>', place_sash)

            tools = ttk.Frame(left, padding=(12, 8))
            tools.pack(side=tk.TOP, fill=tk.X)
            search_row = ttk.Frame(tools)
            search_row.pack(fill=tk.X, pady=(0, 8))
            ttk.Label(search_row, text=etr('Search')).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Entry(search_row, textvariable=self.search).pack(side=tk.LEFT, fill=tk.X, expand=True)
            buttons = ttk.Frame(tools)
            buttons.pack(fill=tk.X)
            self._icon_button(buttons, 'expand', draw_expand_icon, etr('Expand all'),
                              lambda: self.set_tree_open(True))
            self._icon_button(buttons, 'collapse', draw_collapse_icon, etr('Collapse all'),
                              lambda: self.set_tree_open(False))
            self._icon_button(buttons, 'new_message', draw_new_message_icon, etr('New message'), self.new_message,
                              padx=(12, 6))
            self._icon_button(buttons, 'new_variable', draw_new_variable_icon, etr('New variable'),
                              self.new_variable)
            self.diff_check = ttk.Checkbutton(buttons, text=etr('Show differences only'),
                                              variable=self.only_differences, command=self.populate_tree)
            self.diff_check.pack(side=tk.LEFT, padx=(12, 0))
            theme.bind_tooltip(self.diff_check, lambda: etr(
                'Green: added\nRed: removed\nBlue: changed\n'
                'The file with the later modification timestamp is the newer version.'))
            self.diff_check.state(['disabled'])
            tree_frame = ttk.Frame(left, padding=(4, 0, 0, 4))
            tree_frame.pack(fill=tk.BOTH, expand=True)
            self.tree = ttk.Treeview(tree_frame, show='tree', selectmode='browse', style='Database.Treeview')
            tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
            self.tree.configure(yscrollcommand=tree_scroll.set)
            self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self._configure_difference_tags()
            self.tree.bind('<<TreeviewSelect>>', self.on_tree_select)
            self.tree.bind('<Double-Button-1>', lambda event: self.edit_selected())
            self.clipboard_item = None  # copied signal or environment variable, see copy_selected
            right_buttons = ('<Button-2>', '<Button-3>', '<Control-Button-1>') \
                if self.root.tk.call('tk', 'windowingsystem') == 'aqua' else ('<Button-3>',)
            for button in right_buttons:
                self.tree.bind(button, self.on_tree_right_click)
            self.search.trace_add('write', lambda *args: self.populate_tree())

            def place_split_sash(event):
                split.unbind('<Configure>')
                saved = self.settings.get('table_height')
                if isinstance(saved, int):
                    split.sashpos(0, max(80, min(saved, event.height - 120)))
            split.bind('<Configure>', place_split_sash)

            table_pane, _ = theme.pane(split, variable=self.title_var)
            split.add(table_pane, weight=2)
            actions = ttk.Frame(table_pane, padding=(12, 8, 12, 0))
            actions.pack(side=tk.TOP, fill=tk.X)
            for key, draw, text, command in (('edit', draw_edit_icon, etr('Edit…'), self.edit_selected),
                                             ('new_signal', draw_new_signal_icon, etr('New signal'), self.new_signal),
                                             ('duplicate', draw_duplicate_icon, etr('Duplicate'),
                                              self.duplicate_selected),
                                             ('delete', draw_delete_icon, etr('Delete'), self.delete_selected)):
                self._icon_button(actions, key, draw, text, command)
            info = tk.Frame(table_pane, background=C.SURFACE)
            info.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(8, 6))
            tk.Label(info, textvariable=self.subtitle_var, background=C.SURFACE, foreground=C.MUTED,
                     font=theme.font_label, anchor=tk.W).pack(fill=tk.X)
            tk.Label(info, textvariable=self.description_var, background=C.SURFACE, foreground=C.INK,
                     wraplength=900, justify=tk.LEFT, anchor=tk.W).pack(fill=tk.X)
            self.table = SignalTable(table_pane, theme, SIGNAL_COLUMNS, self.on_table_select,
                                     lambda index: self.edit_selected())
            self.table.pack(fill=tk.BOTH, expand=True)
            for button in right_buttons:
                self.table.body.bind(button, self.on_table_right_click)

            layout_pane, _ = theme.pane(split, 'Layout')
            split.add(layout_pane, weight=3)
            notebook = ttk.Notebook(layout_pane)
            notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
            bit_tab = ttk.Frame(notebook)
            bus_tab = ttk.Frame(notebook)
            notebook.add(bit_tab, text=etr('Bit layout'))
            notebook.add(bus_tab, text=etr('Bus structure'))

            self.bit_canvas = tk.Canvas(bit_tab, background=C.SURFACE, highlightthickness=0)
            bit_y = ttk.Scrollbar(bit_tab, orient=tk.VERTICAL, command=self.bit_canvas.yview)
            self.bit_canvas.configure(yscrollcommand=bit_y.set)
            bit_y.pack(side=tk.RIGHT, fill=tk.Y)
            self.bit_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.bit_canvas.bind('<Motion>', self.on_bit_motion)
            self.bit_canvas.bind('<Button-1>', self.on_bit_press)
            self.bit_canvas.bind('<B1-Motion>', self.on_bit_drag)
            self.bit_canvas.bind('<ButtonRelease-1>', self.on_bit_release)
            self.bit_canvas.bind('<Configure>', self.on_bit_resize)

            self.bus_canvas = tk.Canvas(bus_tab, background=C.SURFACE, highlightthickness=0)
            bus_y = ttk.Scrollbar(bus_tab, orient=tk.VERTICAL, command=self.bus_canvas.yview)
            bus_x = ttk.Scrollbar(bus_tab, orient=tk.HORIZONTAL, command=self.bus_canvas.xview)
            self.bus_canvas.configure(yscrollcommand=bus_y.set, xscrollcommand=bus_x.set)
            bus_y.pack(side=tk.RIGHT, fill=tk.Y)
            bus_x.pack(side=tk.BOTTOM, fill=tk.X)
            self.bus_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # loading

        def set_database(self, database):
            """Show a database loaded by the caller: {'name', 'path', 'messages'}. With several databases
            (combine_databases: 'databases'), the message tree is grouped by database file. Unsaved changes
            of files that are still part of the database are kept."""
            database = database or {'name': '', 'messages': []}
            self.finish_load(True, database['name'], database.get('path'), database)

        def start_load(self, path):
            self.status.set(etr('Analyzing database…'))
            threading.Thread(target=self._load_worker, args=(path,), daemon=True).start()

        def _load_worker(self, path):
            try:
                self.results.put((True, os.path.basename(path), path, load_database(path)))
            except Exception as error:
                self.results.put((False, str(error), None, None))

        def poll_results(self):
            while True:
                try:
                    item = self.results.get_nowait()
                except queue.Empty:
                    break
                self.finish_load(*item)
            self._poll_job = self.root.after(100, self.poll_results)

        def finish_load(self, ok, name, path, database):
            if not ok:
                self.status.set(etr('Could not load the database.'))
                messagebox.showerror(TITLE, name, parent=self.root)
                return
            key = self._current_key()
            entries = database.get('databases') or [{
                'name': name, 'path': path, 'channel': None, 'messages': database['messages'],
                'environment': database.get('environment')}]
            kept = dict((part['path'], part) for part in self.parts if part['path'] and self._dirty(part))
            parts = []
            for entry in entries:
                entry_path = entry.get('path')
                part = {'name': entry.get('name', ''), 'path': entry_path, 'channel': entry.get('channel'),
                        'messages': entry.get('messages') or [], 'environment': entry.get('environment') or {},
                        'source': None, 'saved_text': None,
                        'editable': bool(entry_path) and entry_path.lower().endswith('.dbc')
                        and os.path.isfile(entry_path)}
                old = kept.get(entry_path)
                if old is not None and old['channel'] == part['channel']:
                    part['source'], part['saved_text'] = old['source'], old['saved_text']
                    self._reparse(part)
                parts.append(part)
            if [part['path'] for part in parts] != [part['path'] for part in self.parts]:
                self.undo_stack, self.redo_stack = [], []
            self.parts = parts
            self._rebuild_model()
            self.status.set(etr('Loaded %s') % self.database_name)
            self.populate_tree()
            self._show_nothing()
            if key:
                self._select_key(key)

        def _rebuild_model(self):
            """self.messages, the environment variables and the counts from the parts."""
            combined = combine_databases([({'name': part['name'], 'path': part['path'],
                                            'messages': part['messages'], 'environment': part['environment']},
                                           part['channel']) for part in self.parts])
            self.messages = combined['messages']
            self.message_part = [index for index, part in enumerate(self.parts) for _ in part['messages']]
            self.grouped = len(self.parts) > 1
            self.database_name = combined['name']
            self.environment_groups = [(part['name'], part['channel'],
                                        sorted(part['environment'].values(), key=lambda v: v['name']), index)
                                       for index, part in enumerate(self.parts) if part['environment']]
            self.bus_groups = build_bus_groups(self.messages)
            if hasattr(self, 'diff_check'):  # comparing needs exactly two databases
                self.diff_check.state(['!disabled'] if len(self.parts) == 2 else ['disabled'])
            signal_total = sum(len(message['signals']) for message in self.messages)
            env_total = sum(len(variables) for _, _, variables, _ in self.environment_groups)
            counts = etr('%s messages · %s signals') % (_count(len(self.messages)), _count(signal_total))
            if env_total:
                counts += etr(' · %s environment variables') % _count(env_total)
            self.counts.set(counts)

        def _show_nothing(self):
            self.current = None
            self.current_env = None
            self.bit_preview = None
            self.title_var.set(self.database_name)
            self.subtitle_var.set('')
            self.description_var.set('')
            self.table.set_columns(SIGNAL_COLUMNS)
            self.table.clear()
            self.draw_bit_layout()
            self.draw_bus_structure()
            self._update_edit_state()

        # message tree

        def _differences(self):
            """Return message and signal statuses, plus the differing environment variables.
            Messages are matched by ID, signals and variables by name."""
            if not self.only_differences.get() or len(self.parts) != 2:
                return None
            ignored = ('database', 'channel')

            def same(first, second, skip=()):
                return all(first.get(key) == second.get(key)
                           for key in set(first) | set(second) if key not in ignored + tuple(skip))

            unmatched = ('', '')  # File order is unknown when timestamps are equal or unavailable.
            try:
                first, second = (os.stat(part['path']).st_mtime_ns for part in self.parts)
            except (OSError, TypeError):
                pass
            else:
                if first < second:
                    unmatched = ('removed', 'added')
                elif first > second:
                    unmatched = ('added', 'removed')

            by_id = [{}, {}]
            for index, message in enumerate(self.messages):
                by_id[self.message_part[index]][raw_id(message)] = index
            messages = {}
            for index, message in enumerate(self.messages):
                part = self.message_part[index]
                other = by_id[1 - part].get(raw_id(message))
                if other is None:
                    status = unmatched[part]
                    messages[index] = status, dict.fromkeys(range(len(message['signals'])), status)
                    continue
                counterpart = self.messages[other]
                others = {signal['name']: signal for signal in counterpart['signals']}
                names = {signal['name'] for signal in message['signals']}
                signals = {}
                for number, signal in enumerate(message['signals']):
                    name = signal['name']
                    if name not in others:
                        signals[number] = unmatched[part]
                    elif not same(signal, others[name]):
                        signals[number] = 'changed'
                if (signals or names != others.keys() or len(message['signals']) != len(counterpart['signals'])
                        or not same(message, counterpart, ('signals',))):
                    messages[index] = 'changed', signals
            variables = set()
            for part_index, part in enumerate(self.parts):
                other = self.parts[1 - part_index]['environment']
                for name, variable in part['environment'].items():
                    if name not in other or not same(variable, other[name]):
                        variables.add((part_index, name))
            return messages, variables

        def _configure_difference_tags(self):
            # Keep difference colors visible on selected rows; the background still marks selection.
            ttk.Style(self.tree).map('Database.Treeview', foreground=[('disabled', C.SUBTLE)])
            for status, color in difference_colors().items():
                self.tree.tag_configure(status, foreground=color)

        def populate_tree(self):
            query = self.search.get().strip().lower()
            differences = self._differences()
            self.message_differences = differences[0] if differences is not None else {}
            if self.current is not None:
                signal_differences = self.message_differences.get(self.current[0], ('', {}))[1]
                if self.table.row_differences != signal_differences:
                    self.table.row_differences = signal_differences
                    self.table.draw_body()
            children = self.tree.get_children()
            if children:
                self.tree.delete(*children)
            self.tree_items = {}
            self.env_items = {}
            self.iid_keys, self.key_iids = {}, {}
            groups = {}  # (database name, channel) -> tree item, only with several databases

            def group_item(group):
                if group not in groups:
                    groups[group] = 'g%d' % len(groups)
                    self.tree.insert('', tk.END, iid=groups[group], open=True,
                                     text=group[0] + (etr('   (channel %s)') % group[1] if group[1] else ''))
                return groups[group]
            order = {}
            for message in self.messages:
                order.setdefault((message.get('database', ''), message.get('channel')), len(order))
            grouped = getattr(self, 'grouped', False)
            alphabetical = sorted(enumerate(self.messages), key=lambda item: (
                order[(item[1].get('database', ''), item[1].get('channel'))] if grouped else 0,
                item[1]['name'].lower(), item[0]))
            for message_index, message in alphabetical:
                if differences is not None and message_index not in differences[0]:
                    continue
                status, signal_differences = self.message_differences.get(message_index, ('', {}))
                name_match = not query or query in message['name'].lower()
                wanted = [index for index, signal in enumerate(message['signals'])
                          if (name_match or query in signal['name'].lower())
                          and (differences is None or index in signal_differences)]
                if not name_match and not wanted:
                    continue
                message_iid = 'm%d' % message_index
                parent = ''
                if getattr(self, 'grouped', False):
                    parent = group_item((message.get('database', ''), message.get('channel')))
                self.tree.insert(parent, tk.END, iid=message_iid,
                                 text='%s   %s' % (message['name'], message['id']),
                                 tags=(status,) if status else (),
                                 open=bool(query) and not name_match)
                self.tree_items[message_iid] = (message_index, None)
                self._key_item(message_iid, ('m', self.message_part[message_index], raw_id(message)))
                for signal_index in wanted:
                    signal_iid = 'm%ds%d' % (message_index, signal_index)
                    signal_status = signal_differences.get(signal_index)
                    self.tree.insert(message_iid, tk.END, iid=signal_iid,
                                     text=message['signals'][signal_index]['name'],
                                     tags=(signal_status,) if signal_status else ())
                    self.tree_items[signal_iid] = (message_index, signal_index)
                    self._key_item(signal_iid, ('s', self.message_part[message_index], raw_id(message),
                                                message['signals'][signal_index]['name']))
            # environment variables: one node per database after its messages
            for group_index, (database_name, channel, variables, part_index) in enumerate(self.environment_groups):
                group_match = not query or query in etr(ENV_GROUP).lower()
                wanted = [index for index, variable in enumerate(variables)
                          if (group_match or query in variable['name'].lower())
                          and (differences is None or (part_index, variable['name']) in differences[1])]
                if not wanted:
                    continue
                parent = group_item((database_name, channel)) if getattr(self, 'grouped', False) else ''
                env_iid = 'e%d' % group_index
                self.tree.insert(parent, tk.END, iid=env_iid, text='%s   (%d)' % (etr(ENV_GROUP), len(variables)),
                                 open=bool(query) and not group_match)
                self.env_items[env_iid] = (group_index, None)
                self._key_item(env_iid, ('eg', part_index))
                for variable_index in wanted:
                    variable_iid = 'e%dv%d' % (group_index, variable_index)
                    self.tree.insert(env_iid, tk.END, iid=variable_iid, text=variables[variable_index]['name'])
                    self.env_items[variable_iid] = (group_index, variable_index)
                    self._key_item(variable_iid, ('e', part_index, variables[variable_index]['name']))

        def _key_item(self, iid, key):
            self.iid_keys[iid] = key
            self.key_iids[key] = iid

        def set_tree_open(self, is_open):
            for iid, (_, signal_index) in self.tree_items.items():
                if signal_index is None:
                    self.tree.item(iid, open=is_open)
            for iid, (_, variable_index) in self.env_items.items():
                if variable_index is None:
                    self.tree.item(iid, open=is_open)

        def on_tree_select(self, event=None):
            selection = self.tree.selection()
            if selection and selection[0] in self.tree_items:
                message_index, signal_index = self.tree_items[selection[0]]
                self.show_message(message_index, signal_index)
            elif selection and selection[0] in self.env_items:
                self.show_environment(*self.env_items[selection[0]])
            else:
                self._update_edit_state()

        # message view

        def show_message(self, message_index, signal_index=None):
            message = self.messages[message_index]
            self.current = (message_index, signal_index)
            self.current_env = None
            self.bit_preview = None
            self.title_var.set(message['name'])
            parts = [etr('CAN ID %s') % display(message['id']), etr('Length %s bytes') % display(message['length'])]
            if getattr(self, 'grouped', False):
                parts.insert(0, message.get('database', '') + (etr(' (channel %s)') % message['channel']
                                                              if message.get('channel') else ''))
            if message['bus']:
                parts.append('Bus/Sender %s' % message['bus'])
            if message['senders']:
                parts.append('Sender %s' % message['senders'])
            if message['receivers']:
                parts.append(etr('Receiver %s') % message['receivers'])
            self.subtitle_var.set(' · '.join(parts))
            self.description_var.set(message['description'])

            self.table.set_columns(SIGNAL_COLUMNS)
            self.table.set_rows([[cell_text(signal, key) for key, _, _ in SIGNAL_COLUMNS]
                                 for signal in message['signals']], signal_index,
                                self.message_differences.get(message_index, ('', {}))[1])
            self.draw_bit_layout()
            self.draw_bus_structure()
            self._update_edit_state()

        def show_environment(self, group_index, variable_index=None):
            """All environment variables of a database in the table, variable_index selected."""
            database_name, channel, variables, _ = self.environment_groups[group_index]
            self.current = None
            self.current_env = (group_index, variable_index)
            self.bit_preview = None
            self.title_var.set(etr(ENV_GROUP))
            parts = [etr('%d variables') % len(variables)]
            if getattr(self, 'grouped', False):
                parts.insert(0, database_name + (etr(' (channel %s)') % channel if channel else ''))
            self.subtitle_var.set(' · '.join(parts))
            self.description_var.set(variables[variable_index]['description']
                                     if variable_index is not None else '')
            self.table.set_columns(ENV_COLUMNS)
            self.table.set_rows([[env_cell_text(variable, key) for key, _, _ in ENV_COLUMNS]
                                 for variable in variables], variable_index)
            self.draw_bit_layout()
            self.draw_bus_structure()
            self._update_edit_state()

        def on_table_select(self, index):
            if self.current_env is not None:
                variables = self.environment_groups[self.current_env[0]][2]
                self.current_env = (self.current_env[0], index)
                self.description_var.set(variables[index]['description'])
                self._update_edit_state()
                return
            if self.current is None:
                return
            self.current = (self.current[0], index)
            self.draw_bit_layout()
            self._update_edit_state()

        def draw_bit_layout(self):
            canvas = self.bit_canvas
            canvas.delete('all')
            self.bit_hits = []
            if self.current_env is not None:
                canvas.create_text(12, 12, anchor=tk.NW, fill=C.MUTED,
                                   text=etr('Environment variables are not part of a CAN message.'))
                canvas.configure(scrollregion=(0, 0, 600, 40))
                return
            if self.current is None:
                return
            message_index, selected = self.current
            message = self.messages[message_index]
            signals = message['signals']
            if self.bit_preview is not None and self.bit_preview[0] < len(signals):
                index, start, length = self.bit_preview
                signals = list(signals)
                signals[index] = dict(signals[index], start_bit=str(start), length=str(length))
            bits = signal_bits(signals)
            byte_count = byte_count_for(message, bits)
            if not byte_count:
                canvas.create_text(12, 12, anchor=tk.NW, fill=C.MUTED,
                                   text=etr('No bit length is available for this message.'))
                canvas.configure(scrollregion=(0, 0, 600, 40))
                return

            # Bytes are laid out side by side, as many per row as fit into the canvas width.
            cell_width, cell_height, label_width, row_height, byte_gap = 46, 30, 62, 42, 18
            byte_width = label_width + 8 * (cell_width + 4)
            available = max(canvas.winfo_width(), byte_width) - 10
            per_row = max(1, min(byte_count, (available + byte_gap) // (byte_width + byte_gap)))
            for byte_index in range(byte_count):
                row, column = divmod(byte_index, per_row)
                top = 8 + row * row_height
                block_left = 10 + column * (byte_width + byte_gap)
                canvas.create_text(block_left, top + cell_height / 2.0, anchor=tk.W, fill=C.MUTED,
                                   text='Byte %d' % byte_index)
                for position in range(8):
                    bit = byte_index * 8 + 7 - position
                    indices = bits.get(bit, [])
                    x0 = block_left + label_width + position * (cell_width + 4)
                    y0 = top
                    x1, y1 = x0 + cell_width, y0 + cell_height
                    names = [message['signals'][i]['name'] for i in indices]
                    title = 'Byte %d, Bit %d: %s' % (byte_index, 7 - position,
                                                      ', '.join(names) if names else etr('unassigned'))
                    fill = signal_color(indices[0]) if indices else C.SURFACE_ALT
                    outline, text_color, font = fill if indices else C.LINE_STRONG, C.INK, self.theme.font_label
                    line_width = 1
                    if selected is not None:
                        if selected in indices:
                            outline, font, line_width = C.ACCENT, self.theme.font_title, 2
                        else:
                            text_color = C.SUBTLE
                    rounded_rect(canvas, x0, y0, x1, y1, 7, fill=fill, outline=outline, width=line_width)
                    canvas.create_text((x0 + x1) / 2.0, (y0 + y1) / 2.0, text=str(7 - position),
                                       fill=text_color, font=font)
                    self.bit_hits.append((x0, y0, x1, y1, title, bit, indices))
            rows = (byte_count + per_row - 1) // per_row
            canvas.configure(scrollregion=(0, 0, 10 + per_row * (byte_width + byte_gap),
                                           16 + rows * row_height))

        def on_bit_resize(self, _event):
            """Re-flow the bytes after the canvas was resized (debounced while dragging)."""
            if self.current is None:
                return
            if self._bit_resize_job:
                self.root.after_cancel(self._bit_resize_job)
            self._bit_resize_job = self.root.after(50, self._redraw_bits_after_resize)

        def _redraw_bits_after_resize(self):
            self._bit_resize_job = None
            self.draw_bit_layout()

        def on_bit_motion(self, event):
            text = ''
            hit = self._bit_hit(event)
            if hit is not None:
                text = hit[4]
                if self._editable_message() and self.current[1] is not None and self.current[1] in hit[6]:
                    text += etr('  (drag: move, Shift+drag: resize)')
            self.bit_info.set(text)

        def draw_bus_structure(self):
            canvas = self.bus_canvas
            canvas.delete('all')
            if not self.bus_groups:
                canvas.create_text(12, 12, anchor=tk.NW, fill=C.MUTED,
                                   text=etr('The bus structure will appear after a database is loaded.'))
                canvas.configure(scrollregion=(0, 0, 600, 40))
                return

            selected_bus, senders, receivers = None, set(), set()
            if self.current is not None:
                message = self.messages[self.current[0]]
                selected_bus = message['bus'].strip() or bus_label()
                senders = set(ecu_names(message['senders']))
                receivers = set(ecu_names(message['receivers']))

            top = 8
            width_total = 0
            for group in self.bus_groups:
                base = top + 22
                bus_y = base + BUS_Y
                is_selected = group['bus'] == selected_bus
                canvas.create_text(10, top + 4, anchor=tk.NW, text=group['bus'],
                                   font=self.theme.font_title, fill=C.INK)
                canvas.create_line(4, bus_y, group['width'] - 4, bus_y,
                                   fill=C.ACCENT if is_selected else C.SUBTLE, width=4, capstyle=tk.ROUND)
                for index, (name, box_width, x) in enumerate(group['nodes']):
                    above = index % 2 == 0
                    node_y = base + (19 if above else BUS_LOWER_Y)
                    is_sender = is_selected and name in senders
                    is_receiver = is_selected and name in receivers
                    active = is_sender or is_receiver
                    line_start = node_y + BUS_NODE_HEIGHT if above else bus_y
                    line_end = bus_y if above else node_y
                    canvas.create_line(x, line_start, x, line_end,
                                       fill=C.ACCENT if active else C.LINE_STRONG, width=2)
                    sign = 1 if above else -1
                    if is_sender:
                        y0 = bus_y - 10 * sign
                        canvas.create_polygon(x - 5, y0, x + 5, y0, x, y0 + 9 * sign,
                                              fill=C.ACCENT, outline='')
                    if is_receiver:
                        y0 = node_y + (BUS_NODE_HEIGHT + 10 if above else -10)
                        canvas.create_polygon(x - 5, y0, x + 5, y0, x, y0 - 9 * sign,
                                              fill=C.ACCENT, outline='')
                    rounded_rect(canvas, x - box_width / 2.0, node_y, x + box_width / 2.0,
                                 node_y + BUS_NODE_HEIGHT, 8,
                                 fill=C.ACCENT_SOFT if active else C.SURFACE,
                                 outline=C.ACCENT if active else C.LINE_STRONG)
                    canvas.create_text(x, node_y + BUS_NODE_HEIGHT / 2.0, text=name, font=self.theme.font_label,
                                       fill=C.ACCENT_INK if active else C.INK)
                top = base + BUS_GROUP_HEIGHT + 12
                width_total = max(width_total, group['width'])
            canvas.configure(scrollregion=(0, 0, width_total, top))

        # ---------------------------------------------------------------- selection and state

        def _dirty(self, part):
            return part['source'] is not None and part['source'].text != part['saved_text']

        def _current_key(self):
            """What is shown, as a key that survives rebuilding the model: ('m', part, id),
            ('s', part, id, signal), ('eg', part) or ('e', part, variable)."""
            if self.current_env is not None:
                group, variable = self.current_env
                if group >= len(self.environment_groups):
                    return None
                _, _, variables, part = self.environment_groups[group]
                if variable is not None and variable < len(variables):
                    return ('e', part, variables[variable]['name'])
                return ('eg', part)
            if self.current is not None and self.current[0] < len(self.messages):
                message_index, signal_index = self.current
                message, part = self.messages[message_index], self.message_part[message_index]
                if signal_index is not None and signal_index < len(message['signals']):
                    return ('s', part, raw_id(message), message['signals'][signal_index]['name'])
                return ('m', part, raw_id(message))
            return None

        def _select_key(self, key):
            iid = self.key_iids.get(key)
            if iid is None and key[0] == 's':
                iid = self.key_iids.get(('m',) + key[1:3])
            if iid is None and key[0] == 'e':
                iid = self.key_iids.get(('eg', key[1]))
            if iid is None:
                self._show_nothing()
                return
            self.tree.selection_set(iid)
            self.tree.see(iid)

        def _selected(self):
            """(kind, part index) of the selection: kind is 'message', 'signal', 'env', 'env_group' or None."""
            if self.current_env is not None and self.current_env[0] < len(self.environment_groups):
                group, variable = self.current_env
                return ('env' if variable is not None else 'env_group'), self.environment_groups[group][3]
            if self.current is not None and self.current[0] < len(self.messages):
                message_index, signal_index = self.current
                return ('signal' if signal_index is not None else 'message'), self.message_part[message_index]
            return None, None

        def _default_part(self):
            """The editable file new messages and variables go to: the one of the selection, else the first."""
            _, part = self._selected()
            if part is not None:
                return part if self.parts[part]['editable'] else None
            return next((index for index, item in enumerate(self.parts) if item['editable']), None)

        def _editable_message(self):
            return (self.current is not None and self.current[0] < len(self.messages)
                    and self.parts[self.message_part[self.current[0]]]['editable'])

        def _update_edit_state(self):
            if not hasattr(self, 'edit_buttons'):
                return
            kind, part = self._selected()
            selection = part is not None and self.parts[part]['editable'] and kind in ('message', 'signal', 'env')
            target = self._default_part()
            dirty = [item['name'] for item in self.parts if self._dirty(item)]
            export_part = self.parts[part if part is not None else 0] if self.parts else None
            states = {'new': True, 'open': True, 'save': bool(dirty), 'save_copy': bool(self.parts),
                      'export_arxml': bool(export_part and export_part['messages']),
                      'undo': bool(self.undo_stack), 'redo': bool(self.redo_stack),
                      'new_message': target is not None, 'new_variable': target is not None,
                      'edit': selection, 'duplicate': selection, 'delete': selection,
                      'new_signal': selection and kind in ('message', 'signal')}
            for key, enabled in states.items():
                self.edit_buttons[key].state(['!disabled'] if enabled else ['disabled'])
            if dirty:
                text = etr('Unsaved changes: %s') % ', '.join(dirty)
            elif any(item['editable'] for item in self.parts):
                text = etr('No unsaved changes')
            else:
                text = etr('Read only: only DBC files can be edited') if self.parts else ''
            self.edit_info.set(text)
            self.multi_info.set(etr('%d databases loaded: changes apply to the file of the selection')
                                % len(self.parts) if len(self.parts) > 1 else '')

        # ---------------------------------------------------------------- editing

        def _reparse(self, part):
            part['messages'], part['environment'] = part['source'].parse()

        def _source(self, index):
            """DbcText of the file of a part, read when it is needed for the first time."""
            part = self.parts[index]
            if part['source'] is None:
                try:
                    source = DbcText(read_database_bytes(part['path']))
                    part['messages'], part['environment'] = source.parse()
                except (OSError, ValueError) as error:
                    messagebox.showerror(TITLE, etr('Could not read %s: %s') % (part['name'], error), parent=self.root)
                    return None
                part['source'], part['saved_text'] = source, source.text
                self._rebuild_model()
            return part['source']

        def _refresh(self, key=None):
            """Show the changed model again; key says what to select afterwards."""
            self._rebuild_model()
            self.populate_tree()
            self._show_nothing()
            if key:
                self._select_key(key)
            self._update_edit_state()

        def _edit(self, index, operation, select=None):
            """Apply operation(DbcText) to the file of the part. select: key (or function of the result of
            operation) of what to show afterwards. Returns None, or an error text when nothing was changed."""
            key = self._current_key()
            source = self._source(index)
            if source is None:
                return etr('The file could not be read.')
            part = self.parts[index]
            before = source.text
            try:
                result = operation(source)
                self._reparse(part)
            except ValueError as error:  # DbcEditError is a ValueError
                source.text = before
                try:
                    self._reparse(part)
                except ValueError:
                    pass
                return str(error)
            if source.text != before:
                self.undo_stack.append((index, before, source.text))
                self.redo_stack = []
            self._refresh(select(result) if callable(select) else (select or key))
            return None

        def _edit_reporting(self, index, operation, select=None):
            error = self._edit(index, operation, select)
            if error:
                messagebox.showerror(TITLE, error, parent=self.root)
            return error

        def _restore(self, index, text):
            key = self._current_key()
            source = self._source(index)
            if source is None:
                return
            source.text = text
            try:
                self._reparse(self.parts[index])
            except ValueError as error:
                messagebox.showerror(TITLE, str(error), parent=self.root)
                return
            self._refresh(key)

        def _in_entry(self):
            return isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry, tk.Text))

        def undo(self):
            if self.undo_stack and not self._in_entry():
                index, before, after = self.undo_stack.pop()
                self.redo_stack.append((index, before, after))
                self._restore(index, before)

        def redo(self):
            if self.redo_stack and not self._in_entry():
                index, before, after = self.redo_stack.pop()
                self.undo_stack.append((index, before, after))
                self._restore(index, after)

        def _write(self, path, data, backup=False):
            if backup and path not in self._backed_up and os.path.exists(path):
                shutil.copy2(path, path + '.bak')  # the file as it was before this session's first save
                self._backed_up.add(path)
            temporary = path + '.part'
            with open(temporary, 'wb') as handle:
                handle.write(data)
            os.replace(temporary, path)

        def save_all(self):
            dirty = [part for part in self.parts if self._dirty(part)]
            if not dirty:
                return True
            try:
                for part in dirty:
                    self._write(part['path'], part['source'].to_bytes(), backup=True)
                    part['saved_text'] = part['source'].text
            except (OSError, ValueError) as error:
                messagebox.showerror(TITLE, etr('Could not save: %s') % error, parent=self.root)
                self._update_edit_state()
                return False
            self.status.set(etr('Saved %s') % ', '.join(part['name'] for part in dirty))
            self._update_edit_state()
            if self.on_saved is not None:
                self.on_saved([part['path'] for part in dirty])
            return True

        def new_database(self):
            """Create an empty DBC file (asks for its name) and show it."""
            if not self.confirm_close():
                return
            path = filedialog.asksaveasfilename(
                parent=self.root, title=etr('New DBC file'), defaultextension='.dbc',
                initialdir=os.path.dirname(self.parts[0]['path']) if self.parts and self.parts[0]['path'] else None,
                filetypes=[('Vector DBC', '*.dbc'), (etr('All files'), '*.*')])
            if not path:
                return
            try:
                with open(path, 'wb') as handle:
                    handle.write(EMPTY_DBC)
            except OSError as error:
                messagebox.showerror(TITLE, etr('Could not create the file: %s') % error, parent=self.root)
                return
            self.status.set(etr('Created %s') % path)
            if self.on_new is not None:
                self.on_new(path)
            else:
                self.start_load(path)

        def open_database(self):
            """Load: the analyser opens its window 'Datenbasen…'; stand-alone a file dialog."""
            if self.on_open is not None:
                self.on_open()
                return
            if not self.confirm_close():
                return
            path = self.parts[0]['path'] if self.parts and self.parts[0]['path'] else None
            paths = filedialog.askopenfilenames(
                parent=self.root, title=etr('Load database'), initialdir=os.path.dirname(path) if path else None,
                filetypes=[(etr('CAN database'), '*.dbc *.arxml *.html *.htm'), (etr('All files'), '*.*')])
            if not paths:
                return
            self.start_load(paths[0])

        def save_copy(self, format_key='dbc'):
            if not self.parts:
                return
            _, index = self._selected()
            if index is None:
                index = 0
            part = self.parts[index]
            extension = '.' + format_key
            arxml = format_key == 'arxml'
            label = etr('Save as ARXML…') if arxml else etr('Save as DBC…')
            path = filedialog.asksaveasfilename(
                parent=self.root, title=label, defaultextension=extension,
                initialdir=os.path.dirname(part['path']) if part['path'] else None,
                initialfile=os.path.splitext(part['name'])[0] + extension,
                filetypes=[('AUTOSAR ARXML' if arxml else 'Vector DBC', '*' + extension)])
            if not path:
                return
            if not path.lower().endswith(extension):
                messagebox.showerror(TITLE, etr('Choose a file name ending in %s.') % extension, parent=self.root)
                return
            try:
                if os.path.exists(path) and any(item['path'] and os.path.exists(item['path'])
                                               and os.path.samefile(path, item['path']) for item in self.parts):
                    messagebox.showerror(TITLE, etr('Choose a different file name from the loaded databases.'),
                                         parent=self.root)
                    return
                if arxml:
                    data = export_arxml(part['messages'])
                elif part['editable']:
                    source = self._source(index)
                    if source is None:
                        return
                    data = source.to_bytes()
                else:
                    data = export_dbc(part['messages'], part['environment']).to_bytes()
                self._write(path, data)
            except (OSError, ValueError) as error:
                messagebox.showerror(TITLE, etr('Could not save: %s') % error, parent=self.root)
                return
            self.status.set(etr('Saved a copy as %s') % path)

        # ---------------------------------------------------------------- dialogs

        def _dialog(self, part, title, fields, apply, ok_text='OK'):
            if self.grouped:
                title += ' – %s' % self.parts[part]['name']
            self.dialog = FormDialog(self.root, self.theme, title, fields, apply, ok_text)

        def _free_id_text(self, part):
            numbers = [normalize_id(int(raw_id(message))) for message in self.parts[part]['messages']
                       if raw_id(message)]
            return '0x%X' % (max(numbers) + 1) if numbers else '0x100'

        def message_form(self, message=None, name=None, identifier=None):
            return [
                {'key': 'name', 'label': 'Name', 'value': name if name is not None else message['name']},
                {'key': 'id', 'label': 'CAN ID', 'value': identifier if identifier is not None
                 else id_input_text(message), 'hint': etr('Decimal or 0x…; above 0x7FF the ID is an extended ID.')},
                {'key': 'length', 'label': etr('Length (bytes)'), 'value': message['length'] if message else '8'},
                {'key': 'sender', 'label': 'Sender', 'value': message['senders'] if message else '',
                 'hint': etr('Name of the sending node; empty for none.')},
                {'key': 'description', 'label': etr('Description'), 'kind': 'text', 'height': 4,
                 'value': message['description'] if message else ''}]

        def new_message(self):
            part = self._default_part()
            if part is None:
                return

            def apply(values):
                return self._edit(part, lambda source: source.add_message(
                    {'id': values['id'], 'name': values['name'].strip(), 'length': values['length'].strip(),
                     'sender': values['sender'].strip(), 'description': values['description'].strip()}),
                    select=lambda identifier: ('m', part, identifier))
            self._dialog(part, etr('New message'), self.message_form(identifier=self._free_id_text(part), name=''),
                         apply, etr('Add'))

        def edit_message(self, message_index):
            message, part = self.messages[message_index], self.message_part[message_index]
            identifier = raw_id(message)
            form = self.message_form(message)
            original = dict((field['key'], field['value']) for field in form)

            def apply(values):
                fields = dict((key, values[key].strip()) for key in ('name', 'id', 'length', 'sender', 'description')
                              if values[key].strip() != original[key].strip())
                if 'sender' in fields and not fields['sender']:
                    fields['sender'] = 'Vector__XXX'
                return self._edit(part, lambda source: source.set_message(identifier, fields),
                                  select=lambda new_id: ('m', part, new_id))
            self._dialog(part, etr('Edit message %s') % message['name'], form, apply)

        def duplicate_message(self, message_index):
            message, part = self.messages[message_index], self.message_part[message_index]
            identifier = raw_id(message)

            def apply(values):
                return self._edit(part, lambda source: source.duplicate_message(
                    identifier, {'id': values['id'], 'name': values['name'].strip()}),
                    select=lambda new_id: ('m', part, new_id))
            fields = [{'key': 'name', 'label': 'Name', 'value': message['name'] + '_copy'},
                      {'key': 'id', 'label': 'CAN ID', 'value': self._free_id_text(part)}]
            self._dialog(part, etr('Duplicate message %s') % message['name'], fields, apply, etr('Duplicate'))

        def signal_form(self, signal=None, message=None):
            if signal is None:
                bits = signal_bits(message['signals'])
                signal = {'name': '', 'start_bit': str(max(bits) + 1 if bits else 0), 'length': '8',
                          'byte_order': 'Little endian', 'signedness': 'unsigned', 'factor': '1', 'offset': '0',
                          'minimum': '0', 'maximum': '0', 'unit': '', 'receivers': '', 'multiplex': '',
                          'description': '', 'values': []}
            return [
                {'key': 'name', 'label': 'Name', 'value': signal['name']},
                {'key': 'start_bit', 'label': etr('Start bit'), 'value': signal['start_bit'],
                 'hint': etr('Least significant bit (Intel) or most significant bit (Motorola).')},
                {'key': 'length', 'label': etr('Length (bits)'), 'value': signal['length']},
                {'key': 'order', 'label': etr('Byte order'), 'kind': 'combo', 'values': BYTE_ORDERS,
                 'value': BYTE_ORDERS[0 if signal['byte_order'].startswith('Little') else 1]},
                {'key': 'sign', 'label': etr('Signedness'), 'kind': 'combo', 'values': SIGNEDNESS,
                 'value': signal['signedness'] if signal['signedness'] in SIGNEDNESS else SIGNEDNESS[0]},
                {'key': 'factor', 'label': etr('Factor'), 'value': signal['factor']},
                {'key': 'offset', 'label': 'Offset', 'value': signal['offset']},
                {'key': 'minimum', 'label': 'Minimum', 'value': signal['minimum']},
                {'key': 'maximum', 'label': 'Maximum', 'value': signal['maximum']},
                {'key': 'unit', 'label': etr('Unit'), 'value': signal['unit']},
                {'key': 'receivers', 'label': etr('Receivers'), 'value': ', '.join(split_names(signal['receivers'])),
                 'hint': etr('Names of the receiving nodes, separated by commas.')},
                {'key': 'multiplex', 'label': 'Multiplexing', 'value': signal['multiplex'],
                 'hint': etr('Empty, M (multiplexer) or m<number> (e.g. m1).')},
                {'key': 'description', 'label': etr('Description'), 'kind': 'text', 'height': 2,
                 'value': signal['description']},
                {'key': 'values', 'label': etr('Values'), 'kind': 'text', 'height': 3,
                 'value': '\n'.join(value_lines(signal)), 'hint': etr('One value per line, e.g. 0 = Off')}]

        @staticmethod
        def signal_fields_from(values):
            """Fields of the form -> keys of DbcText.set_signal (values as typed, not yet checked)."""
            return {'name': values['name'].strip(), 'multiplex': values['multiplex'].strip(),
                    'start_bit': values['start_bit'].strip(), 'length': values['length'].strip(),
                    'order': '1' if values['order'] == BYTE_ORDERS[0] else '0',
                    'sign': '-' if values['sign'] == 'signed' else '+',
                    'factor': values['factor'].strip(), 'offset': values['offset'].strip(),
                    'minimum': values['minimum'].strip(), 'maximum': values['maximum'].strip(),
                    'unit': values['unit'], 'receivers': ', '.join(split_names(values['receivers'])),
                    'description': values['description'].strip(), 'values': parse_value_lines(values['values'])}

        def new_signal(self):
            kind, part = self._selected()
            if kind not in ('message', 'signal') or part is None:
                return
            message = self.messages[self.current[0]]
            identifier = raw_id(message)

            def apply(values):
                try:
                    fields = self.signal_fields_from(values)
                except DbcEditError as error:
                    return str(error)
                return self._edit(part, lambda source: source.add_signal(identifier, fields),
                                  select=lambda name: ('s', part, identifier, name))
            self._dialog(part, etr('New signal in %s') % message['name'], self.signal_form(message=message), apply, etr('Add'))

        def edit_signal(self, message_index, signal_index):
            message, part = self.messages[message_index], self.message_part[message_index]
            signal = message['signals'][signal_index]
            identifier, name = raw_id(message), signal['name']
            original = self.signal_fields_from(dict(
                (field['key'], field['value']) for field in self.signal_form(signal)))

            def apply(values):
                try:
                    fields = self.signal_fields_from(values)
                except DbcEditError as error:
                    return str(error)
                changed = dict((key, value) for key, value in fields.items() if original[key] != value)
                return self._edit(part, lambda source: source.set_signal(identifier, name, changed),
                                  select=lambda new_name: ('s', part, identifier, new_name))
            self._dialog(part, etr('Edit signal %s') % name, self.signal_form(signal), apply)

        def duplicate_signal(self, message_index, signal_index):
            message, part = self.messages[message_index], self.message_part[message_index]
            signal = message['signals'][signal_index]
            identifier = raw_id(message)

            def apply(values):
                def operation(source):
                    fields = source.get_signal(identifier, signal['name'])
                    fields.update(name=values['name'].strip(), description=signal['description'],
                                  values=[(item['value'], item['name']) for item in signal['values']])
                    return source.add_signal(identifier, fields, after=signal['name'])
                return self._edit(part, operation, select=lambda name: ('s', part, identifier, name))
            self._dialog(part, etr('Duplicate signal %s') % signal['name'],
                         [{'key': 'name', 'label': 'Name', 'value': signal['name'] + '_copy'}], apply, etr('Duplicate'))

        def environment_form(self, variable=None):
            variable = variable or {'name': '', 'type': 'integer', 'minimum': '0', 'maximum': '1', 'unit': '',
                                    'initial': '0', 'access': 'unrestricted', 'nodes': '', 'description': '',
                                    'values': {}}
            access = variable['access'].split(',')[0]
            return [
                {'key': 'name', 'label': 'Name', 'value': variable['name']},
                {'key': 'type', 'label': etr('Type'), 'kind': 'combo', 'values': ENV_TYPES,
                 'value': variable['type'] if variable['type'] in ENV_TYPES else ENV_TYPES[0]},
                {'key': 'minimum', 'label': 'Minimum', 'value': variable['minimum']},
                {'key': 'maximum', 'label': 'Maximum', 'value': variable['maximum']},
                {'key': 'unit', 'label': etr('Unit'), 'value': variable['unit']},
                {'key': 'initial', 'label': etr('Initial value'), 'value': variable['initial']},
                {'key': 'access', 'label': etr('Access'), 'kind': 'combo', 'values': ENV_ACCESS,
                 'value': access if access in ENV_ACCESS else ENV_ACCESS[0]},
                {'key': 'nodes', 'label': etr('Access nodes'), 'value': variable['nodes'],
                 'hint': etr('Names of the nodes that may access the variable, separated by commas.')},
                {'key': 'description', 'label': etr('Description'), 'kind': 'text', 'height': 3,
                 'value': variable['description']},
                {'key': 'values', 'label': etr('Values'), 'kind': 'text', 'height': 4,
                 'value': '\n'.join('%s = %s' % (js_str(value), text)
                                    for value, text in sorted((variable.get('values') or {}).items())),
                 'hint': etr('One value per line, e.g. 0 = Off')}]

        @staticmethod
        def environment_fields_from(values):
            return {'name': values['name'].strip(), 'type': str(ENV_TYPES.index(values['type'])),
                    'minimum': values['minimum'].strip(), 'maximum': values['maximum'].strip(),
                    'unit': values['unit'], 'initial': values['initial'].strip(),
                    'access': ENV_ACCESS.index(values['access']), 'nodes': ', '.join(split_names(values['nodes'])),
                    'description': values['description'].strip(), 'values': parse_value_lines(values['values'])}

        def new_variable(self):
            part = self._default_part()
            if part is None:
                return

            def apply(values):
                try:
                    fields = self.environment_fields_from(values)
                except DbcEditError as error:
                    return str(error)
                return self._edit(part, lambda source: source.add_environment(fields),
                                  select=lambda name: ('e', part, name))
            self._dialog(part, etr('New environment variable'), self.environment_form(), apply, etr('Add'))

        def edit_variable(self, group_index, variable_index):
            _, _, variables, part = self.environment_groups[group_index]
            variable = variables[variable_index]
            form = self.environment_form(variable)
            original = self.environment_fields_from(dict((field['key'], field['value']) for field in form))

            def apply(values):
                try:
                    fields = self.environment_fields_from(values)
                except DbcEditError as error:
                    return str(error)
                changed = dict((key, value) for key, value in fields.items() if original[key] != value)
                return self._edit(part, lambda source: source.set_environment(variable['name'], changed),
                                  select=lambda name: ('e', part, name))
            self._dialog(part, etr('Edit variable %s') % variable['name'], form, apply)

        def duplicate_variable(self, group_index, variable_index):
            _, _, variables, part = self.environment_groups[group_index]
            variable = variables[variable_index]

            def apply(values):
                return self._edit(part, lambda source: source.duplicate_environment(
                    variable['name'], values['name'].strip()), select=lambda name: ('e', part, name))
            self._dialog(part, etr('Duplicate variable %s') % variable['name'],
                         [{'key': 'name', 'label': 'Name', 'value': variable['name'] + '_copy'}], apply, etr('Duplicate'))

        def edit_selected(self):
            kind, part = self._selected()
            if part is None or not self.parts[part]['editable']:
                return
            if kind == 'message':
                self.edit_message(self.current[0])
            elif kind == 'signal':
                self.edit_signal(*self.current)
            elif kind == 'env':
                self.edit_variable(*self.current_env)

        # ---------------------------------------------------------------- copy and paste

        def on_tree_right_click(self, event):
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.selection_set(iid)
                self.on_tree_select()
            self.show_context_menu(event)
            return 'break'

        def on_table_right_click(self, event):
            self.table.on_click(event)
            self.show_context_menu(event)
            return 'break'

        def show_context_menu(self, event):
            kind, part = self._selected()
            editable = part is not None and self.parts[part]['editable']
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label=etr('Cut'), command=lambda: self.copy_selected(cut=True),
                             state=tk.NORMAL if editable and kind in ('message', 'signal', 'env') else tk.DISABLED)
            menu.add_command(label=etr('Copy'), command=self.copy_selected,
                             state=tk.NORMAL if kind in ('message', 'signal', 'env') else tk.DISABLED)
            menu.add_command(label=etr('Paste'), command=self.paste_clipboard,
                             state=tk.NORMAL if self._paste_target() is not None else tk.DISABLED)
            menu.add_command(label=etr('Delete'), command=self.delete_selected,
                             state=tk.NORMAL if editable and kind in ('message', 'signal', 'env') else tk.DISABLED)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        def copy_selected(self, cut=False):
            """Remember the selected signal or environment variable (all fields) for pasting;
            cut: the original is deleted when it is pasted."""
            kind, part = self._selected()
            if kind not in ('message', 'signal', 'env'):
                return
            source = self._source(part)
            if source is None:
                return
            try:
                if kind == 'message':
                    message = self.messages[self.current[0]]
                    identifier = raw_id(message)
                    fields = source.get_message(identifier)
                    fields.update(description=message['description'], signals=[])
                    for signal in message['signals']:
                        signal_fields = source.get_signal(identifier, signal['name'])
                        signal_fields.update(description=signal['description'],
                                             values=[(item['value'], item['name']) for item in signal['values']])
                        fields['signals'].append(signal_fields)
                    name = message['name']
                elif kind == 'signal':
                    message = self.messages[self.current[0]]
                    signal = message['signals'][self.current[1]]
                    fields = source.get_signal(raw_id(message), signal['name'])
                    fields.update(description=signal['description'],
                                  values=[(item['value'], item['name']) for item in signal['values']])
                    name, identifier = signal['name'], raw_id(message)
                else:
                    name = self.environment_groups[self.current_env[0]][2][self.current_env[1]]['name']
                    fields = source.get_environment(name)
                    identifier = None
            except (ValueError, IndexError) as error:
                messagebox.showerror(TITLE, str(error), parent=self.root)
                return
            if cut and not self.parts[part]['editable']:
                return
            self.clipboard_item = {'kind': kind, 'part': part, 'fields': fields, 'cut': cut,
                                   'identifier': identifier, 'name': name}

        def _paste_target(self):
            """(part, message index or None) where the clipboard would be pasted, else None."""
            item = self.clipboard_item
            kind, part = self._selected()
            if item is None or part is None or not self.parts[part]['editable']:
                return None
            if item['kind'] == 'signal':
                return (part, self.current[0]) if kind in ('message', 'signal') else None
            if item['kind'] == 'message' and item.get('cut') and item['part'] == part:
                return None  # moving a message within its file changes nothing
            return (part, None)

        @staticmethod
        def _copy_name(name, taken):
            """name + '_copy' (then _copy2, ...), not in taken."""
            result, number = name + '_copy', 2
            while result in taken:
                result, number = '%s_copy%d' % (name, number), number + 1
            return result

        def paste_clipboard(self):
            """Paste the copied signal into the selected message, or the variable into the file of the selection.
            In the file it was copied from the name gets the postfix _copy, in another file it stays."""
            target = self._paste_target()
            if target is None:
                return
            part, message_index = target
            item = self.clipboard_item
            fields = dict(item['fields'])
            same = item['part'] == part
            cut = item.get('cut')
            if cut:
                self.clipboard_item = None
            if item['kind'] == 'message':
                def operation(source):
                    if same:
                        return source.duplicate_message(item['identifier'], {
                            'id': self._free_id_text(part), 'name': self._copy_name(
                                fields['name'], [m['name'] for m in self.parts[part]['messages']])})
                    header = dict((key, fields[key]) for key in ('id', 'name', 'length', 'sender', 'description'))
                    identifier = source.add_message(header)
                    for signal in fields['signals']:
                        source.add_signal(identifier, signal)
                    return identifier
                select = lambda identifier: ('m', part, identifier)
                remove = lambda source: source.delete_message(item['identifier'])
            elif item['kind'] == 'signal':
                message = self.messages[message_index]
                identifier = raw_id(message)
                if cut and same:
                    def operation(source):
                        source.delete_signal(item['identifier'], item['name'])
                        return source.add_signal(identifier, fields)
                else:
                    if same:
                        fields['name'] = self._copy_name(fields['name'], [s['name'] for s in message['signals']])
                    operation = lambda source: source.add_signal(identifier, fields)
                select = lambda name: ('s', part, identifier, name)
                remove = lambda source: source.delete_signal(item['identifier'], item['name'])
            else:
                if cut and same:
                    def operation(source):
                        source.delete_environment(item['name'])
                        return source.add_environment(fields)
                else:
                    if same:
                        fields['name'] = self._copy_name(fields['name'], self.parts[part]['environment'])
                    operation = lambda source: source.add_environment(fields)
                select = lambda name: ('e', part, name)
                remove = lambda source: source.delete_environment(item['name'])
            if self._edit_reporting(part, operation, select=select) is None and cut and not same:
                self._edit_reporting(item['part'], remove, select=self._current_key())

        def duplicate_selected(self):
            kind, part = self._selected()
            if part is None or not self.parts[part]['editable']:
                return
            if kind == 'message':
                self.duplicate_message(self.current[0])
            elif kind == 'signal':
                self.duplicate_signal(*self.current)
            elif kind == 'env':
                self.duplicate_variable(*self.current_env)

        def delete_selected(self):
            kind, part = self._selected()
            if part is None or not self.parts[part]['editable']:
                return
            if kind in ('message', 'signal'):
                message = self.messages[self.current[0]]
                identifier = raw_id(message)
                if kind == 'message':
                    question = etr('Delete the message %s with its %d signals?') % (message['name'], len(message['signals']))
                    operation = lambda source: source.delete_message(identifier)
                else:
                    name = message['signals'][self.current[1]]['name']
                    question = etr('Delete the signal %s of the message %s?') % (name, message['name'])
                    operation = lambda source: source.delete_signal(identifier, name)
            elif kind == 'env':
                name = self.environment_groups[self.current_env[0]][2][self.current_env[1]]['name']
                question = etr('Delete the environment variable %s?') % name
                operation = lambda source: source.delete_environment(name)
            else:
                return
            if messagebox.askyesno(TITLE, question, parent=self.root):
                self._edit_reporting(part, operation)

        # ---------------------------------------------------------------- bit layout with the mouse

        def _bit_hit(self, event):
            x = self.bit_canvas.canvasx(event.x)
            y = self.bit_canvas.canvasy(event.y)
            for hit in self.bit_hits:
                if hit[0] <= x <= hit[2] and hit[1] <= y <= hit[3]:
                    return hit
            return None

        def on_bit_press(self, event):
            """A click selects the signal of the bit; dragging moves it (Shift: changes its length)."""
            self._bit_drag = None
            hit = self._bit_hit(event)
            if hit is None or self.current is None or not hit[6]:
                return
            message_index, selected = self.current
            index = selected if selected in hit[6] else hit[6][0]
            if index != selected:
                self.table.select(index)
                self.table.see(index)
                self.on_table_select(index)
            if not self._editable_message():
                return
            signal = self.messages[message_index]['signals'][index]
            start, length = as_whole(signal.get('start_bit')), as_whole(signal.get('length'))
            if start is None or length is None:
                return
            self._bit_drag = {'index': index, 'bit': hit[5], 'start': start, 'length': length,
                              'big': is_big_endian(signal), 'resize': bool(event.state & 0x1), 'result': None}

        def on_bit_drag(self, event):
            drag = self._bit_drag
            hit = self._bit_hit(event) if drag is not None else None
            if hit is None:
                return
            big = drag['big']
            if drag['resize']:
                start = drag['start']
                length = bit_order(hit[5], big) - bit_order(start, big) + 1
            else:
                start = bit_order(bit_order(drag['start'], big) + bit_order(hit[5], big) - bit_order(drag['bit'], big),
                                  big)
                length = drag['length']
            message = self.messages[self.current[0]]
            limit = byte_count_for(message, signal_bits(message['signals'])) * 8
            bits = signal_bit_numbers(start, length, big) if start >= 0 and length >= 1 else []
            if not bits or min(bits) < 0 or max(bits) >= limit:
                return
            if (start, length) != (drag['result'] or (drag['start'], drag['length'])):
                drag['result'] = (start, length)
                self.bit_preview = (drag['index'], start, length)
                self.draw_bit_layout()

        def on_bit_release(self, event):
            drag, self._bit_drag = self._bit_drag, None
            if drag is None or self.bit_preview is None:
                return
            self.bit_preview = None
            result = drag['result']
            if result is None or result == (drag['start'], drag['length']):
                self.draw_bit_layout()
                return
            message = self.messages[self.current[0]]
            part, identifier = self.message_part[self.current[0]], raw_id(message)
            name = message['signals'][drag['index']]['name']
            fields = {'start_bit': str(result[0]), 'length': str(result[1])}
            if self._edit_reporting(part, lambda source: source.set_signal(identifier, name, fields),
                                    select=('s', part, identifier, name)):
                self.draw_bit_layout()

        def theme_changed(self):
            """Redraw the drawings after a switch between light and dark mode."""
            self._configure_difference_tags()
            self.table.draw_body()
            self.draw_bit_layout()
            self.draw_bus_structure()

        def poll_placeholder(self):
            pass


def run_gui(initial_path=None):
    if tk is None:
        print('Error: tkinter is not available in this Python installation.', file=sys.stderr)
        return 1
    root = tk.Tk()
    apply_appearance(root)
    ViewerApp(root, initial_path)
    root.mainloop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Show a DBC, ARXML or HTML CAN database.')
    parser.add_argument('input', nargs='?', help='DBC, ARXML or HTML catalogue file to open on start')
    args = parser.parse_args(argv)
    return run_gui(args.input)


if __name__ == '__main__':
    sys.exit(main())
