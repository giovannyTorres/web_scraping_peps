"""Optional bounded fnmatch rules for article paths, separate from discovery."""
from fnmatch import fnmatchcase
import re
from urllib.parse import unquote, urlsplit

ARTICLE_PATH_SCOPE_VERSION = 'article-path-scope-1'
ARTICLE_PATH_SCOPE_FLAG = '_article_path_scope'
MAX_GLOB_PATTERN_CHARS = 240
MAX_GLOB_WILDCARDS = 6
MAX_GLOB_PATTERNS = 32


def validated_article_globs(source):
    """Return allow/deny tuples; absent or None leaves that list unrestricted."""
    groups = []
    for key in ('article_path_globs', 'article_excluded_path_globs'):
        value = source.get(key)
        if value is None:
            groups.append(())
            continue
        if not isinstance(value, list) or len(value) > MAX_GLOB_PATTERNS:
            raise ValueError(key + ' must be a list of at most 32 path patterns')
        for pattern in value:
            if (not isinstance(pattern, str) or not pattern.startswith('/') or pattern.startswith('//')
                    or len(pattern) > MAX_GLOB_PATTERN_CHARS
                    or any(ord(char) < 32 or ord(char) == 127 for char in pattern)
                    or '\\' in pattern or '#' in pattern
                    or sum(pattern.count(char) for char in '*?[') > MAX_GLOB_WILDCARDS):
                raise ValueError(key + ' contains an invalid bounded path pattern')
        groups.append(tuple(value))
    return tuple(groups)


def article_path_allowed(url, source):
    """Match the decoded URL path only; query/host policy is enforced separately.

    Star follows fnmatch semantics and may cross slashes. An exclusion wins.
    Configured rules fail closed on malformed patterns and ambiguous escaped
    separators/traversal. Sources with no rules keep their previous behavior.
    """
    try:
        allowed, excluded = validated_article_globs(source)
    except ValueError:
        return False
    if not allowed and not excluded:
        return True
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
            return False
        raw = parts.path or '/'
        if len(raw) > 4096:
            return False
        path = unquote(raw, encoding='utf-8', errors='strict')
        if (not path.startswith('/') or path.startswith('//') or '\\' in path
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or re.search(r'%[0-9a-fA-F]{2}', path)
                or any(segment in ('.', '..') for segment in path.split('/'))):
            return False
    except (ValueError, UnicodeError):
        return False
    return ((not allowed or any(fnmatchcase(path, pattern) for pattern in allowed))
            and not any(fnmatchcase(path, pattern) for pattern in excluded))
