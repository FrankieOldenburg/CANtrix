"""Recording with a sigrok-pico (Raspberry Pi Pico with the firmware of github.com/pico-coder/sigrok-pico).

The Pico is a USB serial port. This module speaks its protocol directly (as the libsigrok driver
"raspberrypi-pico" does) and writes a Sigrok file (`.sr`), so neither sigrok-cli nor PulseView is needed.
Needs pyserial (imported only when it is used), otherwise only the standard library (Python 3.6).

Up to 21 digital channels (D2…D22) and 3 analog channels (A0…A2). Recording is continuous. The firmware has two
formats for the data. With at most the digital channels D2…D5 and no analog channel:
    0x80…0xFF  bits 6:4 = repetitions of the previous value, bits 3:0 = the next sample
    0x30…0x7F  the previous value (byte - 47) * 8 times
otherwise one "slice" per sample: first the digital channels, 7 per byte (0x80 set), then one byte per analog
channel (0x80 | 7 bit value), and between the slices
    0x30…0x4F  the previous slice (byte - 47) times
    0x50…0x7F  the previous slice (byte - 78) * 32 times
In both formats:
    "!"        the Pico could not send fast enough and aborted
    "$n+"      end of the recording, n = number of data bytes sent
"""
import re
import sys
import threading
import time
import zipfile
from array import array

from CAN_Language import tr

PICO_VENDOR_ID = 0x2E8A  # Raspberry Pi
DIGITAL = ['D%d' % number for number in range(2, 23)]  # named like in PulseView: D2 = GP2 = digital channel 0
ANALOG = ['A0', 'A1', 'A2']  # GP26 … GP28
ANALOG_STEP = 0.0257  # volts per step of the 7 bit values (3.3 V / 128)
CLOCK = 120000000  # the sample rate is derived from it
MIN_RATE = 5000
MAX_SAMPLES = 200000000
MAX_BYTES = 500000000  # in memory: one byte per sample and digital channel, four per analog channel
MAX_RAW_BYTES = 100000000


def automatic_rate(bitrate):
    """Ten samples per bit, raised to the next rate that divides the clock of the Pico evenly or nearly."""
    target = max(MIN_RATE, 10 * bitrate)
    return CLOCK // max(1, CLOCK // target)


def analog_limit(count):
    """Highest sample rate with this number of analog channels: the ADC converts below 500 kHz in total, and the
    rate should divide its 48 MHz clock and 24 MHz evenly."""
    return {1: 400000, 2: 240000, 3: 160000}[count]


def _serial_module():
    try:
        import serial
        import serial.tools.list_ports
    except ImportError:
        raise RuntimeError(tr('Für den sigrok-pico fehlt pyserial (Installation: pip3 install --user pyserial).'))
    return serial


def list_ports():
    """Serial ports that belong to a Raspberry Pi Pico; [] without pyserial."""
    try:
        serial = _serial_module()
    except RuntimeError:
        return []
    return sorted(port.device for port in serial.tools.list_ports.comports() if port.vid == PICO_VENDOR_ID)


# ---------------------------------------------------------------- protocol

def _drain(port, quiet=0.1, limit=2.0):
    """Discard what the device still sends."""
    end = time.time() + limit
    last = time.time()
    while time.time() < end and time.time() - last < quiet:
        if port.in_waiting:
            port.read(port.in_waiting)
            last = time.time()
        else:
            time.sleep(0.005)


def _response(port, command, timeout=1.0):
    """Send a command line and return its answer (the bytes that arrive until a short pause)."""
    port.write(command.encode('ascii') + b'\n')
    answer = b''
    end = time.time() + timeout
    last = None
    while time.time() < end:
        if port.in_waiting:
            answer += port.read(port.in_waiting)
            last = time.time()
        elif last is not None and time.time() - last > 0.02:
            break
        else:
            time.sleep(0.002)
    return answer


def _acknowledged(port, command):
    if not _response(port, command).startswith(b'*'):
        raise RuntimeError(tr('Der sigrok-pico hat das Kommando "%s" nicht angenommen.') % command)


def identify(port):
    """(analog channels, digital channels) of the sigrok-pico on this port; RuntimeError if it is none."""
    for _ in range(2):
        port.write(b'*')  # reset
        time.sleep(0.01)
        _drain(port)
        match = re.match(rb'SRPICO,A(\d\d)\dD(\d\d),02', _response(port, 'i'))
        if match:
            return int(match.group(1)), int(match.group(2))
    raise RuntimeError(tr('Auf diesem Anschluss antwortet kein sigrok-pico.'))


