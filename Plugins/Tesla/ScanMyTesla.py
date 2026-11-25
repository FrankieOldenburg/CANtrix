#!/usr/bin/env python3
"""Scan My Tesla - Anzeige im Stil der "Scan My Tesla"-App fuer einen CAN-Trace.

Plugin fuer den CANtrix (Python 3.6+, nur Standardbibliothek, tkinter).

Aufruf:
    python3 ScanMyTesla.py [trace.asc] [-d datenbasis.dbc ...] [--dump SEKUNDEN]

Der Trace ist ein Vector-ASC-Trace eines Tesla Model 3 (Fahrzeug-CAN). Die
Signale werden mit der Datenbasis (DBC, z. B. "Model3CAN.dbc") dekodiert. Das
Plugin nimmt die im CANtrix geladene Datenbasis: der Analyzer uebergibt
ihre Pfade in der Umgebungsvariable CANTRIX_DATABASES (getrennt durch
os.pathsep). Mit -d (mehrfach moeglich) kann man sie beim Start von Hand
angeben; eine eingebaute Datenbasis gibt es nicht. Die Anzeige zeigt zu jedem Zeitpunkt
des Traces den letzten Wert jedes Signals; ein Zeitschieber und Abspielen
ersetzen die Live-Daten der App. Der Balken unter jeder Zeile zeigt den Wert
im Bereich (Minimum bis Maximum) des gesamten Traces.

Im Plugin-Fenster des CANtrixs: Skript "ScanMyTesla.py", Haken
"Aktuellen Trace an das Plugin uebergeben".
"""

import argparse
import bisect
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATABASE_VARIABLE = 'CANTRIX_DATABASES'

MILE = 1.609344
CELLS = 96


# --------------------------------------------------------------------------
# Datenbasis (DBC)
# --------------------------------------------------------------------------

BO_RE = re.compile(r'^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)')
SG_RE = re.compile(
    r'^\s*SG_\s+(\w+)\s*(M|m\d+M?)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*'
    r'\(\s*([^,\s]+)\s*,\s*([^)\s]+)\s*\)\s*\[\s*([^|\s]*)\s*\|\s*([^\]\s]*)\s*\]\s*'
    r'"([^"]*)"')


class Signal(object):
    def __init__(self, name, mux, start, length, big, signed, factor, offset, unit, low=0.0, high=0.0):
        self.name = name
        self.selector = (mux == 'M')
        self.mux_value = int(mux[1:].rstrip('M')) if mux and mux[0] == 'm' else None
        self.start = start
        self.length = length
        self.big = big
        self.signed = signed
        self.factor = factor
        self.offset = offset
        self.unit = unit
        self.low = low
        self.high = high
        self.mask = (1 << length) - 1
        if big:
            plan = []
            pos = start
            for _ in range(length):
                plan.append(pos)
                pos = pos + 15 if pos % 8 == 0 else pos - 1
            self.plan = plan
        else:
            self.plan = None

    def raw(self, data, number):
        """Rohwert aus den Daten (`number` = Daten als Intel-Ganzzahl)."""
        if self.plan is None:
            raw = (number >> self.start) & self.mask
        else:
            raw = 0
            for pos in self.plan:
                index = pos // 8
                bit = (data[index] >> (pos % 8)) & 1 if index < len(data) else 0
                raw = (raw << 1) | bit
        if self.signed and raw >> (self.length - 1):
            raw -= 1 << self.length
        return raw

    def invalid(self, raw):
        """Ungueltig: Temperatur mit lauter 1-Bits ("keine Daten") oder ausserhalb des DBC-Bereichs."""
        if self.unit == 'C' and not self.signed and raw >= self.mask - 4 and self.length >= 8:
            return True
        if self.high > self.low:
            value = raw * self.factor + self.offset
            span = self.high - self.low
            return value < self.low - span * 0.01 or value > self.high + span * 0.01
        return False

    def physical(self, raw):
        if self.factor == 1.0 and self.offset == 0.0:
            return raw     # exakte Ganzzahl (z. B. Textsignale mit 56 Bit)
        return raw * self.factor + self.offset


class Message(object):
    def __init__(self, identifier, name, length):
        self.identifier = identifier
        self.name = name
        self.length = length
        self.signals = []
        self.selector = None


