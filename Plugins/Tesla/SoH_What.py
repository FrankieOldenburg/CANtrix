#!/usr/bin/env python3
"""SoH What - Batteriezustand (State of Health) eines Tesla Model 3 aus einem CAN-Trace.

Plugin fuer den CANtrix (Python 3.6+, nur Standardbibliothek, tkinter).
Zeigt in Karten (Battery Health, HV Isolation, HV Safety, Current State, Cell
Voltages, Charge Statistics, Module Health, Module Temperatures, Battery Lifetime
Stats) zu jedem Zeitpunkt des Traces die Werte, wie die App "SoH:WHAT".

Aufruf:
    python3 SoH_What.py [trace.asc] [-d datenbasis.dbc ...] [--dump SEKUNDEN]

Die Datenbasis (Tesla Model 3 DBC) kommt vom CANtrix
(Umgebungsvariable CANTRIX_DATABASES) oder per -d. Trace-Leser und
DBC-Parser stammen aus ScanMyTesla.py im selben Ordner.

Im Beispiel-Trace nicht verlaesslich (die IDs 0x352/0x292 enthalten dort Frames mit anderem
Inhalt): Original Energy, Nominal Full Energy und SOH zeigen dann "-".
Nicht anzeigbar, weil die Signale in der DBC fehlen: Cell Capacity (CAC),
VSH Impedance Test, Moisture Detection, Temperaturen je Modul.
Werte ausserhalb ihres plausiblen Bereichs werden uebersprungen: es gilt der
letzte plausible Wert vor dem Zeitpunkt.
"""

import argparse
import bisect
import os
import re
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ScanMyTesla as base   # noqa: E402  (Trace-Leser, DBC-Parser)

MILE = base.MILE
CELLS = base.CELLS
MODULES = [(1, 1, 23), (2, 24, 48), (3, 49, 73), (4, 74, 96)]
MODULE_TYPES = {0: 'UNKNOWN', 1: 'E3 NCT', 2: 'E1 NCT', 3: 'E3 CT', 4: 'E1 CT', 5: 'E1 CP'}
CONTACTORS = {0: 'SNA', 1: 'OPEN', 2: 'OPENING', 3: 'CLOSING', 4: 'CLOSED', 5: 'WELDED',
              6: 'BLOCKED'}
HV_STATES = {0: 'HV down', 1: 'HV coming up', 2: 'HV going down', 3: 'HV up for drive',
             4: 'HV up for charge', 5: 'HV up for DC charge', 6: 'HV up'}
HVIL = {1: 'Intact', 0: 'Unknown', 2: 'Current source fault', 3: 'Internal open fault',
        4: 'Vehicle open fault', 5: 'Penthouse lid open', 6: 'Unknown location open',
        7: 'Vehicle node fault', 8: 'No 12V supply', 9: 'Vehicle or penthouse lid open'}

STRING_SIGNALS = (['VINA405', 'VINB405', 'VINC405'] +
                  ['BMS_packSerialNumberByte%02d' % i for i in range(1, 15)])
MODULE_SIGNALS = []
for _n in range(1, 5):
    MODULE_SIGNALS += ['BMS_kwhChargeTotalModule%d' % _n, 'BMS_kwhDischargeTotalModule%d' % _n,
                       'BMS_kwhAcChargeTotalModule%d' % _n, 'BMS_kwhDcChargeTotalModule%d' % _n]
SIGNALS = (['Odometer3B6', 'BMS_nominalFullPackEnergy', 'BMS_nominalEnergyRemaining',
            'BMS_idealEnergyRemaining', 'BMS_expectedEnergyRemaining', 'BMS_energyBuffer',
            'BattBeginningOfLifeEnergy292', 'SOCUI292', 'SOCmin292', 'SOCmax292', 'SOCave292',
            'UI_SOC', 'UI_idealRange', 'UI_Range', 'BattVoltage132', 'SmoothBattCurrent132',
            'BMS_isolationResistance', 'BMS_internalHvilSenseV', 'HVP_hvilStatus',
            'BMS_contactorState', 'BMS_hvState', 'v12vBattVoltage261',
            'BattBrickTempMax332', 'BattBrickTempMin332', 'BMS_packMass', 'BMS_moduleType', 'GTW_birthday',
            'TotalDischargeKWh3D2', 'TotalChargeKWh3D2', 'BMS_acChargerKwhTotal',
            'BMS_dcChargerKwhTotal', 'BMS_kwhRegenChargeTotal', 'BMS_kwhDriveDischargeTotal'] +
           ['Brick%d' % i for i in range(CELLS)] + STRING_SIGNALS + MODULE_SIGNALS)