def open_port(name=None):
    """Opens the given port, without a name the first Pico that identifies itself; returns (port, identity)."""
    serial = _serial_module()
    names = [name] if name else list_ports()
    if not names:
        raise RuntimeError(tr('Kein Raspberry Pi Pico gefunden.'))
    error = None
    for candidate in names:
        port = None
        try:
            port = serial.Serial(candidate, 115200, timeout=0.1, write_timeout=1.0)
            return port, identify(port)
        except (OSError, ValueError, RuntimeError) as failure:  # serial.SerialException is an OSError
            error = failure
            if port is not None:
                port.close()
    raise RuntimeError('%s: %s' % (names[-1], error))


def configure(port, identity, digital, analog, rate):
    """Enable the channels (numbers from 0) and set the sample rate. The firmware needs contiguous digital
    channels, so all of them up to the highest one wanted are enabled."""
    analog_count, digital_count = identity
    if (digital and max(digital) >= digital_count) or (analog and max(analog) >= analog_count):
        raise RuntimeError(tr('Die Firmware dieses sigrok-pico hat nur %d digitale und %d analoge Kanäle.')
                           % (digital_count, analog_count))
    for number in range(analog_count):
        _acknowledged(port, 'A%d%d' % (number in analog, number))
    for number in range(digital_count):
        _acknowledged(port, 'D%d%d' % (bool(digital) and number <= max(digital), number))
    _acknowledged(port, 'L1000')  # sample limit, without meaning in continuous mode but expected
    _acknowledged(port, 'R%d' % rate)


def expand(data, limit=MAX_SAMPLES):
    """The format for D2…D5 only: returns (one byte per sample with the 4 channel bits, truncated)."""
    runs = [[bytes((value,)) * ((code - 47) * 8) for value in range(16)] for code in range(48, 128)]
    short = [[bytes((value,)) * count for value in range(16)] for count in range(8)]
    pieces, total, last = [], 0, 0
    for code in data:
        if code >= 0x80:
            repeat = (code >> 4) & 7
            if repeat:
                pieces.append(short[repeat][last])
            last = code & 15
            pieces.append(short[1][last])
            total += repeat + 1
        elif code >= 0x30:
            piece = runs[code - 48][last]
            pieces.append(piece)
            total += len(piece)
        if total >= limit:
            return b''.join(pieces)[:limit], True
    return b''.join(pieces), False


def expand_slices(data, width, limit=MAX_SAMPLES):
    """The format with one slice of `width` bytes per sample: returns (all slices joined, truncated)."""
    pieces, total, previous, position, size = [], 0, b'', 0, len(data)
    truncated = False
    while position < size:
        code = data[position]
        if code >= 0x80:
            if position + width > size:
                break  # incomplete at the end
            previous = data[position:position + width]
            pieces.append(previous)
            total += 1
            position += width
        else:
            if code >= 0x30 and previous:
                count = code - 47 if code <= 79 else (code - 78) * 32
                pieces.append(previous * count)
                total += count
            position += 1
        if total >= limit:
            truncated = True
            break
    return b''.join(pieces)[:limit * width], truncated