def read_dbcs(paths):
    """Liest mehrere DBC-Dateien; bei doppelten IDs/Namen gewinnt die erste."""
    messages = {}
    signals = {}
    for path in paths:
        more_messages, more_signals = read_dbc(path)
        for identifier, message in more_messages.items():
            messages.setdefault(identifier, message)
        for name, item in more_signals.items():
            signals.setdefault(name, item)
    return messages, signals


def database_paths(arguments):
    """Pfade der Datenbasis: -d, sonst die vom CANtrix uebergebenen."""
    paths = list(arguments or [])
    if not paths:
        paths = [p for p in os.environ.get(DATABASE_VARIABLE, '').split(os.pathsep) if p]
    return paths


def read_dbc(path):
    """Gibt (Nachrichten je ID, Signale je Name) zurueck."""
    with open(path, 'rb') as handle:
        text = handle.read().decode('latin-1')
    messages = {}
    signals = {}
    current = None
    for line in text.replace('\r', '').split('\n'):
        match = BO_RE.match(line)
        if match:
            current = Message(int(match.group(1)) & 0x1FFFFFFF, match.group(2),
                              int(match.group(3)))
            messages[current.identifier] = current
            continue
        if current is None:
            continue
        match = SG_RE.match(line)
        if not match:
            if line.strip() and not line.startswith((' ', '\t')):
                current = None
            continue
        (name, mux, start, length, order, sign, factor, offset,
         low, high, unit) = match.groups()
        try:
            signal = Signal(name, mux, int(start), int(length), order == '0',
                            sign == '-', float(factor), float(offset), unit,
                            float(low or 0), float(high or 0))
        except ValueError:
            continue
        current.signals.append(signal)
        if signal.selector:
            current.selector = signal
        signals.setdefault(name, (current, signal))
    return messages, signals


# --------------------------------------------------------------------------
# Trace (ASC)
# --------------------------------------------------------------------------

class Trace(object):
    """Dekodierte Verlaeufe der benoetigten Signale (nur Wertaenderungen)."""

    def __init__(self):
        self.series = {}          # Name -> (Zeiten, Werte)
        self.frame_times = []     # Zeit jedes CAN-Frames
        self.start = 0.0
        self.end = 0.0
        self.frames = 0

    def value(self, name, t):
        item = self.series.get(name)
        if item is None:
            return None
        index = bisect.bisect_right(item[0], t) - 1
        return item[1][index] if index >= 0 else None

    def span(self, name):
        item = self.series.get(name)
        if not item or not item[1]:
            return None
        return min(item[1]), max(item[1])


def read_trace(path, messages, wanted, progress=None):
    """Liest den ASC-Trace und dekodiert die Signale aus `wanted` (Namen)."""
    by_id = {}
    for message in messages.values():
        chosen = [s for s in message.signals if s.name in wanted]
        if chosen:
            by_id[message.identifier] = (message, chosen)
    trace = Trace()
    series = dict((name, ([], [])) for name in wanted)
    last_raw = {}
    size = max(os.path.getsize(path), 1)
    done = 0
    first = None
    times = trace.frame_times
    with open(path, 'r', errors='replace') as handle:
        for number, line in enumerate(handle):
            done += len(line)
            if progress is not None and number % 20000 == 0:
                progress(done / size)
            parts = line.split()
            if len(parts) < 7 or parts[3] not in ('Rx', 'Tx') or parts[4] != 'D':
                continue
            try:
                t = float(parts[0])
                identifier = int(parts[2].rstrip('xX'), 16)
                dlc = int(parts[5])
                data = bytes(int(x, 16) for x in parts[6:6 + dlc])
            except ValueError:
                continue
            if first is None:
                first = t
            trace.end = t
            times.append(t)
            entry = by_id.get(identifier)
            if entry is None or not data:
                continue
            message, chosen = entry
            numeric = int.from_bytes(data, 'little')
            selected = None
            if message.selector is not None:
                selected = message.selector.raw(data, numeric)
            for signal in chosen:
                if signal.mux_value is not None and signal.mux_value != selected:
                    continue
                raw = signal.raw(data, numeric)
                if last_raw.get(signal.name) == raw:
                    continue
                last_raw[signal.name] = raw
                if signal.invalid(raw):
                    continue
                stamps, values = series[signal.name]
                stamps.append(t)
                values.append(signal.physical(raw))
    trace.series = dict((k, v) for k, v in series.items() if v[0])
    trace.start = first or 0.0
    trace.frames = len(times)
    if progress is not None:
        progress(1.0)
    return trace


# --------------------------------------------------------------------------
# Anzeige-Liste (Reihenfolge und Namen wie in der App)
# --------------------------------------------------------------------------

