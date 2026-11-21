"""Archive-only backup/restore. Financial, nonce and request journals stay in place."""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from .zk_archive import ArchiveStore
from .zk_worker import state_wire


def backup(worker,path):
    with worker.exclusive():
        block,state,_,_,_=worker.context(allow_halt=True)
        # Backup may retain historical branches; the live target must be present.
        current=state_wire(state.functions.getState().call(block_identifier=block.number));worker.stable(block)
        worker.archive_ref(current)
        return worker.archive_store().backup(path)


def restore(worker,path):
    path=Path(path);a=worker.c['archive']
    if path.is_symlink() or not path.is_file() or path.stat().st_size>int(a['max_bytes']):raise ValueError('ZK_ARCHIVE_BACKUP_LIMIT')
    with worker.exclusive():
        block,state,_,_,_=worker.context(allow_halt=True)
        expected=state_wire(state.functions.getState().call(block_identifier=block.number));worker.stable(block)
        destination=worker.archive_store()
        with tempfile.TemporaryDirectory(prefix='restore-',dir=destination.path) as temp:
            folder=Path(temp);shutil.copyfile(path,folder/'archive.sqlite');(folder/'archive.sqlite').chmod(0o600)
            scope={k:worker.c[k] for k in ('chain_id','zk_state','bitcoin_policy')}
            source=ArchiveStore(folder,scope,int(a['max_headers']),int(a['max_bytes']))
            try:
                ref=source.ref_existing(expected)
                # Do not reuse a resident cache to validate an untrusted backup.
                if hasattr(worker,'_native_session'):worker._native_session.close();del worker._native_session
                result=worker.native(ref,[],[],folder/'validation')
                if result['before']!=expected or result['after']!=expected or result['resident_cache_hit']:
                    raise ValueError('ZK_ARCHIVE_RESTORE_NATIVE_MISMATCH')
                newer,state2,_,_,_=worker.context(allow_halt=True)
                if state_wire(state2.functions.getState().call(block_identifier=newer.number))!=expected:raise ValueError('ZK_ARCHIVE_RESTORE_STATE_CHANGED')
                worker.stable(newer);destination.import_verified(source,expected)
            finally:source.close()
        return dict(status='RESTORED',commitment=expected[7],known_count=expected[11],financial_journals_untouched=True)


def migrate_legacy(worker,expected,headers):
    """One-time replay of the retained V3 array; never delete its financial journal."""
    a=worker.c['archive'];destination=worker.archive_store()
    if not isinstance(headers,list) or len(headers)>int(a['max_headers']):raise ValueError('ZK_ARCHIVE_LEGACY_LIMIT')
    with tempfile.TemporaryDirectory(prefix='legacy-',dir=destination.path) as temp:
        folder=Path(temp)
        scope={k:worker.c[k] for k in ('chain_id','zk_state','bitcoin_policy')}
        source=ArchiveStore(folder,scope,int(a['max_headers']),int(a['max_bytes']))
        try:
            initial=worker.native([],[],[],folder/'initial')['before']
            ref=source.ref(initial)
            for i in range(0,len(headers),64):
                result=worker.native(ref,headers[i:i+64],[],folder/('batch-'+str(i)))
                ref=source.promote(result['before'],result['after'],result['delta'],dict(legacy_reconstruction=True))
            if ref.store.version(ref.commitment)['state']!=expected:raise ValueError('ZK_ARCHIVE_LEGACY_STATE_MISMATCH')
            destination.import_verified(source,expected)
        finally:source.close()
