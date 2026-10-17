"""Narrow operator-reviewed API grants, never a general robots bypass."""
import re
from urllib.parse import parse_qs, urlsplit


def documented_search_api(url, source):
    """The source catalogue is trusted configuration, not a result-page claim.

    A public API intentionally enabled by its operator can have a different
    contract from indexing search-result HTML. Require a reviewed, evidenced
    grant and restrict it to the documented JSON operation. Articles, arbitrary
    redirects and other providers cannot inherit this permission.
    """
    grant = source.get('documented_api_permission')
    if (source.get('kind') != 'searxng' or not isinstance(grant, dict)
            or grant.get('scope') != 'public_json_search_api'
            or grant.get('method') != 'GET' or grant.get('reviewed') is not True
            or source.get('download_articles') or source.get('article_terms_approved')
            or not re.fullmatch(r'[a-f0-9]{64}', str(grant.get('review_receipt_sha256', '')))):
        return False
    try:
        endpoint = urlsplit(grant.get('endpoint', ''))
        configured = urlsplit(source.get('search_url') or source.get('api_url') or source.get('url') or '')
        target = urlsplit(url)
        operator = urlsplit(grant.get('operator_documentation_url', ''))
        if (endpoint.scheme != 'https' or endpoint.path != '/search' or endpoint.query or endpoint.fragment
                or endpoint.username or endpoint.password or endpoint.port not in (None, 443)
                or not endpoint.hostname or operator.scheme != 'https' or operator.hostname != endpoint.hostname
                or grant.get('api_documentation_url') != 'https://docs.searxng.org/dev/search_api.html'):
            return False
        if any((p.scheme, p.netloc, p.path) != (endpoint.scheme, endpoint.netloc, endpoint.path)
               or p.fragment for p in (configured, target)):
            return False
        query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
        if (set(query) - {'q', 'format', 'language', 'pageno', 'categories', 'safesearch', 'time_range', 'engines'}
                or any(len(value) != 1 for value in query.values())
                or query.get('format') != ['json'] or not 1 <= len(query.get('q', [''])[0]) <= 500):
            return False
        return True
    except (ValueError, TypeError, AttributeError):
        return False
