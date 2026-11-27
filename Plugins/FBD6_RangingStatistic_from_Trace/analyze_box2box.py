"""Standalone version of the report chapter "Box2Box Write Window".

Copied from analyze_byte_pairs.py. Expects analyze_byte_pairs_cfg.json,
Bindingkeys.txt and AesGcm.cin beside this script.

Usage: python analyze_box2box.py TRACE.asc|TRACE.blf [OUTPUT.html]
       python analyze_box2box.py --live [--show-results]

--live reads ASC lines from stdin (CANtrix plugin in live mode) and prints every
result at once, coloured with ANSI codes; the end of stdin prints the statistics.
"""
import os
import re
import html
import json
import argparse
import sys
import tempfile
import secrets
import webbrowser
from pathlib import Path
from contextlib import contextmanager
from decimal import Decimal, localcontext, ROUND_HALF_UP
from collections import Counter


_HTML_TOC_HEADERS = [
    ("CANFD Byte Pair Analysis Tool (Byte 16 & 17)", "Report Header"),
    ("OVERALL BYTE PAIR SUMMARY (sorted by frequency, descending):", "Byte Pair Summary"),
    ("STATUS BYTE OVERVIEW (Byte 16, 17, 18, 19 = Status1, Status2, Status3, Status4):", "Status Byte Overview"),
    ("SPECIAL ANALYSIS: BYTE 19 FOR STATUS1/STATUS2 = 0xFD/0x01", "Status 0xFD/0x01"),
    ("BYTE PAIR DISTRIBUTION BY CAN ID:", "Byte Pair Distribution"),
    ("DEVIATION ANALYSIS (Sorted by Variability - High to Low):", "Deviation Analysis"),
    ("BYTE PAIR SIMILARITY ACROSS CAN IDs (Shared Values Analysis):", "Byte Pair Similarity"),
]
_RESET_OVERVIEW_HEADER_TEXT = "RESET OVERVIEW BY POSITION (Byte1 ResetState = 0x01, Byte2 ResetReason):"
_CONTROL_RANGING_SESSION_ID = '192'
_CONTROL_RANGING_SESSION_NAME = 'ControlRangingSession'
_CONTROL_RANGING_SESSION_SETUP1_ID = '190'
_CONTROL_RANGING_SESSION_SETUP1_NAME = 'ControlRangingSessionSetup1'
_CONTROL_RANGING_TARGET_IDS = {_CONTROL_RANGING_SESSION_ID, _CONTROL_RANGING_SESSION_SETUP1_ID}
_RANGING_FUNCTION_MEANINGS = {
    '01': 'Start',
    '02': 'Suspend',
    '03': 'Resume',
    '04': 'Stop',
    '05': 'Delete',
}
_RESUME_RANGING_SESSION_ANCHOR_NAMES = {
    '1e0': 'VM',
    '1e1': 'HM',
    '1e2': 'Miko',
    '1e3': 'Trunk',
    '1e4': 'VL',
    '1e5': 'VR',
    '1e6': 'HL',
    '1e7': 'HR',
}
_RESUME_RANGING_SESSION_TARGET_IDS = set(_RESUME_RANGING_SESSION_ANCHOR_NAMES)
_STATUS_UWB_RANGING_FDD2_NAMES = {
    '1d2': 'HL',
    '1cd': 'HM',
    '1d3': 'HR',
    '1ce': 'Miko',
    '1cf': 'Trunk',
    '1d0': 'VL',
    '1cc': 'VM',
    '1d1': 'VR',
}
_STATUS_UWB_RANGING_FDD2_IDS = set(_STATUS_UWB_RANGING_FDD2_NAMES)
_STATUS_UWB_RANGING_FDD1_TO_FDD2 = {
    '1c8': '1d2',
    '1c3': '1cd',
    '1c9': '1d3',
    '1c4': '1ce',
    '1c5': '1cf',
    '1c6': '1d0',
    '1c2': '1cc',
    '1c7': '1d1',
}
_STATUS_UWB_RANGING_FDD1_IDS = set(_STATUS_UWB_RANGING_FDD1_TO_FDD2)
_TIME_SYNC_DEVICE_OFFSET_NAMES = {
    '74': 'HL',
    '6f': 'HM',
    '75': 'HR',
    '70': 'Miko',
    '71': 'Trunk',
    '72': 'VL',
    '6e': 'VM',
    '73': 'VR',
}
_TIME_SYNC_DEVICE_OFFSET_IDS = set(_TIME_SYNC_DEVICE_OFFSET_NAMES)
_BLE_DATEN_IPB_TARGET_IDS = {format(msg_id, 'x') for msg_id in range(0x1fe, 0x209 + 1)}
_BLE_DATEN_IPB_COMMAND_BYTE = '13'
_BLE_DATEN_IPB_KNOWN_NAMES = {
    '1fe': 'VM',
    '1ff': 'HM',
    '200': 'MiKo',
    '201': 'Trunk',
    '202': 'VL',
    '203': 'VR',
    '204': 'HL',
    '205': 'HR',
}
_BLE_OPERATION_MODE_ANCHOR_NAMES = {
    'd2': 'VM',
    'd3': 'HM',
    'd6': 'VL',
    'd7': 'VR',
    'd8': 'HL',
    'd9': 'HR',
}
_BLE_OPERATION_MODE_TARGET_IDS = set(_BLE_OPERATION_MODE_ANCHOR_NAMES)
_BLE_OPERATION_MODE_MEANINGS = {
    '00': 'no change',
    '01': 'Scanning',
    '02': 'Advertising smart access passive entry',
    '03': 'Deactivated',
    '04': 'Advertising Owner Pairing without user',
    '05': 'Advertising Owner Pairing with user',
    '06': 'Advertising Car Sharing',
    'FD': 'nicht_verfuegbar',
    'FE': 'Funktion_meldet_Fehler',
    'FF': 'Signal_unbefuellt',
}
_STATUS_RANGING_INTENT_ANCHOR_NAMES = {
    'ec': 'HL',
    'e7': 'HM',
    'ed': 'HR',
    'e8': 'Miko',
    'e9': 'Trunk',
    'ea': 'VL',
    'eb': 'VR',
}
_STATUS_RANGING_INTENT_TARGET_IDS = set(_STATUS_RANGING_INTENT_ANCHOR_NAMES)
_STATUS_FBD_ANCHOR_NAMES = {
    '96': 'VM',
    '97': 'HM',
    '98': 'Miko',
    '99': 'Trunk',
    '9a': 'VL',
    '9b': 'VR',
    '9c': 'HL',
    '9d': 'HR',
}
_STATUS_FBD_TARGET_IDS = set(_STATUS_FBD_ANCHOR_NAMES)
_STATUS_CPD_DATA3_TARGET_IDS = {
    format(msg_id, 'x') for msg_id in range(0x15e, 0x168)
} | {'343'}
_STATUS_CPD_DATA4_TARGET_IDS = {
    format(msg_id, 'x') for msg_id in range(0x168, 0x172)
} | {'344'}
_CONTROL_CONFIGURATION_FBD_TARGET_IDS = {'258'}
_WAKE_UP_FBD_ONLY_MODE_NAMES = {
    'a0': 'VM', 'a1': 'HM', 'a2': 'Miko', 'a3': 'Trunk', 'a4': 'VL', 'a5': 'VR',
    'a6': 'HL', 'a7': 'HR', 'a8': 'SML', 'a9': 'SMR', 'b4': 'MM',
}
_STATUS_RANGING_INTENT_MESSAGE_NAMES = {
    'e6': 'VM', 'e7': 'HM', 'e8': 'Miko', 'e9': 'Trunk', 'ea': 'VL', 'eb': 'VR',
    'ec': 'HL', 'ed': 'HR', 'ee': 'SML', 'ef': 'SMR', 'f2': 'MM',
}
_POWER_MODE_MEANINGS = {
    '00': 'NA',
    '01': 'INIT',
    '02': 'SLEEP',
    '03': 'NORMAL',
    '04': 'WITHOUT_FBD_ONLY',
    '05': 'WITH_FBD_ONLY',
    '06': 'RESET_RQ',
    '07': 'POWERDOWN',
    '08': 'POSTINIT',
    '09': 'SLEEP_PREPARE',
    '0A': 'POWERDOWN_PREPARE',
}
_CONTROL_FBD_ID = '8c'
_CONTROL_FBD_TARGET_IDS = {_CONTROL_FBD_ID}
_STATUS_UWB_RANGING_ANCHOR_NAMES = {
    '1ae': 'VM',
    '1af': 'HM',
    '1b0': 'Miko',
    '1b1': 'Trunk',
    '1b2': 'VL',
    '1b3': 'VR',
    '1b4': 'HL',
    '1b5': 'HR',
}
_STATUS_UWB_RANGING_TARGET_IDS = set(_STATUS_UWB_RANGING_ANCHOR_NAMES)
_TIME_SYNC_ANCHOR_NAMES = {
    '64': 'VM',
    '65': 'HM',
    '66': 'Miko',
    '67': 'Trunk',
    '68': 'VL',
    '69': 'VR',
    '6a': 'HL',
    '6b': 'HR',
}
_TIME_SYNC_TARGET_IDS = set(_TIME_SYNC_ANCHOR_NAMES)
_STATUS_TIME_SYNC_ANCHOR_NAMES = {
    '78': 'VM',
    '79': 'HM',
    '7a': 'Miko',
    '7b': 'Trunk',
    '7c': 'VL',
    '7d': 'VR',
    '7e': 'HL',
    '7f': 'HR',
}
_STATUS_TIME_SYNC_TARGET_IDS = set(_STATUS_TIME_SYNC_ANCHOR_NAMES)
_TARGET_CANFD_BUS_NAME = 'CAN_FD_IPB_41'
_ASC_CANFD_CHANNEL_FILTER = None
_CAN_ID_MAPPING = {
    '1ae': 'VM', '1af': 'HM', '1b0': 'Miko', '1b1': 'Trunk',
    '1b2': 'VL', '1b3': 'VR', '1b4': 'HL', '1b5': 'HR',
}
_RESET_CAN_ID_TO_POSITION = {
    '96': 'VM', '97': 'HM', '98': 'Miko', '99': 'Trunk',
    '9a': 'VL', '9b': 'VR', '9c': 'HL', '9d': 'HR',
}
_LOCALIZATION_CAN_ID = '3e7'
_BYTE_INDICES = {
    'primary_pair': [16, 17],
    'status_bytes': [16, 17, 18, 19],
    'reset_state_reason': [1, 2],
    'session_id': [0],
    'session_irk': [0, 1],
    'timestamp_poll': [0] + list(range(29, 37)),
    'ranging_round': [0, 5, 6],
    'ranging_round_responder': [0, 5, 6, 18],
    'localization_result': [8, 6],
    'localization_timeline': [8, 6, 1, 2, 3, 4],
    'localization_details': [8, 1, 2, 3, 4, 6, 7, 12, 13, 15, 16],
    'ranging_block_index': [0, 3, 4],
    'status_error_states': [15, 21, 27],
    'status_power_mode': [0],
    'control_power_mode': [2],
    'time_sync': [0, 1, 2, 3],
    'status_time_sync': [0, 1],
    'ble_connections': [9],
    'ble_operation_mode': [0],
    'ranging_intent': [0, 1],
}
_PAYLOAD_INDICES = {
    'ble_daten_header': [0, 1, 2],
    'ble_daten_devices_start': 3,
    'control_ranging_function': 2,
    'control_ranging_session': 3,
    'control_ranging_setup_session': 2,
    'resume_ranging_function': 1,
    'resume_ranging_session': 2,
}
_PROJECT_MEANING_OVERRIDES = {}
_ROUND_SKIPPED_STATUS_PAIR = ('FD', '01')
_RESET_ACTIVE_STATE = '01'
_SUCCESSFUL_STATUS_PAIR = ('00', '00')
_ERROR_STATUS1 = 'FE'
_FALLBACK_STATUS1 = 'E1'
_GROUPED_STATUS1_VALUES = {'FB', 'E1', 'FC'}
_STATUS_ERROR_IGNORE_VALUES = {'00', 'FF'}
_NOT_AVAILABLE_VALUE = 'FF'
_USE_BUILTIN_MEANINGS = True


