import tempfile, unittest
from unittest.mock import patch
from charter.budget import BudgetGateway
from test_charter_budget import Handler, Upstream

class RecordedSpendingTests(unittest.TestCase):
    def test_configuration_preserves_explicit_policy(self):
        from charter.runner import validate_config
        self.assertTrue(validate_config({"population":[{"model":"a","count":1}],"recorded_spending_only":True})["recorded_spending_only"])
        with self.assertRaises(ValueError):validate_config({"population":[{"model":"a","count":1}],"recorded_spending_only":"true"})
    def test_large_reservation_does_not_stop_call_but_actual_spending_does(self):
        with tempfile.TemporaryDirectory() as d:
            g=BudgetGateway('fake-test-key',d,limit=50.5,agent_limits={'a':50},recorded_spending_only=True)
            g._spent=49.99;g._entries=[{'agent':'a','cost':49.99,'tokens':1}]
            replies=[]
            with patch.object(g,'_request_reservation',return_value=(100,100000)),patch.object(g,'_reply',side_effect=lambda h,s,b:replies.append(s)),patch('urllib.request.urlopen',return_value=Upstream({'usage':{'cost':.02,'total_tokens':1},'choices':[]})) as upstream:
                g._handle_post(Handler('a'))
                g._handle_post(Handler('a'))
            self.assertEqual(replies,[200,402]);self.assertEqual(upstream.call_count,1)
            self.assertAlmostEqual(g.snapshot()['spent_usd'],50.01)
            self.assertEqual(g.snapshot()['agents']['a']['stop_reason'],'dollars')
    def test_uncertain_reservations_are_retained_but_do_not_use_allowance(self):
        with tempfile.TemporaryDirectory() as d:
            g=BudgetGateway('fake-test-key',d,limit=50.5,agent_limits={'a':50},recorded_spending_only=True,allow_reserved_uncertainty=True)
            g._reserved={'failed':100};g._reserved_agents={'failed':'a'};g._reserved_tokens={'failed':10000}
            replies=[]
            with patch.object(g,'_reply',side_effect=lambda h,s,b:replies.append(s)),patch('urllib.request.urlopen',return_value=Upstream({'usage':{'cost':.01,'total_tokens':1},'choices':[]})):
                g._handle_post(Handler('a'))
            self.assertEqual(replies,[200]);self.assertEqual(g.snapshot()['reserved_usd'],100)
