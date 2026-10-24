"""Atomic per-user session journal and exclusive process lock for fixed profit."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile

from strategies.fixed_profit_filtered import CONFIG, CONFIG_HASH, KEY, VERSION


class SessionJournal:
    def __init__(self,directory,user_id,today,*,live):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.lock=(self.directory/f'{user_id}.lock').open('a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close();raise RuntimeError('Another fixed-profit runner owns this account lock')
        mode='live' if live else 'paper'
        self.path=self.directory/f'{user_id}-{today}-{mode}.json'
        try:
            if self.path.exists():raise RuntimeError('Session already journaled; no automatic same-day restart')
            for path in self.directory.glob(f'{user_id}-*-live.json'):
                record=json.loads(path.read_text())
                actions=[e['action'] for e in record['events']]
                if 'ENTRY_INTENT' in actions and 'EXIT_FILLED' not in actions:
                    raise RuntimeError('Unresolved prior live entry; reconcile broker account before restart')
            self.record=dict(strategy_key=KEY,version=VERSION,config=dict(CONFIG),config_hash=CONFIG_HASH,
                             user_id=str(user_id),date=str(today),mode=mode,status='running',events=[])
            self._save()
        except BaseException:
            self.lock.close();raise

    def _save(self):
        fd,name=tempfile.mkstemp(dir=self.directory,prefix='.fixed-profit-')
        try:
            with os.fdopen(fd,'w') as handle:
                json.dump(self.record,handle,indent=2);handle.flush();os.fsync(handle.fileno())
            os.replace(name,self.path)
            directory_fd=os.open(self.directory,os.O_RDONLY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
        finally:
            if os.path.exists(name):os.unlink(name)

    def event(self,action,**details):
        self.record['events'].append(dict(time=datetime.now(timezone.utc).isoformat(),action=action,**details))
        self._save()

    def heartbeat(self):
        self.record["heartbeat_at"]=datetime.now(timezone.utc).isoformat();self._save()

    def finish(self,status):
        self.record['status']=status;self._save()

    def close(self):
        self.lock.close()