class Entry(object):
    """Eine Zeile der Anzeige."""

    def __init__(self, label, unit, decimals, deps, function):
        self.label = label
        self.unit = unit
        self.decimals = decimals
        self.deps = deps
        self.function = function    # (Trace, Zeit) -> Wert oder None
        self.low = None
        self.high = None
        self.direct = None          # Signalname, wenn der Wert unveraendert uebernommen wird


def simple(label, name, unit='', decimals=1, scale=1.0, shift=0.0):
    if scale == 1.0 and shift == 0.0:
        function = lambda trace, t: trace.value(name, t)
        entry = Entry(label, unit, decimals, [name], function)
        entry.direct = name
        return entry
    else:
        def function(trace, t):
            value = trace.value(name, t)
            return None if value is None else value * scale + shift
    return Entry(label, unit, decimals, [name], function)


def computed(label, unit, decimals, deps, function):
    return Entry(label, unit, decimals, deps, function)


def cell_values(trace, t):
    values = (trace.value('Brick%d' % i, t) for i in range(CELLS))
    return [v for v in values if v is not None and v > 0.5]


def cell_stat(kind):
    def function(trace, t):
        values = cell_values(trace, t)
        if not values:
            return None
        if kind == 'min':
            return min(values)
        if kind == 'max':
            return max(values)
        if kind == 'avg':
            return sum(values) / len(values)
        return (max(values) - min(values)) * 1000.0
    return function


def product(first, second, factor=1.0):
    def function(trace, t):
        a = trace.value(first, t)
        b = trace.value(second, t)
        return None if a is None or b is None else a * b * factor
    return function


def difference(first, second):
    def function(trace, t):
        a = trace.value(first, t)
        b = trace.value(second, t)
        return None if a is None or b is None else a - b
    return function


def mean_of(first, second):
    def function(trace, t):
        a = trace.value(first, t)
        b = trace.value(second, t)
        return None if a is None or b is None else (a + b) / 2.0
    return function


def received_count(trace, t):
    return float(sum(1 for name in trace.series if trace.value(name, t) is not None))


def packets_per_second(trace, t):
    times = trace.frame_times
    return float(bisect.bisect_right(times, t) - bisect.bisect_right(times, t - 1.0))


