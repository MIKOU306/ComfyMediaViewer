"""Strict normalization of the v2 query contract; no database access."""
import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone


class QuerySpecError(ValueError):
    pass


def _object(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise QuerySpecError('Invalid object or unknown fields')
    return value


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\0' in value:
        raise QuerySpecError('Invalid text')
    return value


def _uuid(value):
    if not isinstance(value, str):
        raise QuerySpecError('UUID must be text')
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise QuerySpecError('Invalid UUID') from error


def _array(value, maximum, transform):
    if not isinstance(value, list) or len(value) > maximum:
        raise QuerySpecError('Invalid array or budget exceeded')
    return sorted(set(transform(item) for item in value))


def _enum(value, values):
    if not isinstance(value, str) or value not in values:
        raise QuerySpecError('Unsupported enum value')
    return value


def _date(value):
    value = _text(value, 64)
    if not re.match(r'^\d{4}-\d{2}-\d{2}T', value):
        raise QuerySpecError('Date requires ISO8601 time and timezone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError('Timezone required')
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, OverflowError) as error:
        raise QuerySpecError('Invalid timezone-aware date') from error


def normalize_query(request):
    request = _object(request, ('libraryId', 'rootIds', 'directory', 'recursive',
                                'filters', 'sort', 'limit', 'cursor'))
    library = _uuid(request.get('libraryId'))
    roots = _array(request.get('rootIds', []), 100, _uuid)
    directory = request.get('directory')
    if directory is not None:
        directory = _text(directory, 4096)
        if len(roots) != 1 or '\\' in directory or ':' in directory or any(
                segment in ('', '.', '..') for segment in directory.split('/')):
            raise QuerySpecError('Directory must be relative to exactly one root')
    recursive = request.get('recursive', True)
    if type(recursive) is not bool:
        raise QuerySpecError('recursive must be boolean')
    filters = _object(request.get('filters', {}), ('workflowClasses', 'workflowKind', 'workflowFamilyUuids', 'recentOnly', 'favorite', 'favoriteCategory', 'markState', 'tags', 'tagTerms', 'excludedTags', 'tagMode', 'tagHierarchy',
                      'types', 'formats', 'date', 'text', 'nameTerms', 'prompt', 'models', 'modelTerms', 'loras', 'loraTerms', 'samplers', 'samplerTerms', 'dimensions', 'safety'))
    safety = _enum(filters.get('safety', 'all'), ('all', 'hide_nsfw'))
    workflow_classes = _array(filters.get('workflowClasses', []), 4,
                              lambda value: _enum(value, ('full', 'prompt', 'none', 'unknown')))
    if 'workflowClasses' in filters and not workflow_classes:
        raise QuerySpecError('workflowClasses must not be empty')
    workflow_kind = filters.get('workflowKind')
    if workflow_kind is not None:
        workflow_kind = _enum(workflow_kind, ('t2i', 'i2i', 'cn', 'other', 'unknown'))
    workflow_families = _array(filters.get('workflowFamilyUuids', []), 100, _uuid)
    if 'workflowFamilyUuids' in filters and not workflow_families:
        raise QuerySpecError('workflowFamilyUuids must not be empty')
    recent_only = filters.get('recentOnly', False)
    if type(recent_only) is not bool:
        raise QuerySpecError('recentOnly must be boolean')
    mark_state = filters.get('markState')
    if mark_state is not None:
        mark_state = _text(mark_state, 256)
    favorite = filters.get('favorite')
    if favorite is not None and type(favorite) is not bool:
        raise QuerySpecError('favorite must be boolean or null')
    favorite_category = filters.get('favoriteCategory')
    if favorite_category is not None:
        favorite_category = _text(favorite_category, 512)
        if favorite is False:
            raise QuerySpecError('favoriteCategory conflicts with favorite=false')
        favorite = True
    if 'tagMode' in filters and 'tags' not in filters:
        raise QuerySpecError('tagMode requires tags')
    tags = _array(filters.get('tags', []), 50, lambda v: _text(v, 256))
    excluded_tags = _array(filters.get('excludedTags', []), 50, lambda v: _text(v, 256))
    tag_terms = _array(filters.get('tagTerms', []), 50,
                      lambda v: unicodedata.normalize('NFC', _text(v, 256)).casefold())
    tag_mode = _enum(filters.get('tagMode', 'all'), ('all', 'any'))
    tag_hierarchy = filters.get('tagHierarchy', False)
    if type(tag_hierarchy) is not bool:
        raise QuerySpecError('tagHierarchy must be boolean')
    types = _array(filters.get('types', []), 2, lambda v: _enum(v, ('image', 'video')))
    def extension(value):
        value = _text(value, 16)
        if not re.fullmatch('[a-z0-9]+', value):
            raise QuerySpecError('Invalid lowercase extension')
        return value
    formats = _array(filters.get('formats', []), 100, extension)
    date = filters.get('date')
    if date is not None:
        date = _object(date, ('field', 'after', 'before', 'beforeInclusive'))
        inclusive = date.get('beforeInclusive', False)
        if type(inclusive) is not bool or ('beforeInclusive' in date and 'before' not in date):
            raise QuerySpecError('beforeInclusive requires a before boundary and boolean value')
        _enum(date.get('field', 'mtime'), ('mtime',))
        if not any(key in date for key in ('after', 'before')):
            raise QuerySpecError('Date requires a boundary')
        date = {'field': 'mtime', 'after': _date(date['after']) if 'after' in date else None,
                'before': _date(date['before']) if 'before' in date else None,
                'beforeInclusive': inclusive}
        if date['after'] and date['before'] and (date['after'] > date['before'] or
                (date['after'] == date['before'] and not inclusive)):
            raise QuerySpecError('Date boundaries are reversed')
    text = filters.get('text')
    name_terms = _array(filters.get('nameTerms', []), 20,
                       lambda v: unicodedata.normalize('NFC', _text(v, 2048)).casefold())
    if text is not None:
        text = unicodedata.normalize('NFC', _text(text, 2048)).casefold()
    prompt = filters.get('prompt')
    if prompt is not None:
        prompt = _object(prompt, ('text', 'side', 'mode'))
        prompt = {'text': unicodedata.normalize('NFC', _text(prompt.get('text'), 2048)).casefold(),
                  'side': _enum(prompt.get('side', 'any'), ('positive', 'negative', 'any')),
                  'mode': _enum(prompt.get('mode', 'literal'), ('literal',))}
    models = _array(filters.get('models', []), 50, lambda v: _text(v, 512))
    model_terms = _array(filters.get('modelTerms', []), 20,
                        lambda v: unicodedata.normalize('NFC', _text(v, 512)).casefold())
    loras = _array(filters.get('loras', []), 50, lambda v: _text(v, 512))
    lora_terms = _array(filters.get('loraTerms', []), 20,
                       lambda v: unicodedata.normalize('NFC', _text(v, 512)).casefold())
    samplers = _array(filters.get('samplers', []), 50, lambda v: _text(v, 256))
    sampler_terms = _array(filters.get('samplerTerms', []), 20,
                          lambda v: unicodedata.normalize('NFC', _text(v, 256)).casefold())
    dimensions = filters.get('dimensions')
    if dimensions is not None:
        dimensions = _object(dimensions, ('width', 'height'))
        if set(dimensions) != {'width', 'height'} or any(type(value) is not int or not 1 <= value <= 2147483647
                                                        for value in dimensions.values()):
            raise QuerySpecError('Dimensions require positive integer width and height')
    sort = _enum(request.get('sort', 'mtime_desc'), ('mtime_desc', 'mtime_asc',
                 'name_asc', 'name_desc', 'size_desc', 'size_asc', 'recent_desc'))
    if sort == 'recent_desc' and not recent_only:
        raise QuerySpecError('recent_desc requires recentOnly')
    limit = request.get('limit', 60)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise QuerySpecError('limit must be integer in 1..200')
    cursor = request.get('cursor')
    if cursor is not None:
        cursor = _text(cursor, 8192)
    normalized = {'libraryId': library, 'rootIds': roots, 'directory': directory,
                  'recursive': recursive, 'sort': sort, 'limit': limit,
                  'filters': {'workflowClasses': workflow_classes, 'workflowKind': workflow_kind,
                              'workflowFamilyUuids': workflow_families,
                              'recentOnly': recent_only, 'favorite': favorite, 'favoriteCategory': favorite_category, 'markState': mark_state, 'tags': tags, 'tagTerms': tag_terms, 'excludedTags': excluded_tags,
                              'tagMode': tag_mode if tags else 'all', 'tagHierarchy': tag_hierarchy, 'types': types,
                              'formats': formats, 'date': date, 'text': text, 'nameTerms': name_terms,
                              'prompt': prompt, 'models': models, 'modelTerms': model_terms, 'loras': loras, 'loraTerms': lora_terms,
                              'samplers': samplers, 'samplerTerms': sampler_terms, 'dimensions': dimensions,
                              'safety': safety}}
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'spec': normalized, 'cursor': cursor,
            'queryHash': hashlib.sha256(canonical.encode('utf-8')).hexdigest()}
