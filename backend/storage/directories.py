"""Retained directory observations, published with the owning scan epoch."""
import hashlib
import os

SQL = (
    'CREATE TABLE catalog_directory_schema(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)',
    'CREATE TABLE catalog_directory(root_id INTEGER NOT NULL REFERENCES library_root(id),'
    'path_key TEXT NOT NULL,relative_path TEXT NOT NULL,parent_key TEXT NOT NULL,name TEXT NOT NULL,'
    "availability TEXT NOT NULL CHECK(availability IN ('present','missing')),seen_epoch TEXT NOT NULL,"
    'PRIMARY KEY(root_id,path_key))',
    'CREATE INDEX catalog_directory_parent ON catalog_directory(root_id,parent_key)',
)
CHECKSUM = hashlib.sha256(('1\n' + '\n'.join(SQL)).encode()).hexdigest()


def initialize(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_directory_schema'").fetchone():
        rows = db.execute('SELECT version,checksum FROM catalog_directory_schema').fetchall()
        if len(rows) != 1 or tuple(rows[0]) != (1, CHECKSUM):
            raise ValueError('Directory schema checksum/version mismatch')
        for name, statement in zip(('catalog_directory_schema', 'catalog_directory', 'catalog_directory_parent'), SQL):
            row = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
            if row is None or row[0] != statement:
                raise ValueError('Directory schema is incomplete or changed')
        return
    if db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('catalog_directory','catalog_directory_parent')").fetchone():
        raise ValueError('Unversioned directory schema')
    for statement in SQL:
        db.execute(statement)
    db.execute('INSERT INTO catalog_directory_schema VALUES(1,?)', (CHECKSUM,))


def observe(db, root, epoch, path):
    relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root['path'])).replace('\\', '/')
    if relative == '.':
        return
    if any(part in ('', '.', '..') for part in relative.split('/')):
        raise ValueError('Directory lies outside scan root')
    parts = relative.split('/')
    parent = '/'.join(parts[:-1])
    key = relative.casefold() if root['case_sensitive'] == 0 else relative
    parent_key = parent.casefold() if root['case_sensitive'] == 0 else parent
    db.execute('INSERT INTO catalog_directory(root_id,path_key,relative_path,parent_key,name,availability,seen_epoch) '
               "VALUES(?,?,?,?,?,'present',?) ON CONFLICT(root_id,path_key) DO UPDATE SET "
               "relative_path=excluded.relative_path,parent_key=excluded.parent_key,name=excluded.name,"
               "availability='present',seen_epoch=excluded.seen_epoch",
               (root['id'], key, relative, parent_key, parts[-1], epoch))


def finish(db, root_id, epoch):
    db.execute("UPDATE catalog_directory SET availability='missing' WHERE root_id=? AND seen_epoch!=?",
               (root_id, epoch))
