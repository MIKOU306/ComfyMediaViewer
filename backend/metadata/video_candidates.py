"""Select generation candidates without merging duplicate container tags."""
from collections.abc import Mapping


def generation_key(record):
    key = record.get('key')
    if not isinstance(key, str):
        return None
    source = record.get('source')
    if isinstance(source, Mapping):
        name, mean = source.get('freeform_name'), source.get('freeform_mean')
        if isinstance(name, str) and isinstance(mean, str) and key == f'----:{mean}:{name}':
            key = name
    normalized = key.casefold()
    return normalized if normalized in ('prompt', 'workflow', 'parameters', 'comment', '©cmt') else None


def generation_candidates(snapshot):
    """Return ordered key/text/source triples from retained container material.

    Parsing and ambiguity resolution belong to the publishing pipeline. Unknown
    or invalid candidates remain in the raw archive and are never coerced.
    """
    result = snapshot.get('result', {})
    candidates = []
    for record in result.get('text_candidates', []):
        if not isinstance(record, Mapping):
            continue
        key = record.get('key')
        text = record.get('text')
        source = record.get('source')
        if (not isinstance(key, str) or not isinstance(text, str)
                or not isinstance(source, Mapping) or not source
                or record.get('status') != 'ok' or record.get('capture_error')):
            continue
        normalized = generation_key(record)
        if normalized:
            candidates.append({'key': normalized, 'text': text, 'source': dict(source)})
    return candidates