def _uppercase_keys(values):
    return {str(key).upper(): value for key, value in values.items()}


def _apply_project_config(config_path):
    """Load project-specific CAN IDs, payload layouts, and decoded meanings."""
    global _TARGET_CANFD_BUS_NAME, _CAN_ID_MAPPING, _RESET_CAN_ID_TO_POSITION
    global _CONTROL_RANGING_SESSION_ID, _CONTROL_RANGING_SESSION_NAME
    global _CONTROL_RANGING_SESSION_SETUP1_ID, _CONTROL_RANGING_SESSION_SETUP1_NAME
    global _CONTROL_RANGING_TARGET_IDS, _RANGING_FUNCTION_MEANINGS
    global _RESUME_RANGING_SESSION_ANCHOR_NAMES, _RESUME_RANGING_SESSION_TARGET_IDS
    global _STATUS_UWB_RANGING_FDD2_NAMES, _STATUS_UWB_RANGING_FDD2_IDS
    global _STATUS_UWB_RANGING_FDD1_TO_FDD2, _STATUS_UWB_RANGING_FDD1_IDS
    global _TIME_SYNC_DEVICE_OFFSET_NAMES, _TIME_SYNC_DEVICE_OFFSET_IDS
    global _BLE_DATEN_IPB_TARGET_IDS, _BLE_DATEN_IPB_COMMAND_BYTE
    global _BLE_DATEN_IPB_KNOWN_NAMES, _BLE_OPERATION_MODE_ANCHOR_NAMES
    global _BLE_OPERATION_MODE_TARGET_IDS, _BLE_OPERATION_MODE_MEANINGS
    global _STATUS_RANGING_INTENT_ANCHOR_NAMES, _STATUS_RANGING_INTENT_TARGET_IDS
    global _STATUS_FBD_ANCHOR_NAMES, _STATUS_FBD_TARGET_IDS, _POWER_MODE_MEANINGS
    global _STATUS_CPD_DATA3_TARGET_IDS, _STATUS_CPD_DATA4_TARGET_IDS
    global _CONTROL_CONFIGURATION_FBD_TARGET_IDS
    global _WAKE_UP_FBD_ONLY_MODE_NAMES, _STATUS_RANGING_INTENT_MESSAGE_NAMES
    global _CONTROL_FBD_ID, _CONTROL_FBD_TARGET_IDS
    global _STATUS_UWB_RANGING_ANCHOR_NAMES, _STATUS_UWB_RANGING_TARGET_IDS
    global _TIME_SYNC_ANCHOR_NAMES, _TIME_SYNC_TARGET_IDS
    global _STATUS_TIME_SYNC_ANCHOR_NAMES, _STATUS_TIME_SYNC_TARGET_IDS
    global _LOCALIZATION_CAN_ID, _LOCALIZATION_ZONES, _RANGING_INTENT_MARKER
    global _BYTE_INDICES, _PAYLOAD_INDICES, _PROJECT_MEANING_OVERRIDES
    global _ROUND_SKIPPED_STATUS_PAIR, _RESET_ACTIVE_STATE
    global _SUCCESSFUL_STATUS_PAIR, _ERROR_STATUS1, _FALLBACK_STATUS1
    global _GROUPED_STATUS1_VALUES
    global _STATUS_ERROR_IGNORE_VALUES, _NOT_AVAILABLE_VALUE
    global _USE_BUILTIN_MEANINGS
    global _HTML_TOC_HEADERS, _RESET_OVERVIEW_HEADER_TEXT

    with open(config_path, 'r', encoding='utf-8') as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise ValueError('The project config root must be a JSON object.')

    can_ids = config.get('can_ids', {})
    names = config.get('names', {})
    byte_indices = config.get('byte_indices', {})
    meanings = config.get('meanings', {})
    _USE_BUILTIN_MEANINGS = config.get('use_builtin_meanings', True)
    if not isinstance(_USE_BUILTIN_MEANINGS, bool):
        raise ValueError('use_builtin_meanings must be true or false.')
    _TARGET_CANFD_BUS_NAME = config.get('canfd_bus_name', _TARGET_CANFD_BUS_NAME)
    _CAN_ID_MAPPING = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('primary', _CAN_ID_MAPPING).items()
    }
    _RESET_CAN_ID_TO_POSITION = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('reset', _RESET_CAN_ID_TO_POSITION).items()
    }

    _CONTROL_RANGING_SESSION_ID = _normalize_id(
        can_ids.get('control_ranging_session', _CONTROL_RANGING_SESSION_ID)
    )
    _CONTROL_RANGING_SESSION_SETUP1_ID = _normalize_id(
        can_ids.get('control_ranging_session_setup1', _CONTROL_RANGING_SESSION_SETUP1_ID)
    )
    _CONTROL_RANGING_SESSION_NAME = names.get(
        'control_ranging_session', _CONTROL_RANGING_SESSION_NAME
    )
    _CONTROL_RANGING_SESSION_SETUP1_NAME = names.get(
        'control_ranging_session_setup1', _CONTROL_RANGING_SESSION_SETUP1_NAME
    )
    _CONTROL_RANGING_TARGET_IDS = {
        _CONTROL_RANGING_SESSION_ID, _CONTROL_RANGING_SESSION_SETUP1_ID
    }

    _RESUME_RANGING_SESSION_ANCHOR_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('resume_ranging_session', _RESUME_RANGING_SESSION_ANCHOR_NAMES).items()
    }
    _RESUME_RANGING_SESSION_TARGET_IDS = set(_RESUME_RANGING_SESSION_ANCHOR_NAMES)

    _STATUS_UWB_RANGING_FDD2_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('status_uwb_ranging_fdd2', _STATUS_UWB_RANGING_FDD2_NAMES).items()
    }
    _STATUS_UWB_RANGING_FDD2_IDS = set(_STATUS_UWB_RANGING_FDD2_NAMES)
    _STATUS_UWB_RANGING_FDD1_TO_FDD2 = {
        _normalize_id(source): _normalize_id(target)
        for source, target in can_ids.get('status_uwb_ranging_fdd1_to_fdd2', _STATUS_UWB_RANGING_FDD1_TO_FDD2).items()
    }
    _STATUS_UWB_RANGING_FDD1_IDS = set(_STATUS_UWB_RANGING_FDD1_TO_FDD2)
    _TIME_SYNC_DEVICE_OFFSET_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('time_sync_device_offset', _TIME_SYNC_DEVICE_OFFSET_NAMES).items()
    }
    _TIME_SYNC_DEVICE_OFFSET_IDS = set(_TIME_SYNC_DEVICE_OFFSET_NAMES)

    _BLE_DATEN_IPB_TARGET_IDS = {
        _normalize_id(can_id)
        for can_id in can_ids.get('ble_daten_ipb', sorted(_BLE_DATEN_IPB_TARGET_IDS))
    }
    _BLE_DATEN_IPB_COMMAND_BYTE = config.get(
        'ble_daten_ipb_command_byte', _BLE_DATEN_IPB_COMMAND_BYTE
    ).upper()
    _BLE_DATEN_IPB_KNOWN_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('ble_daten_ipb_names', _BLE_DATEN_IPB_KNOWN_NAMES).items()
    }
    _BLE_OPERATION_MODE_ANCHOR_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('ble_operation_mode', _BLE_OPERATION_MODE_ANCHOR_NAMES).items()
    }
    _BLE_OPERATION_MODE_TARGET_IDS = set(_BLE_OPERATION_MODE_ANCHOR_NAMES)
    _STATUS_RANGING_INTENT_ANCHOR_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('status_ranging_intent', _STATUS_RANGING_INTENT_ANCHOR_NAMES).items()
    }
    _STATUS_RANGING_INTENT_TARGET_IDS = set(_STATUS_RANGING_INTENT_ANCHOR_NAMES)
    _STATUS_FBD_ANCHOR_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('status_fbd', _STATUS_FBD_ANCHOR_NAMES).items()
    }
    _STATUS_FBD_TARGET_IDS = set(_STATUS_FBD_ANCHOR_NAMES)
    _STATUS_CPD_DATA3_TARGET_IDS = {
        _normalize_id(can_id)
        for can_id in can_ids.get('status_cpd_data3', sorted(_STATUS_CPD_DATA3_TARGET_IDS))
    }
    _STATUS_CPD_DATA4_TARGET_IDS = {
        _normalize_id(can_id)
        for can_id in can_ids.get('status_cpd_data4', sorted(_STATUS_CPD_DATA4_TARGET_IDS))
    }
    _CONTROL_CONFIGURATION_FBD_TARGET_IDS = {
        _normalize_id(can_id)
        for can_id in can_ids.get(
            'control_configuration_fbd', sorted(_CONTROL_CONFIGURATION_FBD_TARGET_IDS)
        )
    }
    _WAKE_UP_FBD_ONLY_MODE_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get(
            'wake_up_fbd_only_mode', _WAKE_UP_FBD_ONLY_MODE_NAMES
        ).items()
    }
    _STATUS_RANGING_INTENT_MESSAGE_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get(
            'status_ranging_intent_message_names', _STATUS_RANGING_INTENT_MESSAGE_NAMES
        ).items()
    }
    builtin_power_modes = _POWER_MODE_MEANINGS if _USE_BUILTIN_MEANINGS else {}
    _POWER_MODE_MEANINGS = {
        **builtin_power_modes, **_uppercase_keys(meanings.get('power_modes', {}))
    }
    _CONTROL_FBD_ID = _normalize_id(can_ids.get('control_fbd', _CONTROL_FBD_ID))
    _CONTROL_FBD_TARGET_IDS = {_CONTROL_FBD_ID}
    _STATUS_UWB_RANGING_ANCHOR_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('status_uwb_ranging', _STATUS_UWB_RANGING_ANCHOR_NAMES).items()
    }
    _STATUS_UWB_RANGING_TARGET_IDS = set(_STATUS_UWB_RANGING_ANCHOR_NAMES)
    _TIME_SYNC_ANCHOR_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('time_sync', _TIME_SYNC_ANCHOR_NAMES).items()
    }
    _TIME_SYNC_TARGET_IDS = set(_TIME_SYNC_ANCHOR_NAMES)
    _STATUS_TIME_SYNC_ANCHOR_NAMES = {
        _normalize_id(can_id): name
        for can_id, name in can_ids.get('status_time_sync', _STATUS_TIME_SYNC_ANCHOR_NAMES).items()
    }
    _STATUS_TIME_SYNC_TARGET_IDS = set(_STATUS_TIME_SYNC_ANCHOR_NAMES)
    _LOCALIZATION_CAN_ID = _normalize_id(
        can_ids.get('localization', _LOCALIZATION_CAN_ID)
    )
    if config.get('replace_localization_zones', False):
        _LOCALIZATION_ZONES = dict(config.get('localization_zones', {}))
    else:
        _LOCALIZATION_ZONES = {**_LOCALIZATION_ZONES, **config.get('localization_zones', {})}
    _RANGING_INTENT_MARKER = config.get('ranging_intent_marker', _RANGING_INTENT_MARKER)
    builtin_ranging_functions = _RANGING_FUNCTION_MEANINGS if _USE_BUILTIN_MEANINGS else {}
    _RANGING_FUNCTION_MEANINGS = {
        **builtin_ranging_functions,
        **_uppercase_keys(meanings.get('ranging_functions', {})),
    }
    builtin_ble_modes = _BLE_OPERATION_MODE_MEANINGS if _USE_BUILTIN_MEANINGS else {}
    _BLE_OPERATION_MODE_MEANINGS = {
        **builtin_ble_modes,
        **_uppercase_keys(meanings.get('ble_operation_modes', {})),
    }
    _BYTE_INDICES = {**_BYTE_INDICES, **byte_indices}
    _PAYLOAD_INDICES = {
        **_PAYLOAD_INDICES, **config.get('payload_indices', {})
    }
    _PROJECT_MEANING_OVERRIDES = meanings
    status_codes = config.get('status_codes', {})
    _ROUND_SKIPPED_STATUS_PAIR = tuple(
        value.upper()
        for value in status_codes.get('round_skipped_pair', _ROUND_SKIPPED_STATUS_PAIR)
    )
    _RESET_ACTIVE_STATE = status_codes.get('reset_active_state', _RESET_ACTIVE_STATE).upper()
    _SUCCESSFUL_STATUS_PAIR = tuple(
        value.upper()
        for value in status_codes.get('successful_pair', _SUCCESSFUL_STATUS_PAIR)
    )
    _ERROR_STATUS1 = status_codes.get('error_status1', _ERROR_STATUS1).upper()
    _FALLBACK_STATUS1 = status_codes.get('fallback_status1', _FALLBACK_STATUS1).upper()
    _GROUPED_STATUS1_VALUES = {
        value.upper()
        for value in status_codes.get('grouped_status1_values', _GROUPED_STATUS1_VALUES)
    }
    _STATUS_ERROR_IGNORE_VALUES = {
        value.upper()
        for value in status_codes.get('error_state_ignore_values', _STATUS_ERROR_IGNORE_VALUES)
    }
    _NOT_AVAILABLE_VALUE = status_codes.get(
        'not_available_value', _NOT_AVAILABLE_VALUE
    ).upper()

    primary_bytes = _BYTE_INDICES['primary_pair']
    status_bytes = _BYTE_INDICES['status_bytes']
    reset_bytes = _BYTE_INDICES['reset_state_reason']
    pair_text = f"0x{_ROUND_SKIPPED_STATUS_PAIR[0]}/0x{_ROUND_SKIPPED_STATUS_PAIR[1]}"
    _HTML_TOC_HEADERS = [
        (f'CANFD Byte Pair Analysis Tool (Byte {primary_bytes[0]} & {primary_bytes[1]})', 'Report Header'),
        ('OVERALL BYTE PAIR SUMMARY (sorted by frequency, descending):', 'Byte Pair Summary'),
        (f'STATUS BYTE OVERVIEW (Byte {", ".join(map(str, status_bytes))} = Status1, Status2, Status3, Status4):', 'Status Byte Overview'),
        (
            f'SPECIAL ANALYSIS: BYTE {status_bytes[3]} FOR STATUS1/STATUS2 = {pair_text}',
            f'Status {_ROUND_SKIPPED_STATUS_PAIR[0]}/{_ROUND_SKIPPED_STATUS_PAIR[1]}',
        ),
        ('BYTE PAIR DISTRIBUTION BY CAN ID:', 'Byte Pair Distribution'),
        ('DEVIATION ANALYSIS (Sorted by Variability - High to Low):', 'Deviation Analysis'),
        ('BYTE PAIR SIMILARITY ACROSS CAN IDs (Shared Values Analysis):', 'Byte Pair Similarity'),
    ]
    _RESET_OVERVIEW_HEADER_TEXT = (
        f'RESET OVERVIEW BY POSITION (Byte{reset_bytes[0]} ResetState = '
        f'0x{_RESET_ACTIVE_STATE}, Byte{reset_bytes[1]} ResetReason):'
    )


