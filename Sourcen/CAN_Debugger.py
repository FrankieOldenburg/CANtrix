"""Werte von Variablen eines Lauterbach TRACE32 Debuggers (Remote API) für den Trace Viewer.

Nur Standardbibliothek (Python 3.6); zum Lesen wird das Paket lauterbach-trace32-rcl gebraucht
(pip install lauterbach-trace32-rcl)."""

import hashlib
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from CAN_Common import C, decode_text
from CAN_DbcEditor import write_generated_dbc
from CAN_Language import tr

DEFAULTS = {'host': 'localhost', 'port': 20000, 'variables': '', 'interval': 100}


def normalize_settings(value):
    """Gültige Einstellungen (Host, Port, Variablen, Intervall in ms) aus einem gespeicherten Wert."""
    settings = dict(DEFAULTS)
    if isinstance(value, dict):
        if isinstance(value.get('host'), str) and value['host'].strip():
            settings['host'] = value['host'].strip()
        if isinstance(value.get('port'), int) and 0 < value['port'] < 65536:
            settings['port'] = value['port']
        if isinstance(value.get('variables'), str):
            settings['variables'] = value['variables']
        if isinstance(value.get('interval'), int) and value['interval'] >= 10:
            settings['interval'] = value['interval']
        imported = value.get('watch_variables')
        if isinstance(imported, list):
            watched = set(variable_names(settings))
            names = [name for name in imported if isinstance(name, str) and name in watched]
            if names:
                settings['watch_variables'] = sorted(set(names))
        if isinstance(value.get('environment_database'), str) and value['environment_database']:
            settings['environment_database'] = value['environment_database']
    return settings


def variable_names(settings):
    return [name.strip() for name in settings['variables'].replace('\n', ',').split(',') if name.strip()]


def watch_names(text):
    """Read variable names from CANcase-style VAR.ADDWATCH commands in a TRACE32 script."""
    return list(dict.fromkeys(re.findall(r'^\s*VAR\.ADDWATCH[ \t]+(\S+)', text, re.IGNORECASE | re.MULTILINE)))


def watch_environment_names(settings):
    """Map imported TRACE32 symbols to stable, valid DBC identifiers."""
    names = {}
    for symbol in settings.get('watch_variables', ()):
        name = 'TRACE32_' + symbol
        if len(name) > 32 or not re.fullmatch(r'[A-Za-z0-9_]+', name):
            base = re.sub(r'[^A-Za-z0-9_]', '_', name)
            suffix = hashlib.sha256(symbol.encode('utf-8')).hexdigest()[:10]
            name = base[:21] + '_' + suffix
        names[symbol] = name
    return names


def create_watch_database(names, directory=None):
    """Save an environment-only DBC and reuse identical generated files."""
    if not names:
        return None
    fields = {name: {'name': name, 'type': 'float', 'minimum': '-1.7976931348623157e308',
                     'maximum': '1.7976931348623157e308', 'access': 'read',
                     'description': 'TRACE32: ' + symbol} for symbol, name in names.items()}
    return write_generated_dbc(fields, 'TRACE32', 'TRACE32-Watch', directory)


def read_t32_value(debugger, name):
    """Read a scalar or, as in CANcase, fall back to reading the symbol as raw bytes."""
    try:
        return debugger.variable.read(name).value
    except Exception as scalar_error:
        try:
            address = debugger.fnc.var_address(name)
            size = debugger.fnc.var_sizeof(name)
            if size <= 0:
                raise ValueError(tr('Ungültige Symbolgröße: %s') % size)
            return list(debugger.memory.read(address, length=size))
        except Exception as array_error:
            raise RuntimeError(tr('Skalar: %s | Array/Struktur: %s') % (scalar_error, array_error))


