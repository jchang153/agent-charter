"""Offline upstream simulations: no credentials, sockets, or paid requests."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from charter.budget import BudgetGateway

class Handler:
    def __init__(self, actor):
        raw=json.dumps({'model':'openai/gpt-5','messages':[], 'max_tokens':16}).encode()
        self.headers={'x-agent-id':actor,'Content-Length':str(len(raw))}
        self.rfile=io.BytesIO(raw)
        self.path='/chat/completions'

class Upstream:
    status=200
    def __init__(self,body):self.body=json.dumps(body).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,*args):return self.body

class AccountingIsolationTests(unittest.TestCase):
    def test_larger_output_ceiling_is_forwarded_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = BudgetGateway('fake-test-key', directory, limit=50.5, agent_limits={'a':50})
            replies = []
            with patch.object(gateway, '_reply', side_effect=lambda h,s,b:replies.append(s)), patch('urllib.request.urlopen', return_value=Upstream({'usage':{'cost':0.001,'total_tokens':1},'choices':[]})) as upstream:
                for limit in (32768,65536,65537):
                    handler = Handler('a')
                    raw = json.dumps({'model':'openai/gpt-5','messages':[],'max_tokens':limit}).encode()
                    handler.rfile = io.BytesIO(raw)
                    handler.headers['Content-Length'] = str(len(raw))
                    gateway._handle_post(handler)
                self.assertEqual(replies, [200,200,400])
                self.assertEqual(upstream.call_count, 2)
                self.assertEqual(json.loads(upstream.call_args.args[0].data)['max_tokens'], 65536)

    def test_missing_usage_quarantines_only_affected_agent_with_reservation(self):
        with tempfile.TemporaryDirectory() as d:
            g=BudgetGateway('fake-test-key',d,agent_limits={'a':10,'b':10},allow_reserved_uncertainty=True)
            replies=[]
            with patch.object(g,'_reply',side_effect=lambda h,s,b:replies.append(s)), patch('urllib.request.urlopen',return_value=Upstream({'id':'generation-example'})) as upstream:
                g._handle_post(Handler('a'))
                self.assertEqual(replies[-1],502)
                self.assertGreater(g.snapshot()['reserved_usd'],0)
                self.assertTrue(g.snapshot()['safe_to_continue'])
                self.assertIn('a',g.snapshot()['quarantined_agents'])
                g._handle_post(Handler('a'))
                self.assertEqual(replies[-1],503)
                self.assertEqual(upstream.call_count,1)
            with patch.object(g,'_reply',side_effect=lambda h,s,b:replies.append(s)), patch('urllib.request.urlopen',return_value=Upstream({'usage':{'cost':0.001,'total_tokens':1},'choices':[]})):
                g._handle_post(Handler('b'))
                self.assertEqual(replies[-1],200)
            self.assertAlmostEqual(g.snapshot()['spent_usd'],0.001)
            restored=BudgetGateway('fake-test-key',d,agent_limits={'a':10,'b':10},allow_reserved_uncertainty=True)
            self.assertIn('a',restored.snapshot()['quarantined_agents'])
            self.assertTrue(restored.snapshot()['safe_to_continue'])
            pending=list(Path(d).glob('*.pending.json'))
            self.assertEqual(json.loads(pending[0].read_text())['generation_id'],'generation-example')

    def test_reconciliation_reads_existing_generation_without_repeating_inference(self):
        with tempfile.TemporaryDirectory() as d:
            g=BudgetGateway('fake-test-key',d,agent_limits={'a':10},allow_reserved_uncertainty=True)
            replies=[]
            with patch.object(g,'_reply',side_effect=lambda h,s,b:replies.append(s)), patch('urllib.request.urlopen',side_effect=[
                Upstream({'id':'gen-example','choices':[]}),
                Upstream({'data':{'id':'gen-example','model':'openai/gpt-5','total_cost':0.002,'tokens_prompt':3,'tokens_completion':4}})
            ]) as upstream:
                g._handle_post(Handler('a'))
                self.assertEqual(upstream.call_count,2)
                self.assertEqual(upstream.call_args_list[0].args[0].get_method(),'POST')
                self.assertEqual(upstream.call_args_list[1].args[0].get_method(),'GET')
                self.assertEqual(replies[-1],200)
                self.assertEqual(g.snapshot()['reserved_usd'],0)
                self.assertAlmostEqual(g.snapshot()['spent_usd'],0.002)
                self.assertFalse(g.snapshot()['quarantined_agents'])
