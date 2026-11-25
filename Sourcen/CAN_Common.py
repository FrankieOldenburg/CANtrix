#!/usr/bin/env python3
"""Gemeinsame Teile von CAN_DatabaseEditor.py und CAN_TraceViewer.py.

- Parser für CAN-Datenbasen (DBC, ARXML, HTML-Message-Catalogue)
- Hilfsfunktionen für Zahlen und Texte (verhalten sich wie die JavaScript-Vorlagen)
- CAN-IDs und Bit-Layout von Signalen (Intel und Motorola)
- Einstellungsdateien sowie Fensterposition und hellen Modus für tkinter
- Einheitlicher, moderner Stil aller Fenster (Farben, Schriften, ttk-Styles, Kopfleiste, Karten, Statusleiste)

Kompatibel mit Python 3.6.
"""
import base64
import gzip
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk
except ImportError:
    tk = None

MAX_FILE_SIZE = 100 * 1024 * 1024
DATABASE_TYPES = ('.dbc', '.arxml', '.html', '.htm')


# ---------------------------------------------------------------- settings and window

def is_frozen():
    """True in the executable made with PyInstaller (batch files "03 - make exe_...")."""
    return bool(getattr(sys, 'frozen', False))


def relative_path(path, base_dir):
    """Path relative to base_dir (written with '/'), or the unchanged path when that is not possible (other drive)."""
    if not isinstance(path, str) or not path or not base_dir:
        return path
    try:
        return os.path.relpath(path, base_dir).replace(os.sep, '/')
    except ValueError:
        return path


def resolve_path(path, base_dir):
    """Absolute path of a (possibly relative) path from a configuration file in base_dir."""
    if not isinstance(path, str) or not path or not base_dir:
        return path
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path.replace('/', os.sep)))


def resource_dir():
    """Folder with README, Screenshots and Bilder: the project folder, in the executable the bundled data."""
    if is_frozen():
        # PyInstaller uses ``_MEIPASS`` for bundled resources, but the exact
        # value differs between one-file builds and macOS app bundles.  In an
        # app bundle the data files live in Contents/Resources; some PyInstaller
        # versions report Contents/Frameworks as _MEIPASS instead.
        candidates = [getattr(sys, '_MEIPASS', '')]
        executable_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.normpath(os.path.join(executable_dir, '..', 'Resources')))
        candidates.append(executable_dir)
        for candidate in candidates:
            if candidate and os.path.isdir(os.path.join(candidate, 'Bilder')):
                return candidate
        return candidates[0] or executable_dir
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def program_dir():
    """Folder of the user's files (Database, Traces, Panel, Plugins, configurations): the project folder, in
    the executable the folder next to it (on macOS next to the .app)."""
    if not is_frozen():
        return resource_dir()
    path = os.path.dirname(os.path.abspath(sys.executable))
    if sys.platform == 'darwin' and path.endswith(os.path.join('.app', 'Contents', 'MacOS')):
        path = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    return path


def load_settings(path):
    try:
        with open(path, encoding='utf-8') as handle:
            settings = json.load(handle)
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings, path):
    """Write the settings; return None on success or the OSError."""
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(settings, handle, indent=2)
        return None
    except OSError as error:
        return error


def initial_geometry(root, settings, default):
    """Last saved window size and position. The size is kept even when the position no longer fits on the
    screen (e.g. a maximised Windows window at -8,-8 or a monitor that is gone); unusable sizes (a minimised
    Windows window reports 160x28 at -32000,-32000) fall back to the default."""
    match = re.match(r'^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$', str(settings.get('geometry', '')))
    if match:
        width, height, x, y = (int(value) for value in match.groups())
        if width >= 300 and height >= 200:
            screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
            size = '%dx%d' % (min(width, screen_w), min(height, screen_h))
            if -width + 100 < x < screen_w - 100 and -8 <= y < screen_h - 100:
                return '%s+%d+%d' % (size, x, y)
            return size
    return default


class DockFrame(tk.Frame):
    """Stands in for a window (Tk/Toplevel) when a viewer is shown docked in the main window ('docked view'):
    the window methods do nothing, key bindings go to the main window while the frame is visible."""
    docked = True
    _FRAME_EVENTS = ('<Configure>', '<Map>', '<Unmap>', '<Destroy>', '<Visibility>', '<FocusIn>', '<FocusOut>',
                     '<Enter>', '<Leave>')

    def __init__(self, master):
        super().__init__(master, background=C.APP_BG)

    def title(self, text=None):
        """Title bar of the pane around the frame (if there is one)."""
        pane = getattr(self, 'pane', None)
        if text is not None and pane is not None:
            pane.title_label.configure(text=text)
        return ''

    def geometry(self, *args):
        return ''

    def minsize(self, *args):
        pass

    def maxsize(self, *args):
        pass

    def resizable(self, *args):
        pass

    def protocol(self, *args):
        pass

    def bind(self, sequence=None, func=None, add=None):
        if sequence and func is not None and sequence not in self._FRAME_EVENTS and not sequence.startswith('<<'):
            return self.winfo_toplevel().bind(
                sequence, lambda event: func(event) if self.winfo_ismapped() else None, '+')
        if sequence == '<Destroy>':
            add = '+'  # the pane around the frame also listens for it
        return super().bind(sequence, func, add)


