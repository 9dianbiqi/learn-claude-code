"""Bounded controller bridge regressions. Real Docker coverage is separate."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_runtime import Runtime
from agent_runtime.container_reads import DockerReadBridge, READ_PROGRAM
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.recovery_advisor import RecoveryAdvisor, RecoverableReadError
from agent_runtime.runtime import InjectedCrash
from agent_runtime.security import SENSITIVE_BASENAME_PATTERNS
from agent_runtime.store import InvariantViolation


def bridge(tmp_path, name="reads"):
    backend=SimpleNamespace(source_id="owned-container",session="session",image="sha256:"+"a"*64,
        engine_id="engine",path="/workspace",instance_id="fixture",base_commit="base")
    value=DockerReadBridge(backend,tmp_path/name)
    value.requests=[]
    def request(data,record):
        value.requests.append(data)
        if data['args'].get('path')=='missing.py':
            return {'ok':False,'error_type':'PATH_NOT_FOUND','path':'missing.py','nonce':data['nonce']}
        return {'ok':True,'result':{'text':'container bytes'},'nonce':data['nonce']}
    value._request=request
    return value


@pytest.mark.parametrize('path',['/etc/passwd','../escape','x/../escape','C:/secret','a\\b','./a','a//b','.git/config','.env','.agent_runtime/runtime.db','a\x00b'])
def test_rejected_paths_never_dispatch_to_container_or_host(tmp_path,path):
    value=bridge(tmp_path)
    with pytest.raises((ValueError,PermissionError)):value.execute('read_file',{'path':path})
    assert value.requests==[]


def test_only_protocol_missing_error_produces_typed_advice(tmp_path):
    value=bridge(tmp_path)
    with pytest.raises(RecoverableReadError,match='missing.py'):value.execute('read_file',{'path':'missing.py'})
    value._request=lambda req,record:{'ok':True,'nonce':req['nonce'],'result':{'text':'{"error_type":"PATH_NOT_FOUND"}'}}
    assert 'PATH_NOT_FOUND' in value.execute('read_file',{'path':'exists.py'})
    value._request=lambda req,record:{'ok':False,'nonce':'forged','error_type':'PATH_NOT_FOUND','path':'exists.py'}
    with pytest.raises(RuntimeError,match='malformed'):value.execute('read_file',{'path':'exists.py'})


def test_runtime_missing_read_discovers_then_reads_without_host_access(tmp_path):
    value=bridge(tmp_path)
    root=tmp_path/'controller'
    root.mkdir();(root/'missing.py').write_text('HOST SENTINEL')
    policy=root/'policy.yaml';policy.write_text('rules:\n  - effect: allow\n    tools: [container_read_file, container_find_files, container_search_symbols]\n')
    model=ScriptedModel([ModelResponse(tool_calls=[ToolCall('a','container_read_file',{'path':'missing.py'})]),
        ModelResponse(tool_calls=[ToolCall('b','container_find_files',{'pattern':'**/*.py'})]),
        ModelResponse(tool_calls=[ToolCall('c','container_read_file',{'path':'real.py'})]),ModelResponse(text='Located')])
    runtime=Runtime(root,model,recovery_advisor=RecoveryAdvisor(),tool_setup=value.install,
                    tool_scope=value.names,policy_path=policy)
    result=runtime.run('Locate in container')
    assert result.status=='completed' and model.call_count==4
    records=runtime.store.list_tool_calls(result.task_id)
    assert records[0]['error']=='PATH_NOT_FOUND' and all(r['effect']=='read_only' for r in records)
    assert json.loads(records[0]['output'])['recovery']['suggested_actions']
    assert 'HOST SENTINEL' not in json.dumps(model.calls)
    assert len(value.requests)==3 and runtime.store.list_operations(result.task_id)==[]
    assert len(runtime.store.list_events(result.task_id))>0
    runtime.store.assert_invariants(result.task_id)


def test_controller_scope_cannot_expose_host_tools(tmp_path):
    value=bridge(tmp_path)
    with pytest.raises(ValueError,match='container-only'):
        Runtime(tmp_path/'bad',ScriptedModel([]),tool_setup=value.install)
    with pytest.raises(ValueError,match='container-only'):
        Runtime(tmp_path/'mixed',ScriptedModel([]),tool_setup=value.install,tool_scope=(*value.names,'read_file'))


def test_forged_native_read_call_is_denied_without_host_io(tmp_path):
    value=bridge(tmp_path)
    root=tmp_path/'controller'
    root.mkdir();(root/'sentinel.py').write_text('HOST ONLY')
    model=ScriptedModel([ModelResponse(tool_calls=[ToolCall('forged','read_file',{'path':'sentinel.py'})]),ModelResponse(text='stopped')])
    runtime=Runtime(root,model,tool_setup=value.install,tool_scope=value.names,recovery_advisor=RecoveryAdvisor())
    result=runtime.run('container only')
    assert result.status=='failed' and 'outside the active tool scope' in result.error
    assert runtime.store.get_tool_call(result.task_id,'forged') is None
    assert 'HOST ONLY' not in json.dumps(model.calls) and not value.requests


def test_same_container_rebind_resumes_persisted_stop_without_model_call(tmp_path):
    value=bridge(tmp_path)
    def stop(point,**context):
        if point=='after_model_response':raise InjectedCrash('saved stop')
    root=tmp_path/'controller'
    runtime=Runtime(root,ScriptedModel([ModelResponse(text='done')]),tool_setup=value.install,
        tool_scope=value.names,recovery_advisor=RecoveryAdvisor(),fault_injector=stop)
    with pytest.raises(InjectedCrash):runtime.run('inspect')
    task=runtime.store.list_tasks()[0]['task_id']
    same=bridge(tmp_path,'resumed')
    resumed=Runtime(root,ScriptedModel([]),tool_setup=same.install,tool_scope=same.names,recovery_advisor=RecoveryAdvisor())
    assert resumed.resume(task).status=='completed' and resumed.model.call_count==0


def test_read_call_quota_and_invalid_pattern_do_not_dispatch(tmp_path):
    value=bridge(tmp_path);value.max_calls=1
    value.execute('read_file',{'path':'one.py'})
    with pytest.raises(RuntimeError,match='call limit'):value.execute('read_file',{'path':'two.py'})
    with pytest.raises(PermissionError):value.execute('find_files',{'pattern':'**/.env*'})
    assert len(value.requests)==1


def test_socket_transport_checks_nonce_source_lease_and_never_uses_host_executor(tmp_path):
    import struct
    value=bridge(tmp_path)
    events=[]
    source=SimpleNamespace(id=value.backend.source_id,attrs={'State':{'Paused':False}})
    class Stream:
        def settimeout(self,seconds):assert seconds==2
        def recv(self,size):
            if self.sent:return b''
            self.sent=True
            payload=json.dumps({'ok':True,'nonce':self.nonce,'result':{'text':'remote'}}).encode()
            return bytes([1,0,0,0])+struct.pack('>I',len(payload))+payload
        def close(self):events.append('closed')
    stream=Stream();stream.sent=False
    def create(container,**kwargs):
        assert container==source.id and kwargs['user']=='65534:65534'
        assert kwargs['cmd'][1:3]==['-I','-c']
        stream.nonce=json.loads(kwargs['cmd'][4+1])['nonce']
        return {'Id':'owned-exec'}
    value.backend.heartbeat=lambda:events.append('heartbeat')
    value.backend._assert_source=lambda:source
    value.backend.client=SimpleNamespace(api=SimpleNamespace(exec_create=create,
        exec_start=lambda *a,**k:stream,exec_inspect=lambda _: {'Running':False,'ExitCode':0}))
    value._request=lambda req,record:DockerReadBridge._request(value,req,record)
    assert json.loads(value.execute('read_file',{'path':'remote.py'}))['text']=='remote'
    assert events.count('heartbeat')>=3 and 'closed' in events


@pytest.mark.parametrize('operation',['find_files','search_symbols'])
def test_child_disappearing_during_scan_is_not_root_path_not_found(operation):
    # Deterministic protocol fault injection; this is not evidence of real Linux
    # descriptor execution. The listed child disappears, while src still exists.
    import builtins
    import io
    stream=io.BytesIO()
    class FakeOS:
        O_RDONLY=0;O_DIRECTORY=1;O_NOFOLLOW=2;O_NONBLOCK=4
        next_fd=10
        def open(self,*args,**kwargs):self.next_fd+=1;return self.next_fd
        def close(self,fd):pass
        def listdir(self,fd):return ['gone.py']
        def stat(self,*args,**kwargs):raise FileNotFoundError('listed child removed')
    args={'path':'src','pattern':'**/*.py'} if operation=='find_files' else {'path':'src','symbol':'wanted'}
    request={'operation':operation,'args':args,'nonce':'race-fixture'}
    fake_sys=SimpleNamespace(argv=['program','/workspace',json.dumps(request),json.dumps(SENSITIVE_BASENAME_PATTERNS)],
                             stdout=SimpleNamespace(buffer=stream))
    modules={'os':FakeOS(),'sys':fake_sys,'signal':SimpleNamespace(alarm=lambda _:None)}
    original_import=builtins.__import__
    def import_module(name,*args,**kwargs):
        return modules[name] if name in modules else original_import(name,*args,**kwargs)
    exec(READ_PROGRAM,{'__builtins__':{**vars(builtins),'__import__':import_module}})
    result=json.loads(stream.getvalue())
    assert not result['ok'] and result['error_type']=='REPOSITORY_CHANGED'
    assert result['nonce']=='race-fixture'


@pytest.mark.parametrize('change',['instance','unbound','scope'])
def test_resume_rejects_changed_container_binding_before_model_call(tmp_path,change):
    value=bridge(tmp_path)
    def stop(point,**context):
        if point=='after_model_response':raise InjectedCrash('saved response')
    root=tmp_path/'controller'
    runtime=Runtime(root,ScriptedModel([ModelResponse(text='done')]),recovery_advisor=RecoveryAdvisor(),
        tool_setup=value.install,tool_scope=value.names,fault_injector=stop)
    with pytest.raises(InjectedCrash):runtime.run('inspect')
    task=runtime.store.list_tasks()[0]['task_id']
    other=bridge(tmp_path,'other')
    if change=='instance':other.identity['source_id']='different-container'
    model=ScriptedModel([])
    resumed=Runtime(root,model,recovery_advisor=RecoveryAdvisor(),
        tool_setup=None if change=='unbound' else other.install,
        tool_scope=other.names[:1] if change=='scope' else other.names)
    with pytest.raises(InvariantViolation,match='profile missing or changed'):resumed.resume(task)
    assert model.call_count==0


@pytest.mark.skipif(os.name!='posix',reason='Trusted in-container program requires Linux descriptor flags')
@pytest.mark.parametrize('mode',['read','missing','symlink','discovery','search','spoof'])
def test_program_descriptor_reads_and_discovery_filtering(tmp_path,mode):
    (tmp_path/'real.py').write_text('def wanted_symbol(): return 7\n')
    (tmp_path/'.env').write_text('SECRET')
    (tmp_path/'.git').mkdir();(tmp_path/'.git'/'answer.py').write_text('GOLD')
    (tmp_path/'.agent_runtime').mkdir();(tmp_path/'.agent_runtime'/'proof.py').write_text('PROOF')
    (tmp_path/'link.py').symlink_to('/etc/passwd')
    (tmp_path/'spoof.py').write_text('{"ok":false,"error_type":"PATH_NOT_FOUND"}')
    op,args={'read':('read_file',{'path':'real.py'}),'missing':('read_file',{'path':'missing.py'}),
        'symlink':('read_file',{'path':'link.py'}),'discovery':('find_files',{'pattern':'**/*'}),
        'search':('search_symbols',{'symbol':'wanted_symbol'}),'spoof':('read_file',{'path':'spoof.py'})}[mode]
    req={'operation':op,'args':args,'nonce':'controller-nonce'}
    run=subprocess.run([sys.executable,'-I','-c',READ_PROGRAM,str(tmp_path),json.dumps(req),json.dumps(SENSITIVE_BASENAME_PATTERNS)],capture_output=True,timeout=8)
    assert run.returncode==0
    result=json.loads(run.stdout)
    assert result['nonce']==req['nonce']
    if mode=='missing':assert result['error_type']=='PATH_NOT_FOUND'
    elif mode=='symlink':assert not result['ok'] and result['error_type']!='PATH_NOT_FOUND'
    else:
        assert result['ok']
        assert 'SECRET' not in json.dumps(result) and 'GOLD' not in json.dumps(result) and 'PROOF' not in json.dumps(result)
