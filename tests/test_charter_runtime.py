"""Zero-cost runtime tests: Docker is mocked and never launched."""
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from charter.runtime import DockerRuntime, OUTPUT_LIMIT, _bounded_run, validate_population


def tar_bytes(name="./notes.txt", text=b"private notes", kind=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(text)
        if kind:
            member.type = kind
            member.linkname = "/outside"
            member.size = 0
        archive.addfile(member, io.BytesIO(text))
    return output.getvalue()


def result(stdout=b"", **changes):
    return {"stdout": stdout, "stderr": b"", "returncode": 0,
            "timed_out": False, "truncated": False, **changes}


class PopulationTests(unittest.TestCase):
    def test_explicit_mixed_population(self):
        self.assertEqual(validate_population([{"model": "vendor/a", "count": 2},
                                              {"model": "vendor/b", "count": 1}]),
                         [{"agent_id": "agent-001", "model": "vendor/a"},
                          {"agent_id": "agent-002", "model": "vendor/a"},
                          {"agent_id": "agent-003", "model": "vendor/b"}])

    def test_invalid_populations(self):
        for population in (None, [], {}, [{"model": "", "count": 1}],
                           [{"model": " a", "count": 1}],
                           [{"model": "a", "count": True}],
                           [{"model": "a", "count": 0}],
                           [{"model": "a", "count": 129}],
                           [{"model": "a", "count": 1, "key": "bad"}]):
            with self.subTest(population=population), self.assertRaises(ValueError):
                validate_population(population)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = DockerRuntime(self.directory.name, ["agent-001", "agent-002"])

    def test_prepare_offline_and_complete_isolation(self):
        with patch("subprocess.Popen") as popen:
            path = self.runtime.prepare()
            popen.assert_not_called()
        config = json.loads(path.read_text())
        self.assertEqual(set(config["services"]), set(self.runtime.agent_ids))
        for service in config["services"].values():
            self.assertNotIn("volumes", service)
            self.assertNotIn("environment", service)
            self.assertEqual(service["user"], "1000:1000")
            self.assertEqual(service["network_mode"], "none")
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
            self.assertEqual(service["mem_limit"], "512m")
            self.assertEqual(service["cpus"], 1)
            self.assertEqual(service["pids_limit"], 128)
            self.assertIn("uid=1000,gid=1000", service["tmpfs"][0])
            self.assertIn("@sha256:", service["image"])
        with self.assertRaises(FileExistsError):
            self.runtime.prepare()

    def test_bad_ids_and_unpinned_image_rejected(self):
        for ids in ([], ["../escape"], ["agent-001", "agent-001"]):
            with self.assertRaises(ValueError):
                DockerRuntime(self.directory.name, ids)
        with self.assertRaises(ValueError):
            DockerRuntime(self.directory.name, ["agent-001"], image="ubuntu:latest")

    @patch("charter.runtime._bounded_run")
    def test_explicit_launch_and_shell_args(self, run):
        run.return_value = result(b"ok")
        self.runtime.prepare()
        self.assertFalse(self.runtime.started)
        self.runtime.start()
        launch = run.call_args.args[0]
        self.assertEqual(launch[-3:], ["up", "--detach", "--no-build"])
        command = "printf '%s' '$(do-not-evaluate-on-host)'"
        output = self.runtime.bash("agent-002", command)
        args = run.call_args.args[0]
        self.assertEqual(args[-11:], ["exec", "-T", "--user", "1000:1000", "agent-002",
                                     "timeout", "--signal=KILL", "30s", "/bin/bash", "-lc", command])
        self.assertEqual(output["stdout"], "ok")
        self.assertEqual(run.call_args.kwargs, {"timeout": 35, "limit": OUTPUT_LIMIT})
        with self.assertRaises(ValueError):
            self.runtime.bash("agent-003", "true")
        with self.assertRaises(RuntimeError):
            self.runtime.cleanup()
        run.return_value = result(returncode=137)
        self.assertTrue(self.runtime.bash("agent-001", "true")["timed_out"])

    @patch("charter.runtime._bounded_run")
    def test_archive_then_project_scoped_cleanup(self, run):
        self.runtime.prepare()
        run.return_value = result()
        self.runtime.start()
        run.return_value = result(tar_bytes())
        destination = self.runtime.archive()
        for actor in self.runtime.agent_ids:
            self.assertEqual((destination / actor / "notes.txt").read_text(), "private notes")
        with self.assertRaises(RuntimeError):
            self.runtime.bash("agent-001", "true")
        run.return_value = result()
        self.runtime.cleanup()
        args = run.call_args.args[0]
        self.assertIn(self.runtime.project, args)
        self.assertEqual(args[-2:], ["down", "--remove-orphans"])
        self.assertFalse(self.runtime.started)

    @patch("charter.runtime._bounded_run")
    def test_archive_failure_retains_containers(self, run):
        self.runtime.prepare()
        run.return_value = result()
        self.runtime.start()
        run.return_value = result(tar_bytes(), truncated=True)
        with self.assertRaises(RuntimeError):
            self.runtime.archive()
        self.assertFalse(self.runtime.archived)
        self.assertTrue(self.runtime.started)
        with self.assertRaises(RuntimeError):
            self.runtime.cleanup()

    def test_archive_rejects_traversal_links_and_existing_symlinks(self):
        for name, kind in (("../escape", None), ("/escape", None),
                           ("link", tarfile.SYMTYPE), ("link", tarfile.LNKTYPE),
                           ("device", tarfile.CHRTYPE)):
            with self.subTest(name=name, kind=kind), self.assertRaises(ValueError):
                self.runtime._export_tar(tar_bytes(name, kind=kind), Path(self.directory.name) / "export")
        target = Path(self.directory.name) / "linked"
        target.mkdir()
        (target / "notes.txt").symlink_to(Path(self.directory.name) / "outside")
        with self.assertRaises(ValueError):
            self.runtime._export_tar(tar_bytes(), target)


class BoundedOutputTests(unittest.TestCase):
    @patch("charter.runtime.subprocess.Popen")
    def test_pipe_drain_output_bound_and_timeout(self, popen):
        process = popen.return_value
        process.stdout = io.BytesIO(b"x" * 200)
        process.stderr = io.BytesIO(b"y" * 200)
        process.wait.side_effect = [subprocess.TimeoutExpired("docker", 1), None]
        process.returncode = -9
        output = _bounded_run(["docker", "fake"], timeout=1, limit=32)
        self.assertEqual(len(output["stdout"]), 32)
        self.assertEqual(len(output["stderr"]), 32)
        self.assertTrue(output["truncated"])
        self.assertTrue(output["timed_out"])
        process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
