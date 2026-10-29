"""Offline preparation and explicit lifecycle for isolated constitution agents."""
from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import threading
import uuid
from pathlib import Path, PurePosixPath

DEFAULT_IMAGE = "aisiuk/inspect-tool-support@sha256:bbaecf7a093cfe24703c97d562bb059899e272cd5d57f16d9916fe05e6614419"
OUTPUT_LIMIT = 32_768
ARCHIVE_LIMIT = 64 * 1024 * 1024


def validate_population(population: list[dict]) -> list[dict]:
    """Expand an explicitly supplied population; never select a model or size."""
    if not isinstance(population, list) or not population:
        raise ValueError("population must be a nonempty list of model/count entries")
    agents = []
    for entry in population:
        if not isinstance(entry, dict) or set(entry) != {"model", "count"}:
            raise ValueError("each population entry must contain only model and count")
        model, count = entry["model"], entry["count"]
        if not isinstance(model, str) or not model.strip() or model != model.strip():
            raise ValueError("model must be a nonempty trimmed string")
        if type(count) is not int or count < 1:
            raise ValueError("count must be a positive integer")
        if len(agents) + count > 128:
            raise ValueError("population exceeds 128 agents")
        for _ in range(count):
            agents.append({"agent_id": f"agent-{len(agents) + 1:03d}", "model": model})
    return agents


