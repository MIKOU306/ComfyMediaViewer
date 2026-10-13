import importlib.util,json,os,sqlite3,statistics,sys,time,uuid
from pathlib import Path
from PIL import Image,PngImagePlugin

import tempfile
source=Path(__file__).resolve().parents[1]
base=Path(tempfile.mkdtemp(prefix='cmv-v3-verification-'))
work=base/'regression';work.mkdir()
data=work/'data';data.mkdir();media=work/'media';media.mkdir()
os.environ.update({'CMV_EXE_DIR':str(work),'LOCALAPPDATA':str(work/'localappdata'),
    'APPDATA':str(work/'appdata'),'CMV_AUTO_MIGRATE':'0','PYTHONDONTWRITEBYTECODE':'1'})
(work/'portable.flag').write_text(json.dumps({'dir':str(data)}),encoding='utf-8')
sys.path.insert(0,str(source))
from backend.migration.guard import DataWriterGuard,WriterBusy
from backend.storage.bootstrap import initialize_fresh
from backend.storage.runtime_state import load_state,StorageStateError
from backend.storage.server_catalog import ServerCatalog
from backend.storage.catalog import CatalogRepository
from backend.storage.media_stream import MediaStreamConflict
from backend.query.repository import QueryRepository
from backend.query.cursor import CursorCodec,CursorExpired
from backend.query.key import load_or_create_key
from backend.storage.scans import CatalogScans
from backend.storage.integrity import foreign_key_violation
from backend.storage.performance_schema import initialize as indexes
checks={};timings={}
def check(name,value):
    checks[name]=bool(value);assert value,name
def rejects(name,fn):
    try:fn()
    except Exception as e:checks[name]=type(e).__name__;return
    checks[name]=False;raise AssertionError(name)
def timing(fn,n=7):
    rows=[]
    for _ in range(n):
        start=time.perf_counter();fn();rows.append((time.perf_counter()-start)*1000)
    return round(statistics.median(rows),3)
