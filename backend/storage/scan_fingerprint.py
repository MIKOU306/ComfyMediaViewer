"""A fingerprint is authoritative only with its completed scan epoch."""
import hashlib

SQL = (
    'CREATE TABLE catalog_scan_fingerprint_schema(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)',
    'CREATE TABLE catalog_scan_fingerprint(root_id INTEGER PRIMARY KEY REFERENCES library_root(id),'
    'epoch TEXT NOT NULL,signature TEXT NOT NULL)',
)
CHECKSUM = hashlib.sha256(('1\n'+'\n'.join(SQL)).encode()).hexdigest()


def initialize(db, *, create=True):
    found = db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_scan_fingerprint_schema'").fetchone()
    if not found:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_scan_fingerprint'").fetchone():
            raise RuntimeError('Unversioned scan fingerprint schema')
        if not create:
            return False
        for sql in SQL:
            db.execute(sql)
        db.execute('INSERT INTO catalog_scan_fingerprint_schema VALUES(1,?)',(CHECKSUM,))
    rows = db.execute('SELECT version,checksum FROM catalog_scan_fingerprint_schema ORDER BY version').fetchall()
    if [tuple(row) for row in rows] != [(1,CHECKSUM)]:
        raise RuntimeError('Scan fingerprint version/checksum mismatch')
    for sql in SQL:
        name=sql.split('(')[0].split()[-1]
        row=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None or row[0]!=sql:
            raise RuntimeError('Scan fingerprint schema missing or changed')
    return True
