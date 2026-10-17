"""Bounded, offline plans for entity-directed news discovery.

Queries retrieve candidates, never prove identity or conduct. Google News RSS
offers best-effort dated search, not an exhaustive historical archive. GDELT
DOC searches only its recent rolling window; older dates are never relabeled
as covered. General publisher feeds are an explicit, separate strategy.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import re
from urllib.parse import urlencode, urlsplit

from .identity import COMPANY_KINDS, GENERIC_CONTEXT, LEGAL_SUFFIXES, STOP, _fold, _tokens
from .store import canonical, digest
from .public_search import PUBLIC_SEARCH_KINDS, public_search_url, public_queries
from .retrieval_aliases import retrieval_alias_allowed

PRESETS = {
    'identity': {'label': 'Todas las noticias de la entidad', 'terms': ''},
    'public_role': {'label': 'Actividad pública y trayectoria',
                    'terms': '(gobierno OR alcalde OR senador OR diputado OR funcionario OR contrato OR nombramiento)'},
    'adverse': {'label': 'Señales adversas para revisión',
                'terms': '(corrupción OR fraude OR "lavado de dinero" OR investigación OR sanción OR detenido OR absuelto)'},
}
PUBLISHER_FOCUS_TERMS = {
    'public_role': ('gobierno', 'funcionario', 'contrato'),
    'adverse': ('corrupción', 'fraude', 'sanción', '"lavado de dinero"'),
}
SEARCH_KEYS = {'strategy', 'preset', 'start', 'end', 'window', 'max_queries', 'include_aliases', 'use_context',
               'execution_mode', 'session_seconds'}


def _utc(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.max, timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError('now requiere fecha o datetime')
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def default_search_config(now=None):
    """Compatibility fallback; the new-install catalogue explicitly selects quick."""
    today = _utc(now).date()
    return {'strategy': 'targeted', 'preset': 'identity', 'start': date(today.year - 10, 1, 1).isoformat(),
            'end': today.isoformat(), 'window': 'auto', 'max_queries': 1000,
            'include_aliases': True, 'use_context': True, 'execution_mode': 'extended', 'session_seconds': 900}


def normalize_search_config(value=None, now=None):
    if value is not None and not isinstance(value, dict):
        raise ValueError('La búsqueda requiere un objeto de configuración')
    if set(value or {}) - SEARCH_KEYS:
        raise ValueError('Opciones de búsqueda no reconocidas')
    result = {**default_search_config(now), **(value or {})}
    if 'session_seconds' not in (value or {}) and result['execution_mode'] == 'quick':
        result['session_seconds'] = 120
    if result['execution_mode'] not in ('quick', 'extended'):
        raise ValueError('execution_mode debe ser quick o extended')
    seconds = result['session_seconds']
    if type(seconds) is not int or seconds not in ((120,) if result['execution_mode'] == 'quick' else (600, 900, 1800, 3600, 7200)):
        raise ValueError('session_seconds requiere 120 para quick o 600, 900, 1800, 3600, 7200 para extended')
    if result['strategy'] not in ('targeted', 'shared', 'feeds', 'public'):
        raise ValueError('strategy debe ser targeted, shared, feeds o public')
    if result['preset'] not in PRESETS:
        raise ValueError('preset debe ser identity, public_role o adverse')
    if result['window'] not in ('auto', 'year', 'month'):
        raise ValueError('window debe ser auto, year o month')
    if type(result['max_queries']) is not int or not 1 <= result['max_queries'] <= 20000:
        raise ValueError('max_queries requiere entre 1 y 20000 consultas')
    for key in ('include_aliases', 'use_context'):
        if type(result[key]) is not bool:
            raise ValueError(key + ' debe ser booleano')
    try:
        first, last = [date.fromisoformat(result[key]) for key in ('start', 'end')]
    except (TypeError, ValueError):
        raise ValueError('El período requiere fechas ISO AAAA-MM-DD válidas') from None
    if first > last:
        raise ValueError('La fecha inicial debe ser anterior o igual a la final')
    if first.year < 1990 or last > _utc(now).date():
        raise ValueError('El período debe empezar desde 1990 y terminar como máximo hoy')
    return result


def merge_source_defaults(config, defaults):
    """Add capabilities without replacing any existing permissions or choices."""
    if not isinstance(config, dict) or not isinstance(config.get('sources'), list):
        raise ValueError('Configuración de noticias requiere sources: lista')
    result = deepcopy(config)
    existing = {source.get('id'): source for source in result['sources'] if isinstance(source, dict)}
    for source in defaults.get('sources', []):
        if source['id'] not in existing:
            added = deepcopy(source)
            added.update(enabled=False, terms_approved=False, download_articles=False, article_terms_approved=False)
            result['sources'].append(added)
    # An installed catalogue without a search section had the legacy search.
    # New-install quick defaults must not silently relabel its existing queue.
    result.setdefault('search', default_search_config())
    return result


def _phrase(value):
    if not isinstance(value, str):
        return ''
    return ' '.join(re.sub(r'["\\\x00-\x1f]', ' ', value).split())[:300]


_GENERIC_COMPANY_WORDS = STOP | GENERIC_CONTEXT | frozenset({
    'grupo', 'sociedad', 'corporacion', 'compania', 'holding', 'holdings',
}) | frozenset(token for suffix in LEGAL_SUFFIXES for token in suffix)


def _is_company(entity):
    return _fold(entity.get('kind', 'person')) in COMPANY_KINDS


def _distinctive_company_letters(value):
    # Same retrieval floor as RelevanceIndex: corporate aliases need four
    # non-generic letters. This does not validate their identity or conduct.
    return sum(char.isalpha() for token in _tokens(value)
               if token.value not in _GENERIC_COMPANY_WORDS for char in token.value)


def _company_core(value):
    tokens = _tokens(value)
    sequence = tuple(token.value for token in tokens)
    for suffix in LEGAL_SUFFIXES:
        if len(sequence) > len(suffix) and sequence[-len(suffix):] == suffix:
            core = value[:tokens[len(tokens) - len(suffix) - 1].end]
            # Do not turn generic legal labels such as Grupo S.A. into a
            # standalone broad query. Canonical short brands remain usable.
            return core if _distinctive_company_letters(core) >= 2 else value
    return value


def _uncontextual_corporate_alias(entity, value):
    return _is_company(entity) and _distinctive_company_letters(value) >= 4


def _variants(entity, aliases):
    name = _phrase(entity.get('name'))
    values = [name]
    if aliases:
        values.extend(entity.get('aliases') or [])
        context = entity.get('context') or {}
        if isinstance(context, dict):
            for key in ('legal_name', 'legal_names', 'trade_names', 'business_names', 'verified_aliases', 'abbreviations'):
                extra = context.get(key) or []
                if isinstance(extra, dict):
                    extra = [dict(value, name=key) if isinstance(value, dict) else key for key, value in extra.items()]
                values.extend(extra if isinstance(extra, list) else [extra])
    seen, result = set(), []
    for value in values:
        if not retrieval_alias_allowed(entity, value):
            continue
        if isinstance(value, dict):
            if value.get('validated') is False or value.get('verified') is False or value.get('status') in ('rejected', 'revoked'):
                continue
            value = value.get('name') or value.get('value') or value.get('alias')
        value = _phrase(value)
        # Exact corporate cores retrieve the spelling used by publishers;
        # preserve the full legal label as another query. Personal names and
        # personal aliases are never shortened by this normalization.
        forms = [_company_core(value), value] if _is_company(entity) else [value]
        for form in forms:
            if form and form.casefold() not in seen:
                seen.add(form.casefold())
                result.append(form)
    return result[:51]


def entity_queries(entity, config, max_chars=500):
    """Exact phrases, plus an optional contextual search; no keyword-only job."""
    variants = _variants(entity, config['include_aliases'])
    if not variants:
        return []
    context = entity.get('context') or {}
    hints = []
    if config['use_context'] and isinstance(context, dict):
        for key in ('state', 'municipality', 'city', 'role', 'position', 'cargo'):
            hint = _phrase(context.get(key))
            if hint and hint.casefold() not in {x.casefold() for x in hints}:
                hints.append(hint)
    hint_query = '(' + ' OR '.join('"' + x + '"' for x in hints[:3]) + ')' if hints else ''
    suffix = PRESETS[config['preset']]['terms']
    queries, group = [], []
    for variant in variants:
        # Short nicknames/acronyms need another literal clue to avoid flooding
        # the corpus with a generic word. The legal/full name remains broad.
        short_alias = (variant != variants[0] and len(variant.split()) == 1
                       and not _uncontextual_corporate_alias(entity, variant))
        if short_alias and not hint_query:
            continue
        phrase = '"' + variant + '"'
        if short_alias:
            phrase = '(' + phrase + ' ' + hint_query + ')'
        candidate = '(' + ' OR '.join([*group, phrase]) + ')'
        if len(candidate) > max_chars and group:
            queries.append({'query': '(' + ' OR '.join(group) + ')', 'query_type': 'identity'})
            group = []
        if len(phrase) + 2 <= max_chars:
            group.append(phrase)
    if group:
        queries.append({'query': '(' + ' OR '.join(group) + ')', 'query_type': 'identity'})
    # A profile refines discovery only after the nominal request. Requiring
    # adverse vocabulary in every request hides neutral coverage for people
    # whose public record simply uses their name.
    if suffix:
        queries.extend({'query': query['query'] + ' ' + suffix, 'query_type': 'focus'}
                       for query in list(queries) if len(query['query']) + len(suffix) + 1 <= max_chars)
    if hint_query:
        contextual = '"' + variants[0] + '" ' + hint_query + (' ' + suffix if suffix else '')
        if len(contextual) <= max_chars:
            queries.append({'query': contextual, 'query_type': 'context'})
    return queries


def _bound(value, end=False):
    if isinstance(value, str) and re.fullmatch(r'\d{14}', value):
        return datetime.strptime(value, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    parsed = date.fromisoformat(value)
    return datetime.combine(parsed + timedelta(days=1 if end else 0), time.min, timezone.utc)


def _personal_terminal_phrase(name):
    """A weaker retrieval lead, never a verified alias or a name correction.

    Spanish professional names often omit a second given name in reporting.
    Retain two terminal name words and their intervening/leading connectors;
    the original canonical name still governs identity validation downstream.
    Compound given names cannot be reliably parsed here, so this intentionally
    carries no assertion that the selected words are verified surnames.
    """
    name = _phrase(name)
    words = list(re.finditer(r'\S+', name))
    if any(not all(char.isalpha() or char in "-'’" for char in word.group())
           for word in words):
        return None
    connectors = {'de', 'del', 'la', 'las', 'los', 'el', 'y', 'e',
                  'da', 'das', 'do', 'dos', 'van', 'von'}
    meaningful = [index for index, word in enumerate(words)
                  if _fold(word.group()) not in connectors]
    if len(meaningful) < 3:
        return None
    final = [words[index].group() for index in meaningful[-2:]]
    if sum(char.isalpha() for word in final for char in word) < 6:
        return None
    start = meaningful[-2]
    while start > 1 and _fold(words[start - 1].group()) in connectors:
        start -= 1
    phrase = name[words[start].start():].strip()
    return phrase if phrase and _fold(phrase) != _fold(name) else None


def wordpress_queries(entity, config, *, variants=None, personal_fallbacks=False):
    variants = _variants(entity, config['include_aliases']) if variants is None else variants
    context = entity.get('context') or {}
    hints = [_phrase(context.get(key)) for key in ('state', 'municipality', 'role', 'position')
             if config['use_context'] and isinstance(context, dict) and _phrase(context.get(key))]
    terms = ('', *PUBLISHER_FOCUS_TERMS.get(config['preset'], ()))
    queries = []
    contextual = next((query for query in public_queries(entity, {'use_context': config['use_context']}, variants[:1])
                       if query['query_type'] == 'context'), None)
    for index, name in enumerate(variants):
        short = (index > 0 and len(name.split()) == 1
                 and not _uncontextual_corporate_alias(entity, name))
        if short and not hints:
            continue
        phrase = '"' + name + '"' + (' "' + hints[0] + '"' if short else '')
        for term in terms:
            queries.append({'query': phrase + (' ' + term if term else ''),
                            'query_type': ('focus' if term else 'identity') if index == 0 else 'alias'})
            if index == 0 and not term and contextual:
                hint = contextual['context_hint']
                text = phrase + ' ' + ' '.join('"' + _phrase(value) + '"' for value in contextual['context_hints'])
                if len(text) <= 500:
                    queries.append({'query': text, 'query_type': 'context', 'context_hint': hint})
    # Explicit source capability: existing saved profiles and other adapters
    # keep their original query budget. These are retrieval variants only;
    # never append them to entity.aliases or to the names used for attribution.
    if personal_fallbacks is True and not _is_company(entity) and variants:
        terminal = _personal_terminal_phrase(entity.get('name'))
        if terminal:
            given = _phrase(entity.get('name')).split()[0]
            # Two surnames alone fetched other people with the same family
            # names in real publisher probes. Require the first given name as
            # well; dropped middle names still become recoverable.
            query = '"' + given + '" "' + terminal + '"'
            if hints:
                query += ' "' + hints[0] + '"'
            if all(item['query'] != query for item in queries):
                queries.append({'query': query, 'query_type': 'retrieval_fallback',
                                'retrieval_variant': 'given_and_terminal_phrase',
                                'retrieval_origin': 'canonical_name',
                                'retrieval_given_name': given,
                                'retrieval_phrase': terminal,
                                'identity_confirmation': False})
    return queries


def _gdelt_batch_queries(entities, source, config, suffix, max_chars):
    """One flat OR of primary names/corporate brands; never nested ORs.

    Publisher WordPress search is deliberately excluded: it does not document
    the same Boolean semantics. Alias/context requests follow the first sweep.
    """
    terms = ('', *PUBLISHER_FOCUS_TERMS.get(config['preset'], ()))
    names = {}
    for entity in entities:
        variants = _variants(entity, config['include_aliases']) if _is_company(entity) else [_phrase(entity.get('name'))]
        for index, name in enumerate(variants):
            # Keep corporate brands in the first sweep instead of waiting for
            # all personal aliases. Longer legal spellings remain available in
            # separate secondary queries, without inflating the flat OR batch.
            if (not name or (index and (_company_core(name) != name
                                       or not _uncontextual_corporate_alias(entity, name)))):
                continue
            value = names.setdefault(name.casefold(), {'name': name, 'ids': {}})
            value['ids'][str(entity['id'])] = None
    limit = min(50, max(1, int(source.get('batch_size', 6))))
    for rank, term in enumerate(terms):
        tail = (' ' + term if term else '') + suffix
        def query(items):
            phrases = ['"' + item['name'] + '"' for item in items]
            return ('(' + ' OR '.join(phrases) + ')' if len(phrases) > 1 else phrases[0]) + tail
        batch = []
        def output(items):
            return {'query': query(items), 'query_type': 'focus' if term else 'identity', 'rank': rank,
                    'entity_ids': list(dict.fromkeys(identifier for item in items for identifier in item['ids'])),
                    'names': [item['name'] for item in items], 'batched': len(items) > 1}
        for item in names.values():
            if batch and (len(batch) >= limit or len(query([*batch, item])) > max_chars):
                yield output(batch)
                batch = []
            if len(query([item])) <= max_chars:
                batch.append(item)
        if batch:
            yield output(batch)


def split_query_windows(source, payload):
    """Calendar splits match the date filters supported by publisher RSS."""
    first, last = [_bound(payload[key]) for key in ('start', 'end')]
    if source.get('search_kind') != 'wordpress_rss' or (last - first).total_seconds() <= 86400:
        return []
    if first.month != (last - timedelta(seconds=1)).month or first.year != (last - timedelta(seconds=1)).year:
        windows = list(reversed(_windows(first, last, 'month')))
    else:
        windows = []
        cursor = first
        while cursor < last:
            finish = min(last, datetime.combine(cursor.date() + timedelta(days=1), time.min, timezone.utc))
            windows.append((cursor, finish))
            cursor = finish
    return [(a.strftime('%Y%m%d%H%M%S'), b.strftime('%Y%m%d%H%M%S')) for a, b in windows]


def _windows(first, last, unit):
    result = []
    while first < last:
        if unit == 'year':
            next_boundary = first.replace(year=first.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            year, month = (first.year + 1, 1) if first.month == 12 else (first.year, first.month + 1)
            next_boundary = first.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
        finish = min(last, next_boundary)
        result.append((first, finish))
        first = finish
    return list(reversed(result))


def _recent_floor(current, source):
    month_index = current.year * 12 + current.month - 1 - 3
    year, month0 = divmod(month_index, 12)
    calendar_floor = current.replace(year=year, month=month0 + 1,
                                     day=min(current.day, monthrange(year, month0 + 1)[1]))
    return max(calendar_floor, current - timedelta(days=min(90, max(1, int(source.get('max_history_days', 90))))))


def query_url(source, payload):
    if source.get('kind') in PUBLIC_SEARCH_KINDS:
        return public_search_url(payload['query'], source)
    from .shared_crawl import supports_wordpress_search, wordpress_search_url
    if supports_wordpress_search(source):
        return wordpress_search_url(source, payload['query'], payload['start'], payload['end'])
    if source.get('search_kind') == 'wordpress_rss':
        first, last = [_bound(payload[key]) for key in ('start', 'end')]
        args = {'s': payload['query'], 'feed': 'rss2', 'year': first.year}
        if first.year == (last - timedelta(seconds=1)).year and first.month == (last - timedelta(seconds=1)).month:
            args['monthnum'] = first.month
        if (last - first).total_seconds() <= 86400:
            args['day'] = first.day
        base = source.get('search_url') or source.get('base_url', '').rstrip('/') + '/'
        return base + ('&' if '?' in base else '?') + urlencode(args)
    if source.get('search_kind') == 'google_news_rss':
        first, last = [_bound(payload[key]) for key in ('start', 'end')]
        # Windows are half-open. Search dates are hints interpreted by Google;
        # returned publication dates remain source metadata, never invented.
        query = payload['query'] + f' after:{first.date().isoformat()} before:{last.date().isoformat()}'
        args = {'q': query, 'hl': source.get('language', 'es-419'),
                'gl': source.get('country', 'MX'), 'ceid': source.get('edition', 'MX:es-419')}
        return source.get('base_url', 'https://news.google.com/rss/search') + '?' + urlencode(args)
    args = {'query': payload['query'], 'mode': 'ArtList', 'format': 'json', 'sort': 'DateDesc',
            'maxrecords': min(250, max(1, int(source.get('max_records', 250))))}
    if payload.get('start') and payload.get('end'):
        args.update(startdatetime=payload['start'], enddatetime=payload['end'])
    else:
        args['timespan'] = source.get('timespan', '7d')
    return source.get('base_url', 'https://api.gdeltproject.org/api/v2/doc/doc') + '?' + urlencode(args)


class SearchPlanningStopped(Exception):
    """The caller stopped local planning before a complete plan was published."""


def _fair_primary_group(templates, entities, check):
    """Assign the first nominal sweep before spending on another provider.

    The assignment is made offline and never changes in response to a block.
    Batches and homonyms may cover several identities with the same request.
    Remaining provider/name combinations follow the first distinct-name sweep.
    """
    groups, lookups = [], []
    for source, windows, queries in templates:
        check()
        primary = [query for query in queries if query['rank'] == 0]
        if not primary or not windows:
            continue
        lookup = {}
        for index, query in enumerate(primary):
            check()
            for identifier in query['entity_ids']:
                lookup.setdefault(identifier, index)
        groups.append((source, windows, primary))
        lookups.append(lookup)
    used, covered = set(), set()
    assignment = 0
    for entity in entities:
        check()
        identifier = str(entity['id'])
        if identifier in covered:
            continue
        for offset in range(len(groups)):
            provider = (assignment + offset) % len(groups)
            index = lookups[provider].get(identifier)
            if index is None or (provider, index) in used:
                continue
            source, windows, primary = groups[provider]
            query = primary[index]
            used.add((provider, index))
            covered.update(query['entity_ids'])
            assignment += 1
            yield source, windows, query
            break
    # Retain every configured provider combination when the budget allows it;
    # interleave providers so a bounded preview cannot imply a fallback chain.
    for index in range(max((len(queries) for _, _, queries in groups), default=0)):
        for provider, (source, windows, queries) in enumerate(groups):
            check()
            if index < len(queries) and (provider, index) not in used:
                yield source, windows, queries[index]


def _fair_primary_queries(templates, entities, check):
    """Share the nominal first sweep across approved search providers.

    Provider assignments are fixed before any request. Public engines start
    the rotation, followed by directed publisher searches. Complete the first
    distinct-name sweep before spending on another provider/name combination;
    otherwise two public engines can exhaust a large bank's query budget and
    leave every useful publisher search unplanned. This is offline scheduling,
    never an endpoint change in response to a block.
    An approved publisher remains available for body acquisition independently
    from whether its historical search fits in this session's query budget.
    """
    ordered = [template for public in (True, False) for template in templates
               if (template[0].get('kind') in PUBLIC_SEARCH_KINDS) == public]
    yield from _fair_primary_group(ordered, entities, check)


def build_search_plan(entities, sources, config=None, *, start=None, end=None, now=None, should_stop=None):
    def check():
        if should_stop is not None and should_stop():
            raise SearchPlanningStopped()
    def checked(values):
        for value in values:
            check()
            yield value
    check()
    current = _utc(now)
    config = normalize_search_config(config, current)
    first, last = _bound(start or config['start']), _bound(end or config['end'], end=True)
    if first >= last:
        raise ValueError('La fecha inicial debe ser anterior a la final')
    active = [source for source in sources if source.get('enabled') and source.get('terms_approved')]
    quick = config['execution_mode'] == 'quick'
    plan = {'config': config, 'queries': [], 'warnings': [], 'sources': [], 'truncated': False,
            'requested_start': first.isoformat(), 'requested_end_exclusive': last.isoformat(),
            'coverage': 'best_effort_not_exhaustive', 'identity_policy': 'Queries retrieve candidates; identity must be corroborated from the source.',
            'entities_planned': 0, 'query_count': 0,
            'execution': {'mode': config['execution_mode'], 'session_seconds': config['session_seconds'],
                'scope_note': 'El presupuesto es global para la sesión. Se conservan consultas pendientes; no garantiza consultar todas las entidades ni descargar todos sus artículos.'},
            'entity_count_note': 'Entidades planificadas no equivale a entidades consultadas ni a cobertura comprobada.'}
    entities = list(checked(entities))
    shared_sources = []
    prepared = []
    public_providers = set()
    for source in checked(active):
        public = source.get('kind') in PUBLIC_SEARCH_KINDS
        if config['strategy'] == 'public' and not public:
            continue
        if public:
            if config['strategy'] not in ('public', 'targeted'):
                continue
            provider = ('duckduckgo' if source['kind'].startswith('duckduckgo_') else
                        'bing_news_rss' if source['kind'] == 'bing_news_rss' else source['id'])
            if provider in public_providers:
                plan['warnings'].append(source['id'] + ': se usa un solo formato por motor; no se alternan endpoints ni se eluden bloqueos.')
                continue
            public_providers.add(provider)
            prepared.append((source, [(first, last)]))
            plan['sources'].append({'id': source['id'], 'source_name': source.get('source_name') or source['id'],
                'scope': 'public_social_link_search' if source['kind'] == 'lemmy_search' else 'public_web_top_results',
                'historical': False, 'complete_archive': False, 'date_filter_supported': False,
                'start': first.isoformat(), 'end_exclusive': last.isoformat(), 'windows': 1,
                'window_unit': 'unfiltered_search', 'max_records_per_response': min(100, int(source.get('max_records', 50)))})
            if source['kind'] == 'arquivo_fulltext':
                plan['sources'][-1].update(scope='historical_archive_url_metadata', historical=True,
                    date_filter_supported=False, archive_capture_is_not_publication=True,
                    session_budget_fraction=.1, complementary=True)
                plan['warnings'].append(source['id'] + ': complemento histórico de metadatos, limitado al 10 % de la sesión. Se consulta después del barrido nominal; las fechas de captura no son fechas de publicación. Las consultas restantes se conservan para reanudar. No se descargan cuerpos archivados.')
            plan['warnings'].append(source['id'] + ': búsqueda pública nominal sin clave; no garantiza archivo, actualidad ni fechas de publicación. El rango se comprobará sólo cuando exista fecha editorial verificable. Títulos y extractos son descubrimiento, no evidencia puntuable.')
            if source['kind'] == 'bing_news_rss':
                plan['warnings'].append(source['id'] + ': el RSS de Bing limita el uso a un agregador RSS personal y no comercial; otros usos requieren permiso escrito de Microsoft. No habilite esta fuente sin revisar esa condición. Los enlaces explícitos se leen sin solicitar el redireccionador HTTP; cada editor conserva sus permisos separados.')
            if config['preset'] != 'identity':
                plan['warnings'].append(source['id'] + ': tras el primer barrido nominal se añaden dos consultas con palabras del perfil, sin OR ni filtros temporales inventados. Esos términos orientan la búsqueda, no acreditan hallazgos ni generan puntos; los resultados pueden ser neutrales o parciales.')
            continue
        google, gdelt = source.get('search_kind') == 'google_news_rss', source.get('kind') == 'gdelt'
        from .shared_crawl import supports_wordpress_search
        wordpress_api = supports_wordpress_search(source)
        if wordpress_api and config['strategy'] == 'targeted':
            # Old approved standard publisher endpoints support the same
            # search parameter. Compute the capability for this explicit plan
            # without rewriting their saved policy or any existing queue.
            limit = int(source.get('search_max_records', 10))
            source = dict(source, search_kind='wordpress_api_search',
                          search_max_records=min(10, limit) if quick else limit)
        wordpress = source.get('search_kind') == 'wordpress_rss' or wordpress_api
        if source.get('automated_search') is False:
            plan['warnings'].append(source['id'] + ': búsqueda automática omitida por la política robots verificada; candidato de consulta manual.')
            continue
        if (source.get('kind') in ('wordpress_api', 'sitemap') and config['strategy'] != 'feeds'
                and not (wordpress_api and config['strategy'] == 'targeted')):
            if config['strategy'] != 'shared':
                plan['warnings'].append(source['id'] + ': índice general omitido en búsqueda por nombre. Para recorrer su archivo elija explícitamente Índice compartido; habilite una fuente de búsqueda para consultar las entidades.')
                continue
            from .shared_crawl import seed_pages
            pages = seed_pages(source, config, first, last)
            if source.get('kind') == 'wordpress_api' and (last - timedelta(microseconds=1)).year - first.year + 1 > len(pages):
                plan['warnings'].append(source['id'] + ': el límite de páginas deja años antiguos sin planificar; se priorizan los más recientes. Acote el período o elija Ampliado.')
            plan['queries'].extend(pages)
            shared_sources.append(source)
            plan['sources'].append({'id': source['id'], 'source_name': source.get('source_name') or source['id'],
                'scope': 'shared_publisher_index', 'historical': True, 'complete_archive': False,
                'start': first.isoformat(), 'end_exclusive': last.isoformat(), 'windows': len(pages),
                'max_records_per_response': 100 if source.get('kind') == 'wordpress_api' else int(source.get('max_records', 50000))})
            continue
        if config['strategy'] == 'shared':
            continue
        if config['strategy'] == 'feeds':
            if source.get('kind') in ('rss', 'atom') and not google:
                urls = source.get('urls') or [source.get('url') or source.get('base_url')]
                for url in filter(None, urls):
                    plan['queries'].append({'source_id': source['id'], 'kind': 'news_feed', 'url': url, 'entity_ids': [], 'query_type': 'general_feed'})
                plan['sources'].append({'id': source['id'], 'source_name': source.get('source_name') or source['id'], 'scope': 'current_feed_only', 'historical': False})
            continue
        if not google and not gdelt and not wordpress:
            plan['warnings'].append(source['id'] + ': feed general omitido en búsqueda dirigida; use estrategia feeds para adquirirlo.')
            continue
        a, b = first, min(last, current) if gdelt else last
        if gdelt:
            earliest = _recent_floor(current, source)
            if a < earliest:
                plan['warnings'].append(source['id'] + ': GDELT DOC sólo cubre su ventana reciente de hasta 3 meses; el período anterior no se consulta.')
                a = earliest
        if a >= b:
            plan['warnings'].append(source['id'] + ': el período solicitado queda fuera del alcance temporal de la fuente.')
            continue
        # A quick discovery pass starts with annual windows even for a single
        # year. Creating all twelve monthly queries before seeing candidates
        # multiplies a large bank's initial queue without improving the first
        # result. Explicit monthly searches and extended defaults stay intact.
        unit = (config['window'] if config['window'] != 'auto' else
                'year' if (google or wordpress) and (quick or (b - a).days > 730) else 'month')
        # GDELT supports the recent interval directly. Monthly subdivisions are
        # a response-limit recovery strategy, not mandatory work before results.
        # Both execution modes start from one shared recent interval. Extended
        # mode expands only a saturated response instead of multiplying every
        # name by monthly windows before knowing whether any candidates exist.
        windows = [(a, b)] if gdelt or wordpress_api else _windows(a, b, unit)
        if gdelt:
            unit = 'recent_interval'
        elif wordpress_api:
            unit = 'publisher_search_interval'
        prepared.append((source, windows))
        plan['sources'].append({'id': source['id'], 'source_name': source.get('source_name') or source['id'], 'scope': 'publisher_historical_search' if wordpress else 'historical_search_best_effort' if google else 'recent_rolling_window',
            'start': a.isoformat(), 'end_exclusive': b.isoformat(), 'windows': len(windows), 'window_unit': unit,
            'max_records_per_response': min(100 if google else 250, int(source.get('search_max_records', source.get('max_records', 100 if google else 250)))),
            'historical': google or wordpress, 'complete_archive': False})
        if google:
            plan['warnings'].append(source['id'] + ': Google News RSS entrega un top limitado (hasta 100 observado), sin garantía de archivo completo; divida ventanas saturadas. Los enlaces del agregador pueden requerir apertura manual del editor.')
        if wordpress_api:
            plan['warnings'].append(source['id'] + ': búsqueda dirigida del editor por nombre dentro del período, con un top limitado por relevancia. Evita recorrer el índice completo; una respuesta limitada no garantiza revisar todo el histórico. Los extractos son candidatos y el cuerpo requiere su permiso separado.')
        elif wordpress:
            plan['warnings'].append(source['id'] + ': búsqueda del editor por frase y año/mes; RSS limitado y archivo no exhaustivo. Las fechas son filtros solicitados, no garantía de cobertura; se conservan las fechas reportadas.')
    planned_ids, total_ids, templates = set(), {str(entity['id']) for entity in entities}, []
    shared_required = len(plan['queries'])
    plan['queries'] = plan['queries'][:config['max_queries']]
    shared_initial = len(plan['queries'])
    if config['strategy'] in ('targeted', 'public') and prepared:
        entities = list(entities)
        total_ids = {str(entity['id']) for entity in entities}
        hosts = list(dict.fromkeys(urlsplit(source.get('search_url') or source.get('base_url') or source.get('url') or '').hostname or source['id']
                                   for source, _ in prepared))
        for source, windows in checked(prepared):
            grouped = {}
            source_suffix = ''
            if source.get('kind') == 'gdelt':
                if source.get('source_lang'):
                    source_suffix += ' sourcelang:' + source['source_lang']
                if source.get('source_country'):
                    source_suffix += ' sourcecountry:' + source['source_country']
            max_chars = min(1500, max(1, int(source.get('max_query_chars', 500))))
            host = urlsplit(source.get('search_url') or source.get('base_url') or source.get('url') or '').hostname or source['id']
            offset = hosts.index(host) * ((len(entities) + len(hosts) - 1) // len(hosts)) if quick else 0
            ordered = entities[offset:] + entities[:offset]
            batches = list(_gdelt_batch_queries(checked(ordered), source, config, source_suffix, max_chars)) if source.get('kind') == 'gdelt' else []
            included = {identifier for query in batches for identifier in query['entity_ids']}
            if source.get('kind') == 'gdelt' and any(_phrase(entity.get('name')) and str(entity['id']) not in included for entity in ordered):
                plan['warnings'].append(source['id'] + ': nombres o términos que exceden el límite local de longitud no se pudieron incluir en lotes.')
            for entity in checked(ordered):
                variants = _variants(entity, config['include_aliases'])
                if source.get('kind') == 'bing_news_rss':
                    # The captured public RSS queries use one quoted name,
                    # without invented archive/date or Boolean parameters.
                    queries = wordpress_queries(entity, config, variants=variants)
                elif source.get('kind') in PUBLIC_SEARCH_KINDS:
                    queries = public_queries(entity, config, variants)
                elif source.get('search_kind') in ('wordpress_rss', 'wordpress_api_search') or source.get('kind') == 'gdelt':
                    # GDELT rejects nested OR groups. Separate exact-phrase
                    # requests also keep short aliases bound to a context hint.
                    queries = wordpress_queries(entity, config, variants=variants,
                                                personal_fallbacks=(wordpress_api
                                                    and source.get('personal_name_fallbacks') is True))
                else:
                    queries = entity_queries(entity, config, max(1, max_chars - len(source_suffix)))
                for rank, query in enumerate(checked(queries)):
                    if source.get('kind') == 'arquivo_fulltext' and any(
                            other.get('kind') != 'arquivo_fulltext' for other, _ in prepared):
                        # A supplementary archive must not take a person's
                        # place in the primary nominal sweep of other sources.
                        rank += 10
                    if source.get('kind') == 'gdelt':
                        if query['query_type'] in ('identity', 'focus'):
                            continue  # Canonical requests were batched above.
                        rank += 100
                    text = query['query'] + source_suffix
                    if len(text) > max_chars:
                        plan['warnings'].append(source['id'] + ': una consulta excede el límite local de longitud y fue omitida; revise nombres/contexto o aumente el límite de la fuente.')
                        continue
                    template = grouped.setdefault(text, {**query, 'query': text, 'rank': rank, 'entity_ids': {}, 'names': {}})
                    template['rank'] = min(rank, template['rank'])
                    template['entity_ids'][str(entity['id'])] = None
                    template['names'].update(dict.fromkeys(variants))
            templates.append((source, windows, batches + [dict(query, entity_ids=list(query['entity_ids']), names=list(query['names']))
                                                          for query in grouped.values()]))
        required = shared_required + sum(len(windows) * len(queries) for _, windows, queries in templates)
        # Give every PEP a primary-name query before spending the budget on
        # aliases/context or advancing to the next historical window.
        max_windows = max((len(windows) for _, windows, _ in templates), default=0)
        max_rank = max((query['rank'] for _, _, queries in templates for query in queries), default=-1)
        for window_index in range(max_windows):
            check()
            if len(plan['queries']) >= config['max_queries']:
                break
            for rank in range(max_rank + 1):
                check()
                if len(plan['queries']) >= config['max_queries']:
                    break
                if window_index == 0 and rank == 0:
                    scheduled = _fair_primary_queries(templates, entities, check)
                else:
                    scheduled = ((source, windows, query) for source, windows, queries in templates
                                 if window_index < len(windows) for query in queries if query['rank'] == rank)
                for source, windows, query in scheduled:
                    check()
                    if len(plan['queries']) >= config['max_queries']:
                        break
                    a, b = windows[window_index]
                    payload = {key: value for key, value in query.items() if key != 'rank'}
                    payload.update(source_id=source['id'], kind='news_query', start=a.strftime('%Y%m%d%H%M%S'),
                                   end=b.strftime('%Y%m%d%H%M%S'), result_limit=int(source.get('search_max_records', source.get('max_records', 100 if source.get('search_kind') == 'google_news_rss' else 250))),
                                   split_policy='defer' if quick else 'expand')
                    payload['url'] = query_url(source, payload)
                    plan['queries'].append(payload)
                    planned_ids.update(payload['entity_ids'])
        plan['truncated'] = required > len(plan['queries'])
    else:
        required = shared_required
        plan['queries'] = plan['queries'][:config['max_queries']]
        plan['truncated'] = required > len(plan['queries'])
    if plan['truncated']:
        plan['warnings'].append('Presupuesto local de consultas alcanzado: existen entidades o ventanas sin planificar. Amplíe el presupuesto o reduzca el período.')
    if not prepared and not shared_sources and config['strategy'] in ('targeted', 'shared', 'public'):
        plan['warnings'].append('Habilite un buscador web público o una fuente con búsqueda del editor y registre sus permisos. Los índices compartidos requieren esa estrategia explícita; los feeds generales no buscan los nombres cargados.')
    if shared_sources:
        plan['shared_discovery'] = {'sources': len(shared_sources), 'initial_pages': shared_initial,
            'max_pages_per_source': 24 if quick else 240,
            'scope_note': 'Cada página se contrasta localmente con toda la lista. El archivo es parcial: se reparten las primeras páginas entre años antes de profundizar; las páginas pendientes y los metadatos se conservan.'}
        plan['warnings'].append('Índice compartido: títulos, resúmenes y URLs son candidatos; no acreditan identidad ni lectura de artículos. Un archivo parcial no equivale a consultar cada nombre en todo el medio.')
        if config['strategy'] == 'shared':
            plan['execution']['scope_note'] = 'El presupuesto es global para la sesión. Se conservan metadatos y páginas pendientes; contrástelos con la cobertura observada, que puede ser parcial.'
    plan.update(entities_planned=len(planned_ids), entities_covered=len(planned_ids), entities_total=len(total_ids),
                queries_required=required, query_count=len(plan['queries']))
    intervals = {source['id']: max(1, float(source.get('min_interval_seconds', 5))) for source in sources}
    hosts, source_entities, first_session, first_by_source = {}, {}, set(), {}
    last_start = 0.
    for query in checked(plan['queries']):
        host = urlsplit(query['url']).hostname or query['source_id']
        scheduled = hosts.get(host, 0.)
        last_start = max(last_start, scheduled)
        identifiers = query.get('entity_ids', [])
        source_entities.setdefault(query['source_id'], set()).update(identifiers)
        if scheduled < config['session_seconds']:
            first_session.update(identifiers)
            first_by_source.setdefault(query['source_id'], set()).update(identifiers)
        hosts[host] = scheduled + intervals[query['source_id']]
    for source in plan['sources']:
        source['entities_planned'] = len(source_entities.get(source['id'], ()))
        source['estimated_entities_first_session'] = len(first_by_source.get(source['id'], ()))
    plan['estimated_entities_first_session'] = len(first_session)
    plan['first_session_estimate_note'] = 'Límite orientativo de entidades distintas cuyas consultas podrían iniciarse según intervalos de host; excluye latencia, robots, fallos y descarga de cuerpos. No son consultas ejecutadas ni cobertura garantizada.'
    plan['estimated_schedule_seconds'] = round(last_start, 1)
    if quick and prepared:
        plan['warnings'].append('Modo rápido: primero nombres canónicos; los lotes sólo se aplican a GDELT. Las respuestas saturadas quedan como cobertura incompleta sin expansión automática; alias y contexto siguen al primer barrido.')
    plan['schedule_estimate_note'] = 'Programación mínima orientativa por intervalos de host; excluye latencia, robots, reintentos y divisiones adicionales. No es un SLA ni ETA de finalización.'
    plan['warnings'] = list(dict.fromkeys(plan['warnings']))
    check()
    return plan


def summarize_plan(plan, max_examples=10):
    """Jobs hold all queries; polling/receipts retain only a bounded preview."""
    result = {key: deepcopy(value) for key, value in plan.items() if key != 'queries'}
    result['queries'] = deepcopy(plan.get('queries', [])[:max_examples])
    result['queries_preview_truncated'] = len(plan.get('queries', [])) > max_examples
    return result
