"""Synthetic offline bridge tests; never launch a benchmark or contact GitHub."""
import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

try:
    import cryptography  # noqa: F401
except ImportError:
    raise unittest.SkipTest('Optional encrypted CI transport needs cryptography; core runtime does not.')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'network_validation_bridge'))
import secure_bridge as bridge
import remote_job


class BridgeOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key_dir = Path(cls.keys.name)
        for role in ('root-sign', 'root-encrypt', 'runner', 'stranger'):
            bridge.keypair(cls.key_dir / (role + '.private.pem'), cls.key_dir / (role + '.public.pem'))

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.binding = {'protocol': bridge.PROTOCOL, 'repository': 'giovannyTorres/web_scraping_peps',
            'ref_name': 'verification/network-validation', 'run_id': '123456', 'run_attempt': '1',
            'candidate_sha': 'c' * 40, 'session_nonce': 'd' * 64,
            'runner_public_sha256': bridge.fingerprint(bridge.public_key(self.pub('runner'))),
            'root_sign_public_sha256': bridge.fingerprint(bridge.public_key(self.pub('root-sign'))),
            'root_encrypt_public_sha256': bridge.fingerprint(bridge.public_key(self.pub('root-encrypt'))),
            'input_deadline_utc': int(time.time()) + 600}

    def pub(self, role):
        return self.key_dir / (role + '.public.pem')

    def priv(self, role):
        return self.key_dir / (role + '.private.pem')

    def inputs(self):
        path = self.root / 'inputs'
        path.mkdir()
        for name in bridge.INPUT_NAMES:
            (path / name).write_text('{}')
        (path / 'sources-config.json').write_bytes(bridge.canonical({'user_agent': 'SyntheticFixedUA/1'}))
        (path / 'negative-host-policy-registry.json').write_bytes(bridge.canonical({
            'schema_version': 1, 'captured_at': '2026-10-18T00:00:00+00:00', 'scope': 'negative_only',
            'hosts': [{'host': 'denied.example', 'blocked_until': 253402214400,
                       'next_at': 253402214400, 'reason': 'operator_access_denied'},
                      {'host': 'retry.example', 'blocked_until': 0,
                       'next_at': 2000000000, 'reason': 'server_retry_after'},
                      {'host': 'robots.example', 'blocked_until': 253402214400,
                       'next_at': 253402214400, 'reason': 'robots_conservative_host_denial'}]}))
        rows = [{'id': f'fiction-{i}', 'name': f'Persona Enteramente Ficticia {i}',
                 'entity_group': 'notary' if i < 150 else 'municipal_president',
                 'context': {}, 'provenance': 'Synthetic offline bridge fixture'} for i in range(300)]
        rows[0]['context'] = {'documented_fields': {'office': {'value': 'Notaría ficticia',
            'source_url': 'https://directory.example/fictional-entry', 'source_sha256': 'e' * 64}}}
        (path / 'roster.json').write_bytes(bridge.canonical(rows))
        policy = {'schema_version': 1, 'registry_sha256': bridge.sha_file(path / 'negative-host-policy-registry.json'),
            'importer_sha256': 'a' * 64, 'operator_denials_included': True,
            'user_agent_sha256': bridge.hashlib.sha256(b'SyntheticFixedUA/1').hexdigest(),
            'retry_after_included': True, 'robots_restrictions_included': True,
            'conservative_robots_host_denials': True, 'no_content_or_success_cache': True,
            'restrictions_never_shortened': True}
        (path / 'policy-handoff.json').write_bytes(bridge.canonical(policy))
        return path

    def seal_input(self, binding=None):
        inputs = self.inputs()
        bridge.validate_private_inputs(inputs)
        envelope, ticket = self.root / 'in.pepsenc', self.root / 'in.ticket.json'
        bridge.seal(inputs, envelope, ticket, self.pub('runner'), self.priv('root-sign'),
                    binding or self.binding, kind='input')
        return inputs, envelope, ticket

    def test_input_and_output_round_trip_keep_private_sentinels_out_of_public_files(self):
        inputs, envelope, ticket = self.seal_input()
        opened = self.root / 'opened-input'
        bridge.unseal(envelope, ticket, opened, self.priv('runner'), self.pub('root-sign'),
                      self.binding, kind='input')
        self.assertEqual((opened / 'roster.json').read_bytes(), (inputs / 'roster.json').read_bytes())
        self.assertNotIn(b'Persona Enteramente Ficticia', envelope.read_bytes())
        self.assertNotIn(b'Persona Enteramente Ficticia', ticket.read_bytes())
        outputs = self.root / 'private-results'
        outputs.mkdir()
        sentinel = b'PRIVATE_BANK_AND_LOG_SENTINEL\x00' * 1000
        (outputs / 'closed-bank.sqlite3').write_bytes(sentinel)
        (outputs / 'benchmark.stdout').write_bytes(sentinel)
        result, result_ticket = self.root / 'out.pepsenc', self.root / 'out.ticket.json'
        bridge.seal(outputs, result, result_ticket, self.pub('root-encrypt'), self.priv('runner'),
                    self.binding, kind='output')
        bridge.unseal(result, result_ticket, self.root / 'opened-output', self.priv('root-encrypt'),
                      self.pub('runner'), self.binding, kind='output', now=time.time() + 86400)
        self.assertEqual((self.root / 'opened-output' / 'closed-bank.sqlite3').read_bytes(), sentinel)
        self.assertNotIn(b'PRIVATE_BANK_AND_LOG_SENTINEL', result.read_bytes())
        self.assertNotIn(b'PRIVATE_BANK_AND_LOG_SENTINEL', result_ticket.read_bytes())

    def test_ciphertext_signature_key_context_and_expiry_tampering_are_rejected(self):
        _inputs, envelope, ticket = self.seal_input()
        original = envelope.read_bytes()
        mutations = []
        for index in (len(original) // 2, len(original) - 1, len(bridge.MAGIC) + 4 + 10):
            mutated = bytearray(original)
            mutated[index] ^= 1
            mutations.append(bytes(mutated))
        for index, raw in enumerate(mutations):
            envelope.write_bytes(raw)
            destination = self.root / ('bad-cipher-' + str(index))
            with self.assertRaises(Exception):
                bridge.unseal(envelope, ticket, destination, self.priv('runner'),
                              self.pub('root-sign'), self.binding, kind='input')
            self.assertFalse(destination.exists())
        envelope.write_bytes(original)
        for key, value in (('run_id', '999999'), ('run_attempt', '2'), ('candidate_sha', 'f' * 40),
                           ('session_nonce', 'e' * 64)):
            binding = dict(self.binding, **{key: value})
            with self.subTest(binding=key), self.assertRaises(Exception):
                bridge.unseal(envelope, ticket, self.root / ('bad-' + key), self.priv('runner'),
                              self.pub('root-sign'), binding, kind='input')
        for recipient, signer in ((self.priv('stranger'), self.pub('root-sign')),
                                  (self.priv('runner'), self.pub('stranger'))):
            with self.assertRaises(Exception):
                bridge.unseal(envelope, ticket, self.root / 'wrong-key', recipient, signer,
                              self.binding, kind='input')
        with self.assertRaisesRegex(ValueError, 'input_expired'):
            bridge.unseal(envelope, ticket, self.root / 'expired', self.priv('runner'),
                          self.pub('root-sign'), self.binding, kind='input',
                          now=self.binding['input_deadline_utc'] + 1)
        record = json.loads(ticket.read_bytes())
        record['signature'] = base64.b64encode(b'bad-signature').decode()
        ticket.write_bytes(bridge.canonical(record))
        with self.assertRaises(Exception):
            bridge.unseal(envelope, ticket, self.root / 'bad-signature', self.priv('runner'),
                          self.pub('root-sign'), self.binding, kind='input')

    def test_gcm_tag_is_checked_even_when_an_authorized_sender_resigns_a_changed_envelope(self):
        _inputs, envelope, ticket = self.seal_input()
        raw = bytearray(envelope.read_bytes())
        raw[-1] ^= 1
        envelope.write_bytes(raw)
        record = json.loads(ticket.read_bytes())
        record['envelope_sha256'] = bridge.sha_file(envelope)
        record.pop('signature')
        signer = bridge.private_key(self.priv('root-sign'))
        signature = signer.sign(bridge.canonical(record), bridge.padding.PSS(
            mgf=bridge.padding.MGF1(bridge.hashes.SHA256()), salt_length=bridge.padding.PSS.DIGEST_LENGTH),
            bridge.hashes.SHA256())
        ticket.write_bytes(bridge.canonical(dict(record, signature=base64.b64encode(signature).decode())))
        destination = self.root / 'unauthenticated-plaintext'
        with self.assertRaises(Exception):
            bridge.unseal(envelope, ticket, destination, self.priv('runner'), self.pub('root-sign'),
                          self.binding, kind='input')
        self.assertFalse(destination.exists())

    def test_zip_paths_symlinks_duplicates_and_byte_caps_fail_before_extraction(self):
        for index, name in enumerate(('../escape', '/absolute', 'nested/../../escape', 'C:/escape',
                                      'nested\\escape', 'nested//escape')):
            archive = self.root / ('bad-path-' + str(index) + '.zip')
            with zipfile.ZipFile(archive, 'w') as target:
                target.writestr(name, 'private')
            with self.subTest(name=name), self.assertRaises(ValueError):
                bridge.extract_checked(archive, self.root / ('reject-' + str(index)), kind='output')
        archive = self.root / 'symlink.zip'
        with zipfile.ZipFile(archive, 'w') as target:
            info = zipfile.ZipInfo('link')
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            target.writestr(info, '../secret')
        with self.assertRaises(ValueError):
            bridge.extract_checked(archive, self.root / 'reject-link', kind='output')
        archive = self.root / 'huge.zip'
        with zipfile.ZipFile(archive, 'w') as target:
            target.writestr('body', b'x' * 32)
        with patch.object(bridge, 'MAX_OUTPUT', 16), self.assertRaises(ValueError):
            bridge.extract_checked(archive, self.root / 'reject-cap', kind='output')
        archive = self.root / 'duplicate.zip'
        with zipfile.ZipFile(archive, 'w') as target, patch('warnings.warn'):
            target.writestr('same', 'first')
            target.writestr('same', 'second')
        with self.assertRaises(ValueError):
            bridge.extract_checked(archive, self.root / 'reject-duplicate', kind='output')

    def test_input_whitelist_roster_labels_and_incomplete_policy_are_rejected(self):
        inputs = self.inputs()
        (inputs / 'gold.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'allowlisted'):
            bridge.validate_private_inputs(inputs)
        (inputs / 'gold.json').unlink()
        rows = json.loads((inputs / 'roster.json').read_bytes())
        rows[0]['benchmark_label'] = 'PRIVATE_LABEL'
        (inputs / 'roster.json').write_bytes(bridge.canonical(rows))
        with self.assertRaisesRegex(ValueError, 'unapproved_fields'):
            bridge.validate_private_inputs(inputs)
        rows[0].pop('benchmark_label')
        (inputs / 'roster.json').write_bytes(bridge.canonical(rows))
        policy = json.loads((inputs / 'policy-handoff.json').read_bytes())
        policy['robots_restrictions_included'] = False
        (inputs / 'policy-handoff.json').write_bytes(bridge.canonical(policy))
        with self.assertRaisesRegex(ValueError, 'handoff_not_complete'):
            bridge.validate_private_inputs(inputs)

    def test_poll_uses_one_immutable_commit_and_timeout_honors_retry_after(self):
        handshake = {'binding': self.binding, 'input_path': 'network-validation-input/123456-1.pepsenc',
                     'ticket_path': 'network-validation-input/123456-1.ticket.json'}
        requested = []

        def getter(path, _token, _limit, _remaining, *, raw=False):
            requested.append(path)
            if not raw:
                return bridge.canonical({'object': {'sha': 'a' * 40}})
            return b'ciphertext' if path.split('?')[0].endswith('pepsenc') else b'ticket'

        commit = remote_job.poll_input(handshake, self.root / 'download', 'DO_NOT_LOG_TOKEN', getter=getter)
        self.assertEqual(commit, 'a' * 40)
        self.assertTrue(all(path.endswith('?ref=' + 'a' * 40) for path in requested[1:]))
        moment, waits = [0.], []

        def denied(*_args, **_kwargs):
            raise remote_job.WaitForInput(3600)

        def advance(seconds):
            waits.append(seconds)
            moment[0] += seconds

        with self.assertRaises(TimeoutError):
            remote_job.poll_input(handshake, self.root / 'never-download', 'token', getter=denied,
                clock=lambda: moment[0], wall=lambda: self.binding['input_deadline_utc'] - 600,
                sleep=advance)
        self.assertEqual(waits, [600])
        self.assertFalse((self.root / 'never-download').exists())

    def test_child_environment_and_workflow_do_not_publish_private_material(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'PRIVATE_TOKEN', 'GH_READ_TOKEN': 'PRIVATE_TOKEN',
            'ACTIONS_RUNTIME_TOKEN': 'PRIVATE_TOKEN', 'HTTP_PROXY': 'https://proxy.invalid',
            'HTTPS_PROXY': 'https://proxy.invalid', 'ALL_PROXY': 'https://proxy.invalid'}):
            env = remote_job.benchmark_environment()
        self.assertFalse(set(env) & {'GITHUB_TOKEN', 'GH_READ_TOKEN', 'ACTIONS_RUNTIME_TOKEN',
                                   })
        for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):
            self.assertEqual(env[key], 'https://proxy.invalid')
        workflow = (ROOT / '.github' / 'workflows' / 'network-validation.yml.in').read_text()
        self.assertIn('runs-on: ubuntu-latest', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('paths: [.github/workflows/network-validation.yml]', workflow)
        self.assertNotIn('network-validation-input/**', workflow)
        self.assertNotIn('ws-net-private/', workflow)
        self.assertNotIn('actions/cache', workflow)
        self.assertEqual(workflow.count('runs-on:'), 1)

    def test_public_key_artifact_matches_exact_run_and_rejects_extra_files(self):
        handshake = {'schema_version': 1, 'binding': self.binding, 'public_key_file': 'runner-public.pem',
            'input_path': 'network-validation-input/123456-1.pepsenc',
            'ticket_path': 'network-validation-input/123456-1.ticket.json'}
        artifact = self.root / 'key-artifact.zip'
        with zipfile.ZipFile(artifact, 'w', compression=zipfile.ZIP_DEFLATED) as target:
            target.writestr('handshake.json', bridge.canonical(handshake))
            target.writestr('runner-public.pem', self.pub('runner').read_bytes())
        accepted = bridge.accept_key_artifact(artifact, self.root / 'accepted-key', self.pub('root-sign'),
            self.pub('root-encrypt'), '123456', '1', 'c' * 40)
        self.assertEqual(accepted, handshake)
        with self.assertRaises(ValueError):
            bridge.accept_key_artifact(artifact, self.root / 'wrong-attempt', self.pub('root-sign'),
                self.pub('root-encrypt'), '123456', '2', 'c' * 40)
        self.assertFalse((self.root / 'wrong-attempt').exists())
        with zipfile.ZipFile(artifact, 'a') as target:
            target.writestr('private.pem', 'must not exist in a public artifact')
        with self.assertRaises(ValueError):
            bridge.accept_key_artifact(artifact, self.root / 'extra-entry', self.pub('root-sign'),
                self.pub('root-encrypt'), '123456', '1', 'c' * 40)

    def test_workflow_renderer_serializes_only_parsed_public_keys(self):
        contaminated = self.root / 'contaminated-public.pem'
        marker = b'PRIVATE_MATERIAL_MUST_NEVER_BE_PUBLISHED'
        contaminated.write_bytes(self.pub('root-sign').read_bytes() + marker)
        output = self.root / 'rendered.yml'
        subprocess.run([sys.executable, str(ROOT / 'scripts/network_validation_bridge/render_workflow.py'),
                        '--root-sign-public', str(contaminated),
                        '--root-encrypt-public', str(self.pub('root-encrypt')),
                        '--template', str(ROOT / '.github/workflows/network-validation.yml.in'),
                        '--output', str(output)], check=True, capture_output=True)
        rendered = output.read_text()
        expected = base64.b64encode(bridge.public_bytes(bridge.public_key(contaminated))).decode()
        self.assertIn(expected, rendered)
        self.assertNotIn(base64.b64encode(contaminated.read_bytes()).decode(), rendered)
        self.assertNotIn(marker.decode(), rendered)


if __name__ == '__main__':
    unittest.main()
