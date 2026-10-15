import argparse
import csv
import io
import json
import os
import sys
import time
from pathlib import Path

from .engine import Engine, capacity
from .store import read_entities


def default_data_dir():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.cwd()))) / 'PEPS-MX'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.cwd() / 'runtime'))) / 'peps-mx'


def main():
    parser = argparse.ArgumentParser(description='PEPS MX · Banco de fuentes públicas y prioridad de revisión')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--config', type=Path)
    parser.add_argument('--online', action='store_true', help='Permite sólo fuentes habilitadas y autorizadas')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('ui').add_argument('--no-browser', action='store_true')
    for command in ('import-entities', 'import-documents', 'import-legacy'):
        sub.add_parser(command).add_argument('file', type=Path)
    official = sub.add_parser('import-official', help='Valida completamente una copia oficial local antes de importarla')
    official.add_argument('file', type=Path)
    official.add_argument('--source', required=True, choices=('ofac', 'un', 'sat69b'))
    official.add_argument('--source-url', required=True, help='URL HTTPS del recurso oficial de origen')
    official.add_argument('--list-name', choices=('sdn', 'non_sdn'), default='sdn', help='Sólo para OFAC')
    sub.add_parser('score')
    sub.add_parser('reanalyze')
    sub.add_parser('queue')
    worker = sub.add_parser('worker')
    worker.add_argument('--workers', type=int, default=4)
    worker.add_argument('--max-jobs', type=int, default=1000)
    worker.add_argument('--seconds', type=int, default=300)
    worker.add_argument('--daemon', action='store_true')
    worker.add_argument('--daily', action='store_true', help='Encola noticias por ventana UTC diaria y atribuye localmente después de la descarga')
    sub.add_parser('stats')
    sub.add_parser('audit')
    sub.add_parser('retry')
    exp = sub.add_parser('export')
    exp.add_argument('file', type=Path)
    sub.add_parser('backup').add_argument('file', type=Path)
    sub.add_parser('capacity').add_argument('--entities', type=int, default=40000)
    for name in ('news-queue', 'news-acquire', 'news-attribute', 'news-stats', 'news-plan'):
        news_parser = sub.add_parser(name, help='Noticias: adquisición e identidad independientes')
        news_parser.add_argument('--news-config', type=Path)
        if name in ('news-queue', 'news-acquire'):
            news_parser.add_argument('--start', help='Ventana UTC YYYYMMDDHHMMSS')
            news_parser.add_argument('--end', help='Ventana UTC YYYYMMDDHHMMSS')
        if name == 'news-acquire':
            news_parser.add_argument('--workers', type=int, default=4)
            news_parser.add_argument('--seconds', type=int, default=120)
            news_parser.add_argument('--max-tasks', type=int, default=1000)
            news_parser.add_argument('--all-articles', action='store_true', help='Descargar cuerpos permitidos sin prefiltrar por banco')
            news_parser.add_argument('--no-queue', action='store_true')
        if name == 'news-attribute':
            news_parser.add_argument('--limit', type=int, default=1000)
            news_parser.add_argument('--force', action='store_true')
        if name == 'news-plan':
            news_parser.add_argument('--articles-per-entity', type=float, default=5)
            news_parser.add_argument('--unique-url-ratio', type=float, default=.6)
            news_parser.add_argument('--target-days', type=float, default=7)
    args = parser.parse_args()
    if args.command == 'capacity':
        print(json.dumps(capacity(args.entities), ensure_ascii=False, indent=2))
        return
    engine = Engine(args.data_dir, args.config, args.online)
    command = args.command or 'ui'
    if command == 'ui':
        from .app import serve
        serve(engine, open_browser=not getattr(args, 'no_browser', False))
        return
    if command.startswith('news-'):
        from .news import NewsAcquirer
        news = NewsAcquirer(engine, args.news_config)
        if command == 'news-queue':
            result = news.queue(start=args.start, end=args.end)
        elif command == 'news-acquire':
            result = news.acquire(workers=args.workers, seconds=args.seconds, max_articles=args.max_tasks,
                                  download_all=args.all_articles, queue=not args.no_queue, start=args.start, end=args.end)
        elif command == 'news-attribute':
            result = news.attribute(limit=args.limit, force=args.force)
        elif command == 'news-stats':
            result = news.stats()
        else:
            result = news.plan(articles_per_entity=args.articles_per_entity, unique_url_ratio=args.unique_url_ratio,
                               target_days=args.target_days)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if command == 'import-entities':
        result = {'imported': engine.store.import_entities(read_entities(args.file.read_text(encoding='utf-8-sig')))}
    elif command == 'import-documents':
        with args.file.open(encoding='utf-8-sig') as source:
            result = engine.import_documents(json.loads(line) for line in source if line.strip())
    elif command == 'import-official':
        import tempfile
        from .official import load_official_file, official_record_document
        phase = 'stage_validation'
        try:
            # Check provenance before parsing or publishing any list record.
            official_record_document({'source': args.source, 'name': 'validation', 'record_id': 'validation'},
                                     args.source_url)
            with tempfile.TemporaryDirectory(prefix='official-stage-', dir=engine.data_dir) as staging:
                staged_path = Path(staging) / 'records.jsonl'
                with staged_path.open('w', encoding='utf-8') as staged:
                    options = {'list_name': args.list_name} if args.source == 'ofac' else {}
                    validation = load_official_file(args.file, args.source,
                        on_record=lambda record: staged.write(json.dumps(record, ensure_ascii=False) + '\n'), **options)
                if not validation.get('complete'):
                    result = dict(validation, phase='stage_validation', imported_documents=0)
                    engine.store.audit('official_import_rejected', result)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    raise SystemExit(2)
                with staged_path.open(encoding='utf-8') as staged:
                    documents = (official_record_document(json.loads(line), args.source_url,
                                 file_sha256=validation['file_sha256']) for line in staged if line.strip())
                    phase = 'publishing'
                    imported = engine.import_documents(documents)
                result = dict(validation, phase='imported', **imported,
                              source_url=args.source_url,
                              qualification='Copia local; sin verificación de vigencia, identidad definitiva ni sincronización de bajas.')
                engine.store.audit('official_snapshot_imported', result)
        except (ValueError, OSError) as exc:
            print(json.dumps({'status': 'partial' if phase == 'publishing' else 'invalid',
                              'complete': False, 'phase': phase,
                              'error': str(exc)}, ensure_ascii=False, indent=2))
            raise SystemExit(2)
    elif command == 'import-legacy':
        with args.file.open(encoding='utf-8-sig', newline='') as source:
            rows = csv.DictReader(source)
            entities = {e['name']: e['id'] for e in engine.store.iter_entities()}
            def documents():
                for row in rows:
                    name = row.get('nombre_persona_input') or ''
                    if name not in entities:
                        continue
                    yield {'url': row.get('evidence_url') or row.get('url') or '',
                           'title': row.get('title') or row.get('evidence_title') or '',
                           'text': row.get('snippet') or row.get('evidence_text') or row.get('involvement_summary') or '',
                           'source_kind': 'legacy', 'source_id': row.get('source', 'legacy'),
                           'entity_ids': [entities[name]], 'metadata': {'legacy': True, 'requires_source_revalidation': True}}
            result = engine.import_documents(documents())
    elif command == 'score':
        result = {'scored': engine.rescore()}
    elif command == 'reanalyze':
        result = {'reanalyzed': engine.reanalyze(), 'scored': engine.rescore()}
    elif command == 'queue':
        result = {'queued': engine.queue_discovery()}
    elif command == 'worker':
        if args.daemon and not args.online:
            parser.error('worker --daemon requiere --online; no hay tareas de red en modo cerrado')
        try:
            last_window = None
            while True:
                if args.daily:
                    from .store import now
                    window = now()[:10]
                    if window != last_window:
                        from .news import NewsAcquirer
                        NewsAcquirer(engine).queue(window=window)
                        last_window = window
                result = engine.run(args.workers, args.max_jobs, args.seconds)
                if args.daily:
                    from .news import NewsAcquirer
                    result['news_attribution'] = NewsAcquirer(engine).attribute(limit=args.max_jobs)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not args.daemon:
                    return
                time.sleep(30)
        except KeyboardInterrupt:
            return
    elif command == 'stats':
        result = engine.store.stats()
    elif command == 'audit':
        result = engine.store.verify_audit()
    elif command == 'retry':
        result = {'requeued': engine.store.retry_failed()}
    elif command == 'export':
        args.file.parent.mkdir(parents=True, exist_ok=True)
        with args.file.open('w', encoding='utf-8') as dest:
            for row in engine.store.export():
                dest.write(json.dumps(row, ensure_ascii=False) + '\n')
        result = {'exported': str(args.file.resolve())}
    elif command == 'backup':
        engine.store.backup(args.file)
        result = {'backup': str(args.file.resolve())}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
