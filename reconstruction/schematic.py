"""Sponge schematic v3 export, validated with independent NBT decoding."""
import gzip
import numpy as np
from nbtlib import File, Compound, Int, Short, IntArray, ByteArray, List, String


def export_schem(geometry,palette,data_version,output):
    from . import core as c
    validation=c.validate_build(geometry)
    if type(data_version) is not int or data_version<=0: raise ValueError('positive DataVersion required')
    g=c._geom_from(geometry); blocks=c.checked_palette(palette)
    dims=g['dims'].astype(int); W,H,L=map(int,dims)
    if np.any(dims>32767): raise ValueError('dimensions exceed NBT short')
    ids=g['palette_index'].astype(int); pos=g['positions'].astype(int)
    if len(ids) and ids.max()>len(blocks): raise ValueError('material index exceeds palette')
    names=['minecraft:air']; mapping={0:0}
    for i in np.unique(ids):
        name=blocks[i-1]['id']
        if ':' not in name.split('[')[0]: name='minecraft:'+name
        if name in ('minecraft:air','minecraft:cave_air','minecraft:void_air'): raise ValueError('occupied geometry cannot use air material')
        if name not in names: names.append(name)
        mapping[int(i)]=names.index(name)
    flat=np.zeros(W*H*L,np.uint32)
    if len(pos): flat[pos[:,0]+pos[:,2]*W+pos[:,1]*W*L]=[mapping[int(i)] for i in ids]
    data=b''.join(c._varint(int(i)) for i in flat)
    tree=Compound({'Version':Int(3),'DataVersion':Int(data_version),
        'Width':Short(W),'Height':Short(H),'Length':Short(L),'Offset':IntArray([-(W//2),0,-(L//2)]),
        'Metadata':Compound({'Name':String('Reconstructed building')}),
        'Blocks':Compound({'Palette':Compound({name:Int(i) for i,name in enumerate(names)}),
            'Data':ByteArray(np.frombuffer(data,dtype=np.int8)),'BlockEntities':List[Compound]([])}),
        'Entities':List[Compound]([])})
    import io
    memory=io.BytesIO(); File({'Schematic':tree},root_name='').write(memory)
    with c._exclusive_open(output) as f: f.write(gzip.compress(memory.getvalue(),mtime=0))
    import nbtlib
    back=nbtlib.load(output,gzipped=True)['Schematic']
    encoded=np.asarray(back['Blocks']['Data']).astype(np.int8).view(np.uint8).tobytes()
    values=[]; offset=0
    for _ in range(W*H*L):
        v,offset=c._read_varint(encoded,offset); values.append(v)
    if offset!=len(encoded) or not np.array_equal(values,flat): raise AssertionError('schematic varint roundtrip failed')
    if not isinstance(back['Offset'],IntArray): raise AssertionError('invalid Sponge offset tag')
    return {'output':output,'data_version':data_version,'version':3,'geometry_hash':validation['geometry_hash'],
        'palette_size':len(names),'block_count':len(pos),'roundtrip':True,'game_tested':False}
