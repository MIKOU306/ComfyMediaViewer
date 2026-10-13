"""Idempotent catalog half of a journaled file operation."""
from pathlib import Path
import sqlite3

from ..storage.catalog import IdentityConflictError


class CatalogOperationCommit:
    def __init__(self, database):
        self.database = Path(database).absolute()

    def __call__(self, operation):
        connection = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            connection.execute('BEGIN IMMEDIATE')
            from ..storage.asset_lifecycle import validate
            validate(connection, create=True)
            asset = connection.execute(
                'SELECT a.*,r.uuid AS revision_uuid FROM media_asset a '
                'JOIN library l ON l.id=a.library_id '
                'LEFT JOIN media_revision r ON r.id=a.current_revision_id '
                'WHERE a.uuid=? AND l.uuid=?',
                (operation['media_uuid'], operation['library_uuid'])).fetchone()
            if asset is None or asset['revision_uuid'] != operation['revision_uuid']:
                raise IdentityConflictError('Operation does not match the current asset/revision')
            source, destination = operation['source_path'], operation['destination_path']
            action = operation['action']
            if action == 'trash':
                if asset['path'] != source:
                    raise IdentityConflictError('Asset moved after the operation was planned')
                # The original catalog location and all organization records
                # remain available while the bytes reside in trash.
                connection.execute("UPDATE media_asset SET availability='missing',live_path_key=NULL WHERE id=?",
                                   (asset['id'],))
                connection.execute('INSERT INTO asset_lifecycle VALUES(?,?,?,?) '
                                   'ON CONFLICT(asset_id) DO UPDATE SET revision_uuid=excluded.revision_uuid,operation_uuid=excluded.operation_uuid',
                                   (asset['library_id'], asset['id'], operation['revision_uuid'], operation['operation_uuid']))
            elif action in ('restore', 'move'):
                if action == 'restore' and asset['availability'] != 'missing' and asset['path'] != destination:
                    raise IdentityConflictError('Restore would relocate an already available asset')
                if action == 'move' and asset['path'] not in (source, destination):
                    raise IdentityConflictError('Asset location conflicts with the operation')
                roots = connection.execute('SELECT id,path FROM library_root WHERE library_id=?',
                                           (asset['library_id'],)).fetchall()
                candidates = []
                target = Path(destination).absolute()
                for root in roots:
                    try:
                        target.relative_to(Path(root['path']).absolute())
                    except ValueError:
                        continue
                    candidates.append(root)
                if not candidates:
                    raise IdentityConflictError('Destination is outside registered library roots')
                root = max(candidates, key=lambda row: len(row['path']))
                connection.execute(
                    "UPDATE media_asset SET path=?,live_path_key=?,root_id=?,availability='present' WHERE id=?",
                    (destination, destination, root['id'], asset['id']))
                connection.execute('DELETE FROM asset_lifecycle WHERE asset_id=?', (asset['id'],))
            else:
                raise IdentityConflictError('Unsupported file operation action')
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
