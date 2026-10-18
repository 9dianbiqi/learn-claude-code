"""Trusted, bounded read tools for one isolated Docker workspace; no host fallback."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import struct
import time
import uuid

from .docker_recovery import durable_write
from .recovery_advisor import RecoverableReadError
from .security import SENSITIVE_BASENAME_PATTERNS, is_sensitive_relative, glob_targets_sensitive


READ_PROGRAM = r'''
import fnmatch,hashlib,json,os,signal,stat,sys
signal.alarm(5)
root,request,patterns=sys.argv[1],json.loads(sys.argv[2]),json.loads(sys.argv[3])
def forbidden(path):
    parts=path.split('/')
    return any(p.casefold() in ('.git','.agent_runtime','__pycache__') or
               any(fnmatch.fnmatchcase(p.casefold(),v) for v in patterns) for p in parts)
def open_path(path,directory=False):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        parts=[] if path=='.' else path.split('/')
        for index,part in enumerate(parts):
            flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
            if index<len(parts)-1 or directory: flags|=os.O_DIRECTORY
            child=os.open(part,flags,dir_fd=fd); os.close(fd); fd=child
        return fd
    except BaseException:
        os.close(fd); raise
def read(path,limit):
    if forbidden(path): raise PermissionError('Protected repository path')
    fd=open_path(path)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise ValueError('Regular unlinked file required')
        if before.st_size>limit: raise ValueError('File byte limit')
        data=b''
        while True:
            chunk=os.read(fd,min(65536,limit+1-len(data)))
            if not chunk: break
            data+=chunk
            if len(data)>limit: raise ValueError('File byte limit')
        after=os.fstat(fd)
        if (before.st_ino,before.st_dev,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_dev,after.st_size,after.st_mtime_ns,after.st_ctime_ns): raise ValueError('File changed while reading')
        return data
    finally: os.close(fd)
def files(path):
    pending=[path]; count=0
    while pending:
        current=pending.pop(); fd=open_path(current,directory=True)
        try:
            for name in sorted(os.listdir(fd)):
                count+=1
                if count>10000: raise ValueError('Discovery node limit')
                relative=name if current=='.' else current+'/'+name
                if forbidden(relative): continue
                info=os.stat(name,dir_fd=fd,follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode): pending.append(relative)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink==1: yield relative
        finally: os.close(fd)
missing_path=None
try:
    root_check=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); os.close(root_check)
    operation=request['operation']; args=request['args']
    if operation=='read_file':
        missing_path=args['path']
        data=read(args['path'],1048576)
        result={'path':args['path'],'sha256':hashlib.sha256(data).hexdigest(),'text':data.decode('utf-8')}
    else:
        query_root=args.get('path','.')
        missing_path=query_root if query_root!='.' else None
        initial=open_path(query_root,directory=True); os.close(initial)
        missing_path=None
        hits=[]; scanned=0; total=0
        for path in files(args.get('path','.')):
            scanned+=1
            if scanned>2000: raise ValueError('Discovery file limit')
            if operation=='find_files':
                pattern=args['pattern']
                if fnmatch.fnmatchcase(path,pattern) or pattern.startswith('**/') and fnmatch.fnmatchcase(path,pattern[3:]): hits.append({'path':path})
            else:
                data=read(path,131072); total+=len(data)
                if total>8388608: raise ValueError('Search byte limit')
                try: lines=data.decode('utf-8').splitlines()
                except UnicodeDecodeError: continue
                for number,line in enumerate(lines,1):
                    if args['symbol'] in line: hits.append({'path':path,'line':number,'text':line[:400]})
            if len(hits)>=100: break
        result={'matches':hits,'scanned_files':scanned,'limit_reached':len(hits)>=100}
    envelope={'ok':True,'result':result}
except FileNotFoundError:
    envelope=({'ok':False,'error_type':'PATH_NOT_FOUND','path':missing_path} if missing_path is not None
              else {'ok':False,'error_type':'REPOSITORY_CHANGED'})
except Exception as exc:
    envelope={'ok':False,'error_type':type(exc).__name__,'reason':str(exc)[:200]}
envelope['nonce']=request['nonce']
output=json.dumps(envelope,ensure_ascii=True).encode()
if len(output)>65536: output=json.dumps({'ok':False,'error_type':'OUTPUT_LIMIT','nonce':request['nonce']}).encode()
sys.stdout.buffer.write(output)
'''


class DockerReadBridge:
    names = ("container_read_file", "container_find_files", "container_search_symbols")

    def __init__(self, backend, output, *, max_calls=24):
        if type(max_calls) is not int or not 1 <= max_calls <= 64:
            raise ValueError("Bounded read call limit required")
        self.backend, self.max_calls = backend, max_calls
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.calls = 0
        self.identity = {"source_id": backend.source_id, "session": backend.session,
            "image": backend.image, "engine_id": backend.engine_id, "workspace": backend.path,
            "instance": backend.instance_id, "base_commit": backend.base_commit,
            "program_sha256": hashlib.sha256(READ_PROGRAM.encode()).hexdigest(),
            "sensitive_policy": list(SENSITIVE_BASENAME_PATTERNS), "max_calls": max_calls,
            "protocol": "container-reads-v1"}

    @staticmethod
    def _path(value, *, root=False):
        if (not isinstance(value, str) or not value or len(value)>400 or '\\' in value
                or ':' in value or any(ord(c)<32 for c in value) or value.startswith('/')
                or '..' in value.split('/')):
            raise ValueError("Canonical repository-relative path required")
        normalized = PurePosixPath(value).as_posix()
        if normalized != value or normalized == '.' and not root:
            raise ValueError("Canonical repository-relative path required")
        if is_sensitive_relative(normalized) or any(p.casefold() in {'.agent_runtime','__pycache__'} for p in normalized.split('/')):
            raise PermissionError("Protected repository metadata or credentials")
        return normalized

    def install(self, registry):
        fields = ({"path":{"type":"string"}}, {"pattern":{"type":"string"}},
                  {"symbol":{"type":"string"},"path":{"type":"string"}})
        for name, operation, properties in zip(self.names, ('read_file','find_files','search_symbols'), fields):
            required = {'read_file':['path'],'find_files':['pattern'],'search_symbols':['symbol']}[operation]
            registry.register_controller_read_tool(name, "Read-only isolated container "+operation,
                {"type":"object","properties":properties,"required":required,"additionalProperties":False},
                binding=self.identity, timeout_seconds=10,
                adapter=lambda args, operation=operation: self.execute(operation, args))

    def execute(self, operation, args):
        if operation not in {'read_file','find_files','search_symbols'} or not isinstance(args, dict):
            raise ValueError("Unknown read operation")
        allowed = {'read_file':{'path'},'find_files':{'pattern'},'search_symbols':{'symbol','path'}}[operation]
        if set(args)-allowed: raise ValueError("Unexpected read argument")
        args = dict(args)
        if operation == 'read_file': args['path'] = self._path(args.get('path'))
        elif operation == 'find_files':
            value = args.get('pattern')
            if (not isinstance(value,str) or not value or len(value)>200 or '\\' in value or ':' in value
                    or value.startswith('/') or '..' in value.split('/') or any(ord(c)<32 for c in value)):
                raise ValueError("Bounded relative file pattern required")
            if glob_targets_sensitive(value) or '.agent_runtime' in value.casefold():
                raise PermissionError("Protected discovery pattern")
        else:
            args['path'] = self._path(args.get('path','.'), root=True)
            symbol = args.get('symbol')
            if not isinstance(symbol,str) or not symbol.strip() or len(symbol)>120 or any(ord(c)<32 for c in symbol):
                raise ValueError("Bounded literal symbol required")
        if self.calls >= self.max_calls: raise RuntimeError("Container read call limit")
        self.calls += 1
        request = {"operation":operation,"args":args,"nonce":uuid.uuid4().hex}
        record = {"binding":self.identity,"request":request,"ok":False}
        started = time.monotonic()
        try:
            result = self._request(request, record)
            if not isinstance(result,dict) or result.get('nonce') != request['nonce'] or type(result.get('ok')) is not bool:
                raise RuntimeError("Untrusted or malformed read response")
            record['response'] = result
            if result['ok']:
                record['ok'] = True
                return json.dumps(result['result'], sort_keys=True)
            if result.get('error_type') == 'PATH_NOT_FOUND' and result.get('path') == args.get('path','.'):
                raise RecoverableReadError(result['path'])
            raise RuntimeError("Container read failed: "+str(result.get('error_type')))
        except Exception as exc:
            record['error_type'] = type(exc).__name__
            raise
        finally:
            record['seconds'] = time.monotonic()-started
            durable_write(self.output/f'{self.calls:03d}.json', (json.dumps(record,sort_keys=True)+'\n').encode())

    def _request(self, request, record):
        backend = self.backend
        if not callable(backend.heartbeat): raise RuntimeError("A live fenced Runtime heartbeat is required")
        backend.heartbeat()
        source = backend._assert_source()
        if source.attrs['State']['Paused']: raise RuntimeError("Cannot read a frozen source")
        program = ["/opt/miniconda3/envs/testbed/bin/python",'-I','-c',READ_PROGRAM,backend.path,
                   json.dumps(request,sort_keys=True),json.dumps(SENSITIVE_BASENAME_PATTERNS)]
        exec_id = backend.client.api.exec_create(source.id,cmd=program,user='65534:65534',stdout=True,stderr=True)['Id']
        record['exec_id'] = exec_id
        stream = backend.client.api.exec_start(exec_id,socket=True)
        connection = getattr(stream,'_sock',stream)
        connection.settimeout(2)
        stdout,stderr,pending = bytearray(),bytearray(),bytearray()
        deadline = time.monotonic()+8
        try:
            while True:
                backend.heartbeat()
                if time.monotonic()>deadline: raise TimeoutError('Container read deadline')
                data=connection.recv(8192)
                if not data: break
                pending.extend(data)
                while len(pending)>=8:
                    channel,length=pending[0],struct.unpack('>I',pending[4:8])[0]
                    if channel not in {1,2} or length>65536: raise RuntimeError('Invalid read frame')
                    if len(pending)<8+length: break
                    (stdout if channel==1 else stderr).extend(pending[8:8+length]);del pending[:8+length]
                    if len(stdout)+len(stderr)>65536: raise RuntimeError('Read output bound')
        finally: stream.close()
        backend.heartbeat()
        state=backend.client.api.exec_inspect(exec_id)
        record.update(exit_code=state.get('ExitCode'),stdout_bytes=len(stdout),stderr_bytes=len(stderr))
        if pending or state['Running'] or state['ExitCode']!=0 or stderr:
            raise RuntimeError('Container read process failed or incomplete')
        return json.loads(stdout)
