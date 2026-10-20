import hashlib
import numpy as np
import pytest
import nbtlib
from PIL import Image
from reconstruction import core

def wall():
    return {'schema_version':1,'dims':[12,8,12],'components':[
        {'id':1,'role':'wall','shape':'box','material':1,'min':[0,0,0],'max':[12,8,2]},
        {'id':2,'role':'support','shape':'rod','material':1,'start':[2.5,0,1],'end':[2.5,8,1],'radius':1}],
        'openings':[
            {'id':'door','bounds_min':[1,0,0],'bounds_max':[4,5,2],'fill_palette':0,'evidence':'user','confidence':1},
            {'id':'glass','bounds_min':[7,2,0],'bounds_max':[10,6,2],'fill_palette':2,'evidence':'observed','confidence':.9}]}

def cloud(path,xyz,rgb=None,provenance=None):
    data={'xyz':np.asarray(xyz,float),'provenance':np.zeros(len(xyz),np.uint8) if provenance is None else provenance}
    if rgb is not None: data['rgb']=np.asarray(rgb,np.uint8)
    np.savez(path,**data)

PAL={'blocks':[{'id':'stone','mean_rgb':[120,120,120]},{'id':'glass','mean_rgb':[220,240,245]}, {'id':'andesite','mean_rgb':[130,130,130]}]}

def test_openings_carved_after_support_and_glass_frozen(tmp_path):
    path=str(tmp_path/'g.npz'); core.build_geometry(wall(),path)
    d=np.load(path); occ={tuple(p):v for p,v in zip(d['positions'],d['palette_index'])}
    assert len(d['protected_air'])==30 and not set(map(tuple,d['protected_air'])) & set(occ)
    assert all(occ[tuple(p)]==2 for p in d['protected_openings'])
    src=tmp_path/'c.npz'; cloud(src,d['positions']+.5,np.tile([250,100,80],(len(occ),1)))
    out=str(tmp_path/'m.npz')
    report=core.map_materials(path,str(src),PAL,np.eye(4),{'allowed_by_component':{'1':[1,3],'2':[1,3]}},out)
    assert report['geometry_exact'] and core.validate_build(path)['geometry_hash']==core.validate_build(out)['geometry_hash']
    md=np.load(out); occ2={tuple(p):v for p,v in zip(md['positions'],md['palette_index'])}
    assert all(occ2[tuple(p)]==2 for p in md['protected_openings'])
    bad={k:md[k] for k in md.files}; idx=np.flatnonzero([tuple(p) in set(map(tuple,md['protected_openings'])) for p in md['positions']])[0]
    bad['palette_index']=bad['palette_index'].copy(); bad['palette_index'][idx]=1
    np.savez(tmp_path/'bad.npz',**bad)
    with pytest.raises(ValueError,match='fill conflict'): core.validate_build(str(tmp_path/'bad.npz'))

def test_symmetry_deduplicates_keeps_source_and_crop(tmp_path):
    p=tmp_path/'c.npz'; cloud(p,[[-3,2,0],[-3.01,2,0],[-3,0,0]],[[70,80,90]]*3)
    original=p.read_bytes(); out=str(tmp_path/'s.npz')
    rep=core.symmetry_cloud(str(p),[0,0],180,1,.2,out); d=np.load(out)
    assert rep['added']==1 and np.array_equal(d['xyz'][:3],np.load(p)['xyz'])
    assert d['provenance'].tolist()==[0,0,0,1] and d['donor_index'][-1]==0 and p.read_bytes()==original
    core.crop_cloud(out,[-4,-1,-1],[0,4,1],str(tmp_path/'crop.npz')); assert len(np.load(tmp_path/'crop.npz')['xyz'])==3
    assert core.inspect_cloud(str(p))==core.inspect_cloud(str(p))
    with pytest.raises(FileExistsError): core.symmetry_cloud(str(p),[0,0],180,1,.2,out)

@pytest.mark.parametrize('angle',[0,45])
def test_ring_fourfold_symmetry(tmp_path,angle):
    spec={'schema_version':1,'dims':[20,4,20],'components':[{'id':1,'role':'deck','shape':'ring','material':1,
        'min':[0,1,0],'max':[20,3,20],'center':[10,10],'outer_half':[6,6],'inner_half':[2,2],'angle_degrees':angle}],
        'symmetry':{'order':4,'center':[10,10]}}
    p=str(tmp_path/'r.npz'); core.build_geometry(spec,p)
    assert core.validate_build(p)['mismatch_ratio']==0
    assert not any(np.all(v==[10,1,10]) for v in np.load(p)['positions'])

