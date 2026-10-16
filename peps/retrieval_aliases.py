"""Conservative personal-alias retrieval guard, separate from identity verdicts.

A short surname suffix is shared by relatives and unrelated people. Nearby
state or profession cannot establish which given name a document concerns.
Only an existing explicit verification plus recorded provenance can opt such
an alias back into retrieval. This helper never modifies the entity bank.
"""
from __future__ import annotations

from .identity import COMPANY_KINDS, STOP, _fold, _sequence, _verification


def retrieval_alias_allowed(entity, raw_alias):
    """Reject unverified one/two-token personal suffixes before stripping metadata.

    Canonical names, longer aliases, other nicknames and company aliases retain
    their existing policies. ``verified_aliases`` as a container name, a bare
    string or ``validated=True`` alone does not supply explicit verification.
    """
    if _fold(entity.get('kind', 'person')) in COMPANY_KINDS:
        return True
    alias = (raw_alias.get('name') or raw_alias.get('value') or raw_alias.get('alias')) if isinstance(raw_alias, dict) else raw_alias
    name = entity.get('name')
    if not isinstance(alias, str) or not isinstance(name, str):
        return True  # Existing parsing/validation decides malformed entries.
    canonical = tuple(word for word in _sequence(name) if word not in STOP)
    shortened = tuple(word for word in _sequence(alias) if word not in STOP)
    suffix_only = (0 < len(shortened) <= 2 and len(shortened) < len(canonical)
                   and canonical[-len(shortened):] == shortened)
    if not suffix_only:
        return True
    return (isinstance(raw_alias, dict) and raw_alias.get('verified') is True
            and _verification({}, 'alias', raw_alias)['verified'])