# --------------------------------------------------------------------------
# Werte
# --------------------------------------------------------------------------

class Values(object):
    """Plausible Werte zu einem Zeitpunkt (letzter plausibler Wert davor)."""

    def __init__(self, trace, date):
        self.trace = trace
        self.date = date            # Datum der Aufzeichnung (datetime)

    def get(self, name, t, low=float('-inf'), high=float('inf')):
        item = self.trace.series.get(name)
        if not item:
            return None
        times, values = item
        index = bisect.bisect_right(times, t) - 1
        for k in range(index, max(index - 300, -1), -1):
            if low <= values[k] <= high:
                return values[k]
        return None

    def text(self, names, t):
        """Text aus Byte-Signalen (je 7 Byte) bzw. einzelnen Bytes."""
        data = bytearray()
        for name in names:
            raw = self.get(name, t)
            if raw is None:
                return None
            raw = int(raw)
            if name.startswith('VIN'):
                chunk = raw.to_bytes(7, 'little')
                data += chunk.replace(b'\x00', b'')
            else:
                data.append(raw & 0xFF)
        result = data.decode('ascii', 'ignore').strip()
        return result if result.isprintable() and result else None

    def vin(self, t):
        return self.text(['VINA405', 'VINB405', 'VINC405'], t)

    def serial(self, t):
        return self.text(['BMS_packSerialNumberByte%02d' % i for i in range(1, 15)], t)

    def cells(self, t):
        found = []
        for i in range(CELLS):
            value = self.get('Brick%d' % i, t, 2.5, 4.5)
            if value is not None:
                found.append((i + 1, value))
        return found

    def age_years(self, t):
        birthday = self.get('GTW_birthday', t, 1.0e9, 2.0e9)
        if birthday is None or self.date is None:
            return None, None
        born = datetime.fromtimestamp(birthday)
        return born, (self.date - born).days / 365.25


def number(value, decimals=1, unit=''):
    if value is None:
        return '—'
    return ('%.*f' % (decimals, value)).replace('.', ',') + (' ' + unit if unit else '')


def percent(a, b):
    return None if a is None or not b else a / b * 100.0


