"""Functional reconstruction core. Pure numpy/scipy/cv2/plyfile/nbtlib/PIL stages.

Each stage writes NEW output files (exclusive create / refuse existing). No path policy here.
Common geometry NPZ keys: positions (int32 Nx3 MCXYZ), palette_index (uint32, 1-based),
component_id (int32), dims ([W,H,L]), protected_air (Kx3), protected_openings (Mx3),
expected_opening_palette (M) uint32, metadata (scalar unicode JSON).
"""
import hashlib, json, os, struct
import re
import numpy as np
from plyfile import PlyData
import cv2
import scipy.ndimage as ndi
from scipy.optimize import least_squares
from scipy.spatial import cKDTree

GEOM_VERSION = "reconstruction-geometry/1"

def _npz_meta(meta):
    return np.array(json.dumps(meta, sort_keys=True))

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def _exclusive_open(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    return open(path, "xb")

def _write_npz(path, arrays):
    with _exclusive_open(path) as f:
        np.savez_compressed(f, **arrays)

def _write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)

def _load_meta(path, base_keys):
    with np.load(path, allow_pickle=False) as d:
        meta = json.loads(str(d["metadata"])) if "metadata" in d.files else {}
        out = {k: np.asarray(d[k]) for k in base_keys if k in d.files}
    return out, meta, set(d.files)

# ---------------------------------------------------------------- inspect/crop

def _read_cloud(source):
    if not os.path.isfile(source):
        raise FileNotFoundError(source)
    ext = os.path.splitext(source)[1].lower()
    if ext == ".ply":
        p = PlyData.read(source)
        v = p["vertex"]
        names = set(v.data.dtype.names)
        for c in ("x", "y", "z"):
            if c not in names:
                raise ValueError("PLY missing property " + c)
        xyz = np.column_stack([np.asarray(v.data[c]) for c in ("x", "y", "z")]).astype(np.float64)
        if all(c in names for c in ("red", "green", "blue")):
            rgb = np.column_stack([np.asarray(v.data[c]) for c in ("red", "green", "blue")]).astype(np.uint8)
        else:
            rgb = np.full((len(xyz), 3), 128, np.uint8)
        prov = np.asarray(v.data["provenance"], np.uint8) if "provenance" in names else np.zeros(len(xyz), np.uint8)
        don = np.asarray(v.data["donor_index"], np.int32) if "donor_index" in names else np.full(len(xyz), -1, np.int32)
    elif ext == ".npz":
        with np.load(source, allow_pickle=False) as d:
            if "xyz" not in d.files:
                raise ValueError("NPZ missing xyz")
            xyz = np.asarray(d["xyz"], np.float64)
            rgb = np.asarray(d["rgb"], np.uint8) if "rgb" in d.files else np.full((len(xyz), 3), 128, np.uint8)
            prov = np.asarray(d["provenance"], np.uint8) if "provenance" in d.files else np.zeros(len(xyz), np.uint8)
            don = np.asarray(d["donor_index"], np.int32) if "donor_index" in d.files else np.full(len(xyz), -1, np.int32)
    else:
        raise ValueError("unsupported cloud format: " + ext)
    if xyz.ndim != 2 or xyz.shape[1] != 3:
        raise ValueError("xyz must be Nx3")
    if rgb.shape != xyz.shape or prov.shape != (len(xyz),) or don.shape != (len(xyz),):
        raise ValueError("cloud RGB/provenance/donor shape mismatch")
    if not np.all(np.isfinite(xyz)):
        raise ValueError("non-finite xyz")
    return xyz, rgb, prov, don, ext

def inspect_cloud(source):
    xyz, rgb, prov, don, ext = _read_cloud(source)
    rep = {"source": source, "format": ext, "bytes": os.path.getsize(source),
           "sha256": _sha256(source), "count": int(len(xyz)),
           "bounds_min": xyz.min(axis=0).tolist() if len(xyz) else None,
           "bounds_max": xyz.max(axis=0).tolist() if len(xyz) else None,
           "has_rgb": cloud_has_rgb(source), "observed_count": int((prov == 0).sum()),
           "synthetic_count": int((prov != 0).sum())}
    return rep

