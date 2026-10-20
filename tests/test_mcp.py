import asyncio,json,os,sys
from pathlib import Path
import numpy as np
import pytest
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from reconstruction.jobs import JobManager,atomic_json
from test_core import wall,PAL

def test_real_stdio_pipeline_and_reports(tmp_path):
    async def run():
        env=dict(os.environ); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]); env['PYTHONUTF8']='1'
        params=StdioServerParameters(command=sys.executable,args=['-m','reconstruction.server','--root',str(tmp_path)],env=env)
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as s:
                await s.initialize()
                assert {'start_job','capabilities','get_job','read_report'} <= {t.name for t in (await s.list_tools()).tools}
                async def call(name,args):
                    res=await s.call_tool(name,args)
                    return res,None if res.isError else json.loads(next(c.text for c in res.content if c.type=='text'))
                _,start=await call('start_job',{'operation':'run_pipeline','parameters':{'spec':wall(),'palette':PAL,'data_version':5023}})
                jid=start['job_id']; job=None
                for _ in range(300):
                    _,job=await call('get_job',{'job_id':jid})
                    if job['status'] in {'completed','failed','interrupted'}: break
                    await asyncio.sleep(.1)
                assert job['status']=='completed',job
                d=tmp_path/'reconstruction-runs'/jid
                assert (d/'build.schem').exists() and (d/'preview.png').exists()
                g=np.load(d/'geometry.npz'); assert not set(map(tuple,g['protected_air'])) & set(map(tuple,g['positions']))
                _,report=await call('read_report',{'path':f'reconstruction-runs/{jid}/pipeline.json'})
                assert report['validation']['protected_air']==30 and report['export']['roundtrip']
                for name,args in [('start_job',{'operation':'invalid','parameters':{}}),('get_job',{'job_id':'../bad'}),('read_report',{'path':'../outside.json'}),('start_job',{'operation':'import_voxels','parameters':{'source':'../outside.npz'}})]:
                    result,_=await call(name,args); assert result.isError
                assert any(str(r.uri)=='reconstruction://capabilities' for r in (await s.list_resources()).resources)
        manager=JobManager(tmp_path); assert manager.load(jid)['status']=='completed'; manager.pool.shutdown()
    asyncio.run(run())

def test_restart_interrupted(tmp_path):
    manager=JobManager(tmp_path); jid='0'*32; directory=manager.jobdir(jid); directory.mkdir()
    atomic_json(directory/'job.json',{'job_id':jid,'status':'running','operation':'build_geometry'})
    assert manager.load(jid)['status']=='interrupted'
    with pytest.raises(ValueError): manager.load('../x')
    manager.pool.shutdown()