def _bounded_run(args: list[str], *, timeout: int, limit: int) -> dict:
    """Drain both pipes while retaining at most limit bytes, including on timeout."""
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}

    def drain(name, pipe):
        while chunk := pipe.read(8192):
            available = max(0, limit - len(buffers[name]))
            buffers[name].extend(chunk[:available])
            truncated[name] |= len(chunk) > available
        pipe.close()

    readers = [threading.Thread(target=drain, args=(name, pipe), daemon=True)
               for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    for reader in readers:
        reader.join()
    return {"stdout": bytes(buffers["stdout"]), "stderr": bytes(buffers["stderr"]),
            "returncode": process.returncode, "timed_out": timed_out,
            "truncated": any(truncated.values())}


class DockerRuntime:
    """One container and ephemeral private workspace per anonymous participant.

    Constructor and prepare never invoke Docker. start is the explicit launch
    boundary. Successful archive is required before removing private workspaces.
    """

    def __init__(self, run_dir, agent_ids, docker="docker", image=DEFAULT_IMAGE):
        ids = list(agent_ids)
        if not ids or len(set(ids)) != len(ids) or any(
            not isinstance(actor, str) or not re.fullmatch(r"agent-[0-9]{3}", actor)
            for actor in ids
        ):
            raise ValueError("agent IDs must be unique anonymous IDs such as agent-001")
        if not isinstance(image, str) or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("a digest-pinned container image is required")
        self.run_dir = Path(run_dir).resolve()
        self.agent_ids = tuple(ids)
        self.docker = docker
        self.image = image
        self.project = "charter-" + uuid.uuid4().hex[:16]
        self.compose_path = self.run_dir / "compose.json"
        self.prepared = False
        self.started = False
        self.archived = False

    def prepare(self) -> Path:
        if self.started:
            raise RuntimeError("cannot prepare a running environment")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.compose_path.exists():
            raise FileExistsError("refusing to replace an existing runtime configuration")
        services = {}
        for actor in self.agent_ids:
            services[actor] = {
                "image": self.image, "command": ["/bin/sh", "-c", "exec sleep infinity"],
                "user": "1000:1000", "init": True, "working_dir": "/workspace",
                "network_mode": "none", "read_only": True, "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"], "mem_limit": "512m",
                "cpus": 1, "pids_limit": 128, "stop_grace_period": "1s",
                "tmpfs": ["/workspace:rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=0700",
                          "/tmp:rw,nosuid,nodev,noexec,size=32m,uid=1000,gid=1000,mode=0700"],
            }
        self.compose_path.write_text(json.dumps({"name": self.project, "services": services}, indent=2) + "\n")
        self.prepared = True
        return self.compose_path

    def _compose(self, *args) -> list[str]:
        return [self.docker, "compose", "--project-name", self.project,
                "--file", str(self.compose_path), *args]

    def _actor(self, actor):
        if actor not in self.agent_ids:
            raise ValueError("unknown agent")

    def start(self):
        if not self.prepared or self.started or self.archived:
            raise RuntimeError("prepare once before starting")
        # Mark before invocation so a partial launch remains recoverable.
        self.started = True
        result = _bounded_run(self._compose("up", "--detach", "--no-build"), timeout=120, limit=OUTPUT_LIMIT)
        if result["returncode"] or result["timed_out"]:
            raise RuntimeError("container launch failed; preserve environment for recovery")

    def bash(self, actor, command) -> dict:
        self._actor(actor)
        if not self.started or self.archived:
            raise RuntimeError("private workspace is not active")
        if not isinstance(command, str) or not command or len(command) > 16_384 or "\x00" in command:
            raise ValueError("command must contain 1–16384 characters without NUL")
        result = _bounded_run(self._compose("exec", "-T", "--user", "1000:1000", actor,
                             "timeout", "--signal=KILL", "30s", "/bin/bash", "-lc", command),
                              timeout=35, limit=OUTPUT_LIMIT)
        result["stdout"] = result["stdout"].decode("utf-8", errors="replace")
        result["stderr"] = result["stderr"].decode("utf-8", errors="replace")
        result["timed_out"] |= result["returncode"] in (124, 137)
        return result

    def archive(self) -> Path:
        if not self.started:
            raise RuntimeError("no containers have been started")
        destination = self.run_dir / "private_workspaces"
        if self.archived:
            return destination
        for actor in self.agent_ids:
            result = _bounded_run(self._compose("exec", "-T", "--user", "1000:1000", actor,
                                  "timeout", "--signal=KILL", "30s", "tar", "-C", "/workspace", "-cf", "-", "."),
                                  timeout=35, limit=ARCHIVE_LIMIT)
            if result["returncode"] or result["timed_out"] or result["truncated"]:
                raise RuntimeError(f"archive failed for {actor}; containers retained")
            self._export_tar(result["stdout"], destination / actor)
        self.archived = True
        return destination

    def restore(self, actor, source_dir):
        """Restore only regular files from a previously validated private export."""
        self._actor(actor)
        if not self.started or self.archived:
            raise RuntimeError("private workspace is not active")
        root = Path(source_dir).resolve()
        buffer = io.BytesIO()
        total = 0
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path in sorted(root.rglob("*")):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError("unsafe private restore source")
                if path.is_file():
                    total += path.stat().st_size
                if total > ARCHIVE_LIMIT:
                    raise ValueError("private restore exceeds archive size limit")
                archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
        result = subprocess.run(self._compose("exec", "-T", "--user", "1000:1000", actor,
            "timeout", "--signal=KILL", "30s", "tar", "-C", "/workspace", "--no-same-owner", "-xf", "-"),
            input=buffer.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
        if result.returncode:
            raise RuntimeError("private workspace restore failed")

    @staticmethod
    def _export_tar(data: bytes, target: Path):
        # Validate every member before writing; never extract links or devices.
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                    raise ValueError("unsafe private workspace archive member")
            target.mkdir(parents=True, exist_ok=True)
            root = target.resolve()
            for member in members:
                path = target / member.name
                if not path.resolve().is_relative_to(root) or path.is_symlink():
                    raise ValueError("unsafe private workspace destination")
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, path.open("wb") as output:
                        while chunk := source.read(64 * 1024):
                            output.write(chunk)

    def cleanup(self):
        if not self.started or not self.archived:
            raise RuntimeError("successful archive is required before cleanup")
        result = _bounded_run(self._compose("down", "--remove-orphans"), timeout=60, limit=OUTPUT_LIMIT)
        if result["returncode"] or result["timed_out"]:
            raise RuntimeError("container cleanup failed")
        self.started = False