def build_cards(v):
    """Karten: (Titel, [(Beschriftung, Funktion Zeit -> Text)])."""
    g = v.get
    n = number

    def spread(kind):
        def function(t):
            cells = v.cells(t)
            if not cells:
                return '—'
            low = min(cells, key=lambda c: c[1])
            high = max(cells, key=lambda c: c[1])
            if kind == 'min':
                return '%s  (Brick #%d)' % (n(low[1], 3, 'V'), low[0])
            if kind == 'max':
                return '%s  (Brick #%d)' % (n(high[1], 3, 'V'), high[0])
            if kind == 'imbalance':
                return n((high[1] - low[1]) * 1000, 1, 'mV')
            return '%d/%d' % (len(cells), CELLS)
        return function

    def soh(t):
        value = percent(g('BMS_nominalFullPackEnergy', t, 60, 100),
                        g('BattBeginningOfLifeEnergy292', t, 60, 100))
        return n(value if value is not None and value <= 100 else None, 2, '%')

    def degradation(t):
        full = g('BMS_nominalFullPackEnergy', t, 60, 100)
        original = g('BattBeginningOfLifeEnergy292', t, 60, 100)
        return n(None if full is None or original is None or original < full else original - full,
                 1, 'kWh')

    def produced(t):
        born, years = v.age_years(t)
        if born is None:
            return '—'
        return '%s  (%s Jahre)' % (born.strftime('%d.%m.%Y'), n(years, 1))

    def power(t):
        volt = g('BattVoltage132', t, 100, 500)
        amp = g('SmoothBattCurrent132', t, -2000, 2000)
        return n(None if volt is None or amp is None else volt * amp / 1000.0, 2, 'kW')

    def efficiency(t):
        return n(percent(g('TotalDischargeKWh3D2', t, 1, 1e7), g('TotalChargeKWh3D2', t, 1, 1e7)),
                 1, '%')

    def threshold(t):
        volt = g('BattVoltage132', t, 100, 500)
        return n(None if volt is None else 0.5 * volt, 0, 'kΩ') if volt else '—'

    def mapped(name, table, low=0, high=15):
        def function(t):
            value = g(name, t, low, high)
            return '—' if value is None else table.get(int(value), str(int(value)))
        return function

    def one(name, decimals, unit, low=float('-inf'), high=float('inf'), scale=1.0):
        def function(t):
            value = g(name, t, low, high)
            return n(None if value is None else value * scale, decimals, unit)
        return function

    def temperature(kind):
        def function(t):
            low = g('BattBrickTempMin332', t, -30, 80)
            high = g('BattBrickTempMax332', t, -30, 80)
            if kind == 'min':
                return n(low, 1, '°C')
            if kind == 'max':
                return n(high, 1, '°C')
            return n(None if low is None or high is None else high - low, 1, '°C')
        return function

    def module(index, first, last):
        def counters(t):
            ch = g('BMS_kwhChargeTotalModule%d' % index, t, 0, 1e6)
            dis = g('BMS_kwhDischargeTotalModule%d' % index, t, 0, 1e6)
            ac = g('BMS_kwhAcChargeTotalModule%d' % index, t, 0, 1e6)
            dc = g('BMS_kwhDcChargeTotalModule%d' % index, t, 0, 1e6)
            return ch, dis, ac, dc

        def health(t):
            ch, dis, _ac, _dc = counters(t)
            return n(percent(dis, ch), 1, '%')

        def energy(t):
            ch, dis, ac, dc = counters(t)
            return 'Charge %s · Discharge %s · AC/DC %s / %s' % (
                n(ch, 0, 'kWh'), n(dis, 0, 'kWh'), n(ac, 0), n(dc, 0))
        return [('Module %d (Bricks %d-%d)' % (index, first, last), health),
                ('', energy)]

    cards = []
    cards.append(('Battery Health', [
        ('VIN', lambda t: v.vin(t) or '—'),
        ('Produced', produced),
        ('Odometer', one('Odometer3B6', 0, 'km', 0, 1e7)),
        ('State of Health (SOH)', soh),
        ('Original Energy', one('BattBeginningOfLifeEnergy292', 1, 'kWh', 60, 100)),
        ('Nominal Full Energy', one('BMS_nominalFullPackEnergy', 1, 'kWh', 60, 100)),
        ('Degradation', degradation),
    ]))
    cards.append(('HV Isolation', [
        ('Contactors', mapped('BMS_contactorState', CONTACTORS)),
        ('HV Chain', one('BMS_isolationResistance', 0, 'kΩ', 1, 10000)),
        ('Alert Threshold (500 Ω/V)', threshold),
    ]))
    cards.append(('HV Safety', [
        ('HVIL Interlock', mapped('HVP_hvilStatus', HVIL)),
        ('HVIL Sense', one('BMS_internalHvilSenseV', 1, 'V', 0, 65)),
        ('12V Battery', one('v12vBattVoltage261', 1, 'V', 9, 16)),
        ('HV State', mapped('BMS_hvState', HV_STATES)),
    ]))
    cards.append(('Current State', [
        ('State of Charge (SOC, UI)', one('UI_SOC', 1, '%', 0, 100)),
        ('BMS SOC (UI / Min / Max)', lambda t: '%s / %s / %s' % (
            n(g('SOCUI292', t, 0, 100), 1), n(g('SOCmin292', t, 0, 100), 1),
            n(g('SOCmax292', t, 0, 100), 1))),
        ('Expected Range', one('UI_Range', 0, 'km', 0, 450, MILE)),
        ('Ideal Range', one('UI_idealRange', 0, 'km', 0, 450, MILE)),
        ('Pack Voltage', one('BattVoltage132', 1, 'V', 100, 500)),
        ('Pack Current', one('SmoothBattCurrent132', 1, 'A', -2000, 2000)),
        ('Pack Power', power),
    ]))
    cards.append(('Cell Voltages', [
        ('Min Voltage', spread('min')),
        ('Max Voltage', spread('max')),
        ('Imbalance', spread('imbalance')),
        ('Cells collected', spread('count')),
    ]))
    cards.append(('Module Temperatures', [
        ('Min Temperature', temperature('min')),
        ('Max Temperature', temperature('max')),
        ('Temperature Spread', temperature('spread')),
    ]))
    cards.append(('Charge Statistics', [
        ('Total Discharge', one('TotalDischargeKWh3D2', 1, 'kWh', 1, 1e7)),
        ('Total Charge', one('TotalChargeKWh3D2', 1, 'kWh', 1, 1e7)),
        ('Efficiency', efficiency),
        ('AC Charging', one('BMS_acChargerKwhTotal', 1, 'kWh', 0, 1e7)),
        ('DC Fast Charging', one('BMS_dcChargerKwhTotal', 1, 'kWh', 0, 1e7)),
        ('Regeneration', one('BMS_kwhRegenChargeTotal', 1, 'kWh', 0, 1e7)),
    ]))
    rows = []
    for index, first, last in MODULES:
        rows += module(index, first, last)
    cards.append(('Module Health', rows))
    cards.append(('Battery Lifetime Stats', [
        ('Battery Age', lambda t: n(v.age_years(t)[1], 1, 'years')),
        ('Serial Number', lambda t: v.serial(t) or '—'),
        ('Module Type', mapped('BMS_moduleType', MODULE_TYPES, 0, 7)),
        ('Pack Mass', one('BMS_packMass', 0, 'kg', 300, 500)),
        ('Original Energy', one('BattBeginningOfLifeEnergy292', 1, 'kWh', 60, 100)),
    ]))
    return cards


