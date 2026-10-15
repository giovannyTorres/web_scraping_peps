"""Public, synthetic fixtures for guided local import (no personal datasets)."""
import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peps.importing import (ImportConflict, MAX_BYTES, MAX_COLUMNS, MAX_ROWS,
                            commit_import, preview_import)
from peps.store import Store
from peps.identity import IdentityResolver


class GuidedImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'bank.sqlite')

    def preview(self, **options):
        payload = {'text': 'nombre,tipo\nPersona de ejemplo,persona\n',
                   'mapping': ['name', 'kind'], **options}
        return payload, preview_import(self.store, payload)

    def commit(self, payload):
        preview = preview_import(self.store, payload)
        return commit_import(self.store, dict(payload, preview_hash=preview['preview_hash']))

    def test_preview_requires_explicit_mapping_and_does_not_write(self):
        payload, result = self.preview(mapping=None)
        self.assertEqual(['name', 'kind'], result['suggested_mapping'])
        self.assertFalse(result['ready'])
        self.assertEqual('unmapped', result['rows'][0]['status'])
        with self.assertRaises(ValueError):
            commit_import(self.store, dict(payload, preview_hash=result['preview_hash']))
        self.assertEqual(0, self.store.stats()['entities'])
        with self.store.connection() as con:
            self.assertEqual(0, con.execute('SELECT count(*) FROM audit').fetchone()[0])

    def test_single_name_needs_explicit_type_and_rejects_trust_injection(self):
        payload = {'mode': 'single', 'entity': {'name': 'Persona ejemplo'}}
        result = preview_import(self.store, payload)
        self.assertFalse(result['ready'])
        self.assertEqual(1, result['counts']['invalid'])
        payload['entity']['kind'] = 'person'
        self.assertEqual(1, self.commit(payload)['imported'])
        payload['entity']['context'] = {'verified_identity': True}
        with self.assertRaises(ValueError):
            preview_import(self.store, payload)
        payload['entity']['context'] = {'role': {'verified': True}}
        self.assertFalse(preview_import(self.store, payload)['ready'])

    def test_all_delimiters_and_bom_no_header(self):
        for delimiter in (',', ';', '\t', '|'):
            payload = {'text': '\ufeffPersona ejemplo' + delimiter + 'empresa\n',
                       'has_header': False, 'delimiter': delimiter, 'mapping': ['name', 'kind']}
            result = preview_import(self.store, payload)
            self.assertTrue(result['ready'])
            self.assertEqual('company', result['rows'][0]['entity']['kind'])
            self.assertEqual('Persona ejemplo', result['rows'][0]['values'][0])
            self.assertEqual(1, result['rows'][0]['line_start'])

    def test_duplicate_empty_headers_and_ragged_extra_cells_visible(self):
        payload = {'text': 'nombre;nombre;\nAna;Pepe\nSol;Luna;CDMX;extra\n',
                   'mapping': ['name', 'aliases', 'city', 'ignore'], 'default_kind': 'person'}
        result = preview_import(self.store, payload)
        self.assertTrue(result['ready'])
        self.assertEqual(';', result['delimiter'])
        self.assertEqual(4, len(result['columns']))
        self.assertEqual('nombre', result['columns'][1]['header'])
        self.assertEqual('', result['columns'][2]['header'])
        self.assertEqual('extra', result['rows'][1]['values'][3])
        self.assertTrue(result['rows'][0]['warnings'])
        self.assertTrue(result['rows'][1]['warnings'])
        self.assertEqual(['Pepe'], result['rows'][0]['entity']['aliases'])
        with self.assertRaises(ValueError):
            preview_import(self.store, dict(payload, mapping=['name', 'aliases', 'city']))

    def test_preamble_and_multiline_csv_keep_physical_lines(self):
        payload = {'text': 'Informe de pruebas\nDatos de ejemplo\nNombre,Nota,Tipo\n"Ana\nPrueba","Primera línea\nSegunda línea",persona\n',
                   'skip_rows': 2, 'mapping': ['name', 'context_note', 'kind']}
        row = preview_import(self.store, payload)['rows'][0]
        self.assertEqual(4, row['line_start'])
        self.assertEqual(6, row['line_end'])
        self.assertEqual('Ana\nPrueba', row['entity']['name'])
        self.assertEqual('Primera línea\nSegunda línea', row['entity']['context']['context_note'])

    def test_component_names_alias_accumulation_and_spanish_kinds(self):
        payload = {'text': 'n,p,m,t,a,b\nAna,De Ejemplo,Prueba,Persona física,Anita|A,Anita|AP\n',
                   'mapping': ['given_name', 'first_surname', 'second_surname', 'kind', 'aliases', 'aliases']}
        entity = preview_import(self.store, payload)['rows'][0]['entity']
        self.assertEqual('Ana De Ejemplo Prueba', entity['name'])
        self.assertEqual(['Anita', 'A', 'AP'], entity['aliases'])
        self.assertEqual('person', entity['kind'])
        with self.assertRaises(ValueError):
            preview_import(self.store, dict(payload, mapping=['name', 'first_surname', 'ignore', 'kind', 'aliases', 'aliases']))
        with self.assertRaises(ValueError):
            preview_import(self.store, dict(payload, mapping=['name', 'ignore', 'ignore', 'kind', 'city', 'city']))

    def test_custom_kinds_require_user_equivalence_never_guess(self):
        payload, result = self.preview(text='n,t\nAna,P.F.\nSol,Institución\n')
        self.assertFalse(result['ready'])
        self.assertEqual({'P.F.': None, 'Institución': 'organization'}, result['kind_suggestions'])
        payload['kind_values'] = {'P.F.': 'person'}
        result = preview_import(self.store, payload)
        self.assertTrue(result['ready'])
        self.assertEqual('person', result['rows'][0]['entity']['kind'])
        payload['kind_values'] = {}
        payload['default_kind'] = 'person'
        self.assertFalse(preview_import(self.store, payload)['ready'])

    def test_rfc_date_age_normalization_and_invalid_dates(self):
        payload = {'text': 'n,t,r,f,e\nAna,persona,aaaa010101aaa,31/12/2000,25\n',
                   'mapping': ['name', 'kind', 'rfc', 'birth_date', 'age']}
        context = preview_import(self.store, payload)['rows'][0]['entity']['context']
        self.assertEqual({'rfc': 'AAAA010101AAA', 'birth_date': '2000-12-31', 'age': 25}, context)
        for text in ('Ana,persona,INVALIDO,31/12/2000,25',
                     'Ana,persona,AAAA010101AAA,31/02/2000,25',
                     'Ana,persona,AAAA010101AAA,2000-12-31,131'):
            self.assertFalse(preview_import(self.store, dict(payload, text='n,t,r,f,e\n' + text))['ready'])
        self.assertFalse(preview_import(self.store, dict(payload, date_format='iso'))['ready'])

    def test_all_or_nothing_and_explicit_skip_invalid(self):
        payload, result = self.preview(text='n,t\nAna,persona\n,empresa\nSol,empresa\n')
        self.assertEqual(2, result['counts']['new'])
        self.assertFalse(result['ready'])
        with self.assertRaises(ValueError):
            commit_import(self.store, dict(payload, preview_hash=result['preview_hash']))
        self.assertEqual(0, self.store.stats()['entities'])
        payload['skip_invalid'] = True
        receipt = self.commit(payload)
        self.assertEqual(2, receipt['imported'])
        self.assertEqual(1, receipt['counts']['invalid'])
        self.assertEqual(2, self.store.stats()['entities'])

    def test_commit_binds_source_mapping_policy_and_bank(self):
        payload, preview = self.preview()
        token = preview['preview_hash']
        for changed in (dict(payload, text=payload['text'].replace('ejemplo', 'distinta')),
                        dict(payload, existing_policy='update'),
                        dict(payload, skip_invalid=True),
                        dict(payload, mapping=['name', 'ignore'], default_kind='person')):
            with self.assertRaises(ImportConflict):
                commit_import(self.store, dict(changed, preview_hash=token))
        self.store.import_entities([{'name': 'Otra persona', 'kind': 'person'}])
        with self.assertRaises(ImportConflict):
            commit_import(self.store, dict(payload, preview_hash=token))
        with self.assertRaises(ImportConflict):
            commit_import(self.store, payload)

    def test_double_commit_does_not_write_twice(self):
        payload, preview = self.preview()
        payload['preview_hash'] = preview['preview_hash']
        self.assertEqual(1, commit_import(self.store, payload)['imported'])
        with self.assertRaises(ImportConflict):
            commit_import(self.store, payload)
        self.assertEqual(1, self.store.stats()['entities'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_concurrent_commit_only_one_accepts(self):
        payload, preview = self.preview()
        payload['preview_hash'] = preview['preview_hash']
        def work(_):
            try:
                return commit_import(self.store, payload)['imported']
            except ImportConflict:
                return 'stale'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual([1, 'stale'], list(pool.map(work, range(2))))

    def test_default_existing_policy_skips_and_update_preserves_hints_aliases(self):
        self.store.import_entities([{'id': 'one', 'name': 'Nombre anterior', 'kind': 'person',
                                    'aliases': ['Alias histórico'],
                                    'context': {'state': 'Sonora', 'role': 'Director', 'verified': {'source': 'internal-fixture'}}}])
        payload = {'text': 'id,nombre,rol,ciudad,alias\none,Nombre revisado,,Hermosillo,Nuevo alias\n',
                   'mapping': ['id', 'name', 'role', 'city', 'aliases'], 'default_kind': 'person'}
        result = preview_import(self.store, payload)
        self.assertEqual(1, result['counts']['skipped'])
        self.assertEqual(0, self.commit(payload)['imported'])
        self.assertEqual('Nombre anterior', self.store.get_entity('one')['name'])
        payload['existing_policy'] = 'update'
        result = preview_import(self.store, payload)
        expected = result['rows'][0]['entity']
        self.assertEqual(['Alias histórico', 'Nuevo alias'], expected['aliases'])
        self.assertEqual('Director', expected['context']['role'])
        self.assertEqual('Sonora', expected['context']['state'])
        self.assertTrue(any('nombre o tipo' in item for item in result['rows'][0]['warnings']))
        self.commit(payload)
        actual = self.store.get_entity('one')
        self.assertEqual(expected, {key: actual[key] for key in expected})
        self.assertIs(False, actual['context']['verified'])

    def test_changed_verified_fields_do_not_inherit_reviewed_identity_anchors(self):
        for shape in ('dict', 'list_dict', 'list_names'):
            with self.subTest(shape=shape):
                entity_id = 'verified-' + shape
                source = {'verified': True, 'source': 'review-fixture-record'}
                context = {'rfc': 'AAAA010101AAA', 'city': 'León'}
                if shape == 'dict':
                    context['verified_fields'] = {'rfc': source, 'city': source}
                elif shape == 'list_dict':
                    context['verified_fields'] = [dict(source, field='rfc'), dict(source, field='city')]
                else:
                    context['verified_fields'] = ['rfc', 'city']
                    context['provenance'] = {'rfc': 'review-fixture-record', 'city': 'review-fixture-record'}
                self.store.import_entities([{'id': entity_id, 'name': 'Ana Prueba', 'kind': 'person', 'context': context}])
                before = self.store.get_entity(entity_id)
                self.assertTrue(IdentityResolver([before])._rfc_verifications[entity_id]['AAAA010101AAA']['verified'])
                payload = {'mode': 'single', 'entity': {'id': entity_id, 'name': 'Ana Prueba',
                           'kind': 'person', 'context': {'rfc': 'BBBB020202BBB'}}, 'existing_policy': 'update'}
                preview = preview_import(self.store, payload)
                candidate = preview['rows'][0]['entity']
                self.assertFalse(IdentityResolver([candidate])._rfc_verifications[entity_id]['BBBB020202BBB']['verified'])
                self.assertTrue(IdentityResolver([candidate])._profiles[entity_id][0]['verification']['verified'])
                self.commit(payload)
                persisted = self.store.get_entity(entity_id)
                self.assertFalse(IdentityResolver([persisted])._rfc_verifications[entity_id]['BBBB020202BBB']['verified'])
                # Re-importing exactly the original reviewed values retains trust.
                unchanged_id = entity_id + '-unchanged'
                self.store.import_entities([dict(before, id=unchanged_id)])
                unchanged = {'mode': 'single', 'entity': {'id': unchanged_id, 'name': 'Ana Prueba',
                             'kind': 'person', 'context': {'rfc': 'AAAA010101AAA'}}, 'existing_policy': 'update'}
                self.assertEqual(0, self.commit(unchanged)['imported'])
                self.assertTrue(IdentityResolver([self.store.get_entity(unchanged_id)])._rfc_verifications[unchanged_id]['AAAA010101AAA']['verified'])

    def test_renaming_existing_id_revokes_structured_and_alias_verification(self):
        context = {'rfc': 'AAAA010101AAA',
                   'verified_fields': {'rfc': {'verified': True, 'source': 'review-record'},
                                       'canonical_name': {'verified': True, 'source': 'review-record'}},
                   'verified_aliases': {'Antiguo': 'review-record'},
                   'identifiers': [{'type': 'rfc', 'value': 'CCCC030303CCC', 'verified': True, 'source': 'review-record'}],
                   'role': {'value': 'Directora', 'verified': True, 'source': 'review-record'}}
        self.store.import_entities([{'id': 'one', 'name': 'Ana Prueba', 'kind': 'person', 'context': context}])
        payload = {'mode': 'single', 'entity': {'id': 'one', 'name': 'Nombre Cambiado', 'kind': 'person'},
                   'existing_policy': 'update'}
        candidate = preview_import(self.store, payload)['rows'][0]['entity']
        resolver = IdentityResolver([candidate])
        self.assertFalse(any(v['verified'] for v in resolver._rfc_verifications['one'].values()))
        self.assertFalse(any(v.verification['verified'] for v in resolver._variants))
        self.assertFalse(any(v['verification']['verified'] for v in resolver._profiles['one']))
        self.assertNotIn('verified_aliases', candidate['context'])

    def test_appended_alias_does_not_inherit_field_verification(self):
        self.store.import_entities([{'id': 'one', 'name': 'Ana Prueba', 'kind': 'person', 'aliases': ['Anterior'],
                                    'context': {'verified_fields': {'alias': {'verified': True, 'source': 'review-record'}}}}])
        payload = {'mode': 'single', 'entity': {'id': 'one', 'name': 'Ana Prueba', 'kind': 'person', 'aliases': ['Nuevo']},
                   'existing_policy': 'update'}
        candidate = preview_import(self.store, payload)['rows'][0]['entity']
        new = next(v for v in IdentityResolver([candidate])._variants if v.name == 'Nuevo')
        self.assertFalse(new.verification['verified'])
        self.assertEqual(['Anterior', 'Nuevo'], candidate['aliases'])

    def test_ambiguous_dates_require_explicit_format_and_samples_are_bounded(self):
        payload = {'mode': 'single', 'entity': {'name': 'Ana', 'kind': 'person', 'birth_date': '01/02/2000'}}
        self.assertFalse(preview_import(self.store, payload)['ready'])
        result = preview_import(self.store, dict(payload, date_format='dmy'))
        self.assertEqual('2000-02-01', result['rows'][0]['entity']['context']['birth_date'])
        csv = {'text': 'nombre,tipo,ignorada\nAna,persona,' + 'x' * 10_000,
               'mapping': ['name', 'kind', 'ignore']}
        result = preview_import(self.store, csv)
        self.assertEqual(500, len(result['rows'][0]['values'][2]))
        self.assertEqual([2], result['rows'][0]['values_truncated'])
        self.assertTrue(result['ready'])

    def test_blank_optional_update_is_unchanged_not_destructive(self):
        self.store.import_entities([{'id': 'one', 'name': 'Ana', 'kind': 'person', 'aliases': ['A'], 'context': {'city': 'León'}}])
        payload = {'text': 'id,n,t,c,a\none,Ana,persona,,\n',
                   'mapping': ['id', 'name', 'kind', 'city', 'aliases'], 'existing_policy': 'update'}
        result = preview_import(self.store, payload)
        self.assertEqual(1, result['counts']['unchanged'])
        self.assertEqual(0, self.commit(payload)['imported'])
        self.assertEqual('León', self.store.get_entity('one')['context']['city'])

    def test_duplicates_conflicting_ids_and_homonyms(self):
        payload = {'text': 'id,n,t,c\na,Ana,persona,León\na,Ana,persona,León\nb,Ana,persona,CDMX\n',
                   'mapping': ['id', 'name', 'kind', 'city']}
        result = preview_import(self.store, payload)
        self.assertEqual(1, result['counts']['duplicates'])
        self.assertEqual(2, result['counts']['new'])
        self.assertTrue(any('Nombre compartido' in item for item in result['rows'][2]['warnings']))
        self.assertEqual(2, self.commit(payload)['imported'])
        payload['text'] = 'id,n,t,c\nc,Ana,persona,León\nc,Ana,persona,CDMX\n'
        payload['skip_invalid'] = True
        result = preview_import(self.store, payload)
        self.assertFalse(result['ready'])
        self.assertEqual(1, result['counts']['conflicting_ids'])
        with self.assertRaises(ValueError):
            commit_import(self.store, dict(payload, preview_hash=result['preview_hash']))
        self.assertEqual(2, self.store.stats()['entities'])

    def test_rollback_includes_entities_revision_and_audit(self):
        payload, preview = self.preview()
        with patch.object(self.store, '_audit', side_effect=RuntimeError('simulated disk failure')):
            with self.assertRaises(RuntimeError):
                commit_import(self.store, dict(payload, preview_hash=preview['preview_hash']))
        self.assertEqual(0, self.store.stats()['entities'])
        self.assertEqual(preview['preview_hash'], preview_import(self.store, payload)['preview_hash'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_store_external_transaction_does_not_commit_early(self):
        with self.assertRaises(RuntimeError):
            with self.store.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                self.store.import_entities([{'name': 'Ana', 'kind': 'person'}], connection=con)
                raise RuntimeError('rollback caller')
        self.assertEqual(0, self.store.stats()['entities'])

    def test_audit_excludes_source_text_headers_kind_values_and_ignored_secrets(self):
        payload = {'text': 'CABECERA_SECRETA,n,t,x\nignore,Nombre confidencial,CLAVE_SECRETA,NO_GUARDAR\n',
                   'mapping': ['ignore', 'name', 'kind', 'ignore'], 'kind_values': {'CLAVE_SECRETA': 'person'}}
        self.commit(payload)
        with self.store.connection() as con:
            events = [dict(row) for row in con.execute('SELECT * FROM audit')]
        encoded = json.dumps(events)
        for secret in ('CABECERA_SECRETA', 'Nombre confidencial', 'CLAVE_SECRETA', 'NO_GUARDAR'):
            self.assertNotIn(secret, encoded)
        event = json.loads(events[-1]['payload'])
        self.assertEqual('guided_entities_imported', events[-1]['action'])
        self.assertEqual(64, len(event['source_hash']))
        self.assertEqual({'index': 1, 'field': 'name'}, event['mapping'][1])

    def test_invalid_quotes_limits_and_unsafe_types(self):
        payload, _ = self.preview()
        for changed in (dict(payload, text='n,t\n"unterminated,persona'),
                        dict(payload, text='n,t\nAna\x00,persona'),
                        dict(payload, skip_rows=101), dict(payload, has_header='yes'),
                        dict(payload, skip_invalid='true'), dict(payload, delimiter=':'),
                        dict(payload, text='x' * (MAX_BYTES + 1)),
                        dict(payload, text=','.join('x' for _ in range(MAX_COLUMNS + 1)) + '\nAna,persona'),
                        dict(payload, text='n,t\n' + 'Ana,persona\n' * (MAX_ROWS + 1)),
                        dict(payload, text='n,t\n' + 'A' * 64_001 + ',persona')):
            with self.subTest(keys=list(changed)):
                with self.assertRaises(ValueError):
                    preview_import(self.store, changed)

    def test_large_preview_validates_every_row_with_bounded_samples(self):
        payload = {'text': 'nombre,tipo\n' + ''.join('Prueba %s,persona\n' % index for index in range(1000)),
                   'mapping': ['name', 'kind']}
        started = time.perf_counter()
        result = preview_import(self.store, payload)
        elapsed = time.perf_counter() - started
        self.assertTrue(result['ready'])
        self.assertEqual(1000, result['counts']['new'])
        self.assertEqual(20, len(result['rows']))
        self.assertEqual(980, result['rows_truncated'])
        self.assertLess(elapsed, 5.0)
        self.assertEqual(1000, len(self.commit(payload)['accepted_ids']))

    def test_errors_bounded_and_remaining_invalid_rows_not_silently_accepted(self):
        payload = {'text': 'nombre,tipo\n' + ',persona\n' * 150, 'mapping': ['name', 'kind']}
        result = preview_import(self.store, payload)
        self.assertEqual(150, result['counts']['invalid'])
        self.assertEqual(100, len(result['errors']))
        self.assertEqual(50, result['errors_truncated'])
        self.assertFalse(result['ready'])


if __name__ == '__main__':
    unittest.main()