def _resolve_canfd_channels(input_file, bus_name):
    """Return (channels, any_bus_mapping_found) for the requested bus name.

    channels is the set of ASC numeric CANFD channels mapped to bus_name.
    any_bus_mapping_found tells whether the file contains //BusMapping: lines
    at all (plain CANoe exports, e.g. without the CARMEN logger header, don't).
    """
    channels = set()
    any_bus_mapping_found = False
    with open(input_file, 'r', encoding='utf-8', errors='replace') as src:
        for line in src:
            stripped = line.strip()
            if not stripped.startswith('//BusMapping:'):
                continue

            any_bus_mapping_found = True
            parts = stripped.lstrip('/').split()
            if len(parts) >= 5 and parts[1] == 'CANFD' and parts[3] == '=' and parts[4] == bus_name:
                channels.add(parts[2])

    return channels, any_bus_mapping_found


def _configure_canfd_channel_filter(input_file, warn_if_unmapped=True):
    """Configure global ASC CANFD channel filtering for the target bus.

    Falls back to "no filtering" (all CANFD channels accepted) when the file
    has no //BusMapping: annotations at all, since plain CANoe exports don't
    carry that metadata and typically log a single CANFD channel anyway.
    """
    global _ASC_CANFD_CHANNEL_FILTER
    channels, any_bus_mapping_found = _resolve_canfd_channels(input_file, _TARGET_CANFD_BUS_NAME)
    if not channels and not any_bus_mapping_found:
        if not warn_if_unmapped:
            _ASC_CANFD_CHANNEL_FILTER = None
            return
        print(
            f"WARNING: No //BusMapping: annotations found in {input_file}; "
            f"proceeding without CANFD channel filtering."
        )
        _ASC_CANFD_CHANNEL_FILTER = None
        return
    _ASC_CANFD_CHANNEL_FILTER = channels


class BLFTraceError(Exception):
    """Raised when a BLF trace cannot be normalized for analysis."""


def _convert_blf_to_asc(input_file, output_file):
    """Write BLF CAN FD frames in the ASC layout consumed by this analyzer."""
    try:
        import can
    except ImportError as error:
        raise BLFTraceError(
            'BLF input requires python-can; install it with "pip install python-can".'
        ) from error

    dlc_by_length = {
        0: '0', 1: '1', 2: '2', 3: '3', 4: '4', 5: '5', 6: '6', 7: '7', 8: '8',
        12: '9', 16: 'A', 20: 'B', 24: 'C', 32: 'D', 48: 'E', 64: 'F',
    }
    try:
        with can.BLFReader(input_file) as reader, open(output_file, 'w', encoding='utf-8') as output:
            trace_start_timestamp = reader.start_timestamp
            for message in reader:
                if not message.is_fd:
                    continue

                payload = bytes(message.data)
                dlc = dlc_by_length.get(len(payload))
                if dlc is None:
                    continue
                channel = message.channel if message.channel is not None else 1
                direction = 'Tx' if getattr(message, 'is_rx', None) is False else 'Rx'
                payload_text = ' '.join(f'{byte:02X}' for byte in payload)
                output.write(
                    f'{message.timestamp - trace_start_timestamp:.9f} CANFD {channel} {direction} '
                    f'{message.arbitration_id:x} 0 0 10 '
                    f'{int(message.bitrate_switch)} {int(message.error_state_indicator)} '
                    f'{dlc} {len(payload)} {payload_text}\n'
                )
    except Exception as error:
        raise BLFTraceError(f'Could not read BLF trace {input_file}: {error}') from error


@contextmanager
def _analysis_input_file(input_file):
    """Yield an ASC path directly, or a temporary ASC conversion for BLF input."""
    if os.path.splitext(input_file)[1].lower() == '.blf':
        with tempfile.TemporaryDirectory(prefix='analyze_byte_pairs_') as temp_dir:
            normalized_file = os.path.join(temp_dir, 'normalized.asc')
            _convert_blf_to_asc(input_file, normalized_file)
            yield normalized_file
        return

    first_timestamp = _first_timestamp_token(input_file)
    if first_timestamp is None or float(first_timestamp) >= 0:
        yield input_file
        return

    with tempfile.TemporaryDirectory(prefix='analyze_byte_pairs_') as temp_dir:
        normalized_file = os.path.join(temp_dir, 'normalized.asc')
        _shift_asc_timestamps(input_file, normalized_file, Decimal(first_timestamp))
        yield normalized_file


