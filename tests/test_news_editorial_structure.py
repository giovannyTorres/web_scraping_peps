"""Minimal neutral reproductions of editorial navigation, without publisher text."""
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.news import ARTICLE_EXTRACTION_VERSION, NewsAcquirer, article_text
from peps.store import digest, now


class EditorialStructureTests(unittest.TestCase):
    def test_milenio_recirculation_component_does_not_become_article_text(self):
        html = ('<article><p>La alcaldesa presentó el informe.</p>'
                '<div class="nd-text-highlights-detail-bold" data-mrf-recirculation="Nota Recomendada">'
                '<span>Te recomendamos:</span><figure><figcaption><a href="/otra">'
                'Titular ajeno ficticio.</a></figcaption></figure></div>'
                '<p>El informe sigue disponible.</p></article>')
        self.assertEqual(article_text(html), 'La alcaldesa presentó el informe.\n\nEl informe sigue disponible.')

    def test_milenio_class_without_recirculation_attribute_remains_editorial(self):
        html = '<article><div class="nd-text-highlights-detail-bold">Prosa editorial legítima.</div></article>'
        self.assertEqual(article_text(html), 'Prosa editorial legítima.')

    def test_recirculation_attribute_without_milenio_component_is_not_a_blanket_exclusion(self):
        html = '<article><p data-mrf-recirculation="control">Prosa editorial legítima.</p></article>'
        self.assertEqual(article_text(html), 'Prosa editorial legítima.')

    def test_heraldo_standalone_label_and_linked_paragraphs_are_navigation(self):
        html = ('<div class="entry-content"><p>La alcaldesa presentó el informe.</p>'
                '<p>Seguir leyendo:</p><p><a href="/uno">Titular ficticio uno.</a></p>'
                '<p><a href="/dos"><strong>Titular ficticio dos.</strong></a></p>'
                '<p>La alcaldesa negó las acusaciones.</p></div>')
        self.assertEqual(article_text(html), 'La alcaldesa presentó el informe.\n\nLa alcaldesa negó las acusaciones.')

    def test_ordinary_editorial_link_ends_the_navigation_block(self):
        html = ('<article><p>Seguir leyendo:</p><p><a href="/otra">Nota ajena.</a></p>'
                '<p>El <a href="/informe">informe original</a> está disponible.</p>'
                '<p><a href="/resolucion">La resolución absolvió a la empresa.</a></p></article>')
        self.assertEqual(article_text(html), 'El informe original está disponible.\n\nLa resolución absolvió a la empresa.')

    def test_phrase_in_genuine_prose_does_not_start_navigation(self):
        html = ('<article><p>Seguir leyendo: el ejercicio se diseñó para estudiantes.</p>'
                '<p><a href="/informe">Este párrafo completo enlaza el informe.</a></p>'
                '<p>La empresa fue víctima de fraude.</p></article>')
        self.assertEqual(article_text(html), 'Seguir leyendo: el ejercicio se diseñó para estudiantes.\n\n'
                         'Este párrafo completo enlaza el informe.\n\nLa empresa fue víctima de fraude.')

    def test_standalone_label_without_following_all_link_paragraph_is_preserved(self):
        html = ('<article><p>Seguir leyendo:</p><p>La frase figura en el manual.</p>'
                '<p><a href="/informe">Documento editorial.</a></p></article>')
        self.assertEqual(article_text(html), 'Seguir leyendo:\n\nLa frase figura en el manual.\n\nDocumento editorial.')

    def test_mixed_prose_after_label_is_not_removed(self):
        html = ('<article><p>Seguir leyendo:</p><p>Consulte <a href="/resolucion">la resolución</a> '
                'que absuelve a la empresa.</p></article>')
        self.assertEqual(article_text(html), 'Seguir leyendo:\n\nConsulte la resolución que absuelve a la empresa.')

    def test_text_after_label_in_the_same_paragraph_is_preserved(self):
        html = '<article><p>Seguir leyendo: <a href="/informe">es una opción del manual.</a></p></article>'
        self.assertEqual(article_text(html), 'Seguir leyendo: es una opción del manual.')

    def test_multiple_links_and_unicode_spacing_form_one_navigation_paragraph(self):
        html = ('<article><p><strong>SEGUIR LEYENDO&nbsp;:</strong></p>'
                '<p><a href="/uno">Uno.</a> <a href="/dos">Dos.</a></p>'
                '<p>La empresa negó los hechos.</p></article>')
        self.assertEqual(article_text(html), 'La empresa negó los hechos.')

    def test_named_anchor_without_href_is_not_navigation_evidence(self):
        html = '<article><p>Seguir leyendo:</p><p><a name="seccion">Texto del manual.</a></p></article>'
        self.assertEqual(article_text(html), 'Seguir leyendo:\n\nTexto del manual.')

    def test_link_markers_do_not_make_a_tiny_card_win_over_main_text(self):
        body = 'La alcaldesa presentó el informe completo y negó las acusaciones. ' * 4
        html = ('<main><p>' + body + '</p><article><a href="/uno">Uno</a>'
                '<a href="/dos">Dos</a><a href="/tres">Tres</a></article></main>')
        self.assertEqual(article_text(html), body.strip() + '\n\nUno Dos Tres')

    def test_plain_text_offsets_and_paragraphs_are_preserved(self):
        html = ('<article><p>Persona Pública Ejemplo presentó su informe.</p>'
                '<p>Seguir leyendo:</p><p><a href="/otra">Noticia ficticia ajena.</a></p>'
                '<p>Persona Pública Ejemplo no fue acusada de fraude.</p></article>')
        text = article_text(html)
        expected = ('Persona Pública Ejemplo presentó su informe.\n\n'
                    'Persona Pública Ejemplo no fue acusada de fraude.')
        self.assertEqual(text, expected)
        start = text.index('Persona Pública Ejemplo', 1)
        self.assertEqual(text[start:start+len('Persona Pública Ejemplo')], 'Persona Pública Ejemplo')
        self.assertEqual(start, expected.index('Persona Pública Ejemplo', 1))
        self.assertNotIn('\ufff0', text)

    def test_editorial_recommendation_phrase_without_component_is_not_removed(self):
        html = '<article><p>Te recomendamos: revisar el informe y escuchar a la persona afectada.</p></article>'
        self.assertEqual(article_text(html), 'Te recomendamos: revisar el informe y escuchar a la persona afectada.')


class EditorialVersionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.source = {'id': 'fixture', 'kind': 'rss', 'allowed_hosts': ['example.org'],
                       'enabled': True, 'terms_approved': True, 'download_articles': True,
                       'article_terms_approved': True, 'url': 'https://example.org/feed'}
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {'strategy': 'feeds'}}))
        self.neutral = 'Persona Pública Ejemplo presentó su informe anual. ' + 'Texto original ficticio de control. ' * 4
        self.raw = ('<article><p>' + self.neutral + '</p><p>Seguir leyendo:</p>'
                    '<p><a href="/otra">Otra persona fue detenida por fraude.</a></p></article>')

    def legacy(self, rendered):
        url='https://example.org/editorial'
        article_id=digest(url)
        self.news._observe({'url':url,'title':'Persona Pública Ejemplo','text':'Resumen ficticio'},
                           self.source,self.source['url'],{'retrieved_at':now()},None,False)
        self.engine.store.enqueue('news_article',{'source_id':'fixture','article_id':article_id,'url':url},key='editorial-fixture')
        class FixtureFetcher:
            def fetch(inner, requested, _source):
                return {'status':'ok','url':requested,'body':self.raw,'retrieved_at':now(),'sha256':digest(self.raw)}
        with patch('peps.news.ARTICLE_EXTRACTION_VERSION','article-main-4'), patch('peps.news.article_text',return_value=rendered):
            self.news.execute(self.engine.store.claim(kind_prefix='news_article'), FixtureFetcher())
        with self.engine.store.connection() as con:
            row=dict(con.execute('SELECT d.* FROM documents d JOIN news_articles n ON n.document_id=d.id').fetchone())
        row['metadata']=json.loads(row['metadata'])
        return row

    def test_version_four_is_reextracted_from_snapshot_without_http(self):
        old=self.legacy(self.neutral.strip()+'\n\nSeguir leyendo:\n\nOtra persona fue detenida por fraude.')
        snapshot=self.engine.data_dir/old['metadata']['raw_snapshot_path']
        raw_hash=hashlib.sha256(snapshot.read_bytes()).hexdigest()
        with patch('peps.collector.SafeFetcher',side_effect=AssertionError('No HTTP during local migration')):
            first=self.news.reextract_snapshots()
            second=self.news.reextract_snapshots()
        self.assertEqual(first,{'upgraded':1,'examined':1})
        self.assertEqual(second,{'examined':0})
        with self.engine.store.connection() as con:
            current=dict(con.execute('SELECT d.* FROM documents d JOIN news_articles n ON n.document_id=d.id').fetchone())
            preserved=dict(con.execute('SELECT * FROM documents WHERE id=?',(old['id'],)).fetchone())
            receipt=dict(con.execute('SELECT * FROM news_extraction_receipts').fetchone())
        self.assertEqual(current['text'],self.neutral.strip())
        self.assertNotEqual(current['id'],old['id'])
        self.assertEqual(preserved['text'],old['text'])
        self.assertEqual(json.loads(preserved['metadata']),old['metadata'])
        self.assertEqual(json.loads(current['metadata'])['raw_snapshot_sha256'],old['metadata']['raw_snapshot_sha256'])
        self.assertEqual(receipt['version'],ARTICLE_EXTRACTION_VERSION)
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(),raw_hash)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_unchanged_version_four_uses_idempotent_receipt(self):
        old=self.legacy(self.neutral.strip())
        self.assertEqual(self.news.reextract_snapshots(),{'unchanged':1,'examined':1})
        with self.engine.store.connection() as con:
            current=dict(con.execute('SELECT d.* FROM documents d JOIN news_articles n ON n.document_id=d.id').fetchone())
            receipt=dict(con.execute('SELECT * FROM news_extraction_receipts').fetchone())
        self.assertEqual(current['id'],old['id'])
        self.assertEqual(json.loads(current['metadata'])['extraction_version'],'article-main-4')
        self.assertEqual(receipt['version'],ARTICLE_EXTRACTION_VERSION)
        self.assertEqual(self.news.reextract_snapshots(),{'examined':0})
        self.assertTrue(self.engine.store.verify_audit()['ok'])


if __name__=='__main__':
    unittest.main()
