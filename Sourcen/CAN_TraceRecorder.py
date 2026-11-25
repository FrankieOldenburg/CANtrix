"""CAN-Aufzeichnung mit Vector, USBtingo oder einem seriellen OBDLink/ELM327-Adapter.

Voraussetzungen: python-can (Vector-Backend), Vector XL Driver Library (vxlapi64.dll), tkinter.
Start: nur über den Trace Viewer (CANtrix.py).
"""
import queue
import re
import threading
import time
from tkinter import messagebox

import can

from CAN_Language import set_text, tr
from CAN_RecorderSettings import normalize_connection

MAX_MESSAGES_PER_TICK = 2000


OBD_TIMEOUT = 1.0
OBD_LINE_END = re.compile(rb'[\r\n>]')
OBD_HEX_LINE = re.compile(r'[0-9a-fA-F \t]+')


def _obd_frame(line, stamp):
    """Decode an ATH1/ATD1 frame. Standard headers have 3 hex digits, extended headers 8."""
    if not OBD_HEX_LINE.fullmatch(line):
        return None
    digits = ''.join(line.split())
    width = 8 if len(digits) % 2 else 3  # identifier + one DLC digit + whole bytes
    if len(digits) <= width:
        return None
    identifier, dlc = int(digits[:width], 16), int(digits[width], 16)
    data = digits[width + 1:]
    extended = width == 8
    if identifier > (0x1FFFFFFF if extended else 0x7FF) or dlc > 8 or len(data) != dlc * 2:
        return None
    return can.Message(timestamp=stamp, arbitration_id=identifier, is_extended_id=extended,
                       channel=0, data=bytes.fromhex(data))