class FloatingPane(tk.Frame):
    """Window inside another widget ('docked view'): title bar (move, maximise, close) and edges to resize.
    Placed with place() in `area`; its content goes into `body` (a DockFrame, which stands in for a window)."""
    MIN_WIDTH, MIN_HEIGHT = 360, 220
    GRIP, CORNER = 5, 12
    _raise_installed = set()

    def __init__(self, area, theme, title, on_close=None):
        super().__init__(area, background=C.LINE)
        self.area, self.on_close = area, on_close
        self.geo = (0, 0, 600, 400)  # x, y, width, height
        self.maximized = False
        self._restore = None
        inner = tk.Frame(self, background=C.SURFACE)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.bar = bar = tk.Frame(inner, background=C.SURFACE_ALT, height=26)
        bar.pack(side=tk.TOP, fill=tk.X)
        self.title_label = tk.Label(bar, text=title, background=C.SURFACE_ALT, foreground=C.INK,
                                    font=theme.font_label, anchor=tk.W)
        self.title_label.pack(side=tk.LEFT, padx=(10, 0), pady=3)
        for text, command in (('\u2715', self._close), ('\u25a1', self.toggle_maximize)):
            button = tk.Label(bar, text=text, background=C.SURFACE_ALT, foreground=C.MUTED, font=theme.font_label,
                              width=3, cursor='arrow')
            button.pack(side=tk.RIGHT, pady=3)
            button.bind('<Button-1>', lambda event, command=command: command())
            if text == '\u2715':
                self.close_button = button
        tk.Frame(inner, background=C.LINE, height=1).pack(side=tk.TOP, fill=tk.X)
        self.body = DockFrame(inner)
        self.body.pane = self
        self.body.pack(fill=tk.BOTH, expand=True)
        self.body.bind('<Destroy>', lambda event: self._body_destroyed(event))
        for widget in (bar, self.title_label):
            widget.bind('<ButtonPress-1>', self._move_start)
            widget.bind('<B1-Motion>', self._move)
            widget.bind('<Double-Button-1>', lambda event: self.toggle_maximize())
        self._grips()
        self._install_raise(area)

    # -- stacking: a click anywhere in a pane brings it to the front
    @classmethod
    def _install_raise(cls, area):
        top = area.winfo_toplevel()
        if str(top) in cls._raise_installed:
            return
        cls._raise_installed.add(str(top))

        def raise_pane(event):
            widget = event.widget
            while isinstance(widget, tk.Misc):
                if isinstance(widget, FloatingPane):
                    widget.tkraise()
                    return
                widget = widget.master
        top.bind_all('<ButtonPress-1>', raise_pane, '+')

    def _body_destroyed(self, event):
        if str(event.widget) == str(self.body):  # (for a destroyed widget event.widget may be its name)
            self.after_idle(self._destroy_self)

    def _destroy_self(self):
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _close(self):
        if self.on_close is not None:
            self.on_close()

    # -- geometry
    def area_size(self):
        self.area.update_idletasks()
        return max(self.area.winfo_width(), 300), max(self.area.winfo_height(), 200)

    def set_geometry(self, x, y, width, height):
        """Place the pane, kept inside the area (the title bar always stays reachable)."""
        area_w, area_h = self.area_size()
        width = max(self.MIN_WIDTH, min(int(width), area_w))
        height = max(self.MIN_HEIGHT, min(int(height), area_h))
        x = max(-(width - 80), min(int(x), area_w - 80))
        y = max(0, min(int(y), area_h - 30))
        self.geo = (x, y, width, height)
        self.place(x=x, y=y, width=width, height=height)

    def geometry_dict(self):
        x, y, width, height = self._restore if self.maximized and self._restore else self.geo
        return {'x': x, 'y': y, 'width': width, 'height': height, 'maximized': self.maximized}

    def apply_dict(self, state):
        """Geometry from geometry_dict(); None if the state is not usable."""
        try:
            values = [int(state[key]) for key in ('x', 'y', 'width', 'height')]
        except (KeyError, TypeError, ValueError):
            return False
        self.set_geometry(*values)
        if state.get('maximized') is True:
            self.toggle_maximize()
        return True

    def toggle_maximize(self):
        if self.maximized:
            self.maximized = False
            self.set_geometry(*(self._restore or self.geo))
        else:
            self._restore = self.geo
            area_w, area_h = self.area_size()
            self.maximized = True
            self.geo = (0, 0, area_w, area_h)
            self.place(x=0, y=0, width=area_w, height=area_h)
        self.tkraise()

    def refit(self):
        """The area changed its size: stay inside it (a maximised pane fills it)."""
        if self.maximized:
            area_w, area_h = self.area_size()
            self.geo = (0, 0, area_w, area_h)
            self.place(x=0, y=0, width=area_w, height=area_h)
        else:
            self.set_geometry(*self.geo)

    # -- moving and resizing
    def _move_start(self, event):
        self._drag = (event.x_root, event.y_root, self.geo)

    def _move(self, event):
        if self.maximized:
            return
        x_root, y_root, (x, y, width, height) = self._drag
        self.set_geometry(x + event.x_root - x_root, y + event.y_root - y_root, width, height)

    def _grips(self):
        grip, corner = self.GRIP, self.CORNER
        try:  # cursor names differ between systems
            aqua = self.tk.call('tk', 'windowingsystem') == 'aqua'
        except tk.TclError:
            aqua = False
        horizontal = 'resizeleftright' if aqua else 'sb_h_double_arrow'
        vertical = 'resizeupdown' if aqua else 'sb_v_double_arrow'
        diagonal_down = 'bottom_right_corner' if aqua else 'size_nw_se'
        diagonal_up = 'bottom_left_corner' if aqua else 'size_ne_sw'
        # (left, right, top, bottom edges moved), cursor, place arguments
        specs = (
            ((0, 1, 0, 0), horizontal, dict(relx=1.0, x=-grip, y=corner, relheight=1.0, height=-2 * corner, width=grip)),
            ((1, 0, 0, 0), horizontal, dict(x=0, y=corner, relheight=1.0, height=-2 * corner, width=grip)),
            ((0, 0, 0, 1), vertical, dict(rely=1.0, y=-grip, x=corner, relwidth=1.0, width=-2 * corner, height=grip)),
            ((0, 0, 1, 0), vertical, dict(x=corner, y=0, relwidth=1.0, width=-2 * corner, height=grip)),
            ((0, 1, 0, 1), diagonal_down, dict(relx=1.0, rely=1.0, x=-corner, y=-corner, width=corner, height=corner)),
            ((1, 0, 0, 1), diagonal_up, dict(rely=1.0, x=0, y=-corner, width=corner, height=corner)),
            ((0, 1, 1, 0), diagonal_up, dict(relx=1.0, x=-corner, y=0, width=corner, height=corner)),
            ((1, 0, 1, 0), diagonal_down, dict(x=0, y=0, width=corner, height=corner)),
        )
        for edges, cursor, arguments in specs:
            try:
                handle = tk.Frame(self, background=C.LINE, cursor=cursor)
            except tk.TclError:
                handle = tk.Frame(self, background=C.LINE)
            handle.place(**arguments)
            handle.bind('<ButtonPress-1>', self._move_start)
            handle.bind('<B1-Motion>', lambda event, edges=edges: self._resize(event, edges))

    def _resize(self, event, edges):
        if self.maximized:
            return
        x_root, y_root, (x, y, width, height) = self._drag
        dx, dy = event.x_root - x_root, event.y_root - y_root
        left, right, top, bottom = edges
        new_x, new_y, new_w, new_h = x, y, width, height
        if right:
            new_w = width + dx
        if bottom:
            new_h = height + dy
        if left:
            new_w = max(self.MIN_WIDTH, width - dx)
            new_x = x + width - new_w
        if top:
            new_h = max(self.MIN_HEIGHT, height - dy)
            new_y = y + height - new_h
        self.set_geometry(new_x, new_y, new_w, new_h)


def window_geometry(root):
    if getattr(root, 'docked', False):
        return ''  # a docked view has no window geometry of its own
    return '%dx%d+%d+%d' % (root.winfo_width(), root.winfo_height(), root.winfo_x(), root.winfo_y())


def apply_appearance(window):
    """Light or dark window frame on macOS, matching the current palette (Tk 8.6.10+)."""
    if window.tk.call('tk', 'windowingsystem') == 'aqua':
        try:
            window.update_idletasks()  # Tk 9 cannot read the appearance of an unmapped window
            window.tk.call('::tk::unsupported::MacWindowStyle', 'appearance', window,
                           'darkaqua' if C.dark else 'aqua')
        except Exception:  # tkinter.TclError on older Tk versions
            pass


# ---------------------------------------------------------------- common look (modern style)

# Light and dark palette: neutral slate surfaces with an indigo accent. Within one palette every
# colour must be unique, because switching recolours widgets by mapping old to new values.
LIGHT_PALETTE = {
    'INK': '#1e293b', 'MUTED': '#64748b', 'SUBTLE': '#94a3b8',
    'LINE': '#e2e8f0', 'LINE_STRONG': '#cbd5e1', 'GRID': '#eef2f6',
    'APP_BG': '#f1f5f9', 'SURFACE': '#ffffff', 'SURFACE_ALT': '#f8fafc',
    'ACCENT': '#4f46e5', 'ACCENT_HOVER': '#4338ca', 'ACCENT_INK': '#3730a3',
    'ACCENT_SOFT': '#eef2ff', 'ACCENT_SELECT': '#e0e7ff', 'ON_ACCENT': '#fdfdff',
    'SUCCESS': '#059669', 'WARNING': '#b45309', 'DANGER': '#dc2626',
}
DARK_PALETTE = {
    'INK': '#e2e8f0', 'MUTED': '#94a3b8', 'SUBTLE': '#64748b',
    'LINE': '#334155', 'LINE_STRONG': '#475569', 'GRID': '#2a3649',
    'APP_BG': '#0f172a', 'SURFACE': '#1e293b', 'SURFACE_ALT': '#243146',
    'ACCENT': '#6366f1', 'ACCENT_HOVER': '#818cf8', 'ACCENT_INK': '#c7d2fe',
    'ACCENT_SOFT': '#282f52', 'ACCENT_SELECT': '#353d72', 'ON_ACCENT': '#fdfdff',
    'SUCCESS': '#34d399', 'WARNING': '#fbbf24', 'DANGER': '#f87171',
}
# Line colours for signals in plots: distinct and readable on the surface colour.
LIGHT_SERIES = ('#4f46e5', '#ea580c', '#059669', '#9333ea', '#ca8a04', '#0891b2', '#db2777', '#65a30d')
DARK_SERIES = ('#818cf8', '#fb923c', '#34d399', '#c084fc', '#facc15', '#22d3ee', '#f472b6', '#a3e635')


class Palette(object):
    """Current colours, read at use time (C.INK, C.SURFACE, ...), so dark mode can switch at runtime."""

    def __init__(self):
        self.set(True)  # dark mode is the default

    def set(self, dark):
        self.dark = bool(dark)
        for key, value in (DARK_PALETTE if dark else LIGHT_PALETTE).items():
            setattr(self, key, value)
        self.SERIES_COLORS = DARK_SERIES if dark else LIGHT_SERIES


C = Palette()

FONT_SIZES = {  # name: (pixel size, weight)
    'axis': (-11, 'normal'), 'label': (-12, 'normal'), 'text': (-13, 'normal'),
    'heading': (-12, 'bold'), 'title': (-14, 'bold'), 'brand': (-17, 'bold'),
    'section': (-18, 'bold'), 'document': (-22, 'bold'),  # headings ## and # in the Info window
}


# ---- Symbole der Symbol-Buttons (Theme.icon_button), gemeinsam für Analyzer und Database Editor


def icon_hue(index):
    """Icon colour from the signal colours (index 1..7; 0 is skipped, it equals the accent colour)."""
    return C.SERIES_COLORS[index]


def icon_line(color, width=1.5):
    return dict(fill=color, width=width, capstyle='round', joinstyle='round', tags=('icon',))


def draw_new_icon(canvas, color):
    """Blank page with a folded corner and a plus."""
    line = icon_page(canvas, C.MUTED)
    accent = icon_line(C.SUCCESS, 2)
    canvas.create_line(15, 14.5, 15, 20.5, **accent)
    canvas.create_line(12, 17.5, 18, 17.5, **accent)


def draw_open_icon(canvas, color):
    """Open folder."""
    orange = icon_hue(1)
    canvas.create_line(8, 21, 8, 9, 13, 9, 15, 11, 21, 11, 21, 14, **icon_line(orange))
    canvas.create_polygon(8, 21, 11, 14, 24, 14, 21, 21, fill=orange, outline=orange, width=1.5,
                          joinstyle='round', tags=('icon',))


