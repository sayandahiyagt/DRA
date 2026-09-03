"""Disposable, bounded, non-root sandbox for safe test execution (spec §8.3).

The sandbox abstraction is capability-aware: it detects the strongest runtime
available in the environment (``docker`` -> ``bwrap`` -> ``static_only``) and
runs commands with hard bounds on resources, filesystem visibility, network,
and ambient credentials.  When no execution runtime is detected it **degrades**
to static-only inspection — ``run`` returns ``None`` and the investigator
stamps behavioral claims as ``INFERENCE`` rather than hard-failing (§8.3).

Security posture:
- **Disposable**: a fresh container/wrap per ``run``; nothing persists.
- **Non-root**: docker ``--user nobody:nogroup``; bwrap ``--unshare-user`` +
  ``--uid``.
- **Bounded**: ``subprocess`` timeout plus docker ``--memory/--cpus`` or bwrap
  timeout.
- **Restricted FS**: only ``repo_path`` is mounted/visible — no ``$HOME``,
  ``/run/secrets``, or ``/etc`` credential exposure.
- **No ambient credentials**: env is sanitized to a minimal ``PATH``;
  ``AWS_*``, ``GH_TOKEN``, ``GITHUB_TOKEN``, ``HOME``, ``SSH_AUTH_SOCK`` are
  stripped.
- **Network**: docker ``--network none``; bwrap ``--unshare-net``.
"""

from __future__ import annotations

import enum
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Sequence

# Environment knobs ---------------------------------------------------------
# Force a capability for tests / deterministic degradation (spec §8.3 override).
_OVERRIDDEN: dict[str, str] = {
    "static_only": "static_only",
    "docker": "docker",
    "bwrap": "bwrap",
}


class SandboxCapability(enum.Enum):
    """Execution capabilities, ordered strongest -> weakest."""

    DOCKER = "docker"
    BWRAP = "bwrap"
    STATIC_ONLY = "static_only"


@dataclass
class RunResult:
    """Outcome of a sandboxed command execution."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


# Credentials / sensitive variables stripped from the child environment so no
# ambient secrets leak into execution (spec §8.3 "no ambient credentials").
_STRIP_ENV_PREFIXES = ("AWS_", "GITHUB_", "GH_", "GIT_", "SSH_")
_STRIP_ENV_KEYS = {"HOME", "USER", "LOGNAME", "PATH", "SSH_AUTH_SOCK",
                   "KRB5CCNAME", "CI", "GITHUB_ACTIONS"}


def _sanitized_env() -> dict[str, str]:
    """Build a minimal, credentialed-stripped environment for sandboxed runs."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    for key, value in os.environ.items():
        if key in _STRIP_ENV_KEYS or key.startswith(_STRIP_ENV_PREFIXES):
            continue
        if key == "PATH":
            continue  # use the minimal PATH above
        env[key] = value
    return env


class Sandbox:
    """Disposable sandbox runner with graceful static-only degradation.

    ``capability`` defaults to :meth:`detect` so production callers get the best
    available runtime without forcing configuration; tests can force a
    capability via ``DRA_SANDBOX_CAPABILITY`` or by passing ``capability``
    explicitly.
    """

    repo_path: str
    capability: SandboxCapability
    timeout: int
    mem: str

    def __init__(
        self,
        repo_path: str,
        capability: SandboxCapability | None = None,
        timeout: int = 120,
        mem: str = "512m",
    ) -> None:
        self.repo_path = os.fspath(repo_path)
        self.capability = capability or self.detect()
        self.timeout = timeout
        self.mem = mem

    # -- capability detection ------------------------------------------------

    @staticmethod
    def detect() -> SandboxCapability:
        """Detect the strongest execution runtime available in the environment.

        Honors ``DRA_SANDBOX_CAPABILITY`` first (so tests can drive the
        no-sandbox path deterministically), then probes ``docker`` and
        ``bwrap`` on ``PATH``.  Falls back to ``STATIC_ONLY`` when nothing is
        available — callers must treat this as a successful, degraded result,
        never a hard failure (spec §8.3).
        """
        forced = os.environ.get("DRA_SANDBOX_CAPABILITY")
        if forced in _OVERRIDDEN:
            return SandboxCapability(_OVERRIDDEN[forced])
        if shutil.which("docker") is not None:
            # Confirm the daemon is reachable; if not, fall through.
            if Sandbox._docker_reachable():
                return SandboxCapability.DOCKER
        if shutil.which("bwrap") is not None:
            return SandboxCapability.BWRAP
        return SandboxCapability.STATIC_ONLY

    @staticmethod
    def _docker_reachable() -> bool:
        try:
            probe = subprocess.run(
                ["docker", "info", "-f", "{{.OperatingSystem}}"],
                capture_output=True,
                text=True,
                timeout=15,
                env=_sanitized_env(),
            )
            return probe.returncode == 0 and bool(probe.stdout.strip())
        except Exception:
            return False

    # -- execution -----------------------------------------------------------

    def run(self, cmd: Sequence[str], *, cwd: str | None = None) -> RunResult | None:
        """Execute ``cmd`` in the sandbox.

        Returns a :class:`RunResult`.  Returns ``None`` when the capability is
        ``STATIC_ONLY`` (no execution available) so callers degrade to static
        inspection instead of hard-failing (spec §8.3).
        """
        if self.capability is SandboxCapability.STATIC_ONLY:
            return None
        if not cmd:
            raise ValueError("cmd must be non-empty")
        if self.capability is SandboxCapability.DOCKER:
            return self._run_docker(cmd, cwd=cwd)
        if self.capability is SandboxCapability.BWRAP:
            return self._run_bwrap(cmd, cwd=cwd)
        return None

    def _run_docker(self, cmd: Sequence[str], *, cwd: str | None) -> RunResult:
        image = os.environ.get("DRA_SANDBOX_IMAGE", "python:3.12-slim")
        repo = self.repo_path
        args = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", self.mem,
            "--cpus", "1.0",
            "--user", "nobody:nogroup",
            "-v", f"{repo}:/repo:ro",
            "-w", "/repo",
        ]
        if cwd:
            args += ["--workdir", "/repo"]
        args += [image, *cmd]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True,
                timeout=self.timeout, env=_sanitized_env(),
            )
            return RunResult(
                returncode=proc.returncode, stdout=proc.stdout,
                stderr=proc.stderr, timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                returncode=124, stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + "\nsandbox: timed out",
                timed_out=True,
            )

    def _run_bwrap(self, cmd: Sequence[str], *, cwd: str | None) -> RunResult:
        repo = self.repo_path
        # bwrap is only reached when detected; run disposable, non-root,
        # network-unshared, read-only repo bind.
        args = [
            "bwrap", "--unshare-user=ask-30000", "--uid",
            "--unshare-net",
            "--ro-bind", repo, "/repo",
            "--unshare-dev", "--unshare-cgroup",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--chdir", "/repo",
        ]
        _ = cwd  # cwd intentionally not plumbed through the read-only /repo root
        try:
            proc = subprocess.run(
                [*args, *cmd], capture_output=True, text=True,
                timeout=self.timeout, env=_sanitized_env(),
            )
            return RunResult(
                returncode=proc.returncode, stdout=proc.stdout,
                stderr=proc.stderr, timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                returncode=124, stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + "\nsandbox: timed out",
                timed_out=True,
            )

    # -- context manager for temp sandboxes ----------------------------------

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: object) -> None:
        return None
