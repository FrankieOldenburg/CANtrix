"""STN/ELM serial protocol tests. Run with PYTHONPATH=Sourcen python3 -m unittest discover -s tests."""
import queue
import sys
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock, patch

import can

from CAN_TraceRecorder import CanReader, _obd_frame, open_bus, test_connection as check_connection


class SerialPeer:
    """A scripted, fragmented serial peer that never opens a real port."""

    def __init__(self, stn=False, user1=True, rejected=(), streams=()):
        self.stn = stn
        self.user1 = user1
        self.rejected = set(rejected)
        self.streams = deque(streams)
        self.output = bytearray()
        self.writes = []
        self.monitoring = False
        self.closed = False
        self.bitrate = 500000

    @property
    def in_waiting(self):
        return min(3, len(self.output))

    def reset_input_buffer(self):
        self.output.clear()

    def read(self, size):
        chunk = bytes(self.output[:size])
        del self.output[:size]
        return chunk

    def write(self, data):
        text = data.decode('ascii')
        self.writes.append(text)
        if text == ' ':
            if self.monitoring:
                self.output.extend(b'STOPPED\r>')
                self.monitoring = False
            return len(data)
        command = text.strip()
        if self.monitoring:
            raise AssertionError('Command sent during monitoring: ' + command)
        if command in self.rejected:
            reply = '?'
        elif command in ('ATZ', 'ATI'):
            reply = 'ELM327 v2.3'
        elif command == 'STI':
            reply = 'STN2120 v5.8.1' if self.stn else '?'
        elif command == 'STPBRR':
            reply = str(self.bitrate)
        elif command.startswith('STPBR '):
            self.bitrate = int(command.split()[1])
            reply = 'OK'
        elif command.startswith('ATPB') and not self.user1:
            reply = '?'
        elif command in ('STM', 'ATMA'):
            expected = 'STM' if self.stn else 'ATMA'
            if command != expected:
                raise AssertionError('Unexpected monitor command: ' + command)
            pending = self.streams.popleft() if self.streams else b''
            self.output.extend(pending)
            self.monitoring = not pending.endswith(b'>')
            return len(data)
        elif command.startswith('STPX'):
            reply = 'OK'
        elif command.startswith(('AT', 'ST')):
            reply = 'OK'
        else:
            reply = ''  # ELM ATR0 returns directly to the prompt after sending
        self.output.extend((reply + '\r>').encode('ascii'))
        return len(data)

    def close(self):
        self.closed = True


def config(**overrides):
    return dict(dict(interface='obdlink', port='COM-MOCK', serial_baud=115200, bitrate=500000), **overrides)


