"""Bridge existing filesystem listing to explicit catalog identities."""
from pathlib import Path
import sqlite3

from .catalog import CatalogRepository, IdentityConflictError
from .database import new_identity
from .asset_lifecycle import visible_clause


class ServerCatalog:
    def __init__(self, database, library_uuid):
        self.database = Path(database).absolute()
        self.library_uuid = library_uuid
        self.repository = CatalogRepository(self.database)

    def registered_root_paths(self):
        """本库已登记的全部根目录绝对路径。

        供「跨根媒体读取」判定使用（server.py:_inside_registered_root）：
        只认本库登记过的根，不接受调用方传入任意目录，因此无法被用来放行
        未登记位置的磁盘路径。按路径排序，让判定顺序稳定可复现。
        """
        connection = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                'SELECT r.path FROM library_root r JOIN library l ON l.id=r.library_id '
                'WHERE l.uuid=? ORDER BY r.path', (self.library_uuid,)).fetchall()
        finally:
            connection.close()
        return [row['path'] for row in rows]

    def ensure_root(self, root_path):
        """Register a navigable root even when it contains no media files."""
        root_path = str(Path(root_path).resolve())
        connection = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            connection.execute('BEGIN IMMEDIATE')
            library = connection.execute('SELECT id FROM library WHERE uuid=?', (self.library_uuid,)).fetchone()
            if library is None:
                raise IdentityConflictError('Library is missing')
            root = connection.execute('SELECT uuid FROM library_root WHERE library_id=? AND path=?',
                                      (library['id'], root_path)).fetchone()
            root_uuid = root['uuid'] if root else new_identity()
            if root is None:
                connection.execute('INSERT INTO library_root(uuid,library_id,path,path_key,availability) VALUES (?,?,?,?,?)',
                                   (root_uuid, library['id'], root_path, root_path, 'online'))
            connection.commit()
            return root_uuid
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def observe(self, root_path, path, file_stat):
        return self.observe_many(root_path, [(path, file_stat)])[0]

    def observe_many(self, root_path, files, *, batch_size=128):
        """Observe filesystem-listing results in bounded transactions.

        Legacy ``/api/list`` can pass each ``DirEntry`` path and stat together,
        avoiding a catalog connection, schema check, and commit per card. The
        returned records preserve input order and existing root ownership.
        """
        root_path = str(Path(root_path).resolve())
        entries = [(str(Path(path).absolute()), file_stat) for path, file_stat in files]
        if not entries:
            return []
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 512:
            raise ValueError('batch_size must be between 1 and 512')
        results = []
        for offset in range(0, len(entries), batch_size):
            batch = entries[offset:offset + batch_size]
            connection = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
            try:
                connection.execute('BEGIN IMMEDIATE')
                library = connection.execute('SELECT id FROM library WHERE uuid=?', (self.library_uuid,)).fetchone()
                if library is None:
                    raise IdentityConflictError('Library is missing')
                roots = {}
                for path, file_stat in batch:
                    existing = connection.execute(
                        'SELECT DISTINCT r.uuid FROM media_asset a JOIN library_root r ON r.id=a.root_id '
                        'WHERE a.library_id=? AND a.path=? AND ' + visible_clause(connection),
                        (library['id'], path)).fetchall()
                    if len(existing) > 1:
                        raise IdentityConflictError('Path has multiple catalog identities')
                    if existing:
                        root_uuid = existing[0]['uuid']
                    else:
                        root = connection.execute(
                            'SELECT uuid FROM library_root WHERE library_id=? AND path=?',
                            (library['id'], root_path)).fetchone()
                        if root is None:
                            root_uuid = new_identity()
                            connection.execute(
                                'INSERT INTO library_root(uuid,library_id,path,path_key,availability) '
                                'VALUES (?,?,?,?,?)',
                                (root_uuid, library['id'], root_path, root_path, 'online'))
                        else:
                            root_uuid = root['uuid']
                    scoped = roots.get(root_uuid)
                    if scoped is None:
                        _, scoped = self.repository._scope(connection, self.library_uuid, root_uuid)
                        roots[root_uuid] = scoped
                    results.append(self.repository._observe_file_in_transaction(
                        connection, library, scoped, path, file_stat))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return results

    def verify(self, root_path, path):
        observed = self.observe(root_path, path, Path(path).stat())
        return self.repository.strict_verify(self.library_uuid, observed['rootId'], str(Path(path).absolute()),
                                             media_uuid=observed['mediaId'])

    def current(self, path):
        connection = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)
        try:
            roots = connection.execute(
                'SELECT r.uuid FROM media_asset a JOIN library l ON l.id=a.library_id '
                'JOIN library_root r ON r.id=a.root_id WHERE l.uuid=? AND a.path=? AND ' + visible_clause(connection),
                (self.library_uuid, str(Path(path).absolute()))).fetchall()
        finally:
            connection.close()
        if not roots:
            return None
        if len(roots) != 1:
            raise IdentityConflictError('Path has multiple catalog identities')
        return self.repository.resolve_asset(self.library_uuid, roots[0][0], path=str(Path(path).absolute()))