class T32Poller(threading.Thread):
    """Liest die Variablen periodisch und legt Wertänderungen als ('var', Zeit, Name, Text) und den
    Verbindungsstatus als ('status', Zustand, Textvorlage, *Argumente) in die Queue."""

    def __init__(self, settings, out_queue):
        super().__init__(daemon=True)
        self.node, self.port = settings['host'], settings['port']
        self.names = list(dict.fromkeys(variable_names(settings)))
        self.interval = settings['interval'] / 1000.0
        self.out_queue = out_queue
        self._stop_event = threading.Event()

    def run(self):
        try:
            import lauterbach.trace32.rcl as t32
            dbg = t32.connect(node=self.node, port=self.port)
        except ModuleNotFoundError as error:
            if error.name and error.name.split('.')[0] == 'lauterbach':
                self.out_queue.put(('status', 'error', 'TRACE32-Paket fehlt: lauterbach-trace32-rcl.'))
            else:
                self.out_queue.put(('status', 'error', 'TRACE32 Verbindung fehlgeschlagen: %s', str(error)))
            return
        except Exception as error:
            self.out_queue.put(('status', 'error', 'TRACE32 Verbindung fehlgeschlagen: %s', str(error)))
            return
        self.out_queue.put(('status', 'connected', 'TRACE32 verbunden (%s:%s), %d Variablen',
                            self.node, self.port, len(self.names)))
        last = {}
        try:
            while not self._stop_event.is_set():
                for name in self.names:
                    try:
                        sample = str(read_t32_value(dbg, name)), True
                    except Exception as error:
                        sample = 'ERR: %s' % error, False
                    if last.get(name) != sample:
                        last[name] = sample
                        self.out_queue.put(('var', time.time(), name, *sample))
                self._stop_event.wait(self.interval)
        finally:
            try:
                dbg.disconnect()
            except Exception:
                pass
            self.out_queue.put(('status', 'idle', 'TRACE32 getrennt'))

    def stop(self):
        self._stop_event.set()


class DebuggerDialog(object):
    """Modaler Dialog für Host, Port, Variablen und Intervall. `result` = Einstellungen oder None."""

    def __init__(self, parent, settings):
        self.result = None
        self.watch_variables = set(settings.get('watch_variables', ()))
        window = self.window = tk.Toplevel(parent)
        window.title(tr('TRACE32-Variablen'))
        window.configure(background=C.APP_BG)
        window.transient(parent)
        body = ttk.Frame(window, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        self.host = tk.StringVar(value=settings['host'])
        self.port = tk.StringVar(value=str(settings['port']))
        self.interval = tk.StringVar(value=str(settings['interval']))
        for row, (label, variable) in enumerate(((tr('TRACE32 Host'), self.host), (tr('Port'), self.port),
                                                 (tr('Intervall (ms)'), self.interval))):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=3)
            ttk.Entry(body, textvariable=variable, width=24).grid(row=row, column=1, sticky='ew', padx=(8, 0))
        ttk.Label(body, text=tr('Variablen (eine pro Zeile oder mit Komma getrennt)')).grid(
            row=3, column=0, columnspan=2, sticky='w', pady=(8, 3))
        self.text = tk.Text(body, width=44, height=8, wrap='word', relief='flat', background=C.SURFACE,
                            foreground=C.INK, insertbackground=C.INK, highlightthickness=1,
                            highlightbackground=C.LINE)
        self.text.grid(row=4, column=0, columnspan=2, sticky='nsew')
        self.text.insert('1.0', settings['variables'])
        self.error = tk.StringVar()
        ttk.Label(body, textvariable=self.error, foreground=C.DANGER).grid(row=5, column=0, columnspan=2, sticky='w')
        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky='e', pady=(8, 0))
        ttk.Button(buttons, text=tr('Abbrechen'), command=window.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text='OK', style='Accent.TButton', command=self.accept).pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(body, text=tr('Watch-Datei öffnen…'), command=self.import_watch_file).grid(
            row=6, column=0, sticky='w', pady=(8, 0), padx=(0, 8))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(4, weight=1)
        window.bind('<Escape>', lambda event: window.destroy())
        window.grab_set()
        window.wait_window()

    def import_watch_file(self):
        path = filedialog.askopenfilename(parent=self.window, title=tr('Watch-Datei öffnen…'),
                                          filetypes=[('TRACE32', '*.cmm'), (tr('Alle Dateien'), '*.*')])
        if not path:
            return
        try:
            with open(path, 'rb') as handle:
                names = watch_names(decode_text(handle.read()))
        except OSError as error:
            messagebox.showerror(tr('TRACE32-Variablen'), str(error), parent=self.window)
            return
        if not names:
            self.error.set(tr('Keine VAR.ADDWATCH-Einträge gefunden.'))
            return
        self.watch_variables.update(names)
        existing = variable_names({'variables': self.text.get('1.0', 'end')})
        self.text.delete('1.0', 'end')
        self.text.insert('1.0', '\n'.join(dict.fromkeys(existing + names)))
        self.error.set('')

    def accept(self):
        try:
            port, interval = int(self.port.get()), int(self.interval.get())
            if not 0 < port < 65536 or interval < 10:
                raise ValueError
        except ValueError:
            self.error.set(tr('Port und Intervall (mindestens 10 ms) müssen Zahlen sein.'))
            return
        self.result = normalize_settings({'host': self.host.get(), 'port': port,
                                          'variables': self.text.get('1.0', 'end').strip(), 'interval': interval,
                                          'watch_variables': list(self.watch_variables)})
        self.window.destroy()
