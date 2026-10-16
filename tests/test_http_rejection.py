"""Rejected local requests deliver their 403 without parsing or mutating data.

An HTTP/1.0 close with an unread POST body can reset a Windows connection before
the client receives the response. These tests observe real rejected requests and
bound missing/slow-body cleanup independently of OS scheduling.
"""
from email.message import Message
import http.client
import json
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from peps.app import create_server
from peps.engine import Engine


class RejectedBodyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name)
        self.server, self.token = create_server(self.engine)
        self.addCleanup(self.server.server_close)

    def handler(self, lengths=('10',), *, transfer=None, method='POST'):
        handler = object.__new__(self.server.RequestHandlerClass)
        handler.command = method
        handler.headers = Message()
        for length in lengths:
            handler.headers['Content-Length'] = length
        if transfer is not None:
            handler.headers['Transfer-Encoding'] = transfer
        return handler

    def test_slow_trickle_uses_one_total_deadline_and_restores_the_socket(self):
        handler = self.handler()
        clock = SimpleNamespace(value=0.)
        timeouts, reads = [], []

        class Connection:
            timeout = 7.

            def gettimeout(self):
                return self.timeout

            def settimeout(self, timeout):
                self.timeout = timeout
                timeouts.append(timeout)

        class SlowReader:
            def read1(self, size):
                reads.append(size)
                delay = .075
                available = handler.connection.timeout
                clock.value += min(delay, available)
                if available < delay:
                    raise TimeoutError('The body arrived too slowly')
                return b'x'

        handler.connection, handler.rfile = Connection(), SlowReader()
        with patch('peps.app.time.monotonic', side_effect=lambda: clock.value):
            handler.discard_rejected_body()
        self.assertAlmostEqual(clock.value, .2)
        self.assertEqual(len(reads), 3)
        self.assertLess(timeouts[1], timeouts[0])
        self.assertLess(timeouts[2], timeouts[1])
        self.assertEqual(handler.connection.timeout, 7.)
        self.assertTrue(handler.close_connection)

    def test_large_ambiguous_and_unframed_bodies_are_never_read(self):
        cases = [dict(lengths=value) for value in
                 ((), ('0',), ('65537',), ('16000001',), ('-1',), ('bad',), ('2,2',), ('2', '2'), ('٢',))]
        cases += [dict(lengths=('2',), transfer='chunked'), dict(lengths=('2',), transfer=''),
                  dict(lengths=('2',), method='GET')]
        for options in cases:
            with self.subTest(options=options):
                handler = self.handler(**options)
                handler.connection = SimpleNamespace(gettimeout=lambda: self.fail('No read timeout is needed'))
                handler.rfile = SimpleNamespace(read1=lambda _: self.fail('This framing must not be drained'))
                handler.discard_rejected_body()
                self.assertTrue(handler.close_connection)

    def test_small_rejected_posts_are_consumed_before_403_without_json_or_actions(self):
        observed = []
        base = self.server.RequestHandlerClass

        class ReaderProbe:
            def __init__(self, reader):
                self.reader, self.body_bytes = reader, 0

            def read1(self, size):
                chunk = self.reader.read1(size)
                self.body_bytes += len(chunk)
                return chunk

            def __getattr__(self, key):
                return getattr(self.reader, key)

        class InstrumentedHandler(base):
            def setup(self):
                super().setup()
                self.rfile = ReaderProbe(self.rfile)

            def reply(self, data, code=200, content_type='application/json; charset=utf-8'):
                observed.append((code, self.rfile.body_bytes))
                return super().reply(data, code, content_type)

        self.server.RequestHandlerClass = InstrumentedHandler
        thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        cases = [({}, 'Sesión inválida'),
                 ({'X-PEPS-Token': self.token, 'Host': 'attacker.invalid'}, 'Host no permitido'),
                 ({'X-PEPS-Token': self.token, 'Origin': 'https://attacker.invalid'}, 'Origen no permitido')]
        audit_before = self.engine.store.stats()['audit']
        replies = []
        with patch('peps.app.json.loads', side_effect=AssertionError('Unauthorized JSON must not be parsed')), \
                patch('peps.news.NewsAcquirer.acquire') as acquire, patch.object(self.engine, 'rescore') as score:
            for headers, expected in cases:
                connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
                try:
                    connection.request('POST', '/api/workflow-run', body=b'{!', headers=headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    replies.append((response.read(), expected))
                finally:
                    connection.close()
            acquire.assert_not_called()
            score.assert_not_called()
        self.assertEqual(observed, [(403, 2)] * len(cases))
        for raw, expected in replies:
            self.assertEqual(json.loads(raw)['error'], expected)
        self.assertEqual(self.engine.store.stats()['audit'], audit_before)
        self.assertFalse(self.engine.online)


if __name__ == '__main__':
    unittest.main()
