"""Reserve playback capacity before expensive strict source verification."""
from contextlib import contextmanager
from pathlib import Path

from .media_stream import verified_playback, MediaStreamConflict


def start_playback(manager, catalog, root, path, media_id, expected_version):
    root=str(Path(root).resolve())
    path=Path(path).resolve()
    identity=(root,str(path),media_id,expected_version)
    @contextmanager
    def provider():
        target=catalog.verify(root,path)
        if target['mediaId']!=media_id or 'rev:'+target['revisionId']!=expected_version:
            raise MediaStreamConflict('Video identity changed')
        with verified_playback(path,target['contentHash']) as opener:
            yield opener
    token=manager.start(identity,provider())
    return token,identity