def cloud_has_rgb(source):
    if str(source).lower().endswith('.ply'):
        return all(k in PlyData.read(source)['vertex'].data.dtype.names for k in ['red','green','blue'])
    with np.load(source,allow_pickle=False) as d:
        if 'metadata' in d.files and json.loads(str(d['metadata'])).get('has_rgb') is False: return False
        return 'rgb' in d.files

def crop_cloud(source, bounds_min, bounds_max, output):
    lo = np.asarray(bounds_min, np.float64); hi = np.asarray(bounds_max, np.float64)
    if lo.shape != (3,) or hi.shape != (3,) or not np.all(np.isfinite(lo)) or not np.all(np.isfinite(hi)) or np.any(hi < lo):
        raise ValueError("invalid crop bounds")
    xyz, rgb, prov, don, _ = _read_cloud(source)
    m = np.all((xyz >= lo) & (xyz <= hi), axis=1)
    _write_npz(output, {"xyz": xyz[m], "rgb": rgb[m], "provenance": prov[m], "donor_index": don[m],
                        "metadata": _npz_meta({"operation": "crop", "has_rgb":cloud_has_rgb(source), "source_sha256": _sha256(source), "bounds_min": lo.tolist(), "bounds_max": hi.tolist(), "count": int(m.sum())})})
    return {"output": output, "count": int(m.sum()), "source_sha256": _sha256(source)}

# ---------------------------------------------------------------- symmetry
def symmetry_cloud(source, center, angle_degrees, min_height, voxel_size, output):
    if not np.isfinite(voxel_size) or voxel_size <= 0:
        raise ValueError("voxel_size must be positive")
    center = np.asarray(center, np.float64)
    if center.shape != (2,) or not np.all(np.isfinite(center)):
        raise ValueError("center must be [cx,cz]")
    xyz, rgb, prov, don, _ = _read_cloud(source)
    n = len(xyz)
    base_vox = np.floor(xyz / voxel_size).astype(np.int64)
    y = xyz[:, 1]
    if not np.isfinite(min_height) or not np.isfinite(angle_degrees):
        raise ValueError("angle and height must be finite")
    m = (y >= min_height)
    ang = np.deg2rad(float(angle_degrees))
    ca, sa = np.cos(ang), np.sin(ang)
    px = xyz[m, 0] - center[0]; pz = xyz[m, 2] - center[1]
    rx = ca * px - sa * pz + center[0]; rz = sa * px + ca * pz + center[1]
    rot = np.column_stack([rx, xyz[m, 1], rz])
    rot_vox = np.floor(rot / voxel_size).astype(np.int64)
    obs_set = set(map(tuple, base_vox.tolist()))
    add_xyz, add_rgb, add_prov, add_don = [], [], [], []
    src_idx = np.flatnonzero(m)
    for vi, i in enumerate(src_idx):
        t = tuple(rot_vox[vi].tolist())
        if t not in obs_set:
            add_xyz.append(rot[vi]); add_rgb.append(rgb[i]); add_prov.append(1); add_don.append(int(i))
            obs_set.add(t)
    parts_xyz = [xyz, np.asarray(add_xyz, np.float64).reshape(-1, 3)]
    parts_rgb = [rgb, np.asarray(add_rgb, np.uint8).reshape(-1, 3)]
    parts_prov = [prov, np.asarray(add_prov, np.uint8)]
    parts_don = [don, np.asarray(add_don, np.int32)]
    out_xyz = np.concatenate(parts_xyz); out_rgb = np.concatenate(parts_rgb)
    out_prov = np.concatenate(parts_prov); out_don = np.concatenate(parts_don)
    _write_npz(output, {"xyz": out_xyz, "rgb": out_rgb, "provenance": out_prov, "donor_index": out_don,
                        "metadata": _npz_meta({"operation": "symmetry", "has_rgb":cloud_has_rgb(source), "angle_degrees": float(angle_degrees), "min_height": float(min_height), "voxel_size": float(voxel_size), "original_count": int(n), "added": len(add_xyz)})})
    return {"output": output, "original_count": int(n), "added": len(add_xyz), "synthetic_claim": "unmeasured"}

