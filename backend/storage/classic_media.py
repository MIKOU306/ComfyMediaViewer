"""Identity-bound cross-root navigation for the classic lightbox."""
from pathlib import Path
from .catalog import IdentityConflictError, IdentityNotFoundError, _uuid
from .asset_lifecycle import visible_clause


def resolve_media(catalog, media_id, revision_id=None):
    media_id=_uuid(media_id,'media')
    if revision_id is not None:
        revision_id=_uuid(revision_id,'revision')
    repository=catalog.repository
    with repository._transaction(write=False) as db:
        row=db.execute('SELECT a.*,l.uuid AS library_uuid,r.uuid AS root_uuid,r.path AS root_path '
            'FROM media_asset a JOIN library l ON l.id=a.library_id '
            'JOIN library_root r ON r.id=a.root_id AND r.library_id=a.library_id '
            'WHERE l.uuid=? AND a.uuid=? AND '+visible_clause(db),
            (catalog.library_uuid,media_id)).fetchone()
        if row is None or row['availability']!='present':
            raise IdentityNotFoundError('关联素材已不可用')
        identity=repository._asset_record(db,row)
        root_path=row['root_path']
    if revision_id is not None and identity['revisionId']!=revision_id:
        raise IdentityConflictError('关联记录属于历史文件版本，不能作为当前文件打开')
    path=Path(identity['path'])
    if not path.is_file():
        raise IdentityNotFoundError('关联素材文件不存在')
    verified=catalog.verify(root_path,path)
    if verified['mediaId']!=media_id or (revision_id is not None and verified['revisionId']!=revision_id):
        raise IdentityConflictError('关联素材内容已变化，请重新读取关系')
    suffix=path.suffix.lower()
    image=suffix in ('.png','.jpg','.jpeg','.webp','.gif','.bmp')
    item={**verified,'name':path.name,'type':'image' if image else 'video',
          'mtime':verified['mtimeNs']//1000000000,'png':suffix=='.png',
          'playable':image or suffix in ('.mp4','.webm','.m4v','.mov'),
          'expectedVersion':'rev:'+verified['revisionId']}
    return {'root':root_path,'directory':str(path.parent),'item':item}
