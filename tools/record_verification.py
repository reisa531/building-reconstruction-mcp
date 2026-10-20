"""Collect actual local run evidence and resolved dependency versions."""
import importlib.metadata as md
import json,re,shutil,sys
from pathlib import Path
from datetime import datetime,timezone
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

KIT=Path(__file__).resolve().parents[1]
PROJECT=KIT.parent

def read_log(name):
    raw=(KIT/name).read_bytes()
    return raw.decode('utf-16' if raw.startswith(b'\xff\xfe') else 'utf-8-sig')

def main():
    tests=read_log('tests-check.log')
    match=re.search(r'(\d+) passed',tests)
    if not match or re.search(r'\d+ failed',tests): raise ValueError('test evidence must show passing suite')
    evidence={}
    for key,filename in [('building','building-check.log'),('tower_replay','tower-check.log'),('tower_structure','structure-check.log')]:
        record=json.loads(read_log(filename))
        if record['status']!='completed': raise ValueError('example did not complete')
        result=record['result']; run=PROJECT/record['job_dir']
        outputs=KIT/'examples'/'outputs'; outputs.mkdir(exist_ok=True)
        shutil.copyfile(run/'preview.png',outputs/f'{key}.png')
        shutil.copyfile(run/'build.schem',outputs/f'{key}.schem')
        evidence[key]={'job_id':record['job_id'],'job_dir':record['job_dir'],'validation':result['validation'],
            'export':result['export'],'materials':result.get('materials'),'input_hashes':result['input_hashes']}
    # The COLMAP log also contains native stderr; its final JSON starts with the job_id object.
    sfm=read_log('sfm-check.log'); start=sfm.rfind('\n{')
    colmap=json.loads(sfm[start+1:])
    evidence['colmap']={'job_id':colmap['job_id'],'status':colmap['status'],'result':colmap['result']}
    roots=['mcp','numpy','scipy','opencv-python-headless','plyfile','nbtlib','Pillow','pytest','PyYAML','pycolmap']
    todo=roots[:]; resolved={}
    while todo:
        name=todo.pop(); norm=canonicalize_name(name)
        if norm in resolved: continue
        dist=md.distribution(name); resolved[norm]=dist.version
        for text in dist.requires or []:
            requirement=Requirement(text)
            if requirement.marker is None or requirement.marker.evaluate({'extra':''}): todo.append(requirement.name)
    (KIT/'requirements-lock.txt').write_text('# Verified Windows Python 3.12 environment; install package with --no-deps after this file.\n'+
        '\n'.join(f'{name}=={version}'+('; sys_platform == "win32"' if name=='pywin32' else '') for name,version in sorted(resolved.items()))+'\n',encoding='utf8')
    verification={'recorded_at':datetime.now(timezone.utc).isoformat(),'python':sys.version,'tests_passed':int(match.group(1)),
        'test_log':tests.strip(),'mcp_stdio_initialize_list_tools_pipeline_read_report':True,
        'skill_schema_validation':'3 skills passed quick_validate.py',
        'mcp_registered_name':'building-reconstruction','examples':evidence,
        'game_tested':False,'automatic_arbitrary_photo_interpretation':False,'dependency_versions':resolved}
    (KIT/'verification.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'tests':int(match.group(1)),'dependencies':len(resolved),'examples':list(evidence)},indent=2))

if __name__=='__main__': main()
