"""Private network-validation transport; no HTTP or benchmark code here.

RSA-OAEP wraps a random AES-256-GCM key. A separate RSA-PSS signing key
authenticates each complete envelope before any decryption is attempted.
Archives are streamed and extracted manually, never via extractall().
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import time
import zipfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PROTOCOL = 'webscope-network-bridge-v1'
MAGIC = b'WSNET01\n'
CHUNK = 1024 * 1024
MAX_INPUT = 64 * CHUNK
MAX_OUTPUT = 8 * 1024 * CHUNK
MAX_HEADER = 32 * 1024
INPUT_NAMES = frozenset({
    'roster.json', 'sources-config.json', 'protocol-lock.json',
    'roster-sanitization-lock.json', 'utility-lock.json', 'census-policy-lock.json',
    'api-transport-lock.json', 'negative-host-policy-registry.json',
    'policy-handoff.json', 'launch-parameters.json', 'query-input-lock.json',
})
REQUIRED_INPUTS = INPUT_NAMES - {'utility-lock.json', 'census-policy-lock.json', 'query-input-lock.json'}
BINDING_KEYS = frozenset({
    'protocol', 'repository', 'ref_name', 'run_id', 'run_attempt', 'candidate_sha',
    'session_nonce', 'runner_public_sha256', 'root_sign_public_sha256',
    'root_encrypt_public_sha256', 'input_deadline_utc',
})


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate_json_key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('nonfinite_json')))


def sha_file(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            value.update(block)
    return value.hexdigest()


def public_bytes(key):
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def public_key(path):
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError('rsa_public_key_required')
    return key


def private_key(path):
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise ValueError('rsa_private_key_required')
    return key


def fingerprint(key):
    if isinstance(key, rsa.RSAPrivateKey):
        key = key.public_key()
    return hashlib.sha256(public_bytes(key)).hexdigest()


def write_new(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def keypair(private_path, public_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    write_new(private_path, key.private_bytes(serialization.Encoding.PEM,
              serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    write_new(public_path, public_bytes(key.public_key()), 0o644)


def validate_binding(binding):
    if (not isinstance(binding, dict) or set(binding) != BINDING_KEYS
            or binding['protocol'] != PROTOCOL
            or binding['repository'] != 'giovannyTorres/web_scraping_peps'
            or binding['ref_name'] != 'verification/network-validation'
            or not re.fullmatch(r'[0-9a-f]{40}', str(binding['candidate_sha']))
            or not re.fullmatch(r'[0-9a-f]{64}', str(binding['session_nonce']))
            or any(not re.fullmatch(r'[0-9a-f]{64}', str(binding[key])) for key in
                   ('runner_public_sha256', 'root_sign_public_sha256', 'root_encrypt_public_sha256'))
            or any(not re.fullmatch(r'[1-9][0-9]*', str(binding[key])) for key in ('run_id', 'run_attempt'))
            or type(binding['input_deadline_utc']) is not int):
        raise ValueError('invalid_binding')
    if binding['root_sign_public_sha256'] == binding['root_encrypt_public_sha256']:
        raise ValueError('separate_root_keys_required')


def safe_name(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or name.startswith('/') or len(name) > 500):
        raise ValueError('unsafe_archive_path')
    path = PurePosixPath(name)
    if any(part in ('', '.', '..') for part in name.split('/')) or path.is_absolute():
        raise ValueError('unsafe_archive_path')
    return path


def pack_directory(root, archive, *, kind):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('regular_input_directory_required')
    limit = MAX_INPUT if kind == 'input' else MAX_OUTPUT
    count, total = 0, 0
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as output:
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise ValueError('archive_symlink_forbidden')
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError('archive_special_file_forbidden')
            name = path.relative_to(root).as_posix()
            safe_name(name)
            if kind == 'input' and name not in INPUT_NAMES:
                raise ValueError('input_file_not_allowlisted')
            total += path.stat().st_size
            count += 1
            if total > limit or count > (32 if kind == 'input' else 200000):
                raise ValueError('archive_budget_exceeded')
            output.write(path, name)
    if kind == 'input' and not REQUIRED_INPUTS <= {p.name for p in root.iterdir() if p.is_file()}:
        raise ValueError('required_input_missing')


def extract_checked(archive, destination, *, kind):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('destination_must_be_new')
    limit = MAX_INPUT if kind == 'input' else MAX_OUTPUT
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > (32 if kind == 'input' else 200000):
            raise ValueError('archive_count_exceeded')
        seen, total = set(), 0
        # Validate the complete central directory before creating any file.
        for item in entries:
            name = safe_name(item.filename)
            mode = item.external_attr >> 16
            if (item.filename in seen or item.is_dir() or item.flag_bits & 1
                    or item.compress_type != zipfile.ZIP_STORED
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError('unsafe_archive_entry')
            if kind == 'input' and str(name) not in INPUT_NAMES:
                raise ValueError('input_file_not_allowlisted')
            total += item.file_size
            seen.add(item.filename)
            if total > limit:
                raise ValueError('archive_budget_exceeded')
        if kind == 'input' and not REQUIRED_INPUTS <= seen:
            raise ValueError('required_input_missing')
        destination.mkdir(mode=0o700, parents=True)
        try:
            for item in entries:
                path = destination.joinpath(*safe_name(item.filename).parts)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with source.open(item) as incoming, path.open('xb') as outgoing:
                    os.chmod(path, 0o600)
                    copied = 0
                    for block in iter(lambda: incoming.read(CHUNK), b''):
                        copied += len(block)
                        if copied > item.file_size:
                            raise ValueError('archive_size_mismatch')
                        outgoing.write(block)
                    if copied != item.file_size:
                        raise ValueError('archive_size_mismatch')
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise


def seal(root, envelope, ticket_path, recipient_public, signer_private, binding, *, kind):
    validate_binding(binding)
    if kind not in ('input', 'output'):
        raise ValueError('invalid_envelope_kind')
    if kind == 'input' and time.time() > binding['input_deadline_utc']:
        raise ValueError('input_expired')
    recipient, signer = public_key(recipient_public), private_key(signer_private)
    recipient_id = binding['runner_public_sha256'] if kind == 'input' else binding['root_encrypt_public_sha256']
    signer_id = binding['root_sign_public_sha256'] if kind == 'input' else binding['runner_public_sha256']
    if fingerprint(recipient) != recipient_id or fingerprint(signer) != signer_id:
        raise ValueError('key_binding_mismatch')
    envelope, ticket_path = Path(envelope), Path(ticket_path)
    if envelope.exists() or ticket_path.exists():
        raise ValueError('envelope_must_be_new')
    envelope.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='ws-private-seal-') as scratch:
        os.chmod(scratch, 0o700)
        archive = Path(scratch) / 'payload.zip'
        pack_directory(root, archive, kind=kind)
        key, nonce = os.urandom(32), os.urandom(12)
        wrapped = recipient.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                    algorithm=hashes.SHA256(), label=PROTOCOL.encode()))
        header = canonical({'protocol': PROTOCOL, 'kind': kind, 'binding': binding,
            'cipher': 'AES-256-GCM', 'wrap': 'RSA-OAEP-SHA256',
            'nonce': base64.b64encode(nonce).decode(), 'wrapped_key': base64.b64encode(wrapped).decode(),
            'archive_bytes': archive.stat().st_size})
        aad = MAGIC + struct.pack('>I', len(header)) + header
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        cipher.authenticate_additional_data(aad)
        temporary = envelope.with_name(envelope.name + '.partial')
        try:
            with temporary.open('xb') as target, archive.open('rb') as source:
                os.chmod(temporary, 0o600)
                target.write(aad)
                for block in iter(lambda: source.read(CHUNK), b''):
                    target.write(cipher.update(block))
                target.write(cipher.finalize())
                target.write(cipher.tag)
            signed = {'protocol': PROTOCOL, 'kind': kind, 'binding': binding,
                      'envelope_sha256': sha_file(temporary), 'signer_sha256': signer_id}
            signature = signer.sign(canonical(signed), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                    salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
            ticket = dict(signed, signature=base64.b64encode(signature).decode())
            write_new(ticket_path, canonical(ticket))
            temporary.rename(envelope)
        except BaseException:
            temporary.unlink(missing_ok=True)
            ticket_path.unlink(missing_ok=True)
            raise


def unseal(envelope, ticket_path, destination, recipient_private, trusted_signer_public,
           expected_binding, *, kind, now=None):
    validate_binding(expected_binding)
    if kind not in ('input', 'output'):
        raise ValueError('invalid_envelope_kind')
    envelope = Path(envelope)
    maximum = MAX_INPUT if kind == 'input' else MAX_OUTPUT
    if envelope.stat().st_size > maximum + 32 * CHUNK:
        raise ValueError('envelope_budget_exceeded')
    ticket_raw = Path(ticket_path).read_bytes()
    if len(ticket_raw) > MAX_HEADER:
        raise ValueError('ticket_budget_exceeded')
    ticket = strict_json(ticket_raw)
    if (not isinstance(ticket, dict) or set(ticket) != {
            'protocol', 'kind', 'binding', 'envelope_sha256', 'signer_sha256', 'signature'}
            or ticket['protocol'] != PROTOCOL or ticket['kind'] != kind
            or ticket['binding'] != expected_binding):
        raise ValueError('ticket_binding_mismatch')
    if kind == 'input' and (time.time() if now is None else now) > expected_binding['input_deadline_utc']:
        raise ValueError('input_expired')
    signer, recipient = public_key(trusted_signer_public), private_key(recipient_private)
    signer_id = expected_binding['root_sign_public_sha256'] if kind == 'input' else expected_binding['runner_public_sha256']
    recipient_id = expected_binding['runner_public_sha256'] if kind == 'input' else expected_binding['root_encrypt_public_sha256']
    if (fingerprint(signer) != signer_id or ticket['signer_sha256'] != signer_id
            or fingerprint(recipient) != recipient_id):
        raise ValueError('key_binding_mismatch')
    signed = {key: value for key, value in ticket.items() if key != 'signature'}
    signer.verify(base64.b64decode(ticket['signature'], validate=True), canonical(signed),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    if sha_file(envelope) != ticket['envelope_sha256']:
        raise ValueError('envelope_digest_mismatch')
    with envelope.open('rb') as source, tempfile.TemporaryDirectory(prefix='ws-private-open-') as scratch:
        os.chmod(scratch, 0o700)
        prefix = source.read(len(MAGIC) + 4)
        if len(prefix) != len(MAGIC) + 4 or prefix[:len(MAGIC)] != MAGIC:
            raise ValueError('invalid_envelope_header')
        length = struct.unpack('>I', prefix[-4:])[0]
        if not 0 < length <= MAX_HEADER:
            raise ValueError('invalid_envelope_header')
        encoded = source.read(length)
        header = strict_json(encoded)
        if (not isinstance(header, dict) or set(header) != {
                'protocol', 'kind', 'binding', 'cipher', 'wrap', 'nonce', 'wrapped_key', 'archive_bytes'}
                or header['protocol'] != PROTOCOL or header['kind'] != kind
                or header['binding'] != expected_binding or header['cipher'] != 'AES-256-GCM'
                or header['wrap'] != 'RSA-OAEP-SHA256' or type(header['archive_bytes']) is not int
                or not 0 <= header['archive_bytes'] <= maximum + 32 * CHUNK):
            raise ValueError('invalid_envelope_header')
        remaining = envelope.stat().st_size - len(prefix) - length - 16
        if remaining != header['archive_bytes']:
            raise ValueError('envelope_size_mismatch')
        source.seek(-16, 2)
        tag = source.read(16)
        source.seek(len(prefix) + length)
        nonce = base64.b64decode(header['nonce'], validate=True)
        if len(nonce) != 12:
            raise ValueError('invalid_nonce')
        key = recipient.decrypt(base64.b64decode(header['wrapped_key'], validate=True),
              padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=PROTOCOL.encode()))
        if len(key) != 32:
            raise ValueError('invalid_aes_key')
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        cipher.authenticate_additional_data(prefix + encoded)
        archive = Path(scratch) / 'authenticated.zip'
        with archive.open('xb') as target:
            os.chmod(archive, 0o600)
            while remaining:
                block = source.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError('truncated_envelope')
                remaining -= len(block)
                target.write(cipher.update(block))
            target.write(cipher.finalize())
        # Authentication completed before parsing or extracting any ZIP member.
        extract_checked(archive, destination, kind=kind)


def validate_private_inputs(root):
    """Closed input roles; the signed package must contain no old bank or labels."""
    root = Path(root)
    if {path.name for path in root.iterdir()} - INPUT_NAMES:
        raise ValueError('input_file_not_allowlisted')
    if not REQUIRED_INPUTS <= {path.name for path in root.iterdir()}:
        raise ValueError('required_input_missing')
    roster = strict_json((root / 'roster.json').read_bytes())
    rows = roster if isinstance(roster, list) else roster.get('people', roster.get('entities', [])) if isinstance(roster, dict) else []
    allowed = {'id', 'name', 'kind', 'aliases', 'context', 'entity_group', 'source_url', 'source_urls', 'source', 'provenance'}
    context_allowed = {'state', 'municipality', 'city', 'role', 'position', 'cargo', 'office',
                       'notary_number', 'notaria', 'notary_office', 'source_url', 'source_urls', 'source', 'provenance',
                       'documented_fields'}
    if len(rows) != 300:
        raise ValueError('exact_300_sanitized_people_required')
    for row in rows:
        if (not isinstance(row, dict) or set(row) - allowed
                or not isinstance(row.get('context', {}), dict)
                or set(row.get('context', {})) - context_allowed):
            raise ValueError('roster_contains_unapproved_fields')
    policy = strict_json((root / 'policy-handoff.json').read_bytes())
    required = {'schema_version', 'registry_sha256', 'importer_sha256',
                'user_agent_sha256',
                'operator_denials_included', 'retry_after_included', 'robots_restrictions_included',
                'conservative_robots_host_denials', 'no_content_or_success_cache', 'restrictions_never_shortened'}
    if (not isinstance(policy, dict) or set(policy) != required or type(policy['schema_version']) is not int
            or policy['schema_version'] != 1
            or policy['registry_sha256'] != sha_file(root / 'negative-host-policy-registry.json')
            or not re.fullmatch(r'[0-9a-f]{64}', str(policy['importer_sha256']))
            or any(policy[key] is not True for key in required - {
                'schema_version', 'registry_sha256', 'importer_sha256', 'user_agent_sha256'})):
        raise ValueError('negative_policy_handoff_not_complete')
    config = strict_json((root / 'sources-config.json').read_bytes())
    if (not isinstance(config, dict) or not isinstance(config.get('user_agent'), str)
            or hashlib.sha256(config['user_agent'].encode()).hexdigest() != policy['user_agent_sha256']):
        raise ValueError('operator_user_agent_changed')
    registry = strict_json((root / 'negative-host-policy-registry.json').read_bytes())
    if (not isinstance(registry, dict) or set(registry) != {'schema_version', 'captured_at', 'scope', 'hosts'}
            or type(registry['schema_version']) is not int or registry['schema_version'] != 1
            or not isinstance(registry['hosts'], list) or len(registry['hosts']) > 100000):
        raise ValueError('invalid_negative_registry')
    seen_hosts = set()
    for row in registry['hosts']:
        if (not isinstance(row, dict) or set(row) != {'host', 'blocked_until', 'next_at', 'reason'}
                or not isinstance(row['host'], str) or not re.fullmatch(r'[a-z0-9.-]{1,253}', row['host'])
                or row['host'] in seen_hosts or not isinstance(row['reason'], str) or len(row['reason']) > 2000
                or any(type(row[key]) not in (int, float) or not math.isfinite(row[key]) or row[key] < 0
                       for key in ('blocked_until', 'next_at'))):
            raise ValueError('invalid_negative_registry_host')
        seen_hosts.add(row['host'])
    return policy


def extract_artifact_pair(artifact, destination, expected):
    """Unpack only the two known GitHub artifact entries with byte caps."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError('destination_must_be_new')
    with zipfile.ZipFile(artifact) as source:
        entries = source.infolist()
        if len(entries) != 2 or {entry.filename for entry in entries} != set(expected):
            raise ValueError('unexpected_artifact_contents')
        for entry in entries:
            mode = entry.external_attr >> 16
            if (entry.file_size > expected[entry.filename] or entry.is_dir()
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or entry.flag_bits & 1):
                raise ValueError('unsafe_artifact_entry')
        destination.mkdir(parents=True, mode=0o700)
        try:
            for entry in entries:
                with source.open(entry) as incoming, (destination / entry.filename).open('xb') as outgoing:
                    size = 0
                    for block in iter(lambda: incoming.read(CHUNK), b''):
                        size += len(block)
                        if size > expected[entry.filename]:
                            raise ValueError('artifact_budget_exceeded')
                        outgoing.write(block)
                    if size != entry.file_size:
                        raise ValueError('artifact_size_mismatch')
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise


