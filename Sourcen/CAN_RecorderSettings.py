"""Connection settings shared by the Trace Recorder dialog and CAN adapters."""

DEFAULT_CONNECTION = {
    'interface': 'vector', 'channel': 0, 'bitrate': 500000, 'serial': None,
    'data_bitrate': 2000000, 'fd': True, 'app_name': 'CANoe',
    'port': '', 'serial_baud': 115200,
}
INTERFACES = {'vector': 'Vector (VN, VX, VH, …)', 'usbtingo': 'USBtingo', 'obdlink': 'OBDLink / ELM327'}
BITRATES = [125000, 250000, 500000, 1000000]
DATA_BITRATES = [1000000, 2000000, 4000000, 5000000, 8000000]
TIMING_FIELDS = ('sjw_abr', 'tseg1_abr', 'tseg2_abr', 'sjw_dbr', 'tseg1_dbr', 'tseg2_dbr')


def vector_fd_timing(bitrate, data_bitrate):
    """Derive the CANcase timing for an 80 MHz Vector clock, or return an empty dict."""
    if bitrate <= 0 or data_bitrate <= 0:
        return {}
    for prescaler in range(1, 65):
        arb_total, arb_rest = divmod(80000000, bitrate * prescaler)
        data_total, data_rest = divmod(80000000, data_bitrate * prescaler)
        if arb_rest or data_rest or not 3 <= arb_total <= 80 or data_total < 10:
            continue
        arb_tseg1 = round(arb_total * 0.813) - 1
        arb_tseg2 = arb_total - 1 - arb_tseg1
        data_tseg1 = round(data_total * 0.60) - 1
        data_tseg2 = data_total - 1 - data_tseg1
        if min(arb_tseg1, arb_tseg2, data_tseg1, data_tseg2) >= 1:
            return dict(zip(TIMING_FIELDS, (arb_tseg2, arb_tseg1, arb_tseg2, data_tseg2, data_tseg1, data_tseg2)))
    return {}


def normalize_connection(saved=None):
    """Load defaults and preserve explicit settings from older versions."""
    saved = saved if isinstance(saved, dict) else {}
    connection = dict(DEFAULT_CONNECTION)
    if saved.get('interface') in tuple(INTERFACES):
        connection['interface'] = saved['interface']
    for key in ('channel', 'bitrate', 'data_bitrate'):
        value = saved.get(key)
        if type(value) is int and value >= (0 if key == 'channel' else 1):
            connection[key] = value
    if isinstance(saved.get('fd'), bool):
        connection['fd'] = saved['fd']
    if isinstance(saved.get('port'), str):
        connection['port'] = saved['port'].strip()
    if type(saved.get('serial_baud')) is int and saved['serial_baud'] > 0:
        connection['serial_baud'] = saved['serial_baud']
    serial = saved.get('serial')
    if connection['interface'] == 'usbtingo':
        connection['channel'] = 0
        if isinstance(serial, str):
            connection['serial'] = serial.strip() or None
    elif connection['interface'] == 'obdlink':
        connection.update(channel=0, fd=False)
    elif type(serial) is int and serial >= 0:
        connection['serial'] = serial
    elif isinstance(serial, str) and serial.strip().isdecimal():
        connection['serial'] = int(serial.strip())
    if isinstance(saved.get('app_name'), str):
        connection['app_name'] = saved['app_name'].strip() or DEFAULT_CONNECTION['app_name']
    timing = saved.get('timing')
    if isinstance(timing, dict) and all(type(timing.get(key)) is int and timing[key] > 0 for key in TIMING_FIELDS):
        connection['timing'] = {key: timing[key] for key in TIMING_FIELDS}
    else:
        connection['timing'] = vector_fd_timing(connection['bitrate'], connection['data_bitrate'])
    return connection
