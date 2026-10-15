"""Check recovery of a real 2.3.1 bank using an installed prior release.

Usage: python scripts/test_legacy_download_upgrade.py --legacy-python /venv/bin/python
The prior release creates its own catalog, audit events and paused receipt. The
current code opens that same SQLite file and exercises the local HTTP API.
No external requests or user data are used; this optional check needs the old
release installed separately. Ordinary CI covers the corresponding frozen cases.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


LEGACY_SEED = '''
import importlib.metadata,json,sys,time,peps
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest,now
from peps.workflow import GuidedWorkflow
assert importlib.metadata.version('peps-mx') == '2.3.1'
engine=Engine(sys.argv[1]);news=NewsAcquirer(engine)
engine.store.import_entities([{'id':'legacy-recovery','name':'Entidad Ficticia de Recuperación','kind':'company'}])
source=next(s for s in news.sources() if s.get('kind')=='rss')
news.set_source(source['id'],True,True,False,False)
source=next(s for s in news.sources() if s['id']==source['id'])
with engine.store.connection() as con:
    con.execute('BEGIN IMMEDIATE')
    for number in range(235):
        url='https://legacy.fixture.invalid/article/'+str(number)
        news._observe({'url':url,'title':'Noticia ficticia '+str(number),'text':'Metadatos exclusivamente sintéticos.',
                       'published_at':'2024-06-15'},source,'https://legacy.fixture.invalid/feed.xml',
                      {'sha256':digest('legacy-fixture'),'retrieved_at':now()},None,connection=con)
        if number>=171:
            job_id=engine.store.enqueue('news_article',{'article_id':digest(url),'source_id':source['id'],'url':url},
                                       key='legacy-fixture-'+str(number),due=time.time()+3600,connection=con)
            con.execute("UPDATE jobs SET status='deferred',attempts=2,error='Espera sintética' WHERE id=?",(job_id,))
    engine.store._audit(con,'news_discovery_completed',{'fixture':True,'records':235})
workflow=GuidedWorkflow(engine.store,sources_provider=news.sources)
workflow.confirm_sources()
workflow.mark_download(status='paused',details={'pending':64,'cancelled':True})
with engine.store.connection() as con:
    receipt=json.loads(con.execute('SELECT payload FROM workflow_state WHERE id=1').fetchone()[0])
    rows={table:[dict(row) for row in con.execute('SELECT * FROM '+table+' ORDER BY '+key)]
          for table,key in [('jobs','id'),('news_articles','id'),('audit','seq')]}
print(json.dumps({'version':importlib.metadata.version('peps-mx'),'sources':news.sources(),
                 'module_file':peps.__file__,'rows':rows,
                 'receipt':receipt,'articles':news.stats()['articles'],'audit':engine.store.verify_audit()},ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-python', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/legacy-download-upgrade-validation/validation.json')
    args = parser.parse_args()
    from peps.app import create_server
    from peps.engine import Engine
    from peps.news import NewsAcquirer

    report = {'kind': 'actual_installed_231_to_current_http_recovery', 'checks': {},
              'external_requests': 0, 'current_version': importlib.metadata.version('peps-mx')}

    def checked(name, condition):
        assert condition, name
        report['checks'][name] = True

    def jobs(store):
        with store.connection() as con:
            return [dict(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]

    def preserves_legacy_rows(store, original, *, after_analysis=False):
        with store.connection() as con:
            articles = [dict(row) for row in con.execute('SELECT * FROM news_articles ORDER BY id')]
            audit_prefix = [dict(row) for row in con.execute(
                'SELECT * FROM audit WHERE seq<=? ORDER BY seq', (original['audit'][-1]['seq'],))]
        prior_articles = original['news_articles']
        if after_analysis:
            # Analysis creates metadata documents and refreshes attribution.
            # Article identity, metadata and discovery evidence stay unchanged.
            refreshed = {'attributed_signature', 'document_id'}
            articles = [{key: value for key, value in row.items() if key not in refreshed}
                        for row in articles]
            prior_articles = [{key: value for key, value in row.items() if key not in refreshed}
                              for row in prior_articles]
        return articles == prior_articles and audit_prefix == original['audit']

    class NoExternalTransport:
        def __init__(self, *_, **__):
            pass
        def fetch(self, *_, **__):
            raise AssertionError('The future-due legacy queue must not fetch or bypass its backoff')
        def metrics_snapshot(self):
            return {'external_requests': 0}
        def close(self):
            pass

    try:
        with tempfile.TemporaryDirectory(prefix='webscope-legacy-upgrade-') as tmp:
            # Preserve the venv launcher path: resolving its symlink would run
            # the base interpreter without the installed prior release.
            result = subprocess.run([str(args.legacy_python.absolute()), '-I', '-c', LEGACY_SEED, tmp],
                                    capture_output=True, encoding='utf-8', timeout=60)
            if result.returncode:
                raise AssertionError('The actual 2.3.1 seed failed: ' + result.stderr.strip())
            legacy = json.loads(result.stdout)
            checked('bank_and_receipt_were_created_by_the_actual_231_release',
                    legacy['version'] == '2.3.1' and legacy['articles'] == 235 and legacy['audit']['ok'] and
                    '/site-packages/peps/' in legacy['module_file'].replace('\\', '/'))
            original_jobs = legacy['rows']['jobs']
            engine = Engine(tmp)
            checked('opening_with_current_engine_preserves_the_original_jobs_articles_and_audit_prefix',
                    jobs(engine.store) == original_jobs and preserves_legacy_rows(engine.store, legacy['rows']))
            original_config = {source['id']: source for source in legacy['sources']}
            with patch('peps.collector.SafeFetcher', NoExternalTransport):
                server, _ = create_server(engine)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                address = 'http://127.0.0.1:' + str(server.server_port)
                try:
                    with urlopen(address, timeout=10) as response:
                        html = response.read().decode('utf-8')
                    token = re.search(r'<meta name="peps-token" content="([^"]+)"', html)[1]

                    def api(path, data=None):
                        body = json.dumps(data).encode() if data is not None else None
                        with urlopen(Request(address + '/api/' + path, data=body,
                            headers={'X-PEPS-Token': token, 'Content-Type': 'application/json'}), timeout=15) as response:
                            return json.load(response)

                    def idle(kind):
                        deadline = time.monotonic() + 25
                        while time.monotonic() < deadline:
                            status = api('stats')
                            if status['operation']['kind'] == kind and not status['operation']['busy']:
                                return status
                            threading.Event().wait(.02)
                        raise AssertionError('The upgraded operation did not finish')

                    news = NewsAcquirer(engine)
                    sources = {source['id']: source for source in news.sources()}
                    added = [source for key, source in sources.items() if key not in original_config]
                    checked('catalog_upgrade_only_adds_disabled_unapproved_sources', bool(added) and
                            all(not source.get('enabled') and not source.get('terms_approved') for source in added))
                    checked('existing_source_permissions_are_preserved', all(
                        sources[key].get('enabled') == source.get('enabled') and
                        sources[key].get('terms_approved') == source.get('terms_approved') and
                        sources[key].get('download_articles') == source.get('download_articles')
                        for key, source in original_config.items()))
                    workflow = api('workflow')
                    checked('legacy_paused_receipt_is_rebound_without_inventing_a_new_matching_scope',
                            workflow['sources_confirmed'] and workflow['coverage']['resume_available'] and
                            not workflow['coverage']['needs_new_search'] and workflow['counts']['pending_downloads'] == 64 and
                            jobs(engine.store) == original_jobs and preserves_legacy_rows(engine.store, legacy['rows']))
                    checked('new_targeted_plan_is_empty_for_the_original_generic_rss_permissions', news.search_plan()['query_count'] == 0)
                    started = api('news-acquire', {'online': True, 'resume': True})
                    checked('original_queue_can_resume_even_when_new_targeted_plan_is_empty', bool(started['operation_id']))
                    paused = idle('download')
                    checked('upgraded_scheduler_retains_the_exact_64_jobs_and_backoff',
                            paused['operation']['status'] == 'paused' and
                            paused['operation']['last_result']['stop_reason'] == 'backoff' and jobs(engine.store) == original_jobs)
                    closed = api('workflow-finish-download', {})
                    checked('upgraded_paused_bank_can_close_partial_coverage_without_rewriting_the_queue',
                            closed['coverage']['accepted_partial'] and closed['coverage']['closed_partial'] and
                            closed['actions']['analyze']['enabled'] and jobs(engine.store) == original_jobs)
                    api('workflow-analyze', {})
                    analyzed = idle('analysis')
                    explanation = api('explanation?id=legacy-recovery')
                    acquisition = explanation['result']['provenance']['acquisition']
                    checked('upgraded_bank_can_analyze_and_archive_the_actual_metadata_and_pending_limits',
                            analyzed['workflow']['analysis_current'] and acquisition['metadata_only_articles'] == 235 and
                            acquisition['pending_tasks'] == 64 and acquisition['accepted_partial'] is True)
                    checked('upgraded_partial_score_remains_replayable', api('replay', {'run_id': explanation['run']['id']})['matched'])
                    checked('all_original_articles_jobs_and_audit_history_survive_upgrade',
                            news.stats()['articles'] == 235 and jobs(engine.store) == original_jobs and
                            preserves_legacy_rows(engine.store, legacy['rows'], after_analysis=True) and
                            engine.store.verify_audit()['ok'])
                    report['disabled_source_ids_added'] = [source['id'] for source in added]
                    report['legacy_receipt'] = legacy['receipt']
                    report['legacy_module_file'] = legacy['module_file']
                    report['legacy_snapshots'] = {table: {
                        'rows': len(rows),
                        'sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
                    } for table, rows in legacy['rows'].items()}
                    report['acquisition'] = acquisition
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['passed'] = 'error' not in report and bool(report['checks']) and all(report['checks'].values())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