def _first_timestamp_token(input_file):
    with open(input_file, 'r', encoding='utf-8', errors='replace') as source:
        for line in source:
            timestamp = _parse_timestamp(line)
            if timestamp is not None:
                return line.split(maxsplit=1)[0]
    return None


def _shift_asc_timestamps(input_file, output_file, offset):
    with open(input_file, 'r', encoding='utf-8', errors='replace', newline='') as source, \
            open(output_file, 'w', encoding='utf-8', newline='') as output:
        for line in source:
            match = re.match(r'^([ \t]*)(\S+)', line)
            if match is not None:
                try:
                    float(match.group(2))
                except ValueError:
                    pass
                else:
                    shifted_timestamp = Decimal(match.group(2)) - offset
                    line = (
                        line[:match.start(2)] + format(shifted_timestamp, 'f')
                        + line[match.end(2):]
                    )
            output.write(line)


def _line_matches_configured_canfd_channel(parts):
    """Return True when a tokenized CANFD line belongs to the configured bus."""
    if _ASC_CANFD_CHANNEL_FILTER is None:
        return True
    return parts[2] in _ASC_CANFD_CHANNEL_FILTER


def _parse_timestamp(line):
    """Return the leading ASC timestamp as seconds, or None for non-frame lines."""
    parts = line.strip().split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def _session_label(session_id, *, otherwise=None):
    """Format a session ID for display without changing its lookup key."""
    hex_id = session_id if isinstance(session_id, str) else f'{session_id:x}'
    if int(hex_id, 16) == 0x28:
        return '0x28 IDG'
    return f'0x{hex_id}' if otherwise is None else otherwise


_LOCALIZATION_ZONES = {
    '00': ('Inside', 'Inside Zone', 'Inside Zone'),
    '01': ('Trunk', 'Trunk', 'Inside Zone'),
    '03': ('VehicleAccess', 'Entry Zone', 'Entry Zone'),
    '04': ('VehicleLock', 'Locking Zone', 'Locking Zone'),
    '05': ('RemoteOperation', 'Remote Control Zone', 'Remote Control Zone'),
    '0F': ('NoVerifiedDistanceZone', 'No verified distance zone', None),
}
_RANGING_INTENT_MARKER = 'DK Event Notification Device Ranging Intent SubEvent '


def _tool_directory():
    """Return the directory that ships alongside the packaged/script executable."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _default_config_path():
    config_name = 'analyze_byte_pairs_cfg.json'
    external_config = os.path.join(_tool_directory(), config_name)
    if os.path.isfile(external_config):
        return external_config
    bundled_directory = getattr(sys, '_MEIPASS', None)
    if bundled_directory:
        bundled_config = os.path.join(bundled_directory, config_name)
        if os.path.isfile(bundled_config):
            return bundled_config
    return external_config


def _box2box_rx_window(session_id, data):
    """Return the RX-window diagnostic for any session ID with a valid IRK ID."""
    irk_id, uncertainty, device_uncertainty = data
    if irk_id in (0, 255):
        return None
    exponent = device_uncertainty // 8
    window = ((1 << exponent) + 65 + uncertainty) * 2
    return (
        f'Calculated RX Window for Session {_session_label(session_id, otherwise=f"0x{session_id}")} = {window}     || DEBUG  '
        f'UWBDeviceTimeUncertainty: {device_uncertainty} , exponent:  {exponent} , '
        f'TimeOffsetUntercainty: {uncertainty}'
    )


def _box2box_binding_keys():
    """Load local keys without including them in source code or report output."""
    path = os.path.join(_tool_directory(), 'Bindingkeys.txt')
    try:
        with open(path, encoding='utf-8-sig') as handle:
            values = [line.strip() for line in handle]
    except OSError:
        return []
    return [bytes.fromhex(value) for value in values
            if len(value) == 64 and all(character in '0123456789abcdefABCDEF' for character in value)]


class _AesGcmCin:
    """Python adaptation of the AES-256 routines supplied in AesGcm.cin."""

    def __init__(self, key):
        if len(key) != 32:
            raise ValueError('AesGcm.cin requires a 32-byte AES key.')
        path = os.path.join(_tool_directory(), 'AesGcm.cin')
        if not os.path.isfile(path) and getattr(sys, '_MEIPASS', None):
            path = os.path.join(sys._MEIPASS, 'AesGcm.cin')
        with open(path, encoding='cp1252') as source_file:
            source = source_file.read()
        source = re.sub(r'/\*.*?\*/|//[^\r\n]*', '', source, flags=re.DOTALL)
        for name, expected in (('Nb', 4), ('Nk', 8), ('Nr', 14), ('AES_BLOCKLEN', 16),
                               ('AES_KEYLEN', 32), ('AES_keyExpSize', 240), ('MAXPT', 120)):
            match = re.search(r'\bconst\s+(?:int|byte)\s+' + name + r'\s*=\s*(\d+)\s*;', source)
            if match is None or int(match.group(1)) != expected:
                raise ValueError(f'Unsupported AesGcm.cin constant: {name}.')
        tables = {}
        for name, length in (('sbox', 256), ('Rcon', 11)):
            match = re.search(r'\bbyte\s+' + name + r'\s*\[\s*' + str(length)
                              + r'\s*\]\s*=\s*\{([^}]*)\}\s*;', source)
            if match is None:
                raise ValueError(f'Missing AesGcm.cin lookup table: {name}.')
            values = [int(token.strip(), 0) for token in match.group(1).split(',') if token.strip()]
            if len(values) != length or any(value < 0 or value > 255 for value in values):
                raise ValueError(f'Invalid AesGcm.cin lookup table: {name}.')
            tables[name] = values
        self.sbox = tables['sbox']
        self.round_key = list(key)
        for word_index in range(8, 60):
            temporary = self.round_key[-4:]
            if word_index % 8 == 0:
                temporary = [self.sbox[value] for value in temporary[1:] + temporary[:1]]
                temporary[0] ^= tables['Rcon'][word_index // 8]
            elif word_index % 8 == 4:
                temporary = [self.sbox[value] for value in temporary]
            previous_word = self.round_key[(word_index - 8) * 4:(word_index - 7) * 4]
            self.round_key.extend(previous ^ substituted for previous, substituted in zip(previous_word, temporary))

    @staticmethod
    def _xtime(value):
        return ((value << 1) ^ (((value >> 7) & 1) * 0x1b)) & 255

    def encrypt_block(self, block):
        if len(block) != 16:
            raise ValueError('AES requires a 16-byte block.')
        state = [value ^ self.round_key[index] for index, value in enumerate(block)]
        for round_index in range(1, 15):
            substituted = [self.sbox[value] for value in state]
            state = [substituted[4 * ((column + row) % 4) + row]
                     for column in range(4) for row in range(4)]
            if round_index != 14:
                for offset in range(0, 16, 4):
                    column = state[offset:offset + 4]
                    combined = column[0] ^ column[1] ^ column[2] ^ column[3]
                    for row in range(4):
                        state[offset + row] ^= combined ^ self._xtime(column[row] ^ column[(row + 1) % 4])
            state = [value ^ self.round_key[round_index * 16 + index]
                     for index, value in enumerate(state)]
        return bytes(state)

    @staticmethod
    def _multiply(left, right):
        product = 0
        factor = right
        for bit_index in range(127, -1, -1):
            if (left >> bit_index) & 1:
                product ^= factor
            factor = (factor >> 1) ^ (0xe1000000000000000000000000000000 if factor & 1 else 0)
        return product

    @staticmethod
    def _validate(iv, data, aad):
        if len(iv) != 12:
            raise ValueError('AesGcm.cin requires a 12-byte IV.')
        if len(data) > 120 or len(aad) > 8191:
            raise ValueError('Data exceeds the supported AesGcm.cin buffer/length range.')

    def _counter_transform(self, iv, data):
        result = bytearray()
        for block_index, offset in enumerate(range(0, len(data), 16), start=2):
            counter = bytes(iv) + bytes((0, 0, 0, block_index))
            stream = self.encrypt_block(counter)
            result.extend(value ^ mask for value, mask in zip(data[offset:offset + 16], stream))
        return bytes(result)

    def _tag(self, iv, ciphertext, aad):
        hash_key = int.from_bytes(self.encrypt_block(bytes(16)), 'big')
        accumulator = 0
        for data in (aad, ciphertext):
            for offset in range(0, len(data), 16):
                block = bytes(data[offset:offset + 16]).ljust(16, b'\x00')
                accumulator = self._multiply(accumulator ^ int.from_bytes(block, 'big'), hash_key)
        lengths = (len(aad) * 8).to_bytes(8, 'big') + (len(ciphertext) * 8).to_bytes(8, 'big')
        accumulator = self._multiply(accumulator ^ int.from_bytes(lengths, 'big'), hash_key)
        tag_mask = self.encrypt_block(bytes(iv) + b'\x00\x00\x00\x01')
        return (accumulator ^ int.from_bytes(tag_mask, 'big')).to_bytes(16, 'big')

    def encrypt(self, iv, plaintext, aad):
        """Adapt gcmEncrypt, returning ciphertext followed by its 16-byte tag."""
        self._validate(iv, plaintext, aad)
        ciphertext = self._counter_transform(iv, plaintext)
        return ciphertext + self._tag(iv, ciphertext, aad)

    def decrypt(self, iv, encrypted, aad):
        """Adapt gcmDecrypt; never return plaintext when tag verification fails."""
        if len(encrypted) < 16:
            raise ValueError('AES-GCM data must include a 16-byte authentication tag.')
        ciphertext, tag = encrypted[:-16], encrypted[-16:]
        self._validate(iv, ciphertext, aad)
        if not secrets.compare_digest(self._tag(iv, ciphertext, aad), bytes(tag)):
            return None
        return self._counter_transform(iv, ciphertext)


def _decode_box2box_setup(setup1, setup2, keys):
    """Assemble the two CAPL buffers and authenticate encrypted setup data."""
    if setup1 is None or len(setup1) != 64 or len(setup2) != 64:
        return None, 'Setup1 missing or incomplete setup payload'
    length = setup1[26]
    if length < 73 or length > 74:
        return None, f'unsupported cipher data length {length}'
    ciphertext = setup1[27:64] + setup2[3:3 + length - 37]
    if not (int.from_bytes(setup1[9:11], 'little') & 1):
        return ciphertext, 'not encrypted'
    if not keys:
        return None, 'no binding keys available in Bindingkeys.txt'
    for key in keys:
        try:
            plaintext = _AesGcmCin(key).decrypt(
                bytes(setup1[11:23]), bytes(ciphertext + setup2[40:56]),
                bytes(setup1[:9] + setup2[56:64])
            )
        except (OSError, ValueError) as error:
            return None, f'AesGcm.cin unavailable or invalid: {error}'
        if plaintext is not None:
            return plaintext, 'encrypted'
    return None, 'decryption failed'


def _fallback_timestamp(timestamp):
    """Match the reference Write Window's six-digit, half-up timestamp rounding."""
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_HALF_UP
        return f'{float(+Decimal(str(timestamp))):g}'


