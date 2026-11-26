#!/usr/bin/env python3
"""CANtrix.

Hauptfenster mit Menü und "Status & Zusammenfassung". Nur hier werden die CAN-Datenbasis, der
CAN-Trace und das Panel geöffnet und einmal eingelesen; die gelesenen Daten gehen an alle Fenster: Database Editor,
Trace Viewer (mit Trace Recorder) und Panel Viewer laufen als Unterfenster und verwenden immer dieselben Dateien.
Datenbasis, Trace, Panel und Fenstereinstellungen lassen sich als
Konfiguration (.canconfig) speichern und wieder laden; die Unterfenster speichern selbst nichts.

Start: python3 CANtrix.py [KONFIGURATION] [-d DATENBASIS] [-t TRACE] [-p PANEL]
Kompatibel mit Python 3.6. Der Trace Recorder benötigt zusätzlich python-can.
"""
import argparse
import copy
import json
import os
import re
import queue
import sys
import tempfile
import threading
import time

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None

from CAN_Common import (C, FloatingPane, MessageIndex, Theme, apply_appearance, draw_new_icon, draw_open_icon, draw_save_icon,
                        icon_hue, icon_line, icon_page, initial_geometry, is_dark_mode, load_settings, program_dir, relative_path, resolve_path, resource_dir, save_settings, set_dark_mode, window_geometry)
from CAN_Generator import normalize_entries
from CAN_Info import (VERSION, AboutWindow, InfoWindow, can_self_update, check_update, download_source,
                      install_update, open_file, remove_old_updates, restart_program)
from CAN_Panel import DEFAULT_SPEED, SPEEDS, bound_count, read_panel
from CAN_Language import LANGUAGES, keep_language, language, load_language, set_language, set_text, tr
from CAN_Plugins import PluginConfigWindow, PluginRun, build_command, normalize_plugins, script_path
from CAN_RecorderSettings import (BITRATES as RECORDER_BITRATES, DATA_BITRATES as RECORDER_DATA_BITRATES,
                                  INTERFACES as RECORDER_INTERFACES, TIMING_FIELDS, normalize_connection,
                                  vector_fd_timing)
from CAN_TraceViewer import DASH, SectionWindow, absolute_time_text, de_int, natural_key, trace_channels
from CAN_Traces import TraceConfigWindow, config_traces, load_traces, normalize_traces, trace_summary
from CAN_VectorIO import VectorIODialog, create_vector_io_database, normalize_vector_io
from CAN_TraceWriters import write_trace
from CAN_Databases import (DatabaseConfigWindow, config_databases, database_summary, load_databases,
                           normalize_databases)

SETTINGS_PATH = os.path.join(os.path.expanduser('~'), '.cantrix.json')
CONFIG_FORMAT = 'CANtrix'
CONFIG_FORMATS = (CONFIG_FORMAT, 'CAN Analyzer', 'CAN Analyser')  # accept existing configurations
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = program_dir()  # Projektordner (die Skripte liegen in Sourcen/), in der exe der Ordner daneben
TIME_SLIDER_LENGTH = 450# time bar of the Panel Viewer in the toolbar (pixels)
MAIN_SIZE = (1260, 750)  # default size of the main window ("Status & Zusammenfassung")
RECENT_LIMIT = 5  # zuletzt verwendete Konfigurationen im Menü Datei
DEFAULT_CHANNELS = [str(number) for number in range(1, 9)]  # CAN-Kanäle zur Auswahl ohne geladenen Trace
STATUS_COLORS = {'ok': 'SUCCESS', 'warn': 'WARNING', 'error': 'DANGER', 'loading': 'ACCENT_HOVER'}  # palette keys
LOADERS = (('database', load_databases), ('trace', load_traces), ('panel', None))  # database/trace/panel: lists (panels: load_panels)
FILE_KINDS = ('database', 'trace', 'panel')  # files of a configuration

SOURCES = (('trace', 'Trace'), ('live', 'Live'))  # Datenquelle der Viewer
WINDOW_TITLES = {'database_viewer': 'Database Editor', 'trace_viewer': 'Trace Viewer',
                 'graph_viewer': 'Graph Viewer', 'panel_viewer': 'Panel Viewer', 'generator': 'Generator',
                 'logic': 'Logic Analyzer (USBtingo)'}  # Fenster, die gedockt werden können (Tab-Text)

# Unterfenster: Schlüssel in der Konfiguration, Menütext
WINDOWS = (
    ('database_viewer', 'Database Editor'),
    ('trace_viewer', 'Trace Viewer'),
    ('graph_viewer', 'Graph Viewer'),
    ('panel_viewer', 'Panel Viewer'),
    ('generator', 'Generator'),
    ('logic', 'Logic Analyzer (USBtingo)'),
)
VIEWER_KEYS = ('trace_viewer', 'graph_viewer')  # the two windows of the shared trace viewer engine


def panel_key(index):
    """Window key of the index-th panel (0-based): 'panel_viewer', 'panel_viewer_2', …"""
    return 'panel_viewer' if index == 0 else 'panel_viewer_%d' % (index + 1)


def is_panel_key(key):
    return key == 'panel_viewer' or key.startswith('panel_viewer_')


def panel_number(key):
    return 1 if key == 'panel_viewer' else int(key.rsplit('_', 1)[1])


def window_title(key):
    return 'Panel Viewer' if is_panel_key(key) else WINDOW_TITLES[key]


def config_panels(config):
    """Panel paths of a configuration ('panels'; older versions: 'panel')."""
    paths = config.get('panels')
    if not isinstance(paths, list):
        paths = [config.get('panel')]
    return [path for path in paths if isinstance(path, str) and path]


def load_panels(paths, progress=None):
    """Read several panel files: {'panels': [panel, …], 'errors': [text, …]} (unreadable files are reported)."""
    panels, errors = [], []
    for index, path in enumerate(paths):
        try:
            panels.append(read_panel(path))
        except Exception as error:
            errors.append('%s: %s' % (os.path.basename(path), str(error) or error.__class__.__name__))
        if progress:
            progress((index + 1) / float(len(paths)))
    return {'panels': panels, 'errors': errors}


def draw_start_icon(canvas, color):
    """Triangle: start recording or playing."""
    green = C.SUCCESS
    canvas.create_polygon(11, 8, 22, 15, 11, 22, fill=green, outline=green, width=1.5, joinstyle='round',
                          tags=('icon',))


def draw_stop_icon(canvas, color):
    """Square: stop recording or playing."""
    red = C.DANGER
    canvas.create_rectangle(9, 9, 21, 21, fill=red, outline=red, width=1.5, tags=('icon',))


def draw_source_trace_icon(canvas, color):
    """Page with lines: data source trace file."""
    line = icon_line(icon_hue(3))
    canvas.create_line(17, 7, 9, 7, 9, 23, 21, 23, 21, 11, 17, 7, 17, 11, 21, 11, **line)
    for y in (15, 19):
        canvas.create_line(12, y, 18, y, **line)


def draw_source_live_icon(canvas, color):
    """Dot with radio waves: data source live."""
    color = icon_hue(1)
    canvas.create_oval(13, 13, 17, 17, fill=color, outline=color, tags=('icon',))
    canvas.create_arc(8, 8, 22, 22, start=-40, extent=80, style='arc', outline=color, width=1.5, tags=('icon',))
    canvas.create_arc(8, 8, 22, 22, start=140, extent=80, style='arc', outline=color, width=1.5, tags=('icon',))
    canvas.create_arc(4, 4, 26, 26, start=-40, extent=80, style='arc', outline=color, width=1.5, tags=('icon',))
    canvas.create_arc(4, 4, 26, 26, start=140, extent=80, style='arc', outline=color, width=1.5, tags=('icon',))


def draw_open_database_icon(canvas, color):
    """Page with a table: open database."""
    icon_page(canvas, C.MUTED)
    line = icon_line(icon_hue(3))
    for y in (14, 17.25, 20.5):
        canvas.create_line(12, y, 18, y, **line)
    canvas.create_line(14, 14, 14, 20.5, **line)


def draw_open_trace_icon(canvas, color):
    """Page with a signal: open CAN trace."""
    icon_page(canvas, C.MUTED)
    canvas.create_line(11.5, 18, 13, 18, 13, 14, 15.5, 14, 15.5, 20, 17.5, 20, 17.5, 16, 18.5, 16,
                       **icon_line(C.ACCENT_INK))


def draw_open_panel_icon(canvas, color):
    """Page with a gauge: open panel."""
    icon_page(canvas, C.MUTED)
    pink = icon_hue(6)
    canvas.create_arc(11, 14, 19, 22, start=0, extent=180, style='arc', outline=pink, width=1.5, tags=('icon',))
    canvas.create_line(15, 18, 17.5, 15.5, **icon_line(pink))


def draw_panel_icon(canvas, color):
    """Gauge with a needle: Panel Viewer."""
    pink = icon_hue(6)
    canvas.create_arc(7, 8, 23, 24, start=-30, extent=240, style='arc', outline=pink, width=1.5, tags=('icon',))
    canvas.create_line(15, 16, 19.5, 11.5, **icon_line(icon_hue(1), 2))
    canvas.create_oval(13.5, 14.5, 16.5, 17.5, fill=pink, outline=pink, tags=('icon',))


def draw_database_icon(canvas, color):
    """Database cylinder."""
    purple = icon_hue(3)
    canvas.create_oval(8, 7, 22, 12, fill=purple, outline=purple, width=1.5, tags=('icon',))
    for top in (12, 18):
        canvas.create_arc(8, top, 22, top + 5, start=180, extent=180, style='arc', outline=purple, width=1.5,
                          tags=('icon',))
    canvas.create_line(8, 9.5, 8, 20.5, **icon_line(purple))
    canvas.create_line(22, 9.5, 22, 20.5, **icon_line(purple))


def draw_trace_icon(canvas, color):
    """Table rows: Trace Viewer."""
    canvas.create_rectangle(8, 8, 22, 22, outline=C.MUTED, width=1.5, tags=('icon',))
    for y in (13, 17.5):
        canvas.create_line(8, y, 22, y, **icon_line(C.MUTED))
    canvas.create_line(13, 8, 13, 22, **icon_line(C.MUTED))
    canvas.create_line(15, 10.5, 20, 10.5, **icon_line(icon_hue(2), 2))


def draw_generator_icon(canvas, color):
    """CAN data frame with an outgoing arrow: Generator."""
    green = icon_hue(4)
    canvas.create_rectangle(5, 9, 16, 21, outline=C.MUTED, width=1.5, tags=('icon',))
    canvas.create_line(8, 13, 13, 13, **icon_line(green))
    canvas.create_line(8, 17, 13, 17, **icon_line(green))
    arrow = icon_line(green, 2)
    canvas.create_line(19, 15, 25, 15, **arrow)
    canvas.create_line(22, 12, 25, 15, 22, 18, **arrow)


def draw_logic_icon(canvas, color):
    """Square wave: Logic Analyzer (USBtingo)."""
    canvas.create_line(7, 20, 7, 11, 12, 11, 12, 20, 17, 20, 17, 11, 23, 11, **icon_line(icon_hue(5), 2))
    canvas.create_line(7, 24, 23, 24, **icon_line(C.MUTED))


def draw_graph_icon(canvas, color):
    """Axes with a signal curve: Graph Viewer."""
    canvas.create_line(8, 7, 8, 22, 23, 22, **icon_line(C.MUTED))
    canvas.create_line(10, 18, 13, 13, 16, 16, 19, 9, 22, 12, **icon_line(icon_hue(2), 2))


def config_types():
    return [(tr('CANtrix-Konfiguration'), '*.canconfig'), ('JSON', '*.json'), (tr('Alle Dateien'), '*')]


def existing_file(path):
    return path if isinstance(path, str) and os.path.isfile(path) else None


