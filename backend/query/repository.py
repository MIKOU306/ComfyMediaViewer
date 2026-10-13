"""Catalog filtering and keyset pages in one SQLite read snapshot."""
from collections import OrderedDict
import threading
import copy
import os
import time
import unicodedata
import hashlib
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath

from backend.query.index_state import QueryIndexState
from backend.query.spec import normalize_query, QuerySpecError
from backend.query.scan_coverage import scan_coverage


IMAGES = {'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'tif', 'tiff', 'avif'}
VIDEOS = {'mp4', 'webm', 'mov', 'mkv', 'avi', 'm4v'}


def _path(value):
    return PureWindowsPath(value) if '\\' in value or (len(value) > 1 and value[1] == ':') else PurePosixPath(value)


def _name(value):
    return _path(value).name


def _extension(value):
    return _path(value).suffix.lower().lstrip('.')


def _fold(value):
    return unicodedata.normalize('NFC', value).casefold()


def _relative(path, root, sensitive):
    # Preserve exact path spelling; fold only when filesystem semantics are known.
    left, right = path.replace('\\', '/').rstrip('/'), root.replace('\\', '/').rstrip('/')
    left_parts, right_parts = left.split('/'), right.split('/')
    fold = (lambda value: value) if sensitive != 0 else str.casefold
    if len(left_parts) <= len(right_parts) or [fold(part) for part in left_parts[:len(right_parts)]] != [fold(part) for part in right_parts]:
        return None
    return '/'.join(left_parts[len(right_parts):])


def _in_directory(path, root, sensitive, directory, recursive):
    relative = _relative(path, root, sensitive)
    if relative is None:
        return False
    parts = relative.split('/')
    wanted = directory.split('/') if directory else []
    fold = (lambda value: value) if sensitive != 0 else str.casefold
    if len(parts) <= len(wanted) or any(part in ('', '.', '..') for part in parts):
        return False
    if [fold(part) for part in parts[:len(wanted)]] != [fold(part) for part in wanted]:
        return False
    return bool(recursive) or len(parts) == len(wanted) + 1


def _ns(value):
    epoch = datetime.fromisoformat('1970-01-01T00:00:00+00:00')
    delta = datetime.fromisoformat(value) - epoch
    result = (delta.days * 86400 + delta.seconds) * 1000000000 + delta.microseconds * 1000
    if not -(2 ** 63) <= result < 2 ** 63:
        raise QuerySpecError('Date exceeds catalog nanosecond range')
    return result


def _workflow_family_generation(db, library_id, profile_id):
    """Validate family evidence stores and derive a read-snapshot cursor generation."""
    from backend.metadata.workflow import WorkflowArchive
    from backend.metadata.workflow_decisions import WorkflowDecisions
    from backend.metadata.workflow_source_bindings import WorkflowSourceBindings
    try:
        WorkflowArchive._validate_schema(db)
        WorkflowSourceBindings._validate(db)
        WorkflowDecisions._validate(db)
        expected_columns = ["id", "uuid", "source_id", "capture_id", "library_id", "asset_id",
                            "revision_id", "snapshot_id", "version_id", "workflow_id",
                            "source_record_sha256", "imported_utf8_sha256", "conversion",
                            "evidence_kind", "created_at"]
        actual_columns = [row["name"] for row in db.execute(
            "PRAGMA table_info('workflow_source_binding')")]
        if actual_columns != expected_columns:
            raise RuntimeError("Workflow source binding columns are invalid")
        required_objects = {
            ("index", "workflow_source_binding_capture_idx"),
            ("trigger", "workflow_source_binding_scope"),
            ("trigger", "workflow_source_binding_no_update"),
            ("trigger", "workflow_source_binding_no_delete"),
        }
        actual_objects = {(row["type"], row["name"]) for row in db.execute(
            "SELECT type,name FROM sqlite_master WHERE name LIKE 'workflow_source_binding_%'")}
        if not required_objects.issubset(actual_objects):
            raise RuntimeError("Workflow source binding schema objects are incomplete")
        binding_fks = {(row["table"], row["from"], row["to"])
                       for row in db.execute("PRAGMA foreign_key_list('workflow_source_binding')")}
        required_binding_fks = {
            ("workflow_source_evidence", "source_id", "id"),
            ("workflow_capture", "capture_id", "id"),
            ("library", "library_id", "id"),
            ("media_revision", "library_id", "library_id"),
            ("media_revision", "asset_id", "asset_id"),
            ("media_revision", "revision_id", "id"),
            ("metadata_snapshot", "library_id", "library_id"),
            ("metadata_snapshot", "snapshot_id", "id"),
            ("metadata_snapshot", "asset_id", "asset_id"),
            ("metadata_snapshot", "revision_id", "revision_id"),
            ("workflow_version", "library_id", "library_id"),
            ("workflow_version", "version_id", "id"),
            ("workflow_version", "workflow_id", "workflow_id"),
        }
        if not required_binding_fks.issubset(binding_fks):
            raise RuntimeError("Workflow source binding scope foreign keys are invalid")
        binding = db.execute(
            "SELECT COUNT(*) AS row_count,COALESCE(MAX(id),0) AS max_id "
            "FROM workflow_source_binding WHERE library_id=?", (library_id,)).fetchone()
        events = db.execute(
            "SELECT COUNT(*) AS row_count,COALESCE(MAX(id),0) AS max_id "
            "FROM workflow_family_decision_event WHERE library_id=? AND profile_id=?",
            (library_id, profile_id)).fetchone()
    except Exception as error:
        raise QuerySpecError("Workflow family evidence storage is not ready or has an invalid schema") from error
    raw = (library_id, profile_id, int(binding["row_count"]), int(binding["max_id"]),
           int(events["row_count"]), int(events["max_id"]))
    return hashlib.sha256(":".join(map(str, raw)).encode("ascii")).hexdigest()


class QueryRepository:
    def __init__(self, database, library_uuid, profile_uuid, cursor_codec):
        self.state = QueryIndexState(database)
        self.library_uuid = library_uuid
        self.profile_uuid = profile_uuid
        self.codec = cursor_codec
        self._cache_lock = threading.RLock()
        self._counts = OrderedDict()
        self._directory_pages = OrderedDict()

    def context(self, active_root):
        """Return explicit ownership; never infer identity from a basename."""
        with self.state._connection() as db:
            state = self.state.read_in_transaction(db)
            owner = db.execute('SELECT l.id FROM library l JOIN local_profile p ON p.library_id=l.id '
                               'WHERE l.uuid=? AND p.uuid=?',
                               (self.library_uuid, self.profile_uuid)).fetchone()
            if owner is None:
                raise QuerySpecError('Library/Profile ownership mismatch')
            scans = scan_coverage(db, owner['id'])
            roots = [dict(row) for row in db.execute(
                'SELECT uuid AS rootId,path,case_sensitive AS caseSensitive,availability '
                'FROM library_root WHERE library_id=? ORDER BY uuid', (owner['id'],))]
            exact = [row['rootId'] for row in roots if row['path'] == str(active_root)]
            return {'libraryId': self.library_uuid, 'profileId': self.profile_uuid,
                    'roots': roots, 'activeRootId': exact[0] if len(exact) == 1 else None,
                    **state, **scans}

    def directories(self, root_uuid, directory=None, *, limit=100, cursor=None):
        from backend.query.directories import list_directories
        query = normalize_query({'libraryId': self.library_uuid, 'rootIds': [root_uuid],
                                 'directory': directory, 'sort':'name_asc','recursive':False,
                                 'limit':limit,'cursor':cursor})
        query['queryHash'] = hashlib.sha256(('directories:' + query['queryHash']).encode()).hexdigest()
        normalized = query['spec']
        with self.state._connection() as db:
            root = db.execute('SELECT rt.id,rt.path,rt.case_sensitive AS caseSensitive FROM library_root rt '
                              'JOIN library l ON l.id=rt.library_id JOIN local_profile p ON p.library_id=l.id '
                              'WHERE l.uuid=? AND p.uuid=? AND rt.uuid=?',
                              (self.library_uuid, self.profile_uuid, root_uuid)).fetchone()
            if root is None:
                raise QuerySpecError('Root is outside request scope')
            state = self.state.read_in_transaction(db)
            scan = db.execute('SELECT state FROM catalog_scan WHERE root_id=?', (root['id'],)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_scan'").fetchone() else None
            authoritative_dirs = bool(scan and scan['state'] == 'complete')
            wanted_parts = normalized['directory'].split('/') if normalized['directory'] else []
            compare = (lambda text: text) if root['caseSensitive'] != 0 else str.casefold
            def names():
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_directory_schema'").fetchone():
                    from backend.storage.directories import initialize
                    initialize(db)  # Existing schema: validate only, never create in a read transaction.
                    parent_key = compare(normalized['directory'] or '')
                    for row in db.execute('SELECT name FROM catalog_directory WHERE root_id=? AND parent_key=?',
                                          (root['id'], parent_key)):
                        yield row['name']
                    if authoritative_dirs:
                        return
                from backend.storage.asset_lifecycle import visible_clause
                for row in db.execute('SELECT path FROM media_asset a WHERE root_id=? AND ' + visible_clause(db), (root['id'],)):
                    relative = _relative(row['path'], root['path'], root['caseSensitive'])
                    parts = relative.split('/') if relative else []
                    if (len(parts) > len(wanted_parts) + 1 and
                            [compare(part) for part in parts[:len(wanted_parts)]] == [compare(part) for part in wanted_parts]):
                        yield parts[len(wanted_parts)]
            # A bounded complete directory snapshot avoids rescanning paths for each page.
            from pathlib import Path
            target = Path(root['path']).joinpath(*wanted_parts)
            ancestors = (target, *target.parents)
            def signature():
                values = []
                for part in ancestors:
                    try:
                        info = part.lstat()
                    except OSError:
                        values.append((str(part), 'unavailable')); continue
                    if part.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise QuerySpecError('Linked directory browsing is not supported')
                    values.append((str(part),info.st_dev,info.st_ino,info.st_mtime_ns))
                return tuple(values)
            cache_key = (root_uuid, normalized['directory'], state['indexGeneration'], signature())
            with self._cache_lock:
                saved = self._directory_pages.get(cache_key)
                snapshot = copy.deepcopy(saved[1]) if saved and time.monotonic()-saved[0] < 2 else None
            if snapshot is not None and snapshot['directoryAvailability']=='online':
                try:
                    with os.scandir(target) as probe:
                        next(probe,None)
                except OSError:
                    snapshot=None
            if snapshot is None:
                snapshot = list_directories(dict(root), normalized['directory'], names, budget=10001)
                if not snapshot['directoriesTruncated'] and cache_key[-1] == signature():
                    with self._cache_lock:
                        self._directory_pages[cache_key] = (time.monotonic(), copy.deepcopy(snapshot))
                        self._directory_pages.move_to_end(cache_key)
                        while len(self._directory_pages)>8 or sum(len(value[1]['dirs']) for value in self._directory_pages.values())>20000:
                            self._directory_pages.popitem(last=False)
            last = None
            if cursor:
                last, scope_uuid = self.codec.read(cursor, query, snapshot['directoryGeneration'])
                if not isinstance(last, str) or scope_uuid != root_uuid:
                    raise QuerySpecError('Invalid directory pagination position')
            if snapshot['directoriesTruncated']:
                # Above the memory budget retain the original bounded streaming path.
                result = list_directories(dict(root), normalized['directory'], names, budget=limit+1, after=last)
            else:
                result = snapshot
                result['dirs'] = [item for item in snapshot['dirs'] if last is None or compare(item['name'])>last]
            has_more = len(result['dirs']) > limit
            result['dirs'] = result['dirs'][:limit]
            result['nextCursor'] = self.codec.issue(query, result['directoryGeneration'],
                [compare(result['dirs'][-1]['name']), root_uuid]) if has_more else None
            result['directoriesTruncated'] = has_more
            return result

    def query(self, request, *, safety_context=None):
        query = normalize_query(request)
        spec, filters = query['spec'], query['spec']['filters']
        if spec['libraryId'] != self.library_uuid:
            raise QuerySpecError('Library is outside request scope')
        if filters['safety'] == 'hide_nsfw' and safety_context is None:
            raise QuerySpecError('Safety context is required for hide_nsfw')
        if safety_context is not None:
            if (not isinstance(safety_context, dict) or
                    set(safety_context) != {'modelId', 'threshold'} or
                    (safety_context['modelId'] is not None and
                     (not isinstance(safety_context['modelId'], str) or not safety_context['modelId'].strip())) or
                    type(safety_context['threshold']) not in (int, float) or
                    not 0 <= safety_context['threshold'] <= 1):
                raise QuerySpecError('Invalid safety context')
        with self.state._connection() as db:
            state = self.state.read_in_transaction(db)
            safety_generation = None
            workflow_filter = bool(filters['workflowClasses'] or filters['workflowKind'])
            workflow_family_filter = bool(filters['workflowFamilyUuids'])
            if workflow_filter:
                try:
                    from backend.metadata.workflow_projection_store import WorkflowProjectionStore
                    from backend.metadata.workflow_projection import PROJECTION_VERSION
                    workflow_generation = WorkflowProjectionStore.read_generation_in_transaction(db)
                except (ImportError, RuntimeError) as error:
                    raise QuerySpecError('Workflow classification storage is not ready') from error
                state['indexGeneration'] += ':workflow:' + str(workflow_generation)
            if filters['recentOnly']:
                from backend.storage.recent import RecentRepository
                recent_generation = RecentRepository.read_generation_in_transaction(db, self.profile_uuid)
                state['indexGeneration'] += ':recent:' + str(recent_generation)
            if safety_context is not None:
                from backend.query.safety_state import SafetyQueryState
                try:
                    safety_generation = SafetyQueryState.read_in_transaction(
                        db, safety_context['modelId'], safety_context['threshold'])
                except RuntimeError as error:
                    raise QuerySpecError(str(error)) from error
                if filters['safety'] == 'hide_nsfw':
                    verification_generation = db.execute('SELECT serial FROM query_verification_state WHERE singleton=1').fetchone()['serial']
                    state['indexGeneration'] += ':safety:' + safety_generation + ':verification:' + str(verification_generation)
            scope = db.execute('SELECT l.id AS library_id,p.id AS profile_id FROM library l '
                               'JOIN local_profile p ON p.library_id=l.id WHERE l.uuid=? AND p.uuid=?',
                               (self.library_uuid, self.profile_uuid)).fetchone()
            if scope is None:
                raise QuerySpecError('Library/Profile ownership mismatch')
            if workflow_family_filter:
                family_generation = _workflow_family_generation(
                    db, scope['library_id'], scope['profile_id'])
                state['indexGeneration'] += ':workflow-family:' + family_generation
            roots = db.execute('SELECT uuid FROM library_root WHERE library_id=?',
                               (scope['library_id'],)).fetchall()
            if set(spec['rootIds']) - {row['uuid'] for row in roots}:
                raise QuerySpecError('Root is outside request scope')
            scans = scan_coverage(db, scope['library_id'], spec['rootIds'])
            state['indexGeneration'] += ':scan:' + scans['scanCheckpointHash']
            lora_filter = bool(filters['loras'] or filters['loraTerms'])
            if lora_filter:
                state['indexGeneration'] += ':lora-references:2'
            if filters['recentOnly'] or workflow_filter or workflow_family_filter or lora_filter:
                state['indexGeneration'] = 'composite-query:' + hashlib.sha256(state['indexGeneration'].encode('utf-8')).hexdigest()
            db.create_function('media_name', 1, _name, deterministic=True)
            db.create_function('media_ext', 1, _extension, deterministic=True)
            db.create_function('media_fold', 1, _fold, deterministic=True)
            db.create_function('media_relative', 3, _relative, deterministic=True)
            db.create_function('media_in_directory', 5, _in_directory, deterministic=True)
            from backend.storage.asset_lifecycle import visible_clause
            clauses = ['a.library_id=?', visible_clause(db)]
            params = [scope['library_id']]
            if workflow_filter:
                workflow_conditions = ['wp.library_id=a.library_id', 'wp.asset_id=a.id',
                    'wp.revision_id=a.current_revision_id', 'wp.projection_version=?',
                    'wp.snapshot_id=(SELECT ms.id FROM metadata_snapshot ms WHERE ms.library_id=a.library_id '
                    'AND ms.asset_id=a.id AND ms.revision_id=a.current_revision_id '
                    'ORDER BY ms.captured_at DESC,ms.id DESC LIMIT 1)']
                params.append(PROJECTION_VERSION)
                if filters['workflowClasses']:
                    workflow_conditions.append('wp.classification IN (' + ','.join('?' for _ in filters['workflowClasses']) + ')')
                    params.extend(filters['workflowClasses'])
                if filters['workflowKind']:
                    workflow_conditions.append('wp.kind=?')
                    params.append(filters['workflowKind'])
                clauses.append('EXISTS(SELECT 1 FROM workflow_classification_projection wp WHERE '
                               + ' AND '.join(workflow_conditions) + ')')
            if workflow_family_filter:
                family_conditions = [
                    'wfb.library_id=a.library_id', 'wfb.asset_id=a.id',
                    'wfb.revision_id=a.current_revision_id',
                    'wfb.snapshot_id=(SELECT ms.id FROM metadata_snapshot ms WHERE ms.library_id=a.library_id '
                    'AND ms.asset_id=a.id AND ms.revision_id=a.current_revision_id '
                    'ORDER BY ms.captured_at DESC,ms.id DESC LIMIT 1)',
                    'wff.uuid IN (' + ','.join('?' for _ in filters['workflowFamilyUuids']) + ')',
                ]
                params.extend([scope['profile_id'], *filters['workflowFamilyUuids']])
                clauses.append(
                    'EXISTS(SELECT 1 FROM workflow_source_binding wfb '
                    'JOIN workflow_capture wfc ON wfc.id=wfb.capture_id AND wfc.library_id=wfb.library_id '
                    'LEFT JOIN workflow_family_decision_event wfd ON wfd.id=(' 
                    'SELECT e.id FROM workflow_family_decision_event e WHERE e.library_id=wfc.library_id '
                    'AND e.profile_id=? AND e.capture_id=wfc.id ORDER BY e.decision_version DESC LIMIT 1) '
                    'JOIN workflow_family wff ON wff.library_id=wfc.library_id AND wff.id='
                    'CASE WHEN wfd.event_kind=\'confirm\' THEN wfd.target_workflow_id ELSE wfc.workflow_id END '
                    'WHERE ' + ' AND '.join(family_conditions) + ')')
            if filters['recentOnly']:
                clauses.append('rv.asset_id IS NOT NULL')
            def values_clause(field, values):
                clauses.append(field + ' IN (' + ','.join('?' for _ in values) + ')')
                params.extend(values)
            if spec['rootIds']:
                values_clause('rt.uuid', spec['rootIds'])
            directory = spec['directory']
            if directory is not None or not spec['recursive']:
                # Relative directory is matched literally, without LIKE wildcards.
                clauses.append('media_in_directory(a.path,rt.path,rt.case_sensitive,?,?)=1')
                params.extend([directory or '', int(spec['recursive'])])
            if filters['favorite'] is not None:
                clauses.append('COALESCE(s.favorite,0)=?')
                params.append(int(filters['favorite']))
            tags = filters['tags']
            if filters['markState'] is not None:
                if filters['markState'] == '*':
                    clauses.append("COALESCE(s.mark_state,'')<>''")
                else:
                    clauses.append('s.mark_state=?')
                    params.append(filters['markState'])
            if filters['favoriteCategory'] is not None:
                clauses.append('s.favorite_category=?')
                params.append(filters['favoriteCategory'])
            if tags and filters['tagHierarchy']:
                matches = []
                for tag in tags:
                    term = _fold(tag)
                    matches.append('EXISTS(SELECT 1 FROM tag_assignment ta JOIN tag t ON t.id=ta.tag_id '
                                   'AND t.library_id=ta.library_id WHERE ta.asset_id=a.id '
                                   'AND ta.library_id=a.library_id AND t.profile_id=? '
                                   'AND (media_fold(t.name)=? OR instr(media_fold(t.name),?)=1))')
                    params.extend([scope['profile_id'], term, term + '/'])
                clauses.append('(' + (' AND ' if filters['tagMode'] == 'all' else ' OR ').join(matches) + ')')
            elif tags:
                expression = ('SELECT COUNT(DISTINCT t.name) FROM tag_assignment ta JOIN tag t ON t.id=ta.tag_id '
                              'WHERE ta.asset_id=a.id AND ta.library_id=a.library_id AND t.profile_id=? AND '
                              't.name IN (' + ','.join('?' for _ in tags) + ')')
                clauses.append('(' + expression + ') ' + ('=?' if filters['tagMode'] == 'all' else '>0'))
                params.extend([scope['profile_id'], *tags])
                if filters['tagMode'] == 'all':
                    params.append(len(tags))
            formats = filters['formats']
            for term in filters['tagTerms']:
                clauses.append('EXISTS(SELECT 1 FROM tag_assignment ta JOIN tag t ON t.id=ta.tag_id '
                               'AND t.library_id=ta.library_id WHERE ta.asset_id=a.id '
                               'AND ta.library_id=a.library_id AND t.profile_id=? '
                               'AND (media_fold(t.name)=? OR instr(media_fold(t.name),?)=1))')
                params.extend([scope['profile_id'], term, term + '/'])
            excluded_tags = filters['excludedTags']
            if excluded_tags and filters['tagHierarchy']:
                for tag in excluded_tags:
                    term = _fold(tag)
                    clauses.append('NOT EXISTS(SELECT 1 FROM tag_assignment ta JOIN tag t ON t.id=ta.tag_id '
                                   'AND t.library_id=ta.library_id WHERE ta.asset_id=a.id '
                                   'AND ta.library_id=a.library_id AND t.profile_id=? '
                                   'AND (media_fold(t.name)=? OR instr(media_fold(t.name),?)=1))')
                    params.extend([scope['profile_id'], term, term + '/'])
            elif excluded_tags:
                clauses.append('NOT EXISTS(SELECT 1 FROM tag_assignment ta JOIN tag t ON t.id=ta.tag_id '
                               'AND t.library_id=ta.library_id WHERE ta.asset_id=a.id '
                               'AND ta.library_id=a.library_id AND t.profile_id=? AND t.name IN (' +
                               ','.join('?' for _ in excluded_tags) + '))')
                params.extend([scope['profile_id'], *excluded_tags])
            if formats:
                values_clause('media_ext(a.path)', formats)
            if filters['types']:
                extensions = sorted(set().union(*(IMAGES if kind == 'image' else VIDEOS for kind in filters['types'])))
                values_clause('media_ext(a.path)', extensions)
            if filters['date']:
                for key, operator in [('after', '>='), ('before', '<')]:
                    if key == 'before' and filters['date']['beforeInclusive']:
                        operator = '<='
                    value = filters['date'][key]
                    if value is not None:
                        clauses.append('r.mtime_ns' + operator + '?')
                        params.append(_ns(value))
            if filters['text'] is not None:
                clauses.append('instr(media_fold(media_name(a.path)),?)>0')
                params.append(filters['text'])
            prompt = filters['prompt']
            prompt_candidate = None
            prompt_fallback = None
            for term in filters['nameTerms']:
                clauses.append('instr(media_fold(media_name(a.path)),?)>0')
                params.append(term)
            if prompt:
                from backend.query.prompt_fts import prompt_candidate_clause
                candidate = prompt_candidate_clause(db, prompt['text'])
                prompt_candidate = candidate
                if candidate is not None:
                    expression = ('EXISTS(SELECT 1 FROM generation_prompt_binding pb '
                                  'WHERE pb.run_id=g.run_id AND ' + candidate['sql'])
                    params.extend(candidate['params'])
                else:
                    # No FTS candidate set (short term, unsafe token form, or a
                    # database without the extension). The exact match is computed
                    # once in its own CTE: inlining ``instr(media_fold(...))`` made
                    # SQLite re-run the Python fold for every candidate asset row,
                    # which measured in seconds on a 775-asset root. The CTE already
                    # applied the exact predicate, so membership is the whole test.
                    prompt_fallback = {'normalized': _fold(prompt['text'])}
                    expression = ('EXISTS(SELECT 1 FROM generation_prompt_binding pb '
                                  'WHERE pb.run_id=g.run_id AND pb.id IN '
                                  '(SELECT id FROM prompt_exact_bindings)')
                if prompt['side'] != 'any':
                    expression += ' AND pb.side=?'
                    params.append(prompt['side'])
                clauses.append(expression + ')')
            for field in ('models', 'loras'):
                if filters[field]:
                    clauses.append('EXISTS(SELECT 1 FROM generation_model_reference mr ' +
                                   ('' if field == 'loras' else
                                    'JOIN generation_model_usage mu ON mu.run_id=mr.run_id AND mu.model_reference_id=mr.id ') +
                                   'WHERE mr.run_id=g.run_id AND mr.kind ' +
                                   ("='lora'" if field == 'loras' else "!='lora'") +
                                   ' AND mr.raw_name IN (' + ','.join('?' for _ in filters[field]) + '))')
                    params.extend(filters[field])
            for term in filters['modelTerms']:
                clauses.append('EXISTS(SELECT 1 FROM generation_model_reference mr '
                               'JOIN generation_model_usage mu ON mu.run_id=mr.run_id AND mu.model_reference_id=mr.id '
                               "WHERE mr.run_id=g.run_id AND mr.kind IN ('checkpoint','unet') "
                               'AND instr(media_fold(mr.raw_name),?)>0)')
                params.append(term)
            for term in filters['loraTerms']:
                clauses.append('EXISTS(SELECT 1 FROM generation_model_reference mr '
                               "WHERE mr.run_id=g.run_id AND mr.kind='lora' "
                               'AND instr(media_fold(mr.raw_name),?)>0)')
                params.append(term)
            if filters['samplers']:
                clauses.append('EXISTS(SELECT 1 FROM generation_step gs WHERE gs.run_id=g.run_id '
                               "AND json_type(gs.params_json,'$.sampler')='text' "
                               "AND json_extract(gs.params_json,'$.sampler') IN (" +
                               ','.join('?' for _ in filters['samplers']) + '))')
                params.extend(filters['samplers'])
            for term in filters['samplerTerms']:
                clauses.append('EXISTS(SELECT 1 FROM generation_step gs WHERE gs.run_id=g.run_id '
                               "AND json_type(gs.params_json,'$.sampler')='text' "
                               "AND instr(media_fold(json_extract(gs.params_json,'$.sampler')),?)>0)")
                params.append(term)
            if filters['dimensions']:
                # width_px/height_px are VIRTUAL generated columns over
                # result_json's $.dimensions, backed by
                # metadata_snapshot_dimensions_idx. That alone took the full
                # catalog from 6.2s to 1.2s: the old form ran four json_extract
                # calls against result_json (avg 172KB, max 4.2MB per row)
                # inside a per-asset correlated subquery.
                #
                # The "latest snapshot" guard is deliberately KEPT. A revision may
                # carry snapshots from several collector_versions, and dropping the
                # guard changes results when they disagree: on a synthetic case
                # (one revision, 1024x1024 from one collector and 2048x2048 from
                # another) the unguarded form reported 490 instead of 489. Being
                # 1.2s and exactly right beats 0.22s and subtly wrong.
                clauses.append('EXISTS(SELECT 1 FROM metadata_snapshot ms WHERE ms.asset_id=a.id '
                               'AND ms.revision_id=a.current_revision_id AND ms.id=(SELECT latest.id '
                               'FROM metadata_snapshot latest WHERE latest.asset_id=a.id '
                               'AND latest.revision_id=a.current_revision_id ORDER BY latest.captured_at DESC,latest.id DESC LIMIT 1) '
                               'AND ms.width_px=? AND ms.height_px=?)')
                params.extend([filters['dimensions']['width'], filters['dimensions']['height']])
            key = {'mtime': 'r.mtime_ns', 'size': 'r.size_bytes', 'name': 'media_name(a.path)',
                   'recent': 'rv.sequence'}[spec['sort'].split('_')[0]]
            descending = spec['sort'].endswith('_desc')
            if safety_context is None:
                safety_label, safety_score = "'unknown'", '-1.0'
                safety_from = ''
                safety_join_params = []
                select_params = []
            else:
                safety_label = ("CASE WHEN r.verification_state='verified' THEN "
                                "COALESCE(sman.label,CASE WHEN sm.score IS NOT NULL THEN "
                                "CASE WHEN sm.score>=? THEN 'nsfw' ELSE 'sfw' END END,'unknown') "
                                "ELSE 'unknown' END")
                safety_score = ("CASE WHEN r.verification_state='verified' AND sman.label IS NULL "
                                "THEN COALESCE(sm.score,-1.0) ELSE -1.0 END")
                safety_from = ('LEFT JOIN safety_manual sman ON sman.library_id=a.library_id AND sman.asset_id=a.id '
                               'AND sman.revision_id=r.id '
                               'LEFT JOIN safety_model sm ON sm.library_id=a.library_id AND sm.asset_id=a.id '
                               'AND sm.revision_id=r.id AND sm.model_id=? AND ? IS NOT NULL ')
                safety_join_params = [safety_context['modelId'], safety_context['modelId']]
                select_params = [float(safety_context['threshold'])]
            if filters['safety'] == 'hide_nsfw':
                clauses.append("(" + safety_label + ") IS NOT 'nsfw'")
                params.append(float(safety_context['threshold']))
            # The prompt candidate set is prepended as its own CTE so the FTS scan
            # (or the exact fallback scan) runs once per statement rather than once
            # per candidate asset row. The prefix always carries WITH, including
            # when no CTE is needed.
            with_prefix, with_params = 'WITH ', []
            if prompt and prompt_candidate is not None:
                with_prefix = prompt_candidate['withClause'] + ','
                with_params = list(prompt_candidate['withParams'])
            elif prompt and prompt_fallback is not None:
                with_prefix = ('WITH prompt_exact_bindings(id) AS MATERIALIZED ('
                               'SELECT id FROM generation_prompt_binding '
                               'WHERE instr(media_fold(text),?)>0),')
                with_params = [prompt_fallback['normalized']]
            cte = (with_prefix + 'filtered AS (SELECT a.uuid AS mediaId,r.uuid AS revisionId,a.path,a.availability,'
                   'rt.availability AS rootAvailability,'
                   'r.verification_state AS verificationState,'
                   'r.size_bytes AS size,r.mtime_ns AS mtimeNs,COALESCE(s.favorite,0) AS favorite,'
                   's.favorite_category AS favoriteCategory,s.mark_state AS mark,' +
                   safety_label + ' AS n,' + safety_score + ' AS ns,' + key + ' AS sortKey FROM media_asset a '
                   'JOIN library_root rt ON rt.id=a.root_id AND rt.library_id=a.library_id '
                   'LEFT JOIN media_revision r ON r.id=a.current_revision_id AND r.asset_id=a.id '
                   'LEFT JOIN user_media_state s ON s.asset_id=a.id AND s.profile_id=? '
                   + ('LEFT JOIN recent_view rv ON rv.asset_id=a.id AND rv.library_id=a.library_id '
                      'AND rv.profile_id=? ' if filters['recentOnly'] else '') +
                   'LEFT JOIN generation_asset_current g ON g.asset_id=a.id AND g.revision_id=a.current_revision_id '
                   'AND g.library_id=a.library_id '
                   + safety_from +
                   'WHERE ' + ' AND '.join(clauses) + ') ')
            params = [*with_params, *select_params, scope['profile_id'],
                      *([scope['profile_id']] if filters['recentOnly'] else []), *safety_join_params, *params]
            # Count belongs to the normalized query, profile and exact read generation.
            count_key = (self.profile_uuid, query['queryHash'], state['indexGeneration'], repr(params))
            with self._cache_lock:
                indexed_count = self._counts.get(count_key)
            if indexed_count is None:
                indexed_count = db.execute(cte + 'SELECT COUNT(*) FROM filtered', params).fetchone()[0]
                with self._cache_lock:
                    self._counts[count_key] = indexed_count
                    self._counts.move_to_end(count_key)
                    while len(self._counts) > 128:
                        self._counts.popitem(last=False)
            page_clause, page_params = '', []
            if query['cursor']:
                last, media = self.codec.read(query['cursor'], query, state['indexGeneration'])
                if last is None:
                    page_clause = 'WHERE sortKey IS NULL AND mediaId>? '
                    page_params = [media]
                else:
                    page_clause = ('WHERE sortKey IS NULL OR sortKey' + ('<' if descending else '>') +
                                   '? OR (sortKey=? AND mediaId>?) ')
                    page_params = [last, last, media]
            rows = db.execute(cte + 'SELECT * FROM filtered ' + page_clause +
                              'ORDER BY (sortKey IS NULL) ASC,sortKey ' + ('DESC' if descending else 'ASC') +
                              ',mediaId ASC LIMIT ?', [*params, *page_params, spec['limit'] + 1]).fetchall()
            has_more = len(rows) > spec['limit']
            items = [dict(row) for row in rows[:spec['limit']]]
            tags = {item['mediaId']: [] for item in items}
            if items:
                tag_rows = db.execute(
                    'SELECT a.uuid AS mediaId,t.name FROM media_asset a '
                    'JOIN tag_assignment ta ON ta.asset_id=a.id AND ta.library_id=a.library_id '
                    'JOIN tag t ON t.id=ta.tag_id AND t.library_id=ta.library_id '
                    'WHERE a.library_id=? AND t.profile_id=? AND a.uuid IN (' +
                    ','.join('?' for _ in items) + ') ORDER BY a.uuid,t.name',
                    [scope['library_id'], scope['profile_id'], *tags],
                ).fetchall()
                for row in tag_rows:
                    tags[row['mediaId']].append(row['name'])
            cursor = self.codec.issue(query, state['indexGeneration'],
                                      [items[-1]['sortKey'], items[-1]['mediaId']]) if has_more else None
            for item in items:
                item.pop('sortKey')
                item['favorite'] = bool(item['favorite'])
                item['name'] = _name(item['path'])
                extension = _extension(item['path'])
                item['type'] = 'image' if extension in IMAGES else 'video' if extension in VIDEOS else 'file'
                item['mtime'] = item['mtimeNs'] // 1000000000 if item['mtimeNs'] is not None else None
                item['revisionToken'] = 'rev:' + item['revisionId'] if item['revisionId'] else None
                item['fav'] = item['favorite']
                item['favcat'] = item['favoriteCategory']
                item['tags'] = tags[item['mediaId']]
                item['mark'] = item['mark'] or ''
            return {'items': items, 'nextCursor': cursor, 'queryId': query['queryHash'],
                    'indexedCount': indexed_count, 'countState': 'indexed_only',
                    'coverage': 'partial', 'partial': True, **state, **scans}