class OBDSerialBus:
    """A prompt-based ELM/STN connection, shared by the recorder and the generator."""

    def __init__(self, port, serial_baud, bitrate):
        if not port:
            raise ValueError(tr('Bitte einen seriellen Port für OBDLink / ELM327 auswählen.'))
        try:
            import serial
        except ImportError:
            raise RuntimeError(tr('OBDLink / ELM327 benötigt pyserial.'))
        self._serial = serial.Serial(port, baudrate=serial_baud, timeout=0.025, write_timeout=0.5)
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._monitor = False
        self._monitor_status = ''
        self._closed = False
        self.bitrate = bitrate
        try:
            # Interrupt any previous monitor without sending a blank command (which repeats the last request).
            self._write(' ')
            for _ in self._response(time.monotonic() + 0.15, require_prompt=False):
                pass
            self._buffer.clear()
            self._serial.reset_input_buffer()
            self._command('ATZ', timeout=2.5)
            self._command('ATE0', expect='OK')
            self.identity = ' '.join(self._command('ATI'))
            if not self.identity:
                raise OSError(tr('Ungültige Antwort vom OBD-Adapter.'))
            identity = ' '.join(self._command('STI', allow_rejection=True))
            self.stn = bool(re.match(r'STN\d|OBDLINK', identity, re.I))
            if self.stn:
                self.identity = identity
            for command in ('ATL0', 'ATS1', 'ATH1', 'ATD1', 'ATCAF0', 'ATCFC0', 'ATR0', 'ATM0'):
                self._command(command, expect='OK')
            self._command('STCMM 0' if self.stn else 'ATCSM1', expect='OK')
            self._select_protocol(False)
        except Exception:
            self._serial.close()
            raise

    def _write(self, text):
        data = text.encode('ascii')
        if self._serial.write(data) != len(data):
            raise OSError(tr('Serieller Schreibvorgang unvollständig.'))

    def _token(self, deadline):
        """Return one complete line or prompt, retaining fragments across reads."""
        while True:
            end = OBD_LINE_END.search(self._buffer)
            if end:
                index = end.start()
                if self._buffer[index] == ord('>') and index:
                    data = self._buffer[:index]
                    del self._buffer[:index]
                else:
                    data = self._buffer[:index + 1]
                    del self._buffer[:index + 1]
                token = data.decode('ascii', errors='replace').strip()
                if token:
                    return token
                continue
            if time.monotonic() >= deadline:
                return None
            self._buffer.extend(self._serial.read(min(self._serial.in_waiting or 1, 4096)))
            if len(self._buffer) > 8192:
                raise OSError(tr('Ungültige Antwort vom OBD-Adapter.'))

    def _response(self, deadline, require_prompt=True):
        while True:
            token = self._token(deadline)
            if token == '>':
                return
            if token is None:
                if require_prompt:
                    self._closed = True  # an unknown adapter state must not receive more requests
                    self._serial.close()
                    raise TimeoutError(tr('Keine Antwort vom OBD-Adapter.'))
                return
            yield token

    def _command(self, command, expect=None, timeout=OBD_TIMEOUT, allow_rejection=False):
        self._write(command + '\r')
        lines = [line for line in self._response(time.monotonic() + timeout)
                 if line.replace(' ', '').upper() != command.replace(' ', '').upper()]
        if allow_rejection and lines == ['?']:
            return []
        if expect is not None:
            accepted = lines == [expect]
        else:
            accepted = all(line == 'OK' or line == 'NO DATA'
                           or command in ('ATZ', 'ATI', 'STI') and line != '?' for line in lines)
        if not accepted:
            raise OSError(tr('OBD-Adapter meldet bei %s: %s') % (command, ', '.join(lines) or '-'))
        return lines

    def _select_protocol(self, extended):
        if self.stn:
            self._command('STP %d' % (32 if extended else 31), expect='OK')
            self._command('STPBR %d' % self.bitrate, expect='OK')
            self._command('STPBRR', expect=str(self.bitrate))
            self._command('ATCRA', expect='OK')
            self._command('STFAC', expect='OK')
            self._command('STFPA 00,00', expect='OK')
        else:
            divisor, remainder = divmod(500000, self.bitrate)
            option = 0x60 if extended else 0xE0  # raw CAN, variable DLC, both receive ID widths
            user_protocol = not remainder and 1 <= divisor <= 64 and self._command(
                'ATPB%02X%02X' % (option, divisor), expect='OK', allow_rejection=True)
            if user_protocol:
                self._command('ATTPB', expect='OK')
            else:
                protocols = {250000: ('8', '9'), 500000: ('6', '7')}
                if self.bitrate not in protocols:
                    raise ValueError(tr('ELM327 ohne USER1-Unterstützung benötigt 250 oder 500 kbit/s.'))
                self._command('ATTP' + protocols[self.bitrate][extended], expect='OK')
                self._command('ATV1', expect='OK')
            self._command('ATAR', expect='OK')
        self._extended = extended

    def recv(self, timeout=0.1):
        with self._lock:
            if self._closed:
                raise OSError(tr('Die serielle OBD-Verbindung ist geschlossen.'))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self._monitor:
                    self._write(('STM' if self.stn else 'ATMA') + '\r')
                    self._monitor = True
                    self._monitor_status = ''
                token = self._token(deadline)
                if token is None:
                    return None
                if token == '>':
                    self._monitor = False
                    if self._monitor_status != 'BUFFER FULL':
                        raise OSError(tr('OBD-Monitor beendet: %s') % (self._monitor_status or '>'))
                    continue
                frame = _obd_frame(token, time.time())
                if frame is not None:
                    return frame
                self._monitor_status = token
        return None

    def _stop_monitor(self):
        if self._monitor:
            self._write(' ')
            for _ in self._response(time.monotonic() + OBD_TIMEOUT):
                pass
            self._monitor = False

    def send(self, message):
        data = bytes(message.data)
        if message.is_fd or message.is_error_frame or message.is_remote_frame or len(data) > 8:
            raise ValueError(tr('OBDLink / ELM327 unterstützt hier nur klassische CAN-Datenframes.'))
        if not data and not self.stn:
            raise ValueError(tr('ELM327 kann hier keine CAN-Frames ohne Nutzdaten senden.'))
        extended = bool(message.is_extended_id)
        identifier = message.arbitration_id
        if not 0 <= identifier <= (0x1FFFFFFF if extended else 0x7FF):
            raise ValueError(tr('Ungültige ID (hexadezimal)'))
        header = ('%08X' if extended else '%03X') % identifier
        with self._lock:
            if self._closed:
                raise OSError(tr('Die serielle OBD-Verbindung ist geschlossen.'))
            self._stop_monitor()
            if extended != self._extended:
                try:
                    self._select_protocol(extended)
                except Exception:
                    self._closed = True
                    self._serial.close()
                    raise
            payload = data.hex().upper()
            if self.stn:
                self._command('STPX h:%s,d:%s' % (header, payload))
            else:
                self._command('ATSH' + header, expect='OK')
                self._command(payload)

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            try:
                self._stop_monitor()
            except OSError:
                pass
            finally:
                if not self._closed:
                    self._closed = True
                    self._serial.close()


