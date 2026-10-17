"""Render a candidate workflow using PUBLIC root keys only; no remote actions."""
import argparse
import base64
from pathlib import Path

from secure_bridge import fingerprint, public_bytes, public_key, write_new

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root-sign-public', type=Path, required=True)
parser.add_argument('--root-encrypt-public', type=Path, required=True)
parser.add_argument('--template', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
sign, encrypt = public_key(args.root_sign_public), public_key(args.root_encrypt_public)
if fingerprint(sign) == fingerprint(encrypt):
    parser.error('Use separate root signing and decryption keys')
template = args.template.read_text(encoding='utf-8')
for marker, key in (('@@ROOT_SIGN_PUBLIC_PEM_B64@@', sign),
                    ('@@ROOT_ENCRYPT_PUBLIC_PEM_B64@@', encrypt)):
    if template.count(marker) != 1:
        parser.error('Unexpected workflow template')
    template = template.replace(marker, base64.b64encode(public_bytes(key)).decode('ascii'))
write_new(args.output, template.encode('utf-8'), 0o644)
print('public_key_workflow_rendered')
