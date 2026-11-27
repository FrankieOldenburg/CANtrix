#!/usr/bin/env python3
"""BLE Monitor: Bluetooth-Low-Energy-Advertising-Pakete in Echtzeit anzeigen.

Nach dem Beispiel BLE_Monitor.py (Kommandozeile), als Fenster des CANtrix. Zum Scannen wird bleak benötigt
(pip3 install --user bleak); erst beim Start eines Scans importiert. Der Scan läuft mit eigener
asyncio-Schleife in einem Thread, die Pakete gehen über eine Queue an die Oberfläche.

Unter macOS liefert CoreBluetooth statt der MAC-Adresse eine Geräte-UUID.
"""
import asyncio
import collections
import csv
import queue
import sys
import threading
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None

from CAN_Common import (C, Theme, apply_appearance, draw_save_icon, icon_hue, icon_line, initial_geometry,
                        window_geometry)
from CAN_Language import load_language, set_text, tr

MAX_PACKETS = 20000  # older packets are dropped
MAX_ROWS = 2000  # rows of the packet view
POLL_MS = 200

# Common Bluetooth SIG company IDs (excerpt)
COMPANY_IDS = {
    0x004C: 'Apple', 0x0006: 'Microsoft', 0x0075: 'Samsung', 0x00E0: 'Google', 0x0059: 'Nordic Semiconductor',
    0x000F: 'Broadcom', 0x0157: 'Huawei', 0x038F: 'Xiaomi',
}

Packet = collections.namedtuple('Packet', 'time address name rssi tx_power manufacturer uuids service_data')


def hex_text(data):
    return ' '.join('%02x' % byte for byte in bytearray(data))


def make_packet(device, adv, now=None):
    """Packet from the objects bleak passes to the detection callback."""
    return Packet(now or datetime.now(), device.address, adv.local_name or device.name or '', adv.rssi,
                  adv.tx_power,
                  tuple(sorted((cid, hex_text(data)) for cid, data in adv.manufacturer_data.items())),
                  tuple(sorted(adv.service_uuids)),
                  tuple(sorted((uuid, hex_text(data)) for uuid, data in adv.service_data.items())))


def signature(packet):
    """Payload of a packet: for 'changes only'."""
    return packet.name, packet.manufacturer, packet.uuids, packet.service_data


def matches(packet, name='', address='', uuid='', company=None, min_rssi=None):
    if min_rssi is not None and packet.rssi is not None and packet.rssi < min_rssi:
        return False
    if name and name.lower() not in packet.name.lower():
        return False
    if address and address.lower() not in packet.address.lower():
        return False
    if uuid:
        uuids = list(packet.uuids) + [item[0] for item in packet.service_data]
        if not any(uuid.lower() in item.lower() for item in uuids):
            return False
    if company is not None and company not in [item[0] for item in packet.manufacturer]:
        return False
    return True


def company_text(packet):
    return ', '.join('0x%04X %s' % (cid, COMPANY_IDS.get(cid, '')) for cid, _ in packet.manufacturer).strip()


def payload_text(packet):
    parts = [data for _, data in packet.manufacturer]
    parts.extend('%s: %s' % (uuid[4:8] if len(uuid) == 36 else uuid, data) for uuid, data in packet.service_data)
    return ' | '.join(parts)


def detail_lines(packet):
    lines = ['%s  %s' % (packet.address, packet.name or '-')]
    for cid, data in packet.manufacturer:
        lines.append('Manufacturer 0x%04X (%s): %s' % (cid, COMPANY_IDS.get(cid, '?'), data))
    lines.extend('Service UUID: %s' % uuid for uuid in packet.uuids)
    lines.extend('Service Data %s: %s' % item for item in packet.service_data)
    return lines


def write_csv(path, packets):
    """Same columns as the command line example."""
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle, delimiter=';')
        writer.writerow(['timestamp', 'address', 'name', 'rssi', 'tx_power', 'manufacturer_data',
                         'service_uuids', 'service_data'])
        for packet in packets:
            writer.writerow([
                packet.time.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3], packet.address, packet.name or '-',
                packet.rssi, packet.tx_power,
                ' | '.join('0x%04X:%s' % item for item in packet.manufacturer),
                ' | '.join(packet.uuids),
                ' | '.join('%s:%s' % item for item in packet.service_data)])