def channel_data(stream, digital, analog):
    """Splits the data stream into the wanted channels (numbers from 0): returns (list of levels (bytes 0/1) per
    digital channel, list of voltages (array 'f') per analog channel, truncated)."""
    limit = min(MAX_SAMPLES, MAX_BYTES // max(1, len(digital) + 4 * len(analog)))
    if not analog and digital and max(digital) < 4:
        samples, truncated = expand(stream, limit)
        return [samples.translate(_bit_table(number)) for number in digital], [], truncated
    digital_bytes = (max(digital) + 7) // 7 if digital else 0
    # the analog bytes follow in channel order
    width = digital_bytes + len(analog)
    slices, truncated = expand_slices(stream, width, limit)
    levels = [slices[number // 7::width].translate(_bit_table(number % 7)) for number in digital]
    volts = [array('f', map([(code & 127) * ANALOG_STEP for code in range(256)].__getitem__,
                            slices[digital_bytes + position::width])) for position in range(len(analog))]
    return levels, volts, truncated


def _bit_table(bit):
    return bytes((value >> bit) & 1 for value in range(256))


def pack_channels(channels):
    """Levels (bytes 0/1) of several channels packed like in Sigrok files: (unit size, data)."""
    unit = (len(channels) + 7) // 8
    size = len(channels[0])
    out = bytearray(unit * size)
    for byte in range(unit):
        word = 0
        for bit, levels in enumerate(channels[8 * byte:8 * byte + 8]):
            word |= int.from_bytes(levels, 'little') << bit  # every sample stays within its byte
        out[byte::unit] = word.to_bytes(size, 'little')
    return unit, bytes(out)


def write_sigrok(path, rate, digital, analog=()):
    """Sigrok session file. digital: [(name, levels as bytes 0/1)], analog: [(name, array 'f' of volts)]."""
    metadata = ['[global]', 'sigrok version=0.5.2', '', '[device 1]', 'samplerate=%d Hz' % rate,
                'total probes=%d' % len(digital), 'total analog=%d' % len(analog)]
    if digital:
        unit, packed = pack_channels([levels for _, levels in digital])
        metadata += ['capturefile=logic-1', 'unitsize=%d' % unit]
        metadata += ['probe%d=%s' % (number + 1, name) for number, (name, _) in enumerate(digital)]
    first = len(digital) + 1  # analog channels are numbered behind the digital ones
    metadata += ['analog%d=%s' % (first + number, name) for number, (name, _) in enumerate(analog)]
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('version', '2')
        archive.writestr('metadata', '\n'.join(metadata) + '\n')
        if digital:
            archive.writestr('logic-1-1', packed)
        for number, (_, volts) in enumerate(analog):
            if sys.byteorder != 'little':
                volts = array('f', volts)
                volts.byteswap()
            archive.writestr('analog-1-%d-1' % (first + number), volts.tobytes())


# ---------------------------------------------------------------- recording

class PicoRecording(object):
    """One recording: the constructor starts it, stop() ends it and writes the file."""

    def __init__(self, port_name, channels, rate):
        """channels: names out of DIGITAL and ANALOG."""
        self.digital = [number for number, name in enumerate(DIGITAL) if name in channels]
        self.analog = [number for number, name in enumerate(ANALOG) if name in channels]
        if not self.digital and not self.analog:
            raise RuntimeError(tr('Es ist kein Kanal gewählt.'))
        if not MIN_RATE <= rate <= CLOCK:
            raise RuntimeError(tr('Der sigrok-pico tastet mit 5 kHz bis 120 MHz ab.'))
        if self.analog and rate > analog_limit(len(self.analog)):
            raise RuntimeError(tr('Mit %d analogen Kanälen sind höchstens %d kHz möglich.')
                               % (len(self.analog), analog_limit(len(self.analog)) // 1000))
        self.rate = rate - rate % 2  # the firmware rounds down to an even number
        self.data = bytearray()
        self.overflow = self.full = False
        self.error = None
        self._stop = threading.Event()
        self.port, identity = open_port(port_name)
        try:
            configure(self.port, identity, self.digital, self.analog, self.rate)
            _drain(self.port, quiet=0.02)
            self.port.write(b'C\n')  # continuous
        except Exception:
            self.port.close()
            raise
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def finished(self):
        """True when the recording ended by itself (overflow, device removed, too much data)."""
        return not self._thread.is_alive()

    def _read(self):
        port, data = self.port, self.data
        stop_sent = None
        try:
            while True:
                if self._stop.is_set() and stop_sent is None:
                    port.write(b'+')
                    stop_sent = time.time()
                chunk = port.read(max(1, port.in_waiting))
                if chunk:
                    start = len(data)
                    data.extend(chunk)
                    if data.find(b'!', start) >= 0:
                        self.overflow = True
                        return
                    if data.find(b'$', 0 if stop_sent else start) >= 0 and data.endswith(b'+'):
                        return
                    if len(data) > MAX_RAW_BYTES:
                        self.full = True
                        return
                if stop_sent is not None and time.time() - stop_sent > 3.0:
                    return  # no end marker: use what has arrived
        except OSError as error:
            self.error = str(error)

    def stop(self, path):
        """Ends the recording and writes it to path; returns a list of notes. RuntimeError without samples."""
        self._stop.set()
        self._thread.join(8.0)
        try:
            if self.overflow:
                time.sleep(2.2)  # the firmware waits before it sends its end marker
            if self.error is None:
                self.port.write(b'+')
                self.port.write(b'*')
                _drain(self.port)
        except OSError:
            pass
        finally:
            self.port.close()
        data = bytes(self.data)
        end = re.search(rb'[!$]', data)
        sent = re.search(rb'\$(\d+)\+', data)
        stream = data[:end.start()] if end else data
        levels, volts, truncated = channel_data(stream, self.digital, self.analog)
        if not (levels + volts)[0]:
            raise RuntimeError(self.error or tr('Keine Abtastwerte empfangen (Aufzeichnung zu kurz oder '
                                                'Abtastrate zu hoch).'))
        write_sigrok(path, self.rate, [(DIGITAL[number], data) for number, data in zip(self.digital, levels)],
                     [(ANALOG[number], data) for number, data in zip(self.analog, volts)])
        notes = []
        if self.overflow:
            notes.append(tr('Überlauf: Der sigrok-pico konnte die Daten nicht schnell genug senden. Die Aufzeichnung '
                            'endet an dieser Stelle; mit einer niedrigeren Abtastrate dauert sie länger.'))
        elif sent and int(sent.group(1)) != len(stream):
            notes.append(tr('Bei der Übertragung sind Daten verloren gegangen.'))
        if self.error:
            notes.append(self.error)
        if truncated or self.full:
            notes.append(tr('Die Aufzeichnung wurde gekürzt (zu viele Daten).'))
        return notes