def accept_key_artifact(artifact, destination, root_sign_public, root_encrypt_public,
                        run_id, run_attempt, candidate_sha):
    extract_artifact_pair(artifact, destination, {'handshake.json': MAX_HEADER, 'runner-public.pem': MAX_HEADER})
    try:
        handshake = strict_json((Path(destination) / 'handshake.json').read_bytes())
        binding = handshake['binding']
        validate_binding(binding)
        stem = 'network-validation-input/' + str(run_id) + '-' + str(run_attempt)
        if (set(handshake) != {'schema_version', 'binding', 'public_key_file', 'input_path', 'ticket_path'}
                or type(handshake['schema_version']) is not int or handshake['schema_version'] != 1
                or handshake['public_key_file'] != 'runner-public.pem'
                or handshake['input_path'] != stem + '.pepsenc' or handshake['ticket_path'] != stem + '.ticket.json'
                or binding['run_id'] != str(run_id) or binding['run_attempt'] != str(run_attempt)
                or binding['candidate_sha'] != candidate_sha
                or fingerprint(public_key(Path(destination) / 'runner-public.pem')) != binding['runner_public_sha256']
                or fingerprint(public_key(root_sign_public)) != binding['root_sign_public_sha256']
                or fingerprint(public_key(root_encrypt_public)) != binding['root_encrypt_public_sha256']
                or not time.time() < binding['input_deadline_utc'] <= time.time() + 600):
            raise ValueError('handshake_does_not_match_verified_run')
        return handshake
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    generate = sub.add_parser('keygen')
    generate.add_argument('--private', required=True, type=Path)
    generate.add_argument('--public', required=True, type=Path)
    accept = sub.add_parser('accept-key')
    accept.add_argument('--artifact', required=True, type=Path)
    accept.add_argument('--directory', required=True, type=Path)
    accept.add_argument('--root-sign-public', required=True, type=Path)
    accept.add_argument('--root-encrypt-public', required=True, type=Path)
    accept.add_argument('--run-id', required=True)
    accept.add_argument('--run-attempt', required=True)
    accept.add_argument('--candidate-sha', required=True)
    result = sub.add_parser('extract-result-artifact')
    result.add_argument('--artifact', required=True, type=Path)
    result.add_argument('--directory', required=True, type=Path)
    for name in ('seal', 'open'):
        command = sub.add_parser(name)
        command.add_argument('--directory', required=True, type=Path)
        command.add_argument('--envelope', required=True, type=Path)
        command.add_argument('--ticket', required=True, type=Path)
        command.add_argument('--handshake', required=True, type=Path)
        command.add_argument('--kind', required=True, choices=('input', 'output'))
        command.add_argument('--recipient-key', required=True, type=Path)
        command.add_argument('--signer-key', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'keygen':
            # Root keys must not accidentally land alongside publishable code.
            staging_root = Path(__file__).resolve().parents[2]
            if args.private.resolve().is_relative_to(staging_root):
                raise ValueError('private_key_must_be_outside_publishable_tree')
            keypair(args.private, args.public)
        elif args.command == 'accept-key':
            accept_key_artifact(args.artifact, args.directory, args.root_sign_public,
                                args.root_encrypt_public, args.run_id, args.run_attempt, args.candidate_sha)
        elif args.command == 'extract-result-artifact':
            extract_artifact_pair(args.artifact, args.directory, {
                'result.pepsenc': MAX_OUTPUT + 32 * CHUNK, 'result.ticket.json': MAX_HEADER})
        else:
            handshake = strict_json(args.handshake.read_bytes())
            binding = handshake['binding']
            if args.command == 'seal':
                if args.kind == 'input':
                    validate_private_inputs(args.directory)
                seal(args.directory, args.envelope, args.ticket, args.recipient_key,
                     args.signer_key, binding, kind=args.kind)
            else:
                unseal(args.envelope, args.ticket, args.directory, args.recipient_key,
                       args.signer_key, binding, kind=args.kind)
        print('bridge_ok')
    except Exception:
        # Never echo exception payloads, private paths, roster fields or config.
        parser.exit(1, 'bridge_operation_failed\n')


if __name__ == '__main__':
    main()