def _box2box_start_delay(timestamp, time0, session_id, anchor_time, device_times,
                        setup_time0, kind='resume', fallback=False, context=()):
    """Preserve the CAPL estimate, including its use of the last Setup2 Time0."""
    captured_time, captured_timestamp = anchor_time
    now = timestamp * 1000000
    actual_time = captured_time + now - captured_timestamp
    displayed_timestamp = _fallback_timestamp(timestamp) if fallback else f'{timestamp:g}'
    prefix = f'{displayed_timestamp}s: StartDelay-Estimation : '
    yield prefix + (
        f'captured_anchor_systime_u64={captured_time:.0f}  lactualCANTimeStamp={now:.0f}  '
        f'captures_anchor_timesync_timestamp_u64={captured_timestamp:.0f}  '
        f'delta_to_now={now - captured_timestamp:.0f} ---> actual_anchor_systime={actual_time:.0f}'
    ), 'black' if fallback else 'olive'
    offset_session = 255 if kind == 'setup' else session_id
    if offset_session not in device_times:
        spacing = ' ' if fallback else '  '
        yield prefix + f'RESUME SESSION --->{spacing}No DeviceTimeSync found !!', 'bright-red' if fallback else 'red'
        return
    offset = (-device_times[offset_session]) & 0xffffffffffffffff
    positive = setup_time0 > actual_time + offset
    actual_hex = f'{int(actual_time) & 0xffffffff:x}'
    time0_hex = f'{time0 >> 32:x}{time0 & 0xffffffff:x}'
    offset_hex = f'{offset >> 32:x}{offset & 0xffffffff:x}'
    explanation = 'Time0 + TimeDeviceOffset(negative) - ActualSystime(estimated)  --> Startdelay (estimated value +/- 5ms)'
    delay_ms = (time0 - offset - actual_time) / 1000
    fallback_color = ('bright-red' if not positive or delay_ms > 500000 else
                      'magenta' if delay_ms > 250000 else 'black')
    color = fallback_color if fallback else 'olive' if positive else 'red'
    if fallback:
        for text in context:
            yield text, fallback_color
    if kind == 'setup':
        yield prefix + 'CRSS2 for Session X  --->  ' + explanation, color
        if positive:
            detail = f'{time0_hex} - {offset_hex} -{actual_hex}'
            delay = int(time0 - offset - actual_time) / 1000
        else:
            detail = f'{time0_hex} - {offset >> 32:x} - {offset & 0xffffffff:x}{actual_hex})'
            delay_us = int(offset + actual_time - time0)
            if fallback:
                delay_us = min(delay_us, 0xffffffffffffffff)
            delay = -delay_us / 1000
        yield prefix + (
            f'CRSS2 for Session {_session_label(session_id, otherwise=f"{session_id:x}")} --->  {detail}  --> '
            f'Startdelay = {delay:.0f}ms (estimated value +/- 5ms)'
        ), color
    elif kind == 'resume':
        yield prefix + 'RESUME SESSION --->  ' + explanation, color
        if positive:
            spacing = ' ' if fallback else ''
            detail = f'{time0_hex} - {offset_hex} -{spacing}{actual_hex}  --> '
            delay = int(time0 - offset - actual_time) / 1000
        else:
            spacing = '' if fallback else ' '
            detail = f'{time0 >> 32:x} {time0 & 0xffffffff:x} - {offset >> 32:x} {offset & 0xffffffff:x} -{spacing}{actual_hex} --> '
            delay = -int(offset + actual_time - setup_time0) / 1000
        yield prefix + f'RESUME SESSION --->  {detail}Startdelay = {delay:.0f}ms (estimated value +/- 5ms)', color
    else:
        heading = 'RESUME SESSION' if positive else 'RESUME SESSION(FBDOnly)'
        color = fallback_color if fallback else 'olive'
        yield prefix + heading + ' --->  ' + explanation, color
        if positive:
            detail = (f'{time0_hex}-{offset_hex}-{actual_hex}' if fallback else
                      f'{time0_hex}-{actual_hex}{offset >> 32:x}-{offset & 0xffffffff:x}')
            delay_text = f'{int(time0 - offset - actual_time) / 1000:.0f}'
        else:
            detail = f'{time0_hex}-{offset_hex}-{actual_hex}'
            wrapped_delay = int(time0 - offset - actual_time) & 0xffffffffffffffff
            delay_text = f'-{wrapped_delay / 1000:.0f}'
        yield prefix + f'{heading} --->  {detail})  --> Startdelay = {delay_text}ms (estimated value +/- 5ms)', color


def _box2box_runtime_events(payload, prefix):
    """Decode the shared 0x51E..0x525 runtime-measurement layout."""
    tasks = ('Idle', 'Lowlevel', 'Regular', 'Irq', 'Critical')
    runtimes = [int.from_bytes(payload[offset:offset + 8], 'little') for offset in range(0, 40, 8)]
    yield prefix + 'Runtime measurement data received!', 'green'
    yield prefix + 'Total runtime data ', 'green'
    for task, runtime in zip(tasks, runtimes):
        yield prefix + f'Total runtime {task} =  {runtime / 1000000:.6f} s ', 'black'
    yield prefix + 'Max runtime data ', 'green'
    regular_events = {3: 'CYCLIC_5MS', 5: 'CYCLIC_10MS', 9: 'CYCLIC_20MS',
                      17: 'CYCLIC_40MS', 4097: 'BLE_INT_discard'}
    irq_events = {32: 'BLE_INT', 64: 'SPI_INT', 128: 'WUPSYNC_INT', 256: 'EXTFLASH_INT',
                  512: 'GPT_INT', 1024: 'WUPSYNC_INSTALLATION', 2048: 'TSYNC_MASTER_SEND_SYSTEM_TIME'}
    for task, offset, labels in (('Regular', 40, regular_events), ('Irq', 44, irq_events),
                                 ('Critical', 48, {}), ('Lowlevel', 50, {})):
        maximum = int.from_bytes(payload[offset:offset + 2], 'little')
        yield prefix + f'Maxtime {task} task executed =  {maximum} us ', 'black'
        if labels:
            event = labels.get(int.from_bytes(payload[offset + 2:offset + 4], 'little'))
            if event is not None:
                yield prefix + f'Event with maxtime {task} task = OS_EVENT_{event} ', 'black'
    yield prefix + 'CPU Load data ', 'green'
    total = sum(runtimes)
    for task, runtime in zip(tasks, runtimes):
        usage = runtime / total * 100 if total else float('nan')
        trailing = ' ' if task == 'Regular' else ''
        yield prefix + f'{task} task CPU Usage =  {usage:.4f} %{trailing}', 'black'