guard=DataWriterGuard(data).acquire()
try:
    state=initialize_fresh(data,guard)
    check('fresh_pointer_valid',load_state(data)['mode']=='sqlite')
    rejects('fresh_never_overwrites_existing',lambda:initialize_fresh(data,guard))
    rejects('competing_writer_rejected',lambda:DataWriterGuard(data).acquire())
    spec=importlib.util.spec_from_file_location('cmv_optimized_regression',source/'server.py')
    server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
    server._initialize_catalog_storage();server.STATE['root']=str(media)
    cat=server._SQL_CATALOG;root_uuid=cat.ensure_root(media);db=data/'catalog.sqlite'
    p=media/'version.bmp';Image.new('RGB',(128,128),'red').save(p)
    old=cat.verify(str(media),p);args=(p,320,str(media),old['mediaId'],'rev:'+old['revisionId'])
    first=server.targeted_thumb(*args).read_bytes();st=p.stat()
    Image.new('RGB',(128,128),'blue').save(p);os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
    check('same_stat_fixture',p.stat().st_size==st.st_size and p.stat().st_mtime_ns==st.st_mtime_ns)
    rejects('old_thumbnail_revision_rejected',lambda:server.targeted_thumb(*args))
    new=cat.verify(str(media),p)
    check('same_stat_new_revision',new['revisionId']!=old['revisionId'])
    second=server.targeted_thumb(p,320,str(media),new['mediaId'],'rev:'+new['revisionId']).read_bytes()
    check('same_stat_new_thumbnail',first!=second)
    rejects('wrong_identity_rejected',lambda:server.targeted_thumb(p,320,str(media),str(uuid.uuid4()),'rev:'+new['revisionId']))

    # Exercise cache invalidation by a non-cooperating FK-off connection.
    def validate():
        with cat.repository._transaction(write=False):pass
    validate();validate()
    connection=sqlite3.connect(db)
    root_row=connection.execute('SELECT * FROM library_root WHERE uuid=?',(root_uuid,)).fetchone()
    bad_uuid=str(uuid.uuid4());before=db.stat()
    connection.execute('PRAGMA foreign_keys=OFF')
    connection.execute('INSERT INTO library_root(uuid,library_id,path,path_key) VALUES(?,?,?,?)',(bad_uuid,999999,str(media/'bad'),str(media/'bad')))
    connection.commit();os.utime(db,ns=(before.st_atime_ns,before.st_mtime_ns))
    rejects('external_fk_off_write_detected_even_restored_mtime',validate)
    connection.execute('DELETE FROM library_root WHERE uuid=?',(bad_uuid,));connection.commit();validate()
    trigger=connection.execute("SELECT sql FROM sqlite_master WHERE name='media_asset_current_revision_insert'").fetchone()[0]
    connection.execute('DROP TRIGGER media_asset_current_revision_insert');connection.commit()
    rejects('missing_core_trigger_rejected',validate)
    connection.execute(trigger);connection.commit();validate()
    query_trigger=connection.execute("SELECT sql FROM sqlite_master WHERE name='query_epoch_tag_insert'").fetchone()[0]
    connection.execute('DROP TRIGGER query_epoch_tag_insert');connection.commit()
    rejects('missing_query_trigger_rejected',lambda:server._SQL_QUERY.context(str(media)))
    connection.execute(query_trigger);connection.commit()
    connection.execute('PRAGMA journal_mode=WAL');validate()
    connection.execute('INSERT INTO library_root(uuid,library_id,path,path_key) VALUES(?,?,?,?)',(bad_uuid,999999,str(media/'bad'),str(media/'bad')))
    connection.commit();rejects('wal_external_violation_detected',validate)
    connection.execute('DELETE FROM library_root WHERE uuid=?',(bad_uuid,));connection.commit();validate()
    connection.close()

    # Cursor/count and organization generation: mutations expire prior cursors.
    for i in range(6):
        f=media/f'page-{i}.bmp';Image.new('RGB',(16,16),(i*30,10,20)).save(f);cat.observe(str(media),f,f.stat())
    request={'libraryId':state['libraryUuid'],'rootIds':[root_uuid],'recursive':True,'filters':{},'sort':'name_asc','limit':2}
    query=server._SQL_QUERY
    first_page=query.query(request);next_page=query.query({**request,'cursor':first_page['nextCursor']})
    check('cursor_no_duplicates',not ({x['mediaId'] for x in first_page['items']} & {x['mediaId'] for x in next_page['items']}))
    check('count_reused_exact',first_page['indexedCount']==next_page['indexedCount']==7 and len(query._counts)==1)
    projection=server._SQL_ORG.projection()
    server._SQL_ORG.set_tags(media_id=new['mediaId'],tags=['regression'])
    check('organization_cache_invalidated',server._SQL_ORG.projection()['tags'][str(p)]==['regression'])
    rejects('mutation_expires_cursor',lambda:query.query({**request,'cursor':first_page['nextCursor']}))
    tagged=query.query({**request,'filters':{'tags':['regression']}})
    check('full_filtered_query',tagged['indexedCount']==1 and tagged['items'][0]['mediaId']==new['mediaId'])

    # Completed directory observations replace full media-path enumeration.
    folders=media/'folders';folders.mkdir()
    folder_root=cat.ensure_root(folders);scans=CatalogScans(db);scans.initialize();epoch=scans.begin(state['libraryUuid'],folder_root)
    for i in range(401):
        folder=folders/f'dir-{i:04}';folder.mkdir();scans.observe_directory(state['libraryUuid'],folder_root,epoch,folder)
    scans.finish(state['libraryUuid'],folder_root,epoch)
    import backend.query.directories as directory_module
    original=directory_module.list_directories;calls=[]
    def counted(root,directory,names,**kw):
        count=0
        def supply():
            nonlocal count
            for name in names():count+=1;yield name
        result=original(root,directory,supply,**kw);calls.append(count);return result
    directory_module.list_directories=counted
    start=time.perf_counter();cursor=None;returned=[]
    for _ in range(3):
        page=query.directories(folder_root,limit=200,cursor=cursor);returned.extend(x['name'] for x in page['dirs']);cursor=page['nextCursor']
    timings['directory_401_three_pages_ms']=round((time.perf_counter()-start)*1000,3)
    check('directories_complete_unique',len(returned)==len(set(returned))==401 and cursor is None)
    check('directories_one_snapshot',calls==[401])
    directory_module.list_directories=original
    first=query.directories(folder_root,limit=200)
    (folders/'new-folder').mkdir()
    rejects('directory_mutation_expires_cursor',lambda:query.directories(folder_root,limit=200,cursor=first['nextCursor']))

    # Same second and exactly preserved mtime must not retain old prompt metadata.
    def png(path,text):
        info=PngImagePlugin.PngInfo();info.add_text('parameters',text+'\nNegative prompt: none\nSteps: 8, Sampler: Euler, CFG scale: 5, Seed: 1, Size: 32x32, Model: audit')
        Image.new('RGB',(32,32),'steelblue').save(path,pnginfo=info)
    a=media/'older.png';b=media/'newer.png';png(a,'OLDPROMPT');png(b,'UNCHANGED');ns=1790000000123000000
    os.utime(a,ns=(ns,ns));os.utime(b,ns=(ns+2_000_000_000,ns+2_000_000_000))
    server._index_builder(str(media));sig=server._dir_signature_calc(media,True)
    png(a,'NEWPROMPT');os.utime(a,ns=(ns+100_000_000,ns+100_000_000))
    server._index_builder(str(media));check('same_second_prompt_updated',server.IDX['map'][str(a)]['pos']=='NEWPROMPT')
    check('older_file_signature_changes',sig!=server._dir_signature_calc(media,True))
    exact=a.stat();png(a,'ENDPROMPT');os.utime(a,ns=(exact.st_atime_ns,exact.st_mtime_ns))
    server._index_builder(str(media));check('same_stat_prompt_updated',server.IDX['map'][str(a)]['pos']=='ENDPROMPT')
    # Persist the exact completed stat generation, and never publish absence
    # when a directory changes between the pre/post fingerprint passes.
    from backend.storage.scan_directory import scan_directory
    from backend.storage.directory_fingerprint import fingerprint
    isolated=work/'fingerprinted-media';isolated.mkdir();stable=isolated/'stable.bmp'
    Image.new('RGB',(32,32),'red').save(stable)
    scoped=cat.ensure_root(isolated)
    completed=scan_directory(db,state['libraryUuid'],scoped,fingerprint=fingerprint)
    check('fingerprinted_scan_complete',completed['state']=='complete')
    coverage=query.context(str(isolated))['scanCoverage']['roots']
    saved=next(row for row in coverage if row['rootId']==scoped)
    check('fingerprint_persisted_with_epoch',saved['fingerprint']==fingerprint(isolated))
    identity=cat.observe(str(isolated),stable,stable.stat())
    # The only delete here is this test-owned fixture, to exercise missing state.
    stable.unlink()
    calls=[0]
    def changing(path,**kwargs):
        calls[0]+=1
        return fingerprint(path,**kwargs)+(':changed' if calls[0]==2 else '')
    failed=scan_directory(db,state['libraryUuid'],scoped,fingerprint=changing)
    check('changed_scan_not_complete',failed['state']=='failed' and failed['failure']=='changed')
    record=cat.repository.resolve_asset(state['libraryUuid'],scoped,media_uuid=identity['mediaId'])
    check('changed_scan_did_not_publish_absence',record['availability']=='present')
    recovered=scan_directory(db,state['libraryUuid'],scoped,fingerprint=fingerprint)
    check('complete_scan_publishes_confirmed_absence',cat.repository.resolve_asset(state['libraryUuid'],scoped,media_uuid=identity['mediaId'])['availability']=='missing')
    interrupted=work/'interrupted-first-run';interrupted.mkdir()
    (interrupted/'fresh-initialization.json').write_text('{}',encoding='utf-8')
    rejects('interrupted_first_run_no_json_fallback',lambda:load_state(interrupted))
    import launcher_support
    launcher=work/'launcher';launcher.mkdir();user=work/'launcher-user';user.mkdir()
    check('optional_engine_skips_missing_models',not launcher_support.has_models(launcher,user))
    model_dir=user/'models/nsfw';model_dir.mkdir(parents=True);(model_dir/'probe.onnx').write_bytes(b'')
    check('optional_engine_finds_user_models',launcher_support.has_models(launcher,user))
    custom=work/'custom-portable';(custom/'models/nsfw').mkdir(parents=True);(custom/'models/nsfw/probe.onnx').write_bytes(b'')
    (launcher/'portable.flag').write_text(json.dumps({'dir':str(custom)}),encoding='utf-8')
    check('optional_engine_finds_custom_portable_models',launcher_support.has_models(launcher,work/'empty-user'))
    indexes(db);indexes(db)
    connection=sqlite3.connect(db)
    plan=[r[3] for r in connection.execute('EXPLAIN QUERY PLAN SELECT root_id FROM media_asset WHERE library_id=? AND path=?',(1,str(p)))]
    check('path_plan_constrains_path',any('library_id=? AND path=?' in line for line in plan))
    connection.close()
    # Recent lists carry batched identities and reuse a single safety snapshot.
    recent_media=work/'recent-media';recent_media.mkdir();server.STATE['root']=str(recent_media)
    first_recent=recent_media/'first.bmp';second_recent=recent_media/'second.bmp';gone=recent_media/'gone.bmp'
    for fixture in (first_recent,second_recent,gone):Image.new('RGB',(32,32),'red').save(fixture)
    verified_recent=cat.verify(str(recent_media),first_recent)
    server.recent_add(str(first_recent),'preview');server.recent_add(str(second_recent),'copy')
    server.recent_add(str(gone),'preview');gone.unlink()
    server.recent_add(str(first_recent),'download')
    ctx_calls=[0];batch_calls=[];original_ctx=server.nsfw_ctx;original_batch=cat.observe_many;original_current=cat.current
    def counted_ctx():ctx_calls[0]+=1;return original_ctx()
    def counted_batch(root,files,**kw):
        files=list(files);batch_calls.append(len(files));return original_batch(root,files,**kw)
    def no_individual_lookup(path):raise AssertionError('Recent list performed a per-item identity lookup')
    server.nsfw_ctx=counted_ctx;cat.observe_many=counted_batch;cat.current=no_individual_lookup
    try:recent_rows=server.recent_view()
    finally:server.nsfw_ctx=original_ctx;cat.observe_many=original_batch;cat.current=original_current
    check('recent_reuses_one_safety_snapshot',ctx_calls[0]==1)
    check('recent_identity_is_one_batch',batch_calls==[2])
    check('recent_missing_hidden_and_duplicates_removed',[r['path'] for r in recent_rows]==[str(first_recent),str(second_recent)])
    check('recent_action_and_order_preserved',recent_rows[0]['ract']=='download' and recent_rows[1]['ract']=='copy')
    check('recent_list_contains_version_targets',all(r['mediaId'] and r['revisionId'] and r['expectedVersion']=='rev:'+r['revisionId'] for r in recent_rows))
    check('recent_limit_preserved',len(server.recent_view(limit=1))==1)
    picked=recent_rows[0];stamp=first_recent.stat();Image.new('RGB',(32,32),'blue').save(first_recent)
    os.utime(first_recent,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    rejects('recent_same_stat_replacement_rejected',lambda:server.targeted_thumb(first_recent,320,str(recent_media),picked['mediaId'],picked['expectedVersion']))
    check('recent_foreign_keys_intact',not sqlite3.connect(db).execute('PRAGMA foreign_key_check').fetchall())
finally:guard.release()
result={'checks':checks,'timings':timings,'workspace':str(work),'path_plan':plan}
(base/'optimized-backend-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