class CanReader(threading.Thread):
    """Liest CAN-Botschaften im Hintergrund und reicht sie über eine Queue an die GUI weiter."""

    def __init__(self, connection, out_queue):
        super().__init__(daemon=True)
        self.connection = normalize_connection(connection)
        self.fd = self.connection['fd']
        self.out_queue = out_queue
        self.bus = None  # open connection while running (used for sending)
        self._stop_event = threading.Event()

    def run(self):
        connection = self.connection
        try:
            bus = open_bus(connection)
        except Exception as exc:
            self.out_queue.put(("error", ("Verbindung fehlgeschlagen: %s", exc)))
            return

        self.bus = bus
        serial = connection['serial']
        bitrate = connection['bitrate']
        device = tr("Gerät %s, ") % serial if serial is not None else ""
        mode = tr("CAN FD (%s / %s Bit/s)") % (bitrate, connection['data_bitrate']) if self.fd \
            else tr("CAN %s Bit/s") % bitrate
        if connection['interface'] == "obdlink":
            self.out_queue.put(("status", ("Verbunden: %s · %s · Rechner-Zeitstempel",
                                           bus.identity, connection['port'])))
        elif connection['interface'] == "usbtingo":
            self.out_queue.put(("status", ("Verbunden: %sUSBtingo, %s", device, mode)))
        else:
            self.out_queue.put(("status", ("Verbunden: %sKanal %s, %s", device, connection['channel'], mode)))
        try:
            while not self._stop_event.is_set():
                msg = bus.recv(timeout=0.1)
                if msg is not None:
                    self.out_queue.put(("msg", msg))
        except Exception as exc:
            self.out_queue.put(("error", ("Empfang fehlgeschlagen: %s", exc)))
        finally:
            self.bus = None
            bus.shutdown()
            self.out_queue.put(("status", ("Getrennt",)))

    def stop(self):
        self._stop_event.set()


def _register_usbtingo():
    # PyInstaller builds can contain the module without the plugin entry-point metadata.
    import usbtingobus

    if "usbtingo" not in can.interfaces.BACKENDS:
        can.interfaces.BACKENDS["usbtingo"] = (usbtingobus.__name__, "USBtingoBus")
        interfaces = frozenset(can.interfaces.BACKENDS)
        can.interfaces.VALID_INTERFACES = interfaces
        can.util.VALID_INTERFACES = interfaces


def open_bus(connection):
    """Open the selected interface with the settings used by both recording and connection tests."""
    connection = normalize_connection(connection)
    if connection["interface"] == "obdlink":
        return OBDSerialBus(connection["port"], connection["serial_baud"], connection["bitrate"])
    options = {key: connection[key] for key in ("bitrate", "fd", "data_bitrate")}
    if connection["interface"] == "usbtingo":
        _register_usbtingo()
        return can.Bus(interface="usbtingo", channel=connection["serial"], **options)

    if connection["fd"]:
        timing = connection["timing"]
        if int(can.__version__.split(".")[0]) < 4:
            # python-can 3.x uses camelCase Vector timing arguments.
            timing = {key[:-4] + key[-3:].title(): value for key, value in timing.items()}
        options.update(timing)
    return can.Bus(interface="vector", channel=connection["channel"], serial=connection["serial"],
                   app_name=connection["app_name"], **options)


