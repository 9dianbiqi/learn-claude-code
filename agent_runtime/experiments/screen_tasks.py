"""Eight synthetic screening fixtures; private checks/reference code stay outside agent repos."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from agent_runtime.models import VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult


TASKS = {
 'S1': {
  'category':'single-file',
  'files':{'ranges.py':'def merge_ranges(ranges):\n    raise NotImplementedError\n'},
  'nodes': [('finish','Implement merge_ranges(ranges) in ranges.py. Input is a finite iterable of (start,end) integer pairs. Reject start>end with ValueError. Return a sorted list of tuples merging overlapping closed intervals (including equal endpoints), but not intervals separated by a gap. Empty input returns []. Do not mutate input.',('ranges.py',),
             "from ranges import merge_ranges as f\nassert f([(4,6),(1,3),(3,4)])==[(1,6)]\nassert f([])==[]\n")],
  'oracle':"from ranges import merge_ranges as f\nx=[(5,5),(1,2),(3,4),(2,2)]\nassert f(x)==[(1,2),(3,4),(5,5)] and x==[(5,5),(1,2),(3,4),(2,2)]\nassert f(iter([(-3,-1),(-2,0)]))==[(-3,0)]\ntry: f([(3,1)])\nexcept ValueError: pass\nelse: raise AssertionError('invalid range')\n",
  'reference':{'ranges.py':"def merge_ranges(ranges):\n    items=list(ranges)\n    if any(a>b for a,b in items): raise ValueError('range')\n    result=[]\n    for a,b in sorted(items):\n        if result and a<=result[-1][1]: result[-1]=(result[-1][0],max(b,result[-1][1]))\n        else: result.append((a,b))\n    return result\n"}},
 'S2': {
  'category':'single-file',
  'files':{'flags.py':'def resolve_flag(config, env):\n    raise NotImplementedError\n'},
  'nodes':[('finish','Implement resolve_flag(config, env) in flags.py. If config contains enabled, its value must be exactly bool, else TypeError; it overrides env. Otherwise read env ENABLED, accepting a string (trim/casefold) true/1/yes or false/0/no. Invalid strings raise ValueError; nonstrings TypeError. If both keys absent return False. Do not mutate either dict.',('flags.py',),
            "from flags import resolve_flag as f\nassert f({}, {}) is False\nassert f({}, {'ENABLED':' YES '}) is True\nassert f({'enabled':False},{'ENABLED':'true'}) is False\n")],
  'oracle':"from flags import resolve_flag as f\na={'enabled':True}; b={'ENABLED':'bad'}\nassert f(a,b) is True and a=={'enabled':True} and b=={'ENABLED':'bad'}\nfor text in ['false','0',' NO ']: assert f({}, {'ENABLED':text}) is False\nfor value in [0,1,None,'true']:\n try: f({'enabled':value},{})\n except TypeError: pass\n else: raise AssertionError(value)\ntry: f({}, {'ENABLED':'maybe'})\nexcept ValueError: pass\nelse: raise AssertionError('invalid flag')\n",
  'reference':{'flags.py':"def resolve_flag(config,env):\n    if 'enabled' in config:\n        if type(config['enabled']) is not bool: raise TypeError('bool')\n        return config['enabled']\n    if 'ENABLED' not in env: return False\n    x=env['ENABLED']\n    if not isinstance(x,str): raise TypeError('str')\n    x=x.strip().casefold()\n    if x in ('true','1','yes'): return True\n    if x in ('false','0','no'): return False\n    raise ValueError('flag')\n"}},
 'M1': {
  'category':'multi-file',
  'files':{'units.py':'def convert(value, source, target):\n    raise NotImplementedError\n','service.py':'def total_ms(values, unit):\n    raise NotImplementedError\n'},
  'nodes':[('finish','Implement units.convert(value, source, target) supporting ms, s, min, with factors 1,1000,60000. Return numeric converted value; unknown unit raises ValueError. Implement service.total_ms(values, unit) using module-qualified units.convert at call time for each element of any iterable; empty yields 0. Do not mutate input. Both files must work together.',('units.py','service.py'),
            "from units import convert\nfrom service import total_ms\nassert convert(2,'s','ms')==2000\nassert total_ms([1,2],'min')==180000\n")],
  'oracle':"import units,service\nassert units.convert(1500,'ms','s')==1.5\nassert service.total_ms(iter([0,-1,2]),'s')==1000\nassert service.total_ms([],'s')==0\ntry: units.convert(1,'day','s')\nexcept ValueError: pass\nelse: raise AssertionError('unit')\nunits.convert=lambda *args: 7\nassert service.total_ms([1,2,3],'s')==21\n",
  'reference':{'units.py':"def convert(value,source,target):\n    factors={'ms':1,'s':1000,'min':60000}\n    if source not in factors or target not in factors: raise ValueError('unit')\n    return value*factors[source]/factors[target]\n",'service.py':"import units\ndef total_ms(values,unit):\n    return sum(units.convert(x,unit,'ms') for x in values)\n"}},
 'M2': {
  'category':'multi-file',
  'files':{'settings.py':'def load_config(mapping):\n    raise NotImplementedError\n','server.py':'def build_endpoint(mapping):\n    raise NotImplementedError\n'},
  'nodes':[('finish','Implement settings.load_config(mapping), returning a NEW dict with only host (default localhost) and port (default 8080). Host must be a nonempty string; bad host ValueError. Port must be exactly int (bool rejected) between 1 and 65535; wrong type TypeError, out of range ValueError. Implement server.build_endpoint(mapping) using settings.load_config, returning http://{host}:{port}. Ignore other keys, do not mutate mapping. Resolve settings.load_config via the module at call time.',('settings.py','server.py'),
            "from settings import load_config\nfrom server import build_endpoint\nassert load_config({})=={'host':'localhost','port':8080}\nassert build_endpoint({'host':'example','port':80})=='http://example:80'\n")],
  'oracle':"import settings,server\nx={'port':65535,'ignored':1}\nassert settings.load_config(x)=={'host':'localhost','port':65535} and x=={'port':65535,'ignored':1}\nfor v in [True,3.5,'80']:\n try: settings.load_config({'port':v})\n except TypeError: pass\n else: raise AssertionError(v)\nfor v in [0,65536]:\n try: settings.load_config({'port':v})\n except ValueError: pass\n else: raise AssertionError(v)\nsettings.load_config=lambda m: {'host':'patched','port':7}\nassert server.build_endpoint({})=='http://patched:7'\n",
  'reference':{'settings.py':"def load_config(mapping):\n    host=mapping.get('host','localhost'); port=mapping.get('port',8080)\n    if not isinstance(host,str) or not host: raise ValueError('host')\n    if type(port) is not int: raise TypeError('port')\n    if not 1<=port<=65535: raise ValueError('port')\n    return {'host':host,'port':port}\n",'server.py':"import settings\ndef build_endpoint(mapping):\n    c=settings.load_config(mapping)\n    return f\"http://{c['host']}:{c['port']}\"\n"}},
 'P1': {
  'category':'multi-stage',
  'files':{'pairs.py':'def parse_pair(line):\n    raise NotImplementedError\n','totals.py':'def aggregate(lines):\n    raise NotImplementedError\n'},
  'nodes':[
   ('parse','Implement pairs.parse_pair(line): a string with exactly one = separating a nonempty stripped name and stripped nonnegative ASCII integer. Return (name, int). Nonstring TypeError; malformed ValueError. Zero and leading zeros accepted.',('pairs.py',),"from pairs import parse_pair as p\nassert p(' A = 02 ')==('A',2)\nassert p('B=0')==('B',0)\n"),
   ('finish','Implement totals.aggregate(lines) using pairs.parse_pair at call time. Skip blank/whitespace-only STRING lines, sum quantities by name preserving first-seen order. Accept any iterable, do not mutate input, propagate parser errors. Empty returns {}.',('pairs.py','totals.py'),"from totals import aggregate\nassert aggregate(['A=2','','A=3','B=0'])=={'A':5,'B':0}\n")],
  'oracle':"import pairs,totals\nassert totals.aggregate(iter(['B=0','A=1','B=2']))=={'B':2,'A':1}\nassert list(totals.aggregate(['B=0','A=1']))==['B','A']\nfor s in ['=2','A=-1','A=١','A=1=2']:\n try: pairs.parse_pair(s)\n except ValueError: pass\n else: raise AssertionError(s)\npairs.parse_pair=lambda s: ('patched',4)\nassert totals.aggregate(['x','y'])=={'patched':8}\n",
  'reference':{'pairs.py':"import re\ndef parse_pair(line):\n    if not isinstance(line,str): raise TypeError('str')\n    parts=line.split('=')\n    if len(parts)!=2: raise ValueError('pair')\n    name,qty=(x.strip() for x in parts)\n    if not name or not re.fullmatch('[0-9]+',qty): raise ValueError('pair')\n    return name,int(qty)\n",'totals.py':"import pairs\ndef aggregate(lines):\n    out={}\n    for line in lines:\n        if isinstance(line,str) and not line.strip(): continue\n        name,q=pairs.parse_pair(line); out[name]=out.get(name,0)+q\n    return out\n"}},
 'P2': {
  'category':'multi-stage',
  'files':{'codec.py':'def pack(items):\n    raise NotImplementedError\n\ndef unpack(payload):\n    raise NotImplementedError\n','api.py':'def roundtrip_unique(names):\n    raise NotImplementedError\n'},
  'nodes':[
   ('codec','Implement codec.pack(items): accept a list of strings only, else TypeError, return JSON string preserving Unicode and item order. codec.unpack(payload): accept a string only (else TypeError); invalid JSON, non-list JSON or any nonstring member raises ValueError, else return the decoded list.',('codec.py',),"from codec import pack,unpack\nassert unpack(pack(['a','雪']))==['a','雪']\n"),
   ('finish','Implement api.roundtrip_unique(names). Accept any iterable of strings, remove duplicates preserving first-seen order, then return codec.unpack(codec.pack(unique_list)). Use module-qualified codec calls at call time. Do not mutate input. Nonstring members raise TypeError.',('codec.py','api.py'),"from api import roundtrip_unique as f\nassert f(['a','b','a'])==['a','b']\n")],
  'oracle':"import codec,api\nassert api.roundtrip_unique(iter(['雪','a','雪']))==['雪','a']\nfor s in ['{','{}','[1]']:\n try: codec.unpack(s)\n except ValueError: pass\n else: raise AssertionError(s)\ntry: api.roundtrip_unique(['a',1])\nexcept TypeError: pass\nelse: raise AssertionError('type')\ncodec.pack=lambda x: 'sentinel'\ncodec.unpack=lambda x: ['used'] if x=='sentinel' else []\nassert api.roundtrip_unique(['a'])==['used']\n",
  'reference':{'codec.py':"import json\ndef pack(items):\n    if not isinstance(items,list) or any(not isinstance(x,str) for x in items): raise TypeError('strings')\n    return json.dumps(items,ensure_ascii=False)\ndef unpack(payload):\n    if not isinstance(payload,str): raise TypeError('str')\n    result=json.loads(payload)\n    if not isinstance(result,list) or any(not isinstance(x,str) for x in result): raise ValueError('strings')\n    return result\n",'api.py':"import codec\ndef roundtrip_unique(names):\n    items=list(names)\n    if any(not isinstance(x,str) for x in items): raise TypeError('str')\n    return codec.unpack(codec.pack(list(dict.fromkeys(items))))\n"}},
 'U1': {
  'category':'external-state',
  'files':{'arithmetic.py':'def safe_divide(n, d):\n    raise NotImplementedError\n','average.py':'def mean(values):\n    raise NotImplementedError\n'},
  'prepare':('Restore arithmetic.safe_divide(n,d): raise ValueError on zero denominator, otherwise return n/d. This dependency must satisfy its contract even if externally modified.',('arithmetic.py',),"from arithmetic import safe_divide as f\nassert f(6,2)==3\ntry: f(1,0)\nexcept ValueError: pass\nelse: raise AssertionError('zero')\n"),
  'prefix_files':{'arithmetic.py':"def safe_divide(n,d):\n    if d==0: raise ValueError('zero')\n    return n/d\n"},
  'drift':{'arithmetic.py':"def safe_divide(n,d):\n    return n*d\n"},
  'nodes':[('finish','Implement average.mean(values) for any finite iterable of numbers, using arithmetic.safe_divide(sum, count) at call time. Empty input raises ValueError. Do not mutate input; preserve the safe_divide contract.',('arithmetic.py','average.py'),"from average import mean\nassert mean([1,2,3])==2\n")],
  'oracle':"import arithmetic,average\nassert arithmetic.safe_divide(3,2)==1.5\nassert average.mean(iter([-1,2]))==0.5\ntry: average.mean([])\nexcept ValueError: pass\nelse: raise AssertionError('empty')\narithmetic.safe_divide=lambda n,d: 123\nassert average.mean([1,2])==123\n",
  'reference':{'arithmetic.py':"def safe_divide(n,d):\n    if d==0: raise ValueError('zero')\n    return n/d\n",'average.py':"import arithmetic\ndef mean(values):\n    xs=list(values)\n    return arithmetic.safe_divide(sum(xs),len(xs))\n"}},
 'U2': {
  'category':'external-state',
  'files':{'config.json':'{"factor":2}\n','transform.py':'def scale(x):\n    raise NotImplementedError\n','batch.py':'def scale_all(values):\n    raise NotImplementedError\n'},
  'prepare':('Maintain transform.scale(x): load factor from config.json next to this module on EACH call and return x*factor. Config is external input: never edit config.json.',('transform.py','config.json'),"import json\nfrom pathlib import Path\nfrom transform import scale\nf=json.loads(Path('config.json').read_text())['factor']\nassert scale(3)==3*f\n"),
  'prefix_files':{'transform.py':"import json\nfrom pathlib import Path\ndef scale(x):\n    return x*json.loads(Path(__file__).with_name('config.json').read_text())['factor']\n"},
  'drift':{'config.json':'{"factor":5}\n'},
  'nodes':[('finish','Implement batch.scale_all(values) for any iterable of numbers using transform.scale at call time. Return a list in input order, empty yields [], do not mutate input or config.json. Respect current external configuration.',('transform.py','config.json','batch.py'),"import json\nfrom pathlib import Path\nfrom batch import scale_all\nf=json.loads(Path('config.json').read_text())['factor']\nassert scale_all([1,2])==[f,2*f]\n")],
  'oracle':"import json,transform,batch\nfrom pathlib import Path\nassert json.loads(Path('config.json').read_text())=={'factor':5}\nassert batch.scale_all(iter([-2,0,3]))==[-10,0,15]\nassert batch.scale_all([])==[]\ntransform.scale=lambda x:x+100\nassert batch.scale_all([1,2])==[101,102]\n",
  'reference':{'transform.py':"import json\nfrom pathlib import Path\ndef scale(x):\n    return x*json.loads(Path(__file__).with_name('config.json').read_text())['factor']\n",'batch.py':"import transform\ndef scale_all(values):\n    return [transform.scale(x) for x in values]\n"}}
}

for spec in TASKS.values():
    if 'prepare' not in spec:
        spec['files']['ready.txt']='not ready\n'
        spec['prepare']=('Confirm the fixture is prepared: ready.txt must contain exactly ready followed by a newline.',('ready.txt',),"from pathlib import Path\nassert Path('ready.txt').read_text()=='ready\\n'\n")
        spec['prefix_files']={'ready.txt':'ready\n'}


def execute_check(workspace: Path, program: str) -> dict:
    source='import sys\nsys.path.insert(0,sys.argv[1])\n'+program
    try:
        proc=subprocess.run([sys.executable,'-I','-B','-c',source,str(workspace)],cwd=workspace,
                            text=True,capture_output=True,timeout=10)
        return {'passed':proc.returncode==0,'diagnostic':proc.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        return {'passed':False,'diagnostic':'checker timeout'}


def make_dag(task):
    spec=TASKS[task]
    description,paths,program=spec['prepare']
    definitions=[('prepare',description,paths,program),*spec['nodes']]
    nodes=[]
    for index,(name,description,paths,program) in enumerate(definitions):
        def verifier(context, check_program=program, covered=paths):
            root=Path(context.repo_root)
            passed=execute_check(root,check_program)['passed']
            manifest=[{'path':p,'sha256':hashlib.sha256((root/p).read_bytes()).hexdigest()} for p in covered] if passed else []
            return VerifierResult('pass' if passed else 'fail','Published contract passed' if passed else 'Published contract failed; review requirements.',manifest)
        nodes.append(VerifiedSubtaskConfig(name,description,description,paths,f'screen-{task}-{name}','1',
                     'Frozen step contract checker',verifier,hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                     blocked_by=() if index==0 else (definitions[index-1][0],),max_turns=16))
    return VerifiedSubtaskDAGConfig(nodes=tuple(nodes))


def prompt(task):
    spec=TASKS[task]
    plan=[('prepare',spec['prepare'][0]),*[(n,d) for n,d,_,_ in spec['nodes']]]
    return ('Complete this frozen Python maintenance task in dependency order. Use file tools; read before editing. '
            'After tools finish, emit SUBTASK_COMPLETE on its own line in a TEXT-ONLY response, never with tools. '
            'The runtime then invokes the current checker. Existing evidence may be invalidated by external changes. '
            'Do not edit external config.json or the private checker.\nFrozen plan:\n'+
            '\n'.join(f'{name}: {description}' for name,description in plan))


def final_oracle(task, workspace):
    # Separate program from the model-triggered step verifiers.
    return execute_check(Path(workspace),TASKS[task]['oracle'])
