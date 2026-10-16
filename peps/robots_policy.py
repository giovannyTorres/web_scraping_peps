"""RFC 9309 rule matching, independent of robots retrieval and crawl timing.

``urllib.robotparser`` remains useful for crawl-delay/request-rate extensions,
but treats ``*`` and ``$`` in paths literally.  This module handles only the
Allow/Disallow decision; it performs no network requests or source validation.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


_AGENT = re.compile(r"[A-Za-z_-]+\Z")
_PRODUCT = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEX = frozenset("0123456789abcdefABCDEF")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _normalise(value: str) -> tuple[str, int]:
    """Compare UTF-8 octets without decoding encoded reserved characters.

    Percent-encoded unreserved ASCII is equivalent to its literal form.  All
    other escapes retain their identity with uppercase hex; e.g. an encoded
    slash must not become a path separator.  The length counts represented
    octets, rather than the three characters in a percent escape.
    """
    result = []
    length = index = 0
    while index < len(value):
        char = value[index]
        if char == "%" and index + 2 < len(value) and all(
                digit in _HEX for digit in value[index + 1:index + 3]):
            octet = int(value[index + 1:index + 3], 16)
            decoded = chr(octet)
            result.append(decoded if decoded in _UNRESERVED else f"%{octet:02X}")
            index += 3
            length += 1
            continue
        raw = char.encode("utf-8")
        result.append(char if len(raw) == 1 else "".join(f"%{octet:02X}" for octet in raw))
        length += len(raw)
        index += 1
    return "".join(result), length


def _groups(body: str) -> list[tuple[list[str], list[tuple[bool, str]]]]:
    groups = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    seen_rule = False
    # RFC empty lines and unknown records do not end a group.  A new User-agent
    # after a rule starts the next group, including after an empty rule.
    for line in body.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.partition("#")[0]
        field, separator, value = line.partition(":")
        if not separator:
            continue
        field, value = field.strip(" \t").lower(), value.strip(" \t")
        if field == "user-agent":
            if value != "*" and not _AGENT.fullmatch(value):
                continue
            if seen_rule:
                groups.append((agents, rules))
                agents, rules, seen_rule = [], [], False
            agents.append(value.lower())
        elif field in ("allow", "disallow") and agents:
            seen_rule = True
            # Empty rules impose no restriction.  A path must start with /;
            # whitespace/control characters cannot be part of an RFC path.
            if value.startswith("/") and not any(ord(char) <= 32 or ord(char) == 127 for char in value):
                rules.append((field == "allow", value))
    if agents:
        groups.append((agents, rules))
    return groups


def _matches(pattern: str, target: str, anchored: bool) -> bool:
    """Match a leading path pattern without exponential regex backtracking."""
    fragments = pattern.split("*")
    if not target.startswith(fragments[0]):
        return False
    position = len(fragments[0])
    if len(fragments) == 1:
        return not anchored or position == len(target)
    for fragment in fragments[1:-1]:
        match = target.find(fragment, position)
        if match < 0:
            return False
        position = match + len(fragment)
    last = fragments[-1]
    if anchored:
        return target.endswith(last) and len(target) - len(last) >= position
    return target.find(last, position) >= 0


def robots_allowed(body: str, user_agent: str, url: str) -> bool:
    """Return the Allow/Disallow decision for an absolute HTTP(S) URL.

    Match robots product tokens case-insensitively against the HTTP header's
    first product name, ignoring its version and descriptive/contact comment.
    Use the most specific matching token and combine equally specific groups;
    use all ``*`` groups only when no specific token matches.  Path matching is
    case-sensitive on the normalized path and query, with the longest literal
    octet match winning and Allow winning ties.  Empty/no applicable rules allow
    access.  Invalid URL/header control characters fail closed.
    """
    if not isinstance(body, str) or not isinstance(user_agent, str) or not isinstance(url, str):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in user_agent):
        return False
    product_match = _PRODUCT.match(user_agent.lstrip(" "))
    if not product_match:
        return False
    product = product_match.group().lower()
    # urlsplit silently removes CR/LF/TAB; reject them before parsing.
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        _ = parts.port  # Validate malformed/out-of-range ports without I/O.
        path = parts.path or "/"
        if "?" in url.partition("#")[0]:
            path += "?" + parts.query
        target, _ = _normalise(path)
    except (ValueError, UnicodeError):
        return False

    selected: list[tuple[bool, str]] = []
    best_agent = -1
    for agents, rules in _groups(body):
        specificity = max((len(agent) for agent in agents if agent != "*" and agent in product), default=-1)
        if specificity < 0 and "*" in agents:
            specificity = 0
        if specificity < 0:
            continue
        if specificity > best_agent:
            selected, best_agent = list(rules), specificity
        elif specificity == best_agent:
            selected.extend(rules)

    best_rule = -1
    allowed = True
    for allow, raw_pattern in selected:
        anchored = raw_pattern.endswith("$")
        raw_pattern = raw_pattern[:-1] if anchored else raw_pattern
        try:
            # Split before normalization: %2A is a literal escape, not wildcard.
            normalized = [_normalise(fragment) for fragment in raw_pattern.split("*")]
        except UnicodeError:
            continue
        pattern = "*".join(fragment for fragment, _ in normalized)
        length = sum(size for _, size in normalized)
        if _matches(pattern, target, anchored):
            if length > best_rule:
                best_rule, allowed = length, allow
            elif length == best_rule and allow:
                allowed = True
    return allowed
