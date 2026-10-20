"""Create a source kit without datasets, environments or local runtime logs."""
import hashlib,json,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    files=[ROOT/x for x in ['README.md','pyproject.toml','requirements-lock.txt','mcp-config.example.json','verification.json']]
    for directory in ['reconstruction','skills','examples','tests','tools','docs']:
        files.extend(p for p in (ROOT/directory).rglob('*') if p.is_file()
            and '__pycache__' not in p.parts and p.suffix in {'.py','.md','.json','.yaml','.png','.schem','.txt'})
    files=sorted(set(files))
    checksums={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    dest=ROOT/'dist'; dest.mkdir(exist_ok=True)
    out=dest/'building-reconstruction-mcp-skills.zip'
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        for p in files: archive.write(p,Path('reconstruction-kit')/p.relative_to(ROOT))
        archive.writestr('reconstruction-kit/SHA256SUMS.json',json.dumps(checksums,indent=2))
    print(json.dumps({'archive':str(out),'bytes':out.stat().st_size,'files':len(files),
        'sha256':hashlib.sha256(out.read_bytes()).hexdigest()},indent=2))

if __name__=='__main__': main()
