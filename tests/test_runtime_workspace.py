import base64
import subprocess
from pathlib import Path

import pytest

from golem.runtime.workspace import (
    GitError,
    changed_paths,
    clone_at_revision,
    clone_branch,
    commit_all,
    create_branch,
    git_env,
    head_commit,
    pending_ids,
    proposal_branch,
    push_branch,
    remote_branches,
)

AUTHOR = ("-c", "user.name=Test", "-c", "user.email=test@localhost")


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def seed_repo(tmp_path: Path, files: dict[str, str], name: str = "origin") -> Path:
    """A bare repository with one commit on main holding `files`."""
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    for relative, text in files.items():
        path = seed / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    sh("init", "--quiet", "--initial-branch=main", cwd=seed)
    sh("add", "-A", cwd=seed)
    sh(*AUTHOR, "commit", "--quiet", "-m", "seed", cwd=seed)
    bare = tmp_path / f"{name}.git"
    sh("clone", "--quiet", "--bare", str(seed), str(bare), cwd=tmp_path)
    return bare


def add_commit(bare: Path, tmp_path: Path, relative: str, text: str) -> str:
    work = tmp_path / "extra-commit"
    sh("clone", "--quiet", str(bare), str(work), cwd=tmp_path)
    (work / relative).write_text(text)
    sh("add", "-A", cwd=work)
    sh(*AUTHOR, "commit", "--quiet", "-m", "more", cwd=work)
    sh("push", "--quiet", "origin", "main", cwd=work)
    return sh("rev-parse", "HEAD", cwd=work)


