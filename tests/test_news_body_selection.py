"""Body quotas spend requests on useful reading, never on an inferred verdict."""
import json
import tempfile
import unittest

from peps.engine import Engine
from peps.news import NewsAcquirer, REPLACED_BODY, _BodyBudget, _body_selection
from peps.relevance import RelevanceIndex


class ReadingPriorityTests(unittest.TestCase):
    ENTITY = {'id': 'person', 'name': 'Enrique Peña Nieto', 'kind': 'person',
              # This explicit review is a unit fixture, not a bank assertion.
              'aliases': [{'name': 'Peña Nieto', 'verified': True,
                           'source': {'review_id': 'unit-reading-priority'}}],
              'context': {'role': 'presidente'}}

    def priority(self, title, summary=''):
        candidate = RelevanceIndex([self.ENTITY]).rank(title, summary)
        self.assertTrue(candidate['relevant'])
        selection = _body_selection(title, summary, candidate)
        self.assertFalse(selection['identity_verified'])
        self.assertEqual(selection['score_effect'], 'none')
        return selection['priority']

    def test_explicit_report_with_partial_name_precedes_neutral_full_name(self):
        self.assertGreater(self.priority('Presidente Peña Nieto recibe una denuncia.', ''),
                           self.priority('Enrique Peña Nieto presentó su agenda.', ''))

    def test_unverified_surname_alias_is_not_a_reading_candidate(self):
        entity = dict(self.ENTITY, aliases=['Peña Nieto'])
        self.assertFalse(RelevanceIndex([entity]).rank(
            'Presidente Peña Nieto recibe una denuncia.', '')['relevant'])

    def test_denial_victim_and_acquittal_remain_high_priority_reading(self):
        neutral = self.priority('Enrique Peña Nieto presentó su agenda.')
        for title in ('Enrique Peña Nieto negó la acusación de fraude.',
                      'Enrique Peña Nieto fue víctima de fraude.',
                      'Enrique Peña Nieto fue absuelto.'):
            with self.subTest(title=title):
                self.assertGreater(self.priority(title), neutral)

    def test_roundups_and_videos_follow_a_written_report_with_the_same_signal(self):
        report = self.priority('Enrique Peña Nieto recibe una denuncia.')
        self.assertLess(self.priority('Enrique Peña Nieto recibe una denuncia (Video).'), report)
        self.assertLess(self.priority('Enrique Peña Nieto recibe una denuncia en primeras planas.'), report)


class StoredSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.store = self.engine.store
        self.entities = [{'id': 'pemex', 'name': 'Pemex', 'kind': 'company'},
                         {'id': 'odebrecht', 'name': 'Odebrecht', 'kind': 'company'}]
        self.store.import_entities(self.entities)
        self.index = RelevanceIndex(self.entities)
        self.source = {'id': 'fixture', 'kind': 'rss', 'allowed_hosts': ['example.org'],
                       'enabled': True, 'terms_approved': True, 'download_articles': True,
                       'article_terms_approved': True, 'url': 'https://example.org/feed'}
        self.save()

    def save(self):
        self.news.config_path.write_text(json.dumps({'sources': [self.source],
                                                    'search': {'strategy': 'targeted'}}))

    def observe(self, path, title, budget=None):
        return self.news._observe({'url': 'https://example.org/' + path, 'title': title, 'text': ''},
                                  self.source, self.source['url'], {}, None,
                                  candidate=self.index.rank(title, ''), body_budget=budget)

    def body_jobs(self):
        with self.store.connection() as con:
            return [dict(row, payload=json.loads(row['payload']))
                    for row in con.execute("SELECT * FROM jobs WHERE kind='news_article'")]

    def test_later_useful_candidate_replaces_only_an_unattempted_queued_body(self):
        budget = _BodyBudget(maximum=1, per_entity=1)
        first = self.observe('neutral', 'Pemex presenta su informe anual.', budget)
        second = self.observe('review', 'Pemex negó las acusaciones de fraude.', budget)
        self.assertEqual(first['status'], 'article_queued')
        self.assertEqual(second['status'], 'article_queued')
        by_url = {job['payload']['url']: job for job in self.body_jobs()}
        self.assertEqual(by_url['https://example.org/neutral']['status'], 'cancelled')
        self.assertEqual(by_url['https://example.org/neutral']['error'], REPLACED_BODY)
        self.assertEqual(by_url['https://example.org/review']['status'], 'queued')
        self.assertEqual(self.news.stats()['articles'], 2)
        self.assertEqual(self.news.stats()['observations'], 2)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_live_lease_and_server_backoff_are_never_replaced(self):
        for state, attempts in (('running', 1), ('deferred', 1), ('deferred', 0), ('queued', 1)):
            with self.subTest(state=state, attempts=attempts):
                budget = _BodyBudget(maximum=1, per_entity=1)
                suffix = state + str(attempts)
                first = self.observe('neutral-' + suffix, 'Pemex presenta su informe anual.', budget)
                with self.store.connection() as con:
                    con.execute("UPDATE jobs SET status=?,attempts=? WHERE kind='news_article' "
                                "AND json_extract(payload,'$.article_id')=?", (state, attempts, first['id']))
                better = self.observe('review-' + suffix, 'Pemex recibe una denuncia por fraude.', budget)
                self.assertEqual(better['status'], 'body_budget_limited')
                old = next(job for job in self.body_jobs() if job['payload']['article_id'] == first['id'])
                self.assertEqual((old['status'], old['attempts']), (state, attempts))

    def test_resume_selects_best_stored_metadata_for_each_entity_without_discovery(self):
        self.source['article_terms_approved'] = False
        self.save()
        for index in range(60):
            self.observe('neutral-' + str(index), 'Pemex presenta su informe anual.')
        wanted = self.observe('pemex-report', 'Pemex recibe una denuncia por fraude.')
        other = self.observe('odebrecht-report', 'Odebrecht fue absuelto de la acusación.')
        self.assertEqual(self.body_jobs(), [])
        self.source['article_terms_approved'] = True
        self.save()
        result = self.news._rescue_bodies(self.index, [self.source], _BodyBudget(maximum=2, per_entity=1))
        self.assertEqual(result['body_queued'], 2)
        self.assertEqual({job['payload']['article_id'] for job in self.body_jobs()}, {wanted['id'], other['id']})
        self.assertEqual(self.news.stats()['articles'], 62)
        self.assertEqual(self.news.stats()['observations'], 62)

    def test_an_existing_downloaded_reservation_cannot_reset_the_entity_quota(self):
        budget = _BodyBudget(maximum=1, per_entity=1)
        budget.reserve('already-read', ['pemex'], existing=True)
        result = self.observe('review', 'Pemex recibe una denuncia por fraude.', budget)
        self.assertEqual(result['status'], 'body_budget_limited')
        self.assertEqual(self.body_jobs(), [])

    def test_unreplaceable_victim_is_checked_only_once_per_session(self):
        budget = _BodyBudget(maximum=1, per_entity=1)
        budget.reserve('already-started', ['pemex'], priority=0)
        calls = []
        def replace(victim):
            calls.append(victim)
            return False
        for index in range(100):
            self.assertFalse(budget.reserve('new-' + str(index), ['pemex'],
                                            priority=1000 + index, replace=replace))
        self.assertEqual(calls, ['already-started'])


if __name__ == '__main__':
    unittest.main()
