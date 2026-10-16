"""Offline publication-date regression proposal; no reference labels as inputs."""
import json
import unittest

from peps.sources import extract_text, parse_results

URL = 'https://publisher.invalid/noticias/documento/'
TITLE = 'Un documento público con título inequívoco'
CUTOFF = '2026-10-16T22:00:00+00:00'


def node(**changes):
    return dict({'@type': 'NewsArticle', 'mainEntityOfPage': {'@id': URL},
                 'headline': TITLE, 'datePublished': '2020-05-06T10:00:00-06:00'}, **changes)


def script(value):
    return '<script type="application/ld+json">' + json.dumps(value) + '</script>'


def parse(metadata, *, title=TITLE, retrieved_at=CUTOFF):
    body = '<html><head><title>' + title + ' | Periódico</title>' + metadata + '</head><body><article><p>Texto editorial.</p></article></body></html>'
    return parse_results(dict(status='ok', final_url=URL, body=body, retrieved_at=retrieved_at),
                         dict(id='fixture', kind='news', allowed_hosts=['publisher.invalid']))[0]


class PublicationMetadataTests(unittest.TestCase):
    def test_exact_article_date_is_normalized_without_using_modified_date(self):
        actual = parse(script(node(dateModified='2025-01-01T01:00:00Z')))
        self.assertEqual(actual['published_at'], '2020-05-06T16:00:00+00:00')
        self.assertNotIn('datePublished', actual['text'])

    def test_graph_and_type_list_bind_to_exact_url_and_title(self):
        self.assertEqual(parse(script({'@graph': [dict(node(), **{'@type': ['Article', 'Thing']})]}))['published_at'],
                         '2020-05-06T16:00:00+00:00')

    def test_article_fragment_identifier_and_single_trailing_slash_match_page(self):
        value = node(mainEntityOfPage=None, **{'@id': URL.rstrip('/') + '#article'})
        self.assertIsNotNone(parse(script(value))['published_at'])

    def test_global_webpage_and_site_dates_never_become_article_publication(self):
        for kind in ('WebPage', 'WebSite', 'Organization', 'SiteNavigationElement', 'Person'):
            with self.subTest(kind=kind):
                self.assertIsNone(parse(script(dict(node(), **{'@type': kind})))['published_at'])

    def test_modified_only_and_visible_or_url_dates_remain_unknown(self):
        value = node(dateModified='2020-05-06T10:00:00Z')
        del value['datePublished']
        self.assertIsNone(parse(script(value) + '<time>2020-05-06</time>')['published_at'])

    def test_other_articles_same_headline_different_host_path_query_are_rejected(self):
        for other in ('https://other.invalid/noticias/documento/',
                      'https://publisher.invalid/noticias/otro/',
                      URL + '?list=1', 'https://user:secret@publisher.invalid/noticias/documento/',
                      'http://publisher.invalid/noticias/documento/'):
            with self.subTest(other=other):
                self.assertIsNone(parse(script(node(mainEntityOfPage=other)))['published_at'])

    def test_exact_url_wrong_headline_and_short_ambiguous_headline_are_rejected(self):
        for headline in ('Un artículo diferente con otra noticia', 'Inicio'):
            with self.subTest(headline=headline):
                self.assertIsNone(parse(script(node(headline=headline)))['published_at'])

    def test_conflicting_article_publication_claims_are_unknown(self):
        value = [node(), node(datePublished='2020-05-07T10:00:00-06:00')]
        self.assertIsNone(parse(script(value))['published_at'])

    def test_same_instant_with_different_offsets_is_one_date(self):
        value = [node(), node(datePublished='2020-05-06T16:00:00Z')]
        self.assertEqual(parse(script(value))['published_at'], '2020-05-06T16:00:00+00:00')

    def test_nested_article_navigation_lists_are_rejected(self):
        for kind in ('ItemList', 'BreadcrumbList', 'SiteNavigationElement', 'WebSite', 'https://schema.org/ItemList'):
            with self.subTest(kind=kind):
                self.assertIsNone(parse(script({'@type': kind, 'itemListElement': [node()]}))['published_at'])

    def test_future_date_and_missing_observation_timestamp_remain_unknown(self):
        self.assertIsNone(parse(script(node(datePublished='2028-01-01T00:00:00Z')))['published_at'])
        self.assertIsNone(parse(script(node()), retrieved_at=None)['published_at'])

    def test_date_only_metadata_keeps_the_source_day(self):
        self.assertEqual(parse(script(node(datePublished='2020-05-06')))['published_at'], '2020-05-06T00:00:00+00:00')

    def test_conflicting_future_claim_does_not_select_only_the_convenient_past_claim(self):
        self.assertIsNone(parse(script([node(), node(datePublished='2028-01-01T00:00:00Z')]))['published_at'])

    def test_scoped_microdata_publication_requires_its_exact_page_binding(self):
        metadata = ('<article itemscope itemtype="https://schema.org/Article">'
                    '<meta itemprop="datePublished" content="2020-05-06T10:00:00Z">'
                    '<meta itemprop="dateModified" content="2025-01-01T00:00:00Z">'
                    '<meta itemprop="mainEntityOfPage" itemscope itemtype="https://schema.org/WebPage" itemid="' + URL + '">'
                    '</article>')
        self.assertEqual(parse(metadata)['published_at'], '2020-05-06T10:00:00+00:00')
        self.assertIsNone(parse(metadata.replace(URL, 'https://other.invalid/'))['published_at'])

    def test_unscoped_or_nested_person_datepublished_is_ignored(self):
        self.assertIsNone(parse('<meta itemprop="datePublished" content="2020-05-06">')['published_at'])
        metadata = ('<article itemscope itemtype="https://schema.org/Article" itemid="' + URL + '">'
                    '<span itemscope itemtype="https://schema.org/Person">'
                    '<meta itemprop="datePublished" content="2020-05-06"></span></article>')
        self.assertIsNone(parse(metadata)['published_at'])

    def test_microdata_must_be_closed_and_not_in_navigation(self):
        metadata = ('<article itemscope itemtype="https://schema.org/Article" itemid="' + URL + '">'
                    '<meta itemprop="datePublished" content="2020-05-06">')
        self.assertIsNone(parse(metadata)['published_at'])
        self.assertIsNone(parse('<nav>' + metadata + '</article></nav>')['published_at'])

    def test_malformed_duplicate_key_and_oversized_scripts_fail_without_exposing_scripts(self):
        for metadata in ('<script type="application/ld+json">{broken}</script>',
                         '<script type="application/ld+json">{"@type":"Article","datePublished":"2020-01-01","datePublished":"2020-02-01"}</script>',
                         '<script type="application/ld+json">' + ' ' * 131073 + '</script>'):
            with self.subTest(size=len(metadata)):
                self.assertIsNone(parse(metadata)['published_at'])
                self.assertNotIn('datePublished', parse(metadata)['text'])

    def test_deep_or_many_nodes_have_hard_bounds(self):
        value = node()
        for _ in range(18):
            value = {'@graph': [value]}
        self.assertIsNone(parse(script(value))['published_at'])
        self.assertIsNone(parse(script([{}] * 501 + [node()]))['published_at'])
        self.assertIsNone(parse(script({}) * 9 + script(node()))['published_at'])

    def test_existing_explicit_meta_date_stays_authoritative_for_compatibility(self):
        metadata = '<meta property="article:published_time" content="2019-01-01T00:00:00Z">' + script(node())
        self.assertEqual(parse(metadata)['published_at'], '2019-01-01T00:00:00+00:00')

    def test_general_extract_text_does_not_collect_structured_metadata(self):
        self.assertEqual(extract_text('<p>Visible</p>' + script(node())), 'Visible')


if __name__ == '__main__':
    unittest.main()