class Scanner(object):
    """BLE scan in its own thread. Puts ('packet', Packet), ('error', text) and finally ('stopped', None)."""

    def __init__(self, output, passive=False, duration=None):
        self.output = output
        self.passive = passive
        self.duration = duration
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _work(self):
        try:
            from bleak import BleakScanner
        except ImportError:
            self.output.put(('error', tr('bleak ist nicht installiert (pip3 install --user bleak).')))
            self.output.put(('stopped', None))
            return
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._scan(BleakScanner))
        except Exception as error:
            self.output.put(('error', str(error) or error.__class__.__name__))
        finally:
            loop.close()
            self.output.put(('stopped', None))

    async def _scan(self, scanner_class):
        def found(device, adv):
            if not self.stop_event.is_set():
                self.output.put(('packet', make_packet(device, adv)))

        scanner = scanner_class(detection_callback=found, scanning_mode='passive' if self.passive else 'active')
        loop = asyncio.get_event_loop()
        end = loop.time() + self.duration if self.duration else None
        await scanner.start()
        try:
            while not self.stop_event.is_set() and (end is None or loop.time() < end):
                await asyncio.sleep(0.1)
        finally:
            await scanner.stop()


# ---------------------------------------------------------------- icons

def draw_scan_icon(canvas, color):
    canvas.create_polygon(11, 8, 22, 15, 11, 22, fill=C.SUCCESS, outline=C.SUCCESS, width=1.5, tags=('icon',))


def draw_scan_stop_icon(canvas, color):
    canvas.create_rectangle(9, 9, 21, 21, fill=C.DANGER, outline=C.DANGER, width=1.5, tags=('icon',))


def draw_clear_icon(canvas, color):
    """Waste bin."""
    line = icon_line(icon_hue(1))
    canvas.create_line(8, 10, 22, 10, **line)
    canvas.create_line(12, 10, 12, 7.5, 18, 7.5, 18, 10, **line)
    canvas.create_line(10, 10, 11, 23, 19, 23, 20, 10, **line)
    canvas.create_line(15, 13, 15, 20, **line)


# ---------------------------------------------------------------- window

DEVICE_COLUMNS = (('time', 'Zeit', 100), ('address', 'Adresse', 280), ('name', 'Name', 160), ('rssi', 'RSSI', 60),
                  ('tx', 'TX-Power', 70), ('count', 'Anzahl', 70), ('company', 'Hersteller', 160),
                  ('data', 'Daten', 400))
FILTERS = (('name', 'Name:', 14), ('address', 'Adresse:', 14), ('uuid', 'Service-UUID:', 10),
           ('company', 'Company ID:', 8), ('min_rssi', 'Min. RSSI:', 6))
NUMERIC = ('time', 'rssi', 'tx', 'count')

