"""One-shot GitHub runner bridge. Public console is deliberately data-free."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from secure_bridge import (
    MAX_HEADER, MAX_INPUT, PROTOCOL, canonical, fingerprint, keypair,
    public_key, seal, sha_file, strict_json, unseal, validate_binding,
    validate_private_inputs, write_new,
)


def paths():
    temporary = Path(os.environ['RUNNER_TEMP']).resolve()
    return temporary / 'ws-net-private', temporary / 'ws-net-public-key', temporary / 'ws-net-encrypted-result'


def prepare():
    private, public, _result = paths()
    if private.exists() or public.exists():
        raise ValueError('session_already_exists')
    private.mkdir(mode=0o700)
    public.mkdir(mode=0o700)
    (private / 'keys').mkdir(mode=0o700)
    (private / 'results').mkdir(mode=0o700)
    for role in ('SIGN', 'ENCRYPT'):
        raw = base64.b64decode(os.environ['ROOT_' + role + '_PUBLIC_PEM_B64'], validate=True)
        write_new(private / 'keys' / ('root-' + role.lower() + '-public.pem'), raw)
        public_key(private / 'keys' / ('root-' + role.lower() + '-public.pem'))
    keypair(private / 'keys' / 'runner-private.pem', public / 'runner-public.pem')
    binding = {'protocol': PROTOCOL, 'repository': os.environ['GITHUB_REPOSITORY'],
        'ref_name': os.environ['GITHUB_REF_NAME'], 'run_id': os.environ['GITHUB_RUN_ID'],
        'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'candidate_sha': os.environ['GITHUB_SHA'],
        'session_nonce': secrets.token_hex(32),
        'runner_public_sha256': fingerprint(public_key(public / 'runner-public.pem')),
        'root_sign_public_sha256': fingerprint(public_key(private / 'keys' / 'root-sign-public.pem')),
        'root_encrypt_public_sha256': fingerprint(public_key(private / 'keys' / 'root-encrypt-public.pem')),
        'input_deadline_utc': int(time.time()) + 600}
    validate_binding(binding)
    handshake = {'schema_version': 1, 'binding': binding,
        'public_key_file': 'runner-public.pem',
        'input_path': 'network-validation-input/' + binding['run_id'] + '-' + binding['run_attempt'] + '.pepsenc',
        'ticket_path': 'network-validation-input/' + binding['run_id'] + '-' + binding['run_attempt'] + '.ticket.json'}
    write_new(public / 'handshake.json', canonical(handshake), 0o644)
    write_new(private / 'handshake.json', canonical(handshake))


class WaitForInput(Exception):
    def __init__(self, delay=5):
        self.delay = max(1, delay)


def retry_delay(headers):
    raw = headers.get('Retry-After', '')
    try:
        return max(1, int(raw))
    except ValueError:
        try:
            return max(1, parsedate_to_datetime(raw).timestamp() - time.time())
        except (ValueError, TypeError):
            return 10


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # The token must never follow a redirect to another host.


def github_get(path, token, limit, remaining, *, raw=False):
    if not path.startswith('/repos/giovannyTorres/web_scraping_peps/') or remaining <= 0:
        raise ValueError('invalid_read_scope')
    headers = {'Accept': 'application/vnd.github.raw+json' if raw else 'application/vnd.github+json',
               'Authorization': 'Bearer ' + token, 'X-GitHub-Api-Version': '2022-11-28',
               'User-Agent': 'WebScope-Private-Validation-Bridge/1'}
    request = urllib.request.Request('https://api.github.com' + path, headers=headers)
    # Respect the runner's inherited gateway; no alternate route or fallback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(), NoRedirect())
    try:
        with opener.open(request, timeout=max(.1, min(15, remaining))) as response:
            value = bytearray()
            while True:
                block = response.read1(min(1024 * 1024, limit + 1 - len(value)))
                if not block:
                    return bytes(value)
                value.extend(block)
                if len(value) > limit:
                    raise ValueError('github_response_budget_exceeded')
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise WaitForInput() from None
        if exc.code in (429, 503) or exc.code == 403 and exc.headers.get('Retry-After'):
            raise WaitForInput(retry_delay(exc.headers)) from None
        raise ValueError('github_read_denied') from None


def poll_input(handshake, destination, token, *, getter=github_get, clock=time.monotonic,
               wall=time.time, sleep=time.sleep):
    binding = handshake['binding']
    validate_binding(binding)
    budget = min(600, max(0, binding['input_deadline_utc'] - wall()))
    deadline = clock() + budget
    prefix = '/repos/' + binding['repository']
    while clock() < deadline:
        try:
            branch = urllib.parse.quote(binding['ref_name'], safe='/')
            head = strict_json(getter(prefix + '/git/ref/heads/' + branch, token, MAX_HEADER,
                                     deadline - clock()))['object']['sha']
            if not isinstance(head, str) or len(head) != 40 or any(c not in '0123456789abcdef' for c in head):
                raise ValueError('invalid_commit')
            blobs = []
            # Both files are read from the SAME immutable commit. This later
            # commit supplies data only; executable code stays at GITHUB_SHA.
            for name, limit in ((handshake['input_path'], MAX_INPUT + 1024 * 1024),
                                (handshake['ticket_path'], MAX_HEADER)):
                path = urllib.parse.quote(name, safe='/')
                blobs.append(getter(prefix + '/contents/' + path + '?ref=' + head,
                                    token, limit, deadline - clock(), raw=True))
            destination.mkdir(mode=0o700)
            write_new(destination / 'input.pepsenc', blobs[0])
            write_new(destination / 'input.ticket.json', blobs[1])
            return head
        except WaitForInput as wait:
            sleep(min(wait.delay, max(0, deadline - clock())))
    raise TimeoutError('input_window_closed')


def benchmark_environment():
    # No GitHub/ACTIONS/runner token is inherited by the analytical child.
    # Preserve the inherited gateway exactly; the frozen config controls UA.
    allowed = ('PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TZ', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
               'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY',
               'https_proxy', 'http_proxy', 'all_proxy', 'no_proxy')
    return {key: os.environ[key] for key in allowed if key in os.environ}


def launch_parameters(path):
    value = strict_json(Path(path).read_bytes())
    limits = {'seconds': (1, 2400), 'acquire_seconds': (1, 2400), 'max_http': (1, 50000),
        'workers': (1, 32), 'body_budget': (1, 5000), 'per_entity_body_limit': (1, 100),
        'max_jobs': (1, 100000), 'api_workers': (1, 8), 'api_timeout': (1, 300)}
    if (not isinstance(value, dict) or set(value) != set(limits) | {'root_go', 'api_batch_size'}
            or value['root_go'] is not True or type(value['api_batch_size']) is not int
            or value['api_batch_size'] != 25):
        raise ValueError('invalid_launch_parameters')
    for name, (minimum, maximum) in limits.items():
        if type(value[name]) is not int or not minimum <= value[name] <= maximum:
            raise ValueError('invalid_launch_budget')
    if value['acquire_seconds'] > value['seconds']:
        raise ValueError('invalid_launch_budget')
    return value


def execute_benchmark(checkout, inputs, results):
    policy = validate_private_inputs(inputs)
    importer = checkout / 'scripts' / 'network_validation_bridge' / 'import_negative_host_policy.py'
    if not importer.is_file() or sha_file(importer) != policy['importer_sha256']:
        raise ValueError('reviewed_policy_importer_missing_or_changed')
    settings = launch_parameters(inputs / 'launch-parameters.json')
    runner = checkout / 'scripts' / 'validate_recovery_pipeline.py'
    if not runner.is_file():
        raise ValueError('candidate_benchmark_missing')
    args = [sys.executable, str(runner), '--roster', str(inputs / 'roster.json'),
        '--sources-config', str(inputs / 'sources-config.json'), '--data-dir', str(results / 'cold-bank'),
        '--closed-bank', str(results / 'closed-bank'), '--output', str(results / 'report.json'),
        '--protocol-lock', str(inputs / 'protocol-lock.json'), '--launch-lock', str(results / 'launch-lock.json'),
        '--close-receipt', str(results / 'close-receipt.json'), '--api-export', str(results / 'api-export.json'),
        '--host-policy-registry', str(inputs / 'negative-host-policy-registry.json'),
        '--host-policy-importer', str(importer),
        '--roster-sanitization-lock', str(inputs / 'roster-sanitization-lock.json'),
        '--api-transport-lock', str(inputs / 'api-transport-lock.json')]
    for name in ('utility-lock', 'census-policy-lock', 'query-input-lock'):
        if (inputs / (name + '.json')).exists():
            args.extend(['--' + name, str(inputs / (name + '.json'))])
    if 'negative_api_policy' in policy:
        api_importer = checkout / 'scripts' / 'network_validation_bridge' / 'import_negative_api_policy.py'
        if not api_importer.is_file() or sha_file(api_importer) != policy['negative_api_policy']['importer_sha256']:
            raise ValueError('reviewed_api_policy_importer_missing_or_changed')
        args.extend(['--api-policy-registry', str(inputs / 'negative-api-policy-registry.json'),
                     '--api-policy-importer', str(api_importer)])
    for name, value in settings.items():
        if name != 'root_go':
            args.extend(['--' + name.replace('_', '-'), str(value)])
    env = benchmark_environment()
    for phase, flags, timeout in (('freeze', ['--freeze-only'], 600),
                                  ('benchmark', ['--online', '--root-go'], settings['seconds'] + 600)):
        with (results / (phase + '.stdout')).open('xb') as stdout, (results / (phase + '.stderr')).open('xb') as stderr:
            outcome = subprocess.run(args + flags, cwd=checkout, env=env,
                                     stdout=stdout, stderr=stderr, timeout=timeout, check=False)
        if outcome.returncode:
            return outcome.returncode
    return 0


def run():
    private, public, artifact = paths()
    handshake = strict_json((private / 'handshake.json').read_bytes())
    results = private / 'results'
    checkout = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    status, input_commit = 1, None
    try:
        # POSIX wall-clock guard bounds even slow response bodies to <=10 min.
        # The polling loop also bounds every request and wait monotonically.
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('input_window_closed')))
        remaining = min(600, max(.1, handshake['binding']['input_deadline_utc'] - time.time()))
        signal.setitimer(signal.ITIMER_REAL, remaining)
        try:
            input_commit = poll_input(handshake, private / 'download', os.environ['GH_READ_TOKEN'])
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        unseal(private / 'download' / 'input.pepsenc', private / 'download' / 'input.ticket.json',
               private / 'inputs', private / 'keys' / 'runner-private.pem',
               private / 'keys' / 'root-sign-public.pem', handshake['binding'], kind='input')
        # Discard the token before invoking the benchmark; its env is separately
        # allowlisted as well. No child command comes from encrypted input.
        os.environ.pop('GH_READ_TOKEN', None)
        actual = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checkout,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout.decode().strip()
        if actual != handshake['binding']['candidate_sha']:
            raise ValueError('executable_commit_changed')
        status = execute_benchmark(checkout, private / 'inputs', results)
    except BaseException:
        with (results / 'bridge-private-error.txt').open('w', encoding='utf-8') as stream:
            traceback.print_exc(file=stream)
        status = 1
    finally:
        os.environ.pop('GH_READ_TOKEN', None)
        write_new(results / 'bridge-receipt.json', canonical({
            'binding': handshake['binding'], 'input_commit_sha': input_commit,
            'benchmark_return_code': status, 'candidate_checkout_never_updated': True,
            'output_contains_private_data': True, 'external_result_publication': 'encrypted_only'}))
        # Publish a complete two-file encrypted directory only after sealing.
        temporary = Path(tempfile.mkdtemp(prefix='ws-sealed-result-', dir=artifact.parent))
        try:
            seal(results, temporary / 'result.pepsenc', temporary / 'result.ticket.json',
                 private / 'keys' / 'root-encrypt-public.pem', private / 'keys' / 'runner-private.pem',
                 handshake['binding'], kind='output')
            temporary.rename(artifact)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'run', 'cleanup'))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.phase == 'prepare':
            prepare()
        elif args.phase == 'cleanup':
            shutil.rmtree(paths()[0], ignore_errors=True)
        elif run():
            parser.exit(1, 'private_validation_finished_with_findings\n')
        print('private_bridge_' + args.phase + '_complete')
    except Exception:
        parser.exit(1, 'private_bridge_failed_no_plaintext_published\n')


if __name__ == '__main__':
    main()