def test_material_bounded_deterministic_observed_only(tmp_path):
    pos=np.stack(np.meshgrid(np.arange(20),np.arange(2),np.arange(20),indexing='ij'),-1).reshape(-1,3)
    g=str(tmp_path/'g.npz'); np.savez(g,positions=pos,palette_index=np.full(len(pos),3),component_id=np.ones(len(pos),int),dims=[20,2,20])
    theta=np.arctan2(pos[:,2]+.5-10,pos[:,0]+.5-10); lum=np.clip(128*np.exp(.25*np.cos(theta)+.15*((pos[:,0]%2)*2-1)),0,255)
    src=tmp_path/'c.npz'; cloud(src,np.r_[pos+.5,pos+.5],np.r_[np.repeat(lum[:,None],3,1),np.tile([255,0,0],(len(pos),1))],np.r_[np.zeros(len(pos),np.uint8),np.ones(len(pos),np.uint8)])
    pal={'blocks':[{'id':n,'mean_rgb':[v]*3} for n,v in zip(['stone','andesite','smooth_stone','polished_andesite','light_gray_concrete'],[100,115,128,140,155])]}
    policy={'k':1,'allowed_by_component':{'1':[1,2,3,4,5]}}
    report=core.map_materials(g,str(src),pal,np.eye(4),policy,str(tmp_path/'a.npz')); core.map_materials(g,str(src),pal,np.eye(4),policy,str(tmp_path/'b.npz'))
    a=np.load(tmp_path/'a.npz'); b=np.load(tmp_path/'b.npz')
    assert report['supported']==len(pos) and report['per_component']['1']['fitted']
    assert np.array_equal(a['palette_index'],b['palette_index']) and np.allclose(a['sampled_rgb'][:,0],lum.astype(np.uint8))
    assert .85<=np.min(a['brightness_factor'])<=np.max(a['brightness_factor'])<=1.15
    assert np.std(a['brightness_factor'])>0 and np.max(np.abs(a['illumination_logfield']))<=.18
    assert core.validate_build(g)['geometry_hash']==core.validate_build(str(tmp_path/'a.npz'))['geometry_hash']
    no=tmp_path/'none.npz'; cloud(no,pos+.5)
    r=core.map_materials(g,str(no),pal,np.eye(4),policy,str(tmp_path/'fallback.npz'))
    assert r['supported']==0 and np.all(np.load(tmp_path/'fallback.npz')['palette_index']==3)

def test_sponge_true_varints_and_air_order(tmp_path):
    pos=np.c_[np.arange(130),np.zeros(130,int),np.zeros(130,int)].astype(int)
    p=str(tmp_path/'g.npz'); np.savez(p,positions=pos,palette_index=np.arange(1,131),component_id=np.ones(130,int),dims=[131,1,1])
    pal={'blocks':[{'id':f'minecraft:test_{i}','mean_rgb':[100,100,100]} for i in range(130)]}
    out=str(tmp_path/'b.schem'); report=core.export_schem(p,pal,5023,out); tree=nbtlib.load(out,gzipped=True)['Schematic']
    assert report['roundtrip'] and report['palette_size']==131 and isinstance(tree['Offset'],nbtlib.IntArray)
    data=np.asarray(tree['Blocks']['Data']).view(np.uint8).tobytes(); values=[]; cursor=0
    for _ in range(131): value,cursor=core._read_varint(data,cursor); values.append(value)
    assert values==list(range(1,131))+[0] and cursor==len(data) and len(data)>131

def test_preprocess_dimensions_mask_original(tmp_path):
    p=tmp_path/'photo.png'; img=np.full((48,64,3),110,np.uint8); img[10:15,10:15]=255; Image.fromarray(img).save(p)
    digest=hashlib.sha256(p.read_bytes()).hexdigest(); out=tmp_path/'photos'
    core.preprocess_photos([{'source':str(p),'polygon':[[0,0],[32,0],[32,48],[0,48]]}],str(out))
    assert Image.open(out/'images'/'photo.png').size==(64,48) and hashlib.sha256(p.read_bytes()).hexdigest()==digest
    mask=np.asarray(Image.open(out/'masks'/'photo.png.png')); assert mask[12,12]==0 and mask[30,10]==255 and mask[30,50]==0

def test_invalid_and_conflicting_openings(tmp_path):
    spec=wall(); spec['openings'].append({**spec['openings'][0],'id':'conflict','fill_palette':2})
    with pytest.raises(ValueError,match='different fills'): core.build_geometry(spec,str(tmp_path/'o.npz'))
    spec=wall(); spec['dims']=[12.5,8,12]
    with pytest.raises(ValueError): core.build_geometry(spec,str(tmp_path/'x.npz'))
    spec=wall(); spec['components'][0]['shape']='blob'
    with pytest.raises(ValueError): core.build_geometry(spec,str(tmp_path/'y.npz'))