# --------------------------------------------------------------------------
# Trace laden
# --------------------------------------------------------------------------

def trace_date(path):
    """Datum der Aufzeichnung aus der ASC-Kopfzeile "date ..." (sonst Dateizeit)."""
    try:
        with open(path, 'r', errors='replace') as handle:
            for _ in range(10):
                line = handle.readline()
                match = re.match(r'\s*date\s+(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})', line)
                if match:
                    return datetime.strptime(match.group(1) + ' ' + match.group(2),
                                             '%Y-%m-%d %H:%M:%S')
    except OSError:
        pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return datetime.now()


def load(trace_path, dbc_paths, progress=None):
    messages, signals = base.read_dbcs(dbc_paths)
    wanted = set(name for name in SIGNALS if name in signals)
    trace = base.read_trace(trace_path, messages, wanted, progress)
    return Values(trace, trace_date(trace_path)), sorted(set(SIGNALS) - wanted)


# --------------------------------------------------------------------------
# Oberflaeche
# --------------------------------------------------------------------------

PAGE = '#f0f0f5'
CARD = '#ffffff'
INK = '#111111'
MUTED = '#777777'
GOOD = '#34a853'
ACCENT = '#6b73d6'
SPEEDS = ['1x', '5x', '10x', '25x', '50x', '100x']