def draw_save_icon(canvas, color):
    """Floppy disk."""
    blue = icon_hue(5)
    canvas.create_polygon(8, 8, 19, 8, 22, 11, 22, 22, 8, 22, fill=C.SURFACE, outline=blue, width=1.5,
                          joinstyle='round', tags=('icon',))
    canvas.create_rectangle(11, 8, 18, 12, fill=blue, outline=blue, tags=('icon',))
    canvas.create_line(11, 22, 11, 16, 19, 16, 19, 22, **icon_line(blue))


def icon_page(canvas, color):
    """Page with a folded corner (base of the open icons); returns the line options."""
    line = icon_line(color)
    canvas.create_line(17, 7, 9, 7, 9, 23, 21, 23, 21, 11, 17, 7, 17, 11, 21, 11, **line)
    return line


def rounded_rect(canvas, x0, y0, x1, y1, radius, **options):
    """Rectangle with rounded corners on a Canvas (smoothed polygon)."""
    r = max(0, min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    points = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
              x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
    return canvas.create_polygon(points, smooth=True, **options)


def _shared_fonts(root):
    """Named fonts, created once per Tk interpreter and kept alive by the Tk root."""
    master = root._root()
    fonts = getattr(master, '_can_theme_fonts', None)
    if fonts is None:
        family = tkfont.nametofont('TkDefaultFont').actual('family')
        fonts = {name: tkfont.Font(root=master, name='CANtrix%s' % name.title(), family=family,
                                   size=size, weight=weight)
                 for name, (size, weight) in FONT_SIZES.items()}
        mono = tkfont.nametofont('TkFixedFont').actual('family')
        fonts['mono'] = tkfont.Font(root=master, name='CANtrixMono', family=mono, size=-12)
        master._can_theme_fonts = fonts
    return fonts


def _configure_style(root, fonts):
    """ttk styles shared by all windows (once per Tk interpreter and palette)."""
    master = root._root()
    if getattr(master, '_can_theme_styled', None) == C.dark:
        return
    master._can_theme_styled = C.dark
    style = ttk.Style(master)
    if 'clam' in style.theme_names():
        style.theme_use('clam')
    text, label, heading = fonts['text'], fonts['label'], fonts['heading']
    row_height = text.metrics('linespace') + 12

    def flat(color):
        return {'background': color, 'lightcolor': color, 'darkcolor': color}

    style.configure('.', background=C.SURFACE, foreground=C.INK, font=text, bordercolor=C.LINE_STRONG,
                    troughcolor=C.SURFACE, focuscolor=C.ACCENT, selectbackground=C.ACCENT_SELECT,
                    selectforeground=C.ACCENT_INK, insertcolor=C.INK, lightcolor=C.SURFACE, darkcolor=C.SURFACE)
    style.configure('TFrame', background=C.SURFACE)
    style.configure('TLabel', background=C.SURFACE, foreground=C.INK)

    # Buttons: flat with a thin border (clam draws no border for relief flat), soft accent on hover; no focus ring.
    for name in ('TButton', 'Accent.TButton', 'Toolbutton'):
        element = 'Toolbutton' if name == 'Toolbutton' else 'Button'
        style.layout(name, [('%s.border' % element, {'sticky': 'nswe', 'border': '1', 'children': [
            ('%s.padding' % element, {'sticky': 'nswe', 'children': [
                ('%s.label' % element, {'sticky': 'nswe'})]})]})])
    style.configure('TButton', padding=(12, 4), width=-4, anchor=tk.CENTER, relief=tk.RAISED, bordercolor=C.LINE_STRONG,
                    foreground=C.INK, **flat(C.SURFACE))
    style.map('TButton',
              background=[('disabled', C.SURFACE_ALT), ('pressed', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              lightcolor=[('disabled', C.SURFACE_ALT), ('pressed', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              darkcolor=[('disabled', C.SURFACE_ALT), ('pressed', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              bordercolor=[('disabled', C.LINE), ('active', C.ACCENT)],
              foreground=[('disabled', C.SUBTLE), ('active', C.ACCENT_INK)],
              relief=[('pressed', tk.RAISED)])
    style.configure('Accent.TButton', foreground=C.ON_ACCENT, bordercolor=C.ACCENT, **flat(C.ACCENT))
    style.map('Accent.TButton',
              background=[('disabled', C.LINE), ('pressed', C.ACCENT_INK), ('active', C.ACCENT_HOVER)],
              lightcolor=[('disabled', C.LINE), ('pressed', C.ACCENT_INK), ('active', C.ACCENT_HOVER)],
              darkcolor=[('disabled', C.LINE), ('pressed', C.ACCENT_INK), ('active', C.ACCENT_HOVER)],
              bordercolor=[('disabled', C.LINE), ('pressed', C.ACCENT_INK), ('active', C.ACCENT_HOVER)],
              foreground=[('disabled', C.SUBTLE)])
    # Segmented toggle buttons (radio buttons with style 'Toolbutton').
    style.configure('Toolbutton', padding=(12, 4), anchor=tk.CENTER, relief=tk.RAISED, bordercolor=C.LINE_STRONG,
                    foreground=C.MUTED, **flat(C.SURFACE))
    style.map('Toolbutton',
              background=[('disabled', C.SURFACE_ALT), ('selected', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              lightcolor=[('disabled', C.SURFACE_ALT), ('selected', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              darkcolor=[('disabled', C.SURFACE_ALT), ('selected', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              bordercolor=[('disabled', C.LINE), ('selected', C.ACCENT), ('active', C.ACCENT)],
              foreground=[('disabled', C.SUBTLE), ('selected', C.ACCENT_INK), ('active', C.ACCENT_INK)],
              relief=[('selected', tk.RAISED), ('pressed', tk.RAISED)])

    # Input fields: white, thin border, accent border with focus.
    for name in ('TEntry', 'TCombobox', 'TSpinbox'):
        style.configure(name, fieldbackground=C.SURFACE, foreground=C.INK, bordercolor=C.LINE_STRONG,
                        padding=(8, 4), arrowcolor=C.MUTED, arrowsize=12, **flat(C.SURFACE))
        style.map(name, bordercolor=[('focus', C.ACCENT)], lightcolor=[('focus', C.ACCENT_SOFT)],
                  darkcolor=[('focus', C.ACCENT_SOFT)], fieldbackground=[('readonly', C.SURFACE)],
                  background=[('active', C.ACCENT_SOFT)], arrowcolor=[('active', C.ACCENT)],
                  foreground=[('disabled', C.SUBTLE)])
    master.option_add('*TCombobox*Listbox.background', C.SURFACE)
    master.option_add('*TCombobox*Listbox.foreground', C.INK)
    master.option_add('*TCombobox*Listbox.selectBackground', C.ACCENT_SELECT)
    master.option_add('*TCombobox*Listbox.selectForeground', C.ACCENT_INK)
    master.option_add('*TCombobox*Listbox.font', text)

    # Tables and trees: no frame, roomy rows, soft selection, quiet headings.
    style.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
    style.configure('Treeview', background=C.SURFACE, fieldbackground=C.SURFACE, foreground=C.INK, font=text,
                    rowheight=row_height, borderwidth=0, relief=tk.FLAT)
    style.map('Treeview', background=[('selected', C.ACCENT_SELECT)], foreground=[('selected', C.ACCENT_INK)])
    style.configure('Treeview.Heading', font=heading, foreground=C.MUTED, padding=(8, 6), relief=tk.FLAT,
                    bordercolor=C.LINE, background=C.SURFACE_ALT, lightcolor=C.SURFACE_ALT, darkcolor=C.LINE)
    style.map('Treeview.Heading', background=[('active', C.ACCENT_SOFT)], foreground=[('active', C.ACCENT_INK)],
              lightcolor=[('active', C.ACCENT_SOFT)])

    # Slim scrollbars without arrows.
    for orient, sticky in (('Vertical', 'ns'), ('Horizontal', 'ew')):
        style.layout('%s.TScrollbar' % orient, [('%s.Scrollbar.trough' % orient, {'sticky': sticky, 'children': [
            ('%s.Scrollbar.thumb' % orient, {'expand': '1', 'sticky': 'nswe'})]})])
    style.configure('TScrollbar', troughcolor=C.SURFACE, bordercolor=C.SURFACE, arrowsize=9, gripcount=0,
                    relief=tk.FLAT, borderwidth=0, **flat(C.LINE_STRONG))
    style.map('TScrollbar', background=[('pressed', C.MUTED), ('active', C.SUBTLE)],
              lightcolor=[('pressed', C.MUTED), ('active', C.SUBTLE)], darkcolor=[('pressed', C.MUTED), ('active', C.SUBTLE)])

    # Check boxes: accent when ticked.
    style.configure('TCheckbutton', background=C.SURFACE, foreground=C.INK, indicatorbackground=C.SURFACE,
                    indicatorforeground=C.ON_ACCENT, upperbordercolor=C.LINE_STRONG,
                    lowerbordercolor=C.LINE_STRONG, padding=(0, 4), focuscolor=C.SURFACE)
    style.map('TCheckbutton', background=[('active', C.SURFACE)],
              indicatorbackground=[('selected', C.ACCENT), ('active', C.ACCENT_SOFT)],
              upperbordercolor=[('selected', C.ACCENT), ('active', C.ACCENT)],
              lowerbordercolor=[('selected', C.ACCENT), ('active', C.ACCENT)])

    # Pill-shaped tabs.
    style.layout('TNotebook.Tab', [('Notebook.tab', {'sticky': 'nswe', 'children': [
        ('Notebook.padding', {'side': 'top', 'sticky': 'nswe', 'children': [
            ('Notebook.label', {'side': 'top', 'sticky': ''})]})]})])
    style.configure('TNotebook', borderwidth=0, tabmargins=(0, 0, 0, 6),
                    bordercolor=C.SURFACE, **flat(C.SURFACE))
    style.configure('TNotebook.Tab', padding=(14, 5), font=label, foreground=C.MUTED, bordercolor=C.SURFACE,
                    **flat(C.SURFACE))
    style.map('TNotebook.Tab',
              background=[('selected', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              lightcolor=[('selected', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              darkcolor=[('selected', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              bordercolor=[('selected', C.ACCENT_SELECT), ('active', C.ACCENT_SOFT)],
              foreground=[('selected', C.ACCENT_INK), ('active', C.ACCENT_INK)],
              padding=[('selected', (14, 5))], expand=[('selected', (0, 0, 0, 0))])

    # Progress bars: accent bar in a soft trough.
    style.configure('Horizontal.TProgressbar', troughcolor=C.GRID, bordercolor=C.LINE, thickness=8,
                    **flat(C.ACCENT))
    # Sliders (e.g. time in the Panel Viewer): accent handle on a soft trough.
    style.configure('Horizontal.TScale', troughcolor=C.GRID, bordercolor=C.LINE, sliderthickness=12,
                    sliderlength=18, troughrelief=tk.FLAT, **flat(C.ACCENT))
    scale_states = [('disabled', C.LINE_STRONG), ('pressed', C.ACCENT_INK), ('active', C.ACCENT_HOVER)]
    style.map('Horizontal.TScale', background=scale_states, lightcolor=scale_states, darkcolor=scale_states,
              bordercolor=[('disabled', C.LINE)])

    # Split views: the gap between cards is the window background.
    style.configure('TPanedwindow', background=C.APP_BG)
    style.configure('Sash', sashthickness=10, gripcount=0, gripsize=0, bordercolor=C.APP_BG, **flat(C.APP_BG))
    # Split view inside a card.
    style.configure('Card.TPanedwindow', background=C.SURFACE)


class Theme(object):
    """Fonts and building blocks shared by all windows: header bar, cards and status bar."""

    def __init__(self, root):
        fonts = _shared_fonts(root)
        self.font_axis = fonts['axis']
        self.font_label = fonts['label']
        self.font_text = fonts['text']
        self.font_heading = fonts['heading']
        self.font_title = fonts['title']
        self.font_brand = fonts['brand']
        self.font_section = fonts['section']
        self.font_document = fonts['document']
        self.font_mono = fonts['mono']
        _configure_style(root, fonts)
        root.configure(background=C.APP_BG)

    def header(self, root, title, subtitle='', subtitle_variable=None):
        """Light top bar with logo and window name; returns the frame for extra widgets."""
        if getattr(root, 'docked', False) and subtitle_variable is None:
            return tk.Frame(root)  # docked: the title bar of the pane names the window, nothing else to show
        outer = tk.Frame(root, background=C.SURFACE)
        outer.pack(side=tk.TOP, fill=tk.X)
        if getattr(root, 'docked', False):  # docked in the main window: no logo and name (the title bar shows it)
            header = tk.Frame(outer, background=C.SURFACE)
            header.pack(side=tk.TOP, fill=tk.X, padx=16, pady=6)
            tk.Frame(outer, background=C.LINE, height=1).pack(side=tk.TOP, fill=tk.X)
            if subtitle_variable is not None:  # only the changing information (fixed subtitles are left out)
                tk.Label(header, textvariable=subtitle_variable, background=C.ACCENT_SOFT,
                         foreground=C.ACCENT_INK, font=self.font_label, padx=10, pady=3).pack(side=tk.RIGHT)
            return header
        header = tk.Frame(outer, background=C.SURFACE)
        header.pack(side=tk.TOP, fill=tk.X, padx=16, pady=10)
        tk.Frame(outer, background=C.LINE, height=1).pack(side=tk.TOP, fill=tk.X)

        logo = tk.Canvas(header, width=30, height=30, background=C.SURFACE, highlightthickness=0)
        rounded_rect(logo, 1, 1, 29, 29, 8, fill=C.ACCENT, outline='')
        logo.create_line(7, 19, 11, 19, 11, 11, 15, 11, 15, 19, 19, 19, 19, 11, 23, 11,
                         fill=C.ON_ACCENT, width=2, capstyle=tk.ROUND, joinstyle=tk.ROUND)
        logo.pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(header, text=title, background=C.SURFACE, foreground=C.INK,
                 font=self.font_brand).pack(side=tk.LEFT)
        if subtitle or subtitle_variable is not None:
            tk.Label(header, text=subtitle, textvariable=subtitle_variable, background=C.ACCENT_SOFT,
                     foreground=C.ACCENT_INK, font=self.font_label, padx=10, pady=3).pack(side=tk.RIGHT)
        return header

    def footer(self, root, variable):
        """Status bar at the bottom; returns the frame for extra labels."""
        outer = tk.Frame(root, background=C.SURFACE)
        outer.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Frame(outer, background=C.LINE, height=1).pack(side=tk.TOP, fill=tk.X)
        footer = tk.Frame(outer, background=C.SURFACE)
        footer.pack(side=tk.TOP, fill=tk.X, padx=8)
        dot = tk.Canvas(footer, width=8, height=8, background=C.SURFACE, highlightthickness=0)
        dot.create_oval(1, 1, 7, 7, fill=C.SUCCESS, outline='')
        dot.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(footer, textvariable=variable, background=C.SURFACE, foreground=C.MUTED, font=self.font_label,
                 anchor=tk.W).pack(side=tk.LEFT, padx=6, pady=5)
        return footer

    def footer_label(self, footer, variable):
        label = tk.Label(footer, textvariable=variable, background=C.SURFACE, foreground=C.MUTED,
                         font=self.font_label)
        label.pack(side=tk.RIGHT, padx=8)
        return label

    def pane(self, parent, title='', variable=None):
        """White card with a thin border and a title row; returns (card, title row)."""
        outer = tk.Frame(parent, background=C.SURFACE, highlightthickness=1, highlightbackground=C.LINE,
                         highlightcolor=C.LINE)
        bar = tk.Frame(outer, background=C.SURFACE, height=36)
        bar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(4, 2))
        tk.Label(bar, text=title, textvariable=variable, background=C.SURFACE, foreground=C.INK,
                 font=self.font_title).pack(side=tk.LEFT, padx=(8, 8), pady=4)
        tk.Frame(outer, background=C.LINE, height=1).pack(side=tk.TOP, fill=tk.X)
        return outer, bar

    def bind_tooltip(self, widget, tooltip):
        """Show text (or the result of a callable) while the pointer is over a widget."""
        if not tooltip:
            return
        pending = tip = None

        def show():
            nonlocal pending, tip
            pending = None
            text = tooltip() if callable(tooltip) else tooltip
            if not text:
                return
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tk.Label(tip, text=text, background=C.INK, foreground=C.SURFACE, font=self.font_label,
                     justify=tk.LEFT, padx=8, pady=3).pack()
            tip.wm_geometry('+%d+%d' % (widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + 6))

        def schedule(_event):
            nonlocal pending
            if pending is None and tip is None:
                pending = widget.after(500, show)

        def hide(_event):
            nonlocal pending, tip
            if pending is not None:
                widget.after_cancel(pending)
                pending = None
            if tip is not None:
                tip.destroy()
                tip = None

        widget.bind('<Enter>', schedule, add='+')
        for event in ('<Leave>', '<ButtonPress-1>', '<Destroy>'):
            widget.bind(event, hide, add='+')

    def icon_button(self, parent, draw, command, tooltip='', size=30, active=None):
        """Flat square button with a drawn icon and a tooltip (text or function returning text).
        draw(canvas, color) draws the icon in a size x size area; it is redrawn for hover and click.
        The virtual event <<IconRefresh>> redraws it, e.g. when draw shows a state that has changed.
        active(): optional function; while it returns True the button is highlighted (like a selected toggle).
        canvas.state(['disabled'] / ['!disabled']) switches the button off and on."""
        canvas = tk.Canvas(parent, width=size, height=size, background=C.SURFACE, highlightthickness=0,
                           cursor='hand2')
        rounded_rect(canvas, 1, 1, size - 1, size - 1, 7, fill=C.SURFACE, outline=C.LINE_STRONG, tags=('bg',))
        state = {'inside': False, 'pressed': False, 'disabled': False}

        def paint():
            if state['disabled']:
                fill, outline, ink = C.SURFACE, C.LINE, C.SUBTLE
            elif state['pressed']:
                fill, outline, ink = C.ACCENT_SELECT, C.ACCENT, C.ACCENT_INK
            elif state['inside']:
                fill, outline, ink = C.ACCENT_SOFT, C.ACCENT, C.ACCENT_INK
            elif active is not None and active():
                fill, outline, ink = C.ACCENT_SELECT, C.ACCENT, C.ACCENT_INK
            else:
                fill, outline, ink = C.SURFACE, C.LINE_STRONG, C.MUTED
            canvas.itemconfigure('bg', fill=fill, outline=outline)
            canvas.delete('icon')
            draw(canvas, ink)
            if state['disabled']:  # not executable: the whole icon grey (interiors stay the button colour)
                for item in canvas.find_withtag('icon'):
                    for option in ('fill', 'outline'):
                        try:
                            value = str(canvas.itemcget(item, option))
                        except tk.TclError:
                            continue
                        if value and value.lower() != C.SURFACE.lower():
                            canvas.itemconfigure(item, **{option: C.SUBTLE})

        def enter(_event):
            state['inside'] = True
            if not state['disabled']:
                paint()

        def leave(_event):
            state['inside'] = state['pressed'] = False
            if not state['disabled']:
                paint()

        def press(_event):
            if state['disabled']:
                return
            state['pressed'] = True
            paint()

        def release(event):
            clicked = state['pressed'] and 0 <= event.x < size and 0 <= event.y < size
            state['pressed'] = False
            paint()
            if clicked:
                command()

        canvas.bind('<Enter>', enter)
        canvas.bind('<Leave>', leave)
        canvas.bind('<ButtonPress-1>', press)
        canvas.bind('<ButtonRelease-1>', release)
        self.bind_tooltip(canvas, tooltip)
        canvas.bind('<<IconRefresh>>', lambda _event: paint())

        def set_state(flags):
            state['disabled'] = 'disabled' in flags
            if state['disabled']:
                state['pressed'] = False
                canvas.configure(cursor='')
            else:
                canvas.configure(cursor='hand2')
            paint()

        canvas.state = set_state
        paint()
        return canvas

    def bar_label(self, bar, variable):
        """Muted info text in a card's title row."""
        label = tk.Label(bar, textvariable=variable, background=C.SURFACE, foreground=C.MUTED,
                         font=self.font_label)
        label.pack(side=tk.LEFT)
        return label


COLOR_OPTIONS = ('background', 'foreground', 'highlightbackground', 'highlightcolor', 'insertbackground',
                 'selectbackground', 'selectforeground', 'activebackground', 'activeforeground', 'fill', 'outline')


def _recolor_widget(widget, mapping):
    """Replace old palette colours by new ones in a widget, its tags and canvas items."""
    def swap(get, put, option):
        try:
            value = str(get(option)).lower()
        except (tk.TclError, ValueError):
            return
        if value in mapping:
            put(**{option: mapping[value]})

    for option in COLOR_OPTIONS:
        swap(widget.cget, widget.configure, option)
    if isinstance(widget, tk.Canvas):
        for item in widget.find_all():
            for option in ('fill', 'outline'):
                swap(lambda name, item=item: widget.itemcget(item, name),
                     lambda item=item, **kw: widget.itemconfigure(item, **kw), option)
    elif isinstance(widget, tk.Text):
        for tag in widget.tag_names():
            for option in ('foreground', 'background'):
                swap(lambda name, tag=tag: widget.tag_cget(tag, name),
                     lambda tag=tag, **kw: widget.tag_configure(tag, **kw), option)
    elif isinstance(widget, ttk.Treeview):
        try:
            tags = widget.tk.splitlist(widget.tk.call(widget._w, 'tag', 'names'))
        except tk.TclError:  # Tk before 8.6
            tags = ()
        for tag in tags:
            for option in ('foreground', 'background'):
                swap(lambda name, tag=tag: widget.tag_configure(tag, name),
                     lambda tag=tag, **kw: widget.tag_configure(tag, **kw), option)
    for child in widget.winfo_children():
        _recolor_widget(child, mapping)


def is_dark_mode():
    return C.dark


def set_dark_mode(root, dark):
    """Switch the palette of all windows in this Tk interpreter. Windows keep their content;
    canvases with drawings should be redrawn afterwards (they read the colours from C)."""
    dark = bool(dark)
    if dark == C.dark:
        return
    old = DARK_PALETTE if C.dark else LIGHT_PALETTE
    new = DARK_PALETTE if dark else LIGHT_PALETTE
    mapping = dict((old[key].lower(), new[key]) for key in old)
    old_series = DARK_SERIES if C.dark else LIGHT_SERIES
    mapping.update(zip(old_series, DARK_SERIES if dark else LIGHT_SERIES))
    C.set(dark)
    master = root._root()
    if hasattr(master, '_can_theme_fonts'):
        _configure_style(master, master._can_theme_fonts)
    _recolor_widget(master, mapping)
    for window in [master] + [w for w in _all_widgets(master) if isinstance(w, tk.Toplevel)]:
        apply_appearance(window)


def _all_widgets(widget):
    for child in widget.winfo_children():
        yield child
        for grandchild in _all_widgets(child):
            yield grandchild


# ---------------------------------------------------------------- JS-compatible helpers

def decode_text(data):
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('cp1252', errors='replace')


def js_number(text):
    value = str(text).strip()
    if value == '':
        return 0.0
    if re.match(r'^0[xX][0-9a-fA-F]+$', value):
        return float(int(value[2:], 16))
    if re.match(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$', value):
        return float(value)
    if value in ('Infinity', '+Infinity'):
        return float('inf')
    if value == '-Infinity':
        return float('-inf')
    return float('nan')


def js_str(number):
    if math.isnan(number):
        return 'NaN'
    if math.isinf(number):
        return 'Infinity' if number > 0 else '-Infinity'
    if number == int(number) and abs(number) < 1e21:
        return str(int(number))
    return re.sub(r'e([+-])0*(\d)', r'e\1\2', repr(number))


def format_number(text):
    return js_str(float('%.12g' % js_number(text)))


def id_text(identifier):
    if not identifier:
        return ''
    number = js_number(identifier)
    hex_part = '%X' % int(number) if math.isfinite(number) else 'NAN'
    return '%s (0x%s)' % (identifier, hex_part)


def ordered_unique(items):
    return list(dict.fromkeys(items))


def ecu_names(text):
    return [name.strip() for name in text.split(',') if name.strip()]


def display(value):
    return '—' if value is None or value == '' else str(value)


def as_whole(text):
    if text is None or text == '':
        return None
    number = js_number(text)
    if math.isfinite(number) and number == int(number):
        return int(number)
    return None


# ---------------------------------------------------------------- DBC

DBC_MESSAGE_RE = re.compile(r'^BO_\s+(\d+)\s+([^:\s]+)\s*:\s*(\d+)\s+(\S+)')
DBC_SIGNAL_RE = re.compile(
    r'^\s*SG_\s+(\S+?)(?:\s+(m\d+|M))?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*\(([-+\d.eE]+),\s*([-+\d.eE]+)\)'
    r'\s*\[([-+\d.eE]+)\|([-+\d.eE]+)\]\s*"([^"]*)"\s*(.*)$')
DBC_COMMENT_MESSAGE_RE = re.compile(r'^CM_\s+BO_\s+(\d+)\s+"((?:[^"\\]|\\.)*)";', re.M)
DBC_COMMENT_SIGNAL_RE = re.compile(r'^CM_\s+SG_\s+(\d+)\s+(\S+)\s+"((?:[^"\\]|\\.)*)";', re.M)
DBC_VALUE_RE = re.compile(r'^VAL_\s+(\d+)\s+(\S+)\s+((?:[^;"\r\n]|"[^"\r\n]*")+?)\s*;', re.M)
DBC_VALUE_PAIR_RE = re.compile(r'(-?\d+)\s+"([^"]*)"')
# Environment variables: EV_ name: type [min|max] "unit" initial id access nodes;
DBC_ENV_RE = re.compile(r'^EV_\s+([^:\s]+)\s*:\s*(\d+)\s*\[\s*([-+\d.eE]+)\s*\|\s*([-+\d.eE]+)\s*\]\s*"([^"]*)"'
                        r'\s*([-+\d.eE]+)?(?:\s+(\d+))?(?:\s+DUMMY_NODE_VECTOR([0-9a-fA-F]+))?[ \t]*([^;\n]*)', re.M)
DBC_ENV_COMMENT_RE = re.compile(r'^CM_\s+EV_\s+(\S+)\s+"((?:[^"\\]|\\.)*)";', re.M)
DBC_ENV_VALUE_RE = re.compile(r'^VAL_\s+([A-Za-z_]\w*)\s+(-?\d(?:[^;"\r\n]|"[^"\r\n]*")*?)\s*;', re.M)  # VAL_ without message ID


def unescape_dbc(text):
    return text.replace('\\"', '"').replace('\\\\', '\\')


def parse_dbc(source, source_name):
    messages = []
    by_identifier = {}
    owners = []
    current = None
    current_identifier = None

    for line in re.split(r'\r?\n', source):
        message_match = DBC_MESSAGE_RE.match(line)
        if message_match:
            identifier, name, length, sender = message_match.groups()
            current = {'name': name, 'id': id_text(identifier), 'length': length, 'bus': '',
                       'senders': sender, 'receivers': '', 'description': '', 'signals': []}
            current_identifier = identifier
            messages.append(current)
            by_identifier[identifier] = current
            continue

        signal_match = DBC_SIGNAL_RE.match(line)
        if signal_match and current is not None:
            (name, multiplex, start_bit, length, byte_order, sign, factor, offset,
             minimum, maximum, unit, receivers) = signal_match.groups()
            little_endian = byte_order == '1'
            byte_order_text = 'Little endian' if little_endian else 'Big endian'
            signal = {'name': name, 'position': 'Bit ' + start_bit, 'start_bit': start_bit,
                      'length': length, 'data_type': byte_order_text, 'byte_order': byte_order_text,
                      'signedness': 'signed' if sign == '-' else 'unsigned', 'factor': factor,
                      'offset': offset, 'minimum': minimum, 'maximum': maximum, 'unit': unit,
                      'computation': '', 'receivers': receivers.strip(), 'description': '',
                      'multiplex': multiplex or '', 'values': []}
            current['signals'].append(signal)
            owners.append((current_identifier, signal))

    if not messages and not re.search(r'^\s*(?:BS_|BU_)\s*:', source, re.M):  # an empty DBC still has these
        raise ValueError('No BO_ messages were found in the DBC file "%s".' % source_name)

    for message in messages:
        names = [name for signal in message['signals']
                 for name in re.split(r'\s+', signal['receivers']) if name]
        message['receivers'] = ', '.join(ordered_unique(names))

    lookup = {}
    for identifier, signal in owners:
        lookup[(identifier, signal['name'])] = signal

    for match in DBC_COMMENT_MESSAGE_RE.finditer(source):
        message = by_identifier.get(match.group(1))
        if message is not None:
            message['description'] = unescape_dbc(match.group(2))
    for match in DBC_COMMENT_SIGNAL_RE.finditer(source):
        signal = lookup.get((match.group(1), match.group(2)))
        if signal is not None:
            signal['description'] = unescape_dbc(match.group(3))
    for match in DBC_VALUE_RE.finditer(source):
        signal = lookup.get((match.group(1), match.group(2)))
        if signal is not None:
            signal['values'] = [{'value': value, 'name': name}
                                for value, name in DBC_VALUE_PAIR_RE.findall(match.group(3))]
    return messages


ENV_TYPES = {'0': 'integer', '1': 'float', '2': 'string', '3': 'data'}
ENV_ACCESS = {0: 'unrestricted', 1: 'read', 2: 'write', 3: 'read/write'}  # DUMMY_NODE_VECTOR<n>


def parse_dbc_environment(source):
    """Environment variables of a DBC (EV_), e.g. the I/O of a VN1600 interface.
    Returns {name: {'name', 'type', 'minimum', 'maximum', 'unit', 'initial', 'access', 'nodes', 'description',
    'values'}}; 'values' maps raw values (float) to their names (VAL_ name value "text" ...;), 'nodes' is the
    comma separated list of access nodes."""
    variables = {}
    for match in DBC_ENV_RE.finditer(source):
        name, kind, minimum, maximum, unit, initial, _, access, nodes = match.groups()
        access_text = ''
        if access:
            code = int(access, 16)
            access_text = ENV_ACCESS.get(code & 0x7FFF, access)
            if code & 0x8000 and kind != '2':
                access_text += ', string'  # bit 15: the variable is a string
        nodes = ', '.join(node for node in ecu_names(nodes or '') if node != 'Vector__XXX')
        variables[name] = {'name': name, 'type': ENV_TYPES.get(kind, kind), 'minimum': minimum,
                           'maximum': maximum, 'unit': unit, 'initial': initial or '', 'access': access_text,
                           'nodes': nodes, 'description': '', 'values': {}}
    for match in DBC_ENV_COMMENT_RE.finditer(source):
        if match.group(1) in variables:
            variables[match.group(1)]['description'] = unescape_dbc(match.group(2))
    for match in DBC_ENV_VALUE_RE.finditer(source):
        if match.group(1) in variables:
            variables[match.group(1)]['values'] = dict(
                (float(value), text) for value, text in DBC_VALUE_PAIR_RE.findall(match.group(2)))
    return variables


# ---------------------------------------------------------------- ARXML

ARXML_KINDS = ('CAN-FRAME', 'CAN-FRAME-TRIGGERING', 'FRAME-PORT', 'I-SIGNAL-I-PDU',
               'I-SIGNAL', 'SYSTEM-SIGNAL', 'COMPU-METHOD', 'DATA-CONSTR', 'UNIT')


def local_name(tag):
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def children_named(element, name):
    if element is None:
        return []
    return [child for child in element if local_name(child.tag) == name]


def child_named(element, name):
    found = children_named(element, name)
    return found[0] if found else None


def descendants_named(element, name):
    if element is None:
        return []
    return [node for node in element.iter() if node is not element and local_name(node.tag) == name]


def first_descendant(element, name):
    if element is None:
        return None
    for node in element.iter():
        if node is not element and local_name(node.tag) == name:
            return node
    return None


def element_text(element):
    return ''.join(element.itertext()).strip() if element is not None else ''


def first_text(element, name):
    return element_text(first_descendant(element, name))


def segment_after(path, marker):
    parts = path.split('/')
    if marker not in parts:
        return ''
    index = parts.index(marker)
    return parts[index + 1] if index + 1 < len(parts) else ''


def en_description(element):
    for node in children_named(child_named(element, 'DESC'), 'L-2'):
        if node.get('L') == 'EN':
            return element_text(node)
    return ''


def index_arxml(root):
    index = {kind: {} for kind in ARXML_KINDS}
    stack = [(root, '')]
    while stack:
        element, prefix = stack.pop()
        short_name = child_named(element, 'SHORT-NAME')
        path = prefix + '/' + element_text(short_name) if short_name is not None else prefix
        kind = local_name(element.tag)
        if kind in index:
            index[kind][path or '/'] = element
        stack.extend((child, path) for child in reversed(list(element)))
    return index


def parse_arxml(data, source_name, progress=None):
    """progress(fraction) is called now and then with the share already done (0..1)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError('"%s" is not a valid ARXML file.' % source_name)
    if progress:
        progress(0.15)
    elements = index_arxml(root)
    if progress:
        progress(0.35)
    triggerings = list(elements['CAN-FRAME-TRIGGERING'].items())
    step = max(1, len(triggerings) // 100)

    def lookup(kind, reference):
        return elements[kind].get(reference) if reference else None

    messages = []
    for number, (triggering_path, triggering) in enumerate(triggerings):
        if progress and number % step == 0:
            progress(0.35 + 0.65 * number / len(triggerings))
        frame = lookup('CAN-FRAME', first_text(triggering, 'FRAME-REF'))
        if frame is None:
            continue
        identifier = first_text(triggering, 'IDENTIFIER')

        senders, receivers = [], []
        port_refs = [element_text(node) for node in descendants_named(triggering, 'FRAME-PORT-REF')]
        for reference in port_refs:
            ecu = segment_after(reference, 'EcuInstances')
            if not ecu:
                continue
            direction = (first_text(lookup('FRAME-PORT', reference), 'COMMUNICATION-DIRECTION')
                         or ('OUT' if reference.endswith('_OUT') else 'IN' if reference.endswith('_IN') else ''))
            if direction == 'OUT':
                senders.append(ecu)
            elif direction == 'IN':
                receivers.append(ecu)

        candidates = [first_text(triggering, 'PDU-TRIGGERING-REF'), triggering_path] + port_refs
        bus_path = next((path for path in candidates if segment_after(path, 'CommunicationClusters')), '')
        bus = segment_after(bus_path, 'CommunicationClusters')

        pdu = lookup('I-SIGNAL-I-PDU', first_text(frame, 'PDU-REF'))
        signals = []
        for mapping in descendants_named(pdu, 'I-SIGNAL-TO-I-PDU-MAPPING'):
            signal = lookup('I-SIGNAL', first_text(mapping, 'I-SIGNAL-REF'))
            if signal is None:
                continue
            system = lookup('SYSTEM-SIGNAL', first_text(signal, 'SYSTEM-SIGNAL-REF'))
            compu = lookup('COMPU-METHOD', first_text(system, 'COMPU-METHOD-REF'))
            unit_ref = first_text(compu, 'UNIT-REF')
            unit = first_text(lookup('UNIT', unit_ref), 'DISPLAY-NAME') or unit_ref.split('/')[-1]
            constraints = first_descendant(lookup('DATA-CONSTR', first_text(system, 'DATA-CONSTR-REF')),
                                           'PHYS-CONSTRS')
            base_type = first_text(signal, 'BASE-TYPE-REF').split('/')[-1]
            packing = first_text(mapping, 'PACKING-BYTE-ORDER')
            start_bit = first_text(mapping, 'START-POSITION')

            factor, offset = '1', '0'
            linear = False
            values = []
            for scale in descendants_named(compu, 'COMPU-SCALE'):
                constant = first_descendant(scale, 'COMPU-CONST')
                if constant is not None:
                    values.append({'value': element_text(child_named(scale, 'LOWER-LIMIT')),
                                   'name': first_text(constant, 'VT')})
                    continue
                coeffs = first_descendant(scale, 'COMPU-RATIONAL-COEFFS')
                if not linear and coeffs is not None:
                    numerator = [js_number(element_text(node)) for node in
                                 children_named(first_descendant(coeffs, 'COMPU-NUMERATOR'), 'V')]
                    denominator = js_number(first_text(coeffs, 'COMPU-DENOMINATOR'))
                    if not denominator or math.isnan(denominator):
                        denominator = 1.0
                    if len(numerator) == 2:
                        offset = js_str(numerator[0] / denominator)
                        factor = js_str(numerator[1] / denominator)
                        linear = True

            packing_text = ('big endian' if packing == 'MOST-SIGNIFICANT-BYTE-FIRST'
                            else 'little endian' if packing == 'MOST-SIGNIFICANT-BYTE-LAST' else '')
            signedness = ('unsigned' if re.match(r'u', base_type, re.I)
                          else 'signed' if re.match(r's?int', base_type, re.I) else '')
            signals.append({
                'name': element_text(child_named(signal, 'SHORT-NAME')),
                'position': 'Bit ' + start_bit,
                'start_bit': start_bit,
                'length': first_text(signal, 'LENGTH'),
                'data_type': base_type.upper(),
                'byte_order': packing_text,
                'signedness': signedness,
                'factor': format_number(factor),
                'offset': format_number(offset),
                'minimum': element_text(child_named(constraints, 'LOWER-LIMIT')),
                'maximum': element_text(child_named(constraints, 'UPPER-LIMIT')),
                'unit': unit,
                'computation': '',
                'receivers': '',
                'description': en_description(system),
                'multiplex': '',
                'values': values,
            })

        message = {
            'name': element_text(child_named(frame, 'SHORT-NAME')),
            'id': id_text(identifier),
            'length': first_text(frame, 'FRAME-LENGTH'),
            'bus': bus,
            'senders': ', '.join(ordered_unique(senders)),
            'receivers': ', '.join(ordered_unique(receivers)),
            'description': en_description(lookup('I-SIGNAL-I-PDU', first_text(frame, 'PDU-REF'))),
            'signals': signals,
        }
        mode = first_text(triggering, 'CAN-ADDRESSING-MODE')
        if mode:
            message['extended'] = mode == 'EXTENDED'
        behavior = first_text(triggering, 'CAN-FRAME-TX-BEHAVIOR')
        if behavior:
            message['fd'] = behavior == 'CAN-FD'
        messages.append(message)

    if not messages:
        raise ValueError('No CAN messages were found in "%s".' % source_name)
    return messages


# ---------------------------------------------------------------- HTML catalogue

VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
             'param', 'source', 'track', 'wbr'}
COMPRESSED_RE = re.compile(r'\bconst\s+compressed\s*=\s*["\']([^"\']+)["\']')
FRAME_START_RE = re.compile(r'<section\b([^>]*)>', re.I)
CLASS_ATTR_RE = re.compile(r'\sclass\s*=\s*(["\'])(.*?)\1', re.I)


class Node(object):
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children = []

    def classes(self):
        return self.attrs.get('class', '').split()

    def elements(self):
        return [child for child in self.children if isinstance(child, Node)]


class TreeBuilder(HTMLParser):
    """Small DOM builder that mirrors the browser's implicit table handling."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = Node('#document', {}, None)
        self.stack = [self.root]

    def _close_to(self, tags, stop):
        for index in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[index].tag
            if tag in tags:
                del self.stack[index:]
                return
            if tag in stop:
                return

    def _open(self, tag, attrs):
        node = Node(tag, {name: value or '' for name, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._close_to(('tr',), ('table', 'tbody', 'thead', 'tfoot'))
            if self.stack[-1].tag == 'table':
                self._open('tbody', [])
        elif tag in ('td', 'th'):
            self._close_to(('td', 'th'), ('tr', 'table', 'tbody', 'thead', 'tfoot'))
        elif tag in ('tbody', 'thead', 'tfoot'):
            self._close_to(('tbody', 'thead', 'tfoot'), ('table',))
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def text_of(node):
    if node is None:
        return ''
    if isinstance(node, str):
        return node
    return ''.join(text_of(child) for child in node.children)


def normalize(node):
    return re.sub(r'\s+', ' ', text_of(node)).strip()


def iter_descendants(node):
    if node is None:
        return
    stack = list(reversed(node.elements()))
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.elements()))


def select(node, predicate):
    return [item for item in iter_descendants(node) if predicate(item)]


def first(node, predicate):
    return next((item for item in iter_descendants(node) if predicate(item)), None)


def ancestors(node):
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def has_ancestor(node, predicate):
    return any(predicate(item) for item in ancestors(node))


def tag_is(name):
    return lambda node: node.tag == name


def has_class(name):
    return lambda node: name in node.classes()


def closest(node, tag):
    return next((item for item in ancestors(node) if item.tag == tag), None)


def next_element_sibling(node):
    if node.parent is None:
        return None
    siblings = node.parent.children
    position = next(i for i, child in enumerate(siblings) if child is node)
    return next((child for child in siblings[position + 1:] if isinstance(child, Node)), None)


def within_table_tbody(row, table):
    for ancestor in ancestors(row):
        if ancestor is table:
            return False
        if ancestor.tag == 'tbody':
            return True
    return False


def table_rows(table):
    rows = {}
    if table is None:
        return rows
    for row in select(table, tag_is('tr')):
        cells = select(row, lambda node: node.tag in ('th', 'td'))
        label = normalize(cells[0]) if cells else ''
        data_cell = next((cell for cell in cells if cell.tag == 'td'), None)
        value = normalize(data_cell) if data_cell is not None else ''
        if label and value:
            rows[re.sub(r':$', '', label)] = value
    return rows


def frame_ecus(frame, role):
    ecus = []
    for heading in select(frame, has_class('table-heading')):
        if normalize(heading) != role:
            continue
        table = next_element_sibling(heading)
        if table is None:
            continue
        for row in select(table, lambda node: node.tag == 'tr' and within_table_tbody(node, table)):
            cells = select(row, tag_is('td'))
            ecu = normalize(cells[1]) if len(cells) > 1 else ''
            if ecu and ecu not in ecus:
                ecus.append(ecu)
    return ', '.join(ecus)


def computation_offset(computation):
    """Offset of a computation text of the catalogue, e.g. 'PH = (DEC × 0.5 - 127.0)' -> -127.0 (None if absent)."""
    match = re.search(r'DEC\s*(?:[×x*]\s*[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)?\s*([-−+])\s*(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)',
                      computation or '')
    if not match:
        return None
    value = float(match.group(2))
    return value if match.group(1) == '+' else -value


def parse_frame(frame):
    heading = first(frame, lambda node: node.tag == 'h3' and has_ancestor(node, has_class('first-nav-header')))
    spans = [normalize(span) for span in select(heading, tag_is('span'))]
    label_index = next((i for i, text in enumerate(spans) if text.startswith('Frame:')), -1)
    name = None
    if 0 <= label_index + 1 < len(spans):
        name = spans[label_index + 1]
    elif 0 <= label_index < len(spans):
        name = re.sub(r'^Frame:\s*', '', spans[label_index])
    name = name or ''
    if not name:
        return None

    frame_rows = table_rows(first(frame, has_class('frame-attributestable')))
    identifier = frame_rows.get('Identifier', '')
    id_match = re.search(r',\s*(\d+)\s*\((0x[0-9a-f]+)\)', identifier, re.I)
    message_id = '%s (%s)' % (id_match.group(1), id_match.group(2)) if id_match else identifier
    bus_match = re.match(r'\s*([^,]+)', identifier)

    signals = []
    for heading_node in select(frame, tag_is('h4')):
        label_text = normalize(first(heading_node, has_class('element-type')))
        if not label_text.startswith('Signal:'):
            continue
        signal_name = next((text for text in (normalize(span) for span in select(heading_node, tag_is('span')))
                            if text != label_text), '')
        section = closest(heading_node, 'section')
        if not signal_name or section is None:
            continue

        details = table_rows(first(section, has_class('signal-type-details')))
        data_type = details.get('Data type', '')
        length_match = re.search(r'(\d+)\s*Bit', data_type, re.I)
        position = details.get('Position', '')
        position_match = re.search(r'Byte\s+(\d+),\s*Bit\s+(\d+)', position, re.I)
        range_values = re.findall(r'[-+]?\d+(?:\.\d+)?', details.get('Range', ''))

        unit_cell = None
        for row in select(section, lambda node: node.tag == 'tr' and has_ancestor(node, has_class('signal-type-details'))):
            if normalize(first(row, tag_is('th'))) == 'Unit':
                unit_cell = first(row, tag_is('td'))
                break
        unit_span = first(unit_cell, tag_is('span'))

        values = []
        for row in select(section, lambda node: node.tag == 'tr' and has_ancestor(
                node, lambda anc: anc.tag == 'tbody' and has_ancestor(anc, has_class('signal-datatype-enum-details')))):
            cells = select(row, tag_is('td'))
            value = normalize(cells[0]) if cells else ''
            value_name = normalize(cells[1]) if len(cells) > 1 else ''
            if value and value_name:
                values.append({'value': value, 'name': value_name})

        description = normalize(first(section, lambda node: has_class('description')(node)
                                      and has_ancestor(node, has_class('descriptions-signal'))))
        signedness = ('unsigned' if re.search(r'\bUINT|\bUNSIGNED', data_type, re.I)
                      else 'signed' if re.search(r'\bINT|\bSIGNED', data_type, re.I) else '')
        computation = details.get('Computation', '')
        offset = computation_offset(computation)
        signals.append({
            'name': signal_name,
            'position': position,
            'start_bit': str(int(position_match.group(1)) * 8 + int(position_match.group(2)))
            if position_match else '',
            'length': length_match.group(1) if length_match else '',
            'data_type': data_type,
            'byte_order': '',
            'signedness': signedness,
            'factor': details.get('Scaling', ''),
            'offset': format_number(offset) if offset is not None else '',
            'minimum': range_values[0] if range_values else '',
            'maximum': range_values[1] if len(range_values) > 1 else '',
            'unit': text_of(unit_span).strip() if unit_span is not None else '',
            'computation': computation,
            'receivers': '',
            'description': description,
            'multiplex': '',
            'values': values,
        })

    return {
        'name': name,
        'id': message_id,
        'length': frame_rows.get('Length (byte)', ''),
        'bus': bus_match.group(1).strip() if bus_match else '',
        'senders': frame_ecus(frame, 'Sender'),
        'receivers': frame_ecus(frame, 'Receiver'),
        'description': '',
        'signals': signals,
    }


def unpack_catalogue(text):
    match = COMPRESSED_RE.search(text)
    if match:
        try:
            raw = base64.b64decode(match.group(1))
            return gzip.decompress(raw).decode('utf-8', errors='replace')
        except (ValueError, OSError):
            raise ValueError('The compressed HTML data is invalid.')
    if not re.search(r'<html\b', text, re.I):
        raise ValueError('The file does not contain an HTML database.')
    return text


def parse_catalogue_document(source, source_name, progress=None):
    """progress(fraction) is called after every frame with the share already done (0..1)."""
    starts = []
    for match in FRAME_START_RE.finditer(source):
        class_match = CLASS_ATTR_RE.search(match.group(1))
        if class_match and 'frame' in class_match.group(2).split():
            starts.append(match.start())

    messages = []
    ends = starts[1:] + [len(source)]
    for number, (begin, end) in enumerate(zip(starts, ends)):
        if progress:
            progress(float(number) / len(starts))
        builder = TreeBuilder()
        builder.feed(source[begin:end])
        builder.close()
        frame_nodes = builder.root.elements()
        if not frame_nodes:
            continue
        message = parse_frame(frame_nodes[0])
        if message is not None:
            messages.append(message)

    if not messages:
        raise ValueError('No CAN messages were found in "%s".' % source_name)
    return messages


# ---------------------------------------------------------------- loading

def parse_database(data, file_name, progress=None):
    """progress(fraction): optional, called now and then with the share already done (0..1)."""
    extension = os.path.splitext(file_name)[1].lower()
    if extension == '.dbc':
        return parse_dbc(decode_text(data), file_name)  # fast, no progress needed
    if extension == '.arxml':
        return parse_arxml(data, file_name, progress)
    if extension in ('.html', '.htm'):
        return parse_catalogue_document(unpack_catalogue(decode_text(data)), file_name, progress)
    raise ValueError('Please select a file in DBC, ARXML or HTML format.')


def read_database_bytes(path):
    """Check type and size of a database file and return its raw content."""
    file_name = os.path.basename(path)
    if os.path.splitext(file_name)[1].lower() not in DATABASE_TYPES:
        raise ValueError('Please select a file in DBC, ARXML or HTML format.')
    if os.path.getsize(path) > MAX_FILE_SIZE:
        raise ValueError('The file is larger than 100 MB.')
    with open(path, 'rb') as handle:
        return handle.read()


def load_database_file(path):
    return parse_database(read_database_bytes(path), os.path.basename(path))


# ---------------------------------------------------------------- CAN IDs and signal layout

def normalize_id(identifier):
    """Strip the extended-frame flag (bit 31) from a CAN ID."""
    return identifier & 0x1FFFFFFF if identifier > 0x1FFFFFFF else identifier


def database_id(message):
    """Numeric CAN ID of a parsed database message, e.g. from '1320 (0x528)'."""
    text = str(message.get('id') or '')
    match = re.search(r'0x([0-9a-f]+)', text, re.I)
    if match:
        return normalize_id(int(match.group(1), 16))
    match = re.match(r'\s*(\d+)', text)
    return normalize_id(int(match.group(1))) if match else None


def combine_databases(parts):
    """One database from several: parts is a list of (database, channel) with database as read by
    read_database ({'name', 'path', 'messages', 'environment'}) and channel the CAN channel it belongs to
    ('1', …) or None for all channels. Every message is a copy with the keys 'database' (file name) and
    'channel'. Environment variables: the first database that defines one wins. 'databases' lists the
    single databases (name, path, channel, messages, environment) in their order."""
    messages, environment, databases = [], {}, []
    for database, channel in parts:
        own = []
        for message in database.get('messages') or []:
            copy = dict(message)
            copy['database'] = database.get('name', '')
            copy['channel'] = channel
            own.append(copy)
        messages.extend(own)
        for name, variable in (database.get('environment') or {}).items():
            environment.setdefault(name, variable)
        databases.append({'name': database.get('name', ''), 'path': database.get('path'), 'channel': channel,
                          'messages': own, 'environment': database.get('environment') or {}})
    return {'name': ', '.join(item['name'] for item in databases),
            'path': databases[0]['path'] if databases else None,
            'messages': messages, 'environment': environment, 'databases': databases}


class MessageIndex(object):
    """Database message of a CAN ID. A message of a database assigned to a CAN channel ('channel' of the
    message, see combine_databases) only counts for frames of that channel and wins there over messages
    of databases for all channels; otherwise the first database wins."""

    def __init__(self, messages):
        self.by_channel, self.general = {}, {}
        for message in messages or ():
            identifier = database_id(message)
            if identifier is None:
                continue
            channel = message.get('channel')
            if channel:
                self.by_channel.setdefault((str(channel), identifier), message)
            else:
                self.general.setdefault(identifier, message)

    def find(self, identifier, channel=None):
        """Message for a frame with this ID on this channel (None: channel unknown, only general ones)."""
        identifier = normalize_id(identifier)
        if channel is not None:
            message = self.by_channel.get((str(channel), identifier))
            if message is not None:
                return message
        return self.general.get(identifier)

    def find_any(self, identifier):
        """Message for an ID regardless of channels (e.g. live frames whose channel is not known)."""
        identifier = normalize_id(identifier)
        message = self.general.get(identifier)
        if message is None:
            message = next((item for (channel, key), item in self.by_channel.items() if key == identifier), None)
        return message


def is_big_endian(signal):
    return bool(re.search(r'big[\s_-]*endian|motorola', '%s %s' % (
        signal.get('byte_order') or '', signal.get('data_type') or ''), re.I))


def signal_bit_numbers(start, length, big_endian):
    """Bit numbers of a signal from its first to its last bit.

    Intel counts upwards from the start bit (LSB). Motorola starts at the MSB; bit numbers
    count 0..7 from the LSB of each byte and continue with the MSB (bit 7) of the next byte.
    """
    bits = []
    bit = start
    for _ in range(length):
        bits.append(bit)
        if big_endian:
            bit = bit + 15 if bit % 8 == 0 else bit - 1
        else:
            bit += 1
    return bits
