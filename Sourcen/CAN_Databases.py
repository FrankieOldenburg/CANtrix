#!/usr/bin/env python3
"""Mehrere CAN-Datenbasen für den CANtrix.

Eine Konfiguration hat eine Liste von Datenbasen: [{'path': absoluter Pfad, 'channel': '1', … oder None}].
Eine Datenbasis mit CAN-Kanal gilt nur für die Frames dieses Kanals (und hat dort Vorrang), ohne Kanal
für alle Kanäle. load_databases liest die Dateien (unveränderte aus einem Cache) und führt sie mit
combine_databases (CAN_Common.py) zu einer Datenbasis zusammen, die alle Fenster wie bisher nutzen.

DatabaseConfigWindow: Fenster zum Hinzufügen, Ändern und Entfernen, aufgebaut wie das Plugin-Fenster.

Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import os

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None

from CAN_Common import DATABASE_TYPES, C, Theme, apply_appearance, combine_databases, resolve_path
from CAN_Language import set_text, tr
from CAN_TraceViewer import de_int, natural_key, read_database

DEFAULT_CHANNELS = [str(number) for number in range(1, 9)]


def normalize_databases(entries):
    """Valid entries {'path', 'channel'}: absolute paths, channel as text or None, no duplicates."""
    result, seen = [], set()
    for entry in entries or ():
        if isinstance(entry, str):
            entry = {'path': entry}
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not entry['path'].strip():
            continue
        channel = entry.get('channel')
        channel = str(channel).strip() if isinstance(channel, (str, int)) and str(channel).strip() else None
        item = {'path': os.path.abspath(entry['path'].strip()), 'channel': channel}
        key = (item['path'], channel)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def config_databases(config, base=None):
    """Database list of a configuration: 'databases', or 'database' (one path) of older versions. Relative paths count from
    `base`, the folder of the configuration file."""
    entries = config.get('databases')
    if not isinstance(entries, list):
        entries = [config['database']] if isinstance(config.get('database'), str) else []
    # Resolve first: normalize_databases makes relative paths absolute against the current folder.
    entries = [dict(entry, path=resolve_path(entry.get('path'), base)) if isinstance(entry, dict)
               else resolve_path(entry, base) for entry in entries]
    return normalize_databases(entries)


def file_stamp(path):
    try:
        info = os.stat(path)
        return info.st_mtime, info.st_size
    except OSError:
        return None


def load_databases(entries, progress=None, cache=None):
    """Read the databases of the entries (worker thread). cache: {path: (stamp, database)} of files read
    before; an unchanged file is not read again. Returns the combined database with the extra keys
    'errors' (texts of files that could not be read) and 'cache' (the files read now)."""
    parts, errors, fresh = [], [], {}
    total = len(entries) or 1
    for number, entry in enumerate(entries):
        path = entry['path']
        stamp = file_stamp(path)
        known = (cache or {}).get(path) or fresh.get(path)
        try:
            if known is not None and stamp is not None and known[0] == stamp:
                database = known[1]
            else:
                database = read_database(path, progress and (
                    lambda share, number=number: progress((number + share) / float(total))))
                fresh[path] = (stamp, database)
        except Exception as error:  # reported in the status
            text = str(error) or error.__class__.__name__
            if isinstance(error, OSError) and getattr(error, 'strerror', None):
                text = '%s: %s' % (error.strerror, error.filename or '')
            errors.append('%s: %s' % (os.path.basename(path), text))
            continue
        parts.append((database, entry['channel']))
        if progress:
            progress((number + 1) / float(total))
    combined = combine_databases(parts)
    combined['errors'] = errors
    combined['cache'] = fresh
    return combined


def channel_choices(channels=()):
    """CAN channels to choose from: those of the trace and 1–8."""
    return sorted(set(DEFAULT_CHANNELS) | set(channel for channel in channels if channel), key=natural_key)


def channel_text(channel):
    return tr('Kanal %s') % channel if channel else tr('Alle Kanäle')


if tk is not None:

    class DatabaseConfigWindow(object):
        """Add, change and remove databases. on_change(entries) gets the new list after every change.
        channels(): CAN channels of the loaded trace; info(entry): text of the column "Inhalt"."""

        COLUMNS = (('file', 'Datei', 220), ('channel', 'CAN-Kanal', 110), ('content', 'Inhalt', 210),
                   ('path', 'Pfad', 320))

        def __init__(self, parent, entries, on_change, channels=None, info=None, attach_menu=None,
                     initial_dir=None):
            self.entries = [dict(entry) for entry in entries]
            self.on_change = on_change
            self.channels = channels or (lambda: [])
            self.info = info or (lambda entry: '')
            self.initial_dir = initial_dir
            self.editing = None  # index of the database shown in the form, None for a new one
            window = self.window = tk.Toplevel(parent)
            apply_appearance(window)
            window.title('CANtrix – ' + tr('Datenbasen'))
            theme = self.theme = Theme(window)
            self.status = tk.StringVar()
            set_text(self.status, 'Datenbasen: DBC, ARXML oder HTML-Message-Catalogue')
            self.path_var = tk.StringVar()
            self.channel_var = tk.StringVar()
            self.form_title = tk.StringVar(value=tr('Neue Datenbasis'))
            theme.header(window, tr('Datenbasen'), tr('CAN-Datenbasen der Konfiguration'))
            theme.footer(window, self.status)

            body = tk.Frame(window, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

            # The form is packed first (at the bottom), so it always stays complete; the list gets the rest.
            form_pane, _ = theme.pane(body, variable=self.form_title)
            form_pane.pack(side=tk.BOTTOM, fill=tk.X)
            list_pane, _ = theme.pane(body, tr('Geladene Datenbasen'))
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
            ttk.Label(form, text=tr('CAN-Kanal')).grid(row=1, column=0, sticky=tk.W, padx=(0, 10), pady=4)
            self.channel_box = ttk.Combobox(form, textvariable=self.channel_var, state='readonly', width=18)
            self.channel_box.grid(row=1, column=1, sticky=tk.W, pady=4)
            ttk.Label(form, text=tr('Mit CAN-Kanal gilt die Datenbasis nur für die Frames dieses Kanals und hat '
                                    'dort Vorrang; mit "Alle Kanäle" für alle Frames.'),
                      foreground=C.MUTED, font=theme.font_axis).grid(row=2, column=1, columnspan=2, sticky=tk.W)

            buttons = ttk.Frame(form, padding=(0, 12, 0, 0))
            buttons.grid(row=3, column=0, columnspan=3, sticky=tk.EW)
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
            width, height = max(900, window.winfo_reqwidth()), max(560, window.winfo_reqheight())
            window.geometry('%dx%d' % (width, height))
            window.minsize(min(width, 680), window.winfo_reqheight())

        # ---------------------------------------------------------------- list

        def reload(self, entries):
            """Show the databases of another configuration (after loading or starting a new one)."""
            self.entries = [dict(entry) for entry in entries]
            self.refresh()
            self.new()

        def update_info(self):
            """Refresh the column "Inhalt" (e.g. after the files were read)."""
            selection = self.tree.selection()
            self.refresh(select=int(selection[0]) if selection else None)

        def language_changed(self):
            self.update_info()
            self._fill_channels(self._form_channel())

        def refresh(self, select=None):
            tree = self.tree
            tree.delete(*tree.get_children())
            for index, entry in enumerate(self.entries):
                info = self.info(entry)
                missing = not os.path.isfile(entry['path'])
                tree.insert('', tk.END, iid=str(index), tags=('error',) if missing or info.startswith('!') else (),
                            values=(os.path.basename(entry['path']), channel_text(entry['channel']),
                                    tr('Datei fehlt') if missing else info.lstrip('!'), entry['path']))
            if select is not None and select < len(self.entries):
                tree.selection_set(str(select))
                tree.see(str(select))

        # ---------------------------------------------------------------- form

        def _fill_channels(self, current=None):
            """Choices of the field CAN-Kanal: all channels, then 1–8 and the channels of the trace."""
            self._channel_values = [None] + channel_choices(self.channels())
            if current is not None and current not in self._channel_values:
                self._channel_values.append(current)
            self.channel_box.configure(values=[channel_text(channel) for channel in self._channel_values])
            self.channel_var.set(channel_text(current))

        def _form_channel(self):
            names = [channel_text(channel) for channel in getattr(self, '_channel_values', [None])]
            value = self.channel_var.get()
            return self._channel_values[names.index(value)] if value in names else None

        def on_select(self, _event=None):
            selection = self.tree.selection()
            if not selection:
                return
            self.editing = int(selection[0])
            entry = self.entries[self.editing]
            self.path_var.set(entry['path'])
            self._fill_channels(entry['channel'])
            self.form_title.set(tr('Datenbasis bearbeiten'))
            self.save_button.configure(text=tr('Übernehmen'))
            self.remove_button.state(['!disabled'])

        def new(self):
            self.editing = None
            self.tree.selection_set(())
            self.path_var.set('')
            self._fill_channels(None)
            self.form_title.set(tr('Neue Datenbasis'))
            self.save_button.configure(text=tr('Hinzufügen'))
            self.remove_button.state(['disabled'])

        def _check(self, path, channel, skip=None):
            """Error text for an entry, or None."""
            if not path:
                return tr('Bitte eine Datei auswählen.')
            if os.path.splitext(path)[1].lower() not in DATABASE_TYPES:
                return tr('Datenbasen müssen DBC-, ARXML- oder HTML-Dateien sein:\n%s') % path
            if not os.path.isfile(path):
                return tr('Datei nicht gefunden:\n%s') % path
            if any(index != skip and entry['path'] == path and entry['channel'] == channel
                   for index, entry in enumerate(self.entries)):
                return tr('Diese Datenbasis ist für diesen Kanal schon vorhanden:\n%s') % path
            return None

        def choose_files(self):
            """Add one or more database files as new databases (also while one is being edited); they get
            the CAN channel chosen in the form."""
            current = self.path_var.get().strip()
            initial = os.path.dirname(current) if current else self.initial_dir
            paths = filedialog.askopenfilenames(
                parent=self.window, title=tr('Datenbasen auswählen'),
                initialdir=initial if initial and os.path.isdir(initial) else None,
                filetypes=[(tr('CAN-Datenbasis'), '*.dbc *.arxml *.html *.htm'), (tr('Alle Dateien'), '*')])
            paths = self.window.tk.splitlist(paths) if isinstance(paths, str) else list(paths or ())
            if not paths:
                return
            channel = self._form_channel()
            added, rejected = [], []
            for path in paths:
                path = os.path.abspath(path)
                error = self._check(path, channel)
                if error:
                    rejected.append(error)
                    continue
                self.entries.append({'path': path, 'channel': channel})
                added.append(os.path.basename(path))
            if added:
                self.on_change([dict(entry) for entry in self.entries])
                self.refresh(select=len(self.entries) - 1)
                self.on_select()  # the form shows the last added database, e.g. to change its channel
                set_text(self.status, 'Hinzugefügt: %s – der CAN-Kanal lässt sich hier ändern (Übernehmen).',
                         ', '.join(added))
            if rejected:
                messagebox.showerror('CANtrix', '\n\n'.join(rejected), parent=self.window)

        def save(self):
            path = self.path_var.get().strip()
            path = os.path.abspath(path) if path else ''
            channel = self._form_channel()
            error = self._check(path, channel, skip=self.editing)
            if error:
                messagebox.showerror('CANtrix', error, parent=self.window)
                return
            entry = {'path': path, 'channel': channel}
            if self.editing is None:
                self.entries.append(entry)
                index = len(self.entries) - 1
                set_text(self.status, 'Datenbasis hinzugefügt: %s', os.path.basename(path))
            else:
                index = self.editing
                self.entries[index] = entry
                set_text(self.status, 'Datenbasis geändert: %s', os.path.basename(path))
            self.on_change([dict(item) for item in self.entries])
            self.refresh(select=index)
            self.on_select()  # the form now edits the saved database

        def remove(self):
            if self.editing is None:
                return
            entry = self.entries.pop(self.editing)
            self.on_change([dict(item) for item in self.entries])
            self.refresh()
            self.new()
            set_text(self.status, 'Datenbasis entfernt: %s', os.path.basename(entry['path']))


def database_summary(database):
    """'3 Nachrichten · 11 Signale' of a (single) database."""
    messages = database.get('messages') or []
    return tr('%s Nachrichten · %s Signale') % (de_int(len(messages)),
                                               de_int(sum(len(message['signals']) for message in messages)))
