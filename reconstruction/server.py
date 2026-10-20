from __future__ import annotations

import argparse
import json
import logging
import sys
import importlib.util
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from reconstruction.jobs import JobManager, OPERATIONS, safe_under

log = logging.getLogger("reconstruction")
MANAGER: JobManager | None = None
mcp = FastMCP("building-reconstruction")

WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

CAPS = {
    "cloud": ["inspect_cloud", "crop_cloud", "symmetry_cloud"],
    "photos": ["preprocess_photos", "attempt_colmap"],
    "geometry": ["build_geometry", "import_voxels"],
    "materials": ["map_materials"],
    "build": ["validate_build", "export_schem", "preview_build"],
    "pipeline": ["run_pipeline"], "jobs": ["start_job", "get_job", "list_jobs"],
    "colmap": {"supported": "optional", "available": False, "detail": "requires pycolmap; failures reported, never faked"},
}


def mgr() -> JobManager:
    if MANAGER is None:
        raise RuntimeError("server not initialized")
    return MANAGER


@mcp.tool(annotations=READ)
def capabilities() -> dict:
    """Report supported stages, job operations, and optional COLMAP capability."""
    return {**CAPS, "operations": sorted(OPERATIONS),
        'colmap':{'available':importlib.util.find_spec('pycolmap') is not None,'supported':'optional CPU SfM'},
        'limitations':['explicit component specification required; no automatic facade interpretation',
            'no dense SfM, automatic camera-cloud alignment, Potree decoding or automatic fragment repair',
            'orthographic full-block mean-color preview; game import not validated'],
        'defaults':{'brightness_retention':.25,'chroma_retention':.15,'brightness_limits':[.85,1.15]},
        'parameter_reference':'README.md and examples in reconstruction-kit'}


@mcp.tool(annotations=READ)
def get_operation_schema(operation: str) -> dict:
    """Get input JSON Schema for an operation before calling start_job."""
    from .contracts import schema
    return schema(operation)


@mcp.resource("reconstruction://operation/{operation}")
def operation_resource(operation: str) -> str:
    return json.dumps(get_operation_schema(operation))


@mcp.tool(annotations=READ)
def inspect_cloud(source: str) -> dict:
    """Inspect a cloud file under the workspace root."""
    from reconstruction import core
    path = safe_under(mgr().root, source, True)
    return core.inspect_cloud(str(path))


@mcp.tool(annotations=WRITE)
def start_job(operation: str, parameters: dict) -> dict:
    """Start an asynchronous reconstruction job; returns its job id."""
    return {"job_id": mgr().start(operation, parameters)}


@mcp.tool(annotations=READ)
def get_job(job_id: str) -> dict:
    """Get one local job status, log path, result, and error."""
    try:
        return mgr().load(job_id)
    except LookupError as exc:
        raise ValueError("unknown job") from exc


@mcp.tool(annotations=READ)
def list_jobs() -> list[dict]:
    """List local persisted jobs only."""
    return mgr().list()


@mcp.tool(annotations=READ)
def read_report(path: str) -> dict | list:
    """Read a JSON job report of at most 2 MiB under the root."""
    p = safe_under(mgr().root, path, True)
    if p.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("report too large")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("report is not JSON") from exc


@mcp.resource("reconstruction://capabilities")
def capabilities_resource() -> str:
    return json.dumps(capabilities(), sort_keys=True)


@mcp.resource("reconstruction://job/{job_id}")
def job_resource(job_id: str) -> str:
    return json.dumps(get_job(job_id), sort_keys=True)


@mcp.prompt()
def reconstruct_building(task: str) -> str:
    """Short reconstruction guidance prompt."""
    return (
        f"Reconstruct: {task}\n"
        "Keep regular semantic components; preserve door/window air volumes or explicitly locked glazing. "
        "Model illumination as small measured variation and use measured colors, not physical albedo. "
        "If optional COLMAP fails or yields no model, report failure distinctly; never fabricate success."
    )


def main(argv: list[str] | None = None) -> None:
    global MANAGER
    parser = argparse.ArgumentParser(description="reconstruction-kit MCP server (stdio)")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    root = Path(args.root)
    if not root.exists() or not root.is_dir():
        parser.error("--root must be an existing directory")
    MANAGER = JobManager(root)
    log.info("serving root %s", MANAGER.root)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
