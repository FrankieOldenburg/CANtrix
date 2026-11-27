#!/usr/bin/env python3
"""Sprache der Oberfläche (Deutsch oder Englisch).

Die Texte stehen im Code auf Deutsch und werden mit tr() übersetzt; EN enthält die englischen
Übersetzungen (Schlüssel = deutscher Text, auch mit Platzhaltern wie %s). Texte, die schon
englisch sind (z. B. im Database Editor), bleiben unverändert und stehen nicht in EN.

Umschalten zur Laufzeit (set_language): wie beim Dark Mode werden die Fenster nicht neu aufgebaut,
sondern die Texte aller vorhandenen Widgets ersetzt (Beschriftungen, Menüeinträge, Tabellenköpfe,
Fenstertitel und Textvariablen). Formatierte Texte in Textvariablen (Statuszeilen) setzt man mit
set_text(), dann werden sie beim Umschalten neu formatiert. Inhalte, die die Fenster selbst
erzeugen (Tabellenzeilen, Grafiken, Textfelder), zeichnen sie danach in language_changed() neu.

Ohne gespeicherte Sprache gilt die Sprache des Betriebssystems (system_language): Deutsch, wenn das
System auf Deutsch steht, sonst Englisch (auch wenn sie sich nicht feststellen lässt).

Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import locale
import os
import plistlib
import sys

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    tk = None

LANGUAGES = (('de', 'Deutsch'), ('en', 'English'))  # Menütexte bleiben in ihrer eigenen Sprache
FALLBACK_LANGUAGE = 'en'  # if the language of the system is unknown or not supported
SETTINGS_PATH = os.path.join(os.path.expanduser('~'), '.cantrix.json')  # same file as the analyser
MAC_PREFERENCES = os.path.join(os.path.expanduser('~'), 'Library', 'Preferences', '.GlobalPreferences.plist')


def _system_locale_names():
    """Preferred languages/locales of the operating system, most preferred first (e.g. ['de-DE', 'en-US'])."""
    names = []
    if sys.platform == 'darwin':
        try:  # the language list of the system settings; LANG is often missing or 'C' for GUI programs
            with open(MAC_PREFERENCES, 'rb') as handle:
                preferences = plistlib.load(handle)
            names.extend(str(name) for name in preferences.get('AppleLanguages') or [])
            if preferences.get('AppleLocale'):
                names.append(str(preferences['AppleLocale']))
        except Exception:  # missing or unreadable file
            pass
    elif sys.platform.startswith('win'):
        try:
            import ctypes
            language_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            names.append(locale.windows_locale.get(language_id, ''))
        except Exception:
            pass
    for variable in ('LC_ALL', 'LC_MESSAGES', 'LANG', 'LANGUAGE'):
        names.extend(os.environ.get(variable, '').split(':'))
    try:
        names.append(locale.getlocale()[0] or '')
    except (ValueError, TypeError):
        pass
    return [name for name in names if name and name.split('.')[0].upper() not in ('C', 'POSIX')]


def system_language():
    """'de' if the operating system's preferred language is German, otherwise 'en'."""
    names = _system_locale_names()
    if names and names[0].replace('-', '_').lower().split('_')[0] in ('de', 'german', 'deutsch'):
        return 'de'
    return FALLBACK_LANGUAGE


DEFAULT_LANGUAGE = system_language()  # used while no language is stored

_state = {'language': DEFAULT_LANGUAGE}
_texts = {}  # name of a Tk variable -> (template, args, shown text), see set_text


def language():
    return _state['language']


def normalize_language(code):
    return code if code in dict(LANGUAGES) else DEFAULT_LANGUAGE


def load_language(path=SETTINGS_PATH):
    """Use the language stored by the analyser ('language' in ~/.cantrix.json), e.g. for a viewer
    started on its own."""
    from CAN_Common import load_settings
    set_language(load_settings(path).get('language'))


def tr(text):
    """Text in the current language; text is the German original."""
    if _state['language'] == 'en':
        return EN.get(text, text)
    return text


def _render(template, args):
    # Arguments may be functions (e.g. a number formatted for the language), called at every rendering.
    args = tuple(arg() if callable(arg) else arg for arg in args)
    return tr(template) % args if args else tr(template)


def set_text(variable, template, *args):
    """Set a Tk variable to a translated, formatted text that follows a later language switch.
    An argument can be a function without parameters; it is called again after a switch."""
    text = _render(template, args)
    variable.set(text)
    _texts[str(variable)] = (template, args, text)


def set_language(code, root=None):
    """Switch the language. With root, all existing widgets of that Tk interpreter are translated."""
    old, new = _state['language'], normalize_language(code)
    _state['language'] = new
    if root is not None and old != new and tk is not None:
        _retranslate(root._root(), old, new)


def _mapping(old, new):
    if old == new:
        return {}
    if new == 'en':
        return dict((de, en) for de, en in EN.items() if de != en)
    return dict((en, de) for de, en in EN.items() if de != en)


def keep_language(window):
    """Window whose texts in the code are English (the Database Editor): left out of the normal switch,
    its texts are translated with DE instead (English text -> German); see etr()."""
    window._can_keep_language = True


def etr(text):
    """Text of the Database Editor (the English original) in the current language."""
    if _state['language'] == 'de':
        return DE.get(text, text)
    return text


def _english_mapping(new):
    if new == 'de':
        return dict(DE)
    return dict((de, en) for en, de in DE.items())


def _english_widgets(widget):
    for child in widget.winfo_children():
        if getattr(child, '_can_keep_language', False):
            yield child
            for grandchild in _all_below(child):
                yield grandchild
        else:
            for grandchild in _english_widgets(child):
                yield grandchild


def _all_below(widget):
    for child in widget.winfo_children():
        yield child
        for grandchild in _all_below(child):
            yield grandchild


def _translatable_widgets(widget):
    for child in widget.winfo_children():
        if getattr(child, '_can_keep_language', False):
            continue
        yield child
        for grandchild in _translatable_widgets(child):
            yield grandchild


def _retranslate(master, old, new):
    # Formatted texts first (set_text), then everything with a fixed text.
    for name, (template, args, shown) in list(_texts.items()):
        try:
            current = master.getvar(name)
        except tk.TclError:
            current = None
        if current != shown:  # variable gone or set to something else meanwhile
            del _texts[name]
            continue
        text = _render(template, args)
        master.setvar(name, text)
        _texts[name] = (template, args, text)
    mapping = _mapping(old, new)
    for widget in [master] + list(_translatable_widgets(master)):
        try:
            _retranslate_widget(widget, mapping)
        except tk.TclError:
            pass
    english = _english_mapping(new)
    for widget in _english_widgets(master):
        try:
            _retranslate_widget(widget, english)
            if isinstance(widget, ttk.Notebook):
                for tab in widget.tabs():
                    text = widget.tab(tab, 'text')
                    if text in english:
                        widget.tab(tab, text=english[text])
        except tk.TclError:
            pass


def _retranslate_widget(widget, mapping):
    if isinstance(widget, (tk.Tk, tk.Toplevel)):
        title = widget.title()
        if title in mapping:
            widget.title(mapping[title])
    if isinstance(widget, tk.Menu):
        end = widget.index(tk.END)
        for index in range(end + 1 if end is not None else 0):
            if widget.type(index) in ('command', 'cascade', 'checkbutton', 'radiobutton'):
                label = widget.entrycget(index, 'label')
                if label in mapping:
                    widget.entryconfigure(index, label=mapping[label])
        return
    if isinstance(widget, ttk.Treeview):
        for column in ('#0',) + tuple(widget.tk.splitlist(widget.cget('columns'))):
            text = widget.heading(column, 'text')
            if text in mapping:
                widget.heading(column, text=mapping[text])
        return
    if isinstance(widget, (tk.Entry, ttk.Entry, tk.Spinbox, tk.Text)):
        return  # user input is never translated
    keys = widget.keys()
    if 'textvariable' in keys:
        name = str(widget.cget('textvariable'))
        if name:
            value = widget.getvar(name)
            if value in mapping:
                widget.setvar(name, mapping[value])
            return
    if 'text' in keys:
        text = str(widget.cget('text'))
        if text in mapping:
            widget.configure(text=mapping[text])


