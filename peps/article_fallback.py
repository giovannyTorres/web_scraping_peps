"""Conservative, local-only recovery of otherwise insufficient HTML bodies.

The existing editorial parser stays authoritative for usable and explicitly
empty stories. This module never requests URLs, images, scripts or models.
"""
from __future__ import annotations

import configparser
import hashlib
from importlib import resources
import json
import re
import unicodedata

DEPENDENCY_VERSION = '2.3.1'
FALLBACK_VERSION = 'trafilatura-fast-precision-2.3.1-v1'
MINIMUM_BODY_CHARS = 220
MAXIMUM_HTML_BYTES = 2_000_000
CONFIG = {'MAX_TREE_SIZE': '50000', 'EXTRACTION_TIMEOUT': '5',
          'MAX_FILE_SIZE': str(MAXIMUM_HTML_BYTES), 'EXTENSIVE_DATE_SEARCH': 'off'}
OPTIONS = {'fast': True, 'favor_precision': True, 'include_comments': False,
           'include_tables': False, 'include_images': False, 'include_links': False,
           'include_formatting': False}
CONFIG_SHA256 = hashlib.sha256(json.dumps({'config': CONFIG, 'options': OPTIONS,
    'minimum_chars': MINIMUM_BODY_CHARS, 'policy': FALLBACK_VERSION},
    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
_AD_LINE = re.compile(r'^\s*\[?(?:publicidad|advertisement)\]?\s*$', re.I)
_PAYWALL = re.compile(r'"isAccessibleForFree"\s*:\s*(?:false|"false")', re.I)
_BLOCK_CLASS = re.compile(r'(?:^|[-_\s])(?:related|recommend\w*|sidebar|side-notice|'
                          r'comments?|nav|menu|cookie|consent|share|sharing|social)(?:$|[-_\s])', re.I)
_WALL_CLASS = re.compile(r'(?:^|[-_\s])(?:paywall|subscription-wall|login-required)(?:$|[-_\s])', re.I)
_NOTICE = re.compile(r'^(?:just a moment|access denied|verify (?:you|that)|'
                     r'comprobar que|aceptar (?:todas|cookies)|privacy preferences|'
                     r'preferencias de privacidad|iniciar sesi[oó]n|suscr[ií]bete)', re.I)


def _normal(text):
    return ' '.join(unicodedata.normalize('NFC', text).split())


def _extraction_config():
    """Construct the exact configuration shared by extraction and provenance."""
    import trafilatura
    config = configparser.ConfigParser()
    config.read_dict({section: dict(trafilatura.settings.DEFAULT_CONFIG[section])
                      for section in trafilatura.settings.DEFAULT_CONFIG})
    config['DEFAULT'].update(CONFIG)
    return config


def _effective_config_sha256(config):
    values = {section: dict(config[section]) for section in config}
    return hashlib.sha256(json.dumps(values, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def runtime_details(*, _config=None):
    """JSON-serializable provenance, also checked inside the frozen executable."""
    try:
        import trafilatura
        version = trafilatura.__version__
        config = _config if _config is not None else _extraction_config()
        effective_sha256 = _effective_config_sha256(config)
        settings_sha256 = hashlib.sha256(resources.files('trafilatura').joinpath('settings.cfg').read_bytes()).hexdigest()
    except ImportError:
        version, effective_sha256, settings_sha256 = None, None, None
    except (FileNotFoundError, OSError):
        effective_sha256, settings_sha256 = None, None
    return {'method': FALLBACK_VERSION, 'dependency': 'trafilatura',
            'dependency_version': version, 'required_dependency_version': DEPENDENCY_VERSION,
            'available': version == DEPENDENCY_VERSION and settings_sha256 is not None,
            'config_sha256': CONFIG_SHA256, 'effective_config_sha256': effective_sha256,
            'settings_cfg_sha256': settings_sha256,
            'minimum_chars': MINIMUM_BODY_CHARS, 'maximum_html_bytes': MAXIMUM_HTML_BYTES,
            'network': False, 'models_downloaded': False}


def recover_article_text(raw_html, *, expected_editorial=''):
    """Return a witnessed body or abstain; input is an already acquired string.

    A nonempty visible headline and two substantial source paragraphs must
    support the output. Every returned line is source paragraph text or that
    headline, preventing navigation and recommendation headlines from becoming
    body evidence. Source restrictions remain the collector's responsibility.
    """
    if not isinstance(raw_html, str) or not raw_html or len(raw_html.encode('utf-8')) > MAXIMUM_HTML_BYTES:
        return ''
    if _PAYWALL.search(raw_html):
        return ''
    try:
        config = _extraction_config()
    except ImportError:
        raise RuntimeError('La extracción requiere trafilatura==2.3.1; instale requirements-extraction.lock.txt.') from None
    details = runtime_details(_config=config)
    if not details['available']:
        raise RuntimeError('La extracción requiere trafilatura==2.3.1; instale requirements-extraction.lock.txt.')
    import trafilatura
    from lxml import html

    try:
        tree = html.fromstring(raw_html, parser=html.HTMLParser(no_network=True,
            recover=True, huge_tree=False, remove_comments=True, remove_pis=True))
    except (ValueError, TypeError):
        return ''
    title = _normal(' '.join(tree.xpath('//title/text()')))
    if _NOTICE.search(title):
        return ''
    # Inspect visibility before deleting nodes. A layout named right-sidebar
    # on the page or main wrapper is not itself a recommendation sidebar.
    for node in list(tree.iter()):
        if not isinstance(node.tag, str) or node.getparent() is None:
            continue
        classes = node.get('class', '')
        if _WALL_CLASS.search(classes):
            return ''
        layout = node.tag in ('html', 'body') or bool(set(classes.split()) & {'main', 'main-content'})
        navigation_classes = ' '.join(name for name in classes.split() if not name.startswith('has-'))
        hidden = (node.tag in {'nav', 'aside', 'footer', 'form', 'script', 'style', 'noscript',
                              'iframe', 'template', 'svg', 'button'}
                  or node.get('aria-hidden', '').lower() == 'true' or 'hidden' in node.attrib
                  or bool(re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', node.get('style', ''), re.I))
                  or (not layout and bool(_BLOCK_CLASS.search(navigation_classes))))
        if hidden:
            node.drop_tree()
    headlines = [_normal(n.text_content()) for n in tree.xpath('//h1')]
    headlines = [text for text in headlines if len(text) >= 15 and len(text.split()) >= 3 and not _NOTICE.search(text)]
    if len(headlines) != 1:
        return ''
    paragraphs = [_normal(n.text_content()) for n in tree.xpath('//p')]
    paragraphs = [text for text in paragraphs if len(text) >= 40 and not _NOTICE.search(text)]
    if len(paragraphs) < 2:
        return ''
    try:
        result = trafilatura.extract(html.tostring(tree, encoding='unicode'), config=config, **OPTIONS) or ''
    except (ValueError, TypeError, RuntimeError, RecursionError):
        return ''
    lines = [_normal(line) for line in result.splitlines() if line.strip() and not _AD_LINE.fullmatch(line)]
    body = '\n\n'.join(lines).strip()
    if len(body) < MINIMUM_BODY_CHARS or any(_NOTICE.search(line) for line in lines):
        return ''
    witnessed = [line for line in lines if len(line) >= 40 and any(line in p for p in paragraphs)]
    if len(witnessed) < 2:
        return ''
    # Do not admit opaque footer fragments, lists or a different card's title.
    if any(line not in headlines and not any(line in p for p in paragraphs) for line in lines):
        return ''
    expected = _normal(expected_editorial)
    if expected and expected not in _normal(body):
        return ''
    return body
