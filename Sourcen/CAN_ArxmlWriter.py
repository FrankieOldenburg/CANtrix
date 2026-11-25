"""Export the CAN communication model as an AUTOSAR R22-11 ARXML file."""
import re
import xml.etree.ElementTree as ET

from CAN_Common import is_big_endian, signal_bit_numbers
from CAN_DbcEditor import EXTENDED_FLAG, check_number, check_whole, parse_id

NAMESPACE = 'http://autosar.org/schema/r4.0'


def _short_name(value, used, fallback):
    base = re.sub(r'[^a-zA-Z0-9_]', '_', value or fallback)
    if not base[0].isalpha():
        base = 'Item_' + base
    name = base
    number = 2
    while name in used:
        name = '%s_%d' % (base, number)
        number += 1
    used.add(name)
    return name


def _element(parent, tag, text=None, **attributes):
    element = ET.SubElement(parent, tag, attributes)
    element.text = text
    return element


def _named(parent, tag, name):
    element = _element(parent, tag)
    _element(element, 'SHORT-NAME', name)
    return element


def _properties(parent, tag):
    props = _element(parent, tag)
    variants = _element(props, 'SW-DATA-DEF-PROPS-VARIANTS')
    return _element(variants, 'SW-DATA-DEF-PROPS-CONDITIONAL')


def _packing(parent, big_endian=False):
    _element(parent, 'PACKING-BYTE-ORDER',
             'MOST-SIGNIFICANT-BYTE-FIRST' if big_endian else 'MOST-SIGNIFICANT-BYTE-LAST')


