"""Backtest boundaries use synthetic responses; these are not real-web results."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from peps.collector import SafeFetcher
from scripts.backtest_notary_news import (
    BudgetStop, RequestBudget, check_paths, effective_config, load_cohort, parser, run,
)


class NotaryBacktestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def arguments(self, people=8):
        names = ['Uno', 'Dos', 'Tres', 'Cuatro', 'Cinco', 'Seis', 'Siete', 'Ocho']
        cohort = {'entities': [{'id': f'person-{index}', 'name': f'Nombre Público Notarial {names[index]}',
            'group': 'known_incident' if index % 2 else 'no_known_incident',
            'context': {'source_url': 'https://provenance.invalid/gold-do-not-query'},
            'incident_label': 'yes', 'reference_articles': ['https://gold.invalid/not-a-query']}
            for index in range(people)]}
        defaults = json.loads((Path(__file__).resolve().parents[1] / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        source = deepcopy(next(item for item in defaults['sources'] if item['id'] == 'contralinea_index'))
        source.update(enabled=True, terms_approved=True, robots_required=True, download_articles=True,
                      article_terms_approved=True, body_terms_approved=True, min_interval_seconds=0,
                      search_max_records=50)
        config = {'sources': [source], 'search': {'strategy': 'targeted', 'preset': 'adverse',
                  'execution_mode': 'extended', 'session_seconds': 900, 'use_context': True,
                  'include_aliases': False, 'max_queries': people, 'start': '2010-01-01', 'end': '2026-01-01'}}
        (self.root / 'cohort.json').write_text(json.dumps(cohort))
        (self.root / 'sources.json').write_text(json.dumps(config))
        return parser().parse_args(['--online', '--cohort', str(self.root / 'cohort.json'),
            '--sources-config', str(self.root / 'sources.json'), '--data-dir', str(self.root / 'cold'),
            '--output', str(self.root / 'report.json'), '--seconds', '20', '--local-seconds', '10',
            '--max-http', '60', '--body-budget', '30', '--per-entity-body-limit', '5', '--workers', '4'])

    def test_atomic_budget_includes_robots_under_parallel_reservations(self):
        budget = RequestBudget(7, time.monotonic() + 10)
        def reserve(index):
            try:
                return budget.reserve('https://example.org/robots.txt' if index % 2 else 'https://example.org/a',
                                      'https://example.org/search', 'source')
            except BudgetStop:
                return None
        with ThreadPoolExecutor(max_workers=16) as pool:
            events = [event for event in pool.map(reserve, range(100)) if event]
        self.assertEqual(len(events), 7)
        self.assertEqual({event['request_id'] for event in events}, set(range(1, 8)))
        self.assertTrue(any(event['robots_request'] for event in events))
        self.assertTrue(budget.exhausted())
        self.assertEqual(budget.reason, 'http_budget')

    def test_expired_wall_deadline_reserves_no_transport(self):
        budget = RequestBudget(100, time.monotonic() - 1)
        with self.assertRaises(BudgetStop):
            budget.reserve('https://example.org/', 'https://example.org/', 'source')
        self.assertEqual(budget.events, [])
        self.assertEqual(budget.reason, 'wall_deadline')

    def test_gold_labels_are_not_imported_and_context_is_not_queried(self):
        args = self.arguments()
        people, cases = load_cohort(args.cohort)
        self.assertEqual(len(people), 8)
        self.assertEqual(set(people[0]), {'id', 'name', 'kind', 'context', 'aliases'})
        self.assertNotIn('incident_label', people[0])
        self.assertNotIn('reference_articles', people[0])
        self.assertEqual(cases[0]['group'], 'no_known_incident')
        config = effective_config(args)
        self.assertFalse(config['search']['use_context'])
        self.assertEqual(config['search']['preset'], 'identity')
        self.assertTrue(json.loads(args.sources_config.read_text(encoding='utf-8'))['search']['use_context'])

    def test_permissions_and_existing_directory_are_rejected_before_opening_bank(self):
        args = self.arguments()
        config = json.loads(args.sources_config.read_text(encoding='utf-8'))
        config['sources'][0]['robots_required'] = False
        args.sources_config.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'robots'):
            effective_config(args)
        args.data_dir.mkdir()
        with self.assertRaisesRegex(ValueError, 'rutas nuevas'):
            check_paths(args)
        self.assertEqual(list(args.data_dir.iterdir()), [])

    def test_production_pipeline_scales_past_six_names_and_one_body_and_reuses_offline(self):
        args = self.arguments()
        people, _ = load_cohort(args.cohort)
        name_to_index = {person['name']: index for index, person in enumerate(people)}
        calls = []

        def fixture_transport(fetcher, url, headers, timeout, max_bytes, addresses):
            calls.append(url)
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n'
            if parts.path == '/wp-json/wp/v2/posts':
                name = parse_qs(parts.query)['search'][0].strip('"')
                index = name_to_index[name]
                rows = [{'id': index * 2 + offset + 1, 'link': f'https://contralinea.com.mx/article-{index}-{offset}/',
                    'date_gmt': '2020-01-01T12:00:00', 'title': {'rendered': f'{name} comparece {offset}'},
                    'excerpt': {'rendered': f'<p>Información pública de {name}.</p>'}} for offset in range(2)]
                return 200, {'content-type': 'application/json'}, json.dumps(rows).encode()
            index, offset = [int(value) for value in parts.path.strip('/').split('-')[1:]]
            name = people[index]['name']
            text = f'{name} compareció en un acto público número {offset}. ' + ('Este texto sintético es sólo una respuesta de prueba. ' * 10)
            body = f'<html><head><title>{name}</title></head><body><article><h1>{name}</h1><p>{text}</p></article></body></html>'
            return 200, {'content-type': 'text/html'}, body.encode()

        validate_url = SafeFetcher._validate_url
        def no_dns(fetcher, url, source, *, resolve=True):
            return validate_url(fetcher, url, source, resolve=False)

        with patch.object(SafeFetcher, '_transport', fixture_transport), \
             patch.object(SafeFetcher, '_validate_url', no_dns), \
             patch('socket.getaddrinfo', side_effect=AssertionError('No network in this synthetic unit test')):
            report = run(args)
        self.assertTrue(report['operational_passed'], report['operational_checks'])
        self.assertEqual(report['counts']['entities'], 8)
        self.assertEqual(report['operational_summary']['unique_original_documents'], 16)
        self.assertEqual(report['operational_summary']['entity_text_pairs'], 16)
        self.assertEqual(len(calls), 25)
        self.assertTrue(all(person['queries_completed'] == 1 and person['original_bodies'] == 2
                            for person in report['person_results']))
        self.assertEqual(report['offline_validation']['http_transport_calls'], 0)
        self.assertFalse(report['operational_summary']['historical_coverage_complete'])
        self.assertTrue(all(row['relevance'] == row['incident'] == 'unknown' and not row['reviewed_independently']
                            for row in report['retrieved']))
        self.assertTrue(all((args.data_dir / row['text_path']).is_file() for row in report['retrieved']))
        self.assertTrue(all('gold' not in url for url in calls))
        self.assertTrue(args.output.exists())
        with self.assertRaisesRegex(ValueError, 'rutas nuevas'):
            run(args)


if __name__ == '__main__':
    unittest.main()
