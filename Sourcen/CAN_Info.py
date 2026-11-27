#!/usr/bin/env python3
"""Fenster "Hilfe" des CANtrixs: zeigt die README.md (auf Englisch README_en.md).

Links ein Inhaltsverzeichnis aus den Überschriften ## und ### (Klick springt hin, beim Scrollen wird
das aktuelle Kapitel markiert). Einfache Markdown-Darstellung: Überschriften (#, ##, ###), Aufzählungen (- oder *),
`Code`, **fett**, Codeblöcke (```) und Bilder in einer eigenen Zeile (![Text](Pfad), PNG/GIF relativ zum
Projektordner, verkleinert auf die Textbreite; ein Klick öffnet sie in voller Größe). Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk

from CAN_Common import C, Theme, apply_appearance, is_frozen, resource_dir
from CAN_Language import language, set_text, tr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = resource_dir()  # Projektordner (die Skripte liegen in Sourcen/), in der exe die mitgelieferten Daten
README_PATH = os.path.join(ROOT, 'README.md')
README_EN_PATH = os.path.join(ROOT, 'README_en.md')
INLINE_RE = re.compile(r'(`[^`]+`|\*\*[^*]+\*\*)')
IMAGE_RE = re.compile(r'^!\[([^\]]*)\]\(([^)\s]+)\)$')  # image on a line of its own
VERSION = '1.6'
RELEASE_URL = 'https://api.github.com/repos/FrankieOldenburg/CANtrix/releases/latest'
RELEASE_PAGE = 'https://github.com/FrankieOldenburg/CANtrix/releases'
SOURCE_URL = 'https://github.com/FrankieOldenburg/CANtrix/archive/refs/tags/%s.zip'
ABOUT_IMAGE = os.path.join(resource_dir(), 'Bilder', 'CANtrix-Icon.png')
ICON_SIZE = 192  # pixels: icon next to the first heading of the help
IMAGE_MAX_WIDTH = 760  # pixels in the Info window (screenshots are scaled down by whole factors)


def markdown_runs(text):
    """Split Markdown into (text, tags) runs for a Tk Text widget."""
    runs = []
    in_code = False
    blank = True  # collapse several empty lines into one
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('```'):
            in_code = not in_code
            continue
        if in_code:
            runs.append((line + '\n', ('codeblock',)))
            blank = False
            continue
        if not stripped:
            if not blank:
                runs.append(('\n', ()))
            blank = True
            continue
        blank = False
        image = IMAGE_RE.match(stripped)
        if image:  # (alt text, ('image', path)): load() puts the picture there
            runs.append((image.group(1), ('image', image.group(2))))
            runs.append(('\n', ()))
            continue
        heading = re.match(r'^(#{1,3})\s+(.*)$', stripped)
        if heading:
            title = heading.group(2).replace('`', '').replace('**', '')  # no inline formatting in headings
            runs.append((title + '\n', ('h%d' % len(heading.group(1)),)))
            continue
        if stripped.startswith('|') and stripped.endswith('|'):  # table: cells separated by tabs
            cells = [cell.strip() for cell in stripped.strip('|').split('|')]
            if all(re.match(r'^:?-+:?$', cell) for cell in cells):
                continue  # separator line
            header = not any(tags and 'tablerow' in tags for _, tags in runs[-2:])
            base = ('tablerow',)
            for number, cell in enumerate(cells):
                if number:
                    runs.append(('\t', base))
                for part in INLINE_RE.split(cell):
                    if not part:
                        continue
                    if part.startswith('`') and part.endswith('`') and len(part) > 1:
                        runs.append((part[1:-1], base + ('code',)))
                    else:
                        runs.append((part, base + (('bold',) if header else ())))
            runs.append(('\n', base))
            continue
        bullet = re.match(r'^[-*]\s+(.*)$', stripped)
        base = ('bullet',) if bullet else ()
        if bullet:
            runs.append(('•\t', base))
            stripped = bullet.group(1)
        for part in INLINE_RE.split(stripped):
            if not part:
                continue
            if part.startswith('`') and part.endswith('`') and len(part) > 1:
                runs.append((part[1:-1], base + ('code',)))
            elif part.startswith('**') and part.endswith('**') and len(part) > 3:
                runs.append((part[2:-2], base + ('bold',)))
            else:
                runs.append((part, base))
        runs.append(('\n', base))
    return runs


def version_tuple(text):
    """'v1.2.3' -> (1, 2, 3); Teile ohne Zahl zählen als 0."""
    parts = re.findall(r'\d+', str(text))
    return tuple(int(part) for part in parts) or (0,)


def ssl_context():
    """macOS: das Python von python.org kennt die System-Zertifikate oft nicht, dann /etc/ssl/cert.pem."""
    if os.path.exists('/etc/ssl/cert.pem'):
        return ssl.create_default_context(cafile='/etc/ssl/cert.pem')
    return None


def download_file(url, path, timeout=30):
    """Lädt `url` nach `path`; gibt den Fehlertext oder None zurück.

    Geschrieben wird erst nach path + '.part'. Läuft im Hintergrund-Thread."""
    request = urllib.request.Request(url, headers={'User-Agent': 'CANtrix'})
    part = path + '.part'
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            with open(part, 'wb') as handle:
                while True:
                    block = response.read(65536)
                    if not block:
                        break
                    handle.write(block)
        os.replace(part, path)
    except (OSError, ValueError) as error:
        try:
            os.remove(part)
        except OSError:
            pass
        return str(error)
    return None


def download_source(tag, path, timeout=30):
    """Lädt die Quellen (ZIP) der Version `tag` nach `path`; gibt den Fehlertext oder None zurück."""
    return download_file(SOURCE_URL % tag, path, timeout)


def platform_asset():
    """Dateiname des Release-Anhangs für das laufende System (None: keiner vorgesehen)."""
    if sys.platform == 'darwin':
        return 'CANtrix-macOS.zip'
    if sys.platform.startswith('win'):
        return 'CANtrix.exe'
    return None


def can_self_update():
    """True im fertigen Programm (PyInstaller), das sich selbst ersetzen kann; aus den Quellen nicht."""
    return is_frozen() and platform_asset() is not None


def app_location():
    """Pfad der laufenden Programmdatei: die .app (macOS) bzw. die exe (Windows)."""
    path = os.path.abspath(sys.executable)
    if sys.platform == 'darwin':
        marker = '.app' + os.sep
        index = path.find(marker)
        if index >= 0:
            return path[:index + 4]
    return path


def install_update(url, progress=None):
    """Lädt das Programm der neuen Version und ersetzt das laufende; gibt den Fehlertext oder None zurück.

    macOS: ZIP mit der .app, Windows: die exe. Die alte Version wird erst umbenannt, bei einem Fehler zurückgelegt.
    Läuft im Hintergrund-Thread. Nutzerdateien liegen neben dem Programm und bleiben unberührt."""
    target = app_location()
    folder = os.path.dirname(target)
    work = None
    try:
        work = tempfile.mkdtemp(prefix='.update-', dir=folder)  # im selben Ordner: Umbenennen bleibt atomar
        if sys.platform == 'darwin':
            archive = os.path.join(work, 'update.zip')
            error = download_file(url, archive, timeout=60)
            if error:
                return error
            subprocess.check_call(['ditto', '-x', '-k', archive, work])
            new = os.path.join(work, os.path.basename(target))
            if not os.path.isdir(new):
                candidates = [name for name in os.listdir(work) if name.endswith('.app')]
                if not candidates:
                    return 'Die heruntergeladene Datei enthält keine App.'
                new = os.path.join(work, candidates[0])
        else:
            new = os.path.join(work, os.path.basename(target))
            error = download_file(url, new, timeout=60)
            if error:
                return error
        old = os.path.join(work, 'old')
        os.rename(target, old)  # auch eine laufende exe lässt sich umbenennen
        try:
            os.rename(new, target)
        except OSError:
            os.rename(old, target)
            raise
        if sys.platform == 'darwin':
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
            work = None
        # Windows: die laufende alte exe lässt sich nicht löschen; `work` wird beim nächsten Start entfernt.
        else:
            work = None
    except (OSError, subprocess.CalledProcessError) as error:
        return str(error)
    finally:
        if work is not None and sys.platform == 'darwin':
            shutil.rmtree(work, ignore_errors=True)
    return None


def remove_old_updates():
    """Beim Start: Reste einer früheren Aktualisierung neben dem Programm entfernen (Windows: alte exe)."""
    if not can_self_update():
        return
    folder = os.path.dirname(app_location())
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith('.update-'):
            shutil.rmtree(os.path.join(folder, name), ignore_errors=True)


def restart_program():
    """Startet das (neue) Programm; der Aufrufer beendet danach den laufenden Prozess."""
    target = app_location()
    if sys.platform == 'darwin':
        subprocess.Popen(['open', '-n', target])
    else:
        subprocess.Popen([target])


def check_update(timeout=8):
    """Fragt die neueste Veröffentlichung auf GitHub ab: (Fehlertext oder None, Version oder None, Update vorhanden,
    Download-URL des Programms für dieses System oder None).

    Nur Prüfung, kein Download. Läuft im Hintergrund-Thread (blockiert bis zur Antwort)."""
    request = urllib.request.Request(RELEASE_URL, headers={'Accept': 'application/vnd.github+json',
                                                           'User-Agent': 'CANtrix'})
    context = ssl_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 'none', None, False, None  # keine Veröffentlichung (oder Repository nicht öffentlich)
        return 'HTTP %d' % error.code, None, False, None
    except (OSError, ValueError) as error:
        return str(error), None, False, None
    latest = str(data.get('tag_name') or data.get('name') or '').strip()
    if not latest:
        return 'none', None, False, None
    wanted = platform_asset()
    url = None
    for asset in data.get('assets') or []:
        if asset.get('name') == wanted and asset.get('browser_download_url'):
            url = asset['browser_download_url']
    return None, latest, version_tuple(latest) > version_tuple(VERSION), url


def open_file(path):
    """Open a file with the program the system uses for it."""
    try:
        if sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        elif sys.platform.startswith('win'):
            os.startfile(path)
        else:
            subprocess.Popen(['xdg-open', path])
    except OSError:
        pass


class InfoWindow(object):
    """Shows README.md (README_en.md in English) read-only."""

    def __init__(self, parent, attach_menu=None):
        window = self.window = tk.Toplevel(parent)
        apply_appearance(window)
        window.title('CANtrix – ' + tr('Hilfe'))
        window.geometry('%dx%d' % (min(1656, window.winfo_screenwidth()), min(864, window.winfo_screenheight() - 80)))
        window.minsize(500, 360)
        theme = Theme(window)
        self.status = tk.StringVar(value='')
        theme.header(window, tr('Hilfe'))
        theme.footer(window, self.status)

        split = ttk.PanedWindow(window, orient=tk.HORIZONTAL)
        split.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        # Left: table of contents (headings ## and ###); a click scrolls the text to the heading.
        contents, _ = theme.pane(split, tr('Inhalt'))
        tree_frame = ttk.Frame(contents, padding=(4, 4, 0, 4))
        tree_frame.pack(fill=tk.BOTH, expand=True)
        tree = self.tree = ttk.Treeview(tree_frame, show='tree', selectmode='browse')
        tree.column('#0', width=290, stretch=True)
        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=tree_scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.bind('<<TreeviewSelect>>', self.on_contents_select)
        self.headings = []  # (text index, tree item) in document order
        self._syncing = False
        self._sync_job = None

        # Right: card without a title row, the text starts with its own heading.
        pane = tk.Frame(split, background=C.SURFACE, highlightthickness=1, highlightbackground=C.LINE,
                        highlightcolor=C.LINE)
        split.add(contents, weight=0)
        split.add(pane, weight=1)
        frame = ttk.Frame(pane, padding=(4, 4, 0, 4))
        frame.pack(fill=tk.BOTH, expand=True)
        text = self.text = tk.Text(frame, wrap=tk.WORD, borderwidth=0, highlightthickness=0, padx=24, pady=16,
                                   background=C.SURFACE, foreground=C.INK, insertbackground=C.SURFACE,
                                   selectbackground=C.ACCENT_SELECT, font=theme.font_text, cursor='arrow',
                                   spacing1=2, spacing3=2)
        text.tag_configure('h1', font=theme.font_document, foreground=C.INK, spacing1=10, spacing3=10)
        text.tag_configure('h2', font=theme.font_section, foreground=C.INK, spacing1=18, spacing3=8)
        text.tag_configure('h3', font=theme.font_title, foreground=C.ACCENT_HOVER, spacing1=12, spacing3=4)
        text.tag_configure('bold', font=theme.font_heading)
        text.tag_configure('code', font=theme.font_mono, foreground=C.ACCENT_HOVER)
        text.tag_configure('codeblock', font=theme.font_mono, background=C.SURFACE_ALT, lmargin1=12, lmargin2=12)
        text.tag_configure('tablerow', lmargin1=8, lmargin2=8, tabs=(300, 450), spacing1=2, spacing3=2)
        text.tag_configure('bullet', lmargin1=8, lmargin2=26, tabs=(26,))
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        def on_scroll(first, last):
            scroll.set(first, last)
            self.schedule_sync()
            self.place_icon()

        text.configure(yscrollcommand=on_scroll)
        text.tag_configure('intro', rmargin=ICON_SIZE + 56)
        self.intro_end = None
        self.setup_icon()
        text.bind('<Configure>', lambda event: self.place_icon(), add='+')
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        if attach_menu is not None:
            attach_menu(window)
        self.load()

    def language_changed(self):
        self.load()

    def load(self):
        path = README_EN_PATH if language() == 'en' and os.path.isfile(README_EN_PATH) else README_PATH
        text = self.text
        headings = []
        text.configure(state=tk.NORMAL)
        text.delete('1.0', tk.END)
        self.intro_end = None
        try:
            with open(path, encoding='utf-8') as handle:
                content = handle.read()
        except OSError as error:
            text.insert(tk.END, tr('%s konnte nicht gelesen werden:\n%s') % (os.path.basename(path), error))
            set_text(self.status, 'Fehler beim Lesen von %s', path)
        else:
            self.images = []  # keep the PhotoImages alive while they are shown
            for chunk, tags in markdown_runs(content):
                if tags and tags[0] == 'image':
                    self.insert_image(tags[1], chunk)
                    continue
                if 'h2' in tags or 'h3' in tags:
                    headings.append(('h2' in tags, chunk.strip(), text.index('end-1c')))
                text.insert(tk.END, chunk, tags)
            self.status.set('')
            # The introduction (up to the first chapter) leaves room for the icon at its right.
            self.intro_end = headings[0][2] if headings else None
            if self.intro_end:
                text.tag_add('intro', '2.0', self.intro_end)
        text.configure(state=tk.DISABLED)
        self.window.after_idle(self.place_icon)
        self.fill_contents(headings)

    def setup_icon(self):
        """Program icon (CANtrix-Icon.png) at the right of the introduction, below the first heading."""
        self.icon_label = None
        try:
            image = tk.PhotoImage(master=self.text, file=ABOUT_IMAGE)
        except (tk.TclError, OSError):
            return
        factor = max(1, -(-image.width() // ICON_SIZE))
        if factor > 1:
            image = image.subsample(factor, factor)
        self.icon_image = image
        self.icon_label = tk.Label(self.text, image=image, borderwidth=0, background=C.SURFACE)

    def place_icon(self):
        """Put the icon at the right edge next to the introduction; hidden while that is scrolled out of view."""
        label = self.icon_label
        if label is None:
            return
        info = self.text.dlineinfo('2.0') if self.intro_end else None
        if info is None:
            label.place_forget()
            return
        label.place(x=self.text.winfo_width() - ICON_SIZE - 28, y=info[1] + 4)

    def insert_image(self, relative_path, alt):
        """Picture of the README (path relative to the project folder); without it the alt text."""
        path = os.path.join(ROOT, *relative_path.split('/'))
        try:
            image = tk.PhotoImage(master=self.text, file=path)
        except (tk.TclError, OSError):
            self.text.insert(tk.END, '[%s]' % alt, ('code',))
            return
        factor = -(-image.width() // IMAGE_MAX_WIDTH)  # whole factor, rounded up
        if factor > 1:
            image = image.subsample(factor, factor)
        self.images.append(image)
        tag = 'image%d' % len(self.images)
        start = self.text.index('end-1c')
        self.text.image_create(tk.END, image=image, padx=0, pady=6)
        # A click opens the picture in full size in the image viewer of the system.
        self.text.tag_add(tag, start)
        self.text.tag_bind(tag, '<Button-1>', lambda event, path=path: open_file(path))
        self.text.tag_bind(tag, '<Enter>', lambda event: self.text.configure(cursor='hand2'))
        self.text.tag_bind(tag, '<Leave>', lambda event: self.text.configure(cursor='arrow'))

    def fill_contents(self, headings):
        """headings: (is chapter, title, text index) in document order."""
        tree = self.tree
        tree.delete(*tree.get_children())
        self.headings = []
        chapter = ''
        for is_chapter, title, index in headings:
            parent = '' if is_chapter or not chapter else chapter
            item = tree.insert(parent, tk.END, text=title, open=True)
            if is_chapter:
                chapter = item
            self.headings.append((index, item))

    def on_contents_select(self, event=None):
        if self._syncing:
            return
        selection = self.tree.selection()
        for index, item in self.headings:
            if selection and item == selection[0]:
                self.text.yview(index)
                break

    def schedule_sync(self):
        if self._sync_job is None:
            self._sync_job = self.window.after(50, self.sync_contents)

    def sync_contents(self):
        """Mark the heading of the section at the top of the text."""
        self._sync_job = None
        if not self.headings or not self.text.winfo_exists():
            return
        top = self.text.index('@0,0')
        current = None  # None: still in the introduction above the first chapter
        for index, item in self.headings:
            if self.text.compare(index, '>', top):
                break
            current = item
        wanted = (current,) if current else ()
        if self.tree.selection() != wanted:
            self._syncing = True
            try:
                self.tree.selection_set(wanted)
                if current:
                    self.tree.see(current)
            finally:
                self.window.after_idle(self._end_sync)

    def _end_sync(self):
        self._syncing = False

    def show(self):
        """Bring the window to the front and re-read the README (it may have changed)."""
        self.load()
        self.window.deiconify()
        self.window.lift()


class AboutWindow(object):
    """Window 'Über CANtrix': program icon, copyright and version."""

    def __init__(self, parent):
        window = self.window = tk.Toplevel(parent)
        if window.tk.call('tk', 'windowingsystem') == 'aqua':
            # Keep the About dialog as its own macOS window instead of allowing automatic tabs.
            try:
                window.wm_attributes('-tabbingmode', 'disallowed')
            except tk.TclError:  # Tk 8.6: use the supported legacy tabbing identifier API.
                try:
                    window.tk.call('::tk::unsupported::MacWindowStyle', 'tabbingid', window,
                                   'CANtrix-about-%s' % str(window))
                except tk.TclError:
                    pass
        apply_appearance(window)
        window.title('CANtrix – ' + tr('Über CANtrix'))
        window.resizable(False, False)
        theme = Theme(window)
        body = tk.Frame(window, background=C.SURFACE, padx=48, pady=32)
        body.pack(fill=tk.BOTH, expand=True)
        try:
            image = tk.PhotoImage(master=window, file=ABOUT_IMAGE)
            if image.width() > 256:
                image = image.subsample(-(-image.width() // 256))
            label = tk.Label(body, image=image, borderwidth=0, background=C.SURFACE)
            label.image = image  # keep a reference
            label.pack(pady=(0, 16))
        except (tk.TclError, OSError):  # missing image: show the texts only
            pass
        tk.Label(body, text='CANtrix', background=C.SURFACE, foreground=C.INK,
                 font=theme.font_document).pack()
        tk.Label(body, text='\u00a9 2026 Frank Hoheisel', background=C.SURFACE, foreground=C.INK,
                 font=theme.font_text).pack(pady=(8, 0))
        tk.Label(body, text='Version ' + VERSION, background=C.SURFACE, foreground=C.INK,
                 font=theme.font_text).pack(pady=(4, 0))
        # A Toplevel created while the main window is fullscreen can inherit that state on macOS.
        # Restore a normal window and lock the content's requested size explicitly.
        try:
            window.attributes('-fullscreen', False)
            window.state('normal')
        except tk.TclError:
            pass
        window.update_idletasks()
        window.geometry('%dx%d' % (window.winfo_reqwidth(), window.winfo_reqheight()))
        window.bind('<Escape>', lambda event: window.destroy())

    def show(self):
        self.window.deiconify()
        self.window.lift()

    def language_changed(self):
        self.window.title('CANtrix – ' + tr('Über CANtrix'))
