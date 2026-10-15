import json
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.store import digest, now


class PipelineTests(unittest.TestCase):
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
            self.assertGreater(engine.store.list_entities()[0]['score']['score'], 0)
            self.assertTrue(engine.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