class _CpdWriteWindowState:
    """Replay the CPD control/result handlers and their trace-time timers."""

    def __init__(self):
        self.running = False
        self.start_time = 0.0
        self.cycle_ms = 0
        self.jitter_ms = 0
        self.timers = {}
        self.counters = {}
        self.last_results = {'VM': 0.0, 'HM': 0.0}
        self.first_results = set()
        self.startup_max = {'VM': 0.0, 'HM': 0.0}
        self.missing_since = {}
        self.out_of_sync = False
        self.sync_lost_count = 0
        self.sniffer_prepoll = 0
        self.sniffer_poll = 0
        self.sniffer_fdd = 0
        self.sniffer_new_fdd = False

    def advance(self, timestamp):
        now = round(timestamp * 1000000)
        while self.timers:
            key = min(self.timers, key=self.timers.get)
            deadline = self.timers[key]
            if deadline > now:
                break
            del self.timers[key]
            kind, anchor = key
            if kind == 'startup':
                yield (
                    f'{deadline / 1000000:g}s: !!!! ERRROR : After CPD Startcommand '
                    f'no CPD-Results provided by {anchor}_Anchor'
                ), 'black'
            elif self.running:
                self.missing_since[anchor] = deadline / 1000000

    def control(self, timestamp, payload, active_sessions):
        prefix = f'{timestamp:g}s: '
        self.counters.clear()
        for anchor in ('VM', 'HM'):
            self.timers.pop(('watchdog', anchor), None)
        if payload[2] == 1:
            self.running = True
            self.start_time = timestamp
            self.first_results.clear()
            self.out_of_sync = False
            for anchor, bit in (('VM', 1), ('HM', 2)):
                if payload[0] & bit:
                    self.timers[('startup', anchor)] = round(timestamp * 1000000) + 300000
            verb = 're-started' if 21 in active_sessions else 'started'
            active_sessions.add(21)
            yield prefix + f'CPD has been {verb}. Amount of active session = {len(active_sessions):x}', 'orange'
        else:
            if self.out_of_sync:
                yield prefix + f'CPD_HM - {self.sync_lost_count} rounds out of sync at last CPD session', 'dark-green'
                self.sync_lost_count = 0
            for anchor in ('VM', 'HM'):
                self.timers.pop(('startup', anchor), None)
                if self.last_results[anchor] - timestamp > 0.03:
                    yield prefix + (
                        f'CPD stopped suddenly within actual CPD-Session at '
                        f'{self.last_results["VM"]:g}s on {anchor}-Anchor'
                    ), 'black'
            if not self.running:
                self.startup_max = {'VM': 0.0, 'HM': 0.0}
                yield prefix + (
                    'Startup_CPD_VM_max and Startup_CPD_HM_max resetted to 0 '
                    'because trace starts with CPD_Stop-Command'
                ), 'black'
            yield prefix + (
                f'CPD Session started at {self.start_time:g}s stopped - Duration: '
                f'{timestamp - self.start_time:g}s  CPD_VM Startup_time_max: '
                f'{self.startup_max["VM"]:g}ms   CPD_HM Startup_time_max: {self.startup_max["HM"]:g}ms'
            ), 'black'
            self.running = False
            verb = 'stopped' if 21 in active_sessions else 'stopped but was not running'
            active_sessions.discard(21)
            yield prefix + f'CPD has been {verb}. Amount of active session = {len(active_sessions):x}', 'orange'
        self.jitter_ms = 10
        self.cycle_ms = {1: 24, 2: 48, 3: 96}.get(payload[4], self.cycle_ms)

    def result(self, timestamp, payload, anchor):
        prefix = f'{timestamp:g}s: '
        self.timers.pop(('startup', anchor), None)
        if anchor == 'VM' or payload[1] == 3:
            current = payload[0]
            previous = self.counters.get(anchor)
            warning = None
            if previous is not None:
                if current < previous and current != 0 and previous != 255:
                    warning = f'ran backward from {previous} to {current}'
                elif current == previous:
                    warning = f'remained on same value {current}'
                elif ((current - previous) & 255) > 1:
                    warning = f'jump from {previous} to {current} with deviation of {((current - previous) & 255) - 1}'
            self.counters[anchor] = current
            if warning is not None:
                yield prefix + f'CPD {anchor}-Anchor MessageCounter {warning}', 'bright-red'
            elapsed = timestamp - self.last_results[anchor]
            if elapsed < self.cycle_ms / 2000:
                yield (
                    f'{timestamp * 1000:g}s: CPD {anchor}-Anchor performed CPD with '
                    f'{elapsed * 1000:g}ms to early (Limit is at {self.cycle_ms / 2:g}ms)'
                ), 'bright-red'
            self.last_results[anchor] = timestamp
            missing = self.missing_since.pop(anchor, None)
            if missing is not None and (timestamp - missing) * 1000 > self.cycle_ms / 2:
                yield prefix + f'CPD {anchor}-Anchor back after {timestamp - missing:g}s <-- Delay to big', 'bright-red'
            if anchor not in self.first_results:
                self.first_results.add(anchor)
                startup = (timestamp - self.start_time) * 1000
                self.startup_max[anchor] = max(self.startup_max[anchor], startup)
                if startup >= 200:
                    yield prefix + (
                        f'CPD_{anchor} Result - Startup_time: {startup:g}ms   '
                        f'Startup_time_max: {self.startup_max[anchor]:g}ms  '
                        '<--- WARNING: CPD Startup Time to high'
                    ), 'black'
            if anchor == 'VM' or self.running:
                self.timers[('watchdog', anchor)] = round(timestamp * 1000000) + (self.cycle_ms + self.jitter_ms) * 1000
        if anchor == 'HM' and payload[1] == 0x21 and len(payload) >= 64:
            was_out_of_sync = self.out_of_sync
            self.out_of_sync = not any(payload[4:25])
            if self.out_of_sync:
                self.sync_lost_count += 1
            if self.out_of_sync and not was_out_of_sync:
                yield prefix + f'CPD_HM out of Sync #{self.sync_lost_count}  <-- WARNING - RxError:{payload[63]} ', 'bright-red'
            elif not self.out_of_sync and was_out_of_sync:
                yield prefix + f'CPD_HM back in Sync :) - {self.sync_lost_count} rounds out of sync', 'dark-green'
                self.sync_lost_count = 0

    def sniffer(self, timestamp, payload):
        """Use a numeric microsecond field for CAPL's malformed FDD deviation format."""
        if len(payload) < 2 or payload[0] != 0xcc:
            return
        kind = payload[1]
        if kind in (1, 3) and len(payload) < (18 if kind == 3 else 10):
            return
        prefix = f'{timestamp:g}s: '
        if kind == 0:
            yield prefix + 'Sniffer short blindspot -> IDLE-Restart  ', 'black'
        if kind in (1, 3):
            self.sniffer_prepoll = int.from_bytes(payload[2:10], 'big')
        if kind == 3:
            self.sniffer_poll = int.from_bytes(payload[10:18], 'big')
        if self.sniffer_new_fdd:
            self.sniffer_new_fdd = False
            negative = self.sniffer_fdd > self.sniffer_poll
            deviation = (self.sniffer_poll - self.sniffer_fdd if negative
                         else self.sniffer_fdd - self.sniffer_poll) & 0xffffffffffffffff
            sign = '-' if negative else '+'
            yield prefix + (
                f'Sniffer Catched Prepoll at t={self.sniffer_prepoll} and Poll at '
                f't={self.sniffer_poll} as expected by FDD at t={self.sniffer_fdd} '
                f'with deviation of {sign}{deviation}\u00b5s '
            ), 'olive'
        elif kind == 3:
            deviation = (self.sniffer_poll - self.sniffer_prepoll) & 0xffffffffffffffff
            yield prefix + (
                f'Sniffer Catched Prepoll at t={self.sniffer_prepoll}\u00b5s and Poll at '
                f't={self.sniffer_poll}\u00b5s (No FDD received) '
                f'Prepoll to Poll deviation of +{deviation}\u00b5s '
            ), 'olive'
        elif kind == 1:
            yield prefix + f'Sniffer Catched Prepoll at t={self.sniffer_prepoll}\u00b5s and no Poll catched ', 'olive'


@contextmanager
def _trace_lines(source):
    """The lines of a trace file, or source itself if it already is an iterable of lines."""
    if isinstance(source, (str, os.PathLike)):
        with open(source, 'r', encoding='utf-8', errors='replace') as handle:
            yield handle
    else:
        yield source


_ANSI_COLORS = {'blue': '34', 'green': '32', 'orange': '33', 'red': '31', 'violet': '35', 'olive': '92'}


def _run_live(lines, output, show_results=False):
    """Print the events of a running measurement as soon as their line arrives."""
    for text, color in _box2box_events(lines):
        # As in the HTML report, the ranging results are hidden unless asked for.
        if not show_results and 'received with SessionIDShort' in text:
            continue
        code = _ANSI_COLORS.get(color)
        output.write(f'\x1b[{code}m{text}\x1b[0m\n' if code else text + '\n')
        output.flush()


