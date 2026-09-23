"""Real HTTP + durable automatic loop, deterministic learning jobs, no models."""
import json
import os
from pathlib import Path
from training_loop_test import TrainingLoopTest
from fusion_ui import Server
from fusion_publish import save

fixture=TrainingLoopTest()
fixture.setUp()
release=Path(fixture.temp.name)/'release-training'
original=fixture.worker
pending=None

def worker(workspace,body,**kwargs):
    global pending
    job=original(workspace,body,**kwargs)
    if body['action']=='train':
        pending=dict(job)
        job={**job,'status':'running','supervisor_pid':os.getpid()}
        directory=fixture.w/'.fusion/ui/jobs'/job['id']
        save(directory/'job.json',job)
        save(directory/'candidate.progress.json',dict(phase='training',done=2,total=12,loss=.62,
             loss_curve=[dict(step=1,loss=.8),dict(step=2,loss=.62)]))
    return job
fixture.app.launch.side_effect=worker

class FixtureServer(Server):
    def service_actions(self):
        global pending
        if pending and release.exists():
            save(fixture.w/'.fusion/ui/jobs'/pending['id']/'job.json',pending)
            pending=None
        super().service_actions()

server=FixtureServer(0,fixture.app)
print(json.dumps({'url':server.url,'root':fixture.temp.name,'workspace':str(fixture.w),'release':str(release)}),flush=True)
try:server.serve_forever(poll_interval=.1)
except KeyboardInterrupt:pass
finally:
    server.server_close()
    fixture.doCleanups()