# ---------------------------------------------------------------- preprocess
def _stem(p):
    return os.path.splitext(os.path.basename(p))[0]

def preprocess_photos(entries, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir,'images'),exist_ok=True)
    os.makedirs(os.path.join(output_dir,'masks'),exist_ok=True)
    manifest = {"entries": [], "geometry_unchanged": True, "warn": "mask not guaranteed COLMAP-ready"}
    for e in entries:
        src = e["source"]
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        stem = _stem(src)
        with open(src, "rb") as f:
            raw = f.read()
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError("decode failed: " + src)
        h = hashlib.sha256(raw).hexdigest()
        gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(gray)
        norm = np.clip(0.7 * gray.astype(np.float32) + 0.3 * clahe.astype(np.float32), 0, 255).astype(np.uint8)
        H, W = norm.shape[:2]
        mask = np.full((H, W), 255, np.uint8)
        poly = e.get("polygon")
        ml = np.ones((H, W), np.uint8)
        if poly:
            ml[:] = 0
            cv2.fillPoly(ml, [np.asarray(poly, np.int32)], 1)
        nearwhite = np.all(img[:, :, :3] >= 248, axis=-1) if img.ndim == 3 else (img >= 248)
        nearwhite = ndi.binary_dilation(nearwhite, np.ones((3, 3), bool))
        mask[(nearwhite) & (ml > 0)] = 0
        mask[ml == 0] = 0
        png = os.path.join(output_dir,'images', stem + ".png")
        mp = os.path.join(output_dir,'masks', stem + ".png.png")
        ok, buf = cv2.imencode(".png", norm)
        if not ok: raise ValueError("PNG encode failed")
        with _exclusive_open(png) as f: f.write(buf.tobytes())
        ok, buf = cv2.imencode(".png", mask)
        with _exclusive_open(mp) as f: f.write(buf.tobytes())
        manifest["entries"].append({"source": os.path.abspath(src), "normalized": 'images/'+os.path.basename(png), "mask": 'masks/'+os.path.basename(mp), "original_sha256": h, "dimensions":[W,H], "geometry_unchanged": True})
    _write_json(os.path.join(output_dir, "manifest.json"), manifest)
    return manifest

# ---------------------------------------------------------------- build geometry
def _empty_geometry():
    return {"positions": np.zeros((0, 3), np.int32), "palette_index": np.zeros((0,), np.uint32),
            "component_id": np.zeros((0,), np.int32), "dims": np.zeros(3, np.int64),
            "protected_air": np.zeros((0, 3), np.int32),
            "protected_openings": np.zeros((0, 3), np.int32),
            "expected_opening_palette": np.zeros((0,), np.uint32)}

def _key_idx(keys, order):
    if len(keys) == 0:
        return np.zeros((0,), np.int64)
    s = [slice(None)] * keys.shape[1]
    idx = keys[:, 0].astype(np.int64)
    for d in range(1, keys.shape[1]):
        idx = idx * order[d] + keys[:, d]
    return idx

def _grid_block(dims, lo, hi, region_pad=0):
    lo = np.maximum(np.asarray(lo, np.int64), 0); hi = np.minimum(np.asarray(hi, np.int64), dims)
    if np.any(hi <= lo):
        return np.zeros((0, 3), np.int64)
    ax = [np.arange(lo[d], hi[d], dtype=np.int64) for d in range(3)]
    g = np.stack(np.meshgrid(*ax, indexing="ij"), axis=-1).reshape(-1, 3)
    return g

