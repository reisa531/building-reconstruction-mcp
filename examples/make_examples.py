"""Recreate explicit example inputs. No source asset modification."""
import json,math
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent.parent

def write(name,data):
    (HERE/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')

def box(cid,role,lo,hi,material=1):
    return {'id':cid,'role':role,'shape':'box','min':lo,'max':hi,'material':material,'evidence':'inferred'}

def main():
    palette={'blocks':[{'id':'minecraft:light_gray_concrete','mean_rgb':[125,125,115]},
        {'id':'minecraft:glass','mean_rgb':[200,225,230]}, {'id':'minecraft:smooth_stone','mean_rgb':[158,158,158]}]}
    write('building-palette.json',palette)
    parts=[box(1,'wall',[2,0,2],[26,24,4]),box(2,'wall',[2,0,20],[26,24,22]),
        box(3,'wall',[2,0,4],[4,24,20]),box(4,'wall',[24,0,4],[26,24,20])]
    for y in [0,8,16,24]: parts.append(box(len(parts)+1,'floor',[2,y,2],[26,y+1,22],3))
    openings=[]
    for base in [0,8,16]:
        for z in [2,20]:
            for x in [4,12,20]:
                if base==0 and z==2 and x==12: continue
                parts.append(box(len(parts)+1,'window_frame',[x-1,base+2,z],[x+5,base+7,z+2],3))
                openings.append({'id':f'window-{base}-{z}-{x}','bounds_min':[x,base+3,z],
                    'bounds_max':[x+4,base+6,z+2],'fill_palette':2,'evidence':'inferred','confidence':.7})
    openings.append({'id':'entrance','bounds_min':[12,1,2],'bounds_max':[16,6,4],
        'fill_palette':0,'evidence':'user','confidence':1})
    write('building-structure.json',{'schema_version':1,'dims':[28,26,24],'components':parts,'openings':openings})
    write('building-pipeline.json',{'spec_path':'reconstruction-kit/examples/building-structure.json',
        'palette_path':'reconstruction-kit/examples/building-palette.json','data_version':5023})
    # Explicit approximation; current historic tower replay is a separate example.
    parts=[box(1,'base',[9,0,15],[33,3,39],8)]
    floors=[(21,13),(38,13),(55,13),(72,12),(89,12),(106,11),(124,11),(139,9)]
    for idx,(y,radius) in enumerate(floors):
        parts.append({'id':len(parts)+1,'role':'platform','shape':'ring','material':8,
            'min':[0,y,0],'max':[42,y+2,54],'center':[21,27],
            'outer_half':[radius,radius],'inner_half':[4,4],'angle_degrees':45*(idx%2),'evidence':'user'})
    levels=[(3,8)]+floors+[(157,5),(178,2)]
    for (a,ra),(b,rb) in zip(levels,levels[1:]):
        for k in range(4):
            angle=math.pi/2*k
            for twist in [-math.pi/2,math.pi/2]:
                parts.append({'id':len(parts)+1,'role':'support','shape':'rod','material':1,
                    'start':[21+ra*math.cos(angle),a,27+ra*math.sin(angle)],
                    'end':[21+rb*math.cos(angle+twist),b,27+rb*math.sin(angle+twist)],'radius':.9,'evidence':'inferred'})
    parts.append({'id':len(parts)+1,'role':'antenna','shape':'rod','material':1,'start':[21,175,27],'end':[21,191,27],'radius':1,'evidence':'inferred'})
    write('tower-structure.json',{'schema_version':1,'dims':[42,192,54],'components':parts,'openings':[],
        'symmetry':{'order':4,'center':[21,27]}})
    project_path=ROOT/'project.json'
    if project_path.exists():
        from reconstruction.core import _read_cloud
        project=json.loads(project_path.read_text(encoding='utf8'))
        source=project['material_reference']; xyz,*_= _read_cloud(str(ROOT/source))
        mn,mx=xyz.min(0),xyz.max(0); s=191/(mx[2]-mn[2])
        T=[[s,0,0,-mn[0]*s],[0,0,s,-mn[2]*s],[0,-s,0,mx[1]*s],[0,0,0,1]]
        geom=str(Path(project['minecraft_square_platform_geometry'])/'regularized_voxels.npz').replace('\\','/')
        pal=str(Path(project['minecraft_mottled_build'])/'palette.json').replace('\\','/')
        write('tower-palette.json',json.loads((ROOT/pal).read_text(encoding='utf8')))
        pal='reconstruction-kit/examples/tower-palette.json'
        allowed={str(k):v for k,v in {1:[8,7,9,3],2:[1,2,3,10],3:[8,7,9,3],4:[9,11,7],5:[3,8,10],6:[3,8,10],7:[1,2,3,10],8:[1,2,3,10]}.items()}
        write('tower-replay.json',{'geometry_source':geom,'palette_path':pal,'cloud':source,'transform':T,
            'data_version':5023,'policy':{'allowed_by_component':allowed,'brightness_retention':.25,'chroma_retention':.15,'brightness_limits':[.85,1.15]}})
        write('tower-structure-pipeline.json',{'spec_path':'reconstruction-kit/examples/tower-structure.json','palette_path':pal,'data_version':5023})

if __name__=='__main__': main()
