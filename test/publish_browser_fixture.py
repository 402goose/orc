"""Disposable Git+HTTP fixture. All agents and GitHub requests are local fakes."""
import json
import os
from pathlib import Path
import sys
import tempfile

from publish_test import seed_git, manifest
from ui_test import seed_workspace
from fusion_ui import ControlRoom, Server
import fusion_publish as pub

with tempfile.TemporaryDirectory(prefix="orc-publish-browser-") as directory:
    root = Path(directory)
    workspace, remote = seed_git(root)
    config = seed_workspace(workspace)
    worker = workspace / "codex-fixture"
    worker.write_text(worker.read_text().replace("prompt = sys.stdin.read()", "prompt = sys.stdin.read()\nfrom pathlib import Path\nif 'Role: implementation' in prompt:\n    Path('app.txt').write_text('auto published fix\\n')"))
    os.environ.update(ORC_HOME=str(root / "orc-home"), FUSION_TELEMETRY="0", FUSION_DECISIONS_MODE="off")
    binaries = root / "bin"
    binaries.mkdir()
    fake = binaries / "gh"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args=sys.argv[1:]
path=Path(os.environ['FUSION_FAKE_GITHUB'])
state=json.loads(path.read_text()) if path.exists() else {'prs':[], 'calls':[]}
state['calls'].append(args)
def arg(name): return args[args.index(name)+1]
result=None
if args[:2]==['pr','list']:
    result=[p for p in state['prs'] if p['headRefName']==arg('--head')]
elif args[:2]==['pr','create']:
    n=len(state['prs'])+1
    p={'number':n,'url':f'https://github.com/fixture/project/pull/{n}','baseRefName':arg('--base'),'headRefName':arg('--head'),'state':'OPEN','isDraft':'--draft' in args,'statusCheckRollup':[{'name':'unit tests','status':'COMPLETED','conclusion':'SUCCESS'}]}
    state['prs'].append(p)
    result=p['url']
elif args[:2]==['pr','view']:
    result=next(p for p in state['prs'] if args[2] in [p['headRefName'],str(p['number'])])
else: raise SystemExit('Unsupported fake gh args: '+str(args))
path.write_text(json.dumps(state))
print(result if isinstance(result,str) else json.dumps(result))
''')
    fake.chmod(0o755)
    os.environ["FUSION_FAKE_GITHUB"] = str(root / "github.json")
    os.environ["PATH"] = str(binaries) + os.pathsep + os.environ["PATH"]
    data = manifest(workspace)
    data["schema"] = "fusion.workflow.v1"
    for node in data["nodes"].values():
        node.update(agent="codex", attempts=1, task="Fixture task")
    data["spec"] = {"graph": {"nodes": list(data["nodes"].values())}}
    pub.save(pub.root_for(workspace, "wf-test") / "manifest.json", data)
    (workspace / "app.txt").write_text("manually published fix\n")
    app = ControlRoom(workspace, root / "registry.json")
    server = Server(0, app)
    print(json.dumps({"url":server.url,"workspace":str(workspace),"root":str(root)}), flush=True)
    try: server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        for child in app.children: child.wait(timeout=10)