def detect_vector_devices():
    """Sucht angeschlossene Vector-Geräte (jedes Modell, das der XL Driver kennt). Gibt eine Liste von
    Wörterbüchern (serial, channel = Kanal am Gerät ab 0, name, fd) zurück; blockiert kurz, daher im
    Hintergrund aufrufen. Wirft eine Ausnahme, wenn der Treiber fehlt."""
    devices = []
    for config in can.detect_available_configs("vector"):
        serial = config.get("serial", config.get("serial_number"))
        channel = config.get("hw_channel", config.get("channel"))
        info = config.get("vector_channel_config")
        name = getattr(info, "name", None) or ""
        if not name:
            hw_type = config.get("hw_type")
            name = str(getattr(hw_type, "name", hw_type) or "Vector").replace("XL_HWTYPE_", "")
        devices.append({"serial": serial, "channel": channel, "name": name, "fd": bool(config.get("supports_fd"))})
    return devices


def device_text(device):
    """Anzeigetext eines Geräts aus detect_vector_devices."""
    return "%s · %s · %s%s" % (device["name"], device["serial"], tr("Kanal %s") % (device["channel"] + 1 if isinstance(device["channel"], int) else device["channel"]),
                               " (CAN FD)" if device["fd"] else "")


def test_connection(connection, bus=None):
    """Prüft die Verbindung. Gibt (ok, Text) zurück (Text auf Deutsch, durch tr() übersetzt); blockiert kurz,
    daher im Hintergrund aufrufen. bus: offene Verbindung einer laufenden Aufzeichnung (dann wird sie nicht
    noch einmal geöffnet, nur ihr Zustand gemeldet)."""
    if connection.get("interface") == "obdlink":
        try:
            if bus is not None:
                return True, tr("Verbunden: %s · %s · Rechner-Zeitstempel") % (bus.identity, connection["port"])
            test = open_bus(connection)
            try:
                test.recv(timeout=0.2)
                return True, tr("Verbunden: %s · %s · Rechner-Zeitstempel") % (test.identity, connection["port"])
            finally:
                test.shutdown()
        except Exception as error:
            return False, tr("Verbindung fehlgeschlagen: %s") % error
    usbtingo = connection.get("interface") == "usbtingo"
    lines = []
    try:
        if usbtingo:
            _register_usbtingo()
        configs = can.detect_available_configs("usbtingo" if usbtingo else "vector")
    except Exception as exc:
        if usbtingo:
            return False, tr("USBtingo-Treiber nicht verfügbar: %s") % exc
        return False, tr("Vector-Treiber nicht verfügbar: %s") % exc
    found = False
    for config in configs:
        if usbtingo:
            serial = config.get("serial") or config.get("serial_number") or config.get("channel")
            lines.append(tr("Gerät %s") % serial)
            if connection["serial"] in (None, "") or str(connection["serial"]) == str(serial):
                found = True
            continue
        serial = config.get("serial", config.get("serial_number"))
        channel = config.get("hw_channel", config.get("channel"))
        lines.append(tr("Gerät %s, Kanal %s%s") % (serial, channel, " (CAN FD)" if config.get("supports_fd") else ""))
        if channel == connection["channel"] and connection["serial"] in (None, serial):
            found = True
    if not configs:
        return False, tr("Kein USBtingo gefunden.") if usbtingo else tr("Kein Vector-Gerät gefunden.")
    devices = "\n".join(lines)
    if not found and usbtingo:
        return False, tr("USBtingo mit Seriennummer %s nicht gefunden.\nGefunden:\n%s") % (connection["serial"], devices)
    if not found:
        return False, tr("Gerät oder Kanal nicht gefunden (Seriennummer %s, Kanal %s).\nGefunden:\n%s") % (
            connection["serial"] if connection["serial"] is not None else "-", connection["channel"], devices)
    if bus is not None:
        state = getattr(bus, "state", None)
        return True, tr("Verbunden, Zustand: %s\nGefunden:\n%s") % (getattr(state, "name", state), devices)
    try:
        test = open_bus(connection)
    except Exception as exc:
        return False, tr("Verbindung fehlgeschlagen: %s") % exc
    try:
        state = getattr(test, "state", None)
    finally:
        test.shutdown()
    return True, tr("Verbindung in Ordnung, Zustand: %s\nGefunden:\n%s") % (getattr(state, "name", state), devices)