# ---------------------------------------------------------------- Database Editor: German texts

DE = {
    'Start bit': 'Startbit',
    'Length': 'Länge',
    'Description': 'Beschreibung',
    'Values': 'Werte',
    'Factor': 'Faktor',
    'Unit': 'Einheit',
    'Calculation': 'Berechnung',
    'Receivers': 'Empfänger',
    'Data type': 'Datentyp',
    'Byte order': 'Byte-Reihenfolge',
    'Signedness': 'Vorzeichen',
    'Type': 'Typ',
    'Initial value': 'Startwert',
    'Access': 'Zugriff',
    'Access nodes': 'Zugriffsknoten',
    'Environment variables': 'Umgebungsvariablen',
    'Bus not specified': 'Bus nicht angegeben',
    'Line %d of the values must look like "0 = Off".': 'Zeile %d der Werte muss wie "0 = Aus" aussehen.',
    'Cancel': 'Abbrechen',
    'No database loaded. Open one in the CANtrix main window.': 'Keine Datenbasis geladen. Öffne eine im Hauptfenster des CANtrix.',
    'No database loaded': 'Keine Datenbasis geladen',
    'Signals': 'Signale',
    'Save the changes to %s?': 'Änderungen an %s speichern?',
    'New': 'Neu',
    'Save': 'Speichern',
    'Load': 'Laden',
    'Load database': 'Datenbasis laden',
    'CAN database': 'CAN-Datenbasis',
    'Save as…': 'Speichern unter…',
    'Choose a file name ending in %s.': 'Wähle einen Dateinamen mit der Endung %s.',
    'Choose a different file name from the loaded databases.':
        'Wähle einen anderen Dateinamen als den der geladenen Datenbasen.',
    'Undo': 'Rückgängig',
    'Redo': 'Wiederholen',
    'Messages': 'Nachrichten',
    'Search': 'Suche',
    'Show differences only': 'Nur Unterschiede anzeigen',
    'Green: added\nRed: removed\nBlue: changed\n'
    'The file with the later modification timestamp is the newer version.':
        'Grün: hinzugefügt\nRot: entfernt\nBlau: geändert\n'
        'Die Datei mit dem jüngeren Änderungszeitstempel gilt als neuer Stand.',
    'Expand all': 'Alle aufklappen',
    'Collapse all': 'Alle zuklappen',
    'New message': 'Neue Nachricht',
    'New variable': 'Neue Variable',
    'Edit…': 'Bearbeiten…',
    'New signal': 'Neues Signal',
    'Duplicate': 'Duplizieren',
    'Delete': 'Löschen',
    'Cut': 'Ausschneiden',
    'Copy': 'Kopieren',
    'Paste': 'Einfügen',
    'Bit layout': 'Bitlayout',
    'Bus structure': 'Busstruktur',
    'Analyzing database…': 'Datenbasis wird analysiert…',
    'Could not load the database.': 'Die Datenbasis konnte nicht geladen werden.',
    'Loaded %s': '%s geladen',
    '   (channel %s)': '   (Kanal %s)',
    ' (channel %s)': ' (Kanal %s)',
    'CAN ID %s': 'CAN-ID %s',
    'Length %s bytes': 'Länge %s Bytes',
    'Receiver %s': 'Empfänger %s',
    '%d variables': '%d Variablen',
    'Environment variables are not part of a CAN message.': 'Umgebungsvariablen gehören nicht zu einer CAN-Nachricht.',
    'No bit length is available for this message.': 'Für diese Nachricht ist keine Bitlänge bekannt.',
    'unassigned': 'nicht belegt',
    '  (drag: move, Shift+drag: resize)': '  (Ziehen: verschieben, Umschalt+Ziehen: Größe ändern)',
    'The bus structure will appear after a database is loaded.': 'Die Busstruktur erscheint, sobald eine Datenbasis geladen ist.',
    'Unsaved changes: %s': 'Ungespeicherte Änderungen: %s',
    'No unsaved changes': 'Keine ungespeicherten Änderungen',
    '%d databases loaded: changes apply to the file of the selection': '%d Datenbasen geladen: Änderungen gelten für die Datei der Auswahl',
    'Read only: only DBC files can be edited': 'Schreibgeschützt: nur DBC-Dateien lassen sich bearbeiten',
    'Could not read %s: %s': '%s konnte nicht gelesen werden: %s',
    'The file could not be read.': 'Die Datei konnte nicht gelesen werden.',
    'Could not save: %s': 'Speichern fehlgeschlagen: %s',
    'Saved %s': '%s gespeichert',
    'New DBC file': 'Neue DBC-Datei',
    'All files': 'Alle Dateien',
    'Could not create the file: %s': 'Die Datei konnte nicht erstellt werden: %s',
    'Created %s': '%s erstellt',
    'Save a copy of %s': 'Kopie von %s speichern',
    'Saved a copy as %s': 'Kopie gespeichert als %s',
    'Length (bytes)': 'Länge (Bytes)',
    'Add': 'Hinzufügen',
    'Edit message %s': 'Nachricht %s bearbeiten',
    'Duplicate message %s': 'Nachricht %s duplizieren',
    'Decimal or 0x…; above 0x7FF the ID is an extended ID.': 'Dezimal oder 0x…; oberhalb von 0x7FF ist die ID eine Extended-ID.',
    'Name of the sending node; empty for none.': 'Name des sendenden Knotens; leer für keinen.',
    'Length (bits)': 'Länge (Bits)',
    'Least significant bit (Intel) or most significant bit (Motorola).': 'Niederwertigstes Bit (Intel) oder höchstwertiges Bit (Motorola).',
    'Names of the receiving nodes, separated by commas.': 'Namen der empfangenden Knoten, durch Kommas getrennt.',
    'Empty, M (multiplexer) or m<number> (e.g. m1).': 'Leer, M (Multiplexer) oder m<Zahl> (z. B. m1).',
    'One value per line, e.g. 0 = Off': 'Ein Wert pro Zeile, z. B. 0 = Aus',
    'New signal in %s': 'Neues Signal in %s',
    'Edit signal %s': 'Signal %s bearbeiten',
    'Duplicate signal %s': 'Signal %s duplizieren',
    'New environment variable': 'Neue Umgebungsvariable',
    'Edit variable %s': 'Variable %s bearbeiten',
    'Duplicate variable %s': 'Variable %s duplizieren',
    'Names of the nodes that may access the variable, separated by commas.': 'Namen der Knoten, die auf die Variable zugreifen dürfen, durch Kommas getrennt.',
    'Delete the message %s with its %d signals?': 'Die Nachricht %s mit ihren %d Signalen löschen?',
    'Delete the signal %s of the message %s?': 'Das Signal %s der Nachricht %s löschen?',
    'Delete the environment variable %s?': 'Die Umgebungsvariable %s löschen?',
    '%s messages · %s signals': '%s Nachrichten · %s Signale',
    ' · %s environment variables': ' · %s Umgebungsvariablen',
}

# ---------------------------------------------------------------- English texts

