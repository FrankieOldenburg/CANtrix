#!/usr/bin/env python3
"""Explanation of BLE advertising data, as far as the format is known.

Apple does not document its "Continuity" messages; their meaning here follows public reverse engineering
(furiousMAC/continuity) and can be incomplete or wrong for newer iOS versions. Everything else follows the
published formats (iBeacon, Eddystone, Bluetooth SIG services). Standard library only, Python 3.6.

explain(manufacturer, uuids, service_data) returns a list of entries (title, facts).
"""
from CAN_Language import tr

# Apple Continuity message types
APPLE_TYPES = {
    0x02: 'iBeacon', 0x03: 'AirPrint', 0x05: 'AirDrop', 0x06: 'HomeKit', 0x07: 'AirPods / Beats',
    0x08: 'Hey Siri', 0x09: 'AirPlay-Ziel', 0x0A: 'AirPlay-Quelle', 0x0B: 'Magic Switch', 0x0C: 'Handoff',
    0x0D: 'Instant Hotspot (Anfrage)', 0x0E: 'Instant Hotspot (Angebot)', 0x0F: 'Nearby Action',
    0x10: 'Nearby Info', 0x12: 'Wo ist? (Find My)',
}

# Nearby Info: action code (low nibble of the first byte)
APPLE_ACTIONS = {
    0x0: 'Aktivität unbekannt', 0x1: 'Aktivitätsmeldung abgeschaltet', 0x3: 'Ruhezustand, Bildschirm aus',
    0x5: 'Audiowiedergabe bei ausgeschaltetem Bildschirm', 0x7: 'Bildschirm an, wird benutzt',
    0x9: 'Videowiedergabe', 0xA: 'Uhr am Handgelenk und entsperrt', 0xB: 'kürzlich benutzt',
    0xD: 'Fahrer in einem Fahrzeug', 0xE: 'Telefonat oder FaceTime',
}

MICROSOFT_DEVICES = {
    1: 'Xbox', 6: 'iPhone', 7: 'iPad', 8: 'Android', 9: 'Windows-PC', 11: 'Windows Phone', 12: 'Linux',
    13: 'Windows IoT', 14: 'Surface Hub', 15: 'Windows-Laptop', 16: 'Windows-Tablet',
}

# 16 bit service UUIDs
SERVICES = {
    0x1800: 'Generic Access', 0x1801: 'Generic Attribute', 0x1802: 'Immediate Alert', 0x1803: 'Link Loss',
    0x1804: 'TX Power', 0x1805: 'Uhrzeit', 0x1809: 'Thermometer', 0x180A: 'Geräteinformationen',
    0x180D: 'Herzfrequenz', 0x180F: 'Batterie', 0x1810: 'Blutdruck', 0x1812: 'Eingabegerät (HID)',
    0x1816: 'Radfahren: Geschwindigkeit/Trittfrequenz', 0x1818: 'Radfahren: Leistung', 0x181A: 'Umweltsensor',
    0x181C: 'Benutzerdaten', 0x181D: 'Waage', 0x1826: 'Fitnessgerät', 0xFCD2: 'BTHome-Sensor',
    0xFD5A: 'Samsung SmartTag', 0xFD6F: 'Corona-Warn-App / Exposure Notification', 0xFE2C: 'Google Fast Pair',
    0xFE9F: 'Google', 0xFEAA: 'Eddystone', 0xFEED: 'Tile-Tracker',
}

EDDYSTONE_SCHEMES = ('http://www.', 'https://www.', 'http://', 'https://')
EDDYSTONE_ENDINGS = ('.com/', '.org/', '.edu/', '.net/', '.info/', '.biz/', '.gov/', '.com', '.org', '.edu',
                     '.net', '.info', '.biz', '.gov')


def _bytes(text):
    """Bytes of a hex text with blanks."""
    return bytearray.fromhex(text.replace(' ', ''))


def _hex(data):
    return ' '.join('%02x' % byte for byte in data)


def _number(data):
    value = 0
    for byte in data:
        value = value * 256 + byte
    return value


def _signed(byte):
    return byte - 256 if byte > 127 else byte


def _uuid(data):
    text = ''.join('%02x' % byte for byte in data).upper()
    return '-'.join((text[:8], text[8:12], text[12:16], text[16:20], text[20:]))


def short_uuid(uuid):
    """16 bit number of a Bluetooth SIG UUID, otherwise None."""
    text = uuid.lower()
    if len(text) == 36 and text.startswith('0000') and text.endswith('-0000-1000-8000-00805f9b34fb'):
        text = text[4:8]
    if len(text) != 4:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def service_name(uuid):
    name = SERVICES.get(short_uuid(uuid))
    return tr(name) if name else None


def _nearby_info(data):
    if not data:
        return []
    flags, action = data[0] >> 4, data[0] & 0x0F
    facts = [tr(APPLE_ACTIONS[action]) if action in APPLE_ACTIONS else tr('Aktion 0x%X') % action]
    if flags & 0x1:
        facts.append(tr('Hauptgerät der Apple-ID'))
    if flags & 0x4:
        facts.append(tr('AirDrop-Empfang an'))
    if len(data) > 1:
        info = data[1]
        facts.append(tr('WLAN an') if info & 0x04 else tr('WLAN aus'))
        for bit, text in ((0x20, 'Apple Watch gesperrt'), (0x40, 'Entsperren mit der Watch'),
                          (0x80, 'automatisches Entsperren aktiv')):
            if info & bit:
                facts.append(tr(text))
        if len(data) > 2:
            facts.append(tr('Kennung (wechselt): %s') % _hex(data[2:]))
    return facts