class Recorder(object):
    """Aufzeichnung ohne eigene Anzeige: liest im Hintergrund und gibt die Nachrichten im GUI-Thread weiter.
    status_var: Statuszeile des Fensters. on_data(messages) bekommt alle Nachrichten eines Abfrage-Takts (auch
    Error-Frames), on_live(messages, reset=False) nur die ohne Fehler (Panel Viewer), on_running(bool) meldet
    Start und Ende."""

    def __init__(self, root, status_var, on_data=None):
        self.root = root
        self.status_var = status_var
        # Connection settings (channel, bitrate, serial number, data rate, mode); the analyser sets them
        # from "Einstellungen > CAN Trace Recorder". serial None = any device.
        self.connection = normalize_connection()
        self.on_data = on_data
        self.on_live = None
        self.on_running = None
        self.on_status = None  # callback(text): connection status (Verbinde, Verbunden, Getrennt, error)
        self.queue = queue.Queue()
        self.reader = None
        self._poll_job = self.root.after(50, self.process_queue)

    def running(self):
        return self.reader is not None

    def start(self):
        if self.reader is not None:
            return
        connection = self.connection
        if self.on_live is not None:
            self.on_live([], True)
        self.reader = CanReader(connection, self.queue)
        self.reader.start()
        self._report(True)
        self._status("Verbinde ...")

    def stop(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader.join(timeout=2)
            self.reader = None
        self._report(False)

    def send(self, identifier, data, extended=False):
        """Send a message over the connection of the recording; raises RuntimeError with a text on failure."""
        reader = self.reader
        bus = reader.bus if reader is not None else None
        if bus is None:
            raise RuntimeError(tr("Nicht verbunden: Aufzeichnung (Datenquelle Live) starten."))
        fd = len(data) > 8
        if fd and not reader.fd:
            raise RuntimeError(tr("Mehr als 8 Datenbytes brauchen CAN FD (Einstellungen > CAN Trace Recorder)."))
        message = can.Message(arbitration_id=identifier, data=bytes(data), is_extended_id=extended,
                              is_fd=fd, bitrate_switch=fd)
        try:
            bus.send(message)
        except Exception as error:
            raise RuntimeError(tr("Senden fehlgeschlagen: %s") % error)

    def _status(self, *payload):
        set_text(self.status_var, *payload)
        if self.on_status is not None:
            self.on_status(self.status_var.get())

    def _report(self, running):
        if self.on_running is not None:
            self.on_running(running)

    def process_queue(self):
        received = []
        for _ in range(MAX_MESSAGES_PER_TICK):
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "msg":
                received.append(payload)
            elif kind == "status":  # payload: (German template, *args)
                self._status(*payload)
            elif kind == "error":
                self._status(*payload)
                self.stop()
                messagebox.showerror("CAN Trace Recorder", self.status_var.get(), parent=self.root)
        if received:
            if self.on_data is not None:
                self.on_data(received)
            if self.on_live is not None:
                frames = [message for message in received if not message.is_error_frame]
                if frames:
                    self.on_live(frames)
        self._poll_job = self.root.after(50, self.process_queue)

    def close(self):
        """Stoppt die Aufzeichnung und den Abfrage-Takt."""
        self.stop()
        try:
            self.root.after_cancel(self._poll_job)
        except Exception:
            pass
