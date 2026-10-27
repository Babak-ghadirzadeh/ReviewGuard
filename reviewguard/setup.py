"""Read-only readiness checks and a user-invoked macOS scheduler installer."""
import importlib.util
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import urllib.request

def doctor(store):
    result={'python':platform.python_version(),'platform':platform.platform(),
            'data_directory':str(store.root),'pdf_support':importlib.util.find_spec('pdfplumber') is not None,
            'ollama_local':False,'installed_models':[],'scientific_validation':'not_yet_qualified',
            'database_schema':store.con.execute('SELECT max(version) FROM schema_version').fetchone()[0]}
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=3) as response:
            data=json.loads(response.read(1_000_000))
        result['ollama_local']=True
        result['installed_models']=[m.get('name') for m in data.get('models',[])]
    except (OSError,ValueError) as exc:result['ollama_detail']=str(exc)
    return result

def schedule(root,hour=9,remove=False):
    if sys.platform!='darwin':raise ValueError('The built-in scheduler installer is for macOS. See docs/OPERATIONS.md for Linux.')
    if not 0<=hour<=23:raise ValueError('Choose an hour between 0 and 23.')
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    folder=Path.home()/'Library/LaunchAgents';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'org.reviewguard.daily.plist';target=f'gui/{os.getuid()}/org.reviewguard.daily'
    # bootout has a nonzero result when there is no previous registration.
    subprocess.run(['launchctl','bootout',target],capture_output=True,check=False)
    if remove:
        path.unlink(missing_ok=True);return {'scheduled':False,'removed':str(path)}
    project=Path(__file__).resolve().parent.parent
    payload={'Label':'org.reviewguard.daily','ProgramArguments':[sys.executable,'-m','reviewguard','--data-dir',str(root),'daily'],
             'WorkingDirectory':str(project),'StartCalendarInterval':{'Hour':hour,'Minute':0},'RunAtLoad':True,
             'EnvironmentVariables':{'OLLAMA_NO_CLOUD':'1','PYTHONUNBUFFERED':'1'},
             'StandardOutPath':str(root/'daily.log'),'StandardErrorPath':str(root/'daily-error.log')}
    path.write_bytes(plistlib.dumps(payload))
    done=subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],capture_output=True,text=True)
    if done.returncode:raise RuntimeError('Scheduler registration failed: '+done.stderr.strip())
    return {'scheduled':True,'hour_local':hour,'plist':str(path),'note':'Runs while logged in; missed sleep-time jobs follow macOS launchd behavior. Mac must have internet and local Ollama running for AI proposals.'}
