"""Prueba pequeña de red autorizada; no evade bloqueos ni declara cobertura completa."""
import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peps.collector import SafeFetcher
from peps.sources import build_search_url, parse_results
from peps.store import now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((Path(__file__).resolve().parents[1] / 'peps/defaults/sources.json').read_text(encoding='utf-8'))
    config['user_agent'] = 'PEPS-MX-Validation/2.0 (+https://github.com/giovannyTorres/web_scraping_peps)'
    fetcher = SafeFetcher(config, args.state_dir, offline=False)
    gdelt = next(x for x in config['sources'] if x['id'] == 'gdelt')
    gdelt.update(enabled=True, terms_approved=True)
    official = {'id': 'doj', 'kind': 'official', 'base_url': 'https://www.justice.gov',
                'allowed_hosts': ['www.justice.gov'], 'enabled': True, 'terms_approved': True,
                'robots_required': True, 'min_interval_seconds': 5, 'max_bytes': 2000000, 'timeout_seconds': 20}
    inputs = [(gdelt, build_search_url({'id': 'probe', 'name': 'Claudia Sheinbaum Pardo'}, gdelt)),
              (official, 'https://www.justice.gov/es/usao-edny/pr/ex-secretario-de-seguridad-publica-mexicana-genaro-garcia-luna-condenado-mas-de-38')]
    def probe(item):
        source, url = item
        start = time.monotonic()
        decisions = []
        result = None
        while len(decisions) < 4 and time.monotonic() - start < 90:
            result = fetcher.fetch(url, source)
            decisions.append({k: v for k, v in result.items() if k != 'body'})
            if result['status'] != 'deferred' or result.get('request_attempted', True):
                break
            from datetime import datetime
            retry = datetime.fromisoformat(result['retry_at'].replace('Z', '+00:00')).timestamp()
            wait = max(.1, retry - time.time())
            if wait > 15:
                break
            time.sleep(wait + .05)
        records = parse_results(result, source) if result and result.get('status') in ('ok', 'not_modified') else []
        return {'source_id': source['id'], 'url': url, 'elapsed_seconds': round(time.monotonic() - start, 3),
                'decisions': decisions, 'records': records[:20], 'body_chars': len(result.get('body', '')) if result else 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(probe, inputs))
    report = {'generated_at': now(), 'scope': 'two_authorized_public_source_probes', 'results': results,
              'blocked_sources_are_not_bypassed': True,
              'limitation': 'No prueba cobertura web para 40 mil entidades; resultados completos requieren fuentes accesibles.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
