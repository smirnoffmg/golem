"""Builds a role's brief: everything it needs from its first turn, read from the two clones."""

from collections.abc import Sequence
from pathlib import Path

from golem.catalog import AgentCatalog, Role
from golem.runtime.lead import Command, Record, natural_key
from golem.runtime.ports import Brief, LinkedRecord
from golem.runtime.snapshot import Located

ROLES_DIR = "roles"
SKILLS_DIR = "skills"


class BriefError(ValueError):
    pass


def build_brief(
    *,
    run_id: str,
    goal: str,
    catalog: AgentCatalog,
    catalog_dir: Path,
    context_dir: Path,
    records: Sequence[Located],
    command: Command,
) -> Brief:
    by_id = {item.record.id: item for item in records}
    target = find_record(by_id, command.target_id)
    target_path = context_dir / target.source
    skills = catalog_dir / SKILLS_DIR
    return Brief(
        run_id=run_id,
        goal=goal,
        role=role_named(catalog, command.role),
        instructions=read_instructions(catalog_dir, command.role),
        target=target.record,
        target_path=target_path,
        target_text=target_path.read_text(encoding="utf-8"),
        linked=tuple(
            LinkedRecord(id=rid, text=(context_dir / by_id[rid].source).read_text("utf-8"))
            for rid in linked_ids(target.record, [item.record for item in records])
        ),
        workspace=context_dir,
        skills_dir=skills if skills.is_dir() else None,
        delegates=catalog.delegates,
    )


def find_record(by_id: dict[str, Located], record_id: str) -> Located:
    if record_id not in by_id:
        raise BriefError(f"target {record_id!r} is not a record of the context repository")
    return by_id[record_id]


def role_named(catalog: AgentCatalog, name: str) -> Role:
    for role in catalog.roles:
        if role.name == name:
            return role
    raise BriefError(f"role {name!r} is not declared in catalog {catalog.name!r}")


def read_instructions(catalog_dir: Path, role: str) -> str:
    path = catalog_dir / ROLES_DIR / f"{role}.md"
    if not path.is_file():
        raise BriefError(
            f"role {role!r} has no instructions: {ROLES_DIR}/{role}.md is missing in the catalog"
        )
    return path.read_text(encoding="utf-8")


def linked_ids(target: Record, records: Sequence[Record]) -> tuple[str, ...]:
    """Ids the target links to and ids of records linking to it, in natural order."""
    incoming = {record.id for record in records if target.id in record.links}
    ids = (target.links | incoming) - {target.id}
    return tuple(sorted(ids, key=lambda rid: (natural_key(rid), rid)))
