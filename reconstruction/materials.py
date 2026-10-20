"""Conservative, deterministic appearance mapping; not inverse rendering."""
import json
import numpy as np
import cv2
from scipy.optimize import least_squares
from scipy.spatial import cKDTree


def map_materials(geometry, cloud, palette, transform, policy, output):
    from . import core as c
    before = c.validate_build(geometry)
    g = c._geom_from(geometry)
    blocks = c.checked_palette(palette)
    means = np.asarray([b['mean_rgb'] for b in blocks], float)
    base = g['palette_index'].astype(int)
    if len(base) and base.max() > len(blocks): raise ValueError('base material exceeds palette')
    T = np.asarray(transform, float)
    if T.shape != (4,4) or not np.isfinite(T).all() or not np.allclose(T[3], [0,0,0,1]):
        raise ValueError('transform must be finite affine 4x4')
    p = dict(policy or {})
    allowed = p.get('allowed_by_component', {})
    strength = float(p.get('angular_strength', .5))
    gain = float(p.get('max_log_gain', .18))
    bret = float(p.get('brightness_retention', .25))
    cret = float(p.get('chroma_retention', .15))
    limits = np.asarray(p.get('brightness_limits', [.85,1.15]), float)
    radius = float(p.get('nearest_distance', 4))
    k = int(p.get('k',16)); minimum = int(p.get('min_samples',80))
    if not all(np.isfinite(v) and 0 <= v <= 1 for v in [strength,bret,cret]):
        raise ValueError('retention and strength must lie in [0,1]')
    if limits.shape != (2,) or not np.isfinite(limits).all() or not 0 < limits[0] <= 1 <= limits[1]:
        raise ValueError('brightness_limits must straddle 1 and be positive')
    if not np.isfinite(gain) or gain < 0 or not np.isfinite(radius) or radius <= 0 or k < 1 or minimum < 4:
        raise ValueError('invalid material sampling policy')
    xyz, rgb, prov, donor, _ = c._read_cloud(cloud)
    observed = (prov == 0) & c.cloud_has_rgb(cloud)
    pos = g['positions'].astype(int); centers = pos + .5
    samples = np.full((len(pos),3),np.nan); distances = np.full(len(pos),np.inf)
    if observed.any() and len(pos):
        mc = np.c_[xyz[observed],np.ones(observed.sum())] @ T.T
        dd, ii = cKDTree(mc[:,:3]).query(centers,k=min(k,observed.sum()))
        if dd.ndim == 1: dd,ii=dd[:,None],ii[:,None]
        distances = dd[:,0]
        samples = np.median(rgb[observed][ii],axis=1)
    confident = (distances <= radius) & np.isfinite(samples).all(axis=1)
    frozen_set = c._key_set(g.get('protected_openings',np.empty((0,3))))
    frozen = np.array([tuple(v) in frozen_set for v in pos],bool)
    eligible = confident & ~frozen
    out = base.copy(); correction=np.zeros(len(pos)); factor=np.ones(len(pos))
    targets = means[base-1].copy()
    lab_palette = c._srgb2lab(means)
    stats={}
    for cid in np.unique(g['component_id']):
        group = g['component_id']==cid
        allowed_ids = np.asarray(allowed.get(str(int(cid)),sorted(set(base[group]))),int)
        if allowed_ids.ndim!=1 or not len(allowed_ids) or np.any(allowed_ids<1) or np.any(allowed_ids>len(blocks)):
            raise ValueError('allowed_by_component has invalid material index')
        rows=np.flatnonzero(group & eligible)
        coeff=np.zeros(4); fitted=False
        if len(rows):
            rgb_rows=samples[rows]
            log_y=np.log(np.maximum((rgb_rows/255) @ [.2126,.7152,.0722],.02))
            theta=np.arctan2(centers[rows,2]-g['dims'][2]/2,centers[rows,0]-g['dims'][0]/2)
            X=np.c_[np.ones(len(rows)),np.cos(theta),np.sin(theta),centers[rows,1]/g['dims'][1]]
            if len(rows)>=minimum and np.linalg.matrix_rank(X)==4:
                result=least_squares(lambda b:X@b-log_y,np.zeros(4),loss='soft_l1',f_scale=.1,max_nfev=200)
                if not result.success: raise ValueError('illumination fit did not converge')
                coeff=result.x; fitted=True
            remove=np.clip(strength*(coeff[1]*np.cos(theta)+coeff[2]*np.sin(theta)),-gain,gain)
            correction[rows]=-remove
            residual=log_y-remove
            factor[rows]=np.clip(np.exp(bret*(residual-np.median(residual))),*limits)
            target=means[base[rows]-1]*factor[rows,None]
            source_lab=c._srgb2lab(rgb_rows); target_lab=c._srgb2lab(np.clip(target,0,255))
            target_lab[:,1:]+=cret*(source_lab[:,1:]-np.median(source_lab[:,1:],axis=0))
            targets[rows]=np.clip(cv2.cvtColor(target_lab.astype(np.float32).reshape(-1,1,3),cv2.COLOR_Lab2RGB).reshape(-1,3)*255,0,255)
            target_lab=c._srgb2lab(targets[rows])
            nearest=np.argmin(np.sum((target_lab[:,None]-lab_palette[allowed_ids-1][None])**2,axis=2),axis=1)
            out[rows]=allowed_ids[nearest]
        stats[str(int(cid))]={'eligible':len(rows),'fitted':fitted,'coefficients_intercept_cos_sin_height':coeff.tolist(),
            'allowed':allowed_ids.tolist(),'materials':{blocks[i-1]['id']:int((out[group]==i).sum()) for i in np.unique(out[group])}}
    assert np.array_equal(out[~eligible],base[~eligible])
    meta=json.loads(g.get('metadata','{}'))
    report={'heuristic_not_albedo':True,'no_emitted_light':True,'only_measured_colors':True,
        'supported':int(confident.sum()),'eligible':int(eligible.sum()),'total':len(pos),
        'unsupported_base_retained':int((~confident).sum()),'changed_material_blocks':int((out!=base).sum()),
        'policy':{**p,'angular_strength':strength,'max_log_gain':gain,'brightness_retention':bret,'chroma_retention':cret,'brightness_limits':limits.tolist()},
        'per_component':stats,'geometry_hash':before['geometry_hash'],'game_tested':False}
    meta['materials']=report
    c._geom_write(output,{**g,'palette_index':out.astype(np.uint32),'sampled_rgb':samples,
        'target_rgb':targets,'source_color_supported':confident,'source_nearest_distance':distances,
        'illumination_logfield':correction,'brightness_factor':factor,'metadata':json.dumps(meta,sort_keys=True)})
    after=c.validate_build(output)
    if before['geometry_hash']!=after['geometry_hash']: raise AssertionError('material stage changed geometry')
    return {**report,'output':output,'geometry_exact':True}