def _parse_shape(c):
    role = c.get("shape")
    if role not in ("box", "ring", "rod"):
        raise ValueError("unsupported shape: " + repr(role))
    return role

def _component_voxels(c, dims):
    role = _parse_shape(c)
    if role in ("box", "ring"):
        mn = _integer_vector(c["min"], 'component min'); mx = _integer_vector(c["max"], 'component max')
        if np.any(mx <= mn) or np.any(mn < 0) or np.any(mx > dims):
            raise ValueError("component bounds invalid")
        g = _grid_block(dims, mn, mx)
        if role == "box":
            return g
        oc = np.asarray(c["center"], np.float64); oh = np.asarray(c["outer_half"], np.float64); ih = np.asarray(c["inner_half"], np.float64)
        if oc.shape!=(2,) or not np.isfinite(oc).all() or oh.shape != (2,) or ih.shape != (2,) or not np.isfinite(oh).all() or not np.isfinite(ih).all() or np.any(oh <= 0) or np.any(ih < 0) or np.any(ih >= oh):
            raise ValueError("ring half sizes invalid")
        ang = np.deg2rad(float(c.get("angle_degrees", 0)))
        if not np.isfinite(ang): raise ValueError('non-finite ring angle')
        ca, sa = np.cos(ang), np.sin(ang)
        x = g[:, 0] + 0.5 - oc[0]; z = g[:, 2] + 0.5 - oc[1]
        lx = ca * x + sa * z; lz = -sa * x + ca * z
        outer = (np.abs(lx) <= oh[0]) & (np.abs(lz) <= oh[1])
        inner = np.zeros(len(g), bool)
        if np.any(ih > 0):
            inner = (np.abs(lx) <= ih[0]) & (np.abs(lz) <= ih[1])
        return g[outer & ~inner]
    # rod
    start = np.asarray(c["start"], np.float64); end = np.asarray(c["end"], np.float64); r = float(c["radius"])
    if start.shape != (3,) or end.shape != (3,) or not np.all(np.isfinite(start)) or not np.all(np.isfinite(end)) or not np.isfinite(r) or r <= 0:
        raise ValueError("rod params invalid")
    mn = np.floor(np.minimum(start, end) - r).astype(np.int64)
    mx = np.ceil(np.maximum(start, end) + r).astype(np.int64) + 1
    g = _grid_block(dims, mn, mx)
    if len(g) == 0:
        return g
    p = g + 0.5
    d = end - start; L = float(np.dot(d, d))
    proj = p - start
    t = (proj @ d) / L if L > 0 else np.zeros(len(p))
    t = np.clip(t, 0, 1)
    closest = start + t[:, None] * d
    dist = np.linalg.norm(p - closest, axis=1)
    return g[dist <= r]