EN = {
    # CANtrix.py: menu, toolbar, status
    'Bereit': 'Ready',
    'Signalwerte': 'Signal values',
    'Hexadezimal mit 0x eingeben, z. B. 0x1F': 'Enter hexadecimal with 0x, e.g. 0x1F',
    'Leere Nachricht': 'Empty message',
    'Nachrichten hinzugefügt: %s': 'Messages added: %s',
    'Nachrichten senden': 'Send messages',
    'Nachricht hinzufügen': 'Add message',
    'Zyklisches Senden beenden': 'Stop cyclic sending',
    'ID und Daten hexadezimal. Gesendet wird nur während der Aufzeichnung (Datenquelle Live, Start). Taste: Zeichen oder Name wie F5 oder space.':
        'ID and data in hexadecimal. Messages are only sent while recording (data source Live, Start). Key: a character or a name such as F5 or space.',
    'Keine Nachrichten. „Nachricht hinzufügen“ oder Rechtsklick auf eine Zeile der Trace-Tabelle.':
        'No messages. "Add message" or right click on a row of the trace table.',
    'Zyklisch': 'Cyclic',
    'Periode': 'Period',
    'Taste': 'Key',
    'Signale…': 'Signals…',
    'Senden': 'Send',
    'Nachricht hinzugefügt: 0x%s': 'Message added: 0x%s',
    'Für diese ID gibt es keine Signale in der Datenbasis.': 'The database has no signals for this ID.',
    'Ungültige Daten (hexadezimal, Bytes)': 'Invalid data (hexadecimal bytes)',
    'Ungültige ID (hexadezimal)': 'Invalid ID (hexadecimal)',
    'Gesendet: 0x%s (%s×)': 'Sent: 0x%s (%s×)',
    'Zum Generator hinzufügen': 'Add to generator',
    'Nicht verbunden: Aufzeichnung (Datenquelle Live) starten.': 'Not connected: start the recording (data source Live).',
    'Mehr als 8 Datenbytes brauchen CAN FD (Einstellungen > CAN Trace Recorder).':
        'More than 8 data bytes need CAN FD (Settings > CAN Trace Recorder).',
    'Senden fehlgeschlagen: %s': 'Sending failed: %s',
    'Datei': 'File',
    'Neue Konfiguration': 'New configuration',
    'Konfiguration laden…': 'Load configuration…',
    'Konfiguration speichern': 'Save configuration',
    'Konfiguration speichern unter…': 'Save configuration as…',
    'Datenbasis öffnen…': 'Open database…',
    'CAN-Trace öffnen…': 'Open CAN trace…',
    'Beenden': 'Quit',
    'Ansicht': 'View',
    'Hauptfenster': 'Main window',
    'Sprache': 'Language',
    'Plugins konfigurieren…': 'Configure plugins…',
    'Einstellungen': 'Settings',
    'CAN-Kanal auswählen…': 'Select CAN channel…',
    'CAN-Kanal auswählen': 'Select CAN channel',
    'CAN-Kanal': 'CAN channel',
    'Kanal %s': 'Channel %s',
    'Alle Kanäle': 'All channels',
    ' · im Trace: %s': ' · in the trace: %s',
    'CAN-Kanal: %s': 'CAN channel: %s',
    'CAN-Kanal: alle Kanäle': 'CAN channel: all channels',
    'Der Trace Viewer zeigt nur die Frames dieses Kanals.': 'The Trace Viewer only shows the frames of this channel.',
    'Hilfe': 'Help',
    'Über CANtrix': 'About CANtrix',
    'Auf Update prüfen': 'Check for updates',
    'Es wurde keine veröffentlichte Version gefunden.': 'No published version was found.',
    'Die Prüfung ist fehlgeschlagen:\n%s': 'The check failed:\n%s',
    'Ein Update ist verfügbar: Version %s\n(installiert: %s)\n\nQuellen jetzt herunterladen?': 'An update is available: version %s\n(installed: %s)\n\nDownload the sources now?',
    'Quellen speichern unter…': 'Save sources as…',
    'Ein Update ist verfügbar: Version %s\n(installiert: %s)\n\nJetzt installieren und neu starten?': 'An update is available: version %s\n(installed: %s)\n\nInstall it now and restart?',
    'Update wird geladen …': 'Downloading update …',
    'Update fehlgeschlagen': 'Update failed',
    'Das Update ist fehlgeschlagen:\n%s': 'The update failed:\n%s',
    'Das Update wurde installiert. Das Programm startet neu.': 'The update was installed. The program will restart.',
    'Der Download ist fehlgeschlagen:\n%s': 'The download failed:\n%s',
    'Die Quellen wurden gespeichert:\n%s': 'The sources were saved:\n%s',
    'Das Programm ist auf dem neuesten Stand (Version %s).': 'The program is up to date (version %s).',
    'Status & Zusammenfassung': 'Status & summary',
    'Neue Konfiguration (%s+N)': 'New configuration (%s+N)',
    'Konfiguration laden… (%s+L)': 'Load configuration… (%s+L)',
    'Konfiguration speichern (%s+S)': 'Save configuration (%s+S)',
    'Datenbasis öffnen… (%s+D)': 'Open database… (%s+D)',
    'CAN-Trace öffnen… (%s+O)': 'Open CAN trace… (%s+O)',
    'Konfiguration': 'Configuration',
    'Datenbasis': 'Database',
    'Nachrichten': 'Messages',
    'Trace-Status': 'Trace status',
    'Trace-Ereignis': 'Trace event',
    'Hinweis': 'Note',
    'Keine': 'None',
    'Alle': 'All',
    'CAN Trace Viewer – Nachrichtenauswahl': 'CAN Trace Viewer – Message selection',
    'Nachrichtenauswahl': 'Message selection',
    'Nachrichten für die Trace-Tabelle auswählen': 'Select messages for the trace table',
    '%s von %s Nachrichten angezeigt': '%s of %s messages shown',
    'Wird geladen: %s': 'Loading: %s',
    'Nicht geladen': 'Not loaded',
    ' · bekommt den Trace': ' · receives the trace',
    ' · Skript fehlt': ' · script missing',
    '%s Nachrichten · %s Signale': '%s messages · %s signals',
    '%s Frames mit Datenbasis-Nachricht': '%s frames with a database message',
    '%s Status-/Ereigniszeilen': '%s status/event lines',
    'CANtrix-Konfiguration': 'CANtrix configuration',
    'Alle Dateien': 'All files',
    'Datenbasis öffnen': 'Open database',
    'CAN-Datenbasis': 'CAN database',
    'CAN-Trace öffnen': 'Open CAN trace',
    'CAN-Trace': 'CAN trace',
    'Lade %s…': 'Loading %s…',
    'Laden fehlgeschlagen: %s': 'Loading failed: %s',
    'Geladen: %s': 'Loaded: %s',
    'Der Trace Recorder benötigt python-can (%s).\n\nInstallation: pip3 install --user python-can':
        'The Trace Recorder needs python-can (%s).\n\nInstallation: pip3 install --user python-can',
    'Speichern fehlgeschlagen:\n%s': 'Saving failed:\n%s',
    'Konfiguration gespeichert: %s': 'Configuration saved: %s',
    'Konfiguration nicht gefunden:\n%s': 'Configuration not found:\n%s',
    'Plugins geändert – mit „Konfiguration speichern unter…“ sichern':
        'Plugins changed – keep them with “Save configuration as…”',
    'Die Datei ist keine CANtrix-Konfiguration.': 'The file is not a CANtrix configuration.',
    'Plugins konnten nicht in der Konfiguration gespeichert werden:\n%s':
        'The plugins could not be saved in the configuration:\n%s',
    'Plugins gespeichert in %s': 'Plugins saved in %s',
    'Plugin-Skript nicht gefunden:\n%s': 'Plugin script not found:\n%s',
    'Das Plugin "%s" braucht einen Trace.\nBitte zuerst einen CAN-Trace öffnen.':
        'The plugin "%s" needs a trace.\nPlease open a CAN trace first.',
    'Die Argumente des Plugins sind ungültig (%s).': 'The arguments of the plugin are invalid (%s).',
    'Plugin gestartet: %s': 'Plugin started: %s',
    'Konfiguration laden': 'Load configuration',
    'Laden fehlgeschlagen:\n%s': 'Loading failed:\n%s',
    'Konfiguration geladen: %s': 'Configuration loaded: %s',
    'Nicht gefunden:\n%s': 'Not found:\n%s',
    'Fehler: tkinter ist in dieser Python-Installation nicht verfügbar.':
        'Error: tkinter is not available in this Python installation.',
    'CAN-Datenbasen und CAN-Traces analysieren.': 'Analyse CAN databases and CAN traces.',
    'Konfiguration (.canconfig), die beim Start geladen wird': 'Configuration (.canconfig) to load on start',
    'CAN-Datenbasis (DBC, ARXML, HTML)': 'CAN database (DBC, ARXML, HTML)',
    'CAN-Trace (ASC, TRC, LOG, TXT)': 'CAN trace (ASC, TRC, LOG, TXT)',

    # CAN_Plugins.py
    'Startet…': 'Starting…',
    'Abbrechen': 'Cancel',
    'Ausgabe': 'Output',
    '(Ordner: %s)': '(folder: %s)',
    'Start fehlgeschlagen: %s': 'Start failed: %s',
    'Start fehlgeschlagen': 'Start failed',
    'Läuft…': 'Running…',
    'Beendet mit Code %d': 'Finished with code %d',
    'Fertig': 'Done',
    'Beendet mit Fehler (Code %d)': 'Finished with error (code %d)',
    'Wird abgebrochen…': 'Cancelling…',
    'Das Plugin läuft noch. Abbrechen und Fenster schließen?':
        'The plugin is still running. Cancel it and close the window?',
    'Plugins sind Python-Skripte im Ordner %s': 'Plugins are Python scripts in the folder %s',
    'Neues Plugin': 'New plugin',
    'Plugin bearbeiten': 'Edit plugin',
    'Python-Skripte': 'Python scripts',
    'Konfigurierte Plugins': 'Configured plugins',
    'Skript': 'Script',
    'Argumente': 'Arguments',
    'Auswählen…': 'Choose…',
    'Optional, z. B. --serve. Mit {trace} wird der Trace an dieser Stelle eingesetzt, sonst am Ende.':
        'Optional, e.g. --serve. {trace} is replaced by the trace, otherwise it is added at the end.',
    'Trace übergeben': 'Pass the trace',
    'Live-Modus': 'Live mode',
    'Ohne Trace': 'Without trace',
    'Modus': 'Mode',
    ' · Live-Modus': ' · live mode',
    'Im Live-Modus erhält das Plugin die Botschaften der Aufzeichnung und das Argument --live.':
        'In live mode the plugin receives the messages of the recording and the argument --live.',
    'Wartet auf Aufzeichnung…': 'Waiting for a recording…',
    'Live: empfängt Botschaften…': 'Live: receiving messages…',
    'Aufzeichnung beendet, Plugin schließt ab…': 'Recording ended, the plugin is finishing…',
    'Hinweis: Die ersten %d Botschaften der Aufzeichnung sind nicht mehr vorhanden.':
        'Note: the first %d messages of the recording are no longer available.',
    'Neu': 'New',
    'Entfernen': 'Remove',
    'Schließen': 'Close',
    'Hinzufügen': 'Add',
    'Übernehmen': 'Apply',
    'Plugin-Skripte auswählen': 'Choose plugin scripts',
    'Python-Skript': 'Python script',
    'Hinzugefügt: %s – Name, Argumente und Modus lassen sich hier ändern (Übernehmen).':
        'Added: %s – name, arguments and the mode can be changed here (Apply).',
    'Nicht hinzugefügt (nur .py-Dateien):\n%s': 'Not added (only .py files):\n%s',
    'Bitte ein Skript auswählen.': 'Please choose a script.',
    'Plugins müssen Python-Skripte (.py) sein.': 'Plugins must be Python scripts (.py).',
    'Skript nicht gefunden:\n%s': 'Script not found:\n%s',
    'Die Argumente sind ungültig (%s).': 'The arguments are invalid (%s).',
    'Plugin hinzugefügt: %s – Name, Argumente und Modus lassen sich jetzt noch ändern (Übernehmen).':
        'Plugin added: %s – name, arguments and the mode can still be changed now (Apply).',
    'Plugin geändert: %s': 'Plugin changed: %s',
    'Plugin entfernt: %s': 'Plugin removed: %s',

    # CAN_Info.py
    '%s konnte nicht gelesen werden:\n%s': '%s could not be read:\n%s',
    'Fehler beim Lesen von %s': 'Error reading %s',
    'Inhalt': 'Contents',
    'Speichern unter…': 'Save as…',
    'Trace speichern unter': 'Save trace as',
    'In Zwischenablage kopieren': 'Copy to clipboard',
    'Trace wird kopiert…': 'Copying trace…',
    'Trace in die Zwischenablage kopiert (%s Frames).': 'Trace copied to clipboard (%s frames).',
    'Kopieren fehlgeschlagen: %s': 'Copy failed: %s',
    'Wird gespeichert: %s': 'Saving: %s',
    'Trace gespeichert (%s, %s Frames): %s': 'Trace saved (%s, %s frames): %s',
    'Speichern fehlgeschlagen: %s': 'Saving failed: %s',
    'Unbekanntes Dateiformat: %s': 'Unknown file format: %s',
    'Kein Trace geladen.': 'No trace loaded.',

    # CAN_Databases.py (several databases)
    'Datenbasen': 'Databases',
    'Datenbasen…': 'Databases…',
    'Datenbasen… (%s+D)': 'Databases… (%s+D)',
    'Datenbasen: DBC, ARXML oder HTML-Message-Catalogue': 'Databases: DBC, ARXML or HTML message catalogue',
    'CAN-Datenbasen der Konfiguration': 'CAN databases of the configuration',
    'Geladene Datenbasen': 'Loaded databases',
    'Neue Datenbasis': 'New database',
    'Datenbasis bearbeiten': 'Edit database',
    'Pfad': 'Path',
    'Datei fehlt': 'File missing',
    'Bitte eine Datei auswählen.': 'Please choose a file.',
    'Datei nicht gefunden:\n%s': 'File not found:\n%s',
    'Datenbasen auswählen': 'Choose databases',
    'Datenbasen müssen DBC-, ARXML- oder HTML-Dateien sein:\n%s': 'Databases must be DBC, ARXML or HTML files:\n%s',
    'Diese Datenbasis ist für diesen Kanal schon vorhanden:\n%s':
        'This database is already there for this channel:\n%s',
    'Hinzugefügt: %s – der CAN-Kanal lässt sich hier ändern (Übernehmen).':
        'Added: %s – the CAN channel can be changed here (Apply).',
    'Mit CAN-Kanal gilt die Datenbasis nur für die Frames dieses Kanals und hat dort Vorrang; mit "Alle Kanäle" '
    'für alle Frames.':
        'With a CAN channel the database applies only to the frames of this channel and takes precedence there; '
        'with "All channels" to all frames.',
    'Datenbasis hinzugefügt: %s': 'Database added: %s',
    'Datenbasis geändert: %s': 'Database changed: %s',
    'Datenbasis entfernt: %s': 'Database removed: %s',
    ' · Kanal %s': ' · channel %s',
    ' · nicht geladen': ' · not loaded',
    'nicht geladen': 'not loaded',
    'wird geladen…': 'loading…',

    # CAN_Traces.py (several traces combined into one)
    'CAN-Traces': 'CAN traces',
    'CAN-Traces…': 'CAN traces…',
    'CAN-Traces… (%s+O)': 'CAN traces… (%s+O)',
    'CAN-Traces der Konfiguration': 'CAN traces of the configuration',
    'Geladene Traces': 'Loaded traces',
    'Neuer Trace': 'New trace',
    'Trace bearbeiten': 'Edit trace',
    'CAN-Traces auswählen': 'Choose CAN traces',
    'Dieser Trace ist schon vorhanden:\n%s': 'This trace is already there:\n%s',
    'Die Traces werden über ihre Startzeit zu einem Trace zusammengeführt.':
        'The traces are combined into one trace by their start times.',
    'Traces mit Startzeit werden über die absolute Zeit ausgerichtet; ein Trace ohne Startzeit schließt an den '
    'vorherigen in der Liste an.':
        'Traces with a start time are aligned by the absolute time; a trace without a start time follows the '
        'previous one in the list.',
    'Hinzugefügt: %s': 'Added: %s',
    'Trace hinzugefügt: %s': 'Trace added: %s',
    'Trace geändert: %s': 'Trace changed: %s',
    'Trace entfernt: %s': 'Trace removed: %s',
    ' · ab %s s': ' · from %s s',
    'Zusammengeführt: %s Traces, Start %s': 'Combined: %s traces, start %s',
    'Der zusammengeführte Trace konnte nicht geschrieben werden:\n%s': 'The combined trace could not be written:\n%s',
    'CAN-Trace (ASC, BLF, MF4, TRC, LOG, CSV, PCAP, VSB …); mehrfach angeben für mehrere Traces, die zu einem '
    'zusammengeführt werden':
        'CAN trace (ASC, BLF, MF4, TRC, LOG, CSV, PCAP, VSB …); give it several times for several traces, which are '
        'combined into one',
    'CAN-Datenbasis (DBC, ARXML, HTML); mehrfach angeben für mehrere Datenbasen':
        'CAN database (DBC, ARXML, HTML); give it several times for several databases',
    'Uhrzeit': 'Time of day',
    'Beschreibung': 'Description',
    'CAN-Fehler': 'CAN errors',
    'Löschen': 'Clear',
    'Der gewählte Bereich enthält keine Frames.': 'The selected range contains no frames.',
    'Online (Live) – Klick: Offline (Trace)': 'Online (Live) – click: Offline (Trace)',
    'Offline (Trace) – Klick: Online (Live)': 'Offline (Trace) – click: Online (Live)',
    'Online': 'Online',
    'Offline': 'Offline',
    'TRACE32': 'TRACE32',
    'TRACE32-Variablen': 'TRACE32 variables',
    'TRACE32 Host': 'TRACE32 host',
    'TRACE32-Paket fehlt: lauterbach-trace32-rcl.': 'TRACE32 package is missing: lauterbach-trace32-rcl.',
    'TRACE32 Verbindung fehlgeschlagen: %s': 'TRACE32 connection failed: %s',
    'TRACE32 verbindet …': 'Connecting to TRACE32…',
    'TRACE32 verbunden (%s:%s), %d Variablen': 'TRACE32 connected (%s:%s), %d variables',
    'TRACE32 getrennt': 'TRACE32 disconnected',
    'Watch-Datei öffnen…': 'Open watch file…',
    'TRACE32-Datenbasis konnte nicht erstellt werden: %s': 'Could not create the TRACE32 database: %s',
    'Der gewählte Bereich enthält keine Daten.': 'The selected range contains no data.',
    'Umgebungsvariablen ohne CAN-Frames können nur als ASC gespeichert werden.':
        'Environment variables without CAN frames can only be saved as ASC.',
    'Keine VAR.ADDWATCH-Einträge gefunden.': 'No VAR.ADDWATCH entries found.',
    'Ungültige Symbolgröße: %s': 'Invalid symbol size: %s',
    'Skalar: %s | Array/Struktur: %s': 'Scalar: %s | Array/structure: %s',
    'Port': 'Port',
    'Intervall (ms)': 'Interval (ms)',
    'Variablen (eine pro Zeile oder mit Komma getrennt)': 'Variables (one per line or comma separated)',
    'Port und Intervall (mindestens 10 ms) müssen Zahlen sein.': 'Port and interval (at least 10 ms) must be numbers.',
    '%s CAN-Fehler (Error Frames)': '%s CAN errors (error frames)',
    'Digitale und analoge Signale': 'Digital and analog signals',
    'Umgebungsvariablen': 'Environment variables',
    'Zurücksetzen': 'Reset',
    'Der Trace enthält keine Startzeit, die absolute Zeit ist unbekannt.':
        'The trace contains no start time, the absolute time is unknown.',
    'Startzeit: %.3f s': 'Start time: %.3f s',
    'Endzeit: %.3f s': 'End time: %.3f s',

    # CAN_TraceViewer.py
    '%s s · %s := %s (Umgebungsvariable)': '%s s · %s := %s (environment variable)',
    'Keine CAN-Datenzeilen in "%s" erkannt. Unterstützt werden Vector ASC und BLF, ASAM MDF4, PCAN-View TRC, '
    'BUSMASTER, candump, SLCAN, CSV und PCAP/PCAPNG.':
        'No CAN data lines found in "%s". Supported are Vector ASC and BLF, ASAM MDF4, PCAN-View TRC, BUSMASTER, '
        'candump, SLCAN, CSV and PCAP/PCAPNG.',
    'Keine CAN-Frames in "%s" (%s) gefunden.': 'No CAN frames found in "%s" (%s).',
    'Die %s-Datei "%s" ist beschädigt (%s).': 'The %s file "%s" is damaged (%s).',

    # CAN_TraceFormats.py
    'Keine MDF-Datei.': 'Not an MDF file.',
    'Nur MDF 4 (.mf4) wird unterstützt, nicht MDF 3.': 'Only MDF 4 (.mf4) is supported, not MDF 3.',
    'Die MDF-Datei ist beschädigt (ungültiger Verweis auf Position %s).':
        'The MDF file is damaged (invalid link to position %s).',
    'Die PCAP-Datei enthält keine SocketCAN-Frames (Link-Typ %s).':
        'The PCAP file contains no SocketCAN frames (link type %s).',
    'Nicht unterstützte VSB-Version: %s.': 'Unsupported VSB version: %s.',
    'Keine python-can-Datenbank (%s).': 'Not a python-can database (%s).',
    'Die Trace-Datei ist größer als 200 MB.': 'The trace file is larger than 200 MB.',
    '%s Frames': '%s frames',
    '%s Werte': '%s values',
    '%s Variablen': '%s variables',
    '%s Nachrichten': '%s messages',
    '%s Frames · Kanal %s': '%s frames · channel %s',
    '%s Nachrichten · Kanal %s': '%s messages · channel %s',
    'Lade Dateien…': 'Loading files…',
    'Vollbild': 'Full screen',
    'Geteilte Ansicht': 'Split view',
    'Feste Position': 'Fixed position',
    'Doppelklick auf Nachricht: Signale · Doppelklick auf Signal oder Klick in Spalte „Grafik“: Grafik an/aus':
        'Double-click a message: signals · Double-click a signal or click in the “Graph” column: graph on/off',
    'Zeit [s]': 'Time [s]',
    'Kanal': 'Channel',
    'Nachricht': 'Message',
    'Daten': 'Data',
    'Anzahl': 'Count',
    'Grafik': 'Graph',
    'Einheit': 'Unit',
    'Signalauswahl': 'Signal selection',
    'CAN Graph Viewer – Signalauswahl': 'CAN Graph Viewer – Signal selection',
    'Signale für die Grafik auswählen': 'Select signals for the graph',
    'Signale': 'Signals',
    '%s von %s Signalen in der Grafik': '%s of %s signals in the graph',
    'Fenster': 'Window',
    'Klick: Cursor (mit 2. Cursor: der nähere) · Rechtsklick: 2. Cursor · Mausrad und Ziehen: Zoom in der '
    'gewählten Achse · Shift+Mausrad: andere Achse':
        'Click: cursor (with 2nd cursor: the nearer one) · Right click: 2nd cursor · Mouse wheel and drag: zoom '
        'the selected axis · Shift+mouse wheel: other axis',
    '2. Cursor': '2nd cursor',
    'Bitte warten, es wird noch geladen…': 'Please wait, still loading…',
    '%s Datenbank-Nachrichten geladen': '%s database messages loaded',
    'Dateien geladen': 'Files loaded',
    '%s Trace-Frames analysiert': '%s trace frames analysed',
    'Keine passenden Trace-Nachrichten.': 'No matching trace messages.',
    'Keine passenden Datenbasis-Signale im Trace gefunden.': 'No matching database signals found in the trace.',
    'Lade Trace und Datenbasis.': 'Load a trace and a database.',
    'Keine Treffer.': 'No matches.',
    'Über „Signalauswahl“ Signale auswählen, um den Verlauf anzuzeigen':
        'Select signals with “Signal selection” to show their values',
    'Trace und Datenbasis laden': 'Load a trace and a database',
    'CAN-Traces mit einer CAN-Datenbasis analysieren.': 'Analyse CAN traces with a CAN database.',
    'Trace-Datei (ASC, TRC, LOG, TXT), die beim Start geladen wird': 'Trace file (ASC, TRC, LOG, TXT) to load on start',
    'CAN-Datenbasis (DBC, ARXML, HTML), die beim Start geladen wird':
        'CAN database (DBC, ARXML, HTML) to load on start',

    # CAN_TraceRecorder.py
    'Typ': 'Type',
    'Datenquelle': 'Data source',
    'Panel Größe immer optimal': 'Always fit panel size',
    'Gedockte Ansicht': 'Docked view',
    'Verbindung fehlgeschlagen: %s': 'Connection failed: %s',
    'Gerät %s, ': 'device %s, ',
    'CAN FD (%s / %s Bit/s)': 'CAN FD (%s / %s bit/s)',
    'CAN %s Bit/s': 'CAN %s bit/s',
    'Verbunden: %sKanal %s, %s': 'Connected: %schannel %s, %s',
    'Empfang fehlgeschlagen: %s': 'Receiving failed: %s',
    'Getrennt': 'Disconnected',
    'Nicht verbunden': 'Not connected',
    'Verbindung': 'Connection',
    'Keine Sigrok-Datei (Eintrag "metadata" fehlt).': 'Not a Sigrok file (entry "metadata" is missing).',
    'Die Sigrok-Datei enthält keine Abtastrate.': 'The Sigrok file has no sample rate.',
    'Die Sigrok-Datei enthält keine Messdaten.': 'The Sigrok file contains no samples.',
    'Stuff-Fehler': 'Stuff error',
    'Stuff-Fehler (festes Stuff-Bit)': 'Stuff error (fixed stuff bit)',
    'Start of Frame': 'Start of frame',
    'Identifier: %d (0x%X)': 'Identifier: %d (0x%X)',
    'IDE: 0 (Standard-ID)': 'IDE: 0 (standard ID)',
    'IDE: 1 (Extended-ID)': 'IDE: 1 (extended ID)',
    'Full ID (29 Bit): %d (0x%X)': 'Full ID (29 bit): %d (0x%X)',
    'BRS: 1 (Datenphase schneller)': 'BRS: 1 (faster data phase)',
    'DLC: %d (%d Datenbytes)': 'DLC: %d (%d data bytes)',
    'Datenbyte %d: 0x%02X': 'Data byte %d: 0x%02X',
    'Stuff-Zähler: %d (Parität %d)': 'Stuff count: %d (parity %d)',
    'CRC-%d: 0x%X': 'CRC-%d: 0x%X',
    'CRC-15: 0x%X': 'CRC-15: 0x%X',
    'CRC-Delimiter': 'CRC delimiter',
    'Form-Fehler: CRC-Delimiter': 'Form error: CRC delimiter',
    'ACK-Slot: dominant (bestätigt)': 'ACK slot: dominant (acknowledged)',
    'ACK-Slot: rezessiv (nicht bestätigt)': 'ACK slot: recessive (not acknowledged)',
    'ACK-Delimiter': 'ACK delimiter',
    'Form-Fehler: ACK-Delimiter': 'Form error: ACK delimiter',
    'Form-Fehler: EOF': 'Form error: EOF',
    'End of Frame': 'End of frame',
    'Fehler': 'Error',
    'Die Abtastrate (%s MHz) ist für diese Bitrate zu niedrig (mindestens %d Abtastwerte je Bit).': 'The sample rate (%s MHz) is too low for this bit rate (at least %d samples per bit).',
    'Stuff-Bit': 'Stuff bit',
    'Bit': 'Bit',
    'Datenphase': 'data phase',
    'Signale aufzeichnen und anzeigen': 'Record and show signals',
    'Das Textfeld neben einem Kanal ist seine Bezeichnung.': 'The text field next to a channel is its label.',
    'Kanäle…': 'Channels…',
    'CAN-Kanal:': 'CAN channel:',
    'Kanalauswahl': 'Channel selection',
    'Analog': 'Analog',
    'Digital': 'Digital',
    'Analog: höchstens 400 / 240 / 160 kHz bei 1 / 2 / 3 Kanälen.': 'Analog: at most 400 / 240 / 160 kHz with 1 / 2 / 3 channels.',
    'Es ist kein Kanal gewählt.': 'No channel is selected.',
    'Mit %d analogen Kanälen sind höchstens %d kHz möglich.': 'With %d analog channels at most %d kHz are possible.',
    'Die Firmware dieses sigrok-pico hat nur %d digitale und %d analoge Kanäle.': 'The firmware of this sigrok-pico has only %d digital and %d analog channels.',
    '%s: %s Abtastwerte, %s MHz, %s': '%s: %s samples, %s MHz, %s',
    'Anschluss:': 'Port:',
    'Für den sigrok-pico fehlt pyserial (Installation: pip3 install --user pyserial).': 'pyserial is missing for the sigrok-pico (install: pip3 install --user pyserial).',
    'Der sigrok-pico hat das Kommando "%s" nicht angenommen.': 'The sigrok-pico did not accept the command "%s".',
    'Auf diesem Anschluss antwortet kein sigrok-pico.': 'No sigrok-pico answers on this port.',
    'Kein Raspberry Pi Pico gefunden.': 'No Raspberry Pi Pico found.',
    'Der sigrok-pico tastet mit 5 kHz bis 120 MHz ab.': 'The sigrok-pico samples at 5 kHz to 120 MHz.',
    'Keine Abtastwerte empfangen (Aufzeichnung zu kurz oder Abtastrate zu hoch).': 'No samples received (recording too short or sample rate too high).',
    'Überlauf: Der sigrok-pico konnte die Daten nicht schnell genug senden. Die Aufzeichnung endet an dieser Stelle; mit einer niedrigeren Abtastrate dauert sie länger.': 'Overflow: the sigrok-pico could not send the data fast enough. The recording ends at this point; with a lower sample rate it lasts longer.',
    'Bei der Übertragung sind Daten verloren gegangen.': 'Data was lost during the transfer.',
    'Die Aufzeichnung wurde gekürzt (zu viele Daten).': 'The recording was truncated (too much data).',
    'Aufzeichnung': 'Recording',
    'Aufzeichnung öffnen… (Sigrok-Datei .sr)': 'Open recording… (Sigrok file .sr)',
    'Aufzeichnung speichern unter…': 'Save recording as…',
    'Aufzeichnung starten': 'Start recording',
    'Aufzeichnung beenden': 'Stop recording',
    'Hineinzoomen': 'Zoom in',
    'Herauszoomen': 'Zoom out',
    'Alles anzeigen': 'Show all',
    'Bitrate:': 'Bit rate:',
    'Abtastrate (Aufzeichnung):': 'Sample rate (recording):',
    'Aufzeichnung öffnen': 'Open recording',
    'Sigrok-Aufzeichnung': 'Sigrok recording',
    'Gespeichert: %s': 'Saved: %s',
    'Dekodiere …': 'Decoding …',
    'Dekodiere … %s %%': 'Decoding … %s %%',
    'Bitrate und Datenrate müssen positive ganze Zahlen sein.': 'Bit rate and data rate must be positive whole numbers.',
    'Bitrate, Datenrate und Abtastrate müssen positive ganze Zahlen sein.': 'Bit rate, data rate and sample rate must be positive whole numbers.',
    '%s: %s Frames, %s Abtastwerte, %s MHz, %s': '%s: %s frames, %s samples, %s MHz, %s',
    'Aufzeichnung wird gestartet …': 'Starting recording …',
    'Aufzeichnung läuft … %s s': 'Recording … %s s',
    'Aufzeichnung wird beendet …': 'Stopping recording …',
    'Aufzeichnung fehlgeschlagen: %s': 'Recording failed: %s',
    'Die Logikaufzeichnung braucht einen USBtingo (python-can-usbtingo).': 'The logic recording needs a USBtingo (python-can-usbtingo).',
    'Keine Aufzeichnung. Mit dem roten Punkt aufzeichnen oder eine Sigrok-Datei (.sr) öffnen.': 'No recording. Record using the red dot or open a Sigrok file (.sr).',
    'USBtingo-Treiber nicht verfügbar: %s': 'USBtingo driver not available: %s',
    'Gerät %s': 'Device %s',
    'Kein USBtingo gefunden.': 'No USBtingo found.',
    'USBtingo mit Seriennummer %s nicht gefunden.\nGefunden:\n%s': 'USBtingo with serial number %s not found.\nFound:\n%s',
    'Verbunden: %sUSBtingo, %s': 'Connected: %sUSBtingo, %s',
    'Schnittstelle:': 'Interface:',
    'Kanal, Bitrate, Datenrate und (beim Vector) Seriennummer müssen ganze Zahlen sein.': 'Channel, bit rate, data rate and (for Vector) serial number must be whole numbers.',
    'Vector-Treiber nicht verfügbar: %s': 'Vector driver not available: %s',
    'Gerät %s, Kanal %s%s': 'Device %s, channel %s%s',
    'Gerät:': 'Device:',
    'Geräte suchen': 'Find devices',
    'Geräte werden gesucht …': 'Searching for devices …',
    'Kein Vector-Gerät gefunden.': 'No Vector device found.',
    'Gerät oder Kanal nicht gefunden (Seriennummer %s, Kanal %s).\nGefunden:\n%s': 'Device or channel not found (serial number %s, channel %s).\nFound:\n%s',
    'Verbunden, Zustand: %s\nGefunden:\n%s': 'Connected, state: %s\nFound:\n%s',
    'Verbindung in Ordnung, Zustand: %s\nGefunden:\n%s': 'Connection OK, state: %s\nFound:\n%s',
    'Verbindung testen': 'Test connection',
    'Serieller Port:': 'Serial port:',
    'Serielle Baudrate:': 'Serial baud rate:',
    'Die serielle Baudrate muss eine positive ganze Zahl sein.': 'The serial baud rate must be a positive integer.',
    'OBD: Rechner-Zeitstempel, mögliche Frameverluste bei hoher Buslast.':
        'OBD: host timestamps; frames may be dropped under high bus load.',
    'Bitte einen seriellen Port für OBDLink / ELM327 auswählen.': 'Select a serial port for OBDLink / ELM327.',
    'OBDLink / ELM327 benötigt pyserial.': 'OBDLink / ELM327 requires pyserial.',
    'Serieller Schreibvorgang unvollständig.': 'Incomplete serial write.',
    'Ungültige Antwort vom OBD-Adapter.': 'Invalid response from the OBD adapter.',
    'Keine Antwort vom OBD-Adapter.': 'No response from the OBD adapter.',
    'OBD-Adapter meldet bei %s: %s': 'OBD adapter reported for %s: %s',
    'ELM327 ohne USER1-Unterstützung benötigt 250 oder 500 kbit/s.':
        'An ELM327 without USER1 support requires 250 or 500 kbit/s.',
    'Die serielle OBD-Verbindung ist geschlossen.': 'The OBD serial connection is closed.',
    'OBD-Monitor beendet: %s': 'OBD monitor stopped: %s',
    'OBDLink / ELM327 unterstützt hier nur klassische CAN-Datenframes.':
        'OBDLink / ELM327 supports only Classical CAN data frames here.',
    'ELM327 kann hier keine CAN-Frames ohne Nutzdaten senden.': 'ELM327 cannot send empty CAN data frames here.',
    'Verbunden: %s · %s · Rechner-Zeitstempel': 'Connected: %s · %s · host timestamps',
    'Vector I/O Configuration': 'Vector I/O Configuration',
    'Vector-Gerätesuche benötigt Windows mit installiertem XL-Treiber.':
        'Vector device discovery requires Windows with the XL Driver installed.',
    'Pin-Konfiguration': 'Pin configuration',
    'CAN-Interface': 'CAN interface',
    'Modell': 'Model',
    'Zykluszeit (ms)': 'Cycle time (ms)',
    'CH5 · D-SUB9 · %s aktive Pins': 'CH5 · D-SUB9 · %s active pins',
    'Analogeingang': 'Analog input',
    'Digitaleingang 0': 'Digital input 0',
    'Digitaleingang 1': 'Digital input 1',
    'Digitalausgang': 'Digital output',
    '%s Vector-I/O-Geräte gefunden.': '%s Vector I/O devices found.',
    'Kein VN1640/VN1640A mit CH5 gefunden.': 'No VN1640/VN1640A with CH5 found.',
    'Die Zykluszeit muss zwischen 1 und 100 ms liegen.': 'Cycle time must be between 1 and 100 ms.',
    'Bitte ein Gerät auswählen oder seine Seriennummer eingeben.': 'Select a device or enter its serial number.',
    'Bitte eindeutige DBC-Namen eingeben (maximal 32 Zeichen).': 'Enter unique DBC names (up to 32 characters).',
    'Vector-I/O-Datenbasis konnte nicht erstellt werden: %s': 'Could not create the Vector I/O database: %s',
    'Vector-App-Name:': 'Vector application name:',
    'Vector CAN-FD Bit-Timing': 'Vector CAN FD bit timing',
    'Arbitrierung': 'Arbitration',
    'Datenphase': 'Data phase',
    'Aus Bitrate berechnen': 'Calculate from bit rates',
    'Für diese Bitraten ergibt sich kein ganzzahliges Vector-Timing.':
        'These bit rates do not yield integer Vector timing values.',
    'Bitte sechs positive ganze Bit-Timing-Werte eingeben oder alle Felder leeren.':
        'Enter six positive integer bit timing values or leave all fields empty.',
    'Bitraten müssen positiv sein. Kanal und Vector-Seriennummer müssen nichtnegative ganze Zahlen sein.':
        'Bit rates must be positive. The channel and Vector serial number must be nonnegative integers.',
    'Zum Testen geänderter Einstellungen bitte die laufende Aufzeichnung stoppen.':
        'Stop the current recording to test changed settings.',
    'Verbindung wird getestet …': 'Testing connection …',
    'Der Trace Recorder benötigt python-can (%s).': 'The Trace Recorder needs python-can (%s).',
    'Kanal:': 'Channel:',
    'Seriennummer:': 'Serial number:',
    '(leer = beliebiges Gerät)': '(empty = any device)',
    'Modus:': 'Mode:',
    'Datenrate:': 'Data rate:',
    'Eingabefehler': 'Input error',
    'Kanal, Bitrate, Datenrate und Seriennummer müssen ganze Zahlen sein.':
        'Channel, bitrate, data rate and serial number must be whole numbers.',
    'Verbinde ...': 'Connecting ...',
    '%s Frames/s | %s gesamt | %s Error-Frames': '%s frames/s | %s total | %s error frames',

    # CAN_Panel.py and the panel in CANtrix.py
    'Panel öffnen…': 'Open panel…',
    'Panel öffnen… (%s+P)': 'Open panel… (%s+P)',
    'Panel öffnen': 'Open panel',
    'Vector-Panel': 'Vector panel',
    'Vector-Panel (.xvp)': 'Vector panel (.xvp)',
    'Panel-Datei (.xvp)': 'Panel file (.xvp)',
    '%s · %s Elemente · %s verknüpft': '%s · %s elements · %s linked',
    '%s Werte von %s Umgebungsvariablen': '%s values of %s environment variables',
    '"%s" ist keine gültige Panel-Datei (%s).': '"%s" is not a valid panel file (%s).',
    'In "%s" wurde kein Panel gefunden.': 'No panel found in "%s".',
    'Das Panel "%s" enthält keine Elemente.': 'The panel "%s" has no elements.',
    'Bitte eine Panel-Datei (.xvp) wählen.': 'Please select a panel file (.xvp).',
    'Die Panel-Datei ist größer als 20 MB.': 'The panel file is larger than 20 MB.',
    'nicht in der Datenbasis': 'not in the database',
    'nicht im Trace': 'not in the trace',
    'kein Trace geladen': 'no trace loaded',
    '%s (Umgebungsvariable)': '%s (environment variable)',
    'Cursor des Trace Viewers folgen': 'Follow the Trace Viewer cursor',
    'Tempo': 'Speed',
    'Kein Panel geladen': 'No panel loaded',
    '%s Elemente · %s verknüpft · %s mit Werten im Trace': '%s elements · %s linked · %s with values in the trace',
    '%s · nicht verknüpft': '%s · not linked',
    '%s · öffnet %s': '%s · opens %s',
    'In die Menüleiste': 'Move to menu bar',
    'In den Infobereich (Systemtray)': 'Move to system tray',
    'Menüleiste verlassen': 'Leave menu bar',
    'Infobereich verlassen': 'Leave system tray',
    'Die Fenster werden neu geöffnet, dabei geht die Live-Aufzeichnung verloren. Fortfahren?':
        'The windows are opened again and the live recording will be lost. Continue?',
    '%s Elemente · %s verknüpft · %s mit Live-Werten': '%s elements · %s linked · %s with live values',
    '%s Elemente · %s verknüpft · noch keine Live-Daten – Live-Aufzeichnung starten':
        '%s elements · %s linked · no live data yet – start the live recording',
    'nicht live verfügbar': 'not available live',
    'noch keine Live-Daten': 'no live data yet',
    'Vector-Panels (.xvp) mit Werten aus einem CAN-Trace anzeigen.':
        'Show Vector panels (.xvp) with values from a CAN trace.',
    'Zeit': 'Time',
    'Nach Adresse filtern: %s': 'Filter by address: %s',
    'Nach Name filtern: %s': 'Filter by name: %s',
    'Nach Service-UUID filtern: %s': 'Filter by service UUID: %s',
    'Nach Service-UUID filtern': 'Filter by service UUID',
    'Umgebungsvariable %s': 'Environment variable %s',
    'Alle Umgebungsvariablen des Geräts anlegen': 'Create all environment variables of the device',
    'BLE-Datenbasis konnte nicht erstellt werden: %s': 'The BLE database could not be created: %s',
    'Bluetooth-Low-Energy-Advertising': 'Bluetooth Low Energy advertising',
    'Empfang': 'Reception',
    'Scan starten': 'Start scan',
    'Scan beenden': 'Stop scan',
    'Liste leeren': 'Clear list',
    'Als CSV speichern…': 'Save as CSV…',
    'Adresse': 'Address',
    'Adresse:': 'Address:',
    'Service-UUID:': 'Service UUID:',
    'Pakete': 'Packets',
    'Geräte': 'Devices',
    'TX-Power': 'TX power',
    'Hersteller': 'Manufacturer',
    'Nur Änderungen': 'Changes only',
    'Passiver Scan': 'Passive scan',
    'Laufzeit (s):': 'Duration (s):',
    'Die Laufzeit muss eine positive Zahl sein.': 'The duration must be a positive number.',
    'Scan läuft …': 'Scanning …',
    'Scan beendet': 'Scan finished',
    'Scan fehlgeschlagen: %s': 'Scan failed: %s',
    '%s Pakete · %s Geräte': '%s packets · %s devices',
    'bleak ist nicht installiert (pip3 install --user bleak).': 'bleak is not installed (pip3 install --user bleak).',
    'Erklärung': 'Explanation',
    'Erklärung:': 'Explanation:',
    'AirPlay-Ziel': 'AirPlay target',
    'AirPlay-Quelle': 'AirPlay source',
    'Instant Hotspot (Anfrage)': 'Instant Hotspot (request)',
    'Instant Hotspot (Angebot)': 'Instant Hotspot (offer)',
    'Wo ist? (Find My)': 'Find My',
    'Aktivität unbekannt': 'activity unknown',
    'Aktivitätsmeldung abgeschaltet': 'activity reporting off',
    'Ruhezustand, Bildschirm aus': 'idle, screen off',
    'Audiowiedergabe bei ausgeschaltetem Bildschirm': 'audio playing, screen off',
    'Bildschirm an, wird benutzt': 'screen on, in use',
    'Videowiedergabe': 'video playing',
    'Uhr am Handgelenk und entsperrt': 'watch on wrist and unlocked',
    'kürzlich benutzt': 'recently used',
    'Fahrer in einem Fahrzeug': 'user is driving',
    'Telefonat oder FaceTime': 'phone or FaceTime call',
    'Windows-PC': 'Windows PC',
    'Windows-Laptop': 'Windows laptop',
    'Windows-Tablet': 'Windows tablet',
    'Thermometer': 'Health thermometer',
    'Geräteinformationen': 'Device information',
    'Herzfrequenz': 'Heart rate',
    'Batterie': 'Battery',
    'Blutdruck': 'Blood pressure',
    'Eingabegerät (HID)': 'Input device (HID)',
    'Radfahren: Geschwindigkeit/Trittfrequenz': 'Cycling speed and cadence',
    'Radfahren: Leistung': 'Cycling power',
    'Umweltsensor': 'Environmental sensing',
    'Benutzerdaten': 'User data',
    'Waage': 'Weight scale',
    'Fitnessgerät': 'Fitness machine',
    'BTHome-Sensor': 'BTHome sensor',
    'Corona-Warn-App / Exposure Notification': 'Exposure Notification',
    'Tile-Tracker': 'Tile tracker',
    'Aktion 0x%X': 'action 0x%X',
    'Hauptgerät der Apple-ID': 'primary device of the Apple ID',
    'AirDrop-Empfang an': 'AirDrop receiving on',
    'WLAN an': 'Wi-Fi on',
    'WLAN aus': 'Wi-Fi off',
    'Apple Watch gesperrt': 'Apple Watch locked',
    'Entsperren mit der Watch': 'unlock with the Watch',
    'automatisches Entsperren aktiv': 'auto unlock enabled',
    'Kennung (wechselt): %s': 'tag (changes): %s',
    'Zwischenablage-Status 0x%02X': 'clipboard status 0x%02X',
    'Zähler %s': 'counter %s',
    'Inhalt verschlüsselt (%s Byte)': 'content encrypted (%s bytes)',
    '%s dBm in 1 m Abstand': '%s dBm at 1 m distance',
    'vom Besitzer getrennt, sendet seinen Schlüssel': 'separated from its owner, sends its key',
    'Besitzer in der Nähe': 'owner nearby',
    'Apple-Typ 0x%02X': 'Apple type 0x%02X',
    'unvollständig': 'incomplete',
    'Gerätetyp %s': 'device type %s',
    'Microsoft-Gerätekennung (Connected Devices)': 'Microsoft device beacon (Connected Devices)',
    'bietet Kopplung an': 'offers pairing',
    'Batterie %s mV': 'battery %s mV',
    'Temperatur %.1f °C': 'temperature %.1f °C',
    'läuft seit %s s': 'running for %s s',
    'Ladezustand %s %%': 'charge level %s %%',
    'Dienst': 'Service',
}
