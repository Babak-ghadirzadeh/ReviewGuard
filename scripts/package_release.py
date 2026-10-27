#!/usr/bin/env python3
"""Package tracked source only; never collect runtime evidence or credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def main():
    root=Path(__file__).resolve().parent.parent
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=root.parent/'ReviewGuard-v0.3.0a1.zip')
    args=p.parse_args()
    files=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
    files=sorted(f for f in files if f and f!='SOURCE_MANIFEST.json')
    for name in files:
        path=Path(name)
        if path.parts[0] in {'data','work','.venv','dist','build','.git'} or name=='config/local-runtime.json' or path.suffix.lower() in {'.pdf','.docx','.sqlite3','.log','.bin','.whl','.zip'} or path.name.startswith('.env'):
            raise SystemExit('Runtime or sensitive file refused: '+name)
        if (root/path).is_symlink():raise SystemExit('Symbolic link refused: '+name)
    manifest={'version':'0.3.0a1','files':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.out,'w',zipfile.ZIP_DEFLATED) as z:
        for name in files:z.write(root/name,'ReviewGuard/'+name)
        z.writestr('ReviewGuard/SOURCE_MANIFEST.json',json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({'archive':str(args.out.resolve()),'files':len(files),'sha256':hashlib.sha256(args.out.read_bytes()).hexdigest()},indent=2))

if __name__=='__main__':main()
