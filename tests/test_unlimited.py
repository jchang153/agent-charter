import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from charter.budget import BudgetGateway
from charter.state import SessionState
from charter.runner import validate_config
from test_charter_budget import Handler,Upstream
class UnlimitedTests(unittest.TestCase):
 def test_charges_continue_above_ten_without_admission_cap(self):
  with tempfile.TemporaryDirectory() as d:
   g=BudgetGateway('fake-test-key',d,limit=None,agent_limits={'a':None})
   g._spent=50;g._entries=[{'agent':'a','cost':50,'tokens':1}]
   replies=[]
   with patch.object(g,'_reply',side_effect=lambda h,s,b:replies.append(s)),patch('urllib.request.urlopen',return_value=Upstream({'usage':{'cost':.001,'total_tokens':1},'choices':[]})):
    g._handle_post(Handler('a'))
   self.assertEqual(replies,[200]);self.assertIsNone(g.snapshot()['limit_usd']);self.assertAlmostEqual(g.snapshot()['agents']['a']['spent_usd'],50.001)
 def test_budget_exit_recovers_but_voluntary_exit_does_not(self):
  with tempfile.TemporaryDirectory() as d:
   s=SessionState(['a','b'],Path(d));s.withdraw_for_budget('a');s.resume_authorized_budget_change('a');self.assertEqual(s.snapshot()['participants']['a']['status'],'active');self.assertNotIn('a',s.export()['controller_withdrawals'])
   s.dispatch('b','withdraw',{'reason':'Cannot endorse','unresolved_objections':'Disagree'})
   with self.assertRaises(ValueError):s.resume_authorized_budget_change('b')
   restored=SessionState(['a','b'],Path(d));self.assertEqual(restored.snapshot()['participants']['a']['status'],'active')
 def test_uncapped_config_explicit_null(self):
  self.assertIsNone(validate_config({'population':[{'model':'a','count':1}],'agent_budget_usd':None})['agent_budget_usd'])
 def test_canceled_calls_are_not_replayed_and_original_prefix_stays_intact(self):
  from charter.continuation import policy_change_transcript
  original=[{'role':'assistant','tool_calls':[{'id':'pending','type':'function','function':{'name':'constitution_edit','arguments':'{}'}}]}]
  result,canceled=policy_change_transcript(original)
  self.assertEqual(canceled,['pending']);self.assertEqual(result[:1],original);self.assertEqual(len(original),1)
  import json
  self.assertFalse(json.loads(result[1]['content'])['executed']);self.assertNotIn('budget',result[-1]['content'])
 def test_controlled_pause_recovery_keeps_revision_and_journal(self):
  with tempfile.TemporaryDirectory() as d:
   s=SessionState(['a','b'],Path(d));s.interrupt('a','controller_stopped');s.resume_authorized_budget_change('a');restored=SessionState(['a','b'],Path(d));self.assertEqual(restored.snapshot()['participants']['a']['status'],'active')