if tk is not None:

    class AnalyserApp(object):
        def __init__(self, root):
            self.root = root
            self.settings = load_settings(SETTINGS_PATH)
            # Databases of the configuration: [{'path', 'channel'}] (CAN_Databases.py); self.database is all of
            # them combined ({'name', 'path', 'messages', 'environment', 'databases'}), None without one.
            self.database_entries = []
            self.vector_io = None
            self.database = None
            self._database_cache = {}  # path -> ((mtime, size), database): unchanged files are not read again
            self.database_config = None  # open DatabaseConfigWindow
            # Traces of the configuration: [{'path'}] (CAN_Traces.py); self.trace is all of them combined into one
            # ({'name', 'path', 'frames', 'status_count', 'recent', 'environment', 'start', 'traces'}), None without.
            self.trace_entries = []
            self.trace = None
            self._trace_cache = {}  # path -> ((mtime, size), trace as read): unchanged files are not read again
            self.trace_config = None  # open TraceConfigWindow
            self.generator_entries = []  # entries of the generator (CAN_Generator.py), kept while it is closed
            self.panels = {}  # window key -> panel {'name', 'path', 'title', 'width', 'height', 'controls'} (read_panel)
            self.config_path = None
            self.channel = None  # selected CAN channel (e.g. '1'), None for all; part of the configuration
            self.errors = []
            self.connection_text = ''
            self.windows = {}  # key -> (Toplevel, App)
            self.results = queue.Queue()
            self.loading = 0
            self._load_ids = dict((kind, 0) for kind in FILE_KINDS)
            self._config_generation = 0  # increases with every new or loaded configuration
            self._pending = {}  # kind -> path of a file that is being read
            # kind -> (load id, share done 0..1), written by the loading thread, shown by _update_progress
            self._progress = {}
            self._progress_rows = {}  # kind -> (row frame, progress bar, percent text)
            # Plugins belong to the configuration (.canconfig). Plugins from older versions, stored in the
            # settings file, start in the current, not yet saved configuration; they leave the settings
            # file once they were saved in a configuration (write_config).
            self.plugins = normalize_plugins(self.settings.get('plugins'))
            self.plugin_config = None  # open PluginConfigWindow
            self.plugin_runs = []  # PluginRun windows
            self.info = None  # open InfoWindow
            self.about = None  # open AboutWindow

            set_language(self.settings.get('language'))
            self.status_var = tk.StringVar()
            set_text(self.status_var, 'Bereit')
            set_dark_mode(root, self.settings.get('dark_mode') is not False)  # dark by default
            apply_appearance(root)
            self.dark_var = tk.BooleanVar(value=is_dark_mode())
            # Source of the viewers: 'trace' (loaded trace) or 'live' (recording), for Trace and Panel Viewer together
            self.source = tk.StringVar(value='trace')  # always starts with Trace, not remembered
            self.settings.pop('source', None)  # remembered by older versions
            self.panel_fit = tk.BooleanVar(value=self.settings.get('panel_fit') is not False)  # panels always at their best size
            self.docked = tk.BooleanVar(value=self.settings.get('docked') is True)  # windows as tabs in this window
            self.run_var = tk.StringVar(value='stop')  # 'start' while recording or playing
            # Time controls of the Panel Viewer (the Panel Viewer takes these variables over, see open_window).
            self.time_var = tk.StringVar(value='—')
            self.speed_var = tk.StringVar(value=self.settings.get('speed') if self.settings.get('speed') in SPEEDS
                                          else DEFAULT_SPEED)
            self.follow_var = tk.BooleanVar(value=self.settings.get('follow_cursor') is not False)
            self._setting_slider = False
            self._local_moment, self._local_job, self._local_last = 0.0, None, 0.0
            self._play_engaged = False  # the Trace Viewer shows only up to the playing position
            self.selection = None  # selected time range (start, end) of the trace; None = all
            # Checkmarks in the menu "Sprache": one per language, only the current one is set
            self.language_vars = dict((code, tk.BooleanVar(value=code == language())) for code, _ in LANGUAGES)

            root.title('CANtrix')
            if self.settings.get('main_layout', 1) < 2:
                # Once: the main window got bigger; keep the saved position, use the new size.
                position = str(self.settings.get('geometry', '')).partition('+')
                self.settings['geometry'] = '%dx%d' % MAIN_SIZE + (position[1] + position[2] if position[1] else '')
                self.settings['main_layout'] = 2
            geometry = initial_geometry(root, self.settings, '%dx%d' % MAIN_SIZE)
            root.geometry(geometry)
            root.minsize(560, 320)
            self.theme = Theme(root)
            self._build_menu()
            self._build_body()
            # The toolbar (with the time controls) must fit: widen the window if it is narrower.
            root.update_idletasks()
            size = re.match(r'(\d+)x(\d+)', geometry)
            if size and int(size.group(1)) < root.winfo_reqwidth():
                root.geometry('%dx%s' % (root.winfo_reqwidth(), size.group(2)))
            self.render_status()
            root.protocol('WM_DELETE_WINDOW', self.close)
            if root.tk.call('tk', 'windowingsystem') == 'aqua':
                root.createcommand('tk::mac::Quit', self.close)
            self._poll_job = root.after(60, self.poll_results)

        @property
        def database_path(self):
            return self.database['path'] if self.database else None

        @property
        def trace_path(self):
            return self.trace['path'] if self.trace else None

        def panel_keys(self):
            """Keys of the loaded panels in order."""
            return sorted(self.panels, key=panel_number)

        @property
        def panel(self):
            """The first panel (None without one)."""
            keys = self.panel_keys()
            return self.panels[keys[0]] if keys else None

        @property
        def panel_path(self):
            return self.panel['path'] if self.panel else None

        def _free_panel_key(self):
            number = 0
            while panel_key(number) in self.panels or panel_key(number) in self.windows:
                number += 1
            return panel_key(number)

        def _panel_apps(self):
            """The open Panel Viewers in order; the first one is the master of the time controls."""
            keys = sorted((key for key in self.windows if is_panel_key(key)), key=panel_number)
            return [self.windows[key][1] for key in keys]

        def _master(self):
            apps = self._panel_apps()
            return apps[0] if apps else None

        def _assign_master(self):
            """The first Panel Viewer owns the time controls and reports time and playing; the others follow."""
            for index, app in enumerate(self._panel_apps()):
                if index == 0:
                    app.time_var, app.speed_var, app.follow_var = self.time_var, self.speed_var, self.follow_var
                    app.on_time_changed = self._time_changed
                    app.on_playing_changed = self._playing_changed
                else:
                    app.on_time_changed = app.on_playing_changed = None
                    app.time_var = tk.StringVar()
                    app.follow_var = tk.BooleanVar(value=False)
            self._push_range()

        # ------------------------------------------------------------ layout

        def _build_menu(self):
            """The menu bar is shared by the main window and all child windows (see attach_menu)."""
            aqua = self.root.tk.call('tk', 'windowingsystem') == 'aqua'
            mod, label = ('Command', 'Cmd') if aqua else ('Control', 'Ctrl')
            menubar = self.menubar = tk.Menu(self.root)

            file_menu = self.file_menu = tk.Menu(menubar, tearoff=False)
            file_menu.add_command(label=tr('Neue Konfiguration'), accelerator=label + '+N', command=self.new_config)
            file_menu.add_command(label=tr('Konfiguration laden…'), accelerator=label + '+L',
                                  command=self.load_config_dialog)
            file_menu.add_command(label=tr('Konfiguration speichern'), accelerator=label + '+S',
                                  command=self.save_config)
            file_menu.add_command(label=tr('Konfiguration speichern unter…'), command=self.save_config_as)
            self._recent_start = file_menu.index(tk.END) + 1
            self._recent_items = 0
            file_menu.add_separator()
            file_menu.add_command(label=tr('Datenbasen…'), accelerator=label + '+D',
                                  command=self.configure_databases)
            file_menu.add_command(label=tr('CAN-Traces…'), accelerator=label + '+O', command=self.configure_traces)
            file_menu.add_command(label=tr('Panel öffnen…'), accelerator=label + '+P', command=self.open_panel)
            file_menu.add_separator()
            # On macOS Cmd+Q already quits through tk::mac::Quit; the label only shows it.
            file_menu.add_command(label=tr('Beenden'), accelerator=label + '+Q' if aqua else '', command=self.close)
            menubar.add_cascade(label=tr('Datei'), menu=file_menu)

            settings_menu = tk.Menu(menubar, tearoff=False)
            settings_menu.add_command(label=tr('CAN Trace Viewer'), command=self.choose_channel)
            settings_menu.add_command(label=tr('CAN Trace Recorder'), command=self.configure_recorder)
            settings_menu.add_command(label=tr('Vector I/O Configuration'), command=self.configure_vector_io)
            settings_menu.add_command(label=tr('TRACE32'), command=self.configure_debugger)
            menubar.add_cascade(label=tr('Einstellungen'), menu=settings_menu)

            window_menu = tk.Menu(menubar, tearoff=False)
            window_menu.add_command(label=tr('Hauptfenster'), command=self.show_main_window)
            window_menu.add_separator()
            for key, title in WINDOWS:
                window_menu.add_command(label=title, command=lambda key=key: self.show_window(key))
            window_menu.add_separator()
            source_menu = tk.Menu(window_menu, tearoff=False)
            for value, text in SOURCES:
                source_menu.add_radiobutton(label=text, value=value, variable=self.source, command=self.change_source)
            window_menu.add_cascade(label=tr('Datenquelle'), menu=source_menu)
            window_menu.add_separator()
            window_menu.add_checkbutton(label=tr('Panel Größe immer optimal'), variable=self.panel_fit,
                                        command=self.toggle_panel_fit)
            window_menu.add_checkbutton(label=tr('Gedockte Ansicht'), variable=self.docked, command=self.toggle_docked)
            window_menu.add_checkbutton(label='Dark Mode', variable=self.dark_var, command=self.toggle_dark_mode)
            language_menu = tk.Menu(window_menu, tearoff=False)
            for code, name in LANGUAGES:
                language_menu.add_checkbutton(label=name, variable=self.language_vars[code],
                                              command=lambda code=code: self.change_language(code))
            window_menu.add_cascade(label=tr('Sprache'), menu=language_menu)
            menubar.add_cascade(label=tr('Ansicht'), menu=window_menu)

            self.plugin_menu = tk.Menu(menubar, tearoff=False)
            self.plugin_menu.add_command(label=tr('Plugins konfigurieren…'), command=self.configure_plugins)
            menubar.add_cascade(label='Plugins', menu=self.plugin_menu)

            info_menu = tk.Menu(menubar, tearoff=False)
            info_menu.add_command(label=tr('Hilfe'), command=self.show_info)
            info_menu.add_command(label=tr('Auf Update prüfen'), command=self.check_for_update)
            info_menu.add_command(label=tr('Über CANtrix'), command=self.show_about)
            menubar.add_cascade(label='Info', menu=info_menu)

            self.shortcuts = [('<%s-%s>' % (mod, key), command) for key, command in (
                ('n', self.new_config), ('d', self.configure_databases), ('o', self.configure_traces), ('p', self.open_panel),
                ('l', self.load_config_dialog), ('s', self.save_config))]
            self.attach_menu(self.root)
            self.update_recent_menu()
            self.update_plugin_menu()

        def attach_menu(self, window):
            """Show the main menu and its shortcuts in a window, so it is visible in every window."""
            window.configure(menu=self.menubar)
            for sequence, command in self.shortcuts:
                window.bind(sequence, lambda event, command=command: command())

        def show_main_window(self):
            if self.docked.get():
                if self.status_host is None:
                    self._build_status()  # the status pane was closed
                else:
                    self.status_host.tkraise()
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

        def _build_body(self):
            theme = self.theme
            self._build_toolbar()
            theme.footer(self.root, self.status_var)
            # The work area: the status summary fills it; in the docked view it and the windows are panes in it.
            self.area = tk.Frame(self.root, background=C.APP_BG)
            self.area.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            self.area.bind('<Configure>', lambda event: self._refit_panes())
            self.watermark_canvas = tk.Canvas(self.area, borderwidth=0, highlightthickness=0,
                                              background=C.APP_BG)
            self.watermark_canvas.place(relwidth=1, relheight=1)
            self.watermark_canvas.bind('<Configure>', lambda event: self._draw_watermark())
            self.status_host = None
            self.status_canvas = self.progress_frame = None
            self.status_rows = []  # (label, value, kind), see render_status
            self._draw_watermark()
            self._build_status()

        def _build_status(self):
            """(Re)build the status summary: a card filling the main window, or a pane in the docked view."""
            if self.status_host is not None:
                try:
                    self.status_host.destroy()
                except tk.TclError:
                    pass
            self._progress_rows = {}
            if self.docked.get():
                host = self.status_host = self._new_pane('status', tr('Status & Zusammenfassung'),
                                                         self._status_closed)
                parent = host.body
            else:
                host, _ = self.theme.pane(self.area, tr('Status & Zusammenfassung'))
                host.pack(fill=tk.BOTH, expand=True)
                self.status_host = parent = host
            canvas = self.status_canvas = tk.Canvas(parent, height=150, borderwidth=0, highlightthickness=0,
                                                    background=C.SURFACE)
            canvas.pack(fill=tk.BOTH, expand=True)
            canvas.bind('<Configure>', lambda event: self._draw_status())
            canvas.after_idle(self._draw_status)
            # Progress bars of the files being read, below the summary (shown only while loading).
            self.progress_frame = tk.Frame(parent, background=C.SURFACE)
            if hasattr(self, 'database_entries'):  # not during the first build (render_status follows later)
                self.render_status()
                self._update_progress()

        def _status_closed(self):
            """Close button of the status pane: it comes back with 'Ansicht > Hauptfenster'."""
            self._remember_pane('status', self.status_host)
            try:
                self.status_host.destroy()
            except tk.TclError:
                pass
            self.status_host = self.status_canvas = self.progress_frame = None
            self._progress_rows = {}

        # ------------------------------------------------------------ panes (docked view)

        def _new_pane(self, key, title, on_close, geometry=None):
            """Pane in the work area: geometry (dict) from a configuration, else the remembered one, else a default."""
            pane = FloatingPane(self.area, self.theme, title, on_close)
            geometry = geometry or self.settings.get('dock', {}).get(key)
            if not (isinstance(geometry, dict) and pane.apply_dict(geometry)):
                area_w, area_h = pane.area_size()
                count = len([child for child in self.area.winfo_children() if isinstance(child, FloatingPane)]) - 1
                if key == 'status':
                    pane.set_geometry(0, 0, int(area_w * 0.5), int(area_h * 0.55))
                else:
                    pane.set_geometry(40 + 30 * count, 24 + 24 * count, int(area_w * 0.7), int(area_h * 0.85))
            pane.tkraise()
            return pane

        def _remember_pane(self, key, pane):
            if pane is not None:
                self.settings.setdefault('dock', {})[key] = pane.geometry_dict()

        def _refit_panes(self):
            for child in self.area.winfo_children():
                if isinstance(child, FloatingPane):
                    child.refit()

        def _draw_status(self):
            """Draw the rows of the summary."""
            canvas = self.status_canvas
            if canvas is None:
                return
            canvas.delete('all')
            width = canvas.winfo_width()
            theme = self.theme
            value_x = 16 + 130  # like the former tab stop of the text
            y = 12
            previous = None
            for label, value, kind in self.status_rows:
                # Further rows of the same group (e.g. plugins) are only indented, without a label.
                if label != previous:
                    canvas.create_text(16, y, anchor=tk.NW, text=label, fill=C.MUTED, font=theme.font_heading)
                previous = label
                color = getattr(C, STATUS_COLORS[kind]) if kind else C.INK
                item = canvas.create_text(value_x, y, anchor=tk.NW, text=value, fill=color, font=theme.font_mono,
                                          width=max(100, width - value_x - 16))
                y = canvas.bbox(item)[3] + 6

        def _draw_watermark(self):
            """Draw the subtle name behind the windows on the main work area."""
            canvas = self.watermark_canvas
            if canvas is None:
                return
            canvas.delete('all')
            canvas.create_text(max(24, canvas.winfo_width() - 28), max(24, canvas.winfo_height() - 24),
                               anchor=tk.SE, text='CANtrix', fill=C.LINE_STRONG, font=self.theme.font_brand)

        def _build_toolbar(self):
            """Toolbar with configuration, viewer and recording controls."""
            label = 'Cmd' if self.root.tk.call('tk', 'windowingsystem') == 'aqua' else 'Ctrl'
            toolbar = tk.Frame(self.root, background=C.SURFACE)
            toolbar.pack(side=tk.TOP, fill=tk.X, padx=16, pady=10)
            save_tip = lambda: (tr('Konfiguration speichern (%s+S)') % label if self.config_path
                                else tr('Konfiguration speichern unter…'))
            icons = {'database_viewer': draw_database_icon, 'trace_viewer': draw_trace_icon, 'graph_viewer': draw_graph_icon,
                     'generator': draw_generator_icon, 'logic': draw_logic_icon,
                     'panel_viewer': draw_panel_icon}
            groups = (
                ((draw_new_icon, self.new_config, lambda: tr('Neue Konfiguration (%s+N)') % label),
                 (draw_open_icon, self.load_config_dialog, lambda: tr('Konfiguration laden… (%s+L)') % label),
                 (draw_save_icon, self.save_config, save_tip)),
                ((draw_open_database_icon, self.configure_databases, lambda: tr('Datenbasen… (%s+D)') % label),
                 (draw_open_trace_icon, self.configure_traces, lambda: tr('CAN-Traces… (%s+O)') % label),
                 (draw_open_panel_icon, self.open_panel, lambda: tr('Panel öffnen… (%s+P)') % label)),
                tuple((icons[key], lambda key=key: self.show_window(key), title) for key, title in WINDOWS),
            )
            for index, group in enumerate(groups):
                if index:
                    tk.Frame(toolbar, background=C.SURFACE, width=18).pack(side=tk.LEFT)
                for draw, command, tip in group:
                    self.theme.icon_button(toolbar, draw, command, tip).pack(side=tk.LEFT, padx=(0, 6))
            # Next to the icons: Start/Stop of the recording, then Trace/Live for Trace Viewer and Panel Viewer.
            # Like Trace/Live: the active state (recording/playing = Start, otherwise Stop) is highlighted.
            tk.Frame(toolbar, background=C.SURFACE, width=18).pack(side=tk.LEFT)
            # One toggle: stopped shows Start (triangle), running shows Stop (square, highlighted).
            self.run_button = self.theme.icon_button(
                toolbar, lambda canvas, color: (draw_stop_icon if self.run_var.get() == 'start' else draw_start_icon)(
                    canvas, color),
                self.toggle_recording, lambda: tr('Stop') if self.run_var.get() == 'start' else tr('Start'),
                active=lambda: self.run_var.get() == 'start')
            self.run_button.pack(side=tk.LEFT, padx=(0, 6))
            tk.Frame(toolbar, background=C.SURFACE, width=12).pack(side=tk.LEFT)
            # One toggle: shows the active data source (page = Trace, radio waves = Live); a click switches.
            self.source_button = self.theme.icon_button(
                toolbar, lambda canvas, color: (draw_source_live_icon if self.source.get() == 'live'
                                                else draw_source_trace_icon)(canvas, color),
                lambda: self._choose_source('trace' if self.source.get() == 'live' else 'live'),
                lambda: tr('Online (Live) – Klick: Offline (Trace)') if self.source.get() == 'live'
                else tr('Offline (Trace) – Klick: Online (Live)'),
                active=lambda: self.source.get() == 'live')
            self.source_button.pack(side=tk.LEFT, padx=(0, 6))
            self.source_text = tk.StringVar(value=tr('Online') if self.source.get() == 'live' else tr('Offline'))
            ttk.Label(toolbar, textvariable=self.source_text, width=7, foreground=C.MUTED).pack(side=tk.LEFT)
            for variable in (self.run_var, self.source):
                variable.trace_add('write', lambda *args: self._refresh_toolbar_icons())
            self.source.trace_add('write', lambda *args: self.source_text.set(
                tr('Online') if self.source.get() == 'live' else tr('Offline')))
            # Time of the Panel Viewer (trace state): slider, time, speed, follow the cursor of the Trace Viewer.
            self.follow_box = ttk.Checkbutton(toolbar, text=tr('Cursor des Trace Viewers folgen'),
                                              variable=self.follow_var, command=self._remember_time_settings)
            self.follow_box.pack(side=tk.RIGHT)
            self.speed_box = ttk.Combobox(toolbar, textvariable=self.speed_var, values=SPEEDS, width=5,
                                          state='readonly')
            self.speed_box.pack(side=tk.RIGHT, padx=(0, 12))
            self.speed_box.bind('<<ComboboxSelected>>', lambda event: self._remember_time_settings())
            ttk.Label(toolbar, text=tr('Tempo'), foreground=C.MUTED).pack(side=tk.RIGHT, padx=(12, 6))
            ttk.Label(toolbar, textvariable=self.time_var, width=18, anchor=tk.E,
                      font=self.theme.font_mono).pack(side=tk.RIGHT)
            self.time_slider = ttk.Scale(toolbar, orient=tk.HORIZONTAL, from_=0.0, to=1.0, length=TIME_SLIDER_LENGTH,
                                         command=self.on_time_slider)
            self.time_slider.pack(side=tk.RIGHT, padx=(12, 8))
            # Two bars on the time slider select the range of the trace (Trace Viewer, Graph Viewer, saving).
            self.range_markers = []
            for index in (0, 1):
                marker = tk.Frame(toolbar, width=6, bg=C.WARNING, cursor='sb_h_double_arrow')
                marker.bind('<B1-Motion>', lambda event, which=index: self._drag_marker(which, event))
                marker.bind('<ButtonRelease-1>', lambda event: self._apply_selection())
                self.theme.bind_tooltip(marker, lambda index=index: self._range_marker_tooltip(index))
                self.range_markers.append(marker)
            self.time_slider.bind('<Configure>', lambda event: self._place_markers(), add='+')
            self._update_recording_buttons()

        def _choose_source(self, value):
            self.source.set(value)
            self.change_source()

        def _refresh_toolbar_icons(self):
            try:
                for button in (self.run_button, self.source_button):
                    button.event_generate('<<IconRefresh>>')
            except tk.TclError:  # the main window is being closed
                pass

        def _file_row(self, label, kind, path):
            if kind in self._pending:
                return label, tr('Wird geladen: %s') % self._request_text(self._pending[kind]), 'loading'
            return label, path or tr('Nicht geladen'), 'ok' if path else ''

        @staticmethod
        def _request_text(value):
            """File names of a load request (a path, or the database list)."""
            if isinstance(value, list):
                return ', '.join(os.path.basename(entry['path'] if isinstance(entry, dict) else entry)
                                 for entry in value)
            return os.path.basename(value)

        def _database_rows(self):
            """Per database of the configuration: path (with its CAN channel) and contents."""
            if not self.database_entries:
                return [(tr('Datenbasis'), tr('Nicht geladen'), '')]
            pending = self._pending.get('database') or []
            loaded = dict(((item['path'], item['channel']), item)
                          for item in (self.database or {}).get('databases') or [])
            rows = []
            for entry in self.database_entries:
                text = entry['path'] + (tr(' · Kanal %s') % entry['channel'] if entry['channel'] else '')
                item = loaded.get((entry['path'], entry['channel']))
                if any(other['path'] == entry['path'] for other in pending):
                    rows.append((tr('Datenbasis'), tr('Wird geladen: %s') % text, 'loading'))
                elif item is not None:
                    rows.append((tr('Datenbasis'), text, 'ok'))
                    rows.append((tr('Datenbasis'), database_summary(item), ''))
                else:
                    rows.append((tr('Datenbasis'), text + tr(' · nicht geladen'), 'error'))
            return rows

        def _trace_rows(self):
            """Per trace of the configuration: path and frames, start, place in the combined trace."""
            if not self.trace_entries:
                return [('Trace', tr('Nicht geladen'), '')]
            pending = self._pending.get('trace') or []
            parts = dict((part['path'], part) for part in (self.trace or {}).get('traces') or [])
            rows = []
            for entry in self.trace_entries:
                part = parts.get(entry['path'])
                if any(other['path'] == entry['path'] for other in pending):
                    rows.append(('Trace', tr('Wird geladen: %s') % entry['path'], 'loading'))
                elif part is not None:
                    rows.append(('Trace', entry['path'], 'ok'))
                    if len(self.trace_entries) > 1:
                        rows.append(('Trace', trace_summary(part), ''))
                else:
                    rows.append(('Trace', entry['path'] + tr(' · nicht geladen'), 'error'))
            return rows

        def _trace_info(self, entry):
            """Column "Inhalt" of the trace window; '!' at the start marks an error."""
            if any(other['path'] == entry['path'] for other in self._pending.get('trace') or []):
                return tr('wird geladen…')
            for part in (self.trace or {}).get('traces') or []:
                if part['path'] == entry['path']:
                    return trace_summary(part)
            return '!' + tr('nicht geladen')

        def _database_info(self, entry):
            """Column "Inhalt" of the database window; '!' at the start marks an error."""
            if any(other['path'] == entry['path'] for other in self._pending.get('database') or []):
                return tr('wird geladen…')
            for item in (self.database or {}).get('databases') or []:
                if item['path'] == entry['path'] and item['channel'] == entry['channel']:
                    return database_summary(item)
            return '!' + tr('nicht geladen')

        def _update_progress(self):
            """One progress bar per file being read; rows of finished files disappear."""
            if self.progress_frame is None:  # the status pane is closed
                return
            for kind in list(self._progress_rows):
                if kind not in self._pending:
                    self._progress_rows.pop(kind)[0].destroy()
            if not self._pending:
                self.progress_frame.pack_forget()
                return
            if not self.progress_frame.winfo_ismapped():
                self.progress_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 12), before=self.status_canvas)
            for kind, _ in LOADERS:
                if kind not in self._pending:
                    continue
                if kind not in self._progress_rows:
                    row = tk.Frame(self.progress_frame, background=C.SURFACE)
                    row.pack(side=tk.TOP, fill=tk.X, pady=2)
                    tk.Label(row, text={'database': tr('Datenbasis'), 'trace': 'Trace'}.get(kind, 'Panel'),
                             width=12, anchor=tk.W,
                             background=C.SURFACE, foreground=C.MUTED, font=self.theme.font_heading).pack(side=tk.LEFT)
                    percent = tk.StringVar(value='0 %')
                    tk.Label(row, textvariable=percent, width=6, anchor=tk.E, background=C.SURFACE,
                             foreground=C.MUTED, font=self.theme.font_label).pack(side=tk.RIGHT)
                    bar = ttk.Progressbar(row, orient=tk.HORIZONTAL, mode='determinate', maximum=100)
                    bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
                    self._progress_rows[kind] = (row, bar, percent)
                _, bar, percent = self._progress_rows[kind]
                load_id, share = self._progress.get(kind, (None, 0.0))
                value = int(round(100 * share)) if load_id == self._load_ids[kind] else 0
                bar['value'] = value
                percent.set('%d %%' % value)

        def _plugin_rows(self):
            """One row per configured plugin."""
            if not self.plugins:
                return [('Plugins', tr('Keine'), '')]
            rows = []
            for plugin in self.plugins:
                text = '%s · %s%s' % (plugin['name'], plugin['script'],
                                        tr(' · bekommt den Trace') if plugin['pass_trace'] else '')
                if os.path.isfile(script_path(plugin)):
                    rows.append(('Plugin', text, ''))
                else:
                    rows.append(('Plugin', text + tr(' · Skript fehlt'), 'error'))
            return rows

        def render_status(self):
            """Summary of the opened files, as the CAN Tracer showed it."""
            messages = self.database['messages'] if self.database else []
            frames = self.trace['frames'] if self.trace else []
            index = MessageIndex(messages)
            known = sum(1 for frame in frames if index.find(frame.id, frame.channel) is not None) if messages else 0
            unique = len(set(frame.channel_key() for frame in frames))
            # Order: configuration (with its plugins), database, trace, panel, CAN channel; notes at the end.
            # Rows of a group share their label, which is shown only once (see below).
            rows = [(tr('Konfiguration'), self.config_path or tr('Keine'), '')] + self._plugin_rows() + \
                self._database_rows() + self._trace_rows()
            if self.trace:  # the combined trace
                if len(self.trace.get('traces') or ()) > 1:
                    rows.append(('Trace', tr('Zusammengeführt: %s Traces, Start %s') % (
                        de_int(len(self.trace['traces'])),
                        absolute_time_text(self.trace['start'], 0.0) if self.trace.get('start') else DASH), ''))
                rows += [('Trace', '%s Frames · %s IDs' % (de_int(len(frames)), de_int(unique)), ''),
                         ('Trace', tr('%s Frames mit Datenbasis-Nachricht') % de_int(known), '')]
            environment = self.trace.get('environment') if self.trace else None
            if environment:
                count = sum(len(values) for values in environment.values())
                rows.append(('Trace', tr('%s Werte von %s Umgebungsvariablen') % (de_int(count),
                                                                               de_int(len(environment))), ''))
            errors = self.trace.get('errors') if self.trace else None
            if errors:
                rows.append(('Trace', tr('%s CAN-Fehler (Error Frames)') % de_int(len(errors)), 'warn'))
            if self.trace and self.trace['status_count']:
                rows.append((tr('Trace-Status'), tr('%s Status-/Ereigniszeilen') % de_int(self.trace['status_count']),
                             'warn'))
            if self.trace:
                rows.extend((tr('Trace-Ereignis'), event, 'warn') for event in self.trace['recent'])
            if not self.panels:
                rows.append(self._file_row('Panel', 'panel', None))
            for key in self.panel_keys():
                panel = self.panels[key]
                rows.append(('Panel', panel['path'], 'ok'))
                rows.append(('Panel', tr('%s · %s Elemente · %s verknüpft') % (
                    panel['title'], de_int(len(panel['controls'])), de_int(bound_count(panel))), ''))
            if self.panels and 'panel' in self._pending:
                rows.append(self._file_row('Panel', 'panel', None))
            rows.append((tr('CAN-Kanal'), self._channel_text(), ''))
            connected = self.connection_text.startswith(('Verbunden', 'Connected'))
            rows.append((tr('Verbindung'), self.connection_text or tr('Nicht verbunden'),
                         'ok' if connected else ''))
            rows.extend((tr('Hinweis'), error, 'error') for error in self.errors)
            self.status_rows = rows
            self._draw_status()
            title = 'CANtrix'
            if self.config_path:
                title += ' – ' + os.path.basename(self.config_path)
            self.root.title(title)

        def _connection_status(self, text):
            """Connection status of the recording (from the Recorder), shown in the status overview."""
            self.connection_text = text or ''
            self.render_status()

        def _channel_text(self):
            text = tr('Kanal %s') % self.channel if self.channel is not None else tr('Alle Kanäle')
            if self.trace:
                text += tr(' · im Trace: %s') % ', '.join(trace_channels(self.trace['frames']))
            return text

        def _initial_dir(self, path):
            if path and os.path.isdir(os.path.dirname(path)):
                return os.path.dirname(path)
            return ROOT

        # ------------------------------------------------------------ database and trace

        def configure_databases(self):
            """Menu 'Datenbasen…' (Cmd/Ctrl+D): window to add, change and remove the databases."""
            if self.database_config is not None and self.database_config.window.winfo_exists():
                self.database_config.window.deiconify()
                self.database_config.window.lift()
                return
            self.database_config = DatabaseConfigWindow(
                self.root, self.database_entries, self.set_databases,
                channels=lambda: trace_channels(self.trace['frames']) if self.trace else [],
                info=self._database_info, attach_menu=self.attach_menu,
                initial_dir=self._initial_dir(self.database_path))

        def _database_window(self):
            window = self.database_config
            return window if window is not None and window.window.winfo_exists() else None

        def set_databases(self, entries):
            """New list of databases (database window, configuration, command line): read and use them."""
            self.database_entries = normalize_databases(entries)
            if self.database_entries:
                self.load_files({'database': [dict(entry) for entry in self.database_entries]})
            else:
                self._load_ids['database'] += 1  # a database still being read is no longer wanted
                self._pending.pop('database', None)
                self._update_progress()
                self.take_over({'database': None})

        def configure_traces(self):
            """Menu 'CAN-Traces…' (Cmd/Ctrl+O): window to add, change and remove the traces."""
            if self._trace_window() is not None:
                self.trace_config.window.deiconify()
                self.trace_config.window.lift()
                return
            self.trace_config = TraceConfigWindow(self.root, self.trace_entries, self.set_traces,
                                                  info=self._trace_info, attach_menu=self.attach_menu,
                                                  initial_dir=self._initial_dir(self.trace_path))

        def _trace_window(self):
            window = self.trace_config
            return window if window is not None and window.window.winfo_exists() else None

        def set_traces(self, entries):
            """New list of traces (trace window, configuration, command line): read, combine and use them."""
            self.trace_entries = normalize_traces(entries)
            if self.trace_entries:
                self.load_files({'trace': [dict(entry) for entry in self.trace_entries]})
            else:
                self._load_ids['trace'] += 1  # a trace still being read is no longer wanted
                self._pending.pop('trace', None)
                self._update_progress()
                self.take_over({'trace': None})

        def open_panel(self):
            paths = filedialog.askopenfilenames(
                parent=self.root, title=tr('Panel öffnen'), initialdir=self._initial_dir(self.panel_path),
                filetypes=[(tr('Vector-Panel'), '*.xvp'), (tr('Alle Dateien'), '*')])
            self.add_panels([os.path.abspath(path) for path in paths])

        def add_panels(self, paths):
            """Load panels and open a Panel Viewer for each (a panel that is already open is brought to the front)."""
            loaded = dict((panel['path'], key) for key, panel in self.panels.items())
            new = []
            for path in paths:
                if path in loaded:
                    self.open_window(loaded[path])
                elif path not in new:
                    new.append(path)
            if not new:
                return
            before = set(self.panels)

            def show():
                for key in sorted(set(self.panels) - before, key=panel_number):
                    self.open_window(key)
            self.load_files({'panel': new}, show)

        def show_window(self, key):
            """Menu and icons: open a window. Panel Viewer: loaded panels without a window open again, otherwise
            another panel is chosen and opened in a new Panel Viewer."""
            if key != 'panel_viewer':
                self.open_window(key)
                return
            closed = [other for other in self.panel_keys() if other not in self.windows]
            for other in closed:
                self.open_window(other)
            if not closed:
                self.open_panel()

        def set_trace(self, path):
            """Use a single trace (instead of the list)."""
            self.set_traces([path])

        def load_files(self, request, done=None):
            """Read files in the background. request: {'database': [...], 'trace': [...], 'panel': [paths]} (optional).
            done() runs after the files were taken over, also when one could not be read."""
            ids = {}
            for kind in request:
                self._load_ids[kind] += 1
                ids[kind] = self._load_ids[kind]
                self._pending[kind] = request[kind]
            self.render_status()
            self._update_progress()
            self.loading += 1
            self.root.configure(cursor='watch')
            set_text(self.status_var, 'Lade %s…', ', '.join(self._request_text(value) for value in request.values()))
            cache = {'database': dict(self._database_cache), 'trace': dict(self._trace_cache)}  # for the thread

            def run():
                loaded, errors = {}, []
                for kind, loader in LOADERS:
                    if kind in request:
                        try:
                            def progress(share, kind=kind):
                                self._progress[kind] = (ids[kind], share)  # read by the GUI thread
                            if kind == 'database':
                                loaded[kind] = loader(request[kind], progress, cache[kind])
                                errors.extend(loaded[kind].pop('errors'))  # files that could not be read
                            elif kind == 'trace':
                                result = loader(request[kind], progress, cache[kind])
                                errors.extend(result['errors'])
                                loaded[kind] = result['trace']  # None if no trace could be read
                                loaded['trace_cache'] = result['cache']
                            else:
                                result = load_panels(request[kind], progress)
                                errors.extend(result['errors'])
                                loaded[kind] = result['panels']
                        except Exception as error:  # reported in the status
                            text = str(error) or error.__class__.__name__
                            if isinstance(error, OSError) and getattr(error, 'strerror', None):
                                text = '%s: %s' % (error.strerror, error.filename or '')
                            errors.append('%s: %s' % (self._request_text(request[kind]), text))
                self.results.put((ids, loaded, errors, done))
            threading.Thread(target=run, daemon=True).start()

        def poll_results(self):
            try:
                while True:
                    ids, loaded, errors, done = self.results.get_nowait()
                    self.loading -= 1
                    # A newer request for the same kind wins over an older one that finished later.
                    self._trace_cache.update(loaded.pop('trace_cache', {}))
                    fresh = dict((kind, value) for kind, value in loaded.items()
                                 if ids[kind] == self._load_ids[kind])
                    for kind in ids:  # finished (read or failed), unless a newer request is running
                        if ids[kind] == self._load_ids[kind]:
                            self._pending.pop(kind, None)
                    self.errors = errors
                    self.take_over(fresh)
                    if done is not None:
                        done()
                    if errors:
                        set_text(self.status_var, 'Laden fehlgeschlagen: %s', errors[0])
                    elif fresh:
                        names = [item['name'] for value in fresh.values() for item in
                                 (value if isinstance(value, list) else [value]) if item and item['name']]
                        set_text(self.status_var, 'Geladen: %s', ', '.join(names))
            except queue.Empty:
                pass
            self._update_progress()
            if not self.loading:
                self.root.configure(cursor='')
            self._poll_job = self.root.after(60, self.poll_results)

        def take_over(self, fresh):
            """Use newly read files and pass them on to the open windows."""
            if 'database' in fresh:
                database = fresh['database']
                if database is not None:
                    self._database_cache.update(database.pop('cache', {}))
                    known = set(entry['path'] for entry in self.database_entries)
                    self._database_cache = dict((path, value) for path, value in self._database_cache.items()
                                                if path in known)
                self.database = database if database and database['databases'] else None
                fresh = dict(fresh, database=self.database)
            if 'panel' in fresh:
                for panel in fresh['panel']:
                    # an open empty Panel Viewer takes the panel, otherwise it gets a window of its own
                    empty = [key for key in sorted(self.windows, key=lambda key: key) if is_panel_key(key)
                             and key not in self.panels]
                    key = empty[0] if empty else self._free_panel_key()
                    self.panels[key] = panel
                    if key in self.windows:
                        self.windows[key][1].set_sources({'panel': panel})
                fresh = dict((kind, value) for kind, value in fresh.items() if kind != 'panel')
            if 'trace' in fresh:
                self.selection = None
                self._reset_position()
                self.trace = fresh['trace']
                known = set(entry['path'] for entry in self.trace_entries)
                self._trace_cache = dict((path, value) for path, value in self._trace_cache.items() if path in known)
                # A trace with a single channel selects it; a channel the trace does not use is dropped.
                channels = trace_channels(self.trace['frames']) if self.trace else []
                if len(channels) == 1:
                    self.channel = channels[0]
                elif self.channel not in channels:
                    self.channel = None
            self.render_status()
            if self._database_window() is not None:
                self._database_window().update_info()
            if self._trace_window() is not None:
                self._trace_window().update_info()
            if not fresh:
                self._update_recording_buttons()
                return
            files = dict((kind, value) for kind, value in fresh.items() if kind != 'panel')
            viewer = self._viewer()
            if viewer is not None and files:
                viewer.set_channel(self.channel, redraw=False)  # set_sources redraws
                viewer.set_sources(files)
            for key, (_, app) in list(self.windows.items()):
                if key in VIEWER_KEYS:
                    continue
                if is_panel_key(key):
                    app.channel = self.channel  # set_sources rebuilds
                    app.set_sources(fresh)
                elif 'database' in fresh:
                    app.set_database(self.database)
            if 'trace' in fresh and not self._panel_apps():
                self._local_moment = self._local_range()[0]
                self._time_changed()
            self._update_recording_buttons()  # a loaded trace can be played

        def sources(self):
            return dict((kind, value) for kind, value in (('database', self.database), ('trace', self.trace))
                        if value is not None)

        # ------------------------------------------------------------ child windows

        def _viewer(self):
            """The Trace Viewer engine behind the windows Trace Viewer and Graph Viewer (None if none is open)."""
            for key in VIEWER_KEYS:
                if key in self.windows:
                    return self.windows[key][1].engine
            return None

        def open_window(self, key, state=None):
            """Open (or bring to front) a child window. state: window state from a configuration;
            without it the window settings from the last time the window was closed are used."""
            if key in self.windows:
                window = self.windows[key][0]
                if getattr(window, 'docked', False):
                    window.pane.tkraise()
                    return
                window.deiconify()
                window.lift()
                window.focus_force()
                return
            if state is None:
                remembered = self.settings.get('windows', {}).get(key)
                state = {'settings': remembered} if isinstance(remembered, dict) else {}
            settings = state.get('settings') if isinstance(state.get('settings'), dict) else None
            docked = self.docked.get()
            if docked:  # a pane in the main window
                window = self._new_pane(key, window_title(key), lambda key=key: self.close_window(key, forget=True),
                                        state.get('dock')).body
            else:
                window = tk.Toplevel(self.root)
                apply_appearance(window)
            try:
                if key == 'database_viewer':
                    from CAN_DatabaseEditor import ViewerApp
                    keep_language(window)  # the Database Editor is in English only
                    app = ViewerApp(window, settings=settings, database=self.database)
                    app.on_saved = self._database_saved
                    app.on_new = self._database_created
                    app.on_open = self.configure_databases
                elif key == 'generator':
                    from CAN_Generator import GeneratorApp
                    app = GeneratorApp(window, settings=settings, database=self.database,
                                       entries=self.generator_entries)
                    app.on_send = self._generator_send
                elif key == 'logic':
                    from CAN_LogicAnalyzer import LogicApp
                    app = LogicApp(window, settings=settings, connection=self.recorder_connection())
                    app.get_bus = self._usbtingo_bus
                elif key in VIEWER_KEYS:
                    from CAN_TraceViewer import TraceViewerApp
                    section = 'trace' if key == 'trace_viewer' else 'graph'
                    engine, new_engine = self._viewer(), False
                    if engine is None:
                        view = state.get('view') if isinstance(state.get('view'), dict) else None
                        sources = dict((kind, value) for kind, value in self.sources().items() if kind != 'panel')
                        engine = TraceViewerApp(window, settings=settings, view=view, sources=sources,
                                                channel=self.channel, section=section)
                        engine.on_live = self._live_frames
                        engine.on_live_environment = self._live_environment
                        engine.on_watch_database = self._generated_database_changed
                        engine.connection = self.recorder_connection()
                        engine.on_source_changed = self._source_rejected
                        engine.on_running_changed = lambda running: self._update_recording_buttons()
                        engine.on_connection_status = self._connection_status
                        engine.attach_menu = self.attach_menu  # also for its selection windows
                        engine.on_cursor_changed = self._cursor_changed
                        if self.selection:
                            engine.set_range(*self.selection)
                        engine.on_generator_add = self._generator_add
                        new_engine = True
                    else:
                        engine.add_section(section, window, settings)
                    app = SectionWindow(engine, section)
                else:
                    from CAN_Panel import PanelViewerApp
                    sources = self.sources()
                    if key in self.panels:
                        sources['panel'] = self.panels[key]
                    app = PanelViewerApp(window, settings=settings, sources=sources, channel=self.channel,
                                         always_fit=self.panel_fit.get())
                    app.on_open_panels = self.add_panels  # panel buttons
            except Exception as error:
                (window.pane if docked else window).destroy()
                messagebox.showerror('CANtrix', str(error), parent=self.root)
                return
            if not docked:
                self.attach_menu(window)
            self.windows[key] = (window, app)
            if key in VIEWER_KEYS and new_engine:
                app.engine.set_source(self.source.get())
            if is_panel_key(key):
                app.set_source(self.source.get())
                viewer = self._viewer()
                if viewer is not None:
                    app.live_environment(viewer.live_environment_values())
                self._assign_master()
                self._local_stop()
                self._master().show_time()
                self._time_changed()
            self._update_recording_buttons()
            if is_panel_key(key) and self._viewer() is not None and app is self._master():
                app.set_cursor(self._viewer().cursor_time)
            window.protocol('WM_DELETE_WINDOW', lambda key=key: self.close_window(key, forget=True))
            window.bind('<Destroy>', lambda event, key=key, window=window: self._on_destroy(event, key, window))

        def _usbtingo_bus(self):
            """Open connection of a running recording with a USBtingo (for the logic recording), else None."""
            viewer = self._viewer()
            recorder = viewer.recorder if viewer is not None else None
            if recorder is None or not recorder.running() or recorder.reader is None:
                return None
            bus = recorder.reader.bus
            return bus if hasattr(bus, 'recording_start') else None

        def _generator_send(self, identifier, data, extended):
            """Generator: send over the connection of the recording. Returns an error text or None."""
            viewer = self._viewer()
            recorder = viewer.recorder if viewer is not None else None
            if recorder is None or not recorder.running():
                return tr('Nicht verbunden: Aufzeichnung (Datenquelle Live) starten.')
            try:
                recorder.send(identifier, data, extended)
            except RuntimeError as error:
                return str(error)
            return None

        def _generator_add(self, identifier, data):
            """Right click in the trace table: the frame becomes an entry of the generator."""
            self.open_window('generator')
            if 'generator' in self.windows:
                self.windows['generator'][1].add_entry(identifier, data)

        def _database_saved(self, paths):
            """The Database Editor saved changes: read the databases again (viewers, status, trace)."""
            if self.database_entries:
                self.set_databases(self.database_entries)

        def _database_created(self, path):
            """The Database Editor created an empty DBC file: add it to the databases of the configuration."""
            entries = [dict(entry) for entry in self.database_entries]
            if path not in [entry['path'] for entry in entries]:
                entries.append({'path': path, 'channel': None})
            self.set_databases(entries)

        def _generated_database_changed(self, old_path, path):
            entries = [entry for entry in self.database_entries
                       if not old_path or old_path == path or entry['path'] != old_path]
            if path and not any(entry['path'] == path for entry in entries):
                entries.append({'path': path, 'channel': None})
            if entries != self.database_entries:
                self.set_databases(entries)

        def _cursor_changed(self, moment):
            """Cursor set in the Trace Viewer: the Panel Viewer shows the values at that time. Without a Panel
            Viewer the main window keeps the time itself (table and graph follow the position)."""
            panel = self._master()
            if panel is not None:
                panel.set_cursor(moment)
            elif moment is not None and self.follow_var.get() and self.source.get() != 'live' \
                    and self._local_range()[1] > self._local_range()[0]:
                self._local_stop()
                self._play_engaged = True
                self._local_set(moment)

        # ------------------------------------------------------------ time without a Panel Viewer

        def _local_range(self):
            frames = (self.trace or {}).get('frames') or []
            return (frames[0].time, frames[-1].time) if frames else (0.0, 0.0)

        def _local_set(self, moment):
            t0, t1 = self._local_range()
            self._local_moment = min(max(moment, t0), t1)
            self._time_changed()

        def _local_speed(self):
            if self.speed_var.get() == '\u221e':
                return float('inf')
            try:
                return float(self.speed_var.get().rstrip('\u00d7').replace(',', '.'))
            except ValueError:
                return 1.0

        def _local_play(self):
            t0, t1 = self._playback_range()
            if self._local_job is not None or t1 <= t0:
                return
            self._play_engaged = True
            self._local_set(t0)  # playing always starts at the beginning
            self._local_last = time.monotonic()
            self._local_job = self.root.after(40, self._local_step)
            self._playing_changed()

        def _local_step(self):
            now = time.monotonic()
            speed = self._local_speed()
            moment = float('inf') if speed == float('inf') else self._local_moment + (now - self._local_last) * speed
            self._local_last = now
            _, end = self._playback_range()
            if moment >= end:
                self._local_job = None
                self._local_set(end)
                self._playing_changed()
                return
            self._local_set(moment)
            self._local_job = self.root.after(40, self._local_step)

        def _local_stop(self):
            if self._local_job is not None:
                self.root.after_cancel(self._local_job)
                self._local_job = None
                self._playing_changed()

        def _playback_range(self):
            """Return the selected time range, or the complete trace when none is selected."""
            t0, t1 = self._local_range()
            if not self.selection:
                return t0, t1
            start, end = self.selection
            return min(max(start, t0), t1), min(max(end, t0), t1)

        def change_source(self):
            """Switch 'Trace'/'Live' (toolbar or menu): Trace Viewer and Panel Viewer together. Live needs the
            Trace Viewer for Start/Stop, so it is opened."""
            source = self.source.get()
            self._reset_position()
            if source == 'live' and self._viewer() is None:
                self.open_window('trace_viewer')  # sets the source of the new window itself
            if self._viewer() is not None:
                self._viewer().set_source(self.source.get())
            for panel in self._panel_apps():
                panel.set_source(self.source.get())
            self._push_range()
            self._place_markers()
            self._update_recording_buttons()

        def on_time_slider(self, value):
            if self._setting_slider:
                return
            panel = self._master()
            self._play_engaged = True  # from now on the Trace Viewer follows the time
            if panel is not None:
                panel.user_time(float(value))
            else:
                self._local_stop()
                self._local_set(float(value))

        def _time_changed(self):
            """The Panel Viewer changed its time or range: move the slider."""
            panel = self._master()
            if panel is not None:
                t0, t1, moment = panel.t0, panel.t1, panel.moment
                for other in self._panel_apps()[1:]:  # the other panels show the same time
                    other.set_time(min(max(moment, other.t0), other.t1))
            else:
                t0, t1 = self._local_range()
                moment = self._local_moment
                self.time_var.set('%.3f / %.3f s' % (moment, t1) if t1 > t0 else '\u2014')
            self._setting_slider = True
            try:
                self.time_slider.configure(from_=t0, to=max(t1, t0 + 1e-6))
                self.time_slider.set(moment)
            except tk.TclError:
                pass
            self._setting_slider = False
            self._place_markers()
            self._push_position()

        # ------------------------------------------------------------ range of the trace

        def _slider_domain(self):
            return float(self.time_slider.cget('from')), float(self.time_slider.cget('to'))

        def _slider_x(self, moment):
            """x position (in the slider) of a time."""
            return float(self.root.tk.splitlist(self.root.tk.call(str(self.time_slider), 'coords', moment))[0])

        def _place_markers(self):
            """The orange bars sit on the slider at the start and end of the selection (hidden without a trace
            and while recording)."""
            try:
                t0, t1 = self._slider_domain()
                show = self.source.get() != 'live' and bool((self.trace or {}).get('frames')) and t1 - t0 > 1e-3
                if not show:
                    for marker in self.range_markers:
                        marker.place_forget()
                    return
                height = self.time_slider.winfo_height() + 14
                for marker, moment in zip(self.range_markers, self.selection or (t0, t1)):
                    marker.place(in_=self.time_slider, x=self._slider_x(moment), rely=0.5, anchor='center',
                                 height=height)
                    marker.lift()
            except (tk.TclError, ValueError, IndexError):  # slider not yet shown or being closed
                pass

        def _range_marker_tooltip(self, index):
            try:
                t0, t1 = self._slider_domain()
                start, end = self.selection or (t0, t1)
                moment = start if index == 0 else end
                return tr('Startzeit: %.3f s') % moment if index == 0 else tr('Endzeit: %.3f s') % moment
            except (tk.TclError, ValueError, TypeError):
                return None

        def _drag_marker(self, index, event):
            try:
                t0, t1 = self._slider_domain()
                x0, x1 = self._slider_x(t0), self._slider_x(t1)
                if x1 <= x0:
                    return
                x = event.x_root - self.time_slider.winfo_rootx()
                moment = min(max(t0 + (x - x0) / (x1 - x0) * (t1 - t0), t0), t1)
            except (tk.TclError, ValueError, IndexError):
                return
            start, end = self.selection or (t0, t1)
            if index == 0:
                start = min(moment, end)
            else:
                end = max(moment, start)
            self.selection = (start, end)
            self._place_markers()

        def _apply_selection(self):
            """Bar released: the selection is complete when it is not the whole trace."""
            try:
                t0, t1 = self._slider_domain()
            except (tk.TclError, ValueError):
                return
            if self.selection and self.selection[0] <= t0 + 1e-6 and self.selection[1] >= t1 - 1e-6:
                self.selection = None
            self._push_range()

        def _push_range(self):
            panel = self._master()
            if panel is not None:
                start, end = (None, None) if self.source.get() == 'live' or not self.selection else self.selection
                panel.set_play_range(start, end)
            viewer = self._viewer()
            if viewer is not None:
                start, end = (None, None) if self.source.get() == 'live' or not self.selection else self.selection
                viewer.set_range(start, end)

        def _playing_changed(self):
            """Playing started or stopped: from the first start on, the Trace Viewer follows the position."""
            if self.playing():
                self._play_engaged = True
            self._push_position()
            self._update_recording_buttons()

        def _push_position(self):
            """Position of the playing to the Trace Viewer (table and graph show only what lies before it)."""
            viewer = self._viewer()
            panel = self._master()
            if viewer is None:
                return
            moment = panel.moment if panel is not None else self._local_moment
            position = moment if self._play_engaged and self.source.get() != 'live' else None
            viewer.set_position(position, self.playing())

        def _reset_position(self):
            """Back to the whole trace in the Trace Viewer (new trace, other source, Panel Viewer closed)."""
            self._play_engaged = False
            self._local_stop()
            self._local_moment = self._local_range()[0]
            self._push_position()

        def toggle_panel_fit(self):
            """Menu "Panel Größe immer optimal": remember it and fit the open panels now."""
            self.settings['panel_fit'] = self.panel_fit.get()
            for app in self._panel_apps():
                app.always_fit = self.panel_fit.get()
                if app.always_fit:
                    app.fit_window()

        def _remember_time_settings(self):
            self.settings['speed'] = self.speed_var.get()
            self.settings['follow_cursor'] = self.follow_var.get()

        def playing(self):
            panel = self._master()
            return panel.playing() if panel is not None else self._local_job is not None

        def recording(self):
            viewer = self._viewer()
            recorder = viewer.recorder if viewer else None
            return recorder is not None and recorder.running()

        def _update_recording_buttons(self):
            """Live: Start records, Stop ends it. Trace: Start plays the trace in the Panel Viewer (if it has a
            trace), Stop pauses. The active one (Start while running, otherwise Stop) is highlighted."""
            if self.source.get() == 'live':
                running = self.recording()
                can_start = not running
            else:
                running = self.playing()
                panel = self._master()
                # Without a Panel Viewer the main window plays (a trace is enough, a panel is not needed).
                can_start = not running and (panel.can_play() if panel is not None
                                             else self._local_range()[1] > self._local_range()[0])
            try:
                # The time controls act on the Panel Viewer in the state Trace.
                active = self.source.get() != 'live' and (bool(self._panel_apps()) or bool(self.trace))
                self.time_slider.state(['!disabled'] if active else ['disabled'])
                self.follow_box.state(['!disabled'] if active else ['disabled'])
                self.speed_box.configure(state='readonly' if active else 'disabled')
                self.run_var.set('start' if running else 'stop')
                self.run_button.state(['!disabled'] if running or can_start else ['disabled'])
            except tk.TclError:  # the main window is being closed
                pass

        def toggle_recording(self):
            """The Start/Stop toggle: stops while running, otherwise starts."""
            if self.run_var.get() == 'start':
                self.stop_recording()
            else:
                self.start_recording()

        def start_recording(self):
            self._start()
            self._update_recording_buttons()  # shows what really happened (e.g. not started)

        def stop_recording(self):
            self._stop()
            self._update_recording_buttons()

        def _start(self):
            """Button Start. Live: the Trace Viewer (opened if needed) records and shows the frames. Trace: the
            Panel Viewer plays the trace."""
            if self.source.get() != 'live':
                if not self._panel_apps() and self.panels:
                    self.show_window('panel_viewer')
                    if self._master() is not None:
                        self._master().root.update()
                if self._master() is not None:
                    self._master().play()
                else:
                    self._local_play()
                return
            if self._viewer() is None:
                self.open_window('trace_viewer')
                if self._viewer() is None:
                    return
                self._viewer().root.update()  # lays the window out and applies the source
            viewer = self._viewer()
            if viewer.recorder is not None:
                viewer.start_live()

        def _stop(self):
            if self.source.get() != 'live':
                if self._master() is not None:
                    self._master().stop()
                else:
                    self._local_stop()
            elif self.recording():
                self._viewer().stop_live()

        def _source_rejected(self, source):
            """The Trace Viewer could not switch (e.g. python-can missing): back to Trace everywhere."""
            self.source.set(source)
            for panel in self._panel_apps():
                panel.set_source(source)
            self._update_recording_buttons()

        def _live_frames(self, messages, reset=False):
            """Messages received by the Trace Recorder: the Panel Viewer can show them live."""
            for panel in self._panel_apps():
                panel.live_frames(messages, reset)

        def _live_environment(self, samples):
            for panel in self._panel_apps():
                panel.live_environment(samples)

        def _window_closed(self, key):
            """A child window is gone: the time controls and Start/Stop follow."""
            try:
                if is_panel_key(key):
                    self._assign_master()  # the next Panel Viewer takes over the time controls
                    if not self._panel_apps():
                        self._reset_position()
                    self._time_changed()  # without a Panel Viewer the main window keeps the time itself
                self._update_recording_buttons()
            except tk.TclError:  # the main window is being closed
                pass

        def _on_destroy(self, event, key, window):
            if event.widget is window and self.windows.get(key, (None,))[0] is window:
                del self.windows[key]
                self._window_closed(key)

        # ------------------------------------------------------------ Trace Viewer

        def toggle_dark_mode(self):
            """Menu 'Dark Mode': switch all windows; the choice is remembered in ~/.cantrix.json."""
            dark = self.dark_var.get()
            set_dark_mode(self.root, dark)
            self._draw_watermark()
            self.settings['dark_mode'] = dark
            for _, app in self.windows.values():
                redraw = getattr(app, 'theme_changed', None)
                if redraw is not None:
                    redraw()

        # ------------------------------------------------------------ CAN channel

        def channel_choices(self):
            """Channels for the selection: those of the loaded trace, without a trace channels 1 to 8."""
            channels = trace_channels(self.trace['frames']) if self.trace else list(DEFAULT_CHANNELS)
            if self.channel is not None and self.channel not in channels:
                channels = sorted(channels + [self.channel], key=natural_key)
            return channels

        def choose_channel(self):
            """Menu 'Einstellungen > CAN-Kanal auswählen…': small modal dialog with a selection box."""
            ChannelDialog(self.root, self.channel_choices(), self.channel, self.set_channel)

        def recorder_connection(self):
            return normalize_connection(self.settings.get('recorder'))

        def configure_recorder(self):
            """Menu 'Einstellungen > CAN Trace Recorder': modal dialog for channel, bitrate, serial number, data rate."""
            RecorderDialog(self.root, self.recorder_connection(), self.set_recorder_connection, self.test_recorder)

        def configure_vector_io(self):
            config = VectorIODialog(self.root, self.vector_io).result
            if config is None:
                return
            try:
                path = create_vector_io_database(config)
            except (OSError, ValueError) as error:
                messagebox.showerror(tr('Vector I/O Configuration'),
                                     tr('Vector-I/O-Datenbasis konnte nicht erstellt werden: %s') % error,
                                     parent=self.root)
                return
            old_path = (self.vector_io or {}).get('database')
            if path:
                config['database'] = path
            self.vector_io = config
            self._generated_database_changed(old_path, path)

        def configure_debugger(self):
            """Menu 'Einstellungen > TRACE32': variables, interval, host and port (kept in the view of the Trace Viewer)."""
            viewer = self._viewer()
            if viewer is None:
                self.open_window('trace_viewer')
                viewer = self._viewer()
            if viewer is not None:
                viewer.configure_debugger()

        def test_recorder(self, connection, done):
            """Test the connection in a thread; done(ok, text) runs in the GUI thread, the status row is updated."""
            try:
                from CAN_TraceRecorder import test_connection
            except ImportError as error:
                done(False, tr('Der Trace Recorder benötigt python-can (%s).') % error)
                return
            viewer = self._viewer()
            recorder = viewer.recorder if viewer is not None else None
            reader = recorder.reader if recorder is not None and recorder.running() else None
            if reader is not None and reader.connection != normalize_connection(connection):
                done(False, tr('Zum Testen geänderter Einstellungen bitte die laufende Aufzeichnung stoppen.'))
                return
            bus = reader.bus if reader is not None else None
            if reader is not None and bus is None:
                done(False, tr('Verbinde ...'))
                return
            outcome = queue.Queue()
            threading.Thread(target=lambda: outcome.put(test_connection(connection, bus)), daemon=True).start()

            def poll():
                try:
                    ok, text = outcome.get_nowait()
                except queue.Empty:
                    self.root.after(100, poll)
                    return
                if not (recorder is not None and recorder.running()):
                    self._connection_status(text.split('\n')[0] if ok else tr('Nicht verbunden'))
                done(ok, text)
            poll()

        def set_recorder_connection(self, connection):
            self.settings['recorder'] = connection
            save_settings(self.settings, SETTINGS_PATH)
            if self._viewer() is not None:
                self._viewer().connection = dict(connection)
            if 'logic' in self.windows:
                self.windows['logic'][1].set_connection(connection)

        def set_channel(self, channel):
            """Use a CAN channel ('1', '2', …; None for all) in the Trace Viewer and the status."""
            self.channel = channel
            self.render_status()
            if self._viewer() is not None:
                self._viewer().set_channel(channel)
            for panel in self._panel_apps():
                panel.set_channel(channel)
            if channel is None:
                set_text(self.status_var, 'CAN-Kanal: alle Kanäle')
            else:
                set_text(self.status_var, 'CAN-Kanal: %s', channel)

        def change_language(self, code):
            """Menu 'Sprache': switch all windows; the choice is remembered in ~/.cantrix.json."""
            for other, variable in self.language_vars.items():
                variable.set(other == code)
            # Always remember an explicit choice, even if it is the system language that is already active.
            if self.settings.get('language') != code:
                self.settings['language'] = code
                save_settings(self.settings, SETTINGS_PATH)
            if code == language():
                return
            set_language(code, self.root)
            self.render_status()
            apps = [app for _, app in self.windows.values()] + [self.plugin_config, self.info, self.about,
                                                                 self.database_config, self.trace_config] + self.plugin_runs
            for app in apps:
                update = getattr(app, 'language_changed', None)
                if update is not None:
                    try:
                        update()
                    except tk.TclError:  # window already closed
                        pass

        def close_window(self, key, forget=False):
            """Close a child window and remember its window settings for the next time it is opened.
            forget: the user closed a Panel Viewer, so its panel leaves the configuration."""
            window, app = self.windows[key]
            # A docked viewer is hosted by a DockFrame inside a FloatingPane.  Keep a
            # reference to the pane while closing it: destroying the DockFrame only
            # schedules destruction of its pane (see FloatingPane._body_destroyed),
            # which can leave a dead pane covering the other panes until Tk reaches
            # the idle queue.  This is especially noticeable when the panel is
            # closed and another window is selected immediately afterwards.
            pane = window.pane if getattr(window, 'docked', False) else None
            confirm = getattr(app, 'confirm_close', None)
            if confirm is not None and not confirm():  # e.g. unsaved changes in the Database Editor
                return False
            del self.windows[key]
            if key == 'generator':
                self.generator_entries = app.collect_entries()
            if forget and is_panel_key(key):
                self.panels.pop(key, None)
                self.render_status()
            self._window_closed(key)
            self._remember_window(key, app)
            if getattr(window, 'docked', False):
                self._remember_pane(key, window.pane)
            try:
                app.close()
            except tk.TclError:
                pass
            if pane is not None:
                try:
                    pane.destroy()
                except tk.TclError:
                    pass
            return True

        def _remember_window(self, key, app):
            """Window settings for the next time the window is opened (a docked view has no geometry of its own:
            the geometry of the floating window is kept)."""
            try:
                settings = dict(app.window_settings())
            except tk.TclError:
                return
            remembered = self.settings.setdefault('windows', {})
            if not settings.get('geometry') and isinstance(remembered.get(key), dict):
                settings = dict(settings, geometry=remembered[key].get('geometry'))
                if not settings['geometry']:
                    del settings['geometry']
            remembered[key] = settings

        # ------------------------------------------------------------ docked view

        def toggle_docked(self):
            """Menu 'Ansicht > Gedockte Ansicht': open windows are closed and opened again in the other form."""
            states = {}
            for key in list(self.windows):
                app = self.windows[key][1]
                state = {'settings': app.window_settings()}
                if key in VIEWER_KEYS:
                    state['view'] = app.engine.collect_view()
                states[key] = state
            for key in states:
                if not self.close_window(key):  # e.g. the user cancelled closing the Database Editor
                    self.docked.set(not self.docked.get())
                    return
            self._docked_changed()
            for key, state in states.items():
                self.open_window(key, state)

        def _docked_changed(self):
            """The docked view was switched (no window open): remember it and rebuild the status summary."""
            if isinstance(self.status_host, FloatingPane):
                self._remember_pane('status', self.status_host)
            self.settings['docked'] = self.docked.get()
            self._build_status()

        def close_windows(self):
            """Close all child windows; False when one of them stays open (the user cancelled)."""
            for key in list(self.windows):
                if key in self.windows and not self.close_window(key):
                    return False
            return True

        # ------------------------------------------------------------ configuration

        def collect_config(self, base=None):
            """Configuration as dict; file paths relative to the folder `base` of the configuration file."""
            def rel(path):
                return relative_path(path, base)

            def rel_entries(entries):
                return [dict(entry, path=rel(entry['path'])) for entry in entries]

            windows = {'main': {'geometry': window_geometry(self.root)}}
            generator = self.windows['generator'][1].collect_entries() if 'generator' in self.windows \
                else self.generator_entries
            # Panel windows are saved by position (panel_viewer, panel_viewer_2, …), like the list of panels.
            positions = dict((key, panel_key(index)) for index, key in enumerate(self.panel_keys()))
            for key, (window, app) in self.windows.items():
                saved_key = positions.get(key, key)
                state = {'open': True}
                try:
                    state['settings'] = app.window_settings()
                except tk.TclError:
                    pass
                if getattr(window, 'docked', False):
                    state['dock'] = window.pane.geometry_dict()  # place in the docked view
                if key in VIEWER_KEYS:
                    state['view'] = app.engine.collect_view()
                windows[saved_key] = state
            vector_io = dict(self.vector_io) if self.vector_io else None
            if vector_io and vector_io.get('database'):
                vector_io['database'] = rel(vector_io['database'])
            # 'database' (first path) keeps the configuration readable for older versions.
            return {'vector_io': vector_io, 'format': CONFIG_FORMAT, 'version': 1, 'docked': self.docked.get(),
                    'speed': self.speed_var.get(), 'follow_cursor': self.follow_var.get(), 'databases': rel_entries(self.database_entries),
                    'database': rel(self.database_entries[0]['path']) if self.database_entries else None,
                    'traces': rel_entries(self.trace_entries),
                    'trace': rel(self.trace_entries[0]['path']) if self.trace_entries else None, 'panel': rel(self.panel_path),
                    'panels': [rel(self.panels[key]['path']) for key in self.panel_keys()], 'generator': generator, 'channel': self.channel, 'windows': windows, 'plugins': self.plugins}

        def save_config(self):
            if self.config_path:
                self.write_config(self.config_path)
            else:
                self.save_config_as()

        def save_config_as(self):
            path = filedialog.asksaveasfilename(
                parent=self.root, title=tr('Konfiguration speichern'), defaultextension='.canconfig',
                initialdir=self._initial_dir(self.config_path or self.settings.get('last_config')),
                initialfile=os.path.basename(self.config_path or 'CANtrix.canconfig'),
                filetypes=config_types())
            if path:
                self.write_config(os.path.abspath(path))

        def write_config(self, path):
            try:
                with open(path, 'w', encoding='utf-8') as handle:
                    json.dump(self.collect_config(os.path.dirname(os.path.abspath(path))), handle, indent=2)
            except OSError as error:
                messagebox.showerror('CANtrix', tr('Speichern fehlgeschlagen:\n%s') % error, parent=self.root)
                return
            self.config_path = path
            self.settings.pop('plugins', None)  # old place of the plugins, now in the configuration
            self.remember_config(path)
            self.render_status()
            set_text(self.status_var, 'Konfiguration gespeichert: %s', os.path.basename(path))

        # ------------------------------------------------------------ recent configurations

        def recent_configs(self):
            recent = self.settings.get('recent_configs')
            if not isinstance(recent, list):
                last = self.settings.get('last_config')
                recent = [last] if isinstance(last, str) else []
            return [path for path in recent if isinstance(path, str)][:RECENT_LIMIT]

        def remember_config(self, path):
            """Put a configuration at the top of the recent list (and save it right away)."""
            self.settings['last_config'] = path
            self.settings['recent_configs'] = ([path] + [item for item in self.recent_configs()
                                                         if item != path])[:RECENT_LIMIT]
            save_settings(self.settings, SETTINGS_PATH)
            self.update_recent_menu()

        def update_recent_menu(self):
            """Recent configurations in the file menu, after a separator below 'speichern unter…'."""
            menu = self.file_menu
            if self._recent_items:
                menu.delete(self._recent_start, self._recent_start + self._recent_items - 1)
            recent = self.recent_configs()
            names = [os.path.basename(path) for path in recent]
            index = self._recent_start
            if recent:
                menu.insert_separator(index)
                index += 1
            for number, path in enumerate(recent, 1):
                label = '%d  %s' % (number, names[number - 1])
                if names.count(names[number - 1]) > 1:  # same file name in different folders
                    label += '  (%s)' % os.path.basename(os.path.dirname(path))
                menu.insert_command(index, label=label, command=lambda path=path: self.open_recent(path))
                index += 1
            self._recent_items = index - self._recent_start

        def open_recent(self, path):
            if not os.path.isfile(path):
                messagebox.showerror('CANtrix', tr('Konfiguration nicht gefunden:\n%s') % path,
                                     parent=self.root)
                self.settings['recent_configs'] = [item for item in self.recent_configs() if item != path]
                save_settings(self.settings, SETTINGS_PATH)
                self.update_recent_menu()
                return
            self.load_config(path)

        def new_config(self):
            """Start empty: close the child windows and forget database, trace and configuration file."""
            if not self.close_windows():  # a window asks about unsaved changes
                return
            self._config_generation += 1
            for kind in self._load_ids:  # results of files still being read are ignored
                self._load_ids[kind] += 1
            self._pending.clear()
            self._update_progress()
            self.database = self.trace = None
            self.selection = None
            self.panels = {}
            self.generator_entries = []
            self.database_entries = []
            self.vector_io = None
            self.trace_entries = []
            if self._database_window() is not None:
                self._database_window().reload([])
            if self._trace_window() is not None:
                self._trace_window().reload([])
            self.errors = []
            self.config_path = None
            self.channel = None
            self.use_plugins([])
            self._play_engaged = False
            set_text(self.status_var, 'Neue Konfiguration')
            self._update_recording_buttons()

        # ------------------------------------------------------------ plugins

        def update_plugin_menu(self):
            """Configured plugins below 'Plugins konfigurieren…', after a separator."""
            menu = self.plugin_menu
            if menu.index(tk.END) > 0:
                menu.delete(1, tk.END)
            if self.plugins:
                menu.add_separator()
            for plugin in self.plugins:
                menu.add_command(label=plugin['name'], command=lambda plugin=plugin: self.run_plugin(plugin))

        def show_info(self):
            """Menu 'Info': README.md in its own window."""
            if self.info is not None and self.info.window.winfo_exists():
                self.info.show()
                return
            self.info = InfoWindow(self.root, self.attach_menu)

        def check_for_update(self):
            """Menu 'Auf Update prüfen': fragt im Hintergrund die neueste Version ab und meldet das Ergebnis (kein Download)."""
            if getattr(self, '_update_running', False):
                return
            self._update_running = True
            results = queue.Queue()

            def work():
                results.put(check_update())

            def poll():
                try:
                    error, latest, newer, asset = results.get_nowait()
                except queue.Empty:
                    self.root.after(100, poll)
                    return
                self._update_running = False
                if error == 'none':
                    messagebox.showinfo('CANtrix', tr('Es wurde keine veröffentlichte Version gefunden.'),
                                        parent=self.root)
                elif error:
                    messagebox.showerror('CANtrix', tr('Die Prüfung ist fehlgeschlagen:\n%s') % error,
                                         parent=self.root)
                elif newer and asset and can_self_update():
                    if messagebox.askyesno('CANtrix', tr('Ein Update ist verfügbar: Version %s\n'
                                                              '(installiert: %s)\n\nJetzt installieren und neu starten?')
                                           % (latest, VERSION), parent=self.root):
                        self.install_update(asset)
                elif newer:
                    if messagebox.askyesno('CANtrix', tr('Ein Update ist verfügbar: Version %s\n'
                                                              '(installiert: %s)\n\nQuellen jetzt herunterladen?')
                                           % (latest, VERSION), parent=self.root):
                        self.download_update(latest)
                else:
                    messagebox.showinfo('CANtrix', tr('Das Programm ist auf dem neuesten Stand (Version %s).')
                                        % VERSION, parent=self.root)

            threading.Thread(target=work, daemon=True).start()
            poll()

        def install_update(self, url):
            """Lädt die App der neuen Version, ersetzt die laufende und startet neu (nur im fertigen Programm)."""
            set_text(self.status_var, 'Update wird geladen …')
            self.root.update_idletasks()
            results = queue.Queue()
            threading.Thread(target=lambda: results.put(install_update(url)), daemon=True).start()

            def poll():
                try:
                    error = results.get_nowait()
                except queue.Empty:
                    self.root.after(100, poll)
                    return
                if error:
                    set_text(self.status_var, 'Update fehlgeschlagen')
                    messagebox.showerror('CANtrix', tr('Das Update ist fehlgeschlagen:\n%s') % error,
                                         parent=self.root)
                    return
                messagebox.showinfo('CANtrix', tr('Das Update wurde installiert. Das Programm startet neu.'),
                                    parent=self.root)
                self.close()  # sichert die Einstellungen; bricht der Nutzer ab (ungespeicherte Änderungen), bleibt es offen
                try:
                    if not self.root.winfo_exists():
                        restart_program()
                except tk.TclError:
                    restart_program()

            poll()

        def download_update(self, tag):
            """Lädt die Quellen der neuen Version als ZIP (Ziel per Dialog) im Hintergrund; ersetzt nichts im Programm."""
            path = filedialog.asksaveasfilename(parent=self.root, title=tr('Quellen speichern unter…'),
                                                initialfile='CANtrix-%s.zip' % tag, defaultextension='.zip',
                                                filetypes=[('ZIP', '*.zip')])
            if not path:
                return
            results = queue.Queue()
            threading.Thread(target=lambda: results.put(download_source(tag, path)), daemon=True).start()

            def poll():
                try:
                    error = results.get_nowait()
                except queue.Empty:
                    self.root.after(100, poll)
                    return
                if error:
                    messagebox.showerror('CANtrix', tr('Der Download ist fehlgeschlagen:\n%s') % error,
                                         parent=self.root)
                else:
                    messagebox.showinfo('CANtrix', tr('Die Quellen wurden gespeichert:\n%s') % path,
                                        parent=self.root)
                    open_file(os.path.dirname(path))

            poll()

        def show_about(self):
            """Menu 'Über CANtrix': icon, copyright and version."""
            if self.about is not None and self.about.window.winfo_exists():
                self.about.show()
                return
            self.about = AboutWindow(self.root)

        def configure_plugins(self):
            if self.plugin_config is not None and self.plugin_config.window.winfo_exists():
                self.plugin_config.window.deiconify()
                self.plugin_config.window.lift()
                return
            self.plugin_config = PluginConfigWindow(self.root, self.plugins, self.set_plugins, self.attach_menu)

        def use_plugins(self, plugins):
            """Plugins of a new or loaded configuration (menu, status and an open plugin window)."""
            self.plugins = normalize_plugins(plugins)
            self.update_plugin_menu()
            self.render_status()
            if self.plugin_config is not None and self.plugin_config.window.winfo_exists():
                self.plugin_config.reload(self.plugins)

        def set_plugins(self, plugins):
            """Changed in the plugin window: stored in the open configuration file right away."""
            self.plugins = normalize_plugins(plugins)
            self.update_plugin_menu()
            self.render_status()
            if not self.config_path:
                set_text(self.status_var, 'Plugins geändert – mit „Konfiguration speichern unter…“ sichern')
                return
            try:
                with open(self.config_path, encoding='utf-8') as handle:
                    config = json.load(handle)
                if not isinstance(config, dict) or config.get('format') not in CONFIG_FORMATS:
                    raise ValueError(tr('Die Datei ist keine CANtrix-Konfiguration.'))
                config['plugins'] = self.plugins
                with open(self.config_path, 'w', encoding='utf-8') as handle:
                    json.dump(config, handle, indent=2)
            except (OSError, ValueError) as error:
                messagebox.showerror('CANtrix', tr('Plugins konnten nicht in der Konfiguration gespeichert '
                                                        'werden:\n%s') % error, parent=self.root)
                return
            set_text(self.status_var, 'Plugins gespeichert in %s', os.path.basename(self.config_path))

        def run_plugin(self, plugin):
            if not os.path.isfile(script_path(plugin)):
                messagebox.showerror('CANtrix', tr('Plugin-Skript nicht gefunden:\n%s') % script_path(plugin),
                                     parent=self.root)
                return
            if plugin['pass_trace'] and not self.trace_path:
                messagebox.showerror('CANtrix', tr('Das Plugin "%s" braucht einen Trace.\n'
                                                        'Bitte zuerst einen CAN-Trace öffnen.') % plugin['name'],
                                     parent=self.root)
                return
            trace_file = self.trace_path
            selection = self.selection if self.source.get() != 'live' else None
            if plugin['pass_trace'] and (len(self.trace.get('traces') or ()) > 1 or selection):
                trace_file = self.write_combined_trace()
                if trace_file is None:
                    return
            try:
                command = build_command(plugin, trace_file)
            except ValueError as error:
                messagebox.showerror('CANtrix', tr('Die Argumente des Plugins sind ungültig (%s).') % error,
                                     parent=self.root)
                return
            self.plugin_runs = [run for run in self.plugin_runs if run.window.winfo_exists()]
            database_paths = [entry['path'] for entry in self.database_entries if existing_file(entry['path'])]
            self.plugin_runs.append(PluginRun(self.root, plugin, command, self.attach_menu, database_paths))
            set_text(self.status_var, 'Plugin gestartet: %s', plugin['name'])

        def write_combined_trace(self):
            """Several traces or a selected range: write the trace (combined / only the range, times from 0) as
            ASC to the temporary folder for a plugin."""
            path = os.path.join(tempfile.gettempdir(), 'CANtrix_combined_trace.asc')
            start = self.trace.get('start')
            begin = self.selection[0] if self.selection else 0.0
            frames, environment = self.trace['frames'], self.trace.get('environment') or {}
            errors = self.trace.get('errors') or []
            if self.selection:
                end = self.selection[1]
                frames = [copy.copy(frame) for frame in frames if begin <= frame.time <= end]
                for frame in frames:
                    frame.time -= begin
                environment = dict((name, [(moment - begin, text) for moment, text in values if begin <= moment <= end])
                                   for name, values in environment.items())
                errors = [(moment - begin, channel, text) for moment, channel, text in errors
                          if begin <= moment <= end]
            try:
                write_trace(path, 'asc', frames, environment,
                            (start.timestamp() if start else time.time()) + begin, errors)
            except OSError as error:
                messagebox.showerror('CANtrix', tr('Der zusammengeführte Trace konnte nicht geschrieben '
                                                        'werden:\n%s') % error, parent=self.root)
                return None
            return path

        def load_config_dialog(self):
            path = filedialog.askopenfilename(
                parent=self.root, title=tr('Konfiguration laden'), filetypes=config_types(),
                initialdir=self._initial_dir(self.config_path or self.settings.get('last_config')))
            if path:
                self.load_config(os.path.abspath(path))

        def _restore_vector_io(self, saved, base, databases):
            config = normalize_vector_io(saved)
            if config is None:
                return None, databases
            saved_path = resolve_path(config.pop('database', None), base)
            path = saved_path if existing_file(saved_path) and config['pins'] else None
            if path is None:
                try:
                    path = create_vector_io_database(config)
                except (OSError, ValueError) as error:
                    messagebox.showerror(tr('Vector I/O Configuration'),
                                         tr('Vector-I/O-Datenbasis konnte nicht erstellt werden: %s') % error,
                                         parent=self.root)
                    return config, databases
            if path:
                config['database'] = path
            if saved_path and saved_path != path:
                databases = [entry for entry in databases if entry['path'] != saved_path]
            if path and not any(entry['path'] == path for entry in databases):
                databases.append({'path': path, 'channel': None})
            return config, databases

        def load_config(self, path):
            try:
                with open(path, encoding='utf-8') as handle:
                    config = json.load(handle)
                if not isinstance(config, dict) or config.get('format') not in CONFIG_FORMATS:
                    raise ValueError(tr('Die Datei ist keine CANtrix-Konfiguration.'))
            except (OSError, ValueError) as error:
                messagebox.showerror('CANtrix', tr('Laden fehlgeschlagen:\n%s') % error, parent=self.root)
                return

            base = os.path.dirname(os.path.abspath(path))  # relative paths count from the configuration file
            databases = [dict(entry, path=resolve_path(entry['path'], base)) for entry in config_databases(config)]
            traces = [dict(entry, path=resolve_path(entry['path'], base)) for entry in config_traces(config)]
            panel_paths = [resolve_path(item, base) for item in config_panels(config)]
            if not self.close_windows():  # a window asks about unsaved changes
                return
            self.vector_io, databases = self._restore_vector_io(config.get('vector_io'), base, databases)
            missing = [entry['path'] for entry in databases + traces if not existing_file(entry['path'])]
            missing.extend(path for path in panel_paths if not existing_file(path))
            self.database = self.trace = None
            self.panels = {}
            self.generator_entries = normalize_entries(config.get('generator'))
            self.database_entries = databases
            self.trace_entries = traces
            if self._database_window() is not None:
                self._database_window().reload(databases)
            if self._trace_window() is not None:
                self._trace_window().reload(traces)
            self.errors = []
            self.config_path = path
            self.remember_config(path)
            channel = config.get('channel')
            self.channel = str(channel) if isinstance(channel, (str, int)) and str(channel) else None
            self.use_plugins(config.get('plugins'))
            self._config_generation += 1
            generation = self._config_generation

            windows = dict(config.get('windows')) if isinstance(config.get('windows'), dict) else {}
            old = windows.get('trace_viewer')
            if isinstance(old, dict) and 'graph_viewer' not in windows:
                # Older configuration: one Trace Viewer with the sections trace and graph.
                old_settings = old.get('settings') if isinstance(old.get('settings'), dict) else {}
                if old_settings.get('show_graph') is not False:
                    windows['graph_viewer'] = dict(old, settings=dict(old_settings, geometry=None))
                    windows['graph_viewer']['settings'].pop('geometry')
                if old_settings.get('show_trace') is False:
                    windows['trace_viewer'] = dict(old, open=False)
            # Docked view and time controls belong to the configuration (older ones: keep the current values).
            if isinstance(config.get('docked'), bool) and config['docked'] != self.docked.get():
                self.docked.set(config['docked'])
                self._docked_changed()
            if config.get('speed') in SPEEDS:
                self.speed_var.set(config['speed'])
            if isinstance(config.get('follow_cursor'), bool):
                self.follow_var.set(config['follow_cursor'])
            main = windows.get('main') if isinstance(windows.get('main'), dict) else {}
            self.root.geometry(initial_geometry(self.root, main, window_geometry(self.root)))

            def open_windows():
                # The windows open after the files were read, so the Trace Viewer can restore its view.
                if generation != self._config_generation:  # a newer configuration was started meanwhile
                    return
                for key, _ in WINDOWS:
                    state = windows.get(key)
                    if not is_panel_key(key) and isinstance(state, dict) and state.get('open'):
                        self.open_window(key, state)
                for index, key in enumerate(self.panel_keys()):  # every loaded panel gets its window
                    state = windows.get(panel_key(index))
                    self.open_window(key, state if isinstance(state, dict) else None)
                if not panel_paths and isinstance(windows.get('panel_viewer'), dict) \
                        and windows['panel_viewer'].get('open'):
                    self.open_window('panel_viewer', windows['panel_viewer'])  # an empty Panel Viewer
                if not self.errors:
                    set_text(self.status_var, 'Konfiguration geladen: %s', os.path.basename(path))
                if missing:
                    messagebox.showwarning('CANtrix', tr('Nicht gefunden:\n%s') % '\n'.join(missing),
                                           parent=self.root)

            existing_panels = [path for path in panel_paths if existing_file(path)]
            request = {'panel': existing_panels} if existing_panels else {}
            if traces:
                request['trace'] = [dict(entry) for entry in traces]  # missing files are reported
            if databases:
                request['database'] = [dict(entry) for entry in databases]  # missing files are reported
            if request:
                self.load_files(request, open_windows)
            else:
                open_windows()

        # ------------------------------------------------------------ closing

        def close(self):
            try:
                if self.root.state() != 'iconic':  # a minimised window has no usable size
                    self.settings['geometry'] = window_geometry(self.root)
                if isinstance(self.status_host, FloatingPane):
                    self._remember_pane('status', self.status_host)
            except tk.TclError:
                pass
            if not self.close_windows():
                return
            for run in self.plugin_runs:  # running plugins end with the analyser
                run.stop()
            save_settings(self.settings, SETTINGS_PATH)
            self.root.after_cancel(self._poll_job)
            self.root.destroy()