def push_remote_branch(bare: Path, tmp_path: Path, branch: str) -> None:
    work = tmp_path / f"branch-{branch.replace('/', '-')}"
    sh("clone", "--quiet", str(bare), str(work), cwd=tmp_path)
    sh("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=work)


def test_clone_at_revision_checks_out_that_commit(tmp_path):
    bare = seed_repo(tmp_path, {"a.txt": "first\n"})
    first = sh("rev-parse", "main", cwd=bare)
    add_commit(bare, tmp_path, "a.txt", "second\n")

    dest = tmp_path / "clone"
    clone_at_revision(str(bare), first, dest)

    assert (dest / "a.txt").read_text() == "first\n"
    assert head_commit(dest) == first


def test_clone_at_revision_accepts_a_branch_name(tmp_path):
    bare = seed_repo(tmp_path, {"a.txt": "first\n"})
    latest = add_commit(bare, tmp_path, "a.txt", "second\n")

    dest = tmp_path / "clone"
    clone_at_revision(str(bare), "main", dest)

    assert head_commit(dest) == latest


def test_clone_at_unknown_revision_names_it(tmp_path):
    bare = seed_repo(tmp_path, {"a.txt": "x\n"})

    with pytest.raises(GitError, match="revision 'no-such-rev' not found"):
        clone_at_revision(str(bare), "no-such-rev", tmp_path / "clone")


def test_clone_of_missing_repository_fails_with_git_stderr(tmp_path):
    with pytest.raises(GitError, match=r"git clone .* failed") as error:
        clone_branch(str(tmp_path / "missing.git"), "main", tmp_path / "clone")

    assert error.value.stderr
    assert error.value.stderr.strip() in str(error.value)


def test_clone_branch_checks_out_the_branch(tmp_path):
    bare = seed_repo(tmp_path, {"a.txt": "x\n"})

    dest = tmp_path / "clone"
    clone_branch(str(bare), "main", dest)

    assert sh("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "main"


def test_changed_paths_lists_modified_added_and_deleted_files(tmp_path):
    bare = seed_repo(tmp_path, {"keep.md": "k\n", "edit.md": "e\n", "gone.md": "g\n"})
    dest = tmp_path / "clone"
    clone_branch(str(bare), "main", dest)
    base = head_commit(dest)

    (dest / "edit.md").write_text("changed\n")
    (dest / "gone.md").unlink()
    (dest / "sub").mkdir()
    (dest / "sub" / "new file.md").write_text("n\n")

    assert changed_paths(dest, base) == ("edit.md", "gone.md", "sub/new file.md")


def test_changed_paths_reports_both_sides_of_a_rename(tmp_path):
    bare = seed_repo(tmp_path, {"docs/a.md": "same text\n"})
    dest = tmp_path / "clone"
    clone_branch(str(bare), "main", dest)
    base = head_commit(dest)

    (dest / "docs" / "a.md").rename(dest / "moved.md")

    assert changed_paths(dest, base) == ("docs/a.md", "moved.md")


def test_changed_paths_includes_commits_made_after_base(tmp_path):
    bare = seed_repo(tmp_path, {"a.md": "a\n"})
    dest = tmp_path / "clone"
    clone_branch(str(bare), "main", dest)
    base = head_commit(dest)
    (dest / "b.md").write_text("b\n")
    sh("add", "-A", cwd=dest)
    sh(*AUTHOR, "commit", "--quiet", "-m", "by the role", cwd=dest)

    assert changed_paths(dest, base) == ("b.md",)


def test_nothing_changed_gives_no_paths(tmp_path):
    bare = seed_repo(tmp_path, {"a.md": "a\n"})
    dest = tmp_path / "clone"
    clone_branch(str(bare), "main", dest)

    assert changed_paths(dest, head_commit(dest)) == ()


def test_branch_commit_and_push_reach_the_remote(tmp_path):
    bare = seed_repo(tmp_path, {"a.md": "a\n"})
    dest = tmp_path / "clone"
    clone_branch(str(bare), "main", dest)

    create_branch(dest, "golem/H-2/run-1")
    (dest / "a.md").write_text("changed\n")
    commit_all(dest, "golem: researcher on H-2\n\nRun: run-1\n")
    push_branch(dest, "golem/H-2/run-1")

    assert sh("log", "-1", "--format=%an <%ae>|%s", "golem/H-2/run-1", cwd=bare) == (
        "Golem <golem@localhost>|golem: researcher on H-2"
    )
    assert sh("show", "golem/H-2/run-1:a.md", cwd=bare) == "changed"
    assert sh("rev-parse", "main", cwd=bare) != sh("rev-parse", "golem/H-2/run-1", cwd=bare)


def test_remote_branches_lists_branches_under_a_prefix(tmp_path):
    bare = seed_repo(tmp_path, {"a.md": "a\n"})
    push_remote_branch(bare, tmp_path, "golem/H-2/run-1")
    push_remote_branch(bare, tmp_path, "golem/S-1/run-7")
    push_remote_branch(bare, tmp_path, "feature/other")
    dest = tmp_path / "clone"
    clone_branch(str(bare), "main", dest)

    assert remote_branches(dest, "golem/") == ("golem/H-2/run-1", "golem/S-1/run-7")


def test_remote_branches_sees_branches_pushed_after_the_clone(tmp_path):
    bare = seed_repo(tmp_path, {"a.md": "a\n"})
    dest = tmp_path / "clone"
    clone_branch(str(bare), "main", dest)
    push_remote_branch(bare, tmp_path, "golem/H-3/run-2")

    assert remote_branches(dest, "golem/") == ("golem/H-3/run-2",)


def test_proposal_branch_names_target_and_run():
    assert proposal_branch("H-2", "run-1") == "golem/H-2/run-1"


def test_pending_ids_come_from_proposal_branches():
    branches = ("golem/H-2/run-1", "golem/H-2/run-3", "golem/S-1/run-2", "golem/odd")

    assert pending_ids(branches) == frozenset({"H-2", "S-1"})


def test_git_env_without_token_adds_no_credentials():
    env = git_env(None, {"PATH": "/bin"})

    assert env["PATH"] == "/bin"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not any(value.startswith("Authorization") for value in env.values())


def test_git_env_passes_the_token_as_a_basic_auth_header():
    env = git_env("s3cret", {"GIT_CONFIG_COUNT": "5"})

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    expected = base64.b64encode(b"oauth2:s3cret").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"


def test_git_error_does_not_leak_the_token(tmp_path):
    env = git_env("s3cret", {"PATH": "/usr/bin:/bin"})

    with pytest.raises(GitError) as error:
        clone_branch(str(tmp_path / "missing.git"), "main", tmp_path / "clone", env=env)

    assert "s3cret" not in str(error.value)
