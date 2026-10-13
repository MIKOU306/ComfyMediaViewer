"""Execution identity, immutable relation evidence and versioned human decisions.

Parse runs are never used as execution identities. This extension is initialized
explicitly and is not yet exposed through the application API.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import uuid

from ..storage.database import connect, initialize_schema


SQL = """
CREATE TABLE creation_relation_schema(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL);
CREATE TABLE creation_execution(
 id INTEGER PRIMARY KEY,uuid TEXT NOT NULL UNIQUE,library_id INTEGER NOT NULL,
 namespace TEXT NOT NULL,source_key TEXT NOT NULL,evidence_json TEXT NOT NULL,
 identity_state TEXT NOT NULL CHECK(identity_state IN ('execution_unknown','evidenced')),
 UNIQUE(library_id,id),UNIQUE(library_id,namespace,source_key),
 FOREIGN KEY(library_id) REFERENCES library(id));
CREATE TABLE creation_execution_revision(
 library_id INTEGER NOT NULL,execution_id INTEGER NOT NULL,direction TEXT NOT NULL
 CHECK(direction IN ('input','output')),asset_id INTEGER NOT NULL,revision_id INTEGER NOT NULL,role TEXT NOT NULL,
 PRIMARY KEY(execution_id,direction,asset_id,revision_id,role),
 FOREIGN KEY(library_id,execution_id) REFERENCES creation_execution(library_id,id),
 FOREIGN KEY(library_id,asset_id,revision_id) REFERENCES media_revision(library_id,asset_id,id));
CREATE TABLE creation_relation(
 id INTEGER PRIMARY KEY,uuid TEXT NOT NULL UNIQUE,library_id INTEGER NOT NULL,
 source_id INTEGER NOT NULL,target_id INTEGER NOT NULL,domain TEXT NOT NULL,
 kind TEXT NOT NULL,namespace TEXT NOT NULL,source_key TEXT NOT NULL,
 evidence_state TEXT NOT NULL CHECK(evidence_state IN ('evidenced','inferred')),
 evidence_json TEXT NOT NULL,confidence REAL CHECK(confidence BETWEEN 0 AND 1),
 UNIQUE(library_id,id),UNIQUE(library_id,namespace,source_key),
 FOREIGN KEY(library_id,source_id) REFERENCES creation_execution(library_id,id),
 FOREIGN KEY(library_id,target_id) REFERENCES creation_execution(library_id,id));
CREATE TABLE creation_decision(
 relation_id INTEGER NOT NULL,library_id INTEGER NOT NULL,profile_id INTEGER NOT NULL,
 version INTEGER NOT NULL CHECK(version>0),state TEXT NOT NULL CHECK(state IN ('confirmed','revoked')),
 PRIMARY KEY(profile_id,relation_id),
 FOREIGN KEY(library_id,relation_id) REFERENCES creation_relation(library_id,id),
 FOREIGN KEY(library_id,profile_id) REFERENCES local_profile(library_id,id));
CREATE TABLE creation_decision_event(
 id INTEGER PRIMARY KEY,library_id INTEGER NOT NULL,profile_id INTEGER NOT NULL,
 relation_id INTEGER NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 UNIQUE(profile_id,relation_id,version),
 FOREIGN KEY(library_id,relation_id) REFERENCES creation_relation(library_id,id),
 FOREIGN KEY(library_id,profile_id) REFERENCES local_profile(library_id,id));
CREATE INDEX creation_relation_forward ON creation_relation(library_id,source_id,domain);
CREATE INDEX creation_relation_backward ON creation_relation(library_id,target_id,domain);
CREATE TRIGGER creation_execution_immutable BEFORE UPDATE ON creation_execution
 BEGIN SELECT RAISE(ABORT,'execution evidence is immutable'); END;
CREATE TRIGGER creation_execution_no_delete BEFORE DELETE ON creation_execution
 BEGIN SELECT RAISE(ABORT,'execution evidence is immutable'); END;
CREATE TRIGGER creation_execution_revision_immutable BEFORE UPDATE ON creation_execution_revision
 BEGIN SELECT RAISE(ABORT,'execution revision evidence is immutable'); END;
