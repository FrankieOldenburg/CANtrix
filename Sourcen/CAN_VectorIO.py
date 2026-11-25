"""Configuration of the dedicated CH5 digital and analog connector on Vector VN1640/A."""

import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk

from CAN_Common import Theme
from CAN_DbcEditor import write_generated_dbc
from CAN_Language import set_text, tr

MODELS = ('VN1640', 'VN1640A')
# CH5 is hard-wired. The settings select which of these signals the application exposes.
PINS = (
    (1, 'AIN', 'Analogeingang', 'float', '18', 'V', 'read'),
    (4, 'DIN0', 'Digitaleingang 0', 'integer', '1', '', 'read'),
    (5, 'DIN1', 'Digitaleingang 1', 'integer', '1', '', 'read'),
    (8, 'DOUT', 'Digitalausgang', 'integer', '1', '', 'read/write'),
)
DEFAULT_CYCLE = 10
DBC_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,31}')


def normalize_vector_io(value):
    if not isinstance(value, dict):
        return None
    model, serial, cycle = (value.get(key) for key in ('model', 'serial', 'cycle_ms'))
    if model not in MODELS or type(serial) is not int or serial < 1 or type(cycle) is not int or not 1 <= cycle <= 100:
        return None
    pins = value.get('pins')
    if not isinstance(pins, dict):
        return None
    enabled = {}
    for _, key, *_ in PINS:
        if key in pins:
            name = pins[key]
            if not isinstance(name, str) or not DBC_NAME.fullmatch(name) or name in enabled.values():
                return None
            enabled[key] = name
    result = {'model': model, 'serial': serial, 'cycle_ms': cycle, 'pins': enabled}
    if isinstance(value.get('database'), str) and value['database']:
        result['database'] = value['database']
    return result


def create_vector_io_database(config, directory=None):
    if not config['pins']:
        return None
    fields = {}
    for pin, key, label, kind, maximum, unit, access in PINS:
        if key in config['pins']:
            name = config['pins'][key]
            fields[name] = {'name': name, 'type': kind, 'minimum': '0', 'maximum': maximum, 'unit': unit,
                            'access': access, 'description': '%s %s / CH5 Pin %d (%s)' %
                            (config['model'], config['serial'], pin, key)}
            if kind == 'integer':
                fields[name]['values'] = {0: 'Low', 1: 'High'}
    return write_generated_dbc(fields, 'VectorIO', 'Vector-IO', directory)


def vector_io_devices(channels):
    """Find dedicated CH5 channels by driver capability and identify the physical cases."""
    devices = {}
    for channel in channels:
        if channel.hw_channel != 4 or not (int(channel.channel_bus_capabilities) & (64 << 16)):
            continue
        label = channel.name.upper()
        hardware = getattr(channel.hw_type, 'name', '')
        model = next((item for item in reversed(MODELS) if item in label), None)
        if model is None and (hardware == 'XL_HWTYPE_VN1640' or channel.hw_type == 59):
            model = 'VN1640'
        if model and channel.serial_number > 0:
            devices[channel.serial_number] = (model, channel.serial_number)
    return sorted(devices.values(), key=lambda device: (device[0], device[1]))


def detect_vector_io_devices():
    if sys.platform != 'win32':
        raise OSError(tr('Vector-Gerätesuche benötigt Windows mit installiertem XL-Treiber.'))
    from can.interfaces.vector.canlib import get_channel_configs

    return vector_io_devices(get_channel_configs())


