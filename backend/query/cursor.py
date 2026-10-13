"""Authenticated, bounded pagination cursors. Key provisioning is explicit."""
import base64
import hashlib
import hmac
import json
import re
import time


class CursorError(ValueError):
    code = 'invalid_cursor'
    status = 400


class CursorExpired(CursorError):
    code = 'cursor_expired'
    status = 409


def _encode(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _decode(value):
    if not value or not re.fullmatch('[A-Za-z0-9_-]+', value):
        raise CursorError('Malformed cursor encoding')
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except ValueError as error:
        raise CursorError('Malformed cursor encoding') from error
    if _encode(raw) != value:
        raise CursorError('Noncanonical cursor encoding')
    return raw


class CursorCodec:
    def __init__(self, key, *, lifetime=1800, clock=time.time):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError('Cursor key requires at least 32 random bytes')
        if type(lifetime) is not int or not 1 <= lifetime <= 86400:
            raise ValueError('Invalid cursor lifetime')
        self._key = key
        self.lifetime = lifetime
        self.clock = clock

    @staticmethod
    def _binding(query, generation):
        if not isinstance(generation, str) or not generation or len(generation) > 256:
            raise ValueError('Generation must be a bounded opaque identity')
        spec = query['spec']
        return {'queryHash': query['queryHash'], 'libraryId': spec['libraryId'],
                'rootIds': spec['rootIds'], 'sort': spec['sort'], 'generation': generation}

    @staticmethod
    def _position(position):
        if not isinstance(position, list) or len(position) != 2:
            raise CursorError('Invalid pagination position')
        # First key is the sort value (null means unknown); second is media UUID.
        if position[0] is not None and type(position[0]) not in (str, int):
            raise CursorError('Invalid sort key type')
        if isinstance(position[0], str) and len(position[0]) > 4096:
            raise CursorError('Sort key exceeds cursor budget')
        import uuid
        try:
            if not isinstance(position[1], str) or str(uuid.UUID(position[1])) != position[1]:
                raise ValueError('Invalid media UUID')
        except ValueError as error:
            raise CursorError('Invalid media UUID') from error
        return position

    def issue(self, query, generation, position):
        now = int(self.clock())
        payload = {'version': 1, **self._binding(query, generation),
                   'issuedAt': now, 'expiresAt': now + self.lifetime,
                   'position': self._position(position)}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
        token = _encode(raw) + '.' + _encode(hmac.digest(self._key, raw, 'sha256'))
        if len(token) > 8192:
            raise CursorError('Cursor exceeds budget')
        return token

    def read(self, token, query, generation):
        if not isinstance(token, str) or not 1 <= len(token) <= 8192 or token.count('.') != 1:
            raise CursorError('Malformed cursor')
        body, signature = token.split('.')
        raw, signature = _decode(body), _decode(signature)
        if not hmac.compare_digest(signature, hmac.digest(self._key, raw, 'sha256')):
            raise CursorError('Invalid cursor signature')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise CursorError('Invalid cursor payload') from error
        binding = self._binding(query, generation)
        expected_keys = {'version', 'issuedAt', 'expiresAt', 'position', *binding}
        if not isinstance(payload, dict) or set(payload) != expected_keys or payload['version'] != 1:
            raise CursorError('Unsupported cursor payload')
        if any(payload[key] != value for key, value in binding.items()):
            raise CursorExpired('Query scope or index generation changed')
        issued, expires = payload['issuedAt'], payload['expiresAt']
        if type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 86400:
            raise CursorError('Invalid cursor lifetime')
        now = int(self.clock())
        if now >= expires or issued > now + 30:
            raise CursorExpired('Cursor expired or clock changed')
        return self._position(payload['position'])
