#!/usr/bin/env python3
"""Mehrere CAN-Traces für den CANtrix, intern als ein Trace.

Eine Konfiguration hat eine Liste von Traces: [{'path': absoluter Pfad}]. load_traces liest die Dateien
(unveränderte aus einem Cache) und combine_traces führt sie zu einem Trace zusammen, den alle Fenster wie
bisher nutzen:
- Traces mit Startzeit (z. B. ASC "date") werden über die absolute Zeit ausgerichtet: der früheste beginnt
  bei 0, die anderen um die Differenz ihrer Startzeit später. Ein Trace ohne Startzeit schließt an das Ende
  des vorherigen in der Liste an (der erste beginnt bei 0).
- Frames und Werte von Umgebungsvariablen werden nach Zeit sortiert; Status-Zählung und -Ereignisse addiert.
- 'traces' beschreibt die einzelnen Dateien (Name, Pfad, Startzeit, Versatz, Frames).

TraceConfigWindow: Fenster zum Hinzufügen, Ändern und Entfernen, aufgebaut wie das Plugin-Fenster.

Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import os

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None

from CAN_Common import C, Theme, apply_appearance, resolve_path
from CAN_Databases import file_stamp
from CAN_Language import set_text, tr
from CAN_TraceViewer import TRACE_TYPES, Frame, absolute_time_text, de_int, read_trace


def normalize_traces(entries):
    """Valid entries {'path'}: absolute paths without duplicates (strings are accepted as paths)."""
    result, seen = [], set()
    for entry in entries or ():
        path = entry.get('path') if isinstance(entry, dict) else entry
        if not isinstance(path, str) or not path.strip():
            continue
        path = os.path.abspath(path.strip())
        if path not in seen:
            seen.add(path)
            result.append({'path': path})
    return result


def config_traces(config, base=None):
    """Trace list of a configuration: 'traces', or 'trace' (one path) of older versions. Relative paths count from
    `base`, the folder of the configuration file."""
    entries = config.get('traces')
    if not isinstance(entries, list):
        entries = [config['trace']] if isinstance(config.get('trace'), str) else []
    # Resolve first: normalize_traces makes relative paths absolute against the current folder.
    entries = [dict(entry, path=resolve_path(entry.get('path'), base)) if isinstance(entry, dict)
               else resolve_path(entry, base) for entry in entries]
    return normalize_traces(entries)


def combine_traces(traces):
    """One trace from several (dicts as returned by read_trace, in list order); see the module text."""
    if not traces:
        return None
    end_of = lambda trace: max((frame.time for frame in trace['frames']), default=0.0)
    if len(traces) == 1:  # nothing to shift: the trace itself, without copying its frames
        trace = dict(traces[0])
        trace['traces'] = [{'name': trace['name'], 'path': trace['path'], 'start': trace.get('start'),
                            'offset': 0.0, 'frames': len(trace['frames']), 'end': end_of(trace)}]
        return trace
    starts = [trace.get('start') for trace in traces if trace.get('start') is not None]
    first_start = min(starts) if starts else None
    frames, environment, errors, recent, parts = [], {}, [], [], []
    previous_end = 0.0
    for trace in traces:
        start = trace.get('start')
        if start is not None and first_start is not None:
            offset = (start - first_start).total_seconds()
        else:
            offset = previous_end  # no start time: directly after the previous trace
        for frame in trace['frames']:
            frames.append(Frame(frame.time + offset, frame.channel, frame.direction, frame.id, frame.trace_name,
                                frame.data, 0))
        for name, values in (trace.get('environment') or {}).items():
            environment.setdefault(name, []).extend((time + offset, value) for time, value in values)
        errors.extend((time + offset, channel, text) for time, channel, text in trace.get('errors') or ())
        recent.extend('%s: %s' % (trace['name'], line) for line in trace.get('recent') or ())
        end = end_of(trace) + offset
        previous_end = max(previous_end, end)
        parts.append({'name': trace['name'], 'path': trace['path'], 'start': start, 'offset': offset,
                      'frames': len(trace['frames']), 'end': end})
    frames.sort(key=lambda frame: frame.time)
    for number, frame in enumerate(frames, 1):
        frame.line = number  # unique key of a row in the Trace Viewer
    errors.sort(key=lambda error: error[0])
    for values in environment.values():
        values.sort(key=lambda item: item[0])
    if first_start is None and traces[0].get('start') is None:
        start = None
    else:
        start = first_start
    return {'name': ', '.join(part['name'] for part in parts), 'path': parts[0]['path'], 'frames': frames,
            'status_count': sum(trace.get('status_count') or 0 for trace in traces), 'recent': recent[-4:],
            'environment': environment, 'errors': errors, 'start': start, 'traces': parts}


def load_traces(entries, progress=None, cache=None):
    """Read the traces of the entries (worker thread) and combine them. cache: {path: (stamp, trace)} of
    files read before; an unchanged file is not read again. Returns the combined trace (None if none could
    be read) with the extra keys 'errors' and 'cache' (the files read now) in a dict:
    {'trace': …, 'errors': […], 'cache': {…}}."""
    traces, errors, fresh = [], [], {}
    total = len(entries) or 1
    for number, entry in enumerate(entries):
        path = entry['path']
        stamp = file_stamp(path)
        known = (cache or {}).get(path)
        try:
            if known is not None and stamp is not None and known[0] == stamp:
                trace = known[1]
            else:
                trace = read_trace(path, progress and (
                    lambda share, number=number: progress((number + share) / float(total))))
                fresh[path] = (stamp, trace)
        except Exception as error:  # reported in the status
            text = str(error) or error.__class__.__name__
            if isinstance(error, OSError) and getattr(error, 'strerror', None):
                text = '%s: %s' % (error.strerror, error.filename or '')
            errors.append('%s: %s' % (os.path.basename(path), text))
            continue
        traces.append(trace)
        if progress:
            progress((number + 1) / float(total))
    return {'trace': combine_traces(traces), 'errors': errors, 'cache': fresh}


def trace_summary(part):
    """'1.234 Frames · Start 11.08.2026 13:32:42.560 · ab 0.000 s' of one trace of a combined trace."""
    text = tr('%s Frames') % de_int(part['frames'])
    if part.get('start') is not None:
        text += ' · Start %s' % absolute_time_text(part['start'], 0.0)
    return text + tr(' · ab %s s') % ('%.3f' % part['offset'])


if tk is not None:

    class TraceConfigWindow(object):
        """Add, change and remove traces. on_change(entries) gets the new list after every change.
        info(entry): text of the column "Inhalt" ('!' at the start marks an error)."""

        COLUMNS = (('file', 'Datei', 220), ('content', 'Inhalt', 380), ('path', 'Pfad', 320))

        def __init__(self, parent, entries, on_change, info=None, attach_menu=None, initial_dir=None):
            self.entries = [dict(entry) for entry in entries]
            self.on_change = on_change
            self.info = info or (lambda entry: '')
            self.initial_dir = initial_dir
            self.editing = None  # index of the trace shown in the form, None for a new one
            window = self.window = tk.Toplevel(parent)
            apply_appearance(window)
            window.title('CANtrix – ' + tr('CAN-Traces'))
            theme = self.theme = Theme(window)
            self.status = tk.StringVar()
            set_text(self.status, 'Die Traces werden über ihre Startzeit zu einem Trace zusammengeführt.')
            self.path_var = tk.StringVar()
            self.form_title = tk.StringVar(value=tr('Neuer Trace'))
            theme.header(window, tr('CAN-Traces'), tr('CAN-Traces der Konfiguration'))
            theme.footer(window, self.status)

            body = tk.Frame(window, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            # The form is packed first (at the bottom), so it always stays complete; the list gets the rest.
            form_pane, _ = theme.pane(body, variable=self.form_title)
            form_pane.pack(side=tk.BOTTOM, fill=tk.X)
            list_pane, _ = theme.pane(body, tr('Geladene Traces'))
            list_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 12))
            frame = ttk.Frame(list_pane, padding=(4, 0, 0, 4))
            frame.pack(fill=tk.BOTH, expand=True)
            tree = self.tree = ttk.Treeview(frame, columns=[c[0] for c in self.COLUMNS], show='headings',
                                            selectmode='browse', height=5)
            for key, title, width in self.COLUMNS:
                tree.heading(key, text=tr(title), anchor=tk.W)
                tree.column(key, width=width, minwidth=50, anchor=tk.W, stretch=key in ('content', 'path'))
            tree.tag_configure('error', foreground=C.DANGER)
            scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            tree.bind('<<TreeviewSelect>>', self.on_select)

            form = ttk.Frame(form_pane, padding=(12, 10, 12, 12))
            form.pack(fill=tk.X)
            form.columnconfigure(1, weight=1)
            ttk.Label(form, text=tr('Datei')).grid(row=0, column=0, sticky=tk.W, padx=(0, 10), pady=4)
            ttk.Entry(form, textvariable=self.path_var).grid(row=0, column=1, sticky=tk.EW, pady=4)
            ttk.Button(form, text=tr('Auswählen…'), command=self.choose_files).grid(row=0, column=2, padx=(6, 0),
                                                                                   pady=4)
            ttk.Label(form, text=tr('Traces mit Startzeit werden über die absolute Zeit ausgerichtet; ein Trace '
                                    'ohne Startzeit schließt an den vorherigen in der Liste an.'),
                      foreground=C.MUTED, font=theme.font_axis).grid(row=1, column=1, columnspan=2, sticky=tk.W)

            buttons = ttk.Frame(form, padding=(0, 12, 0, 0))
            buttons.grid(row=2, column=0, columnspan=3, sticky=tk.EW)
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
            window.update_idletasks()
            width, height = max(900, window.winfo_reqwidth()), max(520, window.winfo_reqheight())
            window.geometry('%dx%d' % (width, height))
            window.minsize(min(width, 680), window.winfo_reqheight())

        def reload(self, entries):
            """Show the traces of another configuration (after loading or starting a new one)."""
            self.entries = [dict(entry) for entry in entries]
            self.refresh()
            self.new()

        def update_info(self):
            """Refresh the column "Inhalt" (e.g. after the files were read)."""
            selection = self.tree.selection()
            self.refresh(select=int(selection[0]) if selection else None)

        def language_changed(self):
            self.update_info()

        def refresh(self, select=None):
            tree = self.tree
            tree.delete(*tree.get_children())
            for index, entry in enumerate(self.entries):
                info = self.info(entry)
                missing = not os.path.isfile(entry['path'])
                tree.insert('', tk.END, iid=str(index), tags=('error',) if missing or info.startswith('!') else (),
                            values=(os.path.basename(entry['path']),
                                    tr('Datei fehlt') if missing else info.lstrip('!'), entry['path']))
            if select is not None and select < len(self.entries):
                tree.selection_set(str(select))
                tree.see(str(select))

        def on_select(self, _event=None):
            selection = self.tree.selection()
            if not selection:
                return
            self.editing = int(selection[0])
            self.path_var.set(self.entries[self.editing]['path'])
            self.form_title.set(tr('Trace bearbeiten'))
            self.save_button.configure(text=tr('Übernehmen'))
            self.remove_button.state(['!disabled'])

        def new(self):
            self.editing = None
            self.tree.selection_set(())
            self.path_var.set('')
            self.form_title.set(tr('Neuer Trace'))
            self.save_button.configure(text=tr('Hinzufügen'))
            self.remove_button.state(['disabled'])

        def _check(self, path, skip=None):
            if not path:
                return tr('Bitte eine Datei auswählen.')
            if not os.path.isfile(path):
                return tr('Datei nicht gefunden:\n%s') % path
            if any(index != skip and entry['path'] == path for index, entry in enumerate(self.entries)):
                return tr('Dieser Trace ist schon vorhanden:\n%s') % path
            return None

        def choose_files(self):
            """Add one or more trace files as new traces (also while one is being edited)."""
            current = self.path_var.get().strip()
            initial = os.path.dirname(current) if current else self.initial_dir
            paths = filedialog.askopenfilenames(
                parent=self.window, title=tr('CAN-Traces auswählen'),
                initialdir=initial if initial and os.path.isdir(initial) else None,
                filetypes=[(tr('CAN-Trace'), ' '.join('*' + ending for ending in TRACE_TYPES)),
                           (tr('Alle Dateien'), '*')])
            paths = self.window.tk.splitlist(paths) if isinstance(paths, str) else list(paths or ())
            if not paths:
                return
            added, rejected = [], []
            for path in paths:
                path = os.path.abspath(path)
                error = self._check(path)
                if error:
                    rejected.append(error)
                    continue
                self.entries.append({'path': path})
                added.append(os.path.basename(path))
            if added:
                self.on_change([dict(entry) for entry in self.entries])
                self.refresh(select=len(self.entries) - 1)
                self.on_select()
                set_text(self.status, 'Hinzugefügt: %s', ', '.join(added))
            if rejected:
                messagebox.showerror('CANtrix', '\n\n'.join(rejected), parent=self.window)

        def save(self):
            path = self.path_var.get().strip()
            path = os.path.abspath(path) if path else ''
            error = self._check(path, skip=self.editing)
            if error:
                messagebox.showerror('CANtrix', error, parent=self.window)
                return
            if self.editing is None:
                self.entries.append({'path': path})
                index = len(self.entries) - 1
                set_text(self.status, 'Trace hinzugefügt: %s', os.path.basename(path))
            else:
                index = self.editing
                self.entries[index] = {'path': path}
                set_text(self.status, 'Trace geändert: %s', os.path.basename(path))
            self.on_change([dict(item) for item in self.entries])
            self.refresh(select=index)
            self.on_select()

        def remove(self):
            if self.editing is None:
                return
            entry = self.entries.pop(self.editing)
            self.on_change([dict(item) for item in self.entries])
            self.refresh()
            self.new()
            set_text(self.status, 'Trace entfernt: %s', os.path.basename(entry['path']))