class VectorIODialog:
    """Edit one device's I/O configuration and return it after OK."""

    def __init__(self, parent, config):
        self.result = None
        config = normalize_vector_io(config)
        window = self.window = tk.Toplevel(parent)
        window.title(tr('Vector I/O Configuration'))
        window.transient(parent)
        window.resizable(False, False)
        theme = Theme(window)
        theme.header(window, tr('Vector I/O Configuration'))

        self.model = tk.StringVar(value=config['model'] if config else 'VN1640A')
        self.serial = tk.StringVar(value=str(config['serial']) if config else '')
        self.cycle = tk.StringVar(value=str(config['cycle_ms'] if config else DEFAULT_CYCLE))
        self.summary = tk.StringVar()
        self.search_status = tk.StringVar()
        self.validation = tk.StringVar()
        self.devices = []

        notebook = ttk.Notebook(window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        overview = ttk.Frame(notebook, padding=12)
        pins_tab = ttk.Frame(notebook, padding=12)
        notebook.add(overview, text=tr('Übersicht'))
        notebook.add(pins_tab, text=tr('Pin-Konfiguration'))

        ttk.Label(overview, text=tr('CAN-Interface')).grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.device = ttk.Combobox(overview, state='readonly', width=30)
        self.device.grid(row=0, column=1, sticky=tk.EW)
        self.device.bind('<<ComboboxSelected>>', self.select_device)
        self.search_button = ttk.Button(overview, text=tr('Geräte suchen'), command=self.search)
        self.search_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(overview, textvariable=self.search_status, wraplength=410).grid(
            row=1, column=0, columnspan=3, sticky=tk.W, pady=(4, 8))

        ttk.Label(overview, text=tr('Modell')).grid(row=2, column=0, sticky=tk.W)
        ttk.Combobox(overview, state='readonly', textvariable=self.model, values=MODELS, width=14).grid(
            row=2, column=1, sticky=tk.W)
        ttk.Label(overview, text=tr('Seriennummer:')).grid(row=3, column=0, sticky=tk.W, pady=4)
        ttk.Entry(overview, textvariable=self.serial, width=17).grid(row=3, column=1, sticky=tk.W, pady=4)
        ttk.Label(overview, text=tr('Zykluszeit (ms)')).grid(row=4, column=0, sticky=tk.W)
        ttk.Spinbox(overview, from_=1, to=100, textvariable=self.cycle, width=8).grid(row=4, column=1, sticky=tk.W)
        ttk.Label(overview, textvariable=self.summary).grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=(12, 0))
        overview.columnconfigure(1, weight=1)

        for column, label in enumerate((tr('Aktiv'), 'Pin', tr('Funktion'), tr('Umgebungsvariable'))):
            ttk.Label(pins_tab, text=label).grid(row=0, column=column, sticky=tk.W, padx=(0, 10), pady=(0, 4))
        self.pins = {}
        for row, (pin, key, label, *_) in enumerate(PINS, 1):
            selected = config['pins'] if config else None
            enabled = tk.BooleanVar(value=selected is None or key in selected)
            name = tk.StringVar(value=(selected or {}).get(key, 'IO_VN1640_' + key))
            entry = ttk.Entry(pins_tab, textvariable=name, width=22)
            self.pins[key] = (enabled, name)
            ttk.Checkbutton(pins_tab, variable=enabled,
                            command=lambda flag=enabled, field=entry: self.update_pin(flag, field)).grid(
                                row=row, column=0, sticky=tk.W)
            ttk.Label(pins_tab, text=str(pin)).grid(row=row, column=1, sticky=tk.W, padx=(0, 10), pady=4)
            ttk.Label(pins_tab, text=tr(label)).grid(row=row, column=2, sticky=tk.W, padx=(0, 10))
            entry.grid(row=row, column=3, sticky=tk.EW)
            self.update_pin(enabled, entry)

        actions = ttk.Frame(window, padding=(12, 0, 12, 12))
        actions.pack(fill=tk.X)
        ttk.Label(actions, textvariable=self.validation, wraplength=400).pack(anchor=tk.W)
        ttk.Button(actions, text=tr('Abbrechen'), command=window.destroy).pack(side=tk.RIGHT)
        ttk.Button(actions, text='OK', style='Accent.TButton', command=self.accept).pack(side=tk.RIGHT, padx=(0, 8))
        window.bind('<Escape>', lambda event: window.destroy())
        window.bind('<Return>', lambda event: self.accept())
        if sys.platform == 'win32':
            self.search()
        else:
            set_text(self.search_status, 'Vector-Gerätesuche benötigt Windows mit installiertem XL-Treiber.')
        window.grab_set()
        window.wait_window()

    def update_pin(self, enabled, entry):
        entry.state(['!disabled'] if enabled.get() else ['disabled'])
        count = sum(flag.get() for flag, _ in self.pins.values())
        set_text(self.summary, 'CH5 · D-SUB9 · %s aktive Pins', count)

    def select_device(self, event):
        index = self.device.current()
        if index >= 0:
            model, serial = self.devices[index]
            self.model.set(model)
            self.serial.set(str(serial))

    def search(self):
        self.search_button.state(['disabled'])
        set_text(self.search_status, 'Geräte werden gesucht …')
        results = queue.Queue()

        def work():
            try:
                results.put((True, detect_vector_io_devices()))
            except Exception as error:
                results.put((False, str(error)))

        def poll():
            try:
                ok, devices = results.get_nowait()
            except queue.Empty:
                self.window.after(100, poll)
                return
            self.search_button.state(['!disabled'])
            if not ok:
                set_text(self.search_status, '%s', devices)
                return
            self.devices = devices
            self.device.configure(values=['%s · %s · CH5' % device for device in devices])
            for index, (_, serial) in enumerate(devices):
                if str(serial) == self.serial.get():
                    self.device.current(index)
                    self.select_device(None)
                    break
            else:
                self.device.set('')
                if len(devices) == 1 and not self.serial.get():
                    self.device.current(0)
                    self.select_device(None)
            if devices:
                set_text(self.search_status, '%s Vector-I/O-Geräte gefunden.', len(devices))
            else:
                set_text(self.search_status, 'Kein VN1640/VN1640A mit CH5 gefunden.')

        threading.Thread(target=work, daemon=True).start()
        self.window.after(100, poll)

    def accept(self):
        try:
            cycle = int(self.cycle.get())
            if not 1 <= cycle <= 100:
                raise ValueError
        except ValueError:
            set_text(self.validation, 'Die Zykluszeit muss zwischen 1 und 100 ms liegen.')
            return
        try:
            serial = int(self.serial.get())
            if serial < 1:
                raise ValueError
        except ValueError:
            set_text(self.validation, 'Bitte ein Gerät auswählen oder seine Seriennummer eingeben.')
            return
        pins = {key: name.get().strip() for key, (enabled, name) in self.pins.items() if enabled.get()}
        self.result = normalize_vector_io({'model': self.model.get(), 'serial': serial, 'cycle_ms': cycle,
                                           'pins': pins})
        if self.result is None:
            set_text(self.validation, 'Bitte eindeutige DBC-Namen eingeben (maximal 32 Zeichen).')
            return
        self.window.destroy()
