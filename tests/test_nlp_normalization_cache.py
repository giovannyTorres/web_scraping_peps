"""Bounded Unicode preparation must preserve literal source attribution."""
from concurrent.futures import ThreadPoolExecutor
import unittest

from peps import nlp


class NormalizationCacheTests(unittest.TestCase):
    def setUp(self):
        nlp._fold_short_cached.cache_clear()

    def tearDown(self):
        nlp._fold_short_cached.cache_clear()

    def test_unicode_folding_preserves_enye_and_existing_decomposition(self):
        cases = {
            'AN\u0303O': 'año', 'AÑO': 'año', 'ano': 'ano',
            'JOSE\u0301': 'jose', 'İ': 'i', 'Straße': 'straße',
            '😀\u00a0PEÑA': '😀\u00a0peña', '각': '\u1100\u1161\u11a8',
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(nlp._fold(original), expected)
                self.assertEqual(nlp._fold(original), expected)
        self.assertGreater(nlp._fold_short_cached.cache_info().hits, 0)

    def test_long_inputs_and_combining_clusters_are_never_retained(self):
        for original in ('x' * 33, 'A' + '\u0301' * 40, '😀' * 10000):
            self.assertEqual(nlp._fold(original), nlp._fold_uncached(original))
            self.assertEqual(nlp._fold(original), nlp._fold_uncached(original))
        self.assertEqual(nlp._fold_short_cached.cache_info().currsize, 0)
        self.assertEqual(nlp._fold_short_cached.cache_info().misses, 0)
        self.assertEqual(nlp._fold('A' * 32), 'a' * 32)
        self.assertEqual(nlp._fold_short_cached.cache_info().currsize, 1)

    def test_distinct_short_inputs_cannot_grow_cache_beyond_4096_entries(self):
        for index in range(5000):
            value = str(index) + '\u0301'
            self.assertEqual(nlp._fold(value), str(index))
        info = nlp._fold_short_cached.cache_info()
        self.assertEqual(info.maxsize, 4096)
        self.assertEqual(info.currsize, 4096)

    def test_decomposed_mentions_keep_exact_offsets_and_enye_identity_boundary(self):
        name = 'José Peña Villaluz'
        text = '😀 José Pena Villaluz.\n📰 JOSE\u0301 PEN\u0303A VILLALUZ.'
        entity = {'id': 'fixture-cache', 'name': name, 'kind': 'person'}
        mentions = nlp._mentions(text, entity)
        self.assertEqual(len(mentions), 1)
        mention = mentions[0]
        expected = 'JOSE\u0301 PEN\u0303A VILLALUZ'
        self.assertEqual(mention['start'], text.index('JOSE'))
        self.assertEqual(text[mention['start']:mention['end']], expected)
        self.assertEqual(nlp._mentions(text, entity), mentions)

    def test_warm_cache_does_not_transfer_findings_between_entities(self):
        text = ('📰 JOSE\u0301 PEN\u0303A VILLALUZ fue acusado de fraude.\n'
                'María Nube Albor presentó su informe municipal.')
        accused = {'id': 'fixture-accused', 'name': 'José Peña Villaluz', 'kind': 'person'}
        neutral = {'id': 'fixture-neutral', 'name': 'María Nube Albor', 'kind': 'person'}
        cold = nlp.analyze(text, accused)
        self.assertTrue(cold['findings'])
        self.assertEqual(nlp.analyze(text, neutral)['findings'], [])
        self.assertEqual(nlp.analyze(text, accused), cold)
        for mention in cold['mentions']:
            self.assertEqual(text[mention['start']:mention['end']], mention['text'])
        for finding in cold['findings']:
            self.assertEqual(text[finding['start']:finding['end']], finding['quote'])

    def test_concurrent_normalization_keeps_unicode_text_and_offsets_deterministic(self):
        samples = ['Peña', 'PEN\u0303A', 'JOSE\u0301\u00a0NÚÑEZ', '😀 año ano',
                   'A' + '\u0301' * 40, '각 İ ß']
        expected = {text: nlp._normalized(text) for text in samples}
        nlp._fold_short_cached.cache_clear()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(nlp._normalized, samples * 12))
        for text, result in zip(samples * 12, results):
            self.assertEqual(result, expected[text])
        self.assertLessEqual(nlp._fold_short_cached.cache_info().currsize, 4096)


if __name__ == '__main__':
    unittest.main()