CREATE TRIGGER creation_execution_revision_no_delete BEFORE DELETE ON creation_execution_revision
 BEGIN SELECT RAISE(ABORT,'execution revision evidence is immutable'); END;
CREATE TRIGGER creation_relation_immutable BEFORE UPDATE ON creation_relation
 BEGIN SELECT RAISE(ABORT,'relation evidence is immutable'); END;
CREATE TRIGGER creation_relation_no_delete BEFORE DELETE ON creation_relation
 BEGIN SELECT RAISE(ABORT,'relation evidence is immutable'); END;
CREATE TRIGGER creation_event_immutable BEFORE UPDATE ON creation_decision_event
 BEGIN SELECT RAISE(ABORT,'decision history is immutable'); END;
CREATE TRIGGER creation_event_no_delete BEFORE DELETE ON creation_decision_event
 BEGIN SELECT RAISE(ABORT,'decision history is immutable'); END;
"""
CHECKSUM = hashlib.sha256(SQL.encode()).hexdigest()
REQUIRED_OBJECTS = {
    ('table', 'creation_relation_schema'), ('table', 'creation_execution'),
    ('table', 'creation_execution_revision'), ('table', 'creation_relation'),
    ('table', 'creation_decision'), ('table', 'creation_decision_event'),
    ('index', 'creation_relation_forward'), ('index', 'creation_relation_backward'),
    ('trigger', 'creation_execution_immutable'), ('trigger', 'creation_execution_no_delete'),
    ('trigger', 'creation_execution_revision_immutable'),
    ('trigger', 'creation_execution_revision_no_delete'),
    ('trigger', 'creation_relation_immutable'), ('trigger', 'creation_relation_no_delete'),
    ('trigger', 'creation_event_immutable'), ('trigger', 'creation_event_no_delete'),
}


def _json(value):
    if not isinstance(value, dict) or not value:
        raise ValueError('Nonempty evidence object is required')
    result = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    if len(result.encode('utf-8')) > 65536:
        raise ValueError('Evidence exceeds supported size')
    return result


class CreationRelations:
    def __init__(self, database, library_uuid):
        self.database = Path(database).absolute()
        self.library_uuid = library_uuid

    def initialize(self):
        db = connect(self.database)
        try:
            initialize_schema(db)
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='creation_relation_schema'").fetchone()
            if not exists:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'creation_%'").fetchone():
                    raise ValueError('Partial creation relation extension')
                db.executescript('BEGIN IMMEDIATE;\n' + SQL)
                db.execute('INSERT INTO creation_relation_schema VALUES(1,?)', (CHECKSUM,))
                db.commit()
            self._validate(db)
        finally:
            db.close()

    @staticmethod
    def _validate(db):
        rows = [tuple(row) for row in db.execute('SELECT version,checksum FROM creation_relation_schema')]
        if rows != [(1, CHECKSUM)]:
            raise ValueError('Unsupported creation relation extension')
        existing = {(row['type'], row['name']) for row in db.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        missing = REQUIRED_OBJECTS - existing
        if missing:
            labels = ', '.join(f'{kind} {name}' for kind, name in sorted(missing))
            raise ValueError('Incomplete creation relation extension: ' + labels)

    @contextmanager
    def _transaction(self, *, write=True):
        db = connect(self.database)
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if not write:
                db.execute('PRAGMA query_only=ON')
            self._validate(db)
            library = db.execute('SELECT id FROM library WHERE uuid=?', (self.library_uuid,)).fetchone()
            if library is None:
                raise ValueError('Unknown library')
            yield db, library['id']
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _source(namespace, source_key):
        if any(not isinstance(value, str) or not value or len(value) > 1024
               for value in (namespace, source_key)):
            raise ValueError('Bounded stable source identity is required')

    def execution(self, namespace, source_key, evidence, *, identity_state='execution_unknown',
                  inputs=(), outputs=()):
        self._source(namespace, source_key)
        if identity_state not in ('execution_unknown', 'evidenced'):
            raise ValueError('Invalid execution identity state')
        raw = _json(evidence)
        bindings = self._revision_bindings(inputs, 'input') + self._revision_bindings(outputs, 'output')
        with self._transaction() as (db, library):
            binding_ids = []
            for direction, revision_uuid, role in bindings:
                revision = db.execute('SELECT id,asset_id FROM media_revision WHERE library_id=? AND uuid=?',
                                      (library, revision_uuid)).fetchone()
                if revision is None:
                    raise ValueError('Unknown or cross-library media revision')
                binding_ids.append((direction, revision['asset_id'], revision['id'], role))
            old = db.execute('SELECT * FROM creation_execution WHERE library_id=? AND namespace=? AND source_key=?',
                             (library, namespace, source_key)).fetchone()
            if old:
                old_bindings = [tuple(row) for row in db.execute(
                    'SELECT direction,asset_id,revision_id,role FROM creation_execution_revision '
                    'WHERE execution_id=? ORDER BY direction,asset_id,revision_id,role', (old['id'],))]
                if old['evidence_json'] != raw or old['identity_state'] != identity_state \
                        or old_bindings != sorted(binding_ids):
                    raise ValueError('Execution source identity conflict')
                return old['uuid']
            identity = str(uuid.uuid4())
            db.execute('INSERT INTO creation_execution(uuid,library_id,namespace,source_key,evidence_json,identity_state) '
                       'VALUES(?,?,?,?,?,?)', (identity, library, namespace, source_key, raw, identity_state))
            execution_id = db.execute('SELECT id FROM creation_execution WHERE uuid=?',
                                      (identity,)).fetchone()['id']
            for direction, asset_id, revision_id, role in binding_ids:
                db.execute('INSERT INTO creation_execution_revision VALUES(?,?,?,?,?,?)',
                           (library, execution_id, direction, asset_id, revision_id, role))
            return identity

    @staticmethod
    def _revision_bindings(values, direction):
        if not isinstance(values, (list, tuple)):
            raise ValueError(f'{direction}s must be a list of revision roles')
        result = []
        for item in values:
            if not isinstance(item, dict):
                raise ValueError(f'{direction}s must be revision role objects')
            revision_uuid, role = item.get('revisionUuid'), item.get('role')
            if not isinstance(revision_uuid, str) or not revision_uuid or len(revision_uuid) > 128:
                raise ValueError('A durable media revision UUID is required')
            if not isinstance(role, str) or not role or len(role) > 128:
                raise ValueError('A bounded revision role is required')
            result.append((direction, revision_uuid, role))
        return result

    def relation(self, source_uuid, target_uuid, namespace, source_key, evidence, *,
                 domain='derivation', kind='unknown', evidence_state='inferred', confidence=None):
        self._source(namespace, source_key)
        if domain not in ('derivation', 'experiment') or evidence_state not in ('inferred', 'evidenced'):
            raise ValueError('Invalid relation classification')
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise ValueError('Invalid relation kind')
        if confidence is not None and (type(confidence) not in (int, float) or not 0 <= confidence <= 1):
            raise ValueError('Invalid confidence')
        raw = _json(evidence)
        with self._transaction() as (db, library):
            endpoints = []
            for identity in (source_uuid, target_uuid):
                row = db.execute('SELECT id FROM creation_execution WHERE library_id=? AND uuid=?',
                                 (library, identity)).fetchone()
                if row is None:
                    raise ValueError('Unknown or cross-library execution')
                endpoints.append(row['id'])
            values = (*endpoints, domain, kind, evidence_state, raw, confidence)
            old = db.execute('SELECT * FROM creation_relation WHERE library_id=? AND namespace=? AND source_key=?',
                             (library, namespace, source_key)).fetchone()
            if old:
                if tuple(old[name] for name in ('source_id','target_id','domain','kind','evidence_state','evidence_json','confidence')) != values:
                    raise ValueError('Relation source identity conflict')
                return old['uuid']
            identity = str(uuid.uuid4())
            db.execute('INSERT INTO creation_relation(uuid,library_id,namespace,source_key,source_id,target_id,domain,kind,'
                       'evidence_state,evidence_json,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, library, namespace, source_key, *values))
            return identity

    def decide(self, profile_uuid, relation_uuid, expected_version, *, state, reason):
        if type(expected_version) is not int or expected_version < 0 or state not in ('confirmed','revoked'):
            raise ValueError('Invalid decision or version')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
            raise ValueError('Decision reason is required')
        with self._transaction() as (db, library):
            profile = db.execute('SELECT id FROM local_profile WHERE uuid=? AND library_id=?', (profile_uuid, library)).fetchone()
            edge = db.execute('SELECT * FROM creation_relation WHERE uuid=? AND library_id=?', (relation_uuid, library)).fetchone()
            if profile is None or edge is None:
                raise ValueError('Unknown relation or profile')
            profile_id = profile['id']
            old = db.execute('SELECT version FROM creation_decision WHERE profile_id=? AND relation_id=?',
                             (profile_id, edge['id'])).fetchone()
            if (old['version'] if old else 0) != expected_version:
                raise ValueError('Decision version conflict')
            if state == 'confirmed' and edge['domain'] == 'derivation':
                cycle = db.execute('WITH RECURSIVE reachable(id) AS (SELECT ? UNION '
                    'SELECT r.target_id FROM creation_relation r JOIN creation_decision d ON d.relation_id=r.id '
                    'JOIN reachable n ON r.source_id=n.id WHERE r.library_id=? AND r.domain=\'derivation\' '
                    'AND d.profile_id=? AND d.state=\'confirmed\') SELECT 1 FROM reachable WHERE id=? LIMIT 1',
                    (edge['target_id'], library, profile_id, edge['source_id'])).fetchone()
                if cycle:
                    raise ValueError('Confirmed derivation would form a cycle')
            version = expected_version + 1
            db.execute('INSERT INTO creation_decision VALUES(?,?,?,?,?) ON CONFLICT(profile_id,relation_id) '
                       'DO UPDATE SET version=excluded.version,state=excluded.state',
                       (edge['id'], library, profile_id, version, state))
            db.execute('INSERT INTO creation_decision_event(library_id,profile_id,relation_id,version,state,reason) VALUES(?,?,?,?,?,?)',
                       (library, profile_id, edge['id'], version, state, reason))
            return {'relationUuid': relation_uuid, 'version': version, 'state': state}

    @staticmethod
    def _execution_payload(db, row):
        bindings = db.execute(
            'SELECT b.direction,b.role,r.uuid AS revision_uuid,a.uuid AS media_uuid '
            'FROM creation_execution_revision b JOIN media_revision r '
            'ON r.library_id=b.library_id AND r.asset_id=b.asset_id AND r.id=b.revision_id '
            'JOIN media_asset a ON a.library_id=r.library_id AND a.id=r.asset_id '
            'WHERE b.execution_id=? ORDER BY b.direction,b.role,r.uuid', (row['id'],)).fetchall()
        return {'executionUuid': row['uuid'], 'identityState': row['identity_state'],
                'source': {'namespace': row['namespace'], 'key': row['source_key']},
                'evidence': json.loads(row['evidence_json']),
                'revisions': [{'direction': b['direction'], 'role': b['role'],
                               'revisionUuid': b['revision_uuid'], 'mediaUuid': b['media_uuid']}
                              for b in bindings]}

    def executions_for_media(self, media_uuid, *, revision_uuid=None, after_id=0, limit=50):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid execution page')
        with self._transaction(write=False) as (db, library):
            asset = db.execute('SELECT id FROM media_asset WHERE library_id=? AND uuid=?',
                               (library, media_uuid)).fetchone()
            if asset is None:
                raise ValueError('Unknown media identity')
            parameters = [library, after_id, asset['id']]
            revision_clause = ''
            if revision_uuid is not None:
                revision = db.execute('SELECT id FROM media_revision WHERE library_id=? AND asset_id=? AND uuid=?',
                                      (library, asset['id'], revision_uuid)).fetchone()
                if revision is None:
                    raise ValueError('Unknown media revision')
                revision_clause = ' AND b.revision_id=?'
                parameters.append(revision['id'])
            rows = db.execute('SELECT e.* FROM creation_execution e WHERE e.library_id=? AND e.id>? '
                'AND EXISTS(SELECT 1 FROM creation_execution_revision b WHERE b.execution_id=e.id '
                'AND b.library_id=e.library_id AND b.asset_id=?' + revision_clause + ') ORDER BY e.id LIMIT ?',
                [*parameters, limit+1]).fetchall()
            return {'items':[self._execution_payload(db,row) for row in rows[:limit]],
                    'nextAfterId': rows[limit-1]['id'] if len(rows)>limit else None}

    @staticmethod
    def _event_page(db, profile_id, relation_id, after_version=0, limit=50):
        rows = db.execute('SELECT version,state,reason,created_at FROM creation_decision_event '
                          'WHERE profile_id=? AND relation_id=? AND version>? '
                          'ORDER BY version LIMIT ?',
                          (profile_id, relation_id, after_version, limit+1)).fetchall()
        return {'items': [{'version': row['version'], 'state': row['state'],
                           'reason': row['reason'], 'createdAt': row['created_at']}
                          for row in rows[:limit]],
                'nextAfterVersion': rows[limit-1]['version'] if len(rows)>limit else None}

    def history(self, profile_uuid, relation_uuid, *, after_version=0, limit=50):
        if type(after_version) is not int or after_version < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid relation history page')
        with self._transaction(write=False) as (db, library):
            profile = db.execute('SELECT id FROM local_profile WHERE uuid=? AND library_id=?',
                                 (profile_uuid, library)).fetchone()
            relation = db.execute('SELECT id FROM creation_relation WHERE uuid=? AND library_id=?',
                                  (relation_uuid, library)).fetchone()
            if profile is None or relation is None:
                raise ValueError('Unknown profile or relation')
            return self._event_page(db, profile['id'], relation['id'], after_version, limit)

    def graph(self, profile_uuid, execution_uuid, *, include_candidates=False,
              max_nodes=200, max_edges=500, max_depth=20):
        """Return a bounded confirmed derivation traversal and optional candidate edges."""
        if type(include_candidates) is not bool:
            raise ValueError('include_candidates must be boolean')
        for value, maximum, label in ((max_nodes, 500, 'max_nodes'),
                                      (max_edges, 1000, 'max_edges'),
                                      (max_depth, 64, 'max_depth')):
            if type(value) is not int or value < 1 or value > maximum:
                raise ValueError(f'{label} must be between 1 and {maximum}')
        with self._transaction(write=False) as (db, library):
            profile = db.execute('SELECT id FROM local_profile WHERE uuid=? AND library_id=?',
                                 (profile_uuid, library)).fetchone()
            if profile is None:
                raise ValueError('Unknown profile')
            start = db.execute('SELECT * FROM creation_execution WHERE uuid=? AND library_id=?',
                               (execution_uuid, library)).fetchone()
            if start is None:
                raise ValueError('Unknown or cross-library execution')
            profile_id = profile['id']
            nodes = {start['id']: self._execution_payload(db, start)}
            queued = [(start['id'], 0)]
            visited = {start['id']}
            edges, evidence_only = [], []
            seen_edges, seen_evidence = set(), set()
            continuation = []
            while queued:
                current_id, depth = queued.pop(0)
                confirmed = db.execute(
                    "SELECT r.*,d.state,d.version FROM creation_relation r "
                    "JOIN creation_decision d ON d.relation_id=r.id AND d.profile_id=? "
                    "AND d.state='confirmed' WHERE r.library_id=? AND (r.source_id=? OR r.target_id=?) "
                    "AND r.domain='derivation' ORDER BY r.id LIMIT ?",
                    (profile_id, library, current_id, current_id, max_edges + 1)).fetchall()
                confirmed_ids = {row['id'] for row in confirmed}
                evid = db.execute(
                    "SELECT r.* FROM creation_relation r WHERE r.library_id=? AND (r.source_id=? OR r.target_id=?) "
                    "AND r.domain='derivation' AND r.evidence_state='evidenced' ORDER BY r.id LIMIT ?",
                    (library, current_id, current_id, max_edges + 1)).fetchall()
                for row in evid:
                    if row['id'] not in confirmed_ids and row['id'] not in seen_evidence:
                        if len(edges) + len(evidence_only) >= max_edges:
                            continuation.append(nodes[current_id]['executionUuid'])
                            break
                        evidence_only.append(self._relation_payload(db, row, profile_id))
                        seen_evidence.add(row['id'])
                selected = list(confirmed)
                extras = []
                if include_candidates:
                    extras = db.execute(
                        "SELECT r.*,d.state,d.version FROM creation_relation r LEFT JOIN creation_decision d "
                        "ON d.relation_id=r.id AND d.profile_id=? WHERE r.library_id=? AND (r.source_id=? OR r.target_id=?) "
                        "AND (r.evidence_state='inferred' OR r.domain='experiment') ORDER BY r.id LIMIT ?",
                        (profile_id, library, current_id, current_id, max_edges + 1)).fetchall()
                known = {row['id'] for row in selected}
                selected.extend(row for row in extras if row['id'] not in known)
                selected = [row for row in selected if row['id'] not in seen_edges]
                if len(selected) > max_edges or len(edges) + len(evidence_only) + len(selected) > max_edges:
                    continuation.append(nodes[current_id]['executionUuid'])
                    continue
                for edge in selected:
                    edges.append(self._relation_payload(db, edge, profile_id))
                    seen_edges.add(edge['id'])
                    # 只有**已确认**的边才能把邻居拉进执行链。候选边（inferred /
                    # 未确认）必须仅作为边展示：把它的目标并入 nodes 会让未确认的
                    # 推断参与执行链遍历，等于把「候选」悄悄当成「结论」。
                    if edge['id'] not in confirmed_ids:
                        continue
                    neighbor_id = edge['target_id'] if edge['source_id'] == current_id else edge['source_id']
                    target = db.execute('SELECT * FROM creation_execution WHERE id=? AND library_id=?',
                                        (neighbor_id, library)).fetchone()
                    if target['id'] not in nodes:
                        if len(nodes) >= max_nodes or depth >= max_depth:
                            continuation.append(target['uuid'])
                            continue
                        nodes[target['id']] = self._execution_payload(db, target)
                    if target['id'] not in visited and len(nodes) < max_nodes and depth < max_depth:
                        visited.add(target['id'])
                        queued.append((target['id'], depth + 1))
                    elif target['id'] not in visited and (len(nodes) >= max_nodes or depth >= max_depth):
                        continuation.append(target['uuid'])
            continuation = list(dict.fromkeys(continuation))
            return {'status': 'incomplete' if continuation else 'complete',
                    'continuation': continuation or None, 'includeCandidates': include_candidates,
                    'nodes': list(nodes.values()), 'edges': edges,
                    'evidenceOnly': evidence_only}

    @staticmethod
    def _relation_payload(db, row, profile_id):
        source = db.execute('SELECT * FROM creation_execution WHERE id=?', (row['source_id'],)).fetchone()
        target = db.execute('SELECT * FROM creation_execution WHERE id=?', (row['target_id'],)).fetchone()
        decision = db.execute('SELECT version,state FROM creation_decision '
                              'WHERE profile_id=? AND relation_id=?',
                              (profile_id, row['id'])).fetchone()
        events = CreationRelations._event_page(db, profile_id, row['id'])
        return {'relationUuid': row['uuid'], 'domain': row['domain'], 'kind': row['kind'],
                'evidenceState': row['evidence_state'], 'evidence': json.loads(row['evidence_json']),
                'confidence': row['confidence'],
                'source': CreationRelations._execution_payload(db, source),
                'target': CreationRelations._execution_payload(db, target),
                'decision': None if decision is None else {'version': decision['version'],
                                                          'state': decision['state']},
                'events': events['items'], 'nextAfterVersion': events['nextAfterVersion']}
