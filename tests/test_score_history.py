"""Adversarial offline fixtures for immutable evaluation snapshots."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peps.nlp import analyze
from peps.scoring import score_entity
from peps.store import Store, canonical, digest


AS_OF = '2026-10-15'


class ScoreHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'fixture.sqlite3')
        self.store.import_entities([dict(id='fixture', name='José Peña Villaluz',
            kind='person', aliases=[], context={'state': 'Oaxaca'})])
        self.entity = self.store.get_entity('fixture')
        self.document_id = self.store.add_document(dict(url='https://fixture.invalid/history',
            text='📰 Jose\u0301 Peña Villaluz fue detenido por fraude.',
            title='Nota sintética', source_kind='official', source_id='synthetic',
            published_at='2026-10-10', retrieved_at='2026-10-15T00:00:00+00:00',
            metadata={'publisher_id': 'fixture.invalid', 'synthetic': True}), ['fixture'], analyze)

    def save(self, as_of=AS_OF, **changes):
        entity = self.store.get_entity('fixture')
        evidence = self.store.evidence('fixture')
        result = score_entity(entity, evidence, as_of)
        result.update(changes)
        run_id = self.store.save_score('fixture', result,
            entity_snapshot=entity, evidence_snapshot=evidence)
        return run_id, result

    def count(self, table):
        with self.store.connection() as con:
            return con.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def test_content_addressed_dedup_excludes_processing_time(self):
        first_id, first = self.save()
        with patch('peps.store.now', return_value='2099-01-01T00:00:00+00:00'):
            second_id, second = self.save()
        self.assertEqual(first, second)
        self.assertEqual(first_id, second_id)
        self.assertEqual(self.count('score_runs'), 1)
        self.assertEqual(self.store.get_score('fixture'), first)
        self.assertEqual(self.store.latest_score_run('fixture')['id'], first_id)
        with self.store.connection() as con:
            announcements = con.execute("SELECT count(*) FROM audit WHERE action='score_run_saved'").fetchone()[0]
            indexed = (con.execute("EXPLAIN QUERY PLAN SELECT * FROM audit WHERE action='score_run_saved' "
                                   "AND json_extract(payload,'$.run_hash')=?", (first_id,)).fetchall()
                       if self.store._audit_json else [])
        self.assertEqual(announcements, 1)
        if self.store._audit_json:
            self.assertTrue(any('audit_score_run_hash' in row['detail'] for row in indexed))
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_lookup_without_sqlite_json_uses_canonical_sql_filter(self):
        run_id, result = self.save()
        # Exercise the older-SQLite path without changing installed SQLite.
        self.store._audit_json = False
        self.assertEqual(self.store.get_score_run(run_id)['result'], result)
        self.assertEqual(self.store.score_history('fixture')[0]['id'], run_id)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_snapshot_keeps_source_references_and_full_result_without_body_field(self):
        run_id, result = self.save(provenance={'score_authority': {'version': 'fixture'},
                                             'context_method': {'model_hash': 'synthetic-only'}})
        run = self.store.get_score_run(run_id, 'fixture')
        self.assertEqual(run['entity_snapshot'], self.entity)
        self.assertEqual(run['result'], result)
        self.assertEqual(run['result']['provenance']['context_method']['model_hash'], 'synthetic-only')
        self.assertEqual(run['evidence_manifest'][0]['document_id'], self.document_id)
        self.assertNotIn('text', run['evidence_manifest'][0])
        self.assertEqual(self.count('documents'), 1)
        with self.store.connection() as con:
            row = con.execute('SELECT payload FROM score_runs WHERE id=?', (run_id,)).fetchone()
        self.assertEqual(digest(row['payload']), run_id)
        self.assertEqual(set(json.loads(row['payload'])),
                         {'schema_version', 'entity_snapshot', 'evidence_manifest', 'result'})

    def test_history_survives_bank_changes_review_changes_and_retired_links(self):
        run_id, original = self.save()
        evidence_id = self.store.evidence('fixture')[0]['id']
        self.store.review(evidence_id, 'rejected', 'Synthetic auditor', 'Synthetic homonym rejection')
        self.store.import_entities([dict(self.entity, name='Otra Persona Inventada')])
        with self.store.connection() as con:
            con.execute('DELETE FROM evidence WHERE id=?', (evidence_id,))
        self.assertIsNone(self.store.get_score('fixture'))
        self.assertIsNone(self.store.latest_score_run('fixture'))
        run = self.store.get_score_run(run_id)
        archived = self.store.materialize_run_evidence(run)
        self.assertEqual(archived[0]['review'], {})
        self.assertEqual(archived[0]['identity_bank_revision'], 1)
        self.assertEqual(score_entity(run['entity_snapshot'], archived, original['as_of']), original)
        self.assertEqual(self.store.run_evidence(run_id, evidence_id)['document_id'], self.document_id)
        self.assertFalse(self.store.score_history('fixture')[0]['current'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_changed_review_or_evaluation_date_creates_distinct_run(self):
        first, _ = self.save()
        next_day, _ = self.save('2026-10-16')
        evidence_id = self.store.evidence('fixture')[0]['id']
        self.store.review(evidence_id, 'confirmed', 'Synthetic auditor', 'Synthetic source/identity checked')
        reviewed, _ = self.save()
        self.assertEqual(len({first, next_day, reviewed}), 3)
        history = self.store.score_history('fixture')
        self.assertEqual(len(history), 3)
        self.assertEqual(sum(entry['current'] for entry in history), 1)
        self.assertEqual(next(entry['id'] for entry in history if entry['current']), reviewed)
        self.assertEqual(len(self.store.score_history('fixture', limit=1)), 1)

    def test_two_readers_cannot_submit_second_review_from_stale_run(self):
        original_id, _ = self.save()
        evidence_id = self.store.evidence('fixture')[0]['id']
        self.store.review(evidence_id, 'confirmed', 'Alice synthetic', 'Identity checked by first reader',
                          expected_run_id=original_id)
        updated_id, _ = self.save()
        self.assertNotEqual(original_id, updated_id)
        original_review = self.store.evidence('fixture')[0]['review']
        audit_count = self.count('audit')
        with self.assertRaisesRegex(ValueError, 'La evaluación cambió; recargue antes de revisar'):
            self.store.review(evidence_id, 'rejected', 'Bob synthetic', 'Decision based on stale view',
                              expected_run_id=original_id)
        self.assertEqual(self.store.evidence('fixture')[0]['review'], original_review)
        self.assertEqual(self.count('audit'), audit_count)

    def test_bank_revision_change_rejects_review_even_if_current_pointer_survives(self):
        run_id, _ = self.save()
        evidence_id = self.store.evidence('fixture')[0]['id']
        # An unrelated bank update increments the bank revision. Official
        # evidence retains its current pointer, so pointer equality alone is
        # insufficient for authorizing the old view's review.
        self.store.import_entities([dict(id='other', name='Otra Persona Sintética', kind='person')])
        self.assertEqual(self.store.latest_score_run('fixture')['id'], run_id)
        with self.assertRaisesRegex(ValueError, 'La evaluación cambió; recargue antes de revisar'):
            self.store.review(evidence_id, 'confirmed', 'Synthetic reader', 'Old bank snapshot',
                              expected_run_id=run_id)
        self.assertEqual(self.store.evidence('fixture')[0]['review'], {})

    def test_historical_run_and_metadata_change_cannot_authorize_live_review(self):
        old_id, _ = self.save()
        current_id, _ = self.save('2026-10-16')
        evidence_id = self.store.evidence('fixture')[0]['id']
        with self.assertRaisesRegex(ValueError, 'La evaluación cambió; recargue antes de revisar'):
            self.store.review(evidence_id, 'confirmed', 'Synthetic reader', 'Historical view', expected_run_id=old_id)
        with self.store.connection() as con:
            con.execute("UPDATE documents SET published_at='2000-01-01' WHERE id=?", (self.document_id,))
        with self.assertRaisesRegex(ValueError, 'La evaluación cambió; recargue antes de revisar'):
            self.store.review(evidence_id, 'confirmed', 'Synthetic reader', 'Changed source metadata',
                              expected_run_id=current_id)
        self.assertEqual(self.store.evidence('fixture')[0]['review'], {})

    def test_metadata_only_tampering_invalidates_historical_materialization(self):
        run_id, _ = self.save()
        for column, tampered in [('published_at', '2000-01-01'), ('source_kind', 'news'),
                                 ('metadata', '{"publisher_id":"altered.invalid"}'),
                                 ('title', 'Alterado'), ('retrieved_at', '2000-01-01'),
                                 ('source_id', 'altered'), ('url', 'https://fixture.invalid/altered')]:
            with self.subTest(column=column):
                with self.store.connection() as con:
                    original = con.execute('SELECT ' + column + ' FROM documents WHERE id=?',
                                           (self.document_id,)).fetchone()[0]
                    con.execute('UPDATE documents SET ' + column + '=? WHERE id=?', (tampered, self.document_id))
                self.assertFalse(self.store.verify_audit()['ok'])
                with self.assertRaises(ValueError):
                    self.store.get_score_run(run_id)
                with self.store.connection() as con:
                    con.execute('UPDATE documents SET ' + column + '=? WHERE id=?', (original, self.document_id))
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_append_only_triggers_reject_updates_and_deletions(self):
        run_id, _ = self.save()
        for operation in ('UPDATE score_runs SET payload=payload WHERE id=?',
                          'DELETE FROM score_runs WHERE id=?'):
            with self.subTest(operation=operation), self.assertRaises(sqlite3.IntegrityError):
                with self.store.connection() as con:
                    con.execute(operation, (run_id,))
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_payload_tampering_is_detected_even_if_update_guard_is_removed(self):
        run_id, _ = self.save()
        with self.store.connection() as con:
            con.execute('DROP TRIGGER score_runs_no_update')
            payload = json.loads(con.execute('SELECT payload FROM score_runs WHERE id=?', (run_id,)).fetchone()[0])
            payload['result']['score'] = 95
            con.execute('UPDATE score_runs SET payload=? WHERE id=?', (canonical(payload), run_id))
        self.assertEqual(self.store.verify_audit()['reason'], 'score_run_integrity')
        with self.assertRaises(ValueError):
            self.store.get_score_run(run_id)

    def test_rehashing_only_run_and_announcement_cannot_forge_trusted_history(self):
        run_id, original_result = self.save()
        evidence_id = self.store.evidence('fixture')[0]['id']
        with self.store.connection() as con:
            row = con.execute('SELECT payload,created_at FROM score_runs WHERE id=?', (run_id,)).fetchone()
            payload = json.loads(row['payload'])
            payload['result']['score'] = 95
            encoded = canonical(payload)
            forged_id = digest(encoded)
            con.execute('DELETE FROM scores')
            con.execute('DROP TRIGGER score_runs_no_update')
            con.execute('UPDATE score_runs SET id=?,payload=? WHERE id=?', (forged_id, encoded, run_id))
            announcement = con.execute("SELECT * FROM audit WHERE action='score_run_saved'").fetchone()
            event = json.loads(announcement['payload'])
            event['run_hash'] = forged_id
            event['result_hash'] = digest(canonical(payload['result']))
            event_body = canonical(event)
            # The attack repairs the announcing event's own hash but does not
            # rewrite neighboring events. A local-event-only check accepts it.
            event_hash = digest(canonical([announcement['at'], announcement['actor'], announcement['action'],
                                           event_body, announcement['previous_hash']]))
            con.execute('UPDATE audit SET payload=?,event_hash=? WHERE seq=?',
                        (event_body, event_hash, announcement['seq']))
            con.execute('INSERT INTO scores(entity_id,result,computed_at,run_id) VALUES(?,?,?,?)',
                        ('fixture', canonical(payload['result']), row['created_at'], forged_id))
        self.assertFalse(self.store.verify_audit()['ok'])
        reads = (
            lambda: self.store.get_score_run(forged_id),
            lambda: self.store.latest_score_run('fixture'),
            lambda: self.store.score_history('fixture'),
            lambda: self.store.run_evidence(forged_id, evidence_id),
            lambda: self.store.materialize_run_evidence({'id': forged_id, 'entity_id': 'fixture'}),
        )
        for read in reads:
            with self.subTest(read=read), self.assertRaisesRegex(ValueError, 'Cadena de auditoría inválida'):
                read()
        self.assertNotEqual(original_result['score'], 95)

    def test_history_verifies_full_chain_once_per_read_transaction(self):
        for index in range(5):
            self.save(provenance={'synthetic_revision': index})
        original = self.store._assert_audit_chain
        with patch.object(self.store, '_assert_audit_chain', wraps=original) as check:
            self.assertEqual(len(self.store.score_history('fixture')), 5)
            self.assertEqual(check.call_count, 1)

    def test_chain_validation_is_not_cached_across_transactions(self):
        run_id, _ = self.save()
        self.assertEqual(self.store.get_score_run(run_id)['id'], run_id)
        with self.store.connection() as con:
            con.execute("UPDATE audit SET payload='{}' WHERE seq=(SELECT max(seq) FROM audit)")
        with self.assertRaisesRegex(ValueError, 'Cadena de auditoría inválida'):
            self.store.get_score_run(run_id)

    def test_missing_announced_history_is_detected(self):
        run_id, _ = self.save()
        with self.store.connection() as con:
            con.execute('DROP TRIGGER score_runs_no_delete')
            con.execute('DELETE FROM scores')
            con.execute('DELETE FROM score_runs WHERE id=?', (run_id,))
        check = self.store.verify_audit()
        self.assertFalse(check['ok'])
        self.assertEqual(check['reason'], 'missing_score_run')

    def test_orphan_run_with_self_consistent_hash_is_detected(self):
        run_id, _ = self.save()
        with self.store.connection() as con:
            payload = json.loads(con.execute('SELECT payload FROM score_runs WHERE id=?', (run_id,)).fetchone()[0])
            payload['result']['score'] = 95
            encoded = canonical(payload)
            orphan_id = digest(encoded)
            con.execute('INSERT INTO score_runs VALUES(?,?,?,?)',
                        (orphan_id, 'fixture', encoded, '2026-10-15T00:00:00+00:00'))
        check = self.store.verify_audit()
        self.assertFalse(check['ok'])
        self.assertEqual(check['reason'], 'orphan_score_run')
        with self.assertRaises(ValueError):
            self.store.get_score_run(orphan_id)

    def test_created_at_and_current_pointer_are_anchored_in_events(self):
        run_id, _ = self.save()
        with self.store.connection() as con:
            con.execute('UPDATE scores SET run_id=NULL')
        self.assertEqual(self.store.verify_audit()['reason'], 'current_run_pointer_changed')
        with self.store.connection() as con:
            con.execute('UPDATE scores SET run_id=?', (run_id,))
            con.execute('DROP TRIGGER score_runs_no_update')
            con.execute("UPDATE score_runs SET created_at='2099-01-01'")
        self.assertEqual(self.store.verify_audit()['reason'], 'score_run_integrity')

    def test_save_is_atomic_when_audit_operation_fails(self):
        baseline_audit = self.count('audit')
        original = self.store._audit

        def failing_audit(con, action, payload, actor='local'):
            original(con, action, payload, actor)
            if action == 'entity_scored':
                raise RuntimeError('Synthetic transaction failure')

        with patch.object(self.store, '_audit', side_effect=failing_audit):
            with self.assertRaises(RuntimeError):
                self.save()
        self.assertEqual(self.count('scores'), 0)
        self.assertEqual(self.count('score_runs'), 0)
        self.assertEqual(self.count('audit'), baseline_audit)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_historical_ownership_and_target_lookup_beyond_200(self):
        # Two hundred newer records hide the old scoring evidence from the
        # ordinary evidence endpoint; its historical target remains addressable.
        original_evidence = self.store.evidence('fixture')[0]['id']
        for index in range(200):
            self.store.add_document(dict(url=f'https://fixture.invalid/new/{index}',
                text=f'José Peña Villaluz inauguró la biblioteca sintética {index}.',
                source_kind='official', retrieved_at='2026-10-16T00:00:00+00:00'), ['fixture'], analyze)
        run_id, _ = self.save()
        self.assertNotIn(original_evidence, {item['id'] for item in self.store.evidence('fixture', 200)})
        self.assertEqual(self.store.run_evidence(run_id, original_evidence)['id'], original_evidence)
        self.assertIsNone(self.store.run_evidence(run_id, 'unrelated-evidence'))
        self.assertIsNone(self.store.run_evidence('missing-run', original_evidence))
        self.assertIsNone(self.store.get_score_run(run_id, 'different-entity'))

    def test_materialization_preserves_original_input_order_and_unicode_offsets(self):
        self.store.add_document(dict(url='https://fixture.invalid/second',
            text='José Peña Villaluz fue absuelto de fraude.', source_kind='official',
            retrieved_at='2026-10-16T00:00:00+00:00'), ['fixture'], analyze)
        evidence = list(reversed(self.store.evidence('fixture')))
        result = score_entity(self.entity, evidence, AS_OF)
        run_id = self.store.save_score('fixture', result, entity_snapshot=self.entity, evidence_snapshot=evidence)
        run = self.store.get_score_run(run_id)
        materialized = self.store.materialize_run_evidence(run)
        self.assertEqual([item['id'] for item in evidence], [item['id'] for item in materialized])
        self.assertEqual(score_entity(run['entity_snapshot'], materialized, AS_OF), result)
        for item in materialized:
            for finding in item['analysis']['findings']:
                self.assertEqual(item['text'][finding['start']:finding['end']], finding['quote'])
            for mention in item['analysis']['mentions']:
                self.assertEqual(item['text'][mention['start']:mention['end']], mention['text'])
        # Caller edits cannot replace persisted scoring inputs.
        run['evidence_manifest'][0]['review'] = {'status': 'confirmed', 'actor': 'invented'}
        self.assertEqual(self.store.materialize_run_evidence(run), materialized)

    def test_export_includes_runs_once_and_streams_original_documents_separately(self):
        run_id, _ = self.save()
        rows = list(self.store.export())
        runs = [row for row in rows if row['record_type'] == 'score_run']
        documents = [row for row in rows if row['record_type'] == 'document']
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(documents), 1)
        self.assertEqual(runs[0]['id'], run_id)
        self.assertNotIn('text', runs[0]['evidence_manifest'][0])
        self.assertEqual(next(row for row in rows if row['record_type'] == 'entity')['score_run_id'], run_id)

    def test_reject_wrong_snapshot_body_or_membership_without_partial_history(self):
        evidence = self.store.evidence('fixture')
        baseline = score_entity(self.entity, evidence, AS_OF)
        for field, value in [('text', 'An altered source body'), ('document_id', 'another-document')]:
            with self.subTest(field=field):
                modified = copy.deepcopy(evidence)
                modified[0][field] = value
                with self.assertRaises(ValueError):
                    self.store.save_score('fixture', baseline, entity_snapshot=self.entity, evidence_snapshot=modified)
        self.assertEqual(self.count('score_runs'), 0)
        self.assertEqual(self.count('scores'), 0)

    def test_tampered_analysis_cannot_become_a_historical_score_factor(self):
        evidence = self.store.evidence('fixture')
        evidence[0]['analysis']['findings'][0]['stage'] = 'conviction'
        result = score_entity(self.entity, evidence, AS_OF)
        self.assertEqual(result['score'], 0)
        run_id = self.store.save_score('fixture', result, entity_snapshot=self.entity, evidence_snapshot=evidence)
        run = self.store.get_score_run(run_id)
        replay = score_entity(run['entity_snapshot'], self.store.materialize_run_evidence(run), AS_OF)
        self.assertEqual(replay, result)
        self.assertIn('cached_analysis_content_mismatch', replay['evidence_audit'][0]['exclusions'])

    def test_old_scores_migrate_without_inventing_past_snapshots(self):
        path = Path(self.temp.name) / 'old.sqlite3'
        con = sqlite3.connect(path)
        try:
            with con:
                con.executescript('CREATE TABLE scores(entity_id TEXT PRIMARY KEY,result TEXT NOT NULL,computed_at TEXT NOT NULL);')
                con.execute('INSERT INTO scores VALUES(?,?,?)', ('legacy', '{"score":0}', AS_OF))
        finally:
            con.close()
        migrated = Store(path)
        self.assertEqual(migrated.get_score('legacy'), {'score': 0})
        self.assertIsNone(migrated.latest_score_run('legacy'))
        self.assertEqual(migrated.score_history('legacy'), [])

    def test_compatible_save_without_explicit_snapshots_uses_transaction_inputs(self):
        evidence = self.store.evidence('fixture')
        result = score_entity(self.entity, evidence, AS_OF)
        run_id = self.store.save_score('fixture', result)
        run = self.store.get_score_run(run_id)
        self.assertEqual(run['entity_snapshot'], self.entity)
        self.assertEqual(self.store.materialize_run_evidence(run)[0]['text'], evidence[0]['text'])
        self.assertTrue(self.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
