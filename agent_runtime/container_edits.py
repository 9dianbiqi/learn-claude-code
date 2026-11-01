"""Bounded CAS edits in a controller-owned container; never a host fallback.

The container workspace must have one trusted writer. Hash preconditions detect
staleness but do not fence arbitrary concurrent in-container processes. Runtime
records edits as unknown_write and will not replay an ambiguous delivery.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import uuid

from .container_reads import DockerReadBridge
from .docker_recovery import durable_write


EDIT_PROGRAM = r'''
import fnmatch,hashlib,json,os,signal,stat,sys,tempfile
from pathlib import Path
if hasattr(signal,'alarm'): signal.alarm(5)
root,request,patterns=Path(sys.argv[1]),json.loads(sys.argv[2]),json.loads(sys.argv[3])
temporary=None
def protected(parts):
    return any(p.casefold() in ('.git','.agent_runtime','__pycache__') or
        any(fnmatch.fnmatchcase(p.casefold(),v) for v in patterns) for p in parts)
def inspect_path(path):
    info=path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
        raise ValueError('Link/reparse point denied')
    return info
try:
    args=request['args']; name=args['path']; parts=name.split('/')
    if not parts or any(p in ('','..','.') for p in parts) or '\\' in name or ':' in name or protected(parts):
        raise ValueError('Unsafe repository path')
    if not stat.S_ISDIR(inspect_path(root).st_mode): raise ValueError('Invalid workspace')
    target=root
    for part in parts[:-1]:
        target=target/part
        if not stat.S_ISDIR(inspect_path(target).st_mode): raise ValueError('Invalid parent')
    target=target/parts[-1]; info=inspect_path(target)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>1048576:
        raise ValueError('Bounded single-link regular file required')
    with target.open('rb') as stream: before=stream.read(1048577)
    if len(before)>1048576: raise ValueError('File bound exceeded')
    if hashlib.sha256(before).hexdigest()!=args['expected_sha256']: raise ValueError('STALE_FILE')
    text=before.decode('utf-8'); old=args['old_text']; new=args['new_text']
    if not old or text.count(old)!=1: raise ValueError('UNIQUE_MATCH_REQUIRED')
    after=text.replace(old,new,1).encode('utf-8')
    if len(after)>1048576: raise ValueError('Output bound exceeded')
    fd,temporary=tempfile.mkstemp(prefix='.lcc-edit-',dir=str(target.parent))
    with os.fdopen(fd,'wb') as stream:
        stream.write(after); stream.flush(); os.fsync(stream.fileno())
    os.chmod(temporary,stat.S_IMODE(info.st_mode))
    current=inspect_path(target)
    if (current.st_ino,current.st_dev,current.st_nlink,current.st_size,current.st_mtime_ns)!=(info.st_ino,info.st_dev,1,info.st_size,info.st_mtime_ns):
        raise ValueError('STALE_FILE')
    if hashlib.sha256(target.read_bytes()).hexdigest()!=args['expected_sha256']: raise ValueError('STALE_FILE')
    os.replace(temporary,target); temporary=None
    response={'ok':True,'nonce':request['nonce'],'result':{'path':name,
        'before_sha256':args['expected_sha256'],'after_sha256':hashlib.sha256(after).hexdigest()}}
except Exception as exc:
    response={'ok':False,'nonce':request['nonce'],'error_type':type(exc).__name__,'error':str(exc)}
finally:
    if temporary is not None: os.unlink(temporary)
print(json.dumps(response,sort_keys=True))
'''


class DockerEditBridge(DockerReadBridge):
    program = EDIT_PROGRAM
    names = ("container_edit_file",)

    def __init__(self, backend, output, *, max_calls=16):
        super().__init__(backend, output, max_calls=max_calls)
        self.identity["protocol"] = "container-edits-v1"
        self.reads = None

    def install(self, registry):
        properties = {key: {"type": "string"} for key in ("path", "expected_sha256", "old_text", "new_text")}
        registry.register_controller_write_tool(self.names[0],
            "Edit one unique occurrence in a previously read container file; supply its exact SHA-256.",
            {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False},
            binding=self.identity, adapter=self.execute, timeout_seconds=10)

    def execute(self, args):
        if not isinstance(args, dict) or set(args) != {"path", "expected_sha256", "old_text", "new_text"}:
            raise ValueError("exact edit arguments required")
        self._path(args["path"])
        if not isinstance(args["expected_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", args["expected_sha256"]):
            raise ValueError("expected read SHA-256 required")
        if any(not isinstance(args[k], str) for k in ("old_text", "new_text")) or not args["old_text"]:
            raise ValueError("bounded text edit required")
        if len(json.dumps(args).encode()) > 32768:
            raise ValueError("edit request exceeds 32 KiB")
        if self.calls >= self.max_calls:
            raise RuntimeError("container edit call limit")
        if self.reads is None:
            raise RuntimeError("a bound read bridge is required")
        observed = False
        for path in sorted(self.reads.output.glob("*.json"), reverse=True):
            read = json.loads(path.read_text(encoding="utf-8"))
            request = read.get("request", {})
            if request.get("operation") == "read_file" and request.get("args", {}).get("path") == args["path"]:
                observed = (read.get("ok") is True and read.get("binding") == self.reads.identity
                            and read.get("response", {}).get("result", {}).get("sha256") == args["expected_sha256"])
                break
        if not observed:
            raise RuntimeError("matching prior bound-container read required")
        self.calls += 1
        request = {"operation": "edit_file", "args": dict(args), "nonce": uuid.uuid4().hex}
        record = {"binding": self.identity, "request": request, "ok": False}
        try:
            response = self._request(request, record)
            record["response"] = response
            if not isinstance(response, dict) or response.get("nonce") != request["nonce"] or type(response.get("ok")) is not bool:
                raise RuntimeError("malformed edit response; outcome may be unknown")
            if not response["ok"]:
                raise RuntimeError("container edit rejected: " + str(response.get("error")))
            result = response.get("result")
            expected_after = result.get("after_sha256") if isinstance(result, dict) else None
            if (not isinstance(result, dict) or result.get("path") != args["path"]
                    or result.get("before_sha256") != args["expected_sha256"]
                    or not isinstance(expected_after, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_after)):
                raise RuntimeError("edit result identity mismatch")
            record["ok"] = True
            return json.dumps(result, sort_keys=True)
        except Exception as exc:
            record["error"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            durable_write(self.output / f"{self.calls:03d}.json", (json.dumps(record, sort_keys=True) + "\n").encode())


class DockerRepairBridge:
    """Controller capability grouping; no native tools or shell are exposed."""
    names = DockerReadBridge.names + DockerEditBridge.names

    def __init__(self, backend, output, *, max_reads=24, max_edits=16):
        self.backend = backend
        self.reads = DockerReadBridge(backend, Path(output) / "reads", max_calls=max_reads)
        self.edits = DockerEditBridge(backend, Path(output) / "edits", max_calls=max_edits)
        self.edits.reads = self.reads
        self.identity = {"read": self.reads.identity, "edit": self.edits.identity}

    def install(self, registry):
        self.reads.install(registry)
        self.edits.install(registry)


def repair_binding(backend, output, *, max_turns=8, max_reads=24, max_edits=16):
    """Bind the existing frozen-workspace candidate verifier, never an official score.

The caller owns provisioning, immutable image/base identity and cleanup. Neither
gold patches nor test answers are accepted by this model-facing capability.
"""
    if type(max_turns) is not int or not 1 <= max_turns <= 16:
        raise ValueError("finite model turn budget required")
    from .inspect_runtime_solver import ContainerCaseBinding
    from .models import VerifiedSubtaskConfig
    verifier = backend.verifier()
    if verifier.goal != "candidate_patch":
        raise ValueError("candidate workspace verifier required")
    bridge = DockerRepairBridge(backend, output, max_reads=max_reads, max_edits=max_edits)
    identity = hashlib.sha256(json.dumps(bridge.identity, sort_keys=True).encode()).hexdigest()
    node = VerifiedSubtaskConfig("fixture", "Produce a validated candidate patch", "Trusted isolated validation",
        (verifier.evidence_path,), backend.validator_id, "1", identity, verifier,
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), max_turns=max_turns)
    return ContainerCaseBinding(bridge, node)