if tk is not None:

    class BLEMonitorApp(object):
        def __init__(self, root, settings=None):
            """root: Tk, Toplevel or DockFrame. settings: window settings."""
            self.root = root
            self.settings = dict(settings or {})
            self.queue = queue.Queue()
            self.scanner = None
            self.packets = collections.deque(maxlen=MAX_PACKETS)
            self.rows = {}  # row id -> packet shown in the row
            self.devices = {}  # address -> [packet count, row id]
            self.last_signature = {}
            self.next_row = 0
            self.filter_job = None
            self.status = tk.StringVar()
            self.count = tk.StringVar()
            self.theme = Theme(root)
            saved = self.settings.get
            self.filters = dict((key, tk.StringVar(value=str(saved(key, '')))) for key, _, _ in FILTERS)
            self.changes_only = tk.BooleanVar(value=bool(saved('changes_only', False)))
            self.passive = tk.BooleanVar(value=bool(saved('passive', False)))
            self.duration = tk.StringVar(value=str(saved('duration', '')))
            self.view = tk.StringVar(value='packets' if saved('view') == 'packets' else 'devices')
            self._build_layout()
            root.title('CAN BLE Monitor')
            root.minsize(760, 360)
            root.geometry(initial_geometry(root, self.settings, '1200x600'))
            set_text(self.status, 'Bereit')
            self._update_buttons()
            for variable in self.filters.values():
                variable.trace_add('write', lambda *args: self._schedule_rebuild())
            self.poll_job = self.root.after(POLL_MS, self._poll)

        # ------------------------------------------------------------ layout

        def _build_layout(self):
            theme = self.theme
            theme.header(self.root, 'CAN BLE Monitor', tr('Bluetooth-Low-Energy-Advertising'))
            footer = theme.footer(self.root, self.status)
            theme.footer_label(footer, self.count)
            body = tk.Frame(self.root, background=C.APP_BG)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            card, bar = theme.pane(body, tr('Empfang'))
            card.pack(fill=tk.BOTH, expand=True)
            self.buttons = {}
            for key, draw, command, tip in (
                    ('start', draw_scan_icon, self.start_scan, tr('Scan starten')),
                    ('stop', draw_scan_stop_icon, self.stop_scan, tr('Scan beenden')),
                    ('clear', draw_clear_icon, self.clear, tr('Liste leeren')),
                    ('save', draw_save_icon, self.save_csv, tr('Als CSV speichern…'))):
                button = theme.icon_button(bar, draw, command, tip)
                button.pack(side=tk.LEFT, padx=(8 if key in ('start', 'clear') else 2, 2), pady=3)
                self.buttons[key] = button
            for value, label in (('packets', tr('Pakete')), ('devices', tr('Geräte'))):
                ttk.Radiobutton(bar, text=label, value=value, variable=self.view, style='Toolbutton',
                                command=self.rebuild).pack(side=tk.RIGHT, padx=2)

            options = ttk.Frame(card, padding=(12, 8, 12, 0))
            options.pack(side=tk.TOP, fill=tk.X)
            for key, label, width in FILTERS:
                ttk.Label(options, text=tr(label)).pack(side=tk.LEFT)
                ttk.Entry(options, textvariable=self.filters[key], width=width).pack(side=tk.LEFT, padx=(4, 12))
            options = ttk.Frame(card, padding=(12, 6, 12, 6))
            options.pack(side=tk.TOP, fill=tk.X)
            ttk.Checkbutton(options, text=tr('Nur Änderungen'), variable=self.changes_only,
                            command=self.rebuild).pack(side=tk.LEFT, padx=(0, 12))
            passive = ttk.Checkbutton(options, text=tr('Passiver Scan'), variable=self.passive)
            if sys.platform == 'darwin':  # CoreBluetooth has no passive scan
                self.passive.set(False)
            else:
                passive.pack(side=tk.LEFT, padx=(0, 12))
            ttk.Label(options, text=tr('Laufzeit (s):')).pack(side=tk.LEFT)
            ttk.Entry(options, textvariable=self.duration, width=7).pack(side=tk.LEFT, padx=(4, 0))

            self.detail = tk.Text(card, height=6, wrap=tk.NONE, relief=tk.FLAT, background=C.SURFACE,
                                  foreground=C.INK, highlightthickness=1, highlightbackground=C.LINE,
                                  highlightcolor=C.LINE, padx=10, pady=6, font='TkFixedFont',
                                  state=tk.DISABLED)
            self.detail.pack(side=tk.BOTTOM, fill=tk.X)
            frame = ttk.Frame(card)
            frame.pack(fill=tk.BOTH, expand=True, padx=1)
            tree = self.tree = ttk.Treeview(frame, columns=[c[0] for c in DEVICE_COLUMNS], show='headings',
                                            selectmode='browse')
            for key, title, width in DEVICE_COLUMNS:
                anchor = tk.E if key in ('rssi', 'tx', 'count') else tk.W
                tree.heading(key, text=tr(title), anchor=anchor)
                tree.column(key, width=width, minwidth=50, anchor=anchor, stretch=key == 'data')
            vertical = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
            horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
            tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            vertical.pack(side=tk.RIGHT, fill=tk.Y)
            horizontal.pack(side=tk.BOTTOM, fill=tk.X)
            tree.pack(fill=tk.BOTH, expand=True)
            tree.bind('<<TreeviewSelect>>', lambda event: self._show_detail())
            # Right click: Button-2 (macOS, Tk 8.6), Button-3 (others and Tk 9), Control+click on macOS.
            events = ['<Button-3>']
            if tree.tk.call('tk', 'windowingsystem') == 'aqua':
                events += ['<Button-2>', '<Control-Button-1>']
            for event in events:
                tree.bind(event, self._on_right_click)
            self.menu = tk.Menu(tree, tearoff=False)

        def _update_buttons(self):
            scanning = self.scanner is not None
            for key, enabled in (('start', not scanning), ('stop', scanning), ('clear', bool(self.packets)),
                                 ('save', bool(self.packets))):
                self.buttons[key].state(['!disabled' if enabled else 'disabled'])

        # ------------------------------------------------------------ scanning

        def start_scan(self):
            if self.scanner is not None:
                return
            text = self.duration.get().strip().replace(',', '.')
            try:
                duration = float(text) if text else None
                if duration is not None and not duration > 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror('CAN BLE Monitor', tr('Die Laufzeit muss eine positive Zahl sein.'),
                                     parent=self.root)
                return
            self.queue = queue.Queue()  # nothing of an earlier scan
            self.scanner = Scanner(self.queue, passive=self.passive.get(), duration=duration)
            self.error = None
            set_text(self.status, 'Scan läuft …')
            self._update_buttons()

        def stop_scan(self):
            if self.scanner is not None:
                self.scanner.stop()

        def _poll(self):
            new, error, stopped = [], None, False
            while len(new) < MAX_ROWS:  # the rest follows with the next call
                try:
                    kind, value = self.queue.get_nowait()
                except queue.Empty:
                    break
                if kind == 'packet':
                    new.append(value)
                elif kind == 'error':
                    error = value
                else:
                    stopped = True
            if new:
                follow = self.tree.yview()[1] >= 0.999
                self.packets.extend(new)
                last = None
                for packet in new:
                    last = self._add_row(packet) or last
                self._trim_rows()
                if follow and last and self.view.get() == 'packets':
                    self.tree.see(last)
                self._show_count()
                self._show_detail()
            if stopped:
                self.scanner = None
                if self.error is None and error is None:
                    set_text(self.status, 'Scan beendet')
            if error is not None:
                self.error = error
                set_text(self.status, 'Scan fehlgeschlagen: %s', error)
            if new or stopped:
                self._update_buttons()
            self.poll_job = self.root.after(POLL_MS, self._poll)
            if error is not None:
                messagebox.showerror('CAN BLE Monitor', error, parent=self.root)

        # ------------------------------------------------------------ table

        def _filter_values(self):
            """Filter arguments of matches(); incomplete numbers are ignored."""
            values = dict((key, self.filters[key].get().strip()) for key in ('name', 'address', 'uuid'))
            for key, base in (('company', 0), ('min_rssi', 10)):
                try:
                    values[key] = int(self.filters[key].get().strip(), base)
                except ValueError:
                    values[key] = None
            return values

        def _passes(self, packet, filters, signatures):
            if not matches(packet, **filters):
                return False
            if self.changes_only.get():
                current = signature(packet)
                if signatures.get(packet.address) == current:
                    return False
                signatures[packet.address] = current
            return True

        def _add_row(self, packet, filters=None):
            """Show one packet; returns the row id or None when it is filtered out."""
            filters = filters if filters is not None else self._filter_values()
            device = self.devices.setdefault(packet.address, [0, None])
            if not matches(packet, **filters):
                return None
            device[0] += 1
            grouped = self.view.get() == 'devices'
            if not self._passes(packet, filters, self.last_signature):
                if grouped and device[1] is not None:
                    self.tree.set(device[1], 'count', device[0])
                return None
            values = (packet.time.strftime('%H:%M:%S.%f')[:-3], packet.address, packet.name or '-',
                      '' if packet.rssi is None else packet.rssi,
                      '' if packet.tx_power is None else packet.tx_power, device[0], company_text(packet),
                      payload_text(packet))
            if grouped and device[1] is not None:
                row = device[1]
                self.tree.item(row, values=values)
            else:
                self.next_row += 1
                row = 'r%d' % self.next_row
                self.tree.insert('', tk.END, iid=row, values=values)
                device[1] = row
            self.rows[row] = packet
            return row

        def _trim_rows(self):
            if self.view.get() != 'packets':
                return
            extra = self.tree.get_children()[:-MAX_ROWS]
            if extra:
                self.tree.delete(*extra)
                for row in extra:
                    del self.rows[row]

        def _schedule_rebuild(self):
            if self.filter_job is not None:
                self.root.after_cancel(self.filter_job)
            self.filter_job = self.root.after(300, self.rebuild)

        def rebuild(self):
            """Fill the table again from the packets (filters or view changed)."""
            self.filter_job = None
            selected = self._selected_address()
            self.tree.delete(*self.tree.get_children())
            self.rows, self.devices, self.last_signature = {}, {}, {}
            filters = self._filter_values()
            grouped = self.view.get() == 'devices'
            # the packet view only needs the newest rows; the device view counts all packets
            packets = list(self.packets) if grouped or self.changes_only.get() else list(self.packets)[-MAX_ROWS:]
            for packet in packets:
                self._add_row(packet, filters)
            self._trim_rows()
            if grouped and selected in self.devices and self.devices[selected][1] is not None:
                self.tree.selection_set(self.devices[selected][1])
            self._show_count()
            self._show_detail()

        def _on_right_click(self, event):
            row = self.tree.identify_row(event.y)
            if row not in self.rows:
                return None
            self.tree.selection_set(row)
            self._fill_menu(self.rows[row])
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()
            return 'break'

        def _fill_menu(self, packet):
            """Menu entries to take over the address, name or a service UUID of the packet into the filter."""
            self.menu.delete(0, tk.END)

            def add(key, label, value):
                self.menu.add_command(label=label % value, command=lambda: self.filters[key].set(value))

            add('address', tr('Nach Adresse filtern: %s'), packet.address)
            if packet.name:
                add('name', tr('Nach Name filtern: %s'), packet.name)
            else:
                self.menu.add_command(label=tr('Nach Name filtern'), state=tk.DISABLED)
            uuids = sorted(set(packet.uuids) | set(item[0] for item in packet.service_data))
            for uuid in uuids:
                add('uuid', tr('Nach Service-UUID filtern: %s'), uuid)
            if not uuids:
                self.menu.add_command(label=tr('Nach Service-UUID filtern'), state=tk.DISABLED)

        def _selected_address(self):
            selection = self.tree.selection()
            return self.rows[selection[0]].address if selection and selection[0] in self.rows else None

        def _show_detail(self):
            selection = self.tree.selection()
            packet = self.rows.get(selection[0]) if selection else None
            text = '\n'.join(detail_lines(packet)) if packet is not None else ''
            if text == self.detail.get('1.0', 'end-1c'):
                return
            self.detail.configure(state=tk.NORMAL)
            self.detail.delete('1.0', tk.END)
            self.detail.insert('1.0', text)
            self.detail.configure(state=tk.DISABLED)

        def _show_count(self):
            devices = sum(1 for count, _ in self.devices.values() if count)
            set_text(self.count, '%s Pakete · %s Geräte', len(self.packets), devices)

        def clear(self):
            self.packets.clear()
            self.rebuild()
            self._update_buttons()

        def save_csv(self):
            """Write the packets that pass the filters."""
            filters, signatures = self._filter_values(), {}
            packets = [packet for packet in self.packets if self._passes(packet, filters, signatures)]
            path = filedialog.asksaveasfilename(
                parent=self.root, title=tr('Als CSV speichern…'), defaultextension='.csv',
                initialfile=datetime.now().strftime('BLE - %d-%m-%Y - %H_%M_%S.csv'),
                filetypes=[('CSV', '*.csv'), (tr('Alle Dateien'), '*')])
            if not path:
                return
            try:
                write_csv(path, packets)
            except OSError as error:
                messagebox.showerror('CAN BLE Monitor', str(error), parent=self.root)
                return
            set_text(self.status, 'Gespeichert: %s', path)

        # ------------------------------------------------------------ window

        def theme_changed(self):
            pass  # all widgets are recoloured by set_dark_mode

        def language_changed(self):
            self._show_detail()

        def window_settings(self):
            settings = dict((key, variable.get()) for key, variable in self.filters.items())
            settings.update(geometry=window_geometry(self.root), changes_only=self.changes_only.get(),
                            passive=self.passive.get(), duration=self.duration.get(), view=self.view.get())
            return settings

        def close(self):
            self.stop_scan()
            for job in (self.poll_job, self.filter_job):
                if job is not None:
                    try:
                        self.root.after_cancel(job)
                    except tk.TclError:
                        pass
            self.root.destroy()


def main():
    load_language()
    root = tk.Tk()
    apply_appearance(root)
    app = BLEMonitorApp(root)
    root.protocol('WM_DELETE_WINDOW', app.close)
    root.mainloop()


if __name__ == '__main__':
    main()
