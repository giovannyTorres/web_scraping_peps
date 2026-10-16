"""Real loopback HTTP readers see complete catalogues during permission updates."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from peps.app import create_server
from peps.engine import Engine
from peps.news import NewsAcquirer


class SourceConfigurationHTTPTests(unittest.TestCase):
    def test_news_permissions_publish_atomically_to_concurrent_http_readers(self):
        """Hold publication itself; /api/news must still return the old catalogue.

        GET intentionally runs while the POST owns the application's mutation
        lock. An atomic rename keeps readers independent without exposing the
        writer's empty or partially serialized destination file.
        """
        entered, release = threading.Event(), threading.Event()
        results, errors = {}, []
        original_replace = os.replace
        with tempfile.TemporaryDirectory(prefix='webscope-source-http-') as directory, ExitStack() as stack:
            engine = Engine(directory)
            news = NewsAcquirer(engine)
            before = news.config_path.read_bytes()
            source = news.sources()[0]
            target_enabled = not bool(source.get('enabled'))
            server, token = create_server(engine)
            stack.callback(server.server_close)
            address = f'http://127.0.0.1:{server.server_port}'
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            stack.callback(lambda: serving.join(timeout=5))
            stack.callback(server.shutdown)

            def request(path, data=None):
                headers = {'X-PEPS-Token': token}
                body = None
                if data is not None:
                    headers['Content-Type'] = 'application/json'
                    body = json.dumps(data).encode()
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    response = opener.open(urllib.request.Request(address + '/api/' + path,
                        data=body, headers=headers), timeout=10)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    return response.status, json.loads(response.read())

            def held_replace(temporary, destination, *args, **kwargs):
                if Path(destination) == news.config_path:
                    try:
                        self.assertEqual(Path(temporary).parent, news.config_path.parent)
                        self.assertEqual(news.config_path.read_bytes(), before)
                        staged = json.loads(Path(temporary).read_text(encoding='utf-8'))
                        staged_source = next(row for row in staged['sources'] if row['id'] == source['id'])
                        self.assertEqual(staged_source['enabled'], target_enabled)
                        entered.set()
                        if not release.wait(10):
                            raise TimeoutError('The test did not release atomic publication')
                    except BaseException as exc:
                        errors.append(exc)
                        entered.set()
                        raise
                return original_replace(temporary, destination, *args, **kwargs)

            def update():
                try:
                    results['post'] = request('news-source', {'id': source['id'], 'enabled': target_enabled,
                        'terms_approved': True, 'download_articles': False, 'article_terms_approved': False})
                except BaseException as exc:
                    errors.append(exc)

            with patch('peps.collector.SafeFetcher', side_effect=AssertionError('External HTTP forbidden')), patch('os.replace', held_replace):
                writer = threading.Thread(target=update, daemon=True)
                writer.start()
                try:
                    self.assertTrue(entered.wait(5), 'Permission updates must publish an already complete temporary file')
                    if errors:
                        raise errors[0]
                    status, during = request('news')
                    self.assertEqual(status, 200, during)
                    old = next(row for row in during['sources'] if row['id'] == source['id'])
                    self.assertEqual(bool(old.get('enabled')), bool(source.get('enabled')))
                    self.assertEqual(news.config_path.read_bytes(), before)
                finally:
                    release.set()
                    writer.join(timeout=10)
                self.assertFalse(writer.is_alive(), 'The permission writer did not finish')
                if errors:
                    raise errors[0]
            self.assertEqual(results['post'][0], 200, results['post'])
            status, after = request('news')
            self.assertEqual(status, 200, after)
            updated = next(row for row in after['sources'] if row['id'] == source['id'])
            self.assertEqual(updated['enabled'], target_enabled)
            self.assertTrue(engine.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
