"""Observer projection checks, with no API or Docker calls."""
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
from charter.dashboard import observer_snapshot, render


class DashboardTests(unittest.TestCase):
    def test_empty_observer_and_safe_embedding(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = observer_snapshot(directory)
            self.assertEqual(snapshot["participants"], [])
            shared = Path(directory) / "shared"
            shared.mkdir()
            (shared / "snapshot.json").write_text(json.dumps({"constitution": {"revision": 1,
                "text": "</script><script>alert('no')</script>"}, "messages": []}))
            html = render(directory)
            self.assertNotIn("/* INITIAL_STATE */", html)
            self.assertNotIn("</script><script>alert('no')</script>", html)
            self.assertIn("\\u003c/script>", html)

    def test_operator_projection_does_not_read_host_credentials_or_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps({"status": "running", "config": {
                "actors": [{"agent_id": "agent-001", "model": "dummy/model"}]}}))
            actor = root / "agent-001"
            actor.mkdir()
            (actor / "model_responses.json").write_text(json.dumps([{"private_reasoning": "not projected"}]))
            snapshot = observer_snapshot(root)
            self.assertEqual(snapshot["participants"][0]["responses"], 1)
            self.assertNotIn("private_reasoning", json.dumps(snapshot))

    def test_historical_approvals_and_technical_exclusion_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared").mkdir()
            actors = [{"agent_id": "agent-001", "model": "dummy/a"},
                      {"agent_id": "agent-002", "model": "dummy/b"}]
            (root / "manifest.json").write_text(json.dumps({"config": {"actors": actors}}))
            history = [{"actor": "agent-001", "revision": 1, "event": "signed",
                        "statement": "Approved earlier draft", "timestamp": 1},
                       {"actor": "agent-001", "revision": 1, "event": "invalidated", "timestamp": 2}]
            (root / "shared/snapshot.json").write_text(json.dumps({
                "constitution": {"revision": 2, "text": "Latest draft"},
                "history": [{"revision": 1, "text": "Earlier draft"},
                            {"revision": 2, "text": "Latest draft"}],
                "approval_history": history,
                "participants": {"agent-001": {"status": "active", "signature": None},
                    "agent-002": {"status": "technical_excluded", "signature": None}},
                "controller_exclusions": {"agent-002": {"reason": "HTTP retries exhausted"}},
                "required_signers": ["agent-001"]}))
            snapshot = observer_snapshot(root)
            self.assertEqual(snapshot["approval_history"], history)
            self.assertEqual(snapshot["required_signers"], ["agent-001"])
            self.assertEqual(snapshot["constitution"]["revision"], 2)
            self.assertEqual(len(snapshot["history"]), 2)
            excluded = snapshot["participants"][1]
            self.assertFalse(excluded["signed"])
            self.assertEqual(excluded["status"], "technical_excluded")
            self.assertEqual(excluded["technical_exclusion"]["reason"], "HTTP retries exhausted")
            html = render(root)
            self.assertIn("past approvals are not current consent", html)
            self.assertIn("Technical exclusion · outside quorum · no endorsement", html)

    def test_quorum_fallback_excludes_technical_and_voluntary_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared").mkdir()
            actors = [{"agent_id": f"agent-{i:03d}", "model": "dummy/a"} for i in range(1, 4)]
            (root / "manifest.json").write_text(json.dumps({"config": {"actors": actors}}))
            (root / "shared/snapshot.json").write_text(json.dumps({"participants": {
                "agent-001": {"status": "active", "signature": {"revision": 3}},
                "agent-002": {"status": "withdrawn"},
                "agent-003": {"status": "technical_excluded"}}}))
            snapshot = observer_snapshot(root)
            self.assertEqual(snapshot["required_signers"], ["agent-001"])
            self.assertEqual(snapshot["participants"][0]["signature"], {"revision": 3})

    @unittest.skipUnless(shutil.which("node"), "Node needed for dashboard rendering check")
    def test_rendered_revision_approval_counts_and_exclusion_labels(self):
        script = r"""
const fs=require('fs'),vm=require('vm');
const html=fs.readFileSync(process.argv[1],'utf8');
function element(tag){return {tag,children:[],textContent:'',value:'',append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},addEventListener(){}};}
const elements={};const context={document:{getElementById(id){return elements[id]||(elements[id]=element('div'))},createElement:element,querySelectorAll(){return []}},location:{protocol:'file:'},navigator:{},setTimeout(){},Date,Set};
vm.createContext(context);vm.runInContext(html.split('<script>')[1].split('</script>')[0],context);
vm.runInContext(`state={updated_at:'2026-10-08',run_status:'running',outcome:'incomplete',constitution:{revision:2,text:'Latest'},history:[{revision:1,text:'Earlier'}],approval_history:[{actor:'a',revision:1,event:'signed',statement:'Past approval',timestamp:1}],required_signers:['a'],participants:[{id:'a',model:'dummy',status:'active',signature:{revision:2},signed:true,responses:1},{id:'b',model:'dummy',status:'technical_excluded',signature:null,responses:1}],messages:[],billing:{}};paint();`,context);
if(!elements.approval.textContent.includes('1 of 1 remaining participants signed'))throw Error(elements.approval.textContent);
function texts(n){return [n.textContent,...n.children.flatMap(texts)]}
if(!texts(elements.roster).join(' ').includes('Technical exclusion · outside quorum · no endorsement'))throw Error('missing exclusion label');
vm.runInContext('selected=1;paintDocument();',context);
if(!elements.approval.textContent.includes('past approvals are not current consent'))throw Error('historical consent confusion');
if(!texts(elements.approvals).join(' ').includes('Past approval'))throw Error('lost historical approval');
"""
        template = Path(__file__).resolve().parents[1] / "charter/dashboard.html"
        subprocess.run([shutil.which("node"), "-e", script, str(template)], check=True, capture_output=True, text=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
