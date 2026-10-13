"""Lossless seed transport without changing retained parser evidence."""


def seed_text_for_client(value):
    """Copy JSON structures, encoding integer seed fields as decimal text.

    Hashes continue to identify stored evidence, not this wire projection.
    Strings and raw text blocks are preserved verbatim. Other numeric fields
    keep their types; bool is deliberately not treated as an integer seed.
    """
    if isinstance(value, dict):
        return {
            key: str(item) if key in ('seed', 'noise_seed') and type(item) is int
            else seed_text_for_client(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [seed_text_for_client(item) for item in value]
    return value