def build_groups():
    """Gruppen mit ihren Zeilen. Fehlende Signale zeigen einen leeren Wert."""
    s = simple
    groups = []

    groups.append(('Fahrzeug', [
        s('Odometer', 'Odometer3B6', 'km', 0),
        s('Speed', 'DI_vehicleSpeed', 'km/h', 2),
        s('Accelerator Pedal', 'DI_accelPedalPos', '%', 1),
        s('Brake Pedal', 'DI_brakePedalState', '', 0),
        s('Steering Angle', 'SteeringAngle129', 'Deg', 1),
        s('Steering Speed', 'SteeringSpeed129', 'D/S', 1),
        s('F RPM', 'DIF_axleSpeed', 'RPM', 1),
        s('R RPM', 'DIR_axleSpeed', 'RPM', 1),
        s('F torque', 'FrontTorque1D5', 'Nm', 2),
        s('R torque', 'RearTorque1D8', 'Nm', 2),
        s('F torque 186', 'DIF_torqueActual', 'Nm', 2),
        s('R torque 108', 'DIR_torqueActual', 'Nm', 2),
        s('Front ride height', 'VCLEFT_frontRideHeight', 'mm', 2),
        s('Rear ride height', 'VCLEFT_rearRideHeight', 'mm', 2),
        s('Distance', 'Odometer3B6', 'km', 0),
        s('Ideal range', 'UI_idealRange', 'km', 0, scale=MILE),
        s('Range', 'UI_Range', 'km', 0, scale=MILE),
    ]))

    groups.append(('Leistung', [
        computed('Battery power', 'kW', 2, ['BattVoltage132', 'SmoothBattCurrent132'],
                 product('BattVoltage132', 'SmoothBattCurrent132', 0.001)),
        s('F power', 'FrontPower2E5', 'kW', 2),
        s('R power', 'RearPower266', 'kW', 2),
        s('Max discharge power', 'BMS_maxDischargePower', 'kW', 0),
        s('Max regen power', 'BMS_maxRegenPower', 'kW', 0),
        s('Max charge power', 'BMS_chgPowerAvailable', 'kW', 0),
        s('System regen max', 'SystemRegenPowerMax268', 'kW', 0),
        s('System drive max', 'SystemDrivePowerMax268', 'kW', 0),
        s('Battery current', 'SmoothBattCurrent132', 'A', 2),
        s('Max discharge current', 'MaxDischargeCurrent2D2', 'A', 0),
        s('Max charge current', 'MaxChargeCurrent2D2', 'A', 0),
        s('Battery voltage', 'BattVoltage132', 'V', 0),
        s('Max pack voltage', 'MaxVoltage2D2', 'V', 0, scale=2.0),
        s('Min pack voltage', 'MinVoltage2D2', 'V', 0, scale=2.0),
        s('Avg consumption', 'UI_ratedWHpM', 'Wh/km', 0, scale=1 / MILE),
    ]))

    groups.append(('Energie', [
        s('Discharge', 'TotalDischargeKWh3D2', 'kWh', 0),
        s('Charge', 'TotalChargeKWh3D2', 'kWh', 0),
        s('DC Charge', 'BMS_dcChargerKwhTotal', 'kWh', 0),
        s('AC Charge', 'BMS_acChargerKwhTotal', 'kWh', 0),
        s('Regenerated', 'BMS_kwhRegenChargeTotal', 'kWh', 0),
        s('Energy', 'BMS_kwhDriveDischargeTotal', 'kWh', 0),
        s('SOC', 'UI_SOC', '%', 1),
        s('SOC expected', 'UI_uSOE', '%', 1),
        s('BMS SOC UI', 'SOCUI292', '%', 1),
        s('BMS SOC Min', 'SOCmin292', '%', 1),
        s('BMS SOC Max', 'SOCmax292', '%', 1),
        s('BMS SOC Avg', 'SOCave292', '%', 1),
    ]))

    groups.append(('Batterie', [
        computed('Cell volt min', 'Vc', 3, ['Brick%d' % i for i in range(CELLS)],
                 cell_stat('min')),
        computed('Cell volt avg', 'Vc', 3, ['Brick%d' % i for i in range(CELLS)],
                 cell_stat('avg')),
        computed('Cell volt max', 'Vc', 3, ['Brick%d' % i for i in range(CELLS)],
                 cell_stat('max')),
        computed('Cell imbalance', 'mV', 2, ['Brick%d' % i for i in range(CELLS)],
                 cell_stat('spread')),
        s('Cell volt max brick', 'BattBrickVoltageMaxNum332', '', 1),
        s('Cell volt min brick', 'BattBrickVoltageMinNum332', '', 1),
        s('Cell temp max', 'BattBrickTempMax332', 'C', 1),
        s('Cell temp min', 'BattBrickTempMin332', 'C', 1),
        computed('Cell temp avg', 'C', 1, ['BattBrickTempMax332', 'BattBrickTempMin332'],
                 mean_of('BattBrickTempMax332', 'BattBrickTempMin332')),
        s('Pack heat dissipation', 'BMSdissipation312', 'kW', 2),
    ]))

    groups.append(('Temperaturen', [
        s('Front oil temp', 'FrontOilPumpOilTempEst396', 'C', 1),
        s('Rear oil temp', 'DIR_oilPumpFluidT', 'C', 1),
        s('F Inverter PCB temp', 'TempInvPCB376', 'C', 1),
        s('F Inverter temp', 'TempInverter376', 'C', 1),
        s('F Stator temp', 'TempStator376', 'C', 1),
        s('F heatsink temp', 'TempInvHeatsink376', 'C', 1),
        s('R Inverter PCB temp', 'RearTempInvPCB315', 'C', 1),
        s('R Inverter temp', 'RearTempInverter315', 'C', 1),
        s('R Stator temp', 'RearTempStator315', 'C', 1),
        s('R heat sink', 'RearTempInvHeatsink315', 'C', 1),
        s('FL brake est', 'BrakeTempFL3FE', 'C', 1),
        s('FR brake est', 'BrakeTempFR3FE', 'C', 1),
        s('RL brake est', 'BrakeTempRL3FE', 'C', 1),
        s('RR brake est', 'BrakeTempRR3FE', 'C', 1),
        s('Battery inlet', 'VCFRONT_tempCoolantBatInlet', 'C', 1),
        s('Powertrain inlet', 'VCFRONT_tempCoolantPTInlet', 'C', 1),
        s('Outside temp', 'VCFRONT_tempAmbient', 'C', 1),
        s('Outside temp filtered', 'VCFRONT_tempAmbientFiltered', 'C', 1),
    ]))

    groups.append(('Kuehlung & Klima', [
        s('Front oil flow', 'FrontOilFlowActual396', 'lpm', 2),
        s('Rear oil flow', 'DIR_oilPumpFlowActual', 'lpm', 2),
        s('Battery flow', 'VCFRONT_coolantFlowBatActual', 'lpm', 2),
        s('Powertrain flow', 'VCFRONT_coolantFlowPTActual', 'lpm', 2),
        s('Evap demand', 'VCRIGHT_wattsDemandEvap', 'W', 0),
        s('Evap enabled', 'VCRIGHT_hvacEvapEnabled', '0/1', 2),
        s('Evap temp', 'VCRIGHT_tempEvaporator', 'C', 2),
        s('Evap target', 'VCRIGHT_tempEvaporatorTarget', 'C', 2),
        s('Target bat Active cool', 'BMSinletActiveCoolTarget312', 'C', 1),
        s('Target bat Active heat', 'BMSinletActiveHeatTarget312', 'C', 1),
        s('Target bat Passive', 'BMSinletPassiveTarget312', 'C', 1),
        s('Duct left', 'VCRIGHT_tempDuctLeft', 'C', 1),
        s('Duct right', 'VCRIGHT_tempDuctRight', 'C', 1),
        s('Duct target left', 'VCRIGHT_hvacDuctTargetLeft', 'C', 1),
        s('Duct target right', 'VCRIGHT_hvacDuctTargetRight', 'C', 1),
        s('Cabin temp', 'VCRIGHT_hvacCabinTempEst', 'C', 1),
        s('Blower speed target', 'VCRIGHT_hvacBlowerSpeedRPMReq', 'RPM', 0),
        s('Blower speed actual', 'VCLEFT_hvacBlowerRPMActual', 'RPM', 0),
    ]))

    groups.append(('Zellspannungen', [
        s('Cell %2d voltage' % (i + 1), 'Brick%d' % i, 'Vc', 3) for i in range(CELLS)
    ]))

    groups.append(('Statistik', [
        computed('Packets per second', 'x', 0, [], packets_per_second),
        computed('Signals received', 'x', 0, [], received_count),
    ]))
    return groups


