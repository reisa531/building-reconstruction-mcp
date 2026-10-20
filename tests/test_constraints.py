import numpy as np
import pytest
from reconstruction import core
from reconstruction.jobs import JobManager,atomic_json

def test_rotated_opening_and_nonvanishing_requirement(tmp_path):
    spec={'schema_version':1,'dims':[12,6,12],'components':[{'id':1,'shape':'box','role':'test','material':1,'min':[0,0,0],'max':[12,6,12]}],
        'openings':[{'id':'rotated','bounds_min':[4,1,3],'bounds_max':[8,5,9],'angle_degrees':45,'fill_palette':0,'evidence':'user','confidence':1}]}
    path=str(tmp_path/'g.npz'); core.build_geometry(spec,path); d=np.load(path)
    assert len(d['protected_air'])>0 and not set(map(tuple,d['protected_air'])) & set(map(tuple,d['positions']))
    # A rotated rectangle must carve a different cell set from its axis-aligned bounding box.
    assert any(p[0]<4 or p[0]>=8 for p in d['protected_air'])
    spec['components'][0]['max']=[2,2,2]
    with pytest.raises(ValueError,match='vanishes'): core.build_geometry(spec,str(tmp_path/'no.npz'))

def test_live_process_job_not_mislabelled_interrupted(tmp_path):
    import os
    manager=JobManager(tmp_path); jid='1'*32; directory=manager.jobdir(jid); directory.mkdir()
    atomic_json(directory/'job.json',{'job_id':jid,'status':'running','operation':'build_geometry','owner_pid':os.getpid()})
    assert manager.load(jid)['status']=='running'
    manager.pool.shutdown()

def test_point_transform_not_shifted_by_half_voxel(tmp_path):
    g=str(tmp_path/'g.npz'); np.savez(g,positions=np.array([[0,0,0]]),palette_index=[1],component_id=[1],dims=[1,1,1])
    src=str(tmp_path/'c.npz'); np.savez(src,xyz=[[.5,.5,.5]],rgb=[[100,100,100]])
    pal={'blocks':[{'id':'stone','mean_rgb':[100,100,100]}]}
    report=core.map_materials(g,src,pal,np.eye(4),{'nearest_distance':.1},str(tmp_path/'m.npz'))
    assert report['supported']==1 and np.load(tmp_path/'m.npz')['source_nearest_distance'][0]==0