def _apply_openings(occupied, openings, dims, prot_air_list, prot_open_list, exp_pal_list):
    cells = set()
    assigned = {}
    original_cells = set(occupied)
    for op in openings:
        mn = _integer_vector(op["bounds_min"], "opening bounds_min"); mx = _integer_vector(op["bounds_max"], "opening bounds_max")
        if np.any(mx <= mn) or np.any(mn < 0) or np.any(mx > dims):
            raise ValueError("opening bounds invalid")
        theta = float(op.get("angle_degrees", 0))
        if not np.isfinite(theta): raise ValueError("non-finite opening angle")
        center = (mn + mx) / 2
        half = (mx - mn) / 2
        ca, sa = np.cos(np.deg2rad(theta)), np.sin(np.deg2rad(theta))
        extent = np.array([abs(ca)*half[0]+abs(sa)*half[2], half[1], abs(sa)*half[0]+abs(ca)*half[2]])
        if np.any(center-extent < -1e-7) or np.any(center+extent > dims+1e-7):
            raise ValueError("rotated opening extends outside dimensions")
        g = _grid_block(dims, np.floor(center-extent).astype(int), np.ceil(center+extent).astype(int))
        local = g + .5 - center
        lx = ca*local[:,0] + sa*local[:,2]
        lz = -sa*local[:,0] + ca*local[:,2]
        g = g[(np.abs(lx)<=half[0]+1e-8)&(np.abs(lz)<=half[2]+1e-8)]
        fill = int(op.get("fill_palette", 0))
        if fill < 0:
            raise ValueError("fill_palette negative")
        ev = op.get("evidence"); conf = op.get("confidence")
        if ev not in ("observed", "inferred", "user"): raise ValueError("bad evidence")
        if conf is None or not (0 <= float(conf) <= 1): raise ValueError("bad confidence")
        tup = list(map(tuple, g.tolist()))
        hits = any(k in original_cells for k in tup)
        if not hits:
            raise ValueError("opening vanishes (no block hits)")
        if fill == 0:
            prot_air_list.extend(tup)
        else:
            prot_open_list.extend(tup); exp_pal_list.extend([fill] * len(tup))
        for k in tup:
            cells.add(k)
            if k in cells and k in assigned and assigned[k] != fill:
                raise ValueError("overlapping openings have different fills")
            assigned[k] = fill
            if fill:
                old = occupied.get(k, (0, fill))
                occupied[k] = (old[0], fill)
            else:
                occupied.pop(k, None)
    return occupied, cells

def _integer_vector(value, name):
    a = np.asarray(value, dtype=float)
    if a.shape != (3,) or not np.isfinite(a).all() or not np.equal(a, np.floor(a)).all():
        raise ValueError(name + " must contain three finite integers")
    return a.astype(np.int64)

def build_geometry(spec, output):
    if spec.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    dims = _integer_vector(spec["dims"], "dims")
    if dims.shape != (3,) or np.any(dims <= 0) or np.any(dims > 4096) or int(np.prod(dims.astype(np.int64))) > 16_000_000:
        raise ValueError("dims invalid or too large")
    comps = spec.get("components", [])
    ids = [c["id"] for c in comps]
    if len(set(ids)) != len(ids) or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError("component ids must be unique positive")
    occ = {}
    for c in comps:
        role = _parse_shape(c)
        mat = int(c.get("material", 1))
        if mat <= 0 or mat != c.get("material", 1):
            raise ValueError("component material must be positive integer")
        g = _component_voxels(c, dims)
        tup = list(map(tuple, g.tolist()))
        for k in tup:
            occ[k] = (int(c["id"]), mat)
    prot_air, prot_open, exp_pal = [], [], []
    openings = spec.get("openings", [])
    # normalize openings
    norm_ops = []
    if len({op['id'] for op in openings}) != len(openings):
        raise ValueError("opening ids must be unique")
    for op in openings:
        fill_value=op.get('fill_palette',0)
        if type(fill_value) is not int or fill_value<0: raise ValueError('opening fill must be nonnegative integer')
        nm = {**op, "angle_degrees": float(op.get("angle_degrees", 0)), "fill_palette": int(op.get("fill_palette", 0)), "confidence": float(op["confidence"])}
        norm_ops.append(nm)
    occ, _ = _apply_openings(occ, norm_ops, dims, prot_air, prot_open, exp_pal)
    if not occ:
        positions = np.zeros((0, 3), np.int32); pidx = np.zeros((0,), np.uint32); cids = np.zeros((0,), np.int32)
    else:
        keys = np.array(list(occ.keys()), np.int32)
        order = np.lexsort((keys[:, 2], keys[:, 1], keys[:, 0]))
        keys = keys[order]
        pidx = np.array([occ[tuple(k.tolist())][1] for k in keys], np.uint32)
        cids = np.array([occ[tuple(k.tolist())][0] for k in keys], np.int32)
        positions = keys
    sym = spec.get("symmetry")
    meta = {"version": GEOM_VERSION, "dims": dims.tolist(), "openings": norm_ops,
            "symmetry": sym if sym else None, "conceptual_inferred": True, "regular_shapes": True, "survey": False}
    arrays = {"positions": positions.astype(np.int32), "palette_index": pidx.astype(np.uint32),
              "component_id": cids.astype(np.int32), "dims": dims.astype(np.int64),
              "protected_air": (np.array(prot_air, np.int32).reshape(-1, 3) if prot_air else np.zeros((0, 3), np.int32)),
              "protected_openings": (np.array(prot_open, np.int32).reshape(-1, 3) if prot_open else np.zeros((0, 3), np.int32)),
              "expected_opening_palette": np.asarray(exp_pal, np.uint32),
              "metadata": _npz_meta(meta)}
    _write_npz(output, arrays)
    validate_build(output)
    return {"output": output, "block_count": int(len(positions)), "regular_shapes": True, "conceptual_inferred": True, "survey": False}

