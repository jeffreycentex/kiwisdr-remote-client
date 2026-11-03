# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for control ownership, recording safety, and trust boundaries."""
import concurrent.futures
import http.client
import os
import socket
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

import numpy as np

from kiwiremote.api import ApiServer
from kiwiremote.radio import _check_redirect, KiwiTlsError, _BoundedStream
from kiwiremote.service import KiwiService, ApiError
from kiwiremote.sinks import WavSink, BufferedFileSink, M4aSink, HAVE_PYAV
from kiwiremote.state import ServerSpec
from mod_pywebsocket._stream_base import ConnectionTerminatedException


class TestRecordingSafety(unittest.TestCase):
    def test_parts_preserve_existing_files_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'capture.wav')
            existing = os.path.join(directory, 'capture_1.wav')
            with open(existing, 'wb') as f:
                f.write(b'previous recording')
            sink = WavSink(path)
            sink.set_input_rate(12000)
            sink.write(np.zeros(12000, dtype=np.int16))
            sink.set_input_rate(24000)
            sink.write(np.zeros(24000, dtype=np.int16))
            sink.close()
            self.assertEqual(sink.seconds(), 2)
            with open(existing, 'rb') as f:
                self.assertEqual(f.read(), b'previous recording')
            self.assertTrue(sink.current_path.endswith('capture_2.wav'))

    def test_rotation_and_buffer_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = WavSink(os.path.join(directory, 'capture.wav'))
            sink.MAX_DATA_BYTES = 200
            buffered = BufferedFileSink(sink)
            buffered.set_input_rate(12000)
            for _ in range(3):
                buffered.write(np.zeros(100, dtype=np.int16))
            buffered.close()
            self.assertAlmostEqual(sink.seconds(), 300 / 12000)
            for name in os.listdir(directory):
                with wave.open(os.path.join(directory, name)) as f:
                    self.assertEqual(f.getnframes(), 100)

    def test_concurrent_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'capture.wav')
            with concurrent.futures.ThreadPoolExecutor() as pool:
                sinks = list(pool.map(lambda _: WavSink(path), range(12)))
            try:
                self.assertEqual(len({s.current_path for s in sinks}), 12)
            finally:
                for s in sinks:
                    s.close()

    @unittest.skipUnless(HAVE_PYAV, "PyAV not installed")
    def test_m4a_parts_are_playable(self):
        import av
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'capture.m4a')
            sink = BufferedFileSink(M4aSink(path))
            for rate in (12000, 24000):
                sink.set_input_rate(rate)
                sink.write(np.zeros(rate, dtype=np.int16))
            sink.close()
            self.assertEqual(sink.seconds(), 2)
            for name in os.listdir(directory):
                with av.open(os.path.join(directory, name)) as container:
                    self.assertGreater(sum(f.samples for f in container.decode(audio=0)), 40000)

    def test_writer_overflow_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = BufferedFileSink(WavSink(os.path.join(directory, 'capture.wav')))
            try:
                sink.MAX_BYTES = 2
                with self.assertRaisesRegex(RuntimeError, 'cannot keep up'):
                    sink.write(np.zeros(2, dtype=np.int16))
            finally:
                sink.close()


class TestServiceOwnership(unittest.TestCase):
    def test_simultaneous_recording_has_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            svc = KiwiService(record_dir=directory, start_ticker=False)
            barrier = threading.Barrier(8)
            def start(n):
                barrier.wait()
                try:
                    svc.start_recording(name=str(n))
                    return 'started'
                except ApiError as exc:
                    return exc.code
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    outcomes = list(pool.map(start, range(8)))
                self.assertEqual(outcomes.count('started'), 1)
                self.assertEqual(outcomes.count('conflict'), 7)
            finally:
                svc.shutdown()

    def test_connect_wait_does_not_block_disconnect(self):
        svc = KiwiService(start_ticker=False)
        started = threading.Event()
        def connect(spec):
            svc.radio.state = 'connecting'
            started.set()
        try:
            with patch.object(svc.radio, 'connect', side_effect=connect):
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    waiting = pool.submit(svc.connect, 'localhost', wait=10)
                    self.assertTrue(started.wait(2))
                    stopped = pool.submit(svc.disconnect)
                    stopped.result(timeout=2)
                    waiting.result(timeout=2)
        finally:
            svc.shutdown()
            svc.shutdown()  # repeated shutdown is harmless

    def test_invalid_combined_tune_is_atomic(self):
        svc = KiwiService(start_ticker=False)
        try:
            previous = svc.radio.tune
            with self.assertRaises(ApiError):
                svc.tune(freq_hz=-1, mode='AM')
            self.assertEqual(svc.radio.tune, previous)
        finally:
            svc.shutdown()

    def test_invalid_duration_does_not_change_state(self):
        svc = KiwiService(start_ticker=False)
        try:
            for value in (float('nan'), float('inf'), -1):
                with self.assertRaises(ApiError):
                    svc.start_recording(max_seconds=value)
                self.assertFalse(svc.recording_info()['active'])
        finally:
            svc.shutdown()

    def test_recording_open_failure_is_reported(self):
        svc = KiwiService(start_ticker=False)
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(svc.radio, 'start_recording', return_value=False):
                svc.record_dir = directory
                with self.assertRaises(ApiError):
                    svc.start_recording()
        finally:
            svc.shutdown()


class TestTransportSafety(unittest.TestCase):
    def test_redirect_policy(self):
        for args in [('a',443,True,'a',80,False,''), ('a',443,True,'b',443,True,'secret'),
                     ('a',8073,False,'a',8074,False,'secret')]:
            with self.assertRaises(KiwiTlsError):
                _check_redirect(*args)
        _check_redirect('a',8073,False,'b',443,True,'')
        _check_redirect('a',443,True,'a',443,True,'secret')

    def test_oversized_websocket_frame_rejected_before_read(self):
        stream = object.__new__(_BoundedStream)
        with self.assertRaises(ConnectionTerminatedException):
            stream.receive_bytes(1024 * 1024 + 1)

    def test_ipv6_round_trip(self):
        for address in ('[2001:db8::1]:8073', 'https://[2001:db8::1]:443'):
            spec = ServerSpec.parse(address)
            self.assertEqual(ServerSpec.parse(spec.label()), spec)


class TestHttpSafety(unittest.TestCase):
    def setUp(self):
        self.svc = KiwiService(start_ticker=False)
        self.api = ApiServer(self.svc, port=0)
        self.api.start()

    def tearDown(self):
        self.api.stop()
        self.svc.shutdown()

    def test_bad_framing_rejected_without_waiting_for_body(self):
        for headers in ('Content-Length: -1', 'Content-Length: 1\r\nContent-Length: 2',
                        'Transfer-Encoding: chunked', 'Content-Length: 65537'):
            with socket.create_connection(('127.0.0.1', self.api.port), timeout=2) as sock:
                request = ('POST /api/v1/tune HTTP/1.1\r\nHost: localhost\r\n'
                           'Content-Type: application/json\r\n' + headers + '\r\n\r\n')
                sock.sendall(request.encode())
                response = sock.recv(4096)
                self.assertIn(response.split(b' ')[1], (b'400', b'413'))

    def test_nonfinite_number_returns_400(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.api.port, timeout=2)
        try:
            connection.request('POST', '/api/v1/record/start', '{"max_seconds":NaN}',
                               {'Content-Type':'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            self.assertFalse(self.svc.recording_info()['active'])
        finally:
            connection.close()