class App(object):
    def __init__(self, tk, ttk, font, root, trace_path, dbc_paths):
        self.tk = tk
        self.root = root
        self.trace_path = trace_path
        self.dbc_paths = dbc_paths
        self.values = None
        self.labels = []
        self.time = 0.0
        self.playing = False
        self.progress = [0.0]
        self.result = {}
        base_font = font.nametofont('TkDefaultFont').actual('family')
        self.f_title = font.Font(family=base_font, size=15, weight='bold')
        self.f_text = font.Font(family=base_font, size=13)
        self.f_value = font.Font(family='Menlo' if sys.platform == 'darwin' else 'Courier',
                                 size=13, weight='bold')

        root.configure(bg=PAGE)
        root.geometry('560x900')
        root.minsize(360, 300)
        bar = tk.Frame(root, bg=ACCENT)
        bar.pack(side='top', fill='x')
        tk.Label(bar, text='SoH – Battery Health', bg=ACCENT, fg='white',
                 font=self.f_title).pack(side='left', padx=12, pady=8)
        controls = tk.Frame(root, bg=PAGE)
        controls.pack(side='top', fill='x', padx=8, pady=6)
        self.play_button = ttk.Button(controls, text='Start', width=7, command=self.toggle_play,
                                      state='disabled')
        self.play_button.pack(side='left')
        self.slider_var = tk.DoubleVar(value=0.0)
        self.slider = ttk.Scale(controls, from_=0.0, to=1.0, variable=self.slider_var,
                                command=self.on_slider)
        self.slider.state(['disabled'])   # Tk 8.5: ttk.Scale kennt kein -state
        self.slider.pack(side='left', fill='x', expand=True, padx=8)
        self.time_label = tk.Label(controls, text='', bg=PAGE, fg=INK, width=20, anchor='e')
        self.time_label.pack(side='left')
        self.speed_var = tk.StringVar(value='10x')
        ttk.Combobox(controls, textvariable=self.speed_var, values=SPEEDS, width=5,
                     state='readonly').pack(side='left', padx=(8, 0))
        self.status = tk.Label(root, anchor='w', bg=PAGE, fg=MUTED, text='')
        self.status.pack(side='bottom', fill='x', padx=8, pady=2)

        self.canvas = tk.Canvas(root, bg=PAGE, highlightthickness=0)
        scroll = ttk.Scrollbar(root, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.page = tk.Frame(self.canvas, bg=PAGE)
        self.window = self.canvas.create_window((0, 0), window=self.page, anchor='nw')
        self.page.bind('<Configure>', lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        base.bind_scroll(root, self.canvas)
        root.bind('<space>', lambda _e: self.toggle_play())
        root.protocol('WM_DELETE_WINDOW', self.close)

        self.status.configure(text='Lade %s ...' % os.path.basename(trace_path))
        threading.Thread(target=self._load, daemon=True).start()
        root.after(100, self._poll)

    def _load(self):
        try:
            values, missing = load(self.trace_path, self.dbc_paths,
                                   lambda share: self.progress.__setitem__(0, share))
            self.result = {'values': values, 'missing': missing}
        except Exception as error:   # noqa: BLE001 - wird angezeigt
            self.result = {'error': '%s: %s' % (type(error).__name__, error)}

    def _poll(self):
        if not self.result:
            self.status.configure(text='Lade %s ... %d %%' % (os.path.basename(self.trace_path),
                                                             int(self.progress[0] * 100)))
            self.root.after(100, self._poll)
            return
        if 'error' in self.result:
            self.status.configure(text=self.result['error'])
            return
        self.values = self.result['values']
        trace = self.values.trace
        self.time = trace.end
        self.slider.configure(from_=trace.start, to=max(trace.end, trace.start + 1e-6))
        self.slider.state(['!disabled'])
        self.slider_var.set(self.time)
        self.play_button.configure(state='normal')
        self.build()
        self.update_values()
        if self.result['missing']:
            print('Signale nicht in der Datenbasis: ' + ', '.join(self.result['missing']),
                  file=sys.stderr)

    def build(self):
        tk = self.tk
        for title, rows in build_cards(self.values):
            card = tk.Frame(self.page, bg=CARD, highlightbackground='#dddddd',
                            highlightthickness=1)
            card.pack(fill='x', padx=12, pady=8)
            tk.Label(card, text=title, bg=CARD, fg=INK, font=self.f_title,
                     anchor='w').pack(fill='x', padx=14, pady=(10, 4))
            tk.Frame(card, bg='#e4e4e4', height=1).pack(fill='x', padx=14)
            for label, function in rows:
                row = tk.Frame(card, bg=CARD)
                row.pack(fill='x', padx=14, pady=3)
                tk.Label(row, text=label, bg=CARD, fg=MUTED, font=self.f_text,
                         anchor='w').pack(side='left')
                value = tk.Label(row, text='', bg=CARD, fg=INK, font=self.f_value, anchor='e')
                value.pack(side='right')
                self.labels.append((value, function))
            tk.Frame(card, bg=CARD, height=6).pack()

    # -- Zeit --------------------------------------------------------------

    def update_values(self):
        if self.values is None:
            return
        trace = self.values.trace
        for label, function in self.labels:
            text = function(self.time)
            label.configure(text=text, fg=INK if text != '—' else MUTED)
        self.time_label.configure(text='%s s / %s s' % (
            ('%.1f' % (self.time - trace.start)).replace('.', ','),
            ('%.1f' % (trace.end - trace.start)).replace('.', ',')))
        self.status.configure(text='%s - %d Frames' % (os.path.basename(self.trace_path),
                                                       trace.frames))

    def on_slider(self, _value):
        if self.values is None:
            return
        self.time = float(self.slider_var.get())
        self.update_values()

    def toggle_play(self):
        if self.values is None:
            return
        self.playing = not self.playing
        self.play_button.configure(text='Stop' if self.playing else 'Start')
        if self.playing:
            trace = self.values.trace
            if self.time >= trace.end:
                self.time = trace.start
            self.last_tick = time.time()
            self._tick()

    def _tick(self):
        if not self.playing:
            return
        trace = self.values.trace
        now = time.time()
        try:
            speed = float(self.speed_var.get().rstrip('x'))
        except ValueError:
            speed = 1.0
        self.time += (now - self.last_tick) * speed
        self.last_tick = now
        if self.time >= trace.end:
            self.time = trace.end
            self.playing = False
            self.play_button.configure(text='Start')
        self.slider_var.set(self.time)
        self.update_values()
        if self.playing:
            self.root.after(100, self._tick)

    def close(self):
        self.playing = False
        self.root.destroy()


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------

def dump(trace_path, dbc_paths, seconds):
    values, _missing = load(trace_path, dbc_paths)
    t = values.trace.start + seconds
    for title, rows in build_cards(values):
        print('\n== %s' % title)
        for label, function in rows:
            print('  %-30s %s' % (label, function(t)))


def main(argv=None):
    parser = argparse.ArgumentParser(description='SoH What - Batteriezustand eines Tesla aus einem CAN-Trace')
    parser.add_argument('trace', nargs='?', help='ASC-Trace (Fahrzeug-CAN eines Tesla Model 3)')
    parser.add_argument('-d', '--dbc', action='append',
                        help='Datenbasis (DBC), mehrfach moeglich; Standard: die vom CANtrix uebergebene')
    parser.add_argument('--dump', type=float, metavar='SEKUNDEN',
                        help='Werte zu diesem Zeitpunkt als Text ausgeben (ohne Fenster)')
    args = parser.parse_args(argv)
    trace_path = args.trace
    dbc_paths = base.database_paths(args.dbc)
    if args.dump is not None:
        if not trace_path or not dbc_paths:
            parser.error('--dump braucht einen Trace und eine Datenbasis (-d oder %s)'
                         % base.DATABASE_VARIABLE)
        dump(trace_path, dbc_paths, args.dump)
        return 0

    import tkinter as tk
    from tkinter import ttk, filedialog, font, messagebox
    root = tk.Tk()
    root.title('SoH What')
    if not trace_path:
        trace_path = filedialog.askopenfilename(
            parent=root, title='Tesla CAN Trace oeffnen',
            filetypes=[('Vector ASC', '*.asc'), ('Alle Dateien', '*.*')])
        if not trace_path:
            root.destroy()
            return 1
    if not os.path.isfile(trace_path):
        messagebox.showerror('SoH What', 'Trace nicht gefunden:\n%s' % trace_path)
        return 1
    dbc_paths = [p for p in dbc_paths if p.lower().endswith('.dbc')]
    missing = [p for p in dbc_paths if not os.path.isfile(p)]
    if not dbc_paths or missing:
        messagebox.showerror('SoH What', 'Es wird eine geladene DBC-Datenbasis gebraucht.\n'
                             'Bitte im CANtrix eine DBC (z. B. Model3CAN.dbc) laden.' +
                             ('\n\nNicht gefunden:\n' + '\n'.join(missing) if missing else ''))
        return 1
    root.title('SoH What - %s' % os.path.basename(trace_path))
    App(tk, ttk, font, root, trace_path, dbc_paths)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