# ---------------------------------------------------------------- import
REQUIRED = ("positions", "palette_index", "component_id", "dims")

def _geom_from(path):
    with np.load(path, allow_pickle=False) as d:
        g = {k: np.asarray(d[k]) for k in d.files if k != "metadata"}
        g["metadata"] = str(d["metadata"]) if "metadata" in d.files else "{}"
    for k in REQUIRED:
        if k not in g: raise ValueError("geometry missing " + k)
    return g

def _geom_write(path, g):
    arr = {k: np.asarray(v) for k, v in g.items()}
    arr["metadata"] = np.array(str(g.get("metadata", "{}")))
    _write_npz(path, arr)

def import_voxels(source, output):
    with np.load(source, allow_pickle=False) as d:
        if "positions" not in d.files:
            raise ValueError("import source missing positions")
        pos = np.asarray(d["positions"])
        if not np.issubdtype(pos.dtype,np.integer): raise ValueError('positions must be integer')
        pidx = np.asarray(d["palette_index"], np.int64) if "palette_index" in d.files else np.ones(len(pos), np.int64)
        if 'palette_index' in d.files and not np.issubdtype(d['palette_index'].dtype,np.integer): raise ValueError('palette_index must be integer')
        cids = np.asarray(d["component_id"], np.int64) if "component_id" in d.files else np.zeros(len(pos), np.int64)
        dims = np.asarray(d["dims"], np.int64) if "dims" in d.files else (pos.max(axis=0) + 1 if len(pos) else np.ones(3, np.int64))
        meta = str(d["metadata"]) if "metadata" in d.files else "{}"
        extra = {k: np.asarray(d[k]) for k in d.files if k not in ("positions", "palette_index", "component_id", "dims", "metadata")}
    if pos.ndim != 2 or pos.shape[1] != 3: raise ValueError("positions must Nx3")
    if len(pos) and (np.any(pos < 0) or np.any(pos >= dims)): raise ValueError("positions out of bounds")
    if len(pos) > 1:
        u, cnt = np.unique(pos, axis=0, return_counts=True)
        if np.any(cnt > 1): raise ValueError("duplicate positions")
    if np.any(pidx <= 0): raise ValueError("palette_index must be positive")
    g = {"positions": pos.astype(np.int32), "palette_index": pidx.astype(np.uint32), "component_id": cids.astype(np.int32),
         "dims": dims.astype(np.int64), "protected_air": extra.get("protected_air", np.zeros((0, 3), np.int32)),
         "protected_openings": extra.get("protected_openings", np.zeros((0, 3), np.int32)),
         "expected_opening_palette": extra.get("expected_opening_palette", np.zeros((0,), np.uint32)),
         "metadata": meta}
    for k in ("protected_air", "protected_openings"):
        if k in extra and extra[k].ndim != 2: raise ValueError(k + " must Kx3")
    _geom_write(output, g)
    validate_build(output)
    return {"output": output, "block_count": int(len(pos)), "source_provenance": "imported", "schema": GEOM_VERSION}

