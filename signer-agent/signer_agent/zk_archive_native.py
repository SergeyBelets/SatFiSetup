"""Resident public witness builder. Cold restore remains independently replayed."""
import json
import os
from pathlib import Path
import select
import subprocess
from .operations import canonical

class NativeSession:
    def __init__(self,binary,directory):
        self.log=(Path(directory)/'native-resident.log').open('ab')
        self.process=subprocess.Popen([binary,'--serve'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,
            env=dict(PATH='/usr/bin:/bin'),bufsize=0)
    def prepare(self,request,out):
        if self.process.poll() is not None:raise ValueError('ZK_ARCHIVE_NATIVE_RESTART_REQUIRED')
        message=canonical(dict(request,output=str(out))).encode()+b'\n'
        if len(message)>2_000_000:raise ValueError('ZK_ARCHIVE_NATIVE_REQUEST_LIMIT')
        remaining=memoryview(message)
        while remaining:
            count=self.process.stdin.write(remaining)
            if not count:raise ValueError('ZK_ARCHIVE_NATIVE_FAILED')
            remaining=remaining[count:]
        self.process.stdin.flush()
        if not select.select([self.process.stdout],[],[],600)[0]:
            self.close();raise ValueError('ZK_ARCHIVE_NATIVE_TIMEOUT')
        line=self.process.stdout.readline(1024)
        if not line or json.loads(line).get('status')!='PREPARED':raise ValueError('ZK_ARCHIVE_NATIVE_FAILED')
    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        for stream in (self.process.stdin,self.process.stdout,self.log):stream.close()
