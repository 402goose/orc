"""Real scouting/queue HTTP jobs with fake GitHub and workers; no external calls."""
import json
import os
from pathlib import Path
import sys
import tempfile

from publish_test import seed_git
from truffle_test import issue, candidate
from ui_test import seed_workspace
from fusion_ui import ControlRoom, Server

with tempfile.TemporaryDirectory(prefix="orc-truffle-browser-") as directory:
    root = Path(directory)
    workspace, remote = seed_git(root)
    seed_workspace(workspace)
    issues = [issue(n) for n in range(1, 5)]
    issues[0]["title"] = "Preserve typed API errors <script>window.PWNED=true</script>"
    issues[1]["title"] = "Read payment input once"
    issues[2]["assignees"] = [{"login": "owner"}]
    if os.environ.get("SURVEY_BROWSER"):
        issues = [issue(n) for n in range(1, 20)]
        issues[0].update(title="Epic: The old payment oak", body="- [ ] #2\n- [ ] #3\n- [ ] #4")
        issues[1]["title"] = "Preserve typed errors <script>window.PWNED=true</script>"
        issues[4].update(title="Tracking: A quieter forest", body="#6")
        issues[5]["parent"] = {k:issues[4][k] for k in ("number","title","url","state")}
        issues[18]["assignees"] = [{"login":"owner"}]
    (root / "issues.json").write_text(json.dumps(issues))
    suggestion = {"candidates": [candidate(1), candidate(2)], "skipped": [{"number": 4, "reason": "Unclear reproduction; needs product input"}]}
    worker = workspace / "codex-fixture"
    worker.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
prompt=sys.stdin.read()
if 'TRUFFLE_SURVEY_V1' in prompt:
    import re, time
    packet=Path(re.search(r'Read the saved packet at ([^\\n]+)\\. Issue text',prompt).group(1))
    rows=json.loads(packet.read_text())
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Checking the roots and source evidence for '+str([r['number'] for r in rows])}}),flush=True)
    time.sleep(3)
    grades=[]
    for row in rows:
        n=row['number']; grade='A' if n%4==2 else 'B' if n%4==3 else 'C' if n%4==0 else 'D'
        assessment={'number':n,'grade':grade,'reason':'Checked source; bounded fix' if grade in 'AB' else 'Needs a clearer reproduction' if grade=='C' else 'Scope is too broad'}
        if grade in 'AB': assessment['candidate']={**CANDIDATE,'number':n}
        grades.append(assessment)
    message='STATUS: success\\nSUMMARY: Batch source assessment complete\\n```truffle-grades\\n'+json.dumps({'grades':grades})+'\\n```\\nCHANGED: none\\nTESTS: source quotes inspected\\nBLOCKERS: none'
elif 'TRUFFLE_SCOUT_V1' in prompt:
    message='STATUS: success\\nSUMMARY: Two source-backed candidates\\n```truffle\\n'+json.dumps(SUGGESTION)+'\\n```\\nCHANGED: none\\nTESTS: source inspected\\nBLOCKERS: none'
else:
    if 'Role: implementation' in prompt:
        Path('app.txt').write_text('fixed\\n')
    message='STATUS: success\\nSUMMARY: Verified fixture fix\\nCHANGED: app.txt\\nTESTS: fixture regression passed\\nBLOCKERS: none'
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':message}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':80}}),flush=True)
'''.replace("SUGGESTION", repr(suggestion)).replace("CANDIDATE", repr(candidate(1))))
    worker.chmod(0o755)
    binaries = root / "bin"
    binaries.mkdir()
    fake = binaries / "gh"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args=sys.argv[1:]
root=Path(os.environ['FUSION_FAKE_GITHUB_ROOT'])
issues=json.loads((root/'issues.json').read_text())
path=root/'github.json'
state=json.loads(path.read_text()) if path.exists() else {'prs':[], 'calls':[]}
state['calls'].append(args)
def arg(name): return args[args.index(name)+1]
if args[:2]==['api','graphql']:
    cursor=next((a.split('=',1)[1] for a in args if a.startswith('cursor=')), '0')
    start=int(cursor); batch=issues[start:start+10]; end=start+len(batch)
    result={'data':{'repository':{'issues':{'totalCount':len(issues),'nodes':[{**i,'labels':{'nodes':i['labels']},'assignees':{'nodes':i['assignees']}} for i in batch], 'pageInfo':{'hasNextPage':end<len(issues),'endCursor':str(end)}}}}}
elif args[:2]==['issue','list']: result=issues
elif args[:2]==['issue','view']: result=next(i for i in issues if str(i['number'])==args[2])
elif args[:2]==['pr','list']:
    result=[p for p in state['prs'] if '--head' not in args or p['headRefName']==arg('--head')]
elif args[:2]==['pr','create']:
    n=len(state['prs'])+1
    p={'number':n,'url':f'https://github.com/fixture/project/pull/{n}','baseRefName':arg('--base'),'headRefName':arg('--head'),'state':'OPEN','isDraft':'--draft' in args,'closingIssuesReferences':[],'body':Path(arg('--body-file')).read_text()}
    state['prs'].append(p)
    result=p['url']
elif args[:2]==['pr','view']: result=next(p for p in state['prs'] if args[2] in [p['headRefName'],str(p['number'])])
else: raise SystemExit('Unsupported fake gh args: '+str(args))
path.write_text(json.dumps(state))
print(result if isinstance(result,str) else json.dumps(result))
''')
    fake.chmod(0o755)
    os.environ.update(ORC_HOME=str(root / "orc-home"), FUSION_TELEMETRY="0", FUSION_DECISIONS_MODE="off",
                      FUSION_FAKE_GITHUB_ROOT=str(root), PATH=str(binaries) + os.pathsep + os.environ["PATH"])
    app = ControlRoom(workspace, root / "registry.json")
    server = Server(0, app)
    print(json.dumps({"url": server.url, "workspace": str(workspace), "root": str(root)}), flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for child in app.children:
            child.wait(timeout=10)