# --------------------------------------------------------------------------
# Oberflaeche
# --------------------------------------------------------------------------

BG = '#000000'
ROW = '#0c0c0c'
TRACK = '#2a2a2a'
LABEL = '#a0a0a0'
VALUE = '#ffffff'
BAR = '#f5a623'
HEADER = '#d0d0d0'
RED = '#ff3b30'

SPEEDS = ['0,5x', '1x', '2x', '5x', '10x', '25x', '50x', '100x']

ROW_HEIGHT = 30
HEADER_HEIGHT = 34


def de_number(value, decimals):
    return ('%.*f' % (decimals, value)).replace('.', ',')


def de_time(seconds):
    return de_number(seconds, 3) + ' s'


def bind_scroll(root, canvas):
    """Scrollen mit Mausrad und Touchpad (Tk 9 unter macOS meldet <TouchpadScroll>)."""
    canvas.configure(yscrollincrement=10)
    pending = [0.0]

    def on_wheel(event):
        if getattr(event, 'num', 0) == 4:
            steps = -3
        elif getattr(event, 'num', 0) == 5:
            steps = 3
        else:
            delta = event.delta
            steps = -delta // 120 * 3 if abs(delta) >= 120 else (-3 if delta > 0 else 3)
        canvas.yview_scroll(int(steps), 'units')

    def on_touchpad(event):
        try:
            _dx, dy = (float(v) for v in root.tk.splitlist(
                root.tk.call('tk::PreciseScrollDeltas', event.delta)))
        except Exception:   # noqa: BLE001 - Tk ohne Touchpad-Unterstuetzung
            return 'break'
        pending[0] -= dy
        steps = int(pending[0] / 10)
        if steps:
            pending[0] -= steps * 10
            canvas.yview_scroll(steps, 'units')
        return 'break'

    for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
        root.bind_all(sequence, on_wheel)
    try:
        root.bind_all('<TouchpadScroll>', on_touchpad)
    except Exception:   # noqa: BLE001 - aeltere Tk-Versionen kennen das Ereignis nicht
        pass