# ---------------------------------------------------------------- geometry hash
def _occ_hash(positions):
    if len(positions) == 0:
        return hashlib.sha256(b"").hexdigest()
    order = np.lexsort((positions[:, 2], positions[:, 1], positions[:, 0]))
    p = np.asarray(positions, np.int64)[order]
    return hashlib.sha256(p.tobytes()).hexdigest()

def _key_set(positions):
    return set(map(tuple, np.asarray(positions, np.int64).tolist()))

def validate_build(geometry):
    g = _geom_from(geometry)
    dims = _integer_vector(g["dims"], 'dims'); pos = g["positions"]
    if pos.ndim!=2 or pos.shape[1]!=3 or not np.issubdtype(pos.dtype,np.integer):
        raise ValueError('positions must be integer Nx3')
    for key in ['palette_index','component_id']:
        if g[key].shape!=(len(pos),) or not np.issubdtype(g[key].dtype,np.integer):
            raise ValueError(key+' must be integer N')
    if np.any(dims <= 0) or np.prod(dims.astype(np.int64)) > 16_000_000:
        raise ValueError("dims invalid")
    if len(pos) and (np.any(pos < 0) or np.any(pos >= dims)):
        raise ValueError("positions out of bounds")
    if len(pos) > 1:
        if len(np.unique(pos, axis=0)) != len(pos): raise ValueError("duplicate positions")
    pidx = g["palette_index"]
    if np.any(pidx <= 0): raise ValueError("palette_index must be positive")
    prot_air = _key_set(g.get("protected_air", np.zeros((0, 3))))
    prot_open = _key_set(g.get("protected_openings", np.zeros((0, 3))))
    for key in ['protected_air','protected_openings']:
        a=g.get(key,np.empty((0,3),int))
        if a.ndim!=2 or a.shape[1]!=3 or not np.issubdtype(a.dtype,np.integer) or np.any(a<0) or np.any(a>=dims):
            raise ValueError('invalid protected volume coordinates')
    if prot_air & prot_open: raise ValueError('conflicting protected volumes')
    occ = _key_set(pos)
    air_conf = prot_air & occ
    if air_conf: raise ValueError("protected air occupied: %d cells" % len(air_conf))
    if prot_open:
        exp = g.get("expected_opening_palette", np.zeros((0,), np.uint32))
        if len(exp)!=len(g.get('protected_openings',[])): raise ValueError('opening fill length mismatch')
        m = {tuple(p): int(v) for p, v in zip(g.get("protected_openings", []), exp)}
        actual={tuple(p):int(v) for p,v in zip(pos,pidx)}
        for k in prot_open:
            want = m.get(k, 0)
            if actual.get(k,0)!=want:
                raise ValueError("opening fill conflict")
    # components
    blockcounts = {}
    for cid in np.unique(g["component_id"]):
        blockcounts[int(cid)] = int((g["component_id"] == cid).sum())
    sizes = []
    if len(pos):
        grid = np.zeros(dims.astype(int), bool)
        grid[tuple(pos.T)] = True
        lab, n = ndi.label(grid)
        sizes = np.bincount(lab.ravel())[1:].tolist()
    h = _occ_hash(pos)
    meta = json.loads(g.get("metadata", "{}") or "{}")
    rep = {"block_count": int(len(pos)), "component_blockcounts": blockcounts, "connected_components": int(len(sizes)),
           "largest_component": int(max(sizes) if sizes else 0), "geometry_hash": h,
           "protected_air": len(prot_air), "protected_openings": len(prot_open),
           "validated": True, "game_tested": False}
    if meta.get("symmetry"):
        s = meta["symmetry"]; order = int(s.get("order", 4)); cx, cz = s["center"]
        if order > 1 and len(pos):
            obs = _key_set(pos)
            matched = 0; total = 0
            if order > 64: raise ValueError('symmetry order too large')
            for o in range(1,order):
                a = 2 * np.pi * o / order; ca, sa = np.cos(a), np.sin(a)
                for k in pos:
                    dx,dz=k[0]+.5-cx,k[2]+.5-cz
                    rx = ca * dx - sa * dz+cx-.5; rz = sa * dx + ca * dz+cz-.5
                    total += 1
                    if (int(round(rx)),int(k[1]),int(round(rz))) in obs: matched += 1
            rep["mismatch_ratio"] = float(1 - matched / max(total, 1))
    return rep

