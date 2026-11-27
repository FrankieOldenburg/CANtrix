#!/usr/bin/env python3
"""Plugins des CANtrixs.

Ein Plugin ist ein Python-Skript, meist im Ordner Plugins/ (auch in Unterordnern), sonst
beliebig. Der CANtrix startet es als eigenen Prozess im Ordner des Skripts und zeigt seine Ausgabe in einem Fenster.
Optional bekommt es den Pfad des aktuell geladenen Traces als Argument oder, im Live-Modus,
die Botschaften der laufenden Aufzeichnung.

Eine Plugin-Einstellung ist ein dict:
    {'name': Menütext, 'script': Pfad relativ zu Plugins/, außerhalb davon absolut (mit '/'),
     'arguments': zusätzliche Argumente, 'mode': 'trace' | 'live' | 'none',
     'pass_trace': mode == 'trace' (für ältere Versionen)}
Im Live-Modus wird --live angehängt. Das Plugin liest von stdin je Botschaft eine ASC-Zeile
(zuerst die bisherige Aufzeichnung); das Ende von stdin ist das Ende der Aufzeichnung.
Ausgabezeilen dürfen ANSI-Farbcodes (ESC[31m … ESC[0m) enthalten.
Steht in den Argumenten der Platzhalter {trace}, wird dort der Trace-Pfad eingesetzt;
sonst wird er (bei pass_trace) als letztes Argument angehängt.
Die geladenen Datenbasen bekommt jedes Plugin in der Umgebungsvariable CANTRIX_DATABASES
(Pfade, getrennt durch os.pathsep, in der Reihenfolge der Konfiguration).

Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from CAN_Common import C, Theme, apply_appearance, is_frozen, program_dir
from CAN_Language import set_text, tr
from CAN_TraceWriters import asc_line, is_fd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = program_dir()  # Projektordner (die Skripte liegen in Sourcen/), in der exe der Ordner daneben
PLUGINS_DIR = os.path.join(ROOT, 'Plugins')
TRACE_PLACEHOLDER = '{trace}'
LIVE_ARGUMENT = '--live'
MODES = (('trace', 'Trace übergeben'), ('live', 'Live-Modus'), ('none', 'Ohne Trace'))
LIVE_OUTPUT_LINES = 20000  # older output of a live plugin is removed
ANSI_RE = re.compile('\x1b\\[([0-9;]*)m')
ANSI_TAGS = {'31': 'red', '32': 'green', '33': 'orange', '34': 'blue', '35': 'violet', '36': 'blue',
             '90': 'info', '91': 'red', '92': 'olive'}


# ---------------------------------------------------------------- plugin settings

def normalize_plugins(items):
    """Valid plugin entries from the settings file."""
    plugins = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get('script'), str) and item['script'].strip():
            script = item['script'].strip()
            name = str(item.get('name') or '').strip() or os.path.splitext(os.path.basename(script))[0]
            mode = item.get('mode')
            if mode not in [key for key, _title in MODES]:
                mode = 'trace' if item.get('pass_trace') is True else 'none'
            plugins.append({'name': name, 'script': script, 'arguments': str(item.get('arguments') or ''),
                            'mode': mode, 'pass_trace': mode == 'trace'})
    return plugins


def script_path(plugin):
    # join keeps an absolute script path as it is
    return os.path.normpath(os.path.join(PLUGINS_DIR, plugin['script']))


def stored_script(path):
    """How a script is saved: relative to the Plugins folder if inside it, else absolute (with '/')."""
    path = os.path.abspath(path)
    try:
        relative = os.path.relpath(path, PLUGINS_DIR)
    except ValueError:  # Windows: another drive
        return path.replace(os.sep, '/')
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return path.replace(os.sep, '/')
    return relative.replace(os.sep, '/')


def split_arguments(text):
    if os.name != 'nt':
        return shlex.split(text)
    # Windows paths keep their backslashes; only surrounding quotes are removed.
    return [part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part
            for part in shlex.split(text, posix=False)]


MACOS_PYTHONS = ('/Library/Frameworks/Python.framework/Versions/Current/bin/python3',
                 '/opt/homebrew/bin/python3', '/usr/local/bin/python3')


def python_executable():
    """The running Python; on Windows python.exe instead of pythonw.exe, so the plugin has an stdout."""
    if is_frozen():  # the executable cannot run scripts: use a Python of the system
        if sys.platform == 'darwin':
            # A double-clicked app has a short PATH: /usr/bin/python3 (Xcode, Tk 8.5) would show black windows.
            for found in MACOS_PYTHONS:
                if os.path.isfile(found) and os.access(found, os.X_OK):
                    return found
        for name in (('py', 'python') if os.name == 'nt' else ('python3', 'python')):
            found = shutil.which(name)
            if found:
                return found
        return 'python'
    executable = sys.executable
    if os.name == 'nt' and os.path.basename(executable).lower() == 'pythonw.exe':
        console = os.path.join(os.path.dirname(executable), 'python.exe')
        if os.path.isfile(console):
            executable = console
    return executable


def build_command(plugin, trace_path=None):
    """Command line for a plugin. Raises ValueError if the arguments cannot be parsed."""
    arguments = split_arguments(plugin['arguments'])
    if plugin['pass_trace']:
        if any(TRACE_PLACEHOLDER in argument for argument in arguments):
            arguments = [argument.replace(TRACE_PLACEHOLDER, trace_path) for argument in arguments]
        else:
            arguments.append(trace_path)
    else:
        arguments = [argument for argument in arguments if argument != TRACE_PLACEHOLDER]
        if plugin.get('mode') == 'live' and LIVE_ARGUMENT not in arguments:
            arguments.append(LIVE_ARGUMENT)
    return [python_executable(), '-u', script_path(plugin)] + arguments


# ---------------------------------------------------------------- live data

def live_lines(frames):
    """The ASC lines of recorded frames as bytes (CAN FD frames also with up to 8 bytes)."""
    lines = []
    for frame in frames:
        number = int(frame.channel) if str(frame.channel).isdigit() else 1
        lines.append(asc_line(frame, number, frame.fd or is_fd(frame)))
    return ''.join(lines).encode('utf-8')


def ansi_runs(text, tag=None):
    """Split output with ANSI colour codes into [(text, tag)]; returns (runs, tag still active)."""
    runs = []
    position = 0
    for match in ANSI_RE.finditer(text):
        if match.start() > position:
            runs.append((text[position:match.start()], tag))
        tag = ANSI_TAGS.get(match.group(1).split(';')[-1])
        position = match.end()
    if position < len(text):
        runs.append((text[position:], tag))
    return runs, tag


# ---------------------------------------------------------------- output window

class PluginRun(object):
    """Runs a plugin and shows its output (stdout and stderr) live in its own window.

    A live plugin gets the frames of the recording on stdin: it waits ('waiting') until a recording runs,
    is fed while it does ('active') and its input ends with the recording ('done'). channel: only this CAN
    channel (None: all)."""

    def __init__(self, parent, plugin, command, attach_menu=None, database_paths=(), channel=None):
        self.plugin = plugin
        self.live = plugin.get('mode') == 'live'
        self.channel = channel
        self.state = 'waiting' if self.live else None
        self.input = queue.Queue()  # lists of frames for the writer thread, None ends the input
        self.color = None  # ANSI colour still active at the end of the last output
        self.database_paths = list(database_paths)
        self.command = command
        self.lines = queue.Queue()
        self.process = None
        window = self.window = tk.Toplevel(parent)
        apply_appearance(window)
        window.title('CANtrix – Plugin %s' % plugin['name'])
        window.geometry('900x560')
        window.minsize(500, 300)
        theme = self.theme = Theme(window)
        self.status = tk.StringVar()
        set_text(self.status, 'Startet…')
        theme.header(window, plugin['name'], 'Plugin')
        footer = theme.footer(window, self.status)
        self.stop_button = ttk.Button(footer, text=tr('Abbrechen'), command=self.stop)
        self.stop_button.pack(side=tk.RIGHT, padx=8, pady=4)

        pane, bar = theme.pane(window, tr('Ausgabe'))
        pane.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        self.command_var = tk.StringVar(value=os.path.basename(command[2]))
        theme.bar_label(bar, self.command_var)
        frame = ttk.Frame(pane, padding=(4, 0, 0, 4))
        frame.pack(fill=tk.BOTH, expand=True)
        text = self.text = tk.Text(frame, wrap=tk.NONE, borderwidth=0, highlightthickness=0, padx=12, pady=8,
                                   background=C.SURFACE, foreground=C.INK, insertbackground=C.INK,
                                   selectbackground=C.ACCENT_SELECT, font=theme.font_mono)
        text.tag_configure('info', foreground=C.MUTED)
        text.tag_configure('error', foreground=C.DANGER)
        text.tag_configure('ok', foreground=C.SUCCESS)
        for tag, color in (('red', C.DANGER), ('green', C.SUCCESS), ('orange', C.SERIES_COLORS[1]),
                           ('blue', C.SERIES_COLORS[5]), ('violet', C.SERIES_COLORS[3]),
                           ('olive', C.SERIES_COLORS[7])):
            text.tag_configure(tag, foreground=color)
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        xscroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=text.xview)
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set, state=tk.DISABLED)
        text.grid(row=0, column=0, sticky='nsew')
        yscroll.grid(row=0, column=1, sticky='ns')
        xscroll.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        if attach_menu is not None:
            attach_menu(window)
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.start()

    def write(self, line, tag=None):
        text = self.text
        at_end = text.yview()[1] >= 0.999
        text.configure(state=tk.NORMAL)
        text.insert(tk.END, line, (tag,) if tag else ())
        if self.live:
            excess = int(text.index('end-1c').split('.')[0]) - LIVE_OUTPUT_LINES
            if excess > 0:
                text.delete('1.0', '%d.0' % (excess + 1))
        text.configure(state=tk.DISABLED)
        if at_end:
            text.see(tk.END)

    def start(self):
        cwd = os.path.dirname(self.command[2])
        self.write('$ %s\n%s\n\n' % (' '.join(self.command[2:]), tr('(Ordner: %s)') % cwd), 'info')
        environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1',
                           CANTRIX_DATABASES=os.pathsep.join(self.database_paths))
        flags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW: no extra console on Windows
        try:
            self.process = subprocess.Popen(self.command, cwd=cwd, env=environment,
                                            stdin=subprocess.PIPE if self.live else subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            creationflags=flags)
        except OSError as error:
            self.write(tr('Start fehlgeschlagen: %s') % error + '\n', 'error')
            set_text(self.status, 'Start fehlgeschlagen')
            self.stop_button.state(['disabled'])
            return
        set_text(self.status, 'Wartet auf Aufzeichnung…' if self.live else 'Läuft…')
        threading.Thread(target=self._read, daemon=True).start()
        if self.live:
            threading.Thread(target=self._write_input, daemon=True).start()
        self._poll_job = self.window.after(50, self.poll)

    def _read(self):
        for raw in iter(self.process.stdout.readline, b''):
            self.lines.put(raw.decode('utf-8', errors='replace'))
        self.process.stdout.close()
        self.lines.put(None)

    def _write_input(self):
        """Thread: frames to the plugin, so a slow plugin never blocks the window."""
        pipe = self.process.stdin
        try:
            while True:
                frames = self.input.get()
                if frames is None:
                    break
                pipe.write(live_lines(frames))
                pipe.flush()
        except (OSError, ValueError):  # the plugin has ended
            pass
        try:
            pipe.close()
        except (OSError, ValueError):
            pass

    def begin_recording(self, frames=(), dropped=0):
        """A recording runs: frames are those recorded so far, dropped the number already forgotten."""
        if self.state != 'waiting' or not self.running():
            return
        self.state = 'active'
        set_text(self.status, 'Live: empfängt Botschaften…')
        if dropped:
            self.write(tr('Hinweis: Die ersten %d Botschaften der Aufzeichnung sind nicht mehr vorhanden.')
                       % dropped + '\n', 'error')
        self.feed(frames)

    def feed(self, frames):
        if self.state != 'active' or not self.running():
            return
        frames = [frame for frame in frames if self.channel is None or frame.channel == self.channel]
        if frames:
            self.input.put(frames)

    def end_recording(self):
        """The recording ended: close the input, the plugin can finish."""
        if self.state != 'active':
            return
        self.state = 'done'
        self.input.put(None)
        if self.running():
            set_text(self.status, 'Aufzeichnung beendet, Plugin schließt ab…')

    def poll(self):
        chunk = []
        finished = False
        try:
            while len(chunk) < 2000:
                line = self.lines.get_nowait()
                if line is None:
                    finished = True
                    break
                chunk.append(line)
        except queue.Empty:
            pass
        if chunk:
            runs, self.color = ansi_runs(''.join(chunk), self.color)
            for part, tag in runs:
                self.write(part, tag)
        if finished:
            code = self.process.wait()
            if self.live:
                self.state = 'done'
                self.input.put(None)
            ok = code == 0
            self.write('\n%s\n' % (tr('Beendet mit Code %d') % code), 'ok' if ok else 'error')
            if ok:
                set_text(self.status, 'Fertig')
            else:
                set_text(self.status, 'Beendet mit Fehler (Code %d)', code)
            self.stop_button.state(['disabled'])
            return
        self._poll_job = self.window.after(50, self.poll)

    def running(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.running():
            self.process.terminate()
            set_text(self.status, 'Wird abgebrochen…')

    def close(self):
        if self.running():
            if not messagebox.askyesno('CANtrix', tr('Das Plugin läuft noch. Abbrechen und Fenster schließen?'),
                                       parent=self.window):
                return
            self.process.terminate()
        self.window.destroy()


# ---------------------------------------------------------------- configuration window

class PluginConfigWindow(object):
    """Add, change and remove plugins. on_change(plugins) gets the new list after every change."""

    COLUMNS = (('name', 'Name', 180), ('script', 'Skript', 260), ('mode', 'Modus', 130), ('arguments', 'Argumente', 180))

    def __init__(self, parent, plugins, on_change, attach_menu=None):
        self.plugins = [dict(plugin) for plugin in plugins]
        self.on_change = on_change
        self.editing = None  # index of the plugin shown in the form, None for a new one
        window = self.window = tk.Toplevel(parent)
        apply_appearance(window)
        window.title('CANtrix – Plugins')
        theme = self.theme = Theme(window)
        self.status = tk.StringVar()
        set_text(self.status, 'Plugins sind Python-Skripte im Ordner %s', PLUGINS_DIR)
        self.name_var = tk.StringVar()
        self.script_var = tk.StringVar()
        self.arguments_var = tk.StringVar()
        self.mode_var = tk.StringVar(value='trace')
        self.form_title = tk.StringVar(value=tr('Neues Plugin'))
        theme.header(window, 'Plugins', tr('Python-Skripte'))
        theme.footer(window, self.status)

        body = tk.Frame(window, background=C.APP_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        # The form is packed first (at the bottom), so it always stays complete; the list gets the rest.
        form_pane, _ = theme.pane(body, variable=self.form_title)
        form_pane.pack(side=tk.BOTTOM, fill=tk.X)
        list_pane, _ = theme.pane(body, tr('Konfigurierte Plugins'))
        list_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 12))
        frame = ttk.Frame(list_pane, padding=(4, 0, 0, 4))
        frame.pack(fill=tk.BOTH, expand=True)
        tree = self.tree = ttk.Treeview(frame, columns=[c[0] for c in self.COLUMNS], show='headings',
                                        selectmode='browse', height=5)
        for key, title, width in self.COLUMNS:
            tree.heading(key, text=tr(title), anchor=tk.W)
            tree.column(key, width=width, minwidth=50, anchor=tk.W,
                        stretch=key in ('script', 'arguments'))
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.bind('<<TreeviewSelect>>', self.on_select)

        form = ttk.Frame(form_pane, padding=(12, 10, 12, 12))
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text=tr('Name')).grid(row=0, column=0, sticky=tk.W, padx=(0, 10), pady=4)
        ttk.Entry(form, textvariable=self.name_var).grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=4)
        ttk.Label(form, text=tr('Skript')).grid(row=1, column=0, sticky=tk.W, padx=(0, 10), pady=4)
        ttk.Entry(form, textvariable=self.script_var).grid(row=1, column=1, sticky=tk.EW, pady=4)
        ttk.Button(form, text=tr('Auswählen…'), command=self.choose_script).grid(row=1, column=2, padx=(6, 0), pady=4)
        ttk.Label(form, text=tr('Argumente')).grid(row=2, column=0, sticky=tk.W, padx=(0, 10), pady=4)
        ttk.Entry(form, textvariable=self.arguments_var).grid(row=2, column=1, columnspan=2, sticky=tk.EW, pady=4)
        # TRACE_PLACEHOLDER is written out, so the label keeps a fixed text for a language switch.
        ttk.Label(form, text=tr('Optional, z. B. --serve. Mit {trace} wird der Trace an dieser Stelle eingesetzt, '
                                'sonst am Ende.'),
                  foreground=C.MUTED, font=theme.font_axis).grid(row=3, column=1, columnspan=2, sticky=tk.W)
        ttk.Label(form, text=tr('Modus')).grid(row=4, column=0, sticky=tk.W, padx=(0, 10), pady=(6, 0))
        modes = ttk.Frame(form)
        modes.grid(row=4, column=1, columnspan=2, sticky=tk.W, pady=(6, 0))
        for key, title in MODES:
            ttk.Radiobutton(modes, text=tr(title), value=key,
                            variable=self.mode_var).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(form, text=tr('Im Live-Modus erhält das Plugin die Botschaften der Aufzeichnung und das Argument '
                                '--live.'),
                  foreground=C.MUTED, font=theme.font_axis).grid(row=5, column=1, columnspan=2, sticky=tk.W)

        buttons = ttk.Frame(form, padding=(0, 12, 0, 0))
        buttons.grid(row=6, column=0, columnspan=3, sticky=tk.EW)
        ttk.Button(buttons, text=tr('Neu'), command=self.new).pack(side=tk.LEFT)
        self.remove_button = ttk.Button(buttons, text=tr('Entfernen'), command=self.remove)
        self.remove_button.pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text=tr('Schließen'), command=window.destroy).pack(side=tk.RIGHT)
        self.save_button = ttk.Button(buttons, text=tr('Hinzufügen'), style='Accent.TButton', command=self.save)
        self.save_button.pack(side=tk.RIGHT, padx=6)

        if attach_menu is not None:
            attach_menu(window)
        self.refresh()
        self.new()
        # Size from what the content needs, so nothing is cut off (fonts differ between systems).
        window.update_idletasks()
        width, height = max(860, window.winfo_reqwidth()), max(600, window.winfo_reqheight())
        window.geometry('%dx%d' % (width, height))
        window.minsize(min(width, 680), window.winfo_reqheight())

    def reload(self, plugins):
        """Show the plugins of another configuration (after loading or starting a new one)."""
        self.plugins = [dict(plugin) for plugin in plugins]
        self.refresh()
        self.new()

    def language_changed(self):
        """Texts in the list (the mode); labels and buttons are translated by set_language."""
        selection = self.tree.selection()
        self.refresh(select=int(selection[0]) if selection else None)

    def refresh(self, select=None):
        tree = self.tree
        tree.delete(*tree.get_children())
        for index, plugin in enumerate(self.plugins):
            tree.insert('', tk.END, iid=str(index), values=(
                plugin['name'], plugin['script'], tr(dict(MODES)[plugin['mode']]),
                plugin['arguments']))
        if select is not None:
            tree.selection_set(str(select))
            tree.see(str(select))

    def on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        self.editing = int(selection[0])
        plugin = self.plugins[self.editing]
        self.name_var.set(plugin['name'])
        self.script_var.set(plugin['script'])
        self.arguments_var.set(plugin['arguments'])
        self.mode_var.set(plugin['mode'])
        self.form_title.set(tr('Plugin bearbeiten'))
        self.save_button.configure(text=tr('Übernehmen'))
        self.remove_button.state(['!disabled'])

    def new(self):
        self.editing = None
        self.tree.selection_set(())
        for variable in (self.name_var, self.script_var, self.arguments_var):
            variable.set('')
        self.mode_var.set('trace')
        self.form_title.set(tr('Neues Plugin'))
        self.save_button.configure(text=tr('Hinzufügen'))
        self.remove_button.state(['disabled'])

    def choose_script(self):
        """Add one or more scripts as new plugins (also while one is being edited)."""
        if not os.path.isdir(PLUGINS_DIR):
            os.makedirs(PLUGINS_DIR)
        current = self.script_var.get().strip()
        initial = os.path.dirname(script_path({'script': current})) if current else PLUGINS_DIR
        paths = filedialog.askopenfilenames(parent=self.window, title=tr('Plugin-Skripte auswählen'),
                                            initialdir=initial if os.path.isdir(initial) else PLUGINS_DIR,
                                            filetypes=[(tr('Python-Skript'), '*.py'), (tr('Alle Dateien'), '*')])
        paths = self.window.tk.splitlist(paths) if isinstance(paths, str) else list(paths or ())
        if not paths:
            return
        added, rejected = [], []
        for path in paths:
            if not path.lower().endswith('.py') or not os.path.isfile(path):
                rejected.append(os.path.basename(path))
                continue
            plugin = normalize_plugins([{'script': stored_script(path), 'mode': 'trace'}])[0]
            self.plugins.append(plugin)
            added.append(plugin['name'])
        if added:
            self.on_change([dict(item) for item in self.plugins])
            self.refresh(select=len(self.plugins) - 1)
            self.on_select()  # the form shows the last added plugin, e.g. to add arguments
            set_text(self.status, 'Hinzugefügt: %s – Name, Argumente und Modus lassen sich hier ändern '
                                  '(Übernehmen).', ', '.join(added))
        if rejected:
            messagebox.showerror('CANtrix', tr('Nicht hinzugefügt (nur .py-Dateien):\n%s') % '\n'.join(rejected),
                                 parent=self.window)

    def save(self):
        script = self.script_var.get().strip().replace('\\', '/')
        error = None
        if not script:
            error = tr('Bitte ein Skript auswählen.')
        elif not script.lower().endswith('.py'):
            error = tr('Plugins müssen Python-Skripte (.py) sein.')
        elif not os.path.isfile(script_path({'script': script})):
            error = tr('Skript nicht gefunden:\n%s') % script_path({'script': script})
        else:
            try:
                split_arguments(self.arguments_var.get())
            except ValueError as problem:
                error = tr('Die Argumente sind ungültig (%s).') % problem
        if error:
            messagebox.showerror('CANtrix', error, parent=self.window)
            return
        plugin = normalize_plugins([{'name': self.name_var.get(),
                                     'script': stored_script(script_path({'script': script})),
                                     'arguments': self.arguments_var.get().strip(),
                                     'mode': self.mode_var.get()}])[0]
        if self.editing is None:
            self.plugins.append(plugin)
            index = len(self.plugins) - 1
            set_text(self.status, 'Plugin hinzugefügt: %s – Name, Argumente und Modus lassen sich jetzt '
                                  'noch ändern (Übernehmen).', plugin['name'])
        else:
            index = self.editing
            self.plugins[index] = plugin
            set_text(self.status, 'Plugin geändert: %s', plugin['name'])
        self.on_change([dict(item) for item in self.plugins])
        self.refresh(select=index)
        self.on_select()  # the form now edits the saved plugin

    def remove(self):
        if self.editing is None:
            return
        plugin = self.plugins.pop(self.editing)
        self.on_change([dict(item) for item in self.plugins])
        self.refresh()
        self.new()
        set_text(self.status, 'Plugin entfernt: %s', plugin['name'])
