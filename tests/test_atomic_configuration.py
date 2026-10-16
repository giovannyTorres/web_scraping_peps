"""Readers see a complete configuration while source permissions are saved."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from peps.engine import Engine, _write_config_atomic
from peps.news import NewsAcquirer


class AtomicConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.engine = Engine(self.directory)
        self.news = NewsAcquirer(self.engine)

    def concurrent_read_while_saving(self, owner, mutate):
        before = owner.config()
        paused, resume = threading.Event(), threading.Event()
        errors = []
        original_write, original_replace = Path.write_text, os.replace

        def wait_for_reader():
            paused.set()
            if not resume.wait(5):
                raise AssertionError('Reader did not release configuration writer')

        def truncating_write(path, *args, **kwargs):
            # Reproduce the former implementation's observable empty-file
            # window deterministically, without racing thread scheduling.
            if path == owner.config_path:
                with path.open('w', encoding='utf-8'):
                    pass
                wait_for_reader()
            return original_write(path, *args, **kwargs)

        def publish(temporary, target):
            if Path(target) == owner.config_path:
                wait_for_reader()
            return original_replace(temporary, target)

        def writer():
            try:
                mutate()
            except BaseException as error:
                errors.append(error)

        with patch.object(Path, 'write_text', truncating_write), patch('peps.engine.os.replace', publish):
            thread = threading.Thread(target=writer)
            thread.start()
            try:
                self.assertTrue(paused.wait(5), 'Writer did not reach the publication boundary')
                self.assertEqual(owner.config(), before)
            finally:
                resume.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        self.assertNotEqual(owner.config(), before)

    def test_news_permission_reader_sees_old_complete_json_until_new_permissions_publish(self):
        source = next(item for item in self.news.sources() if not item.get('discovery_only'))
        old = source.get('enabled', False)
        self.concurrent_read_while_saving(self.news,
            lambda: self.news.set_source(source['id'], not old, True, False, False))
        updated = next(item for item in self.news.sources() if item['id'] == source['id'])
        self.assertEqual(updated['enabled'], not old)
        self.assertTrue(updated['terms_approved'])
        self.assertFalse(updated['download_articles'])
        self.assertFalse(updated['article_terms_approved'])

    def test_primary_source_permission_reader_also_sees_complete_json(self):
        source = self.engine.sources()[0]
        self.concurrent_read_while_saving(self.engine,
            lambda: self.engine.set_source(source['id'], not source.get('enabled', False), True))

    def test_search_settings_use_the_same_atomic_publication(self):
        current = self.news.search_config()
        preset = 'public_role' if current['preset'] != 'public_role' else 'identity'
        self.concurrent_read_while_saving(self.news, lambda: self.news.set_search_config({'preset': preset}))

    def test_source_permission_save_preserves_file_mode_and_cleans_temporary_files(self):
        self.news.config_path.chmod(0o640)
        mode = stat.S_IMODE(self.news.config_path.stat().st_mode)
        source = next(item for item in self.news.sources() if not item.get('discovery_only'))
        before = set(self.directory.iterdir())
        self.news.set_source(source['id'], True, True)
        self.assertEqual(stat.S_IMODE(self.news.config_path.stat().st_mode), mode)
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_failed_publication_preserves_existing_configuration_and_removes_temporary_file(self):
        path = self.directory / 'isolated.json'
        path.write_text('{"sources":[]}', encoding='utf-8')
        before = set(self.directory.iterdir())
        with patch('peps.engine.os.replace', side_effect=OSError('Publication failed')):
            with self.assertRaisesRegex(OSError, 'Publication failed'):
                _write_config_atomic(path, {'sources': [{'id': 'replacement'}]})
        self.assertEqual(json.loads(path.read_text()), {'sources': []})
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_failed_serialization_never_replaces_existing_configuration(self):
        path = self.directory / 'isolated.json'
        path.write_text('{"sources":[]}', encoding='utf-8')
        before = set(self.directory.iterdir())
        with self.assertRaises(TypeError):
            _write_config_atomic(path, {'sources': [object()]})
        self.assertEqual(json.loads(path.read_text()), {'sources': []})
        self.assertEqual(set(self.directory.iterdir()), before)


if __name__ == '__main__':
    unittest.main()
