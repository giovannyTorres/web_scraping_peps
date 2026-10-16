"""Stress local rescue on varied metadata, asymmetric history and a large bank.

Example: python scripts/stress_news_rescue.py --entities 3000 --refs 90000
         --max-seconds 60 --output runtime/stress/rescue.json

Uses temporary synthetic data and real SQLite; never contacts public sources.
An unfinished main case writes its measurements and exits 1. Cancellation and
deadline checks still run so an incomplete case is distinguishable from a hang.
Timings are local preparation measurements, not web throughput guarantees.
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

try:
    import resource
except ImportError:
    resource = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from peps.engine import Engine
from peps.news import NewsAcquirer, _BodyBudget
from peps.news_projection import ensure_projection
from peps.relevance import RelevanceIndex
from peps.store import canonical, digest, now

SOURCE = dict(id='stress', kind='rss', url='https://editorial.invalid/feed',
    allowed_hosts=['editorial.invalid'], enabled=True, terms_approved=True,
    download_articles=True, article_terms_approved=True, refresh_seconds=86400)


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ('peps/news.py', 'peps/news_projection.py', 'peps/acquisition_coverage.py', 'peps/relevance.py')}


def rss():
    try:
        return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, AttributeError):
        return None


def raw_hash(store):
    fingerprint = hashlib.sha256()
    with store.connection() as con:
        for table in ('news_articles', 'news_observations'):
            fingerprint.update(table.encode())
            for row in con.execute('SELECT * FROM ' + table + ' ORDER BY id'):
                fingerprint.update(canonical(list(row)).encode())
                fingerprint.update(b'\n')
    return fingerprint.hexdigest()


def seed(news, count, size):
    entities = [dict(id=f'e{i:05}', name=f'Persona Sintética de Archivo {i:05}', kind='person',
        aliases=[f'Archivo {i:05}', f'Apellido {i:05}'], context={'role': 'diputado', 'state': f'Estado {i % 32}'})
        if i % 3 else dict(id=f'e{i:05}', name=f'Comercial Sintética {i:05} S. A. de C. V.', kind='company',
                          aliases=[f'Empresa {i:05}']) for i in range(count)]
    news.store.import_entities(entities)
    def records():
        for i in range(size):
            # Each bank ID gets relevant and irrelevant records independently;
            # IDs do not correlate with the five-way relevance split.
            entity = entities[(i // 5) % count]
            title = entity['name'] + ' niega la acusación.' if i % 5 == 0 else 'Resumen general de actividad editorial.'
            summary = 'Contenido sintético de una nota y contexto para el análisis. ' * (2 + i % 17)
            yield (f'{i:064x}', f'https://editorial.invalid/article/{i}', 'stress-publisher', 'stress', title, summary,
                   '2026-01-01T00:00:00+00:00', 'feed_metadata_only', 'discovered')
    def observations():
        for i in range(size):
            if i % 19:
                continue
            for j in range(2 + i % 5):
                # Different excerpts, including a new pertinent snippet on an
                # initially neutral card, prevent a duplicate-only best case.
                title = (entities[(i * 13 + j) % count]['name'] + ' fue víctima de fraude.'
                         if j == 1 else 'Resumen actualizado de hechos generales.')
                yield (digest(f'obs{i}/{j}'), f'{i:064x}', 'stress', f'https://editorial.invalid/search/{j}',
                       f'https://editorial.invalid/article/{i}', f'2026-02-01T00:00:{j:02}+00:00', None,
                       canonical({'title': title, 'summary': 'Texto alternativo de publicación. ' * (3 + j)}))
        # 50000 older observations on one popular URL, with 80 distinct text
        # variants. This must not make a latest-observation lookup quadratic.
        for j in range(50_000):
            title = entities[j % count]['name'] if j % 625 == 0 else 'Resumen general histórico.'
            yield (digest('hot/' + str(j)), f'{1:064x}', 'stress', 'https://editorial.invalid/hot',
                   'https://editorial.invalid/article/1', f'2025-01-01T{j // 3600:02}:{j // 60 % 60:02}:{j % 60:02}+00:00', None,
                   canonical({'title': title, 'summary': 'Un dato de archivo sintético. ' * (1 + j % 4)}))
    with news.store.connection() as con:
        con.executemany('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,discovered_at,scope,status) '
                        'VALUES(?,?,?,?,?,?,?,?,?)', records())
        con.executemany('INSERT INTO news_observations VALUES(?,?,?,?,?,?,?,?)', observations())
    return entities


def measured(news, label, fn):
    before, start, cpu_start = rss(), time.perf_counter(), time.process_time()
    output = fn()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None
    if peak is not None and sys.platform != 'darwin':
        peak *= 1024
    return dict(name=label, seconds=time.perf_counter() - start, rss_before=before, rss_after=rss(),
                cpu_seconds=time.process_time() - cpu_start, peak_rss=peak, result=output)


def main():
    global ROOT, Engine, NewsAcquirer, _BodyBudget, ensure_projection, RelevanceIndex, canonical, digest, now
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--state', type=Path, help='Optional isolated persistent fixture; otherwise a fresh temporary bank.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--entities', type=int, default=3000)
    parser.add_argument('--refs', type=int, default=90000)
    parser.add_argument('--max-seconds', '--seconds', dest='seconds', type=float, default=60)
    parser.add_argument('--cancel-after-seconds', type=float, default=.25)
    parser.add_argument('--body-budget', type=int, default=300)
    parser.add_argument('--per-entity-limit', type=int, default=3)
    parser.add_argument('--build-projection', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.entities <= 100000 or args.refs < 2 or not 0 < args.seconds <= 120:
        parser.error('entities must be 1..100000, refs >=2, max-seconds >0 and <=120')
    if not 0 < args.cancel_after_seconds <= 5 or args.body_budget < 0 or args.per_entity_limit < 0:
        parser.error('cancel-after-seconds must be >0 and <=5; quotas must be nonnegative')
    # Reload imports from the selected checkout before touching any state.
    ROOT = args.source_root.resolve()
    sys.path.insert(0, str(ROOT))
    for name in list(sys.modules):
        if name == 'peps' or name.startswith('peps.'):
            del sys.modules[name]
    from peps.engine import Engine
    from peps.news import NewsAcquirer, _BodyBudget
    from peps.news_projection import ensure_projection
    from peps.relevance import RelevanceIndex
    from peps.store import canonical, digest, now
    temporary = tempfile.TemporaryDirectory(prefix='news-rescue-stress-') if args.state is None else None
    if temporary:
        args.state = Path(temporary.name)
    args.state.mkdir(parents=True, exist_ok=True)
    engine = Engine(args.state, online=False)
    news = NewsAcquirer(engine)
    news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': {'strategy': 'shared'}}))
    report = dict(at=now(), synthetic=True, public_network=False, source_sha256=hashes(), measurements=[],
        method='Real SQLite, varied positive metadata, repeated negative templates and 50000 observations on one URL.',
        limits=['The bank and articles are synthetic.', 'Local preparation time is not public-web throughput.',
                'Peak RSS includes fixture setup; RSS fields are null where the platform cannot measure them.'])
    with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')), \
         patch('socket.create_connection', side_effect=AssertionError('No network')):
        with news.store.connection() as con:
            populated = con.execute('SELECT count(*) FROM news_articles').fetchone()[0]
        start = time.perf_counter()
        entities = list(news.store.iter_entities()) if populated else seed(news, args.entities, args.refs)
        report['setup_seconds'] = time.perf_counter() - start
        report['fixture'] = dict(entities=len(entities))
        with news.store.connection() as con:
            report['fixture'].update({table: con.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                                     for table in ('news_articles', 'news_observations')})
        index = RelevanceIndex(entities)
        with news.store.connection() as con:
            index.bank_revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
            jobs_before = con.execute("SELECT count(*) FROM jobs WHERE kind='news_article'").fetchone()[0]
        from peps.relevance import MODEL_VERSION
        index.model_version = MODEL_VERSION
        report['fixture_reused'] = bool(populated)
        report['history_sha256_before'] = raw_hash(news.store)
        report['jobs_before'] = jobs_before
        if args.build_projection:
            report['measurements'].append(measured(news, 'projection', lambda: ensure_projection(news, index, time.monotonic() + args.seconds)))
        report['measurements'].append(measured(news, 'rescue_bounded', lambda: dict(news._rescue_bodies(
            index, [SOURCE], _BodyBudget(maximum=args.body_budget, per_entity=args.per_entity_limit), time.monotonic() + args.seconds))))
        with news.store.connection() as con:
            report['jobs_after_bounded'] = con.execute("SELECT count(*) FROM jobs WHERE kind='news_article'").fetchone()[0]
        requested = [None]
        def cancel():
            requested[0] = time.perf_counter()
            engine.stop_event.set()
        timer = threading.Timer(args.cancel_after_seconds, cancel)
        timer.start()
        report['measurements'].append(measured(news, 'rescue_cancel_after_250ms', lambda: dict(news._rescue_bodies(
            index, [SOURCE], _BodyBudget(maximum=args.body_budget, per_entity=args.per_entity_limit), time.monotonic() + args.seconds))))
        responded = time.perf_counter()
        requested_during_call = requested[0] is not None
        timer.join()
        report['cancel_response_seconds'] = max(0., responded - requested[0]) if requested_during_call else None
        engine.stop_event.clear()
        # Deadline already expired must not touch stored history or queue.
        report['measurements'].append(measured(news, 'rescue_expired', lambda: dict(news._rescue_bodies(
            index, [SOURCE], _BodyBudget(maximum=args.body_budget, per_entity=args.per_entity_limit), time.monotonic() - 1))))
        with news.store.connection() as con:
            row = con.execute('SELECT id,title,summary FROM news_articles WHERE id=?', (f'{1:064x}',)).fetchone()
            candidate = news._stored_candidate(row, index, con)
        report['hot_url_candidate_count'] = len(candidate['entity_ids'])
        report['history_sha256_after'] = raw_hash(news.store)
        report['audit_ok'] = news.store.verify_audit()['ok']
        report['source_sha256_after'] = hashes()
        bounded = next(case for case in report['measurements'] if case['name'] == 'rescue_bounded')
        expired = next(case for case in report['measurements'] if case['name'] == 'rescue_expired')
        report['checks'] = dict(completed_within_budget=bounded['seconds'] < args.seconds,
            projection_completed=(not args.build_projection or
                next(case for case in report['measurements'] if case['name'] == 'projection')['result'] is True),
            requested_shape=report['fixture']['entities'] == args.entities and report['fixture']['news_articles'] == args.refs,
            historical_leads_recovered=report['hot_url_candidate_count'] > 0,
            body_budget_preserved=report['jobs_after_bounded'] <= max(jobs_before, args.body_budget),
            cancellation_acknowledged=report['cancel_response_seconds'] is not None and report['cancel_response_seconds'] < 1,
            expired_deadline_respected=expired['seconds'] < 1,
            history_preserved=report['history_sha256_before'] == report['history_sha256_after'],
            audit_valid=report['audit_ok'], code_unchanged=report['source_sha256'] == report['source_sha256_after'])
        report['passed'] = all(report['checks'].values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
    if temporary:
        temporary.cleanup()
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