if tk is not None:

    class RecorderDialog(object):
        """Modal connection settings of the Trace Recorder."""

        def __init__(self, parent, connection, on_choose, on_test=None):
            connection = normalize_connection(connection)
            self.on_choose = on_choose
            self.on_test = on_test
            window = self.window = tk.Toplevel(parent)
            apply_appearance(window)
            window.title('CAN Trace Recorder')
            window.resizable(False, False)
            window.transient(parent)
            theme = Theme(window)
            card, _ = theme.pane(window, tr('Verbindung'))
            card.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            body = ttk.Frame(card, padding=(16, 12, 16, 16))
            body.pack(fill=tk.BOTH, expand=True)
            self.interface = tk.StringVar(value=RECORDER_INTERFACES[connection.get('interface', 'vector')])
            self.channel = tk.StringVar(value=str(connection['channel']))
            self.bitrate = tk.StringVar(value=str(connection['bitrate']))
            self.serial = tk.StringVar(value='' if connection['serial'] is None else str(connection['serial']))
            self.data_bitrate = tk.StringVar(value=str(connection['data_bitrate']))
            self.mode = tk.StringVar(value='CAN FD' if connection['fd'] else 'CAN')
            self.app_name = tk.StringVar(value=connection['app_name'])
            self.port = tk.StringVar(value=connection['port'])
            self.serial_baud = tk.StringVar(value=str(connection['serial_baud']))
            port_box = ttk.Entry(body, textvariable=self.port, width=30)
            serial_baud_box = ttk.Combobox(body, textvariable=self.serial_baud,
                                           values=(38400, 115200, 230400, 500000), width=12)
            self.saved_timing = connection['timing']
            app_box = ttk.Entry(body, textvariable=self.app_name, width=14)
            data_rate_box = ttk.Combobox(body, textvariable=self.data_bitrate, values=RECORDER_DATA_BITRATES, width=12)
            channel_box = ttk.Spinbox(body, from_=0, to=63, width=12, textvariable=self.channel)
            mode_box = ttk.Combobox(body, textvariable=self.mode, values=['CAN', 'CAN FD'],
                                    width=12, state='readonly')
            serial_box = ttk.Entry(body, textvariable=self.serial, width=14)
            fields = (
                (tr('Schnittstelle:'), ttk.Combobox(body, textvariable=self.interface,
                                                    values=list(RECORDER_INTERFACES.values()), width=24,
                                                    state='readonly')),
                (tr('Modus:'), mode_box),
                (tr('Kanal:'), channel_box),
                ('Bitrate (CAN):', ttk.Combobox(body, textvariable=self.bitrate, values=RECORDER_BITRATES, width=12)),
                ('Bitrate (CAN FD):', data_rate_box),
                (tr('Seriennummer:'), serial_box),
                (tr('Vector-App-Name:'), app_box),
                (tr('Serieller Port:'), port_box),
                (tr('Serielle Baudrate:'), serial_baud_box),
            )
            for row, (text, widget) in enumerate(fields):
                ttk.Label(body, text=text).grid(row=row, column=0, sticky=tk.W, pady=3, padx=(0, 12))
                widget.grid(row=row, column=1, sticky=tk.W, pady=3)
            ttk.Label(body, text=tr('(leer = beliebiges Gerät)'), foreground=C.MUTED,
                      font=theme.font_axis).grid(row=5, column=2, sticky=tk.W, padx=(8, 0))
            self.device = tk.StringVar()
            self.devices = []
            ttk.Label(body, text=tr('Gerät:')).grid(row=len(fields), column=0, sticky=tk.W, pady=3, padx=(0, 12))
            device_row = ttk.Frame(body)
            device_row.grid(row=len(fields), column=1, columnspan=2, sticky=tk.EW, pady=3)
            self.device_box = ttk.Combobox(device_row, textvariable=self.device, values=[], width=34, state='readonly')
            self.device_box.pack(side=tk.LEFT)
            self.device_box.bind('<<ComboboxSelected>>', self.device_selected)
            self.search_button = ttk.Button(device_row, text=tr('Geräte suchen'), command=self.search_devices)
            self.search_button.pack(side=tk.LEFT, padx=(6, 0))

            timing_frame = ttk.LabelFrame(body, text=tr('Vector CAN-FD Bit-Timing'), padding=8)
            timing_frame.grid(row=len(fields) + 1, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
            self.timing_vars = {}
            timing_widgets = []
            for column, label in enumerate(('SJW', 'TSEG1', 'TSEG2'), 1):
                ttk.Label(timing_frame, text=label).grid(row=0, column=column, padx=4)
            for index, key in enumerate(TIMING_FIELDS):
                var = tk.StringVar(value=str(self.saved_timing.get(key, '')))
                box = ttk.Entry(timing_frame, textvariable=var, width=6)
                box.grid(row=1 + index // 3, column=1 + index % 3, padx=4, pady=2)
                self.timing_vars[key] = var
                timing_widgets.append(box)
            ttk.Label(timing_frame, text=tr('Arbitrierung')).grid(row=1, column=0, sticky=tk.W, padx=(0, 6))
            ttk.Label(timing_frame, text=tr('Datenphase')).grid(row=2, column=0, sticky=tk.W, padx=(0, 6))
            calculate = ttk.Button(timing_frame, text=tr('Aus Bitrate berechnen'), command=self.calculate_timing)
            calculate.grid(row=3, column=0, columnspan=4, sticky=tk.W, pady=(6, 0))
            timing_widgets.append(calculate)

            def update_fields(*args):
                interface = self.interface.get()
                obd = interface == RECORDER_INTERFACES['obdlink']
                vector = interface == RECORDER_INTERFACES['vector']
                if obd and self.mode.get() != 'CAN':
                    self.mode.set('CAN')
                fd = self.mode.get() == 'CAN FD' and not obd
                vector_state = ['!disabled'] if vector else ['disabled']
                mode_box.state(['disabled'] if obd else ['!disabled'])
                serial_box.state(['disabled'] if obd else ['!disabled'])
                for widget in (port_box, serial_baud_box):
                    widget.state(['!disabled'] if obd else ['disabled'])
                for widget in (channel_box, app_box, self.search_button, self.device_box):
                    widget.state(vector_state)
                data_rate_box.state(['!disabled'] if fd else ['disabled'])
                for widget in timing_widgets:
                    widget.state(['!disabled'] if fd and vector else ['disabled'])
                if obd:
                    self.obd_hint.grid()
                else:
                    self.obd_hint.grid_remove()

            self.obd_hint = ttk.Label(body, text=tr('OBD: Rechner-Zeitstempel, mögliche Frameverluste bei hoher Buslast.'),
                                      wraplength=370, foreground=C.MUTED)
            self.obd_hint.grid(row=len(fields) + 2, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
            self.interface.trace_add('write', update_fields)
            self.mode.trace_add('write', update_fields)
            update_fields()
            buttons = ttk.Frame(body, padding=(0, 14, 0, 0))
            buttons.grid(row=len(fields) + 4, column=0, columnspan=3, sticky=tk.EW)
            self.result = tk.StringVar()
            ttk.Label(body, textvariable=self.result, wraplength=360, justify=tk.LEFT).grid(
                row=len(fields) + 3, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
            self.test_button = ttk.Button(buttons, text=tr('Verbindung testen'), command=self.test)
            self.test_button.pack(side=tk.LEFT)
            self.test_light = tk.Canvas(buttons, width=12, height=12, background=C.SURFACE, highlightthickness=0)
            self.test_light.create_oval(1, 1, 11, 11, fill=C.SUBTLE, outline='', tags=('status',))
            self.test_light.pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(buttons, text=tr('Abbrechen'), command=window.destroy).pack(side=tk.RIGHT)
            ttk.Button(buttons, text='OK', style='Accent.TButton', command=self.ok).pack(side=tk.RIGHT, padx=6)
            window.bind('<Return>', lambda event: self.ok())
            window.bind('<Escape>', lambda event: window.destroy())
            window.update_idletasks()
            x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_reqwidth()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_reqheight()) // 3
            window.geometry('+%d+%d' % (max(0, x), max(0, y)))
            window.grab_set()

        def search_devices(self):
            """Sucht Vector-Geräte im Hintergrund und füllt die Geräteauswahl."""
            self.search_button.state(['disabled'])
            set_text(self.result, 'Geräte werden gesucht …')
            found = []

            def work():
                try:
                    from CAN_TraceRecorder import detect_vector_devices
                    found.append(detect_vector_devices())
                except Exception as exc:
                    found.append(exc)

            def poll():
                if not self.window.winfo_exists():
                    return
                if not found:
                    self.window.after(100, poll)
                    return
                self.search_button.state(['!disabled'])
                result = found[0]
                if isinstance(result, Exception):
                    self.result.set(tr('Vector-Treiber nicht verfügbar: %s') % result)
                elif not result:
                    self.result.set(tr('Kein Vector-Gerät gefunden.'))
                else:
                    from CAN_TraceRecorder import device_text
                    self.devices = result
                    self.device_box.configure(values=[device_text(device) for device in result])
                    self.device_box.current(0)
                    self.device_selected()
                    self.result.set('')
            threading.Thread(target=work, daemon=True).start()
            poll()

        def device_selected(self, event=None):
            index = self.device_box.current()
            if 0 <= index < len(self.devices):
                device = self.devices[index]
                self.serial.set(str(device['serial']))
                if isinstance(device['channel'], int):
                    self.channel.set(str(device['channel']))
                if device['fd']:
                    self.mode.set('CAN FD')

        def test(self):
            connection = self.read()
            if connection is None or self.on_test is None:
                return
            self.test_button.state(['disabled'])
            self.test_light.itemconfigure('status', fill=C.SUBTLE)
            set_text(self.result, 'Verbindung wird getestet …')

            def done(ok, text):
                if self.window.winfo_exists():
                    self.test_button.state(['!disabled'])
                    self.test_light.itemconfigure('status', fill=C.SUCCESS if ok else C.DANGER)
                    self.result.set(text)
            self.on_test(connection, done)

        def calculate_timing(self):
            try:
                timing = vector_fd_timing(int(self.bitrate.get()), int(self.data_bitrate.get()))
            except ValueError:
                timing = {}
            if not timing:
                messagebox.showinfo('CAN Trace Recorder',
                                    tr('Für diese Bitraten ergibt sich kein ganzzahliges Vector-Timing.'),
                                    parent=self.window)
                return
            for key, value in timing.items():
                self.timing_vars[key].set(str(value))

        def read(self):
            interface = next(key for key, label in RECORDER_INTERFACES.items() if self.interface.get() == label)
            vector, obd = interface == 'vector', interface == 'obdlink'
            fd = not obd and self.mode.get() == 'CAN FD'
            port = self.port.get().strip()
            if obd and not port:
                messagebox.showerror(tr('Eingabefehler'),
                                     tr('Bitte einen seriellen Port für OBDLink / ELM327 auswählen.'),
                                     parent=self.window)
                return None
            try:
                serial = self.serial.get().strip()
                channel = int(self.channel.get()) if vector else 0
                bitrate = int(self.bitrate.get())
                data_bitrate = int(self.data_bitrate.get())
                serial = (int(serial) if serial else None) if vector else (serial or None)
                if channel < 0 or bitrate <= 0 or data_bitrate <= 0 or isinstance(serial, int) and serial < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(tr('Eingabefehler'),
                                     tr('Bitraten müssen positiv sein. Kanal und Vector-Seriennummer müssen '
                                        'nichtnegative ganze Zahlen sein.'), parent=self.window)
                return None
            try:
                serial_baud = int(self.serial_baud.get())
                if serial_baud <= 0:
                    raise ValueError
            except ValueError:
                if obd:
                    messagebox.showerror(tr('Eingabefehler'), tr('Die serielle Baudrate muss eine positive ganze Zahl sein.'),
                                         parent=self.window)
                    return None
                serial_baud = None

            timing = self.saved_timing
            if vector and fd:
                fields = {key: var.get().strip() for key, var in self.timing_vars.items()}
                try:
                    timing = {key: int(value) for key, value in fields.items()} if any(fields.values()) else {}
                    if any(value <= 0 for value in timing.values()):
                        raise ValueError
                except ValueError:
                    messagebox.showerror(tr('Eingabefehler'),
                                         tr('Bitte sechs positive ganze Bit-Timing-Werte eingeben oder alle Felder leeren.'),
                                         parent=self.window)
                    return None
            return normalize_connection({'interface': interface, 'channel': channel, 'bitrate': bitrate,
                                         'serial': serial, 'data_bitrate': data_bitrate, 'fd': fd,
                                         'app_name': self.app_name.get(), 'timing': timing,
                                         'port': port, 'serial_baud': serial_baud})

        def ok(self):
            connection = self.read()
            if connection is None:
                return
            self.window.destroy()
            self.on_choose(connection)

    class ChannelDialog(object):
        """Modal selection of the CAN channel. on_choose(channel) gets '1', '2', … or None (all channels)."""

        def __init__(self, parent, channels, current, on_choose):
            self.channels = [None] + list(channels)
            self.on_choose = on_choose
            window = self.window = tk.Toplevel(parent)
            apply_appearance(window)
            window.title(tr('CAN-Kanal auswählen'))
            window.resizable(False, False)
            window.transient(parent)
            theme = Theme(window)
            card, _ = theme.pane(window, tr('CAN-Kanal'))
            card.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            body = ttk.Frame(card, padding=(16, 12, 16, 16))
            body.pack(fill=tk.BOTH, expand=True)
            names = [tr('Alle Kanäle')] + [tr('Kanal %s') % channel for channel in channels]
            self.choice = tk.StringVar(value=names[self.channels.index(current)] if current in self.channels
                                       else names[0])
            self.names = names
            box = ttk.Combobox(body, textvariable=self.choice, values=names, state='readonly', width=24)
            box.pack(side=tk.TOP, fill=tk.X)
            ttk.Label(body, text=tr('Der Trace Viewer zeigt nur die Frames dieses Kanals.'), foreground=C.MUTED,
                      font=theme.font_axis).pack(side=tk.TOP, anchor=tk.W, pady=(6, 0))
            buttons = ttk.Frame(body, padding=(0, 14, 0, 0))
            buttons.pack(side=tk.TOP, fill=tk.X)
            ttk.Button(buttons, text=tr('Abbrechen'), command=window.destroy).pack(side=tk.RIGHT)
            ttk.Button(buttons, text='OK', style='Accent.TButton', command=self.ok).pack(side=tk.RIGHT, padx=6)
            window.bind('<Return>', lambda event: self.ok())
            window.bind('<Escape>', lambda event: window.destroy())
            # Centred over the main window, modal.
            window.update_idletasks()
            x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_reqwidth()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_reqheight()) // 3
            window.geometry('+%d+%d' % (max(0, x), max(0, y)))
            box.focus_set()
            window.grab_set()

        def ok(self):
            channel = self.channels[self.names.index(self.choice.get())]
            self.window.destroy()
            self.on_choose(channel)


APP_ICON = os.path.join(resource_dir(), 'Bilder', 'CANtrix-Icon.png')
WINDOWS_ICON = os.path.join(resource_dir(), 'Bilder', 'CANtrix.ico')
SPLASH_IMAGE = os.path.join(resource_dir(), 'Bilder', 'CANtrix Automotive Analyse Splashscreen.png')
SPLASH_MILLISECONDS = 1800
SPLASH_MAX_SCREEN_FRACTION = 0.50


def show_splash(root, path=SPLASH_IMAGE):
    """Start image in the middle of the screen. Returns the window or None (see hide_splash)."""
    try:
        image = tk.PhotoImage(master=root, file=path)
        # Larger than 80 % of the screen: reduce by whole factors.
        factor = max(1, -(-image.width() // int(root.winfo_screenwidth() * SPLASH_MAX_SCREEN_FRACTION)),
                     -(-image.height() // int(root.winfo_screenheight() * SPLASH_MAX_SCREEN_FRACTION)))
        if factor > 1:
            image = image.subsample(factor)
        splash = tk.Toplevel(root)
        splash.overrideredirect(True)
        splash.configure(background=C.APP_BG)
        canvas = tk.Canvas(splash, width=image.width(), height=image.height(), borderwidth=0,
                           highlightthickness=0, background=C.APP_BG)
        canvas.create_image(image.width() // 2, image.height() // 2, image=image)
        canvas.create_text(image.width() - 24, image.height() - 18, anchor=tk.SE,
                           text='Version ' + VERSION, fill='#cbd5e1',
                           font=('Helvetica', 13, 'bold'))
        canvas.image = image  # keep a reference
        canvas.pack()
        x = (root.winfo_screenwidth() - image.width()) // 2
        y = (root.winfo_screenheight() - image.height()) // 2
        splash.geometry('+%d+%d' % (x, y))
        splash.update()
        splash.lift()
        splash.update()
        splash.shown_at = time.time()
        return splash
    except (tk.TclError, OSError):  # missing or unreadable image: start without it
        return None


def hide_splash(splash, milliseconds=SPLASH_MILLISECONDS):
    """Keep the start image above the main window until it has been visible for milliseconds, then remove it."""
    if splash is None:
        return
    try:
        splash.attributes('-topmost', True)  # above the main window, which has just been shown
        splash.lift()
        splash.update()
        remaining = max(0, int(milliseconds - (time.time() - splash.shown_at) * 1000))
        splash.after(remaining, splash.destroy)
    except tk.TclError:
        pass


def run_gui(config_path=None, database_paths=None, trace_paths=None, panel_paths=None):
    if tk is None:
        print(tr('Fehler: tkinter ist in dieser Python-Installation nicht verfügbar.'), file=sys.stderr)
        return 1
    remove_old_updates()
    if sys.platform == 'win32':
        import ctypes
        # Group the Python launchers and the packaged EXE under the same Windows taskbar identity.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FrankieOldenburg.CANtrix')
    root = tk.Tk()
    try:
        if sys.platform == 'win32':
            root.iconbitmap(default=WINDOWS_ICON)
        else:
            root.iconphoto(True, tk.PhotoImage(master=root, file=APP_ICON))
    except tk.TclError:
        pass
    apply_appearance(root)
    root.withdraw()  # the start image comes first, then the main window
    splash = show_splash(root)
    app = AnalyserApp(root)
    root.deiconify()
    hide_splash(splash)
    if config_path:
        app.load_config(os.path.abspath(config_path))
    request = {}
    if database_paths:  # -d several times: several databases
        app.database_entries = normalize_databases(database_paths)
        request['database'] = [dict(entry) for entry in app.database_entries]
    if trace_paths:  # -t several times: several traces, combined into one
        app.trace_entries = normalize_traces(trace_paths)
        request['trace'] = [dict(entry) for entry in app.trace_entries]
    if panel_paths:  # -p several times: a Panel Viewer each, shown right away
        request['panel'] = [os.path.abspath(path) for path in panel_paths]
    if request:
        app.load_files(request, (lambda: [app.open_window(key) for key in app.panel_keys()]) if panel_paths else None)
    root.mainloop()
    return 0


def main(argv=None):
    load_language(SETTINGS_PATH)
    parser = argparse.ArgumentParser(description=tr('CAN-Datenbasen und CAN-Traces analysieren.'))
    parser.add_argument('config', nargs='?', help=tr('Konfiguration (.canconfig), die beim Start geladen wird'))
    parser.add_argument('-d', '--database', action='append',
                        help=tr('CAN-Datenbasis (DBC, ARXML, HTML); mehrfach angeben für mehrere Datenbasen'))
    parser.add_argument('-t', '--trace', action='append',
                        help=tr('CAN-Trace (ASC, BLF, MF4, TRC, LOG, CSV, PCAP, VSB …); mehrfach angeben für mehrere '
                                'Traces, die zu einem zusammengeführt werden'))
    parser.add_argument('-p', '--panel', action='append',
                        help=tr('Vector-Panel (.xvp)'))
    args = parser.parse_args(argv)
    return run_gui(args.config, args.database, args.trace, args.panel)


if __name__ == '__main__':
    sys.exit(main())