def _handoff(data):
    if len(data) < 4:
        return []
    # clipboard status, sequence number (little endian), authentication tag, encrypted data
    return [tr('Zwischenablage-Status 0x%02X') % data[0], tr('Zähler %s') % (data[1] + 256 * data[2]),
            tr('Inhalt verschlüsselt (%s Byte)') % (len(data) - 4)]


def _ibeacon(data):
    if len(data) < 21:
        return []
    return ['UUID %s' % _uuid(data[:16]), 'Major %s' % _number(data[16:18]),
            'Minor %s' % _number(data[18:20]), tr('%s dBm in 1 m Abstand') % _signed(data[20])]


def _find_my(data):
    if len(data) > 2:
        return [tr('vom Besitzer getrennt, sendet seinen Schlüssel')]
    return [tr('Besitzer in der Nähe')]


def _apple(data):
    """Apple data is a list of messages: type, length, value."""
    entries, index = [], 0
    decoders = {0x02: _ibeacon, 0x0C: _handoff, 0x10: _nearby_info, 0x12: _find_my}
    while index + 1 < len(data):
        kind, length = data[index], data[index + 1]
        value = data[index + 2:index + 2 + length]
        index += 2 + length
        name = tr(APPLE_TYPES[kind]) if kind in APPLE_TYPES else tr('Apple-Typ 0x%02X') % kind
        facts = decoders[kind](value) if kind in decoders and len(value) == length else []
        if len(value) < length:
            facts = [tr('unvollständig')]
        entries.append((name, facts))
    return entries


def _microsoft(data):
    if len(data) > 1 and data[0] == 0x01:
        device = data[1] & 0x1F
        name = tr(MICROSOFT_DEVICES[device]) if device in MICROSOFT_DEVICES else tr('Gerätetyp %s') % device
        return [(tr('Microsoft-Gerätekennung (Connected Devices)'), [name])]
    if data and data[0] == 0x03:
        return [('Microsoft Swift Pair', [tr('bietet Kopplung an')])]
    return []


def _eddystone(data):
    if not data:
        return []
    frame = data[0]
    if frame == 0x00 and len(data) >= 18:
        return [('Eddystone-UID', ['Namespace %s' % _hex(data[2:12]), 'Instance %s' % _hex(data[12:18])])]
    if frame == 0x10 and len(data) > 2:
        url = EDDYSTONE_SCHEMES[data[2]] if data[2] < len(EDDYSTONE_SCHEMES) else ''
        for byte in data[3:]:
            url += EDDYSTONE_ENDINGS[byte] if byte < len(EDDYSTONE_ENDINGS) else chr(byte)
        return [('Eddystone-URL', [url])]
    if frame == 0x20 and len(data) >= 14 and data[1] == 0:
        temperature = _signed(data[4]) + data[5] / 256.0
        return [('Eddystone-TLM', [tr('Batterie %s mV') % _number(data[2:4]),
                                   tr('Temperatur %.1f °C') % temperature,
                                   tr('läuft seit %s s') % (_number(data[10:14]) // 10)])]
    return [('Eddystone', [])]


def _service(uuid, data):
    number = short_uuid(uuid)
    if number == 0xFEAA:
        return _eddystone(data)
    name = service_name(uuid)
    if name is None:
        return []
    facts = []
    if number == 0x180F and data:
        facts.append(tr('Ladezustand %s %%') % data[0])
    return [(name, facts)]


def _printable(data):
    """Text contained in the data, if all of it is readable ASCII."""
    stripped = bytes(data).rstrip(b'\0')
    if len(stripped) >= 3 and all(32 <= byte < 127 for byte in bytearray(stripped)):
        return stripped.decode('ascii')
    return None


def explain(manufacturer=(), uuids=(), service_data=()):
    """manufacturer: (company ID, hex text); uuids: service UUIDs; service_data: (UUID, hex text)."""
    entries = []
    for company, text in manufacturer:
        data = _bytes(text)
        if company == 0x004C:
            found = _apple(data)
        elif company == 0x0006:
            found = _microsoft(data)
        else:
            found = []
        if not found and _printable(data):
            found = [(tr('Text'), [_printable(data)])]
        entries.extend(found)
    described = set()
    for uuid, text in service_data:
        found = _service(uuid, _bytes(text))
        if found:
            described.add(uuid)
            entries.extend(found)
    for uuid in uuids:
        name = service_name(uuid)
        if name and uuid not in described:
            entries.append((tr('Dienst'), [name]))
    return entries


def short_text(entries):
    """One line for the table."""
    parts = []
    for title, facts in entries:
        shown = [fact for fact in facts if not fact.startswith(tr('Kennung (wechselt): %s') % '')]
        parts.append('%s: %s' % (title, ', '.join(shown)) if shown else title)
    return ' | '.join(parts)


def long_lines(entries):
    return ['%s: %s' % (title, ', '.join(facts)) if facts else title for title, facts in entries]
