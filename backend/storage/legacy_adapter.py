"""Path compatibility for UUID-owned organization state, without JSON writes."""
from pathlib import Path
import sqlite3
import copy
import threading

from .organization import OrganizationRepository, NotFoundError, ScopeMismatchError
from .asset_lifecycle import visible_clause


class IdentityConflictError(RuntimeError):
    pass


class LegacyOrganizationAdapter:
    def __init__(self, database, library_uuid, profile_uuid):
        self.database = Path(database).absolute()
        self.library_uuid = library_uuid
        self.profile_uuid = profile_uuid
        self.repository = OrganizationRepository(self.database)
        self._projection_lock = threading.RLock()
        self._projection_cache = None

    def resolve(self, *, path=None, media_id=None):
        """Resolve within one Library, refusing ambiguous paths and mixed identities.

        This does not create identities. Catalog observation must happen before
        an organization write to a previously unknown file.
        """
        if not path and not media_id:
            raise NotFoundError('A media identity or path is required')
        connection = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            owner = connection.execute(
                'SELECT l.id FROM library l JOIN local_profile p ON p.library_id=l.id '
                'WHERE l.uuid=? AND p.uuid=?', (self.library_uuid, self.profile_uuid)).fetchone()
            if owner is None:
                raise ScopeMismatchError('Profile does not belong to this library')
            if media_id:
                rows = connection.execute('SELECT uuid,path,library_id FROM media_asset WHERE uuid=?',
                                          (media_id,)).fetchall()
                if rows and rows[0]['library_id'] != owner['id']:
                    raise ScopeMismatchError('Media belongs to another library')
                if rows and path is not None and rows[0]['path'] != path:
                    raise IdentityConflictError('mediaId and path identify different files')
            else:
                rows = connection.execute(
                    'SELECT uuid,path,library_id FROM media_asset a WHERE library_id=? AND path=? AND ' + visible_clause(connection),
                    (owner['id'], path)).fetchall()
            if not rows:
                raise NotFoundError('Media identity is not in the catalog')
            if len(rows) != 1:
                raise IdentityConflictError('Path is occupied by multiple historical identities; use mediaId')
            return rows[0]['uuid']
        finally:
            connection.close()

    def projection(self):
        connection = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)
        try:
            owner = connection.execute(
                'SELECT 1 FROM library l JOIN local_profile p ON p.library_id=l.id '
                'WHERE l.uuid=? AND p.uuid=?', (self.library_uuid, self.profile_uuid)).fetchone()
            if owner is None:
                raise ScopeMismatchError('Profile does not belong to this library')
        finally:
            connection.close()
        from backend.query.index_state import QueryIndexState
        state = QueryIndexState(self.database)
        with self._projection_lock:
            with state._connection() as db:
                generation = state.read_in_transaction(db)['indexGeneration']
            if self._projection_cache and self._projection_cache[0] == generation:
                return copy.deepcopy(self._projection_cache[1])
            result = self.repository.read_projection(self.profile_uuid)
            with state._connection() as db:
                after = state.read_in_transaction(db)['indexGeneration']
            if generation == after:
                self._projection_cache = (generation, copy.deepcopy(result))
            return result

    def set_favorite(self, *, path=None, media_id=None, favorite, seq, category=None):
        identity = self.resolve(path=path, media_id=media_id)
        return self.repository.set_favorite(self.profile_uuid, identity, favorite=favorite,
                                            seq=seq, category=category)

    def set_tags(self, *, path=None, media_id=None, tags):
        identity = self.resolve(path=path, media_id=media_id)
        return self.repository.set_tags(self.profile_uuid, identity, tags)

    def set_mark(self, *, path=None, media_id=None, state, updated=None):
        identity = self.resolve(path=path, media_id=media_id)
        return self.repository.set_mark(self.profile_uuid, identity, state=state, updated=updated)