class App(object):
    def __init__(self, tk, ttk, font, root, trace_path, dbc_paths):
        self.tk = tk
        self.root = root
        self.trace_path = trace_path
        self.dbc_paths = dbc_paths
        self.groups = build_groups()
        self.entries = [e for _name, items in self.groups for e in items]
        self.trace = None
        self.time = 0.0
        self.playing = False
        self.last_tick = 0.0
        self.collapsed = set()
        self.cells = []            # (Entry, Items)
        self.progress = [0.0]
        self.result = {}
        self.dragging = False

        base = font.nametofont('TkDefaultFont').actual('family')
        self.f_label = font.Font(family=base, size=13)
        self.f_value = font.Font(family=base, size=16)
        self.f_unit = font.Font(family=base, size=11)
        self.f_head = font.Font(family=base, size=13, weight='bold')

        root.configure(bg=BG)
        root.geometry('640x900')
        root.minsize(420, 320)
        self._build_toolbar(ttk)
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.scroll = ttk.Scrollbar(root, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.status = tk.Label(root, anchor='w', bg=BG, fg=LABEL, text='')
        self.status.pack(side='bottom', fill='x', padx=8, pady=2)
        self.scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda _e: self.layout())
        self.canvas.bind('<Button-1>', self.on_click)
        bind_scroll(root, self.canvas)
        root.bind('<space>', lambda _e: self.toggle_play())
        root.protocol('WM_DELETE_WINDOW', self.close)

        self.set_status('Lade %s ...' % os.path.basename(trace_path))
        threading.Thread(target=self._load, daemon=True).start()
        root.after(100, self._poll)

    # -- Werkzeugleiste ----------------------------------------------------

    def _build_toolbar(self, ttk):
        tk = self.tk
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(side='top', fill='x', padx=8, pady=(8, 4))
        self.play_button = ttk.Button(bar, text='Start', width=7, command=self.toggle_play,
                                      state='disabled')
        self.play_button.pack(side='left')
        self.slider_var = tk.DoubleVar(value=0.0)
        self.slider = ttk.Scale(bar, from_=0.0, to=1.0, variable=self.slider_var,
                                command=self.on_slider)
        self.slider.state(['disabled'])   # Tk 8.5: ttk.Scale kennt kein -state
        self.slider.pack(side='left', fill='x', expand=True, padx=8)
        self.time_label = tk.Label(bar, text='', bg=BG, fg=VALUE, width=22, anchor='e')
        self.time_label.pack(side='left')
        self.speed_var = tk.StringVar(value='10x')
        self.speed_box = ttk.Combobox(bar, textvariable=self.speed_var, values=SPEEDS,
                                      width=5, state='readonly')
        self.speed_box.pack(side='left', padx=(8, 0))

    def set_status(self, text):
        self.status.configure(text=text)

    # -- Laden -----------------------------------------------------------

    def _load(self):
        try:
            messages, signals = read_dbcs(self.dbc_paths)
            wanted = set()
            missing = set()
            for entry in self.entries:
                for name in entry.deps:
                    if name in signals:
                        wanted.add(name)
                    else:
                        missing.add(name)
            trace = read_trace(self.trace_path, messages, wanted,
                               lambda share: self.progress.__setitem__(0, share))
            for entry in self.entries:
                self._range(entry, trace)
            self.result = {'trace': trace, 'missing': sorted(missing)}
        except Exception as error:   # noqa: BLE001 - wird angezeigt
            self.result = {'error': '%s: %s' % (type(error).__name__, error)}

    @staticmethod
    def _range(entry, trace):
        if entry.direct and entry.direct in trace.series:
            span = trace.span(entry.direct)
            entry.low, entry.high = span if span else (None, None)
        if entry.low is not None:
            return
        values = []
        if trace.end > trace.start:
            steps = 150
            for i in range(steps + 1):
                value = entry.function(trace, trace.start + (trace.end - trace.start) * i / steps)
                if value is not None:
                    values.append(value)
        if values:
            entry.low, entry.high = min(values), max(values)

    def _poll(self):
        if not self.result:
            self.set_status('Lade %s ... %d %%' % (os.path.basename(self.trace_path),
                                                   int(self.progress[0] * 100)))
            self.root.after(100, self._poll)
            return
        if 'error' in self.result:
            self.set_status(self.result['error'])
            return
        self.trace = self.result['trace']
        self.time = self._initial_time()
        self.slider.configure(from_=self.trace.start, to=max(self.trace.end, self.trace.start + 1e-6))
        self.slider.state(['!disabled'])
        self.slider_var.set(self.time)
        self.play_button.configure(state='normal')
        self.layout()
        self.update_values()
        missing = self.result.get('missing')
        if missing:
            print('Signale nicht in der Datenbasis: ' + ', '.join(missing), file=sys.stderr)

    def _initial_time(self):
        """Erster Zeitpunkt, zu dem fast alle vorhandenen Signale einen Wert haben."""
        firsts = sorted(item[0][0] for item in self.trace.series.values())
        if not firsts:
            return self.trace.start
        return min(firsts[int(len(firsts) * 0.95)] + 0.001, self.trace.end)

    # -- Zeit --------------------------------------------------------------

    def set_time(self, value):
        if self.trace is None:
            return
        self.time = min(max(value, self.trace.start), self.trace.end)
        self.slider_var.set(self.time)
        self.update_values()

    def on_slider(self, _value):
        if self.trace is None or self.playing and not self.dragging:
            pass
        if self.trace is None:
            return
        self.time = float(self.slider_var.get())
        self.update_values()

    def toggle_play(self):
        if self.trace is None:
            return
        self.playing = not self.playing
        self.play_button.configure(text='Stop' if self.playing else 'Start')
        if self.playing:
            if self.time >= self.trace.end:
                self.time = self.trace.start
            self.last_tick = time.time()
            self._tick()

    def speed(self):
        try:
            return float(self.speed_var.get().rstrip('x').replace(',', '.'))
        except ValueError:
            return 1.0

    def _tick(self):
        if not self.playing:
            return
        now = time.time()
        self.time += (now - self.last_tick) * self.speed()
        self.last_tick = now
        if self.time >= self.trace.end:
            self.time = self.trace.end
            self.playing = False
            self.play_button.configure(text='Start')
        self.slider_var.set(self.time)
        self.update_values()
        if self.playing:
            self.root.after(50, self._tick)

    # -- Zeichnen ----------------------------------------------------------

    def layout(self):
        """Baut die Zeilen neu auf (bei Groessenaenderung und Ein-/Ausklappen)."""
        canvas = self.canvas
        canvas.delete('all')
        self.cells = []
        self.headers = []
        width = max(canvas.winfo_width(), 300)
        column = width // 2
        y = 0
        for name, items in self.groups:
            open_ = name not in self.collapsed
            canvas.create_polygon(*self._arrow(10, y + HEADER_HEIGHT / 2, open_), fill=VALUE)
            canvas.create_text(30, y + HEADER_HEIGHT / 2, anchor='w', fill=LABEL,
                               font=self.f_head, text='%s (%d)' % (name, len(items)))
            self.headers.append((y, y + HEADER_HEIGHT, name))
            y += HEADER_HEIGHT
            if not open_:
                continue
            for index, entry in enumerate(items):
                x0 = (index % 2) * column
                row_y = y + (index // 2) * ROW_HEIGHT
                self.cells.append((entry, self._make_cell(entry, x0, row_y, column)))
            y += ((len(items) + 1) // 2) * ROW_HEIGHT + 6
        canvas.configure(scrollregion=(0, 0, width, y))
        self.update_values()

    @staticmethod
    def _arrow(x, y, down):
        if down:
            return (x - 5, y - 3, x + 5, y - 3, x, y + 4)
        return (x - 3, y - 5, x - 3, y + 5, x + 4, y)

    def _make_cell(self, entry, x0, y, width):
        canvas = self.canvas
        left, right = x0 + 2, x0 + width - 3
        unit_width = 46
        value_right = right - unit_width
        canvas.create_rectangle(left, y + ROW_HEIGHT - 4, right, y + ROW_HEIGHT - 2,
                                fill=TRACK, outline='')
        bar = canvas.create_rectangle(left, y + ROW_HEIGHT - 4, left, y + ROW_HEIGHT - 2,
                                      fill=BAR, outline='')
        room = value_right - left - 64
        label = entry.label
        while len(label) > 1 and self.f_label.measure(label) > room:
            label = label[:-1]
        canvas.create_text(left, y + 13, anchor='w', fill=LABEL, font=self.f_label, text=label)
        value = canvas.create_text(value_right, y + 12, anchor='e', fill=VALUE,
                                   font=self.f_value, text='')
        canvas.create_text(right, y + 15, anchor='e', fill=LABEL, font=self.f_unit,
                           text=entry.unit)
        return {'bar': bar, 'value': value, 'left': left, 'right': right,
                'y': y + ROW_HEIGHT - 4}

    def update_values(self):
        if self.trace is None:
            return
        canvas = self.canvas
        t = self.time
        for entry, item in self.cells:
            value = entry.function(self.trace, t)
            if value is None:
                canvas.itemconfigure(item['value'], text='')
                fraction = 0.0
            else:
                canvas.itemconfigure(item['value'], text=de_number(value, entry.decimals))
                if entry.low is None or entry.high is None or entry.high <= entry.low:
                    fraction = 1.0
                else:
                    fraction = min(max((value - entry.low) / (entry.high - entry.low), 0.0), 1.0)
            canvas.coords(item['bar'], item['left'], item['y'],
                          item['left'] + (item['right'] - item['left']) * fraction, item['y'] + 2)
        self.time_label.configure(text='%s / %s' % (de_time(t - self.trace.start),
                                                    de_time(self.trace.end - self.trace.start)))
        self.set_status('%s - %d Frames, %d Signale' % (os.path.basename(self.trace_path),
                                                        self.trace.frames, len(self.trace.series)))

    # -- Eingaben ----------------------------------------------------------

    def on_click(self, event):
        y = self.canvas.canvasy(event.y)
        for top, bottom, name in self.headers:
            if top <= y < bottom:
                if name in self.collapsed:
                    self.collapsed.discard(name)
                else:
                    self.collapsed.add(name)
                self.layout()
                return

    def close(self):
        self.playing = False
        self.root.destroy()


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------

def dump(trace_path, dbc_paths, seconds):
    """Textausgabe ohne Fenster (zum Pruefen)."""
    groups = build_groups()
    entries = [e for _n, items in groups for e in items]
    messages, signals = read_dbcs(dbc_paths)
    wanted = set(n for e in entries for n in e.deps if n in signals)
    trace = read_trace(trace_path, messages, wanted)
    t = trace.start + seconds
    print('Frames: %d, Dauer: %.1f s, Signale mit Daten: %d' %
          (trace.frames, trace.end - trace.start, len(trace.series)))
    for name, items in groups:
        print('\n== %s' % name)
        for entry in items:
            value = entry.function(trace, t)
            text = '-' if value is None else de_number(value, entry.decimals)
            print('  %-26s %12s %s' % (entry.label, text, entry.unit))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Scan My Tesla - Anzeige fuer einen Tesla-CAN-Trace')
    parser.add_argument('trace', nargs='?', help='ASC-Trace (Fahrzeug-CAN eines Tesla Model 3)')
    parser.add_argument('-d', '--dbc', action='append', help='Datenbasis (DBC), mehrfach moeglich; '
                        'Standard: die vom CANtrix uebergebene')
    parser.add_argument('--dump', type=float, metavar='SEKUNDEN',
                        help='Werte zu diesem Zeitpunkt als Text ausgeben (ohne Fenster)')
    args = parser.parse_args(argv)

    trace_path = args.trace
    dbc_paths = database_paths(args.dbc)
    if args.dump is not None:
        if not trace_path:
            parser.error('--dump braucht einen Trace')
        if not dbc_paths:
            parser.error('keine Datenbasis (-d oder %s)' % DATABASE_VARIABLE)
        dump(trace_path, dbc_paths, args.dump)
        return 0

    import tkinter as tk
    from tkinter import ttk, filedialog, font, messagebox
    root = tk.Tk()
    root.title('Scan My Tesla')
    if not trace_path:
        trace_path = filedialog.askopenfilename(
            parent=root, title='Tesla CAN Trace oeffnen',
            filetypes=[('Vector ASC', '*.asc'), ('Alle Dateien', '*.*')])
        if not trace_path:
            root.destroy()
            return 1
    if not os.path.isfile(trace_path):
        messagebox.showerror('Scan My Tesla', 'Trace nicht gefunden:\n%s' % trace_path)
        return 1
    dbc_paths = [p for p in dbc_paths if p.lower().endswith('.dbc')]
    missing = [p for p in dbc_paths if not os.path.isfile(p)]
    if not dbc_paths or missing:
        messagebox.showerror('Scan My Tesla', 'Es wird eine geladene DBC-Datenbasis gebraucht.\n'
                             'Bitte im CANtrix eine DBC (z. B. Model3CAN.dbc) laden.' +
                             ('\n\nNicht gefunden:\n' + '\n'.join(missing) if missing else ''))
        return 1
    root.title('Scan My Tesla - %s' % os.path.basename(trace_path))
    App(tk, ttk, font, root, trace_path, dbc_paths)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
