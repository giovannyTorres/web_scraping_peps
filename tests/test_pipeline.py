import json
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.store import digest, now


class PipelineTests(unittest.TestCase):
    def test_rss_discovery_can_download_an_html_article_from_the_same_source(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = Engine(temp, online=True)
            engine.store.import_entities([{'id': 'target', 'name': 'Arturo Pérez Montes'}])
            source = {'id': 'publisher-feed', 'kind': 'rss', 'document_kind': 'news',
                      'url': 'https://example.org/feed?q={query}', 'allowed_hosts': ['example.org'],
                      'enabled': True, 'terms_approved': True, 'discovery': True}
            engine.config_path.write_text(json.dumps({'sources': [source]}), encoding='utf-8')

            class Fetcher:
                def fetch(self, url, _source):
                    if '/feed?' in url:
                        body = ('<rss><channel><item><title>Resumen</title>'
                                '<link>https://example.org/article</link></item></channel></rss>')
                    else:
                        body = ('<html><head><title>Artículo editorial</title>'
                                '<meta property="article:published_time" content="2026-10-01">'
                                '</head><body><p>Arturo Pérez Montes inauguró una biblioteca.</p></body></html>')
                    return {'url': url, 'status': 'ok', 'body': body,
                            'retrieved_at': now(), 'sha256': digest(body)}

            engine.queue_discovery('2026-10-15')
            engine._execute(engine.store.claim(), Fetcher())
            engine._execute(engine.store.claim(), Fetcher())
            self.assertEqual(engine.store.stats()['jobs'], {'done': 2})
            evidence = engine.store.evidence('target')[0]
            self.assertEqual(evidence['source_kind'], 'news')
            self.assertEqual(evidence['title'], 'Artículo editorial')
            self.assertEqual(evidence['published_at'][:10], '2026-10-01')
            self.assertEqual(evidence['text'], 'Arturo Pérez Montes inauguró una biblioteca.')

    def test_cached_discovery_article_date_and_scoring_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = Engine(temp, online=True)
            engine.store.import_entities([{'id': 'target', 'name': 'Arturo Pérez Montes'}])
            config = engine.config()
            config['sources'][0].update(enabled=True, terms_approved=True)
            config['sources'].append({'id': 'publisher', 'kind': 'news', 'allowed_hosts': ['example.org'],
                                      'enabled': True, 'terms_approved': True})
            engine.config_path.write_text(json.dumps(config), encoding='utf-8')
            class Fetcher:
                def __init__(self, *_args, **_kwargs):
                    pass
                def fetch(self, url, source):
                    if source['id'] == 'gdelt':
                        body = json.dumps({'articles': [{'url': 'https://example.org/article', 'title': 'Artículo'}]})
                        content_type = 'application/json'
                    else:
                        body = '<html><head><title>Fuente editorial</title><meta property="article:published_time" content="2026-10-01"></head><body><article>Arturo Pérez Montes fue condenado por corrupción.</article></body></html>'
                        content_type = 'text/html'
                    return {'url': url, 'status': 'not_modified', 'body': body, 'content_type': content_type,
                            'retrieved_at': now(), 'sha256': digest(body)}
            engine.queue_discovery('2026-10-15')
            with patch('peps.collector.SafeFetcher', Fetcher):
                result = engine.run(workers=1, seconds=2)
            self.assertEqual(2, result['processed'])
            self.assertEqual({'done': 2}, engine.store.stats()['jobs'])
            evidence = engine.store.evidence('target')[0]
            self.assertEqual('2026-10-01', evidence['published_at'][:10])
            # A name-only article stays a candidate until identity is checked.
            self.assertEqual(engine.store.list_entities()[0]['score']['score'], 0)
            self.assertEqual(evidence['identity_resolution']['status'], 'candidate')
            self.assertTrue(engine.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