class OBDSerialTest(unittest.TestCase):
    def connect(self, peer, **settings):
        with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=lambda *args, **kwargs: peer)}):
            return open_bus(config(**settings))

    def test_stn_monitor_send_and_resume(self):
        peer = SerialPeer(stn=True, streams=[b'123 2 AA BB\r18 DA F1 10 1 CC\r', b'321 1 DD\r'])
        bus = self.connect(peer, bitrate=125000)
        try:
            standard, extended = bus.recv(), bus.recv()
            self.assertEqual((standard.arbitration_id, bytes(standard.data)), (0x123, b'\xaa\xbb'))
            self.assertEqual((extended.arbitration_id, bytes(extended.data)), (0x18DAF110, b'\xcc'))
            self.assertTrue(extended.is_extended_id)
            self.assertGreater(standard.timestamp, 0)
            bus.send(can.Message(arbitration_id=0x18DAF110, is_extended_id=True, data=b'\x11\x22'))
            self.assertEqual(bus.recv().arbitration_id, 0x321)
            self.assertIn('STCMM 0\r', peer.writes)
            self.assertIn('STPBR 125000\r', peer.writes)
            self.assertIn('STP 32\r', peer.writes)
            self.assertIn('STPX h:18DAF110,d:1122\r', peer.writes)
            self.assertEqual(peer.writes.count('STM\r'), 2)
        finally:
            bus.shutdown()
        self.assertTrue(peer.closed)

    def test_elm_protocol_and_headers(self):
        peer = SerialPeer(streams=[b'123100\r'])
        bus = self.connect(peer, bitrate=250000)
        try:
            self.assertEqual(bus.recv().arbitration_id, 0x123)
            bus.send(can.Message(arbitration_id=0x456, is_extended_id=False, data=b'\x12'))
            bus.send(can.Message(arbitration_id=0x18DA0102, is_extended_id=True, data=b'\xfe'))
            self.assertIn('ATCSM1\r', peer.writes)
            self.assertIn('ATPBE002\r', peer.writes)
            self.assertIn('ATPB6002\r', peer.writes)
            self.assertIn('ATSH456\r', peer.writes)
            self.assertIn('ATSH18DA0102\r', peer.writes)
            self.assertIn('FE\r', peer.writes)
        finally:
            bus.shutdown()

    def test_legacy_elm_protocol_fallback(self):
        peer = SerialPeer(user1=False)
        bus = self.connect(peer, bitrate=250000)
        try:
            self.assertIn('ATTP8\r', peer.writes)
            self.assertIn('ATV1\r', peer.writes)
        finally:
            bus.shutdown()

    def test_buffer_full_restarts_monitor(self):
        peer = SerialPeer(stn=True, streams=[b'BUFFER FULL\r>', b'123 0\r'])
        bus = self.connect(peer)
        try:
            self.assertEqual(bus.recv().dlc, 0)
            self.assertEqual(peer.writes.count('STM\r'), 2)
        finally:
            bus.shutdown()

    def test_connection_without_traffic_and_unsupported_commands(self):
        idle = SerialPeer()
        with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=lambda *args, **kwargs: idle)}):
            self.assertTrue(check_connection(config())[0])
        self.assertTrue(idle.closed)
        rejected = SerialPeer(rejected=['ATCSM1'])
        with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=lambda *args, **kwargs: rejected)}):
            self.assertFalse(check_connection(config())[0])
        self.assertTrue(rejected.closed)
        self.assertNotIn('ATMA\r', rejected.writes)

    def test_recorder_thread_uses_serial_port_and_delivers_classic_frames(self):
        peer = SerialPeer(stn=True, streams=[b'321 1 FE\r'])
        messages = queue.Queue()
        factory = Mock(return_value=peer)
        reader = CanReader(config(fd=True, port='COM7', serial_baud=38400), messages)
        with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=factory)}):
            reader.start()
            try:
                self.assertEqual(messages.get(timeout=2)[0], 'status')
                kind, frame = messages.get(timeout=2)
                self.assertEqual((kind, frame.channel, frame.arbitration_id, frame.is_fd), ('msg', 0, 0x321, False))
            finally:
                reader.stop()
                reader.join(timeout=2)
        factory.assert_called_once_with('COM7', baudrate=38400, timeout=0.025, write_timeout=0.5)
        self.assertFalse(reader.is_alive())
        self.assertTrue(peer.closed)

    def test_frame_validation(self):
        for line in ('7FF 2 01', 'FFF 1 AA', '123 9 01', '18DA0102 2 GG FF', 'BUFFER FULL'):
            self.assertIsNone(_obd_frame(line, 1.0))
        self.assertEqual(_obd_frame('123 0', 1.0).dlc, 0)
        self.assertIsNone(_obd_frame('FFFFFFFF 0', 1.0))

    def test_unsupported_send_never_leaves_monitor_mode(self):
        peer = SerialPeer(streams=[b'123 1 AF\r'])
        bus = self.connect(peer)
        try:
            bus.recv()
            writes = list(peer.writes)
            for message in (can.Message(data=b'', is_extended_id=False), can.Message(is_fd=True, data=b'\x11')):
                with self.assertRaises(ValueError):
                    bus.send(message)
            self.assertEqual(peer.writes, writes)
        finally:
            bus.shutdown()
