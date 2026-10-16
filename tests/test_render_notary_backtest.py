"""Offline report rendering, missingness and untrusted-label boundaries."""
import base64
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from scripts.render_notary_backtest import render_report, safe_link


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []
        self.images = []
        self.scripts = 0
        self.event_attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        values = dict(attrs)
        if tag == 'script':
            self.scripts += 1
        if tag == 'a':
            self.links.append(values.get('href'))
        if tag == 'img':
            self.images.append(values.get('src'))
        self.event_attributes.extend(key for key, value in attrs if key.lower().startswith('on'))


class NotaryBacktestRendererTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / 'informe.html'
        self.bundle = {
            'title': 'Backtesting documental de notarios',
            'scope': 'Muestra pública congelada; no representa a todos los notarios.',
            'cohort': {'entidades': 60, 'periodo': '2016–2026'},
            'summary_cards': [{'label': 'Textos pertinentes', 'value': None, 'note': 'Pendiente de revisión'}],
            'metric_rows': [{'arm': 'native', 'metric': 'Recall documental', 'value': None,
                             'numerator': None, 'denominator': None, 'note': 'Sin referencia completa'},
                            {'arm': 'direct_reference', 'metric': 'Precisión revisada', 'value': .5,
                             'numerator': 2, 'denominator': 4, 'interval': {'lower': .15, 'upper': .85},
                             'note': 'Descarga desde URL conocida'}],
            'confusion_matrices': [{'variant': 'nlp_direct_adverse', 'tp': 2, 'fp': 2, 'fn': 1, 'tn': 4, 'abstain': 3}],
            'volume_rows': [{'entity_id': 'synthetic-1', 'name': 'Ana Nopal Villaluz',
                             'arm': 'native', 'selection_group': 'Control público', 'gold_status': 'Sin señal en referencia',
                             'candidates': 9, 'texts': 0, 'relevant_texts': None, 'notes': 'Cuerpo no obtenido'},
                            {'entity_id': 'synthetic-1', 'name': 'Ana Nopal Villaluz',
                             'arm': 'direct_reference', 'selection_group': 'Control público', 'gold_status': 'Sin señal en referencia',
                             'candidates': 1, 'texts': 1, 'relevant_texts': 1, 'notes': 'URL previamente conocida'}],
            'source_rows': [{'source': 'Editor público', 'arm': 'native', 'queries': 60, 'http': 70,
                             'candidates': 9, 'texts': 0, 'status': 'Sin cuerpos'}],
            'findings': [{'severity': 'high', 'title': 'Cobertura insuficiente',
                          'evidence': 'Se encontraron candidatos sin cuerpos.',
                          'implication': 'No interpretar los ceros como ausencia de incidentes.'}],
            'limitations': ['El recall está acotado a referencias conocidas.'],
            'links': [{'label': 'Fuente pública', 'url': 'https://example.org/article'}],
        }

    def test_nulls_remain_unavailable_and_arms_are_not_aggregated(self):
        result = render_report(self.bundle, self.output, make_plots=False)
        document = self.output.read_text(encoding='utf-8')
        self.assertIn('N/D / N/D', document)
        self.assertIn('50,0 %', document)
        self.assertIn('Descubrimiento por nombre', document)
        self.assertIn('Descarga de referencias conocidas', document)
        self.assertIn('no se suman como descubrimiento', document)
        self.assertIn('value="1"', document)
        self.assertIn('value="2"', document)
        self.assertIn('data-arm="1"', document)
        self.assertIn('data-arm="2"', document)
        self.assertEqual(result['volume_rows'], 2)
        self.assertEqual(result['metric_rows'], 2)
        self.assertEqual(result['svg_artifacts'], [])
        self.assertLess(document.index('Cobertura insuficiente'), document.index('class="cards"'))

    def test_malicious_labels_are_text_and_never_become_html_or_scripts(self):
        attack = '</style><script>alert("x")</script><img src=x onerror="evil()">'
        self.bundle['title'] = attack
        self.bundle['scope'] = attack
        self.bundle['cohort']['malicious'] = attack
        for name in ('summary_cards', 'metric_rows', 'volume_rows', 'findings', 'source_rows', 'confusion_matrices'):
            self.bundle[name][0]['name'] = attack
            for key in ('label', 'metric', 'note', 'title', 'evidence', 'implication', 'variant', 'source', 'selection_group', 'gold_status'):
                self.bundle[name][0][key] = attack
        self.bundle['links'] = [{'label': attack, 'url': 'javascript:alert(1)'},
                                {'label': 'Data', 'url': 'data:text/html,<script>alert(1)</script>'},
                                {'label': 'Credential', 'url': 'https://user:secret@example.org/a'},
                                {'label': 'Network relative', 'url': '//example.org/a'}]
        render_report(self.bundle, self.output, make_plots=False)
        document = self.output.read_text(encoding='utf-8')
        parser = DocumentParser()
        parser.feed(document)
        self.assertEqual(parser.scripts, 1)  # the renderer's fixed filter script
        self.assertFalse(parser.event_attributes)
        self.assertFalse(parser.images)
        self.assertFalse(parser.links)
        self.assertIn('&lt;script&gt;', document)
        self.assertNotIn('href="javascript:', document)
        self.assertIn('textContent', document)
        self.assertNotIn('innerHTML', document)

    def test_local_links_are_existing_allowlisted_artifacts_and_escape_is_rejected(self):
        allowed = self.directory / 'datos.json'
        allowed.write_text('{}', encoding='utf-8')
        executable = self.directory / 'payload.js'
        executable.write_text('alert(1)', encoding='utf-8')
        self.assertEqual(safe_link('datos.json', self.directory), 'datos.json')
        self.assertIsNone(safe_link('payload.js', self.directory))
        self.assertIsNone(safe_link('../outside.json', self.directory))
        self.assertIsNone(safe_link('%2e%2e/outside.json', self.directory))
        self.assertIsNone(safe_link('file:///etc/passwd', self.directory))
        self.assertIsNone(safe_link(r'C:relative.json', self.directory))
        self.assertIsNone(safe_link(r'\\server\share\review.json', self.directory))
        self.assertIsNone(safe_link('%5C%5Cserver%5Cshare%5Creview.json', self.directory))
        self.assertIsNone(safe_link(str(allowed) + ':stream.json', self.directory))
        self.assertIsNone(safe_link('https://example.org/\nscript', self.directory))
        with tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / 'review.json'
            outside_file.write_text('{}', encoding='utf-8')
            self.assertIsNone(safe_link(str(outside_file), self.directory))
            self.assertIsNotNone(safe_link(str(outside_file), self.directory, [Path(outside)]))
            link = self.directory / 'escape.json'
            try:
                link.symlink_to(outside_file)
            except OSError:
                return
            self.assertIsNone(safe_link('escape.json', self.directory))

    def test_sixty_entities_can_be_filtered_without_untrusted_script_data(self):
        row = self.bundle['volume_rows'][0]
        self.bundle['volume_rows'] = [{**row, 'entity_id': f'entity-{index}',
                                       'name': f'Persona Sintética {index}'} for index in range(60)]
        result = render_report(self.bundle, self.output, make_plots=False)
        document = self.output.read_text(encoding='utf-8')
        self.assertEqual(result['volume_rows'], 60)
        self.assertEqual(document.count('<tr data-arm='), 60)
        self.assertIn('id="entity-search"', document)
        self.assertIn('row.textContent', document)
        self.assertIn('id="visible-rows"', document)
        self.assertNotIn('fetch(', document)
        self.assertIn("connect-src &#x27;none&#x27;".replace('&#x27;', "'"), document)

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'optional report plotting dependency not installed')
    def test_standard_svg_artifacts_exist_and_are_embedded_offline_by_arm(self):
        result = render_report(self.bundle, self.output)
        paths = [Path(path) for path in result['svg_artifacts']]
        self.assertEqual(len(paths), 3)  # separate native/reference volumes and quality
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertTrue(all('<svg' in path.read_text(encoding='utf-8') for path in paths))
        parser = DocumentParser()
        parser.feed(self.output.read_text(encoding='utf-8'))
        self.assertEqual(len(parser.images), 3)
        for src in parser.images:
            self.assertTrue(src.startswith('data:image/svg+xml;base64,'))
            raw = base64.b64decode(src.split(',', 1)[1])
            self.assertIn(b'<svg', raw)
        self.assertIn('informe-calidad.svg', parser.links)

    def test_no_fake_charts_are_produced_for_unknown_measurements(self):
        self.bundle['volume_rows'] = [{**self.bundle['volume_rows'][0], 'candidates': None,
                                       'texts': None, 'relevant_texts': None}]
        self.bundle['metric_rows'] = [self.bundle['metric_rows'][0]]
        result = render_report(self.bundle, self.output)
        self.assertEqual(result['svg_artifacts'], [])
        self.assertNotIn('data:image/svg+xml', self.output.read_text(encoding='utf-8'))

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'optional report plotting dependency not installed')
    def test_chart_labels_remain_plain_text_including_math_and_markup(self):
        attack = '$\\not_a_math_command{}$ </text><script>evil()</script>'
        self.bundle['volume_rows'][0]['name'] = attack
        self.bundle['metric_rows'][1]['metric'] = 'Precisión ' + attack
        result = render_report(self.bundle, self.output)
        for filename in result['svg_artifacts']:
            tree = ElementTree.parse(filename)
            self.assertNotIn('script', [node.tag.rsplit('}', 1)[-1] for node in tree.iter()])


if __name__ == '__main__':
    unittest.main()