def _box2box_events(input_file, include_cpd=False, include_fallback=False):
    """Replay the Box2Box Write Window output in trace order.

    input_file is the path of an ASC trace or an iterable of ASC lines.
    """
    message_names = {
        msg_id: f'StatusUWBRangingFDD1FBD_{_STATUS_UWB_RANGING_FDD2_NAMES[fdd2_id]}'
        for msg_id, fdd2_id in _STATUS_UWB_RANGING_FDD1_TO_FDD2.items()
    }
    message_names.update({
        msg_id: f'StatusUWBRangingFDD2FBD_{anchor}'
        for msg_id, anchor in _STATUS_UWB_RANGING_FDD2_NAMES.items()
    })
    status_names = {
        format(0x1a4 + index, 'x'): anchor
        for index, anchor in enumerate(('VM', 'HM', 'Miko', 'Trunk', 'VL', 'VR', 'HL', 'HR'))
    }
    target_ids = (
        set(message_names) | set(status_names) | _TIME_SYNC_DEVICE_OFFSET_IDS
        | _STATUS_UWB_RANGING_TARGET_IDS
        | _TIME_SYNC_TARGET_IDS | _STATUS_TIME_SYNC_TARGET_IDS | _RESUME_RANGING_SESSION_TARGET_IDS
        | _STATUS_FBD_TARGET_IDS | {format(can_id, 'x') for can_id in range(0x51e, 0x526)}
        | {_CONTROL_FBD_ID, _CONTROL_RANGING_SESSION_ID, '190', '191'}
    )
    active_sessions = set()
    cpd_state = _CpdWriteWindowState() if include_cpd else None
    if include_cpd:
        target_ids.update({'12e', '14a', '14b', '341'})
    setup1_by_address = {}
    binding_keys = None
    anchor_time = (0, 0)
    last_anchor_info = ''
    last_offset_info = ''
    pending_anchor_time = None
    device_times = {}
    setup_time0 = 0
    reset_counters = {}
    ranging_counts = Counter()
    error_counts = Counter()
    status_counts = Counter()
    rx_windows = {}
    fallback_sessions = set()
    status_labels = ('0', 'UWB_RANGING_SESSIONSTATE_EMPTY', 'UWB_RANGING_SESSIONSTATE_CONFIGURED',
                     'UWB_RANGING_SESSIONSTATE_ACTIVE', 'RANGING_SESSIONSTATE_SUSPEND')
    multiranging_labels = {
        0x40: 'UwbApp_Responder_GetNextStartTime, Multiranging',
        0x41: 'Start CPD, if BB active, current ranging is aborted, a de-prirotization needs to be reported on CAN and then calculate ranging for next block',
        0x42: 'UwbApp_Responder_GetStartTimeByTimingOrderIndex, Fallback not active',
        0x43: 'UwbApp_Responder_GetStartTimeByTimingOrderIndex, Fallback active',
        0x44: 'UwbApp_Responder_SetFDDSystemTime, Fallback not active',
        0x45: 'Start CPD, predict the future and send de-prioritization in advance if needed, Start in future',
        0x46: 'Start CPD, predict the future and send de-prioritization in advance if needed, Start in past',
    }
    fallback_labels = {
        1: 'No BLE Tsync (no Device Time, Fallback active)',
        2: 'Fallback active',
        3: 'No Device Time (no BLE TSync)',
        4: 'Calculated RxWindow less than 500ms in future',
        5: 'RxWindow more than 500ms in the future',
        6: 'RxWindow >22ms at start',
        7: 'RxWindow >22ms from UWB SyncState',
        8: 'RxWindow >22ms from BLE SyncState',
    }
    power_modes = {
        1: 'Init', 2: 'Sleep Mode', 3: 'Normal Mode',
        4: 'Without FBD only', 5: 'With FBD only',
    }
    with _trace_lines(input_file) as handle:
        for line in handle:
            matching, msg_id, data = _extract_can_data_bytes(line, target_ids)
            timestamp = _parse_timestamp(line)
            if cpd_state is not None and timestamp is not None:
                yield from cpd_state.advance(timestamp)
            if not matching or not data or None in data or timestamp is None:
                continue
            payload = bytes(int(value, 16) for value in data)
            displayed_timestamp = _fallback_timestamp(timestamp) if include_fallback else f'{timestamp:g}'
            prefix = f'{displayed_timestamp}s: '
            if cpd_state is not None and msg_id == '12e' and len(payload) >= 5:
                yield from cpd_state.control(timestamp, payload, active_sessions)
            elif cpd_state is not None and msg_id in ('14a', '14b') and len(payload) >= 2:
                yield from cpd_state.result(timestamp, payload, 'VM' if msg_id == '14a' else 'HM')
            elif cpd_state is not None and msg_id == '341':
                yield from cpd_state.sniffer(timestamp, payload)
            elif msg_id in message_names:
                if cpd_state is not None and msg_id in _STATUS_UWB_RANGING_FDD2_IDS and len(payload) >= 37:
                    cpd_state.sniffer_fdd = int.from_bytes(payload[29:37], 'little')
                    if cpd_state.sniffer_fdd:
                        cpd_state.sniffer_new_fdd = True
                yield prefix + f'{message_names[msg_id]} received with SessionIDShort {_session_label(payload[0])}', 'blue'
            elif 0x51e <= int(msg_id, 16) <= 0x525 and len(payload) >= 52:
                yield from _box2box_runtime_events(payload, prefix)
            elif msg_id in _STATUS_FBD_TARGET_IDS and len(payload) >= 18:
                anchor = _STATUS_FBD_ANCHOR_NAMES[msg_id]
                reason = payload[2]
                if reason == 2:
                    yield prefix + f'{anchor} WD Reset ({reason})', 'red'
                for side, offset in (('UWB', 17), ('BLE', 11)):
                    counter = payload[offset]
                    if counter == 255:
                        continue
                    key = (anchor, side)
                    if key in reset_counters and reset_counters[key] != counter:
                        separator = '!' * 95 if side == 'UWB' and anchor in ('VM', 'Miko') else prefix + '!' * 85
                        yield separator, 'red'
                        marker = '!!!!! RESET !!!!!' if side == 'UWB' else 'RESET !!!!!'
                        yield prefix + (
                            f'{marker} --> {anchor} had a reset on {side} side '
                            f'with resetreason {reason:x} (resetcounter={counter})'
                        ), 'red'
                        yield separator, 'red'
                    reset_counters[key] = counter
            elif msg_id in _TIME_SYNC_TARGET_IDS and len(payload) >= 4:
                if include_fallback:
                    yield prefix + (
                        f'TimeSyncFBD_{_TIME_SYNC_ANCHOR_NAMES[msg_id]} received '
                        f'with SyncTime = {anchor_time[0]}us'
                    ), 'black'
                pending_anchor_time = int.from_bytes(payload[:4], 'little')
            elif msg_id in _STATUS_TIME_SYNC_TARGET_IDS:
                anchor = _STATUS_TIME_SYNC_ANCHOR_NAMES[msg_id]
                if pending_anchor_time is not None:
                    if include_fallback:
                        if anchor in ('HM', 'HL'):
                            last_anchor_info = prefix + (
                                f'{anchor} Actual Anchor-Systime = {anchor_time[0]:.0f}\u00b5s '
                                f'at this measurement time {timestamp * 1000000:.0f}us'
                            )
                        else:
                            last_anchor_info = prefix + f'{anchor} Actual Anchor-Systime = {anchor_time[0] / 1000000:.0f}\u00b5s'
                    anchor_time = (pending_anchor_time, timestamp * 1000000)
                    pending_anchor_time = None
                if include_fallback and anchor in ('HM', 'HL') and len(payload) >= 3:
                    correction = int.from_bytes(payload[1:3], 'little')
                    yield prefix + (
                        f'StatusTimeSyncFBD_{anchor} received with ReSyncCorrection = {correction}\u00b5s'
                    ), 'black'
            elif msg_id == '190' and len(payload) == 64:
                setup1_by_address[payload[:3]] = payload
            elif msg_id == '191' and len(payload) >= 3:
                session_id = payload[2]
                setup1 = setup1_by_address.pop(payload[:3], None)
                if binding_keys is None:
                    binding_keys = _box2box_binding_keys()
                plaintext, state = _decode_box2box_setup(setup1, payload, binding_keys)
                if plaintext is None:
                    if state == 'decryption failed':
                        text = f'ControlRangingSessionSetup2 received (decryption failed) with Session ID = {_session_label(session_id, otherwise=session_id)}'
                    else:
                        text = f'Box2Box replay unavailable for ControlRangingSessionSetup2, Session ID = {_session_label(session_id, otherwise=session_id)}: {state}'
                    yield prefix + text, 'red'
                    continue
                yield prefix + f'ControlRangingSessionSetup2 received ({state})', 'green'
                fields = (
                    (32, 'RAN_Multiplier'), (33, 'Chaps Per Slot'), (34, 'Number Responders'),
                    (35, 'Number_Slot_per_Round'), (52, 'SYNC_Code_Index'),
                    (57, 'UWB_SESSION_ID_SHORT'), (58, 'Selected_UWB_Channel'),
                    (66, 'FINAL_DATA_DISTRIBUTION'), (71, 'Selected_PulseShape_Combo'),
                )
                for offset, name in fields:
                    yield prefix + f'ControlRangingSession_{name} = {plaintext[offset]}', 'green' if offset == 57 else 'black'
                yield prefix + f'ControlRangingSession_Selected_Hopping_Config_Bitmask = 0x{plaintext[72]:x}', 'black'
                yield prefix + 'Time0 = ' + ' '.join(f'{value:x}' for value in plaintext[40:48]), 'black'
                setup_time0 = int.from_bytes(plaintext[40:48], 'big')
                yield prefix + f'Time0 =  {setup_time0} ', 'black'
                yield from _box2box_start_delay(timestamp, setup_time0, session_id, anchor_time,
                                               device_times, setup_time0, 'setup',
                                               fallback=include_fallback,
                                               context=(last_anchor_info, last_offset_info))
                label = _session_label(session_id, otherwise=f'{session_id:x}')
                yield prefix + f'ControlRangingSessionSetup2 received {label} {len(active_sessions):x}', 'orange'
                was_active = session_id in active_sessions
                active_sessions.add(session_id)
                if session_id == 7 and not was_active:
                    text = f'IDG-Ranging has been started. Amount of active session = {len(active_sessions):x}'
                else:
                    verb = 'RE-started' if was_active else 'started'
                    text = f'Session {label} has been {verb}. Amount of active session = {len(active_sessions):x}'
                yield prefix + text, 'orange'
            elif msg_id in status_names and len(payload) >= 7:
                acknowledgements = (payload[4] & 15, payload[4] >> 4, payload[5] & 15, payload[5] >> 4)
                labels = ['NACK' if value == 0 else 'ACK' if value == 1 else str(value)
                          for value in acknowledgements]
                state = status_labels[payload[6]] if payload[6] < len(status_labels) else ''
                yield prefix + (
                    f'StatusRangingSessionFBD_{status_names[msg_id]} received with '
                    f'Setup={labels[0]}, Start={labels[1]}, Suspend={labels[2]}, '
                    f'Resume={labels[3]}, Status={state}'
                ), 'red' if 0 in acknowledgements else 'black'
            elif msg_id in _TIME_SYNC_DEVICE_OFFSET_IDS and len(payload) >= 15:
                session_id = payload[0]
                device_times[session_id] = int.from_bytes(payload[2:10], 'little')
                rx_windows[session_id] = (payload[1], int.from_bytes(payload[10:12], 'little'), payload[12])
                window = _box2box_rx_window(session_id, rx_windows[session_id])
                if window is not None:
                    yield prefix + window, 'violet'
                offset = ''.join(f'{value:x}' for value in reversed(payload[2:10]))
                last_offset_info = prefix + (
                    f'TimeSyncDeviceOffsetFBD_{_TIME_SYNC_DEVICE_OFFSET_NAMES[msg_id]} received '
                    f'with TimeOffset = {offset} for Session ID = {_session_label(session_id, otherwise=session_id)} (IRK=0x{payload[1]:x})'
                )
                if not include_fallback:
                    yield last_offset_info, 'black'
            elif msg_id in _STATUS_UWB_RANGING_TARGET_IDS and len(payload) >= 20:
                anchor = _STATUS_UWB_RANGING_ANCHOR_NAMES[msg_id]
                session_id = payload[0]
                status1, status2, status3, status4 = payload[16:20]
                tof = int.from_bytes(payload[1:3], 'little')
                if tof != 65535 and payload[6] != 255:
                    ranging_counts['valid'] += 1
                elif tof == 65535:
                    ranging_counts['both_invalid' if payload[6] == 255 else 'tof_invalid'] += 1
                if tof in (0, 65535):
                    status_counts[status1] += 1
                key = (anchor, session_id)
                if status1 == 0xe1 or key in fallback_sessions:
                    verb = 'entered' if status1 == 0xe1 else 'left'
                    yield prefix + (
                        f'{anchor}-Anchor {verb} FallbackMode for session {_session_label(session_id, otherwise=f"{session_id:x}")} '
                        f'with status 0x{status1:x} 0x{status2:x} 0x{status3:x} 0x{status2:x}'
                    ), 'orange'
                    if status1 == 0xe1:
                        fallback_sessions.add(key)
                    else:
                        fallback_sessions.discard(key)
                label = _session_label(session_id)
                yield prefix + f'StatusUWBRangingFBD_{anchor} received with SessionIDShort {label}', 'blue'
                if (status1, status2, status3) == (0xfd, 1, 0):
                    error_counts[status4 if status4 in multiranging_labels else None] += 1
                    status4_meaning = (
                        'Starttime obsolete - Round skipped with unknown reason'
                        if status4 in (0x42, 0x43) else multiranging_labels.get(status4, '')
                    )
                    yield prefix + (
                        f'{anchor:>2}: Session ID={label}: 0xfd 0x01 0x00 0x{status4:x}: '
                        + status4_meaning
                    ), 'red' if status4 in (0x42, 0x43) else 'blue'
                if status1 == 0xe1 and status2 in fallback_labels:
                    yield prefix + (
                        f'{anchor:>2}: Session ID={label}: 0xfe 0x{status2:x}: '
                        + fallback_labels[status2]
                    ), 'blue'
            elif msg_id == _CONTROL_FBD_ID and len(payload) >= 3:
                mode = power_modes.get(payload[2] & 0x0f)
                if mode is not None:
                    yield prefix + f"New Power Mode '{mode}' requested.", 'green'
            elif msg_id in _RESUME_RANGING_SESSION_TARGET_IDS and len(payload) >= 15 and payload[0] == 3:
                session_id = payload[1]
                yield prefix + (
                    f'ResumeRangingSessionFBD_{_RESUME_RANGING_SESSION_ANCHOR_NAMES[msg_id]} '
                    f'= RESUME SESSION with Session ID = {_session_label(session_id, otherwise=session_id)}'
                ), 'green'
                yield from _box2box_start_delay(
                    timestamp, int.from_bytes(payload[7:15], 'little'), session_id,
                    anchor_time, device_times, setup_time0, 'fbd-only',
                    fallback=include_fallback, context=(last_anchor_info, last_offset_info)
                )
                active_sessions.add(session_id)
                yield prefix + f'Session {_session_label(session_id, otherwise=f"{session_id:x}")} has been resumed. Amount of active session = {len(active_sessions):x}', 'orange'
            elif msg_id == _CONTROL_RANGING_SESSION_ID and len(payload) >= 4:
                function, session_id = payload[2:4]
                action = _RANGING_FUNCTION_MEANINGS.get(f'{function:02X}')
                detail = f'{action.upper()} SESSION with Session ID = {_session_label(session_id, otherwise=session_id)}' if action else str(function)
                yield prefix + f'ControlRangingSession_Function = {detail}', 'green'
                if function in (2, 4, 5):
                    fallback_sessions = {key for key in fallback_sessions if key[1] != session_id}
                if function in (2, 5):
                    rx_windows.pop(session_id, None)
                elif function == 3 and session_id in rx_windows:
                    window = _box2box_rx_window(session_id, rx_windows[session_id])
                    if window is not None:
                        yield prefix + window, 'violet'
                if function == 3 and len(payload) >= 17:
                    yield from _box2box_start_delay(
                        timestamp, int.from_bytes(payload[9:17], 'little'), session_id,
                        anchor_time, device_times, setup_time0,
                        fallback=include_fallback, context=(last_anchor_info, last_offset_info)
                    )
                was_active = session_id in active_sessions
                if function == 3:
                    active_sessions.add(session_id)
                elif function in (2, 4, 5):
                    active_sessions.discard(session_id)
                transitions = {2: 'suspended', 3: 'resumed', 4: 'stopped', 5: 'deleted'}
                if function in transitions and (function != 5 or was_active):
                    yield prefix + (
                        f'Session {_session_label(session_id, otherwise=f"{session_id:x}")} has been {transitions[function]}. '
                        f'Amount of active session = {len(active_sessions):x}'
                    ), 'orange'
                if function == 5 and session_id == 0xf0:
                    active_sessions.intersection_update({21})
                    yield prefix + (
                        'All Sessions has been deleted. '
                        f'Amount of active session = {len(active_sessions):x}'
                    ), 'orange'
    if ranging_counts['valid']:
        valid, tof_invalid, both_invalid = (ranging_counts[key] for key in ('valid', 'tof_invalid', 'both_invalid'))
        invalid_percent = (tof_invalid + both_invalid) / sum(ranging_counts.values()) * 100
        yield '   ', 'black'
        yield (
            f'Valid Ranging Results = {valid}, ToF invalid = {tof_invalid}, '
            f'ToF and First Path Data invalid = {both_invalid}, {invalid_percent:.2f}% invalid'
        ), 'blue'
        yield (
            f'Skip {status_counts[0xfd]} times, Poll not received {status_counts[0xfa]} times, '
            f'Final not received {status_counts[0xf9]} times, Final Data not received {status_counts[0xf8]} times, '
            f'Final Data empty {status_counts[0xfc]} times, Duty Cycle limit {status_counts[0xf7]} times'
        ), 'blue'
        for code in (*range(0x40, 0x47), None):
            if error_counts[code]:
                label = f'{code:x}' if code is not None else '??'
                yield f'0xfd 0x01 0x00 0x{label}: {error_counts[code]} times', 'blue'
        yield '   ', 'black'


