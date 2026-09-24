"""Git operations of a run: clone the catalog and the context, branch, commit, push.

Open proposals are remote branches of the context repository named
``golem/<target id>/<run id>``: the runtime pushes one per clean run, the orchestrator opens a
merge request from it, and while the branch exists the target is pending for the lead. Record
ids never contain a slash, so the id is the second path segment.

The token is handed to git through ``GIT_CONFIG_*`` environment variables as an HTTP header, so
it never appears in a URL, a command line or git's error output.
"""

import base64
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

PROPOSAL_PREFIX = "golem/"
AUTHOR = ("-c", "user.name=Golem", "-c", "user.email=golem@localhost")
Env = Mapping[str, str] | None


class GitError(RuntimeError):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(f"{message}: {stderr.strip()}" if stderr.strip() else message)
        self.stderr = stderr


def git_env(token: str | None, base: Mapping[str, str]) -> dict[str, str]:
    env = {**base, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        # GitLab takes any non-empty user name with an access token as the password.
        credentials = base64.b64encode(f"oauth2:{token}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
        }
    return env


def git(args: Sequence[str], cwd: Path | None = None, env: Env = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed with exit {result.returncode}", result.stderr)
    return result.stdout


def clone_at_revision(url: str, revision: str, dest: Path, env: Env = None) -> None:
    git(["clone", "--quiet", "--no-checkout", "--", url, str(dest)], env=env)
    commit = resolve_commit(dest, revision, env)
    if commit is None:
        raise GitError(f"revision {revision!r} not found in {url}")
    git(["checkout", "--quiet", "--detach", commit], cwd=dest, env=env)


def resolve_commit(repo: Path, revision: str, env: Env = None) -> str | None:
    for candidate in (revision, f"origin/{revision}"):
        try:
            args = ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"]
            return git(args, repo, env).strip()
        except GitError:
            continue
    return None


def clone_branch(url: str, branch: str, dest: Path, env: Env = None) -> None:
    git(["clone", "--quiet", "--branch", branch, "--", url, str(dest)], env=env)


def head_commit(repo: Path, env: Env = None) -> str:
    return git(["rev-parse", "HEAD"], repo, env).strip()


def create_branch(repo: Path, name: str, env: Env = None) -> None:
    git(["switch", "--quiet", "--create", name], repo, env)


def changed_paths(repo: Path, base: str, env: Env = None) -> tuple[str, ...]:
    """Paths that differ between `base` and the working tree, both sides of a rename included."""
    git(["add", "--all"], repo, env)
    output = git(["diff", "--cached", "--name-only", "--no-renames", "-z", base], repo, env)
    return tuple(path for path in output.split("\0") if path)


def commit_all(repo: Path, message: str, env: Env = None) -> None:
    git(["add", "--all"], repo, env)
    # The role wrote this tree; hooks it may have planted in .git must not run.
    git([*AUTHOR, "commit", "--quiet", "--no-verify", "--message", message], repo, env)


def push_branch(repo: Path, branch: str, env: Env = None) -> None:
    ref = f"refs/heads/{branch}"
    git(["push", "--quiet", "--no-verify", "origin", f"{ref}:{ref}"], repo, env)


def remote_branches(repo: Path, prefix: str, env: Env = None) -> tuple[str, ...]:
    output = git(["ls-remote", "--heads", "origin", f"refs/heads/{prefix}*"], repo, env)
    names = (line.split("\t", 1)[1].removeprefix("refs/heads/") for line in output.splitlines())
    return tuple(sorted(name for name in names if name.startswith(prefix)))


def proposal_branch(target_id: str, run_id: str) -> str:
    return f"{PROPOSAL_PREFIX}{target_id}/{run_id}"


def pending_ids(branches: Sequence[str]) -> frozenset[str]:
    return frozenset(filter(None, map(proposal_target, branches)))


def proposal_target(branch: str) -> str | None:
    if not branch.startswith(PROPOSAL_PREFIX):
        return None
    target, _, run = branch.removeprefix(PROPOSAL_PREFIX).partition("/")
    return target if target and run else None
