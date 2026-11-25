#!/usr/bin/env python3
"""Leser für CAN-Trace-Formate (außer Vector ASC, das liest parse_trace in CAN_TraceViewer.py).

Textformate: PEAK PCAN-View (.trc), BUSMASTER (.log), Linux candump (.log), SLCAN/Lawicel (.txt) und
CSV (.csv, auch SavvyCAN). Binärformate: Vector BLF (.blf), ASAM MDF4 (.mf4), Vehicle Spy VSB (.vsb), python-can SQLite (.db) und PCAP/PCAPNG mit SocketCAN-Frames.
Erkannt wird am Inhalt, nicht an der Dateiendung.

Jeder Leser liefert eine Rows-Liste mit Tupeln (zeit_s, kanal, richtung, id, daten, zeile) und dem
Attribut start (Messbeginn als naive Ortszeit, datetime, oder None): zeit_s relativ zum Messbeginn,
kanal als Text ('1', 'can0'), richtung 'Rx'/'Tx', id ohne Extended-Flag, daten als bytes, zeile =
Zeilennummer (Text) bzw. laufende Nummer (binär). Remote-, Fehler- und leere Frames entfallen.

Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import base64
import binascii
import csv
import datetime
import re
import struct
import zlib

from CAN_Language import tr

# Data length of CAN FD frames by DLC code.
FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
EPOCH_LIMIT = 1e8  # times above this are Unix times and become relative to the first frame


def dlc_length(dlc, fd=True):
    if dlc <= 8:
        return dlc
    return FD_LENGTHS[min(dlc, 15)] if fd else 8


class Rows(list):
    """Rows of a trace; start: beginning of the measurement (naive local datetime) or None. errors: the
    CAN error frames as (time, channel, text), sorted by time (only ASC, BLF, candump and PCAP/PCAPNG
    have them; they are no rows)."""
    start = None

    def __init__(self, *arguments):
        super().__init__(*arguments)
        self.errors = []
        self.environment = {}  # signals without CAN frames (MDF4 signal logging): {name: [(time, text)]}


# Error classes of a SocketCAN error frame (bits of the CAN ID, see linux/can/error.h)
SOCKETCAN_ERRORS = ((0x1, 'TX timeout'), (0x2, 'Arbitration lost'), (0x4, 'Controller problem'),
                    (0x8, 'Protocol violation'), (0x10, 'Transceiver status'), (0x20, 'No ACK'), (0x40, 'Bus off'),
                    (0x80, 'Bus error'), (0x100, 'Controller restarted'))


CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG = 0x80000000, 0x40000000, 0x20000000


def socketcan_error_text(can_id):
    names = [name for bit, name in SOCKETCAN_ERRORS if can_id & bit]
    return 'Error Frame: ' + ', '.join(names) if names else 'Error Frame'


def local_time(unix_time):
    """Naive local datetime of a Unix time, None if it is out of range."""
    try:
        return datetime.datetime.fromtimestamp(unix_time)
    except (OverflowError, OSError, ValueError):
        return None


def relative_times(rows):
    """Make Unix timestamps relative to the first frame (in place) and keep that time as rows.start."""
    if rows and rows[0][0] > EPOCH_LIMIT:
        first = rows[0][0]
        if rows.start is None:
            rows.start = local_time(first)
        rows[:] = [(row[0] - first,) + tuple(row[1:]) for row in rows]
        rows.errors[:] = [(error[0] - first,) + tuple(error[1:]) for error in rows.errors]
    return rows


MONTH_NUMBERS = {name: number for number, names in enumerate((
    ('jan', 'jän'), ('feb',), ('mar', 'mär', 'mrz'), ('apr',), ('may', 'mai'), ('jun',), ('jul',), ('aug',),
    ('sep',), ('oct', 'okt'), ('nov',), ('dec', 'dez')), 1) for name in names}
# Vector ASC: "date Tue Aug 11 01:32:42.560 pm 2026" (also 24 h and without milliseconds)
ASC_DATE_RE = re.compile(r'^\s*(?:date|Begin\s+Triggerblock)\s+\S+\s+(\S+)\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})'
                         r'(?:[.,](\d+))?\s*(am|pm)?\s+(\d{4})', re.I)
# Newer Vector ASC: "date 2026-04-01 04:32:56 +0000" (time zone offset optional)
ASC_ISO_DATE_RE = re.compile(r'^\s*date\s+(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?'
                             r'\s*(?:([+-])(\d{2}):?(\d{2})|Z)?', re.I)


def _fraction(text):
    return int((text or '0')[:6].ljust(6, '0'))


def parse_asc_date(line):
    """Start of measurement from the "date" line of a Vector ASC trace (naive local time) or None."""
    match = ASC_ISO_DATE_RE.match(line)
    if match:
        year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
        try:
            when = datetime.datetime(year, month, day, hour, minute, second, _fraction(match.group(7)))
        except ValueError:
            return None
        if match.group(8) or re.search(r'\dZ\s*$', line.strip()):
            offset = 0
            if match.group(8):
                offset = (int(match.group(9)) * 60 + int(match.group(10))) * (1 if match.group(8) == '+' else -1)
            utc = when - datetime.timedelta(minutes=offset)
            return local_time((utc - datetime.datetime(1970, 1, 1)).total_seconds()) or when
        return when
    match = ASC_DATE_RE.match(line)
    if not match:
        return None
    month = MONTH_NUMBERS.get(match.group(1)[:3].lower())
    hour = int(match.group(3))
    meridiem = (match.group(7) or '').lower()
    if meridiem == 'pm' and hour < 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    try:
        return datetime.datetime(int(match.group(8)), month, int(match.group(2)), hour, int(match.group(4)),
                                 int(match.group(5)), _fraction(match.group(6))) if month else None
    except ValueError:
        return None


def _report(progress, done, total):
    if progress and total:
        progress(min(1.0, float(done) / total))


# ======================================================================== text formats

def _hex_data(tokens):
    try:
        return bytes(int(token, 16) for token in tokens)
    except ValueError:
        return b''


# ------------------------------------------------------------------- PEAK PCAN-View (.trc)

TRC_VERSION_RE = re.compile(r'^;\$FILEVERSION=(\d+)\.(\d+)', re.I)
TRC_COLUMNS_RE = re.compile(r'^;\$COLUMNS=(.+)$', re.I)
TRC_START_RE = re.compile(r'^;\$STARTTIME=([\d.]+)', re.I)
TRC_LINE_RE = re.compile(r'^\s*\d+\)\s+\d+(?:\.\d+)?\s')
TRC_LAYOUTS = {
    (1, 0): 'N O I L D', (1, 1): 'N O T I L D', (1, 2): 'N O B T I L D', (1, 3): 'N O B T I R L D',
    (2, 0): 'N O T I d l D', (2, 1): 'N O T B I d R L D',
}
TRC_DATA_TYPES = ('DT', 'FD', 'FB', 'FE', 'BI')  # data frames of version 2.x


def parse_trc(lines, progress=None):
    version = None
    columns = None
    rows = Rows()
    step = max(1, len(lines) // 200)
    for number, line in enumerate(lines, 1):
        if number % step == 0:
            _report(progress, number, len(lines))
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(';'):
            match = TRC_VERSION_RE.match(stripped)
            if match:
                version = (int(match.group(1)), int(match.group(2)))
            match = TRC_COLUMNS_RE.match(stripped)
            if match:
                columns = [part.strip() for part in match.group(1).split(',')]
            match = TRC_START_RE.match(stripped)
            if match:  # days since 30.12.1899 (OLE date), local time
                try:
                    start = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=float(match.group(1)))
                    rows.start = start + datetime.timedelta(microseconds=-(start.microsecond % 1000) + (
                        1000 if start.microsecond % 1000 >= 500 else 0))  # to whole milliseconds
                except (OverflowError, ValueError):
                    pass
            continue
        tokens = stripped.split()
        if tokens[0].endswith(')'):
            tokens[0] = tokens[0][:-1]
        if columns is None:
            if version is None:  # no header: 1.1 has the direction as third column, 1.0 does not
                version = (1, 1) if len(tokens) > 2 and tokens[2] in ('Rx', 'Tx') else (1, 0)
            columns = TRC_LAYOUTS.get(version, TRC_LAYOUTS[(2, 1)] if version >= (2, 1) else 'N O T I L D').split()
        if 'D' not in columns or len(tokens) < columns.index('D'):
            continue
        fields = dict(zip(columns[:columns.index('D')], tokens))
        data_tokens = tokens[columns.index('D'):]
        kind = fields.get('T', 'DT')
        if version >= (2, 0):
            if kind not in TRC_DATA_TYPES:
                continue  # remote, status, error and event lines
            direction = fields.get('d', 'Rx')
        else:
            if kind not in ('Rx', 'Tx', 'DT'):
                continue  # warnings and errors
            direction = kind if kind in ('Rx', 'Tx') else 'Rx'
        try:
            time = float(fields['O']) / 1000.0
            identifier = int(fields['I'], 16)
            if 'l' in fields:
                length = int(fields['l'])
            else:
                length = dlc_length(int(fields.get('L', len(data_tokens))), kind in TRC_DATA_TYPES[1:])
        except (KeyError, ValueError):
            continue
        data = _hex_data(data_tokens[:length])
        if data:
            rows.append((time, fields.get('B', '1'), direction, identifier, data, number))
    return rows


# ------------------------------------------------------------------- BUSMASTER (.log)

BUSMASTER_START_RE = re.compile(r'\*\*\*START DATE AND TIME\s+(\d+):(\d+):(\d+)\s+(\d+):(\d+):(\d+):(\d+)', re.I)
BUSMASTER_TIME_RE = re.compile(r'^(\d+):(\d+):(\d+):(\d+)$')


def _busmaster_seconds(text):
    match = BUSMASTER_TIME_RE.match(text)
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    # Message times have 4 fraction digits (1/10 ms), the start time in the header has 3 (ms).
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction) / 10.0 ** len(fraction)


def parse_busmaster(lines, progress=None):
    base = 16
    mode = 'system'
    start = None
    previous = 0.0
    rows = Rows()
    step = max(1, len(lines) // 200)
    for number, line in enumerate(lines, 1):
        if number % step == 0:
            _report(progress, number, len(lines))
        stripped = line.strip()
        if not stripped or stripped.startswith('<'):
            continue
        if stripped.startswith('***'):
            upper = stripped.upper()
            if upper == '***DEC***':
                base = 10
            elif upper == '***HEX***':
                base = 16
            elif 'RELATIVE MODE' in upper:
                mode = 'relative'
            elif 'ABSOLUTE MODE' in upper:
                mode = 'absolute'
            elif 'SYSTEM MODE' in upper:
                mode = 'system'
            match = BUSMASTER_START_RE.match(stripped)
            if match:
                day, month, year = (int(part) for part in match.groups()[:3])
                start = _busmaster_seconds('%s:%s:%s:%s' % match.groups()[3:])
                try:
                    rows.start = datetime.datetime(year, month, day) + datetime.timedelta(seconds=start)
                except (OverflowError, ValueError):
                    pass
            continue
        tokens = stripped.split()
        if len(tokens) < 6 or tokens[1] not in ('Rx', 'Tx'):
            continue
        time = _busmaster_seconds(tokens[0])
        kind = tokens[4].lower()
        if time is None or 'r' in kind:
            continue  # remote frames
        try:
            identifier = int(tokens[3], 16) if tokens[3].lower().startswith('0x') else int(tokens[3], base)
            length = int(tokens[5])
            data = bytes(int(token, base) for token in tokens[6:6 + length])
        except ValueError:
            continue
        if mode == 'system':
            if start is None:
                start = time
            time -= start
            if time < -1:
                time += 86400.0  # after midnight
        elif mode == 'relative':
            time = previous = previous + time
        if data:
            rows.append((time, tokens[2], tokens[1], identifier, data, number))
    return rows


# ------------------------------------------------------------------- Linux candump (.log)

# candump -l: "(1795082400.001349) can0 100#000000004B000000", CAN FD "can0 123##1DEADBEEF"
CANDUMP_LOG_RE = re.compile(r'^\s*\((\d+(?:\.\d+)?)\)\s+(\S+)\s+([0-9A-Fa-f]{1,8})#(#?)(\S*)(?:\s+([RT]))?\s*$')
# candump screen output: "(1795082400.001349)  can0  100   [8]  00 00 00 00 4B 00 00 00"
CANDUMP_SCREEN_RE = re.compile(r'^\s*(?:\((\d+(?:\.\d+)?)\)\s+)?(\S+)\s+([0-9A-Fa-f]{3}|[0-9A-Fa-f]{8})\s+'
                               r'\[(\d+)\]\s+((?:[0-9A-Fa-f]{2}\s*)*)$')


def parse_candump(lines, progress=None):
    rows = Rows()
    step = max(1, len(lines) // 200)
    for number, line in enumerate(lines, 1):
        if number % step == 0:
            _report(progress, number, len(lines))
        match = CANDUMP_LOG_RE.match(line)
        if match:
            time, interface, identifier, fd, payload, direction = match.groups()
            if payload[:1] in ('R', 'r'):
                continue  # remote frame
            if fd:
                payload = payload[1:]  # flags nibble of CAN FD
            try:
                data = binascii.unhexlify(payload)
            except (binascii.Error, ValueError):
                continue
            if int(identifier, 16) & CAN_ERR_FLAG:
                rows.errors.append((float(time), interface, socketcan_error_text(int(identifier, 16))))
                continue
            row = (float(time), interface, 'Tx' if direction == 'T' else 'Rx', int(identifier, 16), data, number)
        else:
            match = CANDUMP_SCREEN_RE.match(line)
            if not match:
                continue
            time, interface, identifier, length, payload = match.groups()
            data = _hex_data(payload.split()[:int(length)])
            moment = float(time) if time else (number - 1) * 0.001
            if int(identifier, 16) & CAN_ERR_FLAG:
                rows.errors.append((moment, interface, socketcan_error_text(int(identifier, 16))))
                continue
            row = (moment, interface, 'Rx', int(identifier, 16), data, number)
        if row[4]:
            rows.append(row)
    return relative_times(rows)


# ------------------------------------------------------------------- SLCAN / Lawicel (.txt)

SLCAN_RE = re.compile(r'^([tTdDbB])([0-9A-Fa-f]+)$')
SLCAN_WRAP = 60000  # the millisecond timestamp runs from 0 to 59999


def parse_slcan(lines, progress=None):
    rows = Rows()
    offset = 0
    last = None
    step = max(1, len(lines) // 200)
    for number, line in enumerate(lines, 1):
        if number % step == 0:
            _report(progress, number, len(lines))
        match = SLCAN_RE.match(line.strip())
        if not match:
            continue
        kind, rest = match.groups()
        id_digits = 3 if kind in 'tdb' else 8
        if len(rest) < id_digits + 1:
            continue
        identifier = int(rest[:id_digits], 16)
        dlc = int(rest[id_digits], 16)
        length = dlc_length(dlc, kind not in 'tT')
        payload = rest[id_digits + 1:id_digits + 1 + 2 * length]
        stamp = rest[id_digits + 1 + 2 * length:]
        if len(payload) != 2 * length or not length:
            continue
        if len(stamp) == 4:
            millis = int(stamp, 16)
            if last is not None and millis < last:
                offset += SLCAN_WRAP
            last = millis
            time = (offset + millis) / 1000.0
        else:
            time = (number - 1) * 0.001  # without timestamps: 1 ms per line
        rows.append((time, '1', 'Rx', identifier, binascii.unhexlify(payload), number))
    return rows


# ------------------------------------------------------------------- CSV (.csv)

CSV_COLUMNS = {
    'time': ('timestamp_s', 'timestamp', 'time', 'time_s', 'zeit', 'timestamp_us', 'epoch_timestamp'),
    'id': ('can_id_hex', 'id_hex', 'arbitration_id', 'can_id', 'id', 'identifier', 'can_id_dec', 'id_dec'),
    'channel': ('channel', 'bus', 'kanal', 'interface'),
    'direction': ('direction', 'dir', 'rx/tx', 'richtung'),
    'dlc': ('dlc', 'length', 'len'),
    'data': ('data_hex', 'data', 'daten', 'payload', 'bytes'),
    'remote': ('remote', 'is_remote_frame', 'rtr'),
    'error': ('error', 'is_error_frame'),
}


def _csv_header(line):
    for delimiter in (',', ';', '\t'):
        names = [name.strip().strip('"').lower() for name in line.split(delimiter)]
        if len(names) >= 3:
            found = {key: next((names.index(name) for name in choices if name in names), None)
                     for key, choices in CSV_COLUMNS.items()}
            if found['id'] is not None and found['data'] is not None:
                return delimiter, names, found
    return None


def _csv_data(text, base64_data):
    text = text.strip()
    if base64_data:
        try:
            return base64.b64decode(text)
        except (binascii.Error, ValueError):
            return b''
    text = text.replace('0x', '').replace('0X', '')
    tokens = re.split(r'[\s:,-]+', text) if re.search(r'[\s:,-]', text) else [text[i:i + 2] for i in
                                                                              range(0, len(text), 2)]
    return _hex_data([token for token in tokens if token])


def parse_csv(lines, progress=None):
    header = _csv_header(lines[0]) if lines else None
    if header is None:
        return Rows()
    delimiter, names, found = header
    id_name = names[found['id']]
    hex_id = 'dec' not in id_name
    # python-can's CSV format ("timestamp,arbitration_id,extended,remote,error,dlc,data") has Base64 data.
    base64_data = names[found['data']] == 'data' and 'extended' in names and found['remote'] is not None
    rows = Rows()
    step = max(1, len(lines) // 200)
    for number, fields in enumerate(csv.reader(lines[1:], delimiter=delimiter), 2):
        if number % step == 0:
            _report(progress, number, len(lines))
        if len(fields) < len(names):
            continue

        def value(key, default=''):
            index = found[key]
            return fields[index].strip() if index is not None else default

        if value('remote', '0').lower() in ('1', 'true') or value('error', '0').lower() in ('1', 'true'):
            continue
        try:
            time = float(value('time', '0').replace(',', '.'))
            if names[found['time'] or 0] == 'timestamp_us':
                time /= 1e6
            text = value('id')
            identifier = int(text, 16) if hex_id or text.lower().startswith('0x') else int(text)
        except ValueError:
            continue
        data = _csv_data(value('data'), base64_data)
        if value('dlc'):
            try:
                data = data[:dlc_length(int(value('dlc')))]
            except ValueError:
                pass
        direction = value('direction', 'Rx')
        direction = 'Tx' if direction.lower() in ('tx', 't', 'true', '1') else 'Rx'
        if data:
            rows.append((time, value('channel', '1') or '1', direction, identifier, data, number))
            if rows.start is None and 'epoch_timestamp' in names and time < EPOCH_LIMIT:
                try:  # relative times with an extra Unix time column
                    rows.start = local_time(float(fields[names.index('epoch_timestamp')]) - time)
                except ValueError:
                    pass
    return relative_times(rows)


# ------------------------------------------------------------------- SavvyCAN CSV (.csv)

def _savvycan_header(line):
    """Column names of a SavvyCAN/GVRET CSV ("Time Stamp,ID,Extended,Dir,Bus,LEN,D1,...,D8") or None."""
    names = [name.strip().strip('"').lower() for name in line.split(',')]
    if names[:2] == ['time stamp', 'id'] and 'd1' in names and 'len' in names:
        return names
    return None


def parse_savvycan(lines, progress=None):
    names = _savvycan_header(lines[0])
    first = names.index('d1')
    index = {name: names.index(name) for name in ('id', 'len')}
    extended, direction, bus = (names.index(name) if name in names else None for name in ('extended', 'dir', 'bus'))
    rows = Rows()
    step = max(1, len(lines) // 200)
    for number, line in enumerate(lines[1:], 2):
        if number % step == 0:
            _report(progress, number, len(lines))
        fields = [field.strip() for field in line.split(',')]
        if len(fields) <= index['len']:
            continue
        try:
            time = float(fields[0]) / 1e6  # microseconds
            identifier = int(fields[index['id']], 16)
            length = int(fields[index['len']])
        except ValueError:
            continue
        data = _hex_data([field for field in fields[first:first + length] if field])
        if data:
            rows.append((time, str(int(fields[bus]) + 1) if bus is not None and fields[bus].isdigit() else '1',
                         'Tx' if direction is not None and fields[direction].lower() == 'tx' else 'Rx',
                         identifier, data, number))
    return relative_times(rows)


# ------------------------------------------------------------------- detection

def text_parser(lines):
    """Return (name, parser) for a text trace that is not Vector ASC, or None."""
    sample = [line.strip() for line in lines[:60] if line.strip()][:30]
    if not sample:
        return None
    if sample[0].upper().startswith('***BUSMASTER'):
        return 'BUSMASTER', parse_busmaster
    if any(TRC_VERSION_RE.match(line) for line in sample) or (
            sample[0].startswith(';') and any(TRC_LINE_RE.match(line) for line in sample)):
        return 'PCAN-View TRC', parse_trc
    data_lines = [line for line in sample if not line.startswith(('#', ';', '//'))]
    if not data_lines:
        return None
    if sum(1 for line in data_lines if CANDUMP_LOG_RE.match(line) or CANDUMP_SCREEN_RE.match(line)) * 2 > len(
            data_lines):
        return 'candump', parse_candump
    if sum(1 for line in data_lines if SLCAN_RE.match(line)) * 2 > len(data_lines):
        return 'SLCAN', parse_slcan
    if _savvycan_header(sample[0]):
        return 'SavvyCAN CSV', parse_savvycan
    if _csv_header(sample[0]):
        return 'CSV', parse_csv
    return None


# ======================================================================== binary formats

# ------------------------------------------------------------------- Vector BLF (.blf)

BLF_CAN_MESSAGE, BLF_LOG_CONTAINER, BLF_CAN_MESSAGE2 = 1, 10, 86
BLF_CAN_FD_MESSAGE, BLF_CAN_FD_MESSAGE_64 = 100, 101
BLF_CAN_ERROR, BLF_CAN_ERROR_EXT, BLF_CAN_FD_ERROR_64 = 2, 73, 104  # error frames
BLF_ERROR_KINDS = (BLF_CAN_ERROR, BLF_CAN_ERROR_EXT, BLF_CAN_FD_ERROR_64)
BLF_OBJ = struct.Struct('<4sHHII')
BLF_HEADER_V1 = struct.Struct('<IHHQ')
BLF_HEADER_V2 = struct.Struct('<IBBHQ8x')
BLF_CAN_MSG = struct.Struct('<HBBI8s')
BLF_CAN_FD_MSG = struct.Struct('<HBBIIBBB5x64s')
BLF_CAN_FD_MSG_64 = struct.Struct('<BBBBIIIIIIIHBBI')


def _blf_frame(kind, body):
    """Return (channel, direction, id, data) or None."""
    if kind in (BLF_CAN_MESSAGE, BLF_CAN_MESSAGE2):
        if len(body) < BLF_CAN_MSG.size:
            return None
        channel, flags, dlc, identifier, data = BLF_CAN_MSG.unpack_from(body)
        if flags & 0x80:
            return None  # remote frame
        return channel, 'Tx' if flags & 1 else 'Rx', identifier & 0x1FFFFFFF, data[:min(dlc, 8)]
    if kind == BLF_CAN_FD_MESSAGE:
        if len(body) < BLF_CAN_FD_MSG.size:
            return None
        channel, flags, dlc, identifier, _, _, fd_flags, valid, data = BLF_CAN_FD_MSG.unpack_from(body)
        if flags & 0x80:
            return None
        length = dlc_length(dlc, bool(fd_flags & 1))
        return channel, 'Tx' if flags & 1 else 'Rx', identifier & 0x1FFFFFFF, data[:length]
    if kind == BLF_CAN_FD_MESSAGE_64:
        if len(body) < BLF_CAN_FD_MSG_64.size:
            return None
        fields = BLF_CAN_FD_MSG_64.unpack_from(body)
        channel, dlc, valid, identifier, fd_flags, direction = (fields[0], fields[1], fields[2], fields[4],
                                                               fields[6], fields[12])
        if fd_flags & 0x10:
            return None  # remote frame
        length = valid or dlc_length(dlc, bool(fd_flags & 0x1000))
        data = body[BLF_CAN_FD_MSG_64.size:BLF_CAN_FD_MSG_64.size + length]
        return channel, 'Tx' if direction == 1 else 'Rx', identifier & 0x1FFFFFFF, data
    return None


def _blf_objects(buffer, rows):
    """Read the complete objects in buffer, return the number of bytes used."""
    pos = 0
    while pos + BLF_OBJ.size <= len(buffer):
        signature, header_size, header_version, size, kind = BLF_OBJ.unpack_from(buffer, pos)
        if signature != b'LOBJ':
            # Skip garbage up to the next object.
            found = buffer.find(b'LOBJ', pos + 1)
            if found < 0:
                return max(pos, len(buffer) - 3)
            pos = found
            continue
        if size < BLF_OBJ.size:
            pos += 4
            continue
        if pos + size > len(buffer):
            break  # continues in the next container
        if kind in (BLF_CAN_MESSAGE, BLF_CAN_MESSAGE2, BLF_CAN_FD_MESSAGE, BLF_CAN_FD_MESSAGE_64):
            if header_version == 2:
                flags, _, _, _, stamp = BLF_HEADER_V2.unpack_from(buffer, pos + BLF_OBJ.size)
            else:
                flags, _, _, stamp = BLF_HEADER_V1.unpack_from(buffer, pos + BLF_OBJ.size)
            body = bytes(buffer[pos + header_size:pos + size])
            frame = _blf_frame(kind, body)
            if frame and frame[3]:
                time = stamp * (1e-5 if flags == 1 else 1e-9)  # 1: 10 µs, 2: 1 ns
                rows.append((time, str(frame[0]), frame[1], frame[2], frame[3], len(rows) + 1))
        elif kind in BLF_ERROR_KINDS and size >= header_size + 1:
            if header_version == 2:
                flags, _, _, _, stamp = BLF_HEADER_V2.unpack_from(buffer, pos + BLF_OBJ.size)
            else:
                flags, _, _, stamp = BLF_HEADER_V1.unpack_from(buffer, pos + BLF_OBJ.size)
            channel = buffer[pos + header_size] if kind == BLF_CAN_FD_ERROR_64 else struct.unpack_from(
                '<H', buffer, pos + header_size)[0]
            rows.errors.append((stamp * (1e-5 if flags == 1 else 1e-9), str(channel), 'Error Frame'))
        pos += size + (0 if kind == BLF_CAN_FD_MESSAGE_64 else size % 4)
    return pos


def parse_blf(data, progress=None):
    header_size = struct.unpack_from('<I', data, 4)[0] if len(data) >= 8 else 0
    pos = header_size if 24 <= header_size < len(data) else 144
    rows = Rows()
    stream = bytearray()
    while pos + BLF_OBJ.size <= len(data):
        signature, obj_header_size, _, size, kind = BLF_OBJ.unpack_from(data, pos)
        if signature != b'LOBJ' or size < BLF_OBJ.size:
            found = data.find(b'LOBJ', pos + 1)
            if found < 0:
                break
            pos = found
            continue
        if kind == BLF_LOG_CONTAINER:
            # The container header follows the object header (16 bytes, some writers use more).
            start = pos + max(BLF_OBJ.size, obj_header_size)
            method = struct.unpack_from('<H', data, start)[0]
            payload = data[start + 16:pos + size]
            if method == 2:
                try:
                    payload = zlib.decompress(payload)
                except zlib.error:
                    payload = zlib.decompressobj().decompress(payload)
            stream.extend(payload)
            used = _blf_objects(stream, rows)
            del stream[:used]
        else:
            stream.extend(data[pos:pos + size])
            used = _blf_objects(stream, rows)
            del stream[:used]
        pos += size + size % 4
        _report(progress, pos, len(data))
    rows.sort(key=lambda row: row[0])
    rows.errors.sort(key=lambda error: error[0])
    rows.start = _blf_start(data)
    return rows


def _blf_start(data):
    """Start of measurement from the file header (SYSTEMTIME, local time) or None."""
    if len(data) < 56:
        return None
    year, month, _, day, hour, minute, second, millis = struct.unpack_from('<8H', data, 40)
    try:
        return datetime.datetime(year, month, day, hour, minute, second, millis * 1000) if year >= 1980 else None
    except ValueError:
        return None


# ------------------------------------------------------------------- ASAM MDF4 (.mf4)

class MdfError(ValueError):
    pass


class Mdf(object):
    """Minimal MDF 4 reader for CAN bus logging (ASAM CAN_DataFrame or plain ID/DLC/DataBytes channels)."""

    def __init__(self, data):
        self.data = data
        if data[:3] != b'MDF':
            raise MdfError(tr('Keine MDF-Datei.'))
        try:
            version = int(data[8:9])
        except ValueError:
            version = 0
        if version < 4:
            raise MdfError(tr('Nur MDF 4 (.mf4) wird unterstützt, nicht MDF 3.'))

    def block(self, offset, kinds=None):
        """Return (kind, links, body) of the block at offset."""
        data = self.data
        if offset <= 0 or offset + 24 > len(data) or data[offset:offset + 2] != b'##':
            raise MdfError(tr('Die MDF-Datei ist beschädigt (ungültiger Verweis auf Position %s).') % offset)
        kind = data[offset + 2:offset + 4].decode('latin-1')
        length, count = struct.unpack_from('<QQ', data, offset + 8)
        if kinds and kind not in kinds:
            raise MdfError(tr('Die MDF-Datei ist beschädigt (ungültiger Verweis auf Position %s).') % offset)
        links = struct.unpack_from('<%dQ' % count, data, offset + 24)
        return kind, links, data[offset + 24 + 8 * count:offset + length]

    def text(self, offset):
        if not offset:
            return ''
        kind, _, body = self.block(offset)
        if kind not in ('TX', 'MD'):
            return ''
        text = body.split(b'\0', 1)[0].decode('utf-8', 'replace')
        if kind == 'MD':
            match = re.search(r'<TX>(.*?)</TX>', text, re.S)
            text = match.group(1) if match else ''
        return text.strip()

    def payload(self, offset):
        """Concatenated content of a DT/SD/RD, DZ, DL or HL block."""
        if not offset:
            return b''
        kind, links, body = self.block(offset)
        if kind in ('DT', 'SD', 'RD', 'DV'):
            return body
        if kind == 'DZ':
            zip_type, parameter, original, size = struct.unpack_from('<xxBxIQQ', body)
            raw = zlib.decompress(body[24:24 + size])
            if zip_type == 1 and parameter:
                columns = parameter
                rows = original // columns
                full = rows * columns
                plain = bytearray(raw)
                for column in range(columns):
                    plain[column:full:columns] = raw[column * rows:(column + 1) * rows]
                raw = bytes(plain)
            return raw
        if kind == 'DL':
            parts = []
            while True:
                parts.extend(self.payload(link) for link in links[1:] if link)
                if not links[0]:
                    break
                kind, links, body = self.block(links[0], ('DL',))
            return b''.join(parts)
        if kind == 'HL':
            return self.payload(links[0])
        return b''

    def channels(self, offset):
        result = []
        while offset:
            _, links, body = self.block(offset, ('CN',))
            (cn_type, sync, data_type, bit_offset, byte_offset, bit_count) = struct.unpack_from('<BBBBII', body)
            name = self.text(links[2])
            result.append({'name': name, 'type': cn_type, 'sync': sync, 'data_type': data_type,
                           'bit_offset': bit_offset, 'byte_offset': byte_offset, 'bit_count': bit_count,
                           'conversion': links[4], 'data': links[5]})
            if links[1]:
                kind = self.data[links[1] + 2:links[1] + 4]
                if kind == b'CN':
                    result.extend(self.channels(links[1]))
            offset = links[0]
        return result

    def conversion(self, offset):
        """Linear conversion (a, b) of a master channel, else None."""
        if not offset:
            return None
        kind, _, body = self.block(offset)
        if kind == 'CC' and body[0] == 1 and len(body) >= 40:
            return struct.unpack_from('<dd', body, 24)
        return None


def _mdf_value(record, channel):
    start = channel['byte_offset']
    bits = channel['bit_count']
    data_type = channel['data_type']
    if data_type in (4, 5) and bits in (32, 64) and not channel['bit_offset']:
        form = ('<' if data_type == 4 else '>') + ('f' if bits == 32 else 'd')
        return struct.unpack_from(form, record, start)[0]
    size = (channel['bit_offset'] + bits + 7) // 8
    chunk = record[start:start + size]
    value = int.from_bytes(chunk, 'big' if data_type in (1, 3) else 'little')
    value = (value >> channel['bit_offset']) & ((1 << bits) - 1)
    if data_type in (2, 3) and value >> (bits - 1):
        value -= 1 << bits
    return value


def _mdf_short_name(name):
    return name.rsplit('.', 1)[-1].lower()


def _mdf_vlsd_reader(stream):
    """Function offset -> bytes for a VLSD stream (u32 length + bytes per sample)."""
    def read(offset):
        if offset + 4 > len(stream):
            return b''
        length = struct.unpack_from('<I', stream, offset)[0]
        return stream[offset + 4:offset + 4 + length]
    return read


def parse_mf4(data, progress=None):
    mdf = Mdf(data)
    _, header_links, header_body = mdf.block(64, ('HD',))
    rows = Rows()
    groups = []
    dg = header_links[0]
    while dg:
        _, dg_links, dg_body = mdf.block(dg, ('DG',))
        id_size = dg_body[0] if dg_body else 0
        cgs = {}
        cg = dg_links[1]
        while cg:
            _, cg_links, cg_body = mdf.block(cg, ('CG',))
            record_id, cycles, flags, _, data_bytes, inval_bytes = struct.unpack_from('<QQHH4xII', cg_body)
            cgs[record_id] = {'offset': cg, 'name': mdf.text(cg_links[2]), 'cycles': cycles, 'vlsd': flags & 1,
                              'size': data_bytes + inval_bytes, 'channels': mdf.channels(cg_links[1]),
                              'records': [], 'stream': bytearray()}
            cg = cg_links[0]
        groups.append((id_size, cgs, dg_links[2]))
        dg = dg_links[0]

    for group_number, (id_size, cgs, data_link) in enumerate(groups):
        stream = mdf.payload(data_link)
        if not cgs:
            continue
        if id_size == 0:
            group = next(iter(cgs.values()))
            size = group['size']
            count = len(stream) // size if size else 0
            if group['cycles']:
                count = min(count, group['cycles'])
            group['records'] = [stream[i * size:(i + 1) * size] for i in range(count)]
        else:
            id_form = {1: '<B', 2: '<H', 4: '<I', 8: '<Q'}.get(id_size)
            pos = 0
            while id_form and pos + id_size <= len(stream):
                record_id = struct.unpack_from(id_form, stream, pos)[0]
                pos += id_size
                group = cgs.get(record_id)
                if group is None:
                    break
                if group['vlsd']:
                    length = struct.unpack_from('<I', stream, pos)[0]
                    group['stream'].extend(stream[pos:pos + 4 + length])
                    pos += 4 + length
                else:
                    group['records'].append(stream[pos:pos + group['size']])
                    pos += group['size']
        by_offset = {group['offset']: group for group in cgs.values()}
        for group in cgs.values():
            if group['vlsd'] or not group['records']:
                continue
            found = _mdf_can_rows(mdf, group, by_offset)
            if found:
                rows.extend(found)
            else:
                _mdf_signals(mdf, group, rows.environment)
        _report(progress, group_number + 1, len(groups))
    rows.sort(key=lambda row: row[0])
    shift = rows[0][0] if rows and rows[0][0] < 0 else 0.0  # times before the start time of the file
    result = Rows((row[0] - shift,) + row[1:5] + (number,) for number, row in enumerate(rows, 1))
    start = _mdf_start(header_body)
    result.environment = rows.environment
    for values in result.environment.values():
        values.sort(key=lambda item: item[0])
    result.start = start + datetime.timedelta(seconds=shift) if start else None
    return result


def _mdf_start(body):
    """Start of measurement from the HD block (naive local time) or None."""
    if len(body) < 13:
        return None
    nanos, zone, daylight, flags = struct.unpack_from('<QhhB', body)
    if not nanos:
        return None
    if flags & 1:  # local time stored as if it were UTC
        return datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=nanos // 1000)
    if flags & 2:  # UTC with valid offsets: time at the place of the recording
        return datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=nanos // 1000,
                                                                  minutes=zone + daylight)
    return local_time(nanos / 1e9)


def _mdf_number_text(value):
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            return None
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    return str(value)


def _mdf_signals(mdf, group, environment):
    """Numeric channels of a group without CAN frames (MDF4 signal logging, e.g. decoded CANape/CANoe signals)
    become environment variables {name: [(time, text)]}, so the viewer lists and plots them."""
    time_channel = None
    for channel in group['channels']:
        if channel['type'] in (2, 3):
            time_channel = channel
            break
    if time_channel is None or time_channel['data_type'] not in (0, 1, 4, 5):
        return
    time_linear = mdf.conversion(time_channel['conversion'])
    signals = [channel for channel in group['channels']
               if channel['type'] == 0 and channel['data_type'] <= 5 and 0 < channel['bit_count'] <= 64
               and channel['name']]
    for channel in signals:
        linear = mdf.conversion(channel['conversion'])
        values = environment.setdefault(channel['name'], [])
        for record in group['records']:
            if len(record) < group['size']:
                break
            time = _mdf_value(record, time_channel)
            if time_linear:
                time = time_linear[0] + time_linear[1] * time
            value = _mdf_value(record, channel)
            if linear:
                value = linear[0] + linear[1] * value
            text = _mdf_number_text(value)
            if text is not None:
                values.append((round(float(time), 9), text))
        if not values:
            del environment[channel['name']]


def _mdf_can_rows(mdf, group, groups_by_offset):
    names = [group['name']] + [channel['name'] for channel in group['channels']]
    if any('errorframe' in name.lower() or 'remoteframe' in name.lower() for name in names):
        return []
    roles = {}
    for channel in group['channels']:
        if channel['type'] in (2, 3) and 'time' not in roles:
            roles['time'] = channel
            continue
        short = _mdf_short_name(channel['name'])
        for role, choices in (('id', ('id', 'can_id', 'identifier', 'arbitration_id')), ('dlc', ('dlc',)),
                              ('length', ('datalength',)), ('data', ('databytes', 'data', 'data_bytes')),
                              ('channel', ('buschannel', 'channel')), ('dir', ('dir', 'direction'))):
            if short in choices and role not in roles:
                roles[role] = channel
    if 'id' not in roles or 'data' not in roles:
        return []
    time_channel = roles.get('time')
    linear = mdf.conversion(time_channel['conversion']) if time_channel else None
    data_channel = roles['data']
    vlsd = None
    if data_channel['type'] == 1 and data_channel['data']:
        target = groups_by_offset.get(data_channel['data'])
        if target is not None:  # MDF 4.2: VLSD channel group
            vlsd = _mdf_vlsd_reader(bytes(target['stream']))
        else:
            vlsd = _mdf_vlsd_reader(mdf.payload(data_channel['data']))
    rows = Rows()
    for number, record in enumerate(group['records']):
        if len(record) < group['size']:
            break
        time = _mdf_value(record, time_channel) if time_channel else number * 0.001
        if linear:
            time = linear[0] + linear[1] * time
        if vlsd:
            data = vlsd(_mdf_value(record, dict(data_channel, data_type=0)))
        else:
            start = data_channel['byte_offset']
            data = record[start:start + data_channel['bit_count'] // 8]
        if 'length' in roles:
            data = data[:_mdf_value(record, roles['length'])]
        elif 'dlc' in roles:
            data = data[:dlc_length(_mdf_value(record, roles['dlc']))]
        if not data:
            continue
        channel = str(_mdf_value(record, roles['channel'])) if 'channel' in roles else '1'
        direction = 'Tx' if 'dir' in roles and _mdf_value(record, roles['dir']) == 1 else 'Rx'
        identifier = int(_mdf_value(record, roles['id'])) & 0x1FFFFFFF
        rows.append((float(time), channel, direction, identifier, bytes(data)))
    return rows


# ------------------------------------------------------------------- PCAP / PCAPNG (SocketCAN)

LINKTYPE_CAN_SOCKETCAN = 227
def _socketcan_error(packet):
    """Text of a SocketCAN error frame, None for other frames."""
    if len(packet) >= 8:
        can_id = struct.unpack_from('>I', packet)[0]
        if can_id & CAN_ERR_FLAG:
            return socketcan_error_text(can_id)
    return None


def _socketcan_frame(packet):
    """Return (id, data) of a SocketCAN frame (big-endian ID as in LINKTYPE_CAN_SOCKETCAN) or None."""
    if len(packet) < 8:
        return None
    can_id, length = struct.unpack_from('>IB', packet)
    if can_id & (CAN_RTR_FLAG | CAN_ERR_FLAG):
        return None
    return can_id & 0x1FFFFFFF, bytes(packet[8:8 + min(length, 64)])


def parse_pcap(data, progress=None):
    magic = data[:4]
    endian = '<' if magic in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1') else '>'
    scale = 1e-9 if magic in (b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d') else 1e-6
    linktype = struct.unpack_from(endian + 'I', data, 20)[0] & 0xFFFF
    if linktype != LINKTYPE_CAN_SOCKETCAN:
        raise ValueError(tr('Die PCAP-Datei enthält keine SocketCAN-Frames (Link-Typ %s).') % linktype)
    rows = Rows()
    pos = 24
    record = struct.Struct(endian + 'IIII')
    while pos + 16 <= len(data):
        seconds, fraction, captured, _ = record.unpack_from(data, pos)
        packet = data[pos + 16:pos + 16 + captured]
        pos += 16 + captured
        error = _socketcan_error(packet)
        if error:
            rows.errors.append((seconds + fraction * scale, '1', error))
        frame = _socketcan_frame(packet)
        if frame and frame[1]:
            rows.append((seconds + fraction * scale, '1', 'Rx', frame[0], frame[1], len(rows) + 1))
        if len(rows) % 5000 == 0:
            _report(progress, pos, len(data))
    return relative_times(rows)


def parse_pcapng(data, progress=None):
    rows = Rows()
    interfaces = []  # (linktype, seconds per tick) per interface of the current section
    endian = '<'
    pos = 0
    while pos + 12 <= len(data):
        kind = struct.unpack_from(endian + 'I', data, pos)[0]
        if kind == 0x0A0D0D0A:  # section header: byte order
            endian = '<' if data[pos + 8:pos + 12] == b'\x4d\x3c\x2b\x1a' else '>'
            interfaces = []
        length = struct.unpack_from(endian + 'I', data, pos + 4)[0]
        if length < 12 or pos + length > len(data):
            break
        body = data[pos + 8:pos + length - 4]
        if kind == 1 and len(body) >= 8:  # interface description
            linktype = struct.unpack_from(endian + 'H', body)[0]
            resolution = 1e-6
            option = 8
            while option + 4 <= len(body):
                code, size = struct.unpack_from(endian + 'HH', body, option)
                if code == 0:
                    break
                if code == 9 and size >= 1:  # if_tsresol
                    value = body[option + 4]
                    resolution = 2.0 ** -(value & 0x7F) if value & 0x80 else 10.0 ** -value
                option += 4 + (size + 3) // 4 * 4
            interfaces.append((linktype, resolution))
        elif kind in (6, 2) and len(body) >= 20:  # enhanced / obsolete packet
            if kind == 6:
                interface, high, low, captured = struct.unpack_from(endian + 'IIII', body)
            else:
                interface, high, low, captured = struct.unpack_from(endian + 'HxxIII', body)
            if interface < len(interfaces) and interfaces[interface][0] == LINKTYPE_CAN_SOCKETCAN:
                time = ((high << 32) | low) * interfaces[interface][1]
                error = _socketcan_error(body[20:20 + captured])
                if error:
                    rows.errors.append((time, str(interface + 1), error))
                frame = _socketcan_frame(body[20:20 + captured])
                if frame and frame[1]:
                    rows.append((time, str(interface + 1), 'Rx', frame[0], frame[1], len(rows) + 1))
        pos += length
        if len(rows) % 5000 == 0:
            _report(progress, pos, len(data))
    if not rows and interfaces and all(linktype != LINKTYPE_CAN_SOCKETCAN for linktype, _ in interfaces):
        raise ValueError(tr('Die PCAP-Datei enthält keine SocketCAN-Frames (Link-Typ %s).') % interfaces[0][0])
    return relative_times(rows)


# ------------------------------------------------------- Intrepid Vehicle Spy (VSB)

VSB_MAGIC = b'icsbin'
VSB_RECORD = 64  # one VSBSpyMessage (icsSpyMessage)
VSB_EPOCH = datetime.datetime(2007, 1, 1)  # zero of the hardware time
VSB_TX, VSB_REMOTE, VSB_ERROR = 0x02, 0x08, 0x01  # status bits (lowest byte)
# NetworkID of neoVI -> channel number
VSB_NETWORKS = {1: '1', 2: '2', 3: '3', 4: '4', 42: '2', 44: '3', 61: '4', 62: '5'}
VSB_PROTOCOLS = (1, 30)  # CAN, CAN FD


def parse_vsb(data, progress=None):
    """Vehicle Spy binary (icsbin) version 0x104: records of 64 bytes, a record with extra data
    (CAN FD, more than 8 bytes) is followed by those bytes."""
    version = struct.unpack_from('<I', data, 6)[0] if len(data) >= 10 else 0
    if version != 0x104:
        raise ValueError(tr('Nicht unterstützte VSB-Version: %s.') % hex(version))
    rows = Rows()
    pos = 10
    first = None
    while pos + VSB_RECORD <= len(data):
        record = data[pos:pos + VSB_RECORD]
        pos += VSB_RECORD
        extra = b''
        size = struct.unpack_from('<I', record, 56)[0]
        if record[30] and size:
            extra = data[pos:pos + size]
            pos += size
        status = struct.unpack_from('<I', record, 0)[0]
        time_low, time_high = struct.unpack_from('<II', record, 8)
        ticks = (time_high << 32) | time_low
        time = ticks * (1.6e-6 if record[24] & 0x0F == 1 else 25e-9)
        if first is None:
            first = time
        network, protocol = record[26], record[28]
        if protocol not in VSB_PROTOCOLS or status & (VSB_REMOTE | VSB_ERROR):
            continue
        length = record[32]
        payload = extra[:length] if record[30] and extra else record[40:40 + min(length, 8)]
        if not payload:
            continue
        identifier = struct.unpack_from('<I', record, 36)[0] & 0x1FFFFFFF
        rows.append((time, VSB_NETWORKS.get(network, str(network)), 'Tx' if status & VSB_TX else 'Rx',
                     identifier, bytes(payload), len(rows) + 1))
        if len(rows) % 5000 == 0:
            _report(progress, pos, len(data))
    if first is not None and rows:
        try:
            rows.start = VSB_EPOCH + datetime.timedelta(seconds=first)
        except OverflowError:
            rows.start = None
        rows[:] = [(row[0] - first,) + tuple(row[1:]) for row in rows]
    return rows


# ------------------------------------------------------- python-can SQLite (.db)

def parse_sqlite(data, progress=None):
    """python-can SqliteWriter: table messages (ts, arbitration_id, extended, remote, error, dlc, data)."""
    import os
    import sqlite3
    import tempfile
    handle, path = tempfile.mkstemp(suffix='.db')
    try:
        with os.fdopen(handle, 'wb') as output:
            output.write(data)
        database = sqlite3.connect(path)
        try:
            rows = Rows()
            for number, (time, identifier, remote, error, dlc, payload) in enumerate(database.execute(
                    'SELECT ts, arbitration_id, remote, error, dlc, data FROM messages ORDER BY ts'), 1):
                payload = bytes(payload or b'')[:dlc_length(dlc or len(payload or b''))]
                if payload and not remote and not error:
                    rows.append((float(time), '1', 'Rx', int(identifier), payload, number))
        except sqlite3.Error as failure:
            raise ValueError(tr('Keine python-can-Datenbank (%s).') % failure)
        finally:
            database.close()
    finally:
        os.remove(path)
    return relative_times(rows)


# ------------------------------------------------------------------- detection

def binary_parser(data):
    """Return (name, parser) for a binary trace, or None for text."""
    head = data[:4]
    if head == b'LOGG':
        return 'BLF', parse_blf
    if data[:16] == b'SQLite format 3\x00':
        return 'SQLite', parse_sqlite
    if data[:6] == VSB_MAGIC:
        return 'VSB', parse_vsb
    if head == b'MDF ' or data[:8] == b'UnFinMF ':
        return 'MDF4', parse_mf4
    if head in (b'\xd4\xc3\xb2\xa1', b'\xa1\xb2\xc3\xd4', b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d'):
        return 'PCAP', parse_pcap
    if head == b'\x0a\x0d\x0d\x0a':
        return 'PCAPNG', parse_pcapng
    return None
