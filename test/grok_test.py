"""Grok's documented streaming-json projection and legacy plain runs."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_progress as progress
from fusion_ui import activity_entries


def stream(*events):
    return '\n'.join(json.dumps(e) for e in events)


class GrokTest(unittest.TestCase):
    def test_stream_handoff_ignores_thoughts_tools_and_earlier_commentary(self):
        output = stream(
            {'type':'text','data':'I will inspect the repo.'},
            {'type':'thought','data':'private reasoning'},
            {'type':'tool_call','toolCallId':'c1','kind':'execute','rawInput':{'command':'pytest'}},
            {'type':'tool_call_update','toolCallId':'c1','status':'completed','rawOutput':'STATUS: error'},
            {'type':'text','data':'STATUS: success\nSUMMARY: checked\n'},
            {'type':'text','data':'TESTS: pytest passed\nBLOCKERS: none'},
            {'type':'usage','usage':{'input_tokens':99}},
            {'type':'end','sessionId':'s1','stopReason':'end_turn','usage':{'input_tokens':120,'output_tokens':20},'total_cost_usd':.02,'modelUsage':{'grok-fixture':{}}},
        )
        session, answer, failure, usage, model, _ = core.parse_grok_output(output)
        self.assertEqual(session,'s1')
        self.assertTrue(answer.startswith('STATUS: success'))
        self.assertNotIn('private',answer)
        self.assertNotIn('inspect',answer)
        self.assertIsNone(failure)
        self.assertEqual(usage,{'input_tokens':120,'output_tokens':20,'cost_usd':.02})
        self.assertEqual(model,'grok-fixture')
        self.assertEqual(core.parse_handoff(answer)['tests'],['pytest passed'])

    def test_truncated_cancelled_and_failed_streams_cannot_claim_success(self):
        text = {'type':'text','data':'STATUS: success\nSUMMARY: checked'}
        for ending in [[],[{'type':'error','message':'Request failed'}],
                       [{'type':'end','stopReason':'max_tokens'}],
                       [{'type':'end','stopReason':'cancelled'}]]:
            with self.subTest(ending=ending):
                self.assertTrue(core.parse_grok_output(stream(text,*ending))[2])
        self.assertEqual(core.parse_grok_output(stream(text,{'type':'end','stopReason':'end_turn'}))[3],{})
        self.assertNotIn('cost_usd',core.parse_grok_output(stream(text,{'type':'end','total_cost_usd':.2,'cost_is_partial':True}))[3])

    def test_public_activity_pairs_tool_updates_without_leaking_thoughts(self):
        events = [
            {'type':'text','data':'Checking '}, {'type':'text','data':'**tests**.'},
            {'type':'thought','data':'not public'},
            {'type':'tool_call','toolCallId':'c1','title':'Terminal','toolName':'run_terminal_command'},
            {'type':'tool_call_update','toolCallId':'c1','kind':'execute','rawInput':{'command':'pytest -q'}},
            {'type':'tool_call_update','toolCallId':'c1','status':'failed','rawOutput':'failed assertion'},
            {'type':'text','data':'Investigating the failure.'},
        ]
        entries = activity_entries(stream(*events)+'\n{"type":')
        self.assertEqual(len(entries),3)
        self.assertEqual(entries[0]['text'],'Checking **tests**.')
        self.assertEqual(entries[1]['kind'],'command')
        self.assertEqual(entries[1]['command'],'pytest -q')
        self.assertEqual(entries[1]['status'],'finished')
        self.assertTrue(entries[1]['failed'])
        self.assertEqual(entries[1]['output'],'failed assertion')
        self.assertNotIn('not public',json.dumps(entries))
        self.assertIsNone(progress.worker_message(json.dumps(events[2])))
        self.assertEqual(progress.worker_message(json.dumps(events[0])),'Checking')
        self.assertNotIn('SECRET',progress.worker_message(json.dumps({'type':'tool_call','title':'Execute SECRET'})))

    def test_legacy_plain_output_is_visible_before_newline_and_keeps_markdown(self):
        text = '\x1b[31mChecking **tests**.\nNext: Postgres'
        self.assertEqual(activity_entries(text,plain=True)[0]['text'],'Checking **tests**.\nNext: Postgres')
        self.assertEqual(activity_entries(text),[])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            stderr=io.StringIO()
            with progress.session(True,stderr):
                result=progress.run_logged([sys.executable,'-c',"import sys,time;sys.stdout.write('Checking tests');sys.stdout.flush();time.sleep(.3)"],cwd=root,env=os.environ.copy(),input=None,timeout=5,stdout_path=root/'stdout.log',stderr_path=root/'stderr.log',label='grok',plain_output=True)
            self.assertEqual(result.stdout,'Checking tests')
            self.assertIn('Checking tests',stderr.getvalue())
            self.assertEqual(core.parse_grok_output(result.stdout,'plain')[1],'Checking tests')

    def test_dispatch_requests_stream_and_saves_public_handoff(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'FUSION_DECISIONS_MODE':'off','FUSION_TELEMETRY':'0'}):
            root=Path(d)
            worker=root/'grok-fixture'
            worker.write_text(f'#!{sys.executable}\n'+'''import json,sys
assert sys.argv[sys.argv.index('--output-format')+1]=='streaming-json'
print(json.dumps({'type':'text','data':'Inspecting first.'}))
print(json.dumps({'type':'tool_call','toolCallId':'t1','kind':'read'}))
print(json.dumps({'type':'thought','data':'do not expose'}))
print(json.dumps({'type':'text','data':'STATUS: success\\nSUMMARY: reviewed\\nCHANGED: none\\nTESTS: fixture passed\\nBLOCKERS: none'}))
print(json.dumps({'type':'end','stopReason':'end_turn','sessionId':'fixture','usage':{'input_tokens':12,'output_tokens':5}}))
''')
            worker.chmod(0o755)
            config=core.deep_merge(core.DEFAULTS,{'grok':{'command':str(worker)},'decisions':{'mode':'off'}})
            task=core.make_task(root,'grok','Review fixture','review',[],[],None,False,False)
            result=core.dispatch(config,task,core.RunStore(root))
            self.assertEqual(result['status'],'success',result)
            self.assertEqual(result['summary'],'reviewed')
            self.assertEqual(result['tests'],['fixture passed'])
            self.assertEqual(task['resolved']['output_format'],'streaming-json')
            answer=Path(result['artifacts']['answer']).read_text()
            self.assertNotIn('Inspecting',answer)
            self.assertNotIn('do not expose',answer)


if __name__ == '__main__':
    unittest.main()