def export_arxml(messages):
    """Return UTF-8 ARXML bytes. The input is the shared list of parsed messages."""
    if not messages:
        raise ValueError('The database contains no CAN messages.')
    root = ET.Element('AUTOSAR', {
        'xmlns': NAMESPACE, 'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance',
        'xsi:schemaLocation': NAMESPACE + ' AUTOSAR_00051.xsd',
    })
    package = _named(_element(root, 'AR-PACKAGES'), 'AR-PACKAGE', 'CANtrix')
    system = _named(_element(package, 'ELEMENTS'), 'SYSTEM', 'CANCommunication')
    _element(system, 'CATEGORY', 'SYSTEM_DESCRIPTION')
    fibex = _element(system, 'FIBEX-ELEMENTS')
    _element(system, 'SYSTEM-VERSION', '1.0.0')
    packages = _element(package, 'AR-PACKAGES')
    package_names = ('CommunicationClusters', 'Frames', 'PDUs', 'DataTypes')
    elements = {name: _element(_named(packages, 'AR-PACKAGE', name), 'ELEMENTS') for name in package_names}
    signal_packages = {}

    def signal_elements(group, name):
        if group not in signal_packages:
            signal_packages[group] = _element(_named(packages, 'AR-PACKAGE', group), 'AR-PACKAGES')
        return _element(_named(signal_packages[group], 'AR-PACKAGE', name), 'ELEMENTS')

    def ref(parent, tag, dest, group, name):
        _element(parent, tag, '/CANtrix/%s/%s' % (group, name), DEST=dest)

    clusters, base_types, units = {}, {}, {}
    cluster_names, frame_names, type_names, trigger_names = set(), set(), set(), set()

    for message in messages:
        label = message.get('name') or 'Frame'
        try:
            raw = str(message.get('id') or '')
            token = re.match(r'^\s*(\d+)(?:\s|$)', raw) or re.search(r'(0x[0-9a-f]+)', raw, re.I)
            identifier = int(parse_id(token.group(1) if token else raw))
            address = identifier & 0x1FFFFFFF
            extended = bool(identifier & EXTENDED_FLAG or message.get('extended'))
            if identifier & ~(EXTENDED_FLAG | 0x1FFFFFFF):
                raise ValueError('Invalid extended CAN ID %s.' % identifier)
            length = check_whole(message.get('length'), 'The frame length')
            if length > 64:
                raise ValueError('The frame length must not exceed 64 bytes.')
        except ValueError as error:
            raise ValueError('%s: %s' % (label, error))

        bus = message.get('bus') or 'CAN'
        if bus not in clusters:
            cluster_name = _short_name(bus, cluster_names, 'CAN')
            cluster = _named(elements['CommunicationClusters'], 'CAN-CLUSTER', cluster_name)
            variant = _element(_element(cluster, 'CAN-CLUSTER-VARIANTS'), 'CAN-CLUSTER-CONDITIONAL')
            channel = _named(_element(variant, 'PHYSICAL-CHANNELS'), 'CAN-PHYSICAL-CHANNEL', 'Channel')
            cluster_ref = _element(fibex, 'FIBEX-ELEMENT-REF-CONDITIONAL')
            ref(cluster_ref, 'FIBEX-ELEMENT-REF', 'CAN-CLUSTER', 'CommunicationClusters', cluster_name)
            clusters[bus] = (cluster_name, _element(channel, 'FRAME-TRIGGERINGS'),
                             _element(channel, 'I-SIGNAL-TRIGGERINGS'), _element(channel, 'PDU-TRIGGERINGS'), set())
        cluster_name, frame_triggers, signal_triggers, pdu_triggers, used_ids = clusters[bus]
        if (address, extended) in used_ids:
            raise ValueError('%s: duplicate CAN ID 0x%X on %s.' % (label, address, bus))
        used_ids.add((address, extended))

        name = _short_name(label, frame_names, 'Frame')
        channel_path = cluster_name + '/Channel/'
        frame_trigger = _named(frame_triggers, 'CAN-FRAME-TRIGGERING', 'Frame_' + name)
        ref(frame_trigger, 'FRAME-REF', 'CAN-FRAME', 'Frames', name)
        link = _element(_element(frame_trigger, 'PDU-TRIGGERINGS'), 'PDU-TRIGGERING-REF-CONDITIONAL')
        ref(link, 'PDU-TRIGGERING-REF', 'PDU-TRIGGERING', 'CommunicationClusters', channel_path + 'Pdu_' + name)
        _element(frame_trigger, 'CAN-ADDRESSING-MODE', 'EXTENDED' if extended else 'STANDARD')
        behavior = 'CAN-FD' if message.get('fd') or length > 8 else 'CAN-20'
        _element(frame_trigger, 'CAN-FRAME-RX-BEHAVIOR', behavior)
        _element(frame_trigger, 'CAN-FRAME-TX-BEHAVIOR', behavior)
        _element(frame_trigger, 'IDENTIFIER', str(address))
        pdu_trigger = _named(pdu_triggers, 'PDU-TRIGGERING', 'Pdu_' + name)
        ref(pdu_trigger, 'I-PDU-REF', 'I-SIGNAL-I-PDU', 'PDUs', name)

        frame = _named(elements['Frames'], 'CAN-FRAME', name)
        _element(frame, 'FRAME-LENGTH', str(length))
        mapping = _named(_element(frame, 'PDU-TO-FRAME-MAPPINGS'), 'PDU-TO-FRAME-MAPPING', 'Payload')
        _packing(mapping)
        ref(mapping, 'PDU-REF', 'I-SIGNAL-I-PDU', 'PDUs', name)
        _element(mapping, 'START-POSITION', '0')
        pdu = _named(elements['PDUs'], 'I-SIGNAL-I-PDU', name)
        _element(pdu, 'LENGTH', str(length))
        if not message['signals']:
            _element(pdu, 'UNUSED-BIT-PATTERN', '0')
            continue
        mappings = _element(pdu, 'I-SIGNAL-TO-PDU-MAPPINGS')
        _element(pdu, 'UNUSED-BIT-PATTERN', '0')
        links = _element(pdu_trigger, 'I-SIGNAL-TRIGGERINGS')
        isignals = signal_elements('ISignals', name)
        system_signals = signal_elements('SystemSignals', name)
        mapping_names = set()
        for signal in message['signals']:
            label_signal = signal.get('name')
            mapping_name = _short_name(label_signal, mapping_names, 'Signal')
            signal_path = name + '/' + mapping_name
            qualified_name = name + '_' + mapping_name
            big_endian = is_big_endian(signal)
            try:
                start = check_whole(signal.get('start_bit'), 'The signal start bit')
                size = check_whole(signal.get('length'), 'The signal length', 1)
                if size > length * 8 or max(signal_bit_numbers(start, size, big_endian)) >= length * 8:
                    raise ValueError('The signal does not fit into the frame.')
                factor = signal.get('factor')
                offset = signal.get('offset')
                factor = check_number('1' if factor in (None, '') else factor, 'The signal factor')
                offset = check_number('0' if offset in (None, '') else offset, 'The signal offset')
                bounds = [(tag, check_number(signal[key], key))
                          for key, tag in (('minimum', 'LOWER-LIMIT'), ('maximum', 'UPPER-LIMIT'))
                          if signal.get(key) not in (None, '')]
            except ValueError as error:
                raise ValueError('%s / %s: %s' % (label, label_signal, error))

            isignal = _named(isignals, 'I-SIGNAL', mapping_name)
            _element(isignal, 'DATA-TYPE-POLICY', 'OVERRIDE')
            _element(isignal, 'LENGTH', str(size))
            if signal.get('signedness') in ('signed', 'unsigned'):
                signed = signal['signedness'] == 'signed'
                width = next((width for width in (8, 16, 32, 64) if width >= size), size)
                if (signed, width) not in base_types:
                    base_name = _short_name(('sint' if signed else 'uint') + str(width), type_names, 'BaseType')
                    base = _named(elements['DataTypes'], 'SW-BASE-TYPE', base_name)
                    _element(base, 'CATEGORY', 'FIXED_LENGTH')
                    _element(base, 'BASE-TYPE-SIZE', str(width))
                    _element(base, 'BASE-TYPE-ENCODING', '2C' if signed else 'NONE')
                    base_types[signed, width] = base_name
                network = _properties(isignal, 'NETWORK-REPRESENTATION-PROPS')
                ref(network, 'BASE-TYPE-REF', 'SW-BASE-TYPE', 'DataTypes', base_types[signed, width])
            ref(isignal, 'SYSTEM-SIGNAL-REF', 'SYSTEM-SIGNAL', 'SystemSignals', signal_path)
            sys_signal = _named(system_signals, 'SYSTEM-SIGNAL', mapping_name)
            _element(sys_signal, 'DYNAMIC-LENGTH', 'false')
            physical = _properties(sys_signal, 'PHYSICAL-PROPS')
            compu_name = _short_name('Compu_' + qualified_name, type_names, 'Compu')
            ref(physical, 'COMPU-METHOD-REF', 'COMPU-METHOD', 'DataTypes', compu_name)
            compu = _named(elements['DataTypes'], 'COMPU-METHOD', compu_name)
            _element(compu, 'CATEGORY', 'LINEAR')
            unit = signal.get('unit')
            if unit:
                if unit not in units:
                    unit_name = _short_name(unit, type_names, 'Unit')
                    _element(_named(elements['DataTypes'], 'UNIT', unit_name), 'DISPLAY-NAME', unit)
                    units[unit] = unit_name
                ref(compu, 'UNIT-REF', 'UNIT', 'DataTypes', units[unit])
            scales = _element(_element(compu, 'COMPU-INTERNAL-TO-PHYS'), 'COMPU-SCALES')
            rational = _element(_element(scales, 'COMPU-SCALE'), 'COMPU-RATIONAL-COEFFS')
            numerator = _element(rational, 'COMPU-NUMERATOR')
            _element(numerator, 'V', offset)
            _element(numerator, 'V', factor)
            _element(_element(rational, 'COMPU-DENOMINATOR'), 'V', '1')
            if bounds:
                constraint = _short_name('Bounds_' + qualified_name, type_names, 'Bounds')
                ref(physical, 'DATA-CONSTR-REF', 'DATA-CONSTR', 'DataTypes', constraint)
                rules = _element(_named(elements['DataTypes'], 'DATA-CONSTR', constraint), 'DATA-CONSTR-RULES')
                limits = _element(_element(rules, 'DATA-CONSTR-RULE'), 'PHYS-CONSTRS')
                for tag, value in bounds:
                    _element(limits, tag, value, **{'INTERVAL-TYPE': 'CLOSED'})

            mapping = _named(mappings, 'I-SIGNAL-TO-I-PDU-MAPPING', mapping_name)
            ref(mapping, 'I-SIGNAL-REF', 'I-SIGNAL', 'ISignals', signal_path)
            _packing(mapping, big_endian)
            _element(mapping, 'START-POSITION', str(start))
            trigger_name = _short_name('Signal_' + qualified_name, trigger_names, 'Signal')
            trigger = _named(signal_triggers, 'I-SIGNAL-TRIGGERING', trigger_name)
            ref(trigger, 'I-SIGNAL-REF', 'I-SIGNAL', 'ISignals', signal_path)
            link = _element(links, 'I-SIGNAL-TRIGGERING-REF-CONDITIONAL')
            ref(link, 'I-SIGNAL-TRIGGERING-REF', 'I-SIGNAL-TRIGGERING', 'CommunicationClusters',
                channel_path + trigger_name)

    # Indent using the standard library while supporting Python 3.6.
    def indent(node, depth=0):
        if len(node):
            node.text = '\n' + '  ' * (depth + 1)
            for child in node:
                indent(child, depth + 1)
                child.tail = node.text
            node[-1].tail = '\n' + '  ' * depth

    indent(root)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'
