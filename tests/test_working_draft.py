import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from charter.state import SessionState
from charter.prompts import initial_messages
from charter.tools import tool_definitions
from charter.runner import prepare, validate_config
from charter.budget import BudgetGateway
from test_charter_budget import Handler, Upstream


class WorkingDraftTests(unittest.TestCase):
    def test_short_discussion_and_full_draft_have_separate_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(['a','b'], Path(directory), shared_authorship=True,
                                 draft_workspace=True, discussion_max_chars=2000)
            self.assertFalse(state.dispatch('a','board_post',{'title':'Too long','text':'x'*2001})['ok'])
            self.assertTrue(state.dispatch('a','board_post',{'title':'Discussion','text':'x'*2000})['ok'])
            state.dispatch('a','constitution_read',{})
            self.assertTrue(state.dispatch('a','constitution_update',{
                'expected_revision':0,'text':'Full working draft. '*300,'rationale':'Initial draft'})['ok'])
            self.assertEqual(state.snapshot()['status'],'active')
            self.assertEqual(len(state.snapshot()['messages']),1)
            self.assertFalse(state.dispatch('a','constitution_propose',{
                'expected_revision':1,'title':'Draft in thread','text':'Text','rationale':'Reason'})['ok'])
            self.assertFalse(state.dispatch('a','constitution_submit',{'revision':1})['ok'])
            state.dispatch('a','constitution_read',{})
            state.dispatch('a','constitution_sign',{'revision':1,'statement':'Endorse'})
            state.dispatch('b','constitution_read',{})
            self.assertTrue(state.dispatch('b','constitution_update',{
                'expected_revision':1,'text':'Revised working draft','rationale':'Revision'})['ok'])
            self.assertIsNone(state.snapshot()['participants']['a']['signature'])
            state.dispatch('b','constitution_read',{})
            self.assertFalse(state.dispatch('b','constitution_update',{
                'expected_revision':2,'text':'Another edit','rationale':'Revision'})['ok'])
            restored=SessionState(['a','b'],Path(directory))
            self.assertTrue(restored.draft_workspace)
            self.assertEqual(restored.discussion_max_chars,2000)

    def test_offline_preparation_records_identical_new_interface(self):
        config=validate_config({'population':[{'model':'dummy','count':2}],
            'draft_workspace':True,'shared_authorship':True,'discussion_max_chars':2000,
            'web_research':True,'agent_budget_usd':50})
        options={k:config[k] for k in ('draft_workspace','shared_authorship','discussion_max_chars','web_research')}
        prompts=initial_messages(**options)
        self.assertEqual(prompts,initial_messages(**options))
        self.assertIn('WORKING DRAFT',prompts[0]['content'])
        self.assertIn('2000 characters',prompts[0]['content'])
        definitions=tool_definitions(**options)
        names=[t.get('function',{}).get('name') for t in definitions]
        self.assertNotIn('constitution_propose',names)
        self.assertIn('openrouter:web_search',[t['type'] for t in definitions])
        self.assertIn('openrouter:web_fetch',[t['type'] for t in definitions])
        with tempfile.TemporaryDirectory() as directory,patch('subprocess.Popen') as process:
            _,manifest=prepare(config,Path(directory)/'review')
            process.assert_not_called()
            self.assertTrue(manifest['web_search'])
            self.assertEqual(manifest['container_network'],'none')
            self.assertEqual(json.loads((Path(directory)/'review/prompts.json').read_text()),prompts)

    def test_research_requests_are_bounded_and_costs_are_accounted(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway=BudgetGateway('fake-test-key',directory,limit=50.5,agent_limits={'a':50})
            tools=[t for t in tool_definitions(web_research=True) if t['type'].startswith('openrouter:')]
            payload={'model':'openai/gpt-5','messages':[],'tools':tools,'max_tokens':16}
            baseline=gateway._estimate('openai/gpt-5',100,16)
            reserved,_=gateway._request_reservation('openai/gpt-5',payload,100,16)
            self.assertGreater(reserved,baseline+0.02)
            replies=[]
            with patch.object(gateway,'_reply',side_effect=lambda h,s,b:replies.append(s)),patch('urllib.request.urlopen',return_value=Upstream({'usage':{'cost':0.03,'total_tokens':10},'choices':[]})) as upstream:
                handler=Handler('a');raw=json.dumps(payload).encode();handler.rfile=__import__('io').BytesIO(raw);handler.headers['Content-Length']=str(len(raw))
                gateway._handle_post(handler)
                self.assertEqual(replies[-1],200)
                self.assertAlmostEqual(gateway.snapshot()['spent_usd'],0.03)
                tools[0]['parameters']['max_uses']=100
                raw=json.dumps(payload).encode();handler.rfile=__import__('io').BytesIO(raw);handler.headers['Content-Length']=str(len(raw))
                gateway._handle_post(handler)
                self.assertEqual(replies[-1],400)
                self.assertEqual(upstream.call_count,1)