def _build_box2box_html(input_file):
    """Render CAPL-style output using the Write Window text colors."""
    colors = {'blue': '#add8e6', 'green': '#00c800', 'orange': '#fa9600',
              'black': '#ffffff', 'red': '#c80000', 'violet': '#c800fa', 'olive': '#646400'}
    output = '\n'.join(
        f'<span class="{"box2box-result" if "received with SessionIDShort" in text else "box2box-event"}" '
        f'style="color:{colors[color]}">{html.escape(text)}</span>'
        for text, color in _box2box_events(input_file)
    )
    return (
        '<section id="box2box-write-window" class="reader-overview">'
        '<h2>Box2Box Write Window</h2>'
        '<label><input id="box2box-show-results" type="checkbox"> Ranging Results</label>'
        '<pre class="text-report box2box-output" style="background:#000000;color:#ffffff">'
        f'{output}</pre></section>'
        '<style>.box2box-output span { display:block; } '
        '.box2box-output { white-space:normal; } '
        '.box2box-output span { white-space:pre-wrap; overflow-wrap:anywhere; } '
        '.box2box-output .box2box-result { display:none; } '
        '.box2box-output.show-results .box2box-result { display:block; }</style>'
        '<script>document.getElementById("box2box-show-results").addEventListener("change", function() {'
        'document.querySelector(".box2box-output").classList.toggle("show-results", this.checked);'
        '});</script>'
    )


def _normalize_id(message_id):
    """Normalize CAN ID to lowercase hex string without 0x prefix."""
    msg_id = message_id.strip().lower()
    if msg_id.startswith('0x'):
        msg_id = msg_id[2:]
    return msg_id


def _is_hex_byte(token):
    """Check if token is a valid hex byte (2 characters)."""
    if len(token) != 2:
        return False
    for ch in token:
        if ch not in '0123456789abcdefABCDEF':
            return False
    return True


def _find_canfd_payload_bounds(parts):
    """Return (data_start, data_end) for a tokenized Vector ASC CANFD line."""
    for i in range(7, len(parts) - 2):
        # Payload marker: <BRS> <ESI> <DLC> <byte-count> <payload...>.
        # DLC can be 0..8 or A..F/a..f for CAN FD payloads larger than 8 bytes.
        if (
            len(parts[i]) == 1
            and parts[i].lower() in '0123456789abcdef'
            and parts[i - 1] in ('0', '1')
            and parts[i - 2] in ('0', '1')
            and parts[i + 1].isdigit()
        ):
            dlc_len = int(parts[i + 1])
            data_start = i + 2
            data_end = data_start + dlc_len
            if data_end <= len(parts):
                return data_start, data_end
            return None, None

    return None, None


def _extract_can_data_bytes(line, target_ids):
    """Extract the full CANFD payload for a matching line.

    Returns (is_matching_id, msg_id, data_bytes) where data_bytes is a list of
    upper-case hex tokens (or None for non-hex tokens), or None if the payload
    could not be located.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith('//'):
        return False, None, None

    parts = stripped.split()
    if len(parts) < 10 or parts[1] != 'CANFD':
        return False, None, None
    if not _line_matches_configured_canfd_channel(parts):
        return False, None, None

    msg_id = _normalize_id(parts[4])
    if msg_id not in target_ids:
        return False, None, None

    data_start, data_end = _find_canfd_payload_bounds(parts)
    if data_start is None or data_end is None:
        return True, msg_id, None

    data_bytes = [
        parts[i].upper() if _is_hex_byte(parts[i]) else None
        for i in range(data_start, data_end)
    ]
    return True, msg_id, data_bytes


def _build_document(input_file, display_input_file):
    """Wrap the Box2Box chapter in a standalone HTML page."""
    name = html.escape(os.path.basename(display_input_file))
    section = _build_box2box_html(input_file)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Box2Box Write Window - {name}</title>
<style>
    body {{ margin: 0; padding: 2rem; background: #1e1e1e; color: #d4d4d4; font-family: Arial, sans-serif; }}
    .text-report {{ padding: 1rem; font-family: Consolas, 'Courier New', monospace; line-height: 1.4; }}
    .reader-overview h2 {{ color: #dcdcaa; }}
</style>
</head>
<body>
<p>Input file: {html.escape(display_input_file)}</p>
{section}
</body>
</html>
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Create the Box2Box Write Window report from a CANFD trace.')
    parser.add_argument(
        '--config',
        default=_default_config_path(),
        help='Path to the project JSON config (default: beside the script/executable).',
    )
    parser.add_argument('input_file', nargs='?', help='Input ASC or BLF trace file.')
    parser.add_argument('output_path', nargs='?', help='Output HTML path (default: <trace>_box2box.html).')
    parser.add_argument('--no-browser', action='store_true', help='Do not open the generated report.')
    parser.add_argument('--live', action='store_true', help='Read ASC lines from stdin and print the results at once.')
    parser.add_argument('--show-results', action='store_true', help='With --live: also print the ranging results.')
    args = parser.parse_args()
    if not args.live and args.input_file is None:
        parser.error('the following arguments are required: input_file')

    try:
        _apply_project_config(args.config)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(f'cannot load project config {args.config}: {error}')

    if args.live:
        # No //BusMapping: lines here; CANtrix already sends only the selected CAN channel.
        print('Live mode: waiting for CAN messages...', flush=True)
        _run_live(iter(sys.stdin.readline, ''), sys.stdout, args.show_results)
        print('Live mode: recording ended.')
        sys.exit(0)

    input_file = args.input_file
    is_blf = os.path.splitext(input_file)[1].lower() == '.blf'
    output_path = args.output_path or os.path.splitext(input_file)[0] + '_box2box.html'
    if os.path.abspath(output_path) == os.path.abspath(input_file):
        parser.error('output path must differ from the input trace')
    try:
        with _analysis_input_file(input_file) as analysis_input_file:
            _configure_canfd_channel_filter(analysis_input_file, warn_if_unmapped=not is_blf)
            if _ASC_CANFD_CHANNEL_FILTER is not None and not _ASC_CANFD_CHANNEL_FILTER:
                print(f"ERROR: CANFD bus mapping for {_TARGET_CANFD_BUS_NAME} was not found in {input_file}")
                sys.exit(2)
            document = _build_document(analysis_input_file, input_file)
    except (OSError, BLFTraceError) as error:
        parser.error(str(error))
    with open(output_path, 'w', encoding='utf-8') as handle:
        handle.write(document)
    print(f'HTML report saved to: {output_path}')
    if not args.no_browser and not webbrowser.open(Path(output_path).resolve().as_uri()):
        print('WARNING: The report could not be opened automatically.')
