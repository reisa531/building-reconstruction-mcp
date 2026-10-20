from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import threading
import traceback
import uuid
import importlib.metadata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

JOB_RE = re.compile(r"^[0-9a-f]{32}$")
CODE_VERSION = "reconstruction-kit/1"
CORE_VERSION = "reconstruction-geometry/1"
OPERATIONS = {
    "crop_cloud", "symmetry_cloud", "preprocess_photos", "build_geometry",
    "import_voxels", "map_materials", "export_schem", "preview_build",
    "validate_build", "run_pipeline", "attempt_colmap",
}
CLOUD_OPS = {"crop_cloud", "symmetry_cloud"}
ARTIFACTS = {
    "crop_cloud": "cloud.npz", "symmetry_cloud": "cloud.npz",
    "preprocess_photos": "photos", "build_geometry": "geometry.npz",
    "import_voxels": "geometry.npz", "map_materials": "materials.npz",
    "export_schem": "build.schem", "preview_build": "preview.png",
    "validate_build": "validation.json",
}
BUILD_COLS = {"minecraft:air", "air", "minecraft:cave_air", "minecraft:void_air"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_alive(pid) -> bool:
    if not isinstance(pid,int) or pid<=0: return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle: return False
        try:
            code=wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid,0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        return jsonable(value.tolist())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    return str(value)


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(jsonable(data), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def safe_under(root: Path, rel: str, must_exist: bool) -> Path:
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise ValueError("invalid path")
    base = root.resolve()
    p = (base / rel).resolve(strict=must_exist)
    try:
        p.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path escapes root: {rel}") from exc
    return p


def walk_files(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if (k in {"source","geometry","cloud","images","masks"} or k.endswith("_source")) and isinstance(v, str):
                out.append(v)
            elif k.endswith("_path") and isinstance(v, str):
                out.append(v)
            elif k in {"entries", "parameters", "images", "masks"}:
                out.extend(walk_files(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(walk_files(v))
    return out


def input_hashes(root: Path, params: dict) -> dict:
    result={}
    for rel in dict.fromkeys(walk_files(params)):
        path=safe_under(root,rel,True)
        paths=[path] if path.is_file() else sorted(path.rglob('*'))
        for f in paths:
            if f.is_file():
                f=safe_under(root,str(f),True)
                result[str(f.relative_to(root))]=hash_file(f)
    return result


def load_json_guarded(root: Path, rel: str) -> Any:
    p = safe_under(root, rel, True)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON input {rel}: {exc}") from exc


def core():
    import reconstruction.core as mod
    return mod


class JobManager:
    def __init__(self, root: Path, max_workers: int = 1):
        self.root = root.resolve(strict=True)
        self.runs = safe_under(self.root, "reconstruction-runs", False)
        self.runs.mkdir(parents=True, exist_ok=True)
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="recon-job")
        self.runtime: set[str] = set()
        self.lock = threading.Lock()

    def jobdir(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not JOB_RE.match(job_id):
            raise ValueError("unknown job id")
        p = (self.runs / job_id).resolve()
        try:
            p.relative_to(self.runs.resolve())
        except ValueError as exc:
            raise ValueError("unknown job id") from exc
        return p

    def load(self, job_id: str) -> dict:
        p = self.jobdir(job_id) / "job.json"
        if not p.is_file():
            raise LookupError("unknown job")
        job = json.loads(p.read_text(encoding="utf-8"))
        with self.lock:
            if job.get("status") in {"queued", "running"} and job_id not in self.runtime and not process_alive(job.get('owner_pid')):
                job["status"] = "interrupted"
                job["error"] = "server restarted before job completed"
                job["updated_at"] = now()
                atomic_json(p, job)
        return self.public(job)

    def public(self, job: dict) -> dict:
        jid = job["job_id"]
        d = self.jobdir(jid)
        out = {k: job.get(k) for k in ("job_id", "operation", "status", "created_at", "started_at", "finished_at", "result", "error")}
        out["log_path"] = str((d / "job.log").relative_to(self.root)) if (d / "job.log").exists() else None
        out["job_dir"] = str(d.relative_to(self.root))
        return jsonable(out)

    def list(self) -> list[dict]:
        jobs = []
        for d in self.runs.iterdir() if self.runs.exists() else []:
            if d.is_dir() and JOB_RE.match(d.name) and (d / "job.json").is_file():
                try:
                    jobs.append(self.load(d.name))
                except Exception:
                    continue
        return sorted(jobs, key=lambda j: j.get("created_at") or "")

    def start(self, operation: str, parameters: dict) -> str:
        if operation not in OPERATIONS:
            raise ValueError("unknown operation")
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be a dict")
        from jsonschema import validate
        from .contracts import schema
        validate(parameters,schema(operation))
        hashes = input_hashes(self.root,parameters)
        jid = uuid.uuid4().hex
        d = self.jobdir(jid)
        d.mkdir(parents=True)
        job = {
            "job_id": jid, "operation": operation, "parameters": parameters,
            "status": "queued", "created_at": now(), "updated_at": now(),
            "started_at": None, "finished_at": None, "input_hashes": hashes,
            "code_version": CODE_VERSION, "core_version": CORE_VERSION,
            "owner_pid":os.getpid(),
            "python": sys.version, "job_dir": jid, "result": None, "error": None,
            "dependencies": {name: importlib.metadata.version(name) for name in ['mcp','numpy','scipy','plyfile','nbtlib','Pillow']},
            "implementation_hashes": {f.name:hash_file(f) for f in Path(__file__).parent.glob('*.py')},
        }
        atomic_json(d / "job.json", job)
        with self.lock:
            self.runtime.add(jid)
        self.pool.submit(self._run, jid, operation, parameters)
        return jid

    def _update(self, jid: str, **changes: Any) -> None:
        p = self.jobdir(jid) / "job.json"
        job = json.loads(p.read_text(encoding="utf-8"))
        job.update(changes)
        job["updated_at"] = now()
        atomic_json(p, job)

    def _run(self, jid: str, operation: str, params: dict) -> None:
        d = self.jobdir(jid)
        log = (d / "job.log").open("a", encoding="utf-8")
        self._update(jid, status="running", started_at=now())
        try:
            result = self.execute(jid, operation, params, log)
            initial=json.loads((d/'job.json').read_text(encoding='utf8'))['input_hashes']
            if input_hashes(self.root,params)!=initial: raise RuntimeError('input changed during execution')
            self._update(jid, status="completed", finished_at=now(), result=result, error=None)
        except Exception as exc:
            log.write(traceback.format_exc())
            msg = f"{type(exc).__name__}: {exc}"
            if str(self.root) in msg:
                msg = msg.replace(str(self.root), "<root>")
            self._update(jid, status="failed", finished_at=now(), error=msg)
        finally:
            log.close()
            with self.lock:
                self.runtime.discard(jid)

    def _spec(self, root: Path, p: dict, key: str, pathkey: str) -> dict | None:
        if p.get(key) is not None and p.get(pathkey) is not None:
            raise ValueError(f"{key} and {pathkey} are mutually exclusive")
        if p.get(pathkey) is not None:
            return load_json_guarded(root, p[pathkey])
        value = p.get(key)
        if value is not None and not isinstance(value, (dict,list) if key=='palette' else dict):
            raise ValueError(f"{key} has invalid type")
        return value

    def _artifact(self, jid: str, name: str) -> str:
        p = self.jobdir(jid) / name
        if not p.exists():
            raise RuntimeError(f"core did not produce {name}")
        return str(p.relative_to(self.root))

    def execute(self, jid: str, operation: str, p: dict, log) -> dict:
        root, d, c = self.root, self.jobdir(jid), core()
        if operation in CLOUD_OPS:
            src = safe_under(root, p["source"], True)
            out = d / "cloud.npz"
            if operation == "crop_cloud":
                r = c.crop_cloud(str(src), p["bounds_min"], p["bounds_max"], str(out))
            else:
                r = c.symmetry_cloud(str(src), p["center"], p["angle_degrees"], p["min_height"], p["voxel_size"], str(out))
            return {**jsonable(r or {}), "output": self._artifact(jid, "cloud.npz")}
        if operation == "preprocess_photos":
            entries = [{**e,'source':str(safe_under(root,e['source'],True))} for e in p.get('entries',[])]
            r = c.preprocess_photos(entries, str(d / "photos"))
            return {**jsonable(r or {}), "output": self._artifact(jid, "photos")}
        if operation == "map_materials":
            g = safe_under(root, p["geometry"], True)
            cl = safe_under(root, p["cloud"], True)
            out = d / "materials.npz"
            palette=self._spec(root,p,'palette','palette_path')
            r = c.map_materials(str(g), str(cl), palette, p.get("transform"), p.get("policy"), str(out))
            return {**jsonable(r or {}), "output": self._artifact(jid, "materials.npz")}
        if operation == "export_schem":
            g = safe_under(root, p["geometry"], True)
            out = d / "build.schem"
            r = c.export_schem(str(g), self._spec(root,p,'palette','palette_path'), p["data_version"], str(out))
            return {**jsonable(r or {}), "output": self._artifact(jid, "build.schem")}
        if operation == "preview_build":
            g = safe_under(root, p["geometry"], True)
            out = d / "preview.png"
            r = c.preview_build(str(g), self._spec(root,p,'palette','palette_path'), str(out))
            return {**jsonable(r or {}), "output": self._artifact(jid, "preview.png")}
        if operation == "validate_build":
            g = safe_under(root, p["geometry"], True)
            r = c.validate_build(str(g))
            atomic_json(d / "validation.json", r)
            return {**jsonable(r or {}), "output": self._artifact(jid, "validation.json")}
        if operation == "build_geometry":
            spec = self._spec(root, p, "spec", "spec_path")
            out = d / "geometry.npz"
            r = c.build_geometry(spec, str(out))
            return {**jsonable(r or {}), "output": self._artifact(jid, "geometry.npz")}
        if operation == "import_voxels":
            src = safe_under(root, p["source"], True)
            out = d / "geometry.npz"
            r = c.import_voxels(str(src), str(out))
            return {**jsonable(r or {}), "output": self._artifact(jid, "geometry.npz")}
        if operation == "run_pipeline":
            return self._pipeline(jid, p, log)
        if operation == "attempt_colmap":
            return self._colmap(jid, p, log)
        raise ValueError("unknown operation")

    def _pipeline(self, jid: str, p: dict, log) -> dict:
        root, d, c = self.root, self.jobdir(jid), core()
        spec = self._spec(root, p, "spec", "spec_path")
        gsrc = p.get("geometry_source")
        if spec is not None and gsrc is not None:
            raise ValueError("spec and geometry_source are mutually exclusive")
        if spec is None and gsrc is None:
            raise ValueError("spec or geometry_source required")
        palette = self._spec(root, p, "palette", "palette_path")
        if palette is None:
            raise ValueError("palette required")
        need_materials = p.get("cloud") is not None
        if need_materials and not p.get("transform"):
            raise ValueError("transform required when cloud supplied")
        dv = p.get("data_version")
        if not isinstance(dv, int) or dv <= 0:
            raise ValueError("positive data_version required")
        gpath = d / "geometry.npz"
        if gsrc is not None:
            c.import_voxels(str(safe_under(root, gsrc, True)), str(gpath))
        else:
            c.build_geometry(spec, str(gpath))
        if not gpath.exists():
            raise RuntimeError("geometry was not produced")
        final = gpath
        materials=None
        if need_materials:
            mpath = d / "materials.npz"
            cl = safe_under(root, p["cloud"], True)
            materials=c.map_materials(str(gpath), str(cl), palette, p["transform"], p.get("policy"), str(mpath))
            if not mpath.exists():
                raise RuntimeError("materials were not produced")
            final = mpath
        validation = c.validate_build(str(final))
        export = c.export_schem(str(final), palette, dv, str(d / "build.schem"))
        preview = c.preview_build(str(final), palette, str(d / "preview.png"))
        report = {
            "params": p,
            "input_hashes": input_hashes(root,p),
            "geometry": str(gpath.relative_to(root)), "geometry_used": str(final.relative_to(root)),
            "validation": jsonable(validation), "export": jsonable(export), "preview": jsonable(preview),
            "materials":jsonable(materials),
        }
        atomic_json(d / "pipeline.json", report)
        return {**jsonable(report), "output": self._artifact(jid, "build.schem"), "preview_output": self._artifact(jid, "preview.png")}

    def _colmap(self, jid: str, p: dict, log) -> dict:
        root, d = self.root, self.jobdir(jid)
        try:
            import pycolmap
        except ImportError as exc:
            raise RuntimeError("pycolmap unavailable") from exc
        images = safe_under(root, p["images"], True)
        masks = safe_under(root, p["masks"], True) if p.get("masks") else None
        db = d / "database.db"
        try:
            reader = pycolmap.ImageReaderOptions()
            reader.camera_model = "SIMPLE_RADIAL"
            if masks is not None: reader.mask_path=masks
            extraction=pycolmap.FeatureExtractionOptions(); extraction.use_gpu=False
            matching=pycolmap.FeatureMatchingOptions(); matching.use_gpu=False
            pycolmap.extract_features(str(db), str(images), camera_mode=pycolmap.CameraMode.PER_IMAGE,
                reader_options=reader,extraction_options=extraction,device=pycolmap.Device.cpu)
            pycolmap.match_exhaustive(str(db),matching_options=matching,device=pycolmap.Device.cpu)
            (d/'models').mkdir()
            recs = pycolmap.incremental_mapping(str(db), str(images), str(d / "models"))
        except Exception as exc:
            log.write(traceback.format_exc())
            raise RuntimeError(f"COLMAP failed: {type(exc).__name__}: {exc}") from exc
        count = len(recs) if recs is not None else 0
        if count == 0:
            return {"status": "no_model", "model_count": 0, "registered_images": 0, "masks": bool(masks)}
        registered = 0
        for rec in recs.values() if isinstance(recs, dict) else recs:
            registered += int(getattr(rec, "num_reg_images", lambda: 0)() if callable(getattr(rec, "num_reg_images", None)) else getattr(rec, "num_reg_images", 0))
        return {"status": "completed", "model_count": count, "registered_images": registered, "output": str((d / "models").relative_to(root))}