# ---------------------------------------------------------------- materials
def _srgb2lab(rgb):
    arr = np.asarray(rgb, np.float32).reshape(-1, 1, 3) / 255.0
    return cv2.cvtColor(arr, cv2.COLOR_RGB2Lab).reshape(-1, 3)

def _palette_list(palette):
    if isinstance(palette, dict):
        blocks = palette.get("blocks", palette)
        if isinstance(blocks, list): return blocks
        return [{"id": k, **v} for k, v in blocks.items()]
    return list(palette)

def checked_palette(palette):
    blocks=_palette_list(palette)
    if not blocks: raise ValueError('empty palette')
    for b in blocks:
        if not re.fullmatch(r'(?:[a-z0-9_.-]+:)?[a-z0-9_./-]+(?:\[[a-z0-9_=,.-]+\])?',str(b.get('id',''))):
            raise ValueError('invalid blockstate id')
        rgb=np.asarray(b.get('mean_rgb'),float)
        if rgb.shape!=(3,) or not np.isfinite(rgb).all() or np.any(rgb<0) or np.any(rgb>255):
            raise ValueError('palette mean_rgb must be three values in 0..255')
    return blocks

from .materials import map_materials

# ---------------------------------------------------------------- export schem
def _varint(n):
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n: out.append(b | 0x80)
        else: out.append(b); break
    return bytes(out)

def _read_varint(data, i):
    shift = 0; val = 0
    while True:
        b = data[i]; i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80): return val, i
        shift += 7

from .schematic import export_schem

# ---------------------------------------------------------------- preview
def preview_build(geometry, palette, output):
    from PIL import Image, ImageDraw
    validate_build(geometry)
    g = _geom_from(geometry)
    pos = g["positions"].astype(np.int64); dims = g["dims"]
    pal = checked_palette(palette)
    colors = np.array([p.get("mean_rgb", [128, 128, 128]) for p in pal] + [[128, 128, 128]], np.uint8)
    pidx = g["palette_index"].astype(np.int64)
    imgs = []
    for name in ("front", "back", "side", "top"):
        if name in ("front","back"): W, H = int(dims[0]), int(dims[1]); a, b = pos[:, 0], pos[:, 1]
        elif name == "side": W, H = int(dims[2]), int(dims[1]); a, b = pos[:, 2], pos[:, 1]
        else: W, H = int(dims[0]), int(dims[2]); a, b = pos[:, 0], pos[:, 2]
        img = np.full((H, W, 3), 255, np.uint8)
        if len(pos):
            depth=-pos[:,2] if name=='front' else pos[:,2] if name=='back' else pos[:,0] if name=='side' else pos[:,1]
            order=np.argsort(depth,kind='stable')
            img[H - 1 - b[order], a[order]] = colors[np.clip(pidx[order] - 1, 0, len(colors) - 1)]
        scale=max(1,min(16,576//max(W,H)))
        pixels=Image.fromarray(img).resize((W*scale,H*scale),Image.Resampling.NEAREST)
        im=Image.new('RGB',(W*scale,H*scale+24),'white'); im.paste(pixels,(0,24))
        d = ImageDraw.Draw(im); d.text((4, 4), name, fill=(0, 0, 0))
        imgs.append(im)
    total = Image.new("RGB", (sum(i.width for i in imgs) + 20, max(i.height for i in imgs)), (230, 230, 230))
    x = 0
    for im in imgs:
        total.paste(im, (x, 0)); x += im.width + 10
    with _exclusive_open(output) as f:
        total.save(f, format="PNG")
    return {"output": output, "views": ["front (-Z)", "back (+Z)", "side (+X)", "top (+Y)"], "shading_baked": False}
