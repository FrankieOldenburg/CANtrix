#!/usr/bin/env python3
"""Speichern von CAN-Traces in allen Formaten, die CAN_TraceFormats.py bzw. parse_trace lesen.

write_trace(path, format_key, frames, environment, start) schreibt die Frames (Objekte mit time,
channel, direction, id, data) in eine Datei. Zeiten sind relativ zum Messbeginn; start (Unix-Zeit)
ist die Uhrzeit des Messbeginns für Formate mit absoluter Zeit. Extended-IDs werden an der Größe
erkannt (> 0x7FF), CAN FD an der Datenlänge (> 8 oder keine klassische Länge). Umgebungsvariablen
({name: [(zeit, text)]}) kann nur ASC aufnehmen.

Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import datetime
import os
import struct
import zlib

from CAN_TraceFormats import FD_LENGTHS

# (key, file type description, extension); the description is shown in the save dialog.
WRITE_FORMATS = (
    ('asc', 'Vector ASC', '.asc'),
    ('blf', 'Vector BLF', '.blf'),
    ('mf4', 'ASAM MDF4', '.mf4'),
    ('trc', 'PEAK PCAN-View TRC', '.trc'),
    ('busmaster', 'BUSMASTER', '.log'),
    ('candump', 'Linux candump', '.log'),
    ('slcan', 'SLCAN', '.txt'),
    ('csv', 'CSV', '.csv'),
    ('pcap', 'PCAP (SocketCAN)', '.pcap'),
    ('pcapng', 'PCAPNG (SocketCAN)', '.pcapng'),
)

WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def format_for_path(path, description=None):
    """Format key for a file name; description (chosen file type) decides between the two .log formats."""
    for key, text, extension in WRITE_FORMATS:
        if description and description.startswith(text):
            return key
    extension = os.path.splitext(path)[1].lower()
    for key, text, ending in WRITE_FORMATS:
        if ending == extension:
            return key  # .log: candump comes after BUSMASTER only in the list, so BUSMASTER wins
    return None


def is_extended(frame):
    return frame.id > 0x7FF


def is_fd(frame):
    return frame.fd or len(frame.data) > 8


def dlc_code(length):
    """DLC of a data length (CAN FD lengths are rounded up)."""
    if length <= 8:
        return length
    for code, size in enumerate(FD_LENGTHS):
        if size >= length:
            return code
    return 15


def fd_data(frame):
    """Data padded to the next valid CAN FD length."""
    return frame.data + bytes(FD_LENGTHS[dlc_code(len(frame.data))] - len(frame.data))


def channel_numbers(frames):
    """Number (1, 2, …) for every channel name: digits stay, other names (e.g. 'can0') get the next free ones."""
    names = sorted(set(frame.channel for frame in frames))
    numbers = {name: int(name) for name in names if name.isdigit() and int(name) > 0}
    free = 1
    for name in names:
        if name not in numbers:
            while free in numbers.values():
                free += 1
            numbers[name] = free
            free += 1
    return numbers


def interface_name(channel):
    """candump interface for a channel: '1' -> 'can0', names stay."""
    return 'can%d' % (int(channel) - 1) if channel.isdigit() and int(channel) > 0 else channel


def _hex(data):
    return ' '.join('%02X' % value for value in data)


def _local(start):
    return datetime.datetime.fromtimestamp(start)


# ---------------------------------------------------------------- text formats

def write_asc(handle, frames, environment, start, errors=None):
    numbers = channel_numbers(frames)
    when = _local(start)
    stamp = '%s %s %d %02d:%02d:%02d.%03d %s %d' % (
        WEEKDAYS[when.weekday()], MONTHS[when.month - 1], when.day, (when.hour % 12) or 12, when.minute,
        when.second, when.microsecond // 1000, 'pm' if when.hour >= 12 else 'am', when.year)
    handle.write('date %s\nbase hex  timestamps absolute\ninternal events logged\n// version 10.0.0\n' % stamp)
    handle.write('// Saved by CANtrix\nBegin Triggerblock %s\n   0.000000 Start of measurement\n' % stamp)
    events = [(time, 0, name, value) for name, values in (environment or {}).items() for time, value in values]
    events.extend((frame.time, 1, frame, None) for frame in frames)
    events.extend((time, 2, channel, text) for time, channel, text in errors or ())
    events.sort(key=lambda event: (event[0], event[1]))
    for time, kind, item, value in events:
        if kind == 0:
            handle.write('  %.6f ev %s = %s\n' % (time, item, value))
            continue
        if kind == 2:  # CAN error frame
            number = numbers.get(item) or (int(item) if str(item).isdigit() else 1)
            handle.write('  %.6f %d  %s\n' % (time, number, value.replace('Error Frame', 'ErrorFrame', 1)))
            continue
        handle.write(asc_line(item, numbers[item.channel]))
    handle.write('End TriggerBlock\n')


def asc_line(frame, number):
    """One frame as a line of an ASC file (number: CAN channel)."""
    identifier = ('%X' % frame.id) + ('x' if is_extended(frame) else '')
    if is_fd(frame):
        data = fd_data(frame)
        return '  %.6f CANFD %3d %s %8s %32s 1 0 %X %2d %s\n' % (
            frame.time, number, frame.direction, identifier, '', dlc_code(len(data)), len(data), _hex(data))
    return '  %.6f %d  %-15s %s   d %d %s\n' % (
        frame.time, number, identifier, frame.direction, len(frame.data), _hex(frame.data))


def write_trc(handle, frames, environment, start):
    numbers = channel_numbers(frames)
    when = _local(start)
    days = (when - datetime.datetime(1899, 12, 30)).total_seconds() / 86400.0
    handle.write(';$FILEVERSION=2.1\n;$STARTTIME=%.10f\n;$COLUMNS=N,O,T,B,I,d,R,L,D\n;\n' % days)
    handle.write(';   Start time: %s\n;   Saved by CANtrix\n' % when.strftime('%d.%m.%Y %H:%M:%S.%f')[:-3])
    handle.write(';-------------------------------------------------------------------------------\n'
                 ';   Message   Time    Type ID     Rx/Tx\n'
                 ';   Number    Offset  |  Bus  [hex]  |  Reserved\n'
                 ';   |         [ms]    |  |    |      |  |  Data Length Code\n'
                 ';   |         |       |  |    |      |  |  |    Data [hex] ...\n'
                 ';   |         |       |  |    |      |  |  |    |\n'
                 ';---+-- ------+------ +- +- --+----- +- +- +--- +- -- -- -- -- -- -- --\n')
    for number, frame in enumerate(frames, 1):
        data = fd_data(frame) if is_fd(frame) else frame.data
        identifier = ('%08X' if is_extended(frame) else '%04X') % frame.id
        handle.write('%7d %13.3f %s %2d %8s %s - %2d    %s\n' % (
            number, frame.time * 1000.0, 'FD' if is_fd(frame) else 'DT', numbers[frame.channel], identifier,
            frame.direction, dlc_code(len(data)), _hex(data)))


def write_busmaster(handle, frames, environment, start):
    numbers = channel_numbers(frames)
    when = _local(start)
    handle.write('***BUSMASTER Ver 3.2.2***\n***PROTOCOL CAN***\n***NOTE: PLEASE DO NOT EDIT THIS DOCUMENT***\n')
    handle.write('***[START LOGGING SESSION]***\n')
    handle.write('***START DATE AND TIME %s:%03d***\n' % (when.strftime('%d:%m:%Y %H:%M:%S'),
                                                        when.microsecond // 1000))
    handle.write('***HEX***\n***SYSTEM MODE***\n***START CHANNEL BAUD RATE***\n***END CHANNEL BAUD RATE***\n')
    handle.write('***START DATABASE FILES***\n***END OF DATABASE FILES***\n')
    handle.write('***<Time><Tx/Rx><Channel><CAN ID><Type><DLC><DataBytes>***\n')
    base = when.hour * 3600 + when.minute * 60 + when.second + when.microsecond / 1e6
    for frame in frames:
        clock = base + frame.time
        tenths = int(round(clock * 10000))  # 1/10 ms
        seconds, fraction = divmod(tenths, 10000)
        handle.write('%02d:%02d:%02d:%04d %s %d 0x%03X %s %d %s\n' % (
            (seconds // 3600) % 24, (seconds // 60) % 60, seconds % 60, fraction, frame.direction,
            numbers[frame.channel], frame.id, 'x' if is_extended(frame) else 's', len(frame.data),
            _hex(frame.data)))
    handle.write('***END DATE AND TIME %s***\n***[STOP LOGGING SESSION]***\n' % when.strftime('%d:%m:%Y %H:%M:%S:000'))


def write_candump(handle, frames, environment, start):
    for frame in frames:
        identifier = ('%08X' if is_extended(frame) else '%03X') % frame.id
        if is_fd(frame):
            payload = '#%X%s' % (1, fd_data(frame).hex().upper())  # flags: BRS
        else:
            payload = frame.data.hex().upper()
        handle.write('(%.6f) %s %s#%s\n' % (start + frame.time, interface_name(frame.channel), identifier, payload))


def write_slcan(handle, frames, environment, start):
    for frame in frames:
        extended = is_extended(frame)
        if is_fd(frame):
            data = fd_data(frame)
            kind = 'D' if extended else 'd'
        else:
            data = frame.data
            kind = 'T' if extended else 't'
        identifier = ('%08X' if extended else '%03X') % frame.id
        millis = int(round(frame.time * 1000)) % 60000
        handle.write('%s%s%X%s%04X\r\n' % (kind, identifier, dlc_code(len(data)), data.hex().upper(), millis))


def write_csv(handle, frames, environment, start):
    handle.write('timestamp_s,epoch_timestamp,channel,can_id_hex,can_id_dec,direction,dlc,data_hex,fd\n')
    for frame in frames:
        handle.write('%.6f,%.6f,%s,0x%X,%d,%s,%d,%s,%d\n' % (
            frame.time, start + frame.time, frame.channel, frame.id, frame.id, frame.direction,
            dlc_code(len(frame.data)), _hex(frame.data), is_fd(frame)))


# ---------------------------------------------------------------- binary formats

def _socketcan(frame):
    """SocketCAN frame as in LINKTYPE_CAN_SOCKETCAN (big-endian ID)."""
    can_id = frame.id | (0x80000000 if is_extended(frame) else 0)
    if is_fd(frame):
        data = fd_data(frame)
        return struct.pack('>IBBxx', can_id, len(data), 0x05) + data + bytes(64 - len(data))  # BRS | FDF
    return struct.pack('>IBxxx', can_id, len(frame.data)) + frame.data + bytes(8 - len(frame.data))


def write_pcap(handle, frames, environment, start):
    handle.write(struct.pack('<IHHiIII', 0xA1B2C3D4, 2, 4, 0, 0, 65535, 227))
    for frame in frames:
        micros = int(round((start + frame.time) * 1e6))
        packet = _socketcan(frame)
        handle.write(struct.pack('<IIII', micros // 1000000, micros % 1000000, len(packet), len(packet)))
        handle.write(packet)


def _pcapng_block(kind, body):
    body += bytes(-len(body) % 4)
    return struct.pack('<II', kind, len(body) + 12) + body + struct.pack('<I', len(body) + 12)


def _pcapng_option(code, value):
    return struct.pack('<HH', code, len(value)) + value + bytes(-len(value) % 4)


def write_pcapng(handle, frames, environment, start):
    handle.write(_pcapng_block(0x0A0D0D0A, struct.pack('<IHHq', 0x1A2B3C4D, 1, 0, -1) +
                               _pcapng_option(4, b'CANtrix') + _pcapng_option(0, b'')))
    channels = sorted(set(frame.channel for frame in frames), key=lambda name: (not name.isdigit(), name.zfill(8)))
    interfaces = {}
    for index, channel in enumerate(channels):
        interfaces[channel] = index
        options = (_pcapng_option(2, interface_name(channel).encode('utf-8')) +
                   _pcapng_option(9, bytes([9])) + _pcapng_option(0, b''))  # name, ns resolution
        handle.write(_pcapng_block(1, struct.pack('<HHI', 227, 0, 0) + options))
    for frame in frames:
        nanos = int(round((start + frame.time) * 1e9))
        packet = _socketcan(frame)
        handle.write(_pcapng_block(6, struct.pack('<IIIII', interfaces[frame.channel], nanos >> 32,
                                                  nanos & 0xFFFFFFFF, len(packet), len(packet)) + packet))


def _systemtime(when):
    return struct.pack('<8H', when.year, when.month, (when.weekday() + 1) % 7, when.day, when.hour, when.minute,
                       when.second, when.microsecond // 1000)


BLF_CONTAINER_SIZE = 128 * 1024


def write_blf(handle, frames, environment, start):
    numbers = channel_numbers(frames)
    handle.write(bytes(144))  # file header, written at the end
    file_size = 144
    uncompressed_total = 144
    chunk = bytearray()
    count = 0

    def flush():
        nonlocal file_size, uncompressed_total
        if not chunk:
            return
        packed = zlib.compress(bytes(chunk), 6)
        size = 32 + len(packed)
        handle.write(struct.pack('<4sHHII', b'LOBJ', 16, 1, size, 10))
        handle.write(struct.pack('<H6xI4x', 2, len(chunk)))
        handle.write(packed + bytes(size % 4))
        file_size += size + size % 4
        uncompressed_total += len(chunk)
        del chunk[:]

    for frame in frames:
        stamp = int(round(frame.time * 1e9))
        identifier = frame.id | (0x80000000 if is_extended(frame) else 0)
        direction = 1 if frame.direction == 'Tx' else 0
        if is_fd(frame):
            data = fd_data(frame)
            body = struct.pack('<HBBIIBBB5x64s', numbers[frame.channel], direction, dlc_code(len(data)), identifier,
                               0, 0, 0x3, len(data), data)  # fd flags: EDL | BRS
            kind = 100
        else:
            body = struct.pack('<HBBI8s', numbers[frame.channel], direction, len(frame.data), identifier,
                               frame.data)
            kind = 1
        size = 32 + len(body)
        chunk.extend(struct.pack('<4sHHIIIHHQ', b'LOBJ', 32, 1, size, kind, 2, 0, 0, stamp))  # 2: ns
        chunk.extend(body + bytes(size % 4))
        count += 1
        if len(chunk) >= BLF_CONTAINER_SIZE:
            flush()
    flush()
    begin = _local(start)
    end = _local(start + (frames[-1].time if frames else 0))
    header = struct.pack('<4sIBBBBBBBBQQII', b'LOGG', 144, 0, 1, 0, 0, 4, 8, 1, 0, file_size,
                         uncompressed_total, count, 0) + _systemtime(begin) + _systemtime(end)
    handle.seek(0)
    handle.write(header + bytes(144 - len(header)))


# ---------------------------------------------------------------- ASAM MDF4 (bus logging)

class _MdfBuilder(object):
    """Collects MDF 4 blocks; links are given as block numbers and resolved when written."""

    def __init__(self):
        self.blocks = []  # [kind, links, data]

    def add(self, kind, links, data):
        self.blocks.append([kind, links, data])
        return len(self.blocks)  # reference = 1-based block number

    def text(self, text):
        return self.add('TX', [], text.encode('utf-8') + b'\0')

    def write(self, handle, first_offset):
        offsets = []
        position = first_offset
        for kind, links, data in self.blocks:
            offsets.append(position)
            size = 24 + 8 * len(links) + len(data)
            position += size + (-size % 8)
        for kind, links, data in self.blocks:
            resolved = [offsets[link - 1] if link else 0 for link in links]
            size = 24 + 8 * len(links) + len(data)
            handle.write(struct.pack('<4s4xQQ', ('##' + kind).encode('ascii'), size, len(links)))
            handle.write(struct.pack('<%dQ' % len(links), *resolved) + data + bytes(-size % 8))


def _mdf_channel(builder, name, cn_type, sync, data_type, byte_offset, bit_offset, bit_count, next_link=0,
                 composition=0, source=0, flags=0):
    data = struct.pack('<BBBBIIIIBBH6d', cn_type, sync, data_type, bit_offset, byte_offset, bit_count, flags, 0,
                       0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return builder.add('CN', [next_link, composition, builder.text(name), source, 0, 0, 0, 0], data)


def write_mf4(handle, frames, environment, start):
    size = 64 if any(is_fd(frame) for frame in frames) else 8
    record_size = 16 + size
    numbers = channel_numbers(frames)
    builder = _MdfBuilder()
    header = builder.add('HD', [], b'')  # must directly follow the ID block; filled in at the end
    # Record: Timestamp f64 | BusChannel u8 | ID u32 (bit 31 = IDE) | DLC u8 | DataLength u8 | Dir/EDL/BRS u8 | DataBytes
    children = 0
    for name, data_type, byte_offset, bit_offset, bits in reversed((
            ('CAN_DataFrame.BusChannel', 0, 8, 0, 8),
            ('CAN_DataFrame.ID', 0, 9, 0, 29),
            ('CAN_DataFrame.IDE', 0, 12, 7, 1),
            ('CAN_DataFrame.DLC', 0, 13, 0, 4),
            ('CAN_DataFrame.DataLength', 0, 14, 0, 7),
            ('CAN_DataFrame.Dir', 0, 15, 0, 1),
            ('CAN_DataFrame.EDL', 0, 15, 1, 1),
            ('CAN_DataFrame.BRS', 0, 15, 2, 1),
            ('CAN_DataFrame.DataBytes', 10, 16, 0, size * 8))):
        children = _mdf_channel(builder, name, 0, 0, data_type, byte_offset, bit_offset, bits, next_link=children)
    source = builder.add('SI', [builder.text('CAN'), builder.text('CAN'), 0], struct.pack('<BBB5x', 2, 2, 0))
    frame_channel = _mdf_channel(builder, 'CAN_DataFrame', 0, 0, 10, 8, 0, (8 + size) * 8, composition=children,
                                 source=source, flags=0x400)  # bus event
    master = _mdf_channel(builder, 'Timestamp', 2, 1, 4, 0, 0, 64, next_link=frame_channel)
    records = bytearray()
    for frame in frames:
        data = fd_data(frame) if is_fd(frame) else frame.data
        identifier = frame.id | (0x80000000 if is_extended(frame) else 0)
        flags = (1 if frame.direction == 'Tx' else 0) | (0x6 if is_fd(frame) else 0)
        records.extend(struct.pack('<dBIBBB', frame.time, numbers[frame.channel], identifier, dlc_code(len(data)),
                                   len(data), flags))
        records.extend(data + bytes(size - len(data)))
    data_block = builder.add('DT', [], bytes(records))
    group = builder.add('CG', [0, master, builder.text('CAN_DataFrame'), source, 0, 0],
                        struct.pack('<QQHH4xII', 0, len(frames), 0x2, ord('.'), record_size, 0))  # bus event
    data_group = builder.add('DG', [0, group, data_block, 0], struct.pack('<B7x', 0))
    comment = builder.add('MD', [], b'<FHcomment><TX>Saved by CANtrix</TX><tool_id>CANtrix</tool_id>'
                                    b'<tool_vendor>-</tool_vendor><tool_version>1.0</tool_version></FHcomment>\0')
    when = int(round(start * 1e9))
    history = builder.add('FH', [0, comment], struct.pack('<QhhB3x', when, 0, 0, 0))
    builder.blocks[header - 1] = ['HD', [data_group, history, 0, 0, 0, 0],
                                  struct.pack('<QhhBBBxdd', when, 0, 0, 0, 0, 0, 0.0, 0.0)]
    handle.write(b'MDF     4.10    CANAnlsr' + bytes(4) + struct.pack('<H', 410) + bytes(34))
    builder.write(handle, 64)


WRITERS = {'asc': write_asc, 'trc': write_trc, 'busmaster': write_busmaster, 'candump': write_candump,
           'slcan': write_slcan, 'csv': write_csv, 'blf': write_blf, 'mf4': write_mf4, 'pcap': write_pcap,
           'pcapng': write_pcapng}
BINARY = ('blf', 'mf4', 'pcap', 'pcapng')


def write_trace(path, format_key, frames, environment=None, start=0.0, errors=None):
    """Write frames to path in the given format (key of WRITE_FORMATS). The file is written to a
    temporary name first and replaces path only when complete."""
    writer = WRITERS[format_key]
    temporary = path + '.part'
    try:
        if format_key in BINARY:
            with open(temporary, 'wb') as handle:
                writer(handle, frames, environment, start)
        else:
            with open(temporary, 'w', encoding='utf-8', newline='') as handle:
                if format_key == 'asc':  # only ASC has CAN error frames
                    writer(handle, frames, environment, start, errors)
                else:
                    writer(handle, frames, environment, start)
        os.replace(temporary, path)
    except OSError as error:
        raise OSError(error.errno, error.strerror, path)  # name the target, not the temporary file
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
