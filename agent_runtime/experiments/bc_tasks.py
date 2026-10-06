"""Two small synthetic coding tasks. Private checks never enter the workspace."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from agent_runtime.models import VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult

TASKS = {
    "duration": {
        "files": {
            "duration.py": "def parse_duration(text):\n    raise NotImplementedError\n",
            "settings.py": "def load_timeout(settings):\n    raise NotImplementedError\n",
            "README.md": "Implement duration parsing and timeout configuration. See the supplied plan for the contract.\n",
        },
        "steps": [
            ("parser", "Implement parse_duration(text) in duration.py. Accept only strings containing a nonnegative ASCII integer immediately followed by ms, s, or m, allowing surrounding whitespace. Return integer milliseconds. Reject malformed strings with ValueError, nonstrings with TypeError. Zero and leading zeros are valid.", ("duration.py",)),
            ("integration", "Implement load_timeout(settings) in settings.py using duration.parse_duration. settings is a dict. If timeout is absent return 30000; otherwise parse it and require a result greater than zero, raising ValueError for zero. Preserve parser TypeError/ValueError behavior. Do not mutate settings or change parse_duration's API.", ("settings.py", "duration.py")),
        ],
        "checks": [
            "from duration import parse_duration as p\nassert p(' 2s ')==2000\nassert p('03m')==180000\nassert p('0ms')==0\nfor x in ['-1s','1.5s','2 h','s','1S','', '1 s','١s']:\n try: p(x)\n except ValueError: pass\n else: raise AssertionError(repr(x))\nfor x in [None, 2, True]:\n try: p(x)\n except TypeError: pass\n else: raise AssertionError(repr(x))\n",
            "from settings import load_timeout as f\nassert f({})==30000\nd={'timeout':'2m','other':3}\nassert f(d)==120000 and d=={'timeout':'2m','other':3}\nassert f({'timeout':'1ms'})==1\nfor x in ['0s','-1s','bad']:\n try: f({'timeout':x})\n except ValueError: pass\n else: raise AssertionError(repr(x))\ntry: f({'timeout':None})\nexcept TypeError: pass\nelse: raise AssertionError('None')\n",
        ],
    },
    "inventory": {
        "files": {
            "records.py": "def parse_record(line):\n    raise NotImplementedError\n",
            "inventory.py": "def aggregate(lines):\n    raise NotImplementedError\n",
            "README.md": "Implement inventory record parsing and aggregation. See the supplied plan for the contract.\n",
        },
        "steps": [
            ("parser", "Implement parse_record(line) in records.py. Accept a string of exactly two comma-separated fields: SKU and quantity. Strip whitespace from each field. SKU must be nonempty; quantity must contain only ASCII digits and represent a nonnegative integer. Return (SKU, quantity). Reject malformed values with ValueError and nonstrings with TypeError. Preserve SKU case; zero and leading zeros are valid.", ("records.py",)),
            ("integration", "Implement aggregate(lines) in inventory.py using records.parse_record. Accept any iterable of record strings, sum quantities per SKU, return a dict preserving first-seen SKU insertion order, including zero-quantity SKUs. Empty iterable returns {}. Propagate parser errors. Do not change parse_record's API.", ("inventory.py", "records.py")),
        ],
        "checks": [
            "from records import parse_record as p\nassert p(' A-1 , 002 ')==('A-1',2)\nassert p('x,0')==('x',0)\nfor x in ['a,-1','a,+1','a,1.2',',2','a,','a,1,2','a,١','a 2']:\n try: p(x)\n except ValueError: pass\n else: raise AssertionError(repr(x))\nfor x in [None,1]:\n try: p(x)\n except TypeError: pass\n else: raise AssertionError(repr(x))\n",
            "from inventory import aggregate as f\nassert f([])=={}\nr=f(iter(['B,2','A,0','B,3','a,1']))\nassert r=={'B':5,'A':0,'a':1} and list(r)==['B','A','a']\ntry: f(['A,2','bad'])\nexcept ValueError: pass\nelse: raise AssertionError('must propagate')\n",
        ],
    },
}


def check(task: str, workspace: Path, stage: int | None = None) -> dict:
    checks = TASKS[task]["checks"]
    source = "\n".join(checks if stage is None else checks[:stage + 1])
    # Run fresh imports outside the agent process; no private test files in its repo.
    program = "import sys\nsys.path.insert(0, sys.argv[1])\n" + source
    try:
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(workspace)],
                                capture_output=True, text=True, timeout=10)
        return {"passed": result.returncode == 0, "returncode": result.returncode,
                "diagnostic": result.stderr[-3000:]}
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": None, "diagnostic": "checker timeout"}


def make_dag(task: str) -> VerifiedSubtaskDAGConfig:
    nodes = []
    for index, (name, description, paths) in enumerate(TASKS[task]["steps"]):
        def verify(context, stage=index, evidence_paths=paths):
            workspace = Path(context.repo_root)
            result = check(task, workspace, stage)
            manifest = [{"path": path, "sha256": hashlib.sha256((workspace/path).read_bytes()).hexdigest()}
                        for path in evidence_paths if (workspace/path).is_file()]
            return VerifierResult("pass" if result["passed"] else "fail",
                                  "Contract checks passed" if result["passed"] else "Contract checks failed; review the published contract.",
                                  manifest if result["passed"] else [])
        nodes.append(VerifiedSubtaskConfig(
            subtask_id=name, description=description, completion_criteria=description,
            evidence_paths=paths, verifier_id=f"bc-pilot-{task}-{name}", verifier_version="1",
            verification_rule="Frozen private Python assertions for the published contract",
            verifier=verify, verifier_implementation_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            blocked_by=() if index == 0 else ("parser",), max_turns=16))
    return VerifiedSubtaskDAGConfig(nodes=tuple(nodes))


def prompt(task: str) -> str:
    return ("Complete this two-step Python coding task in dependency order using file tools. "
            "Read before editing. Only edit the supplied .py files. Do not access runtime metadata. "
            "After each subtask, wait for every tool result, then emit SUBTASK_COMPLETE on a separate line "
            "with a short summary in a TEXT-ONLY response with ZERO tool calls. NEVER write that marker "
            "in a response containing tool calls, not even in an explanation. "
            "the runtime will verify it and supply the next subtask.\nFrozen plan:\n" +
            "\n".join(f"{name}: {description}" for name, description, _ in TASKS[task]["steps"]))
