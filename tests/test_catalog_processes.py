"""Processes (ADR 0019): the process file, and the checks across a deployment's pinned catalogs."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from test_settings import TASKS_ENV

from golem.catalog import (
    DELEGATE_GROUP,
    AgentCatalog,
    CatalogError,
    ProcessCatalog,
    check_catalogs,
    load_catalogs,
    render_goal,
)
from golem.settings import SettingsError, task_service_settings
from golem.tasks.__main__ import pinned_processes

CONTEXT = {"url": "https://git.example.com/corsar/context.git", "branch": "main"}
KINDS = [{"name": "change", "initial": "open", "statuses": ["open", "done"]}]


def goal_agent(name: str, **fields: object) -> dict[str, object]:
    return {
        "name": name,
        "description": f"The {name}.",
        "version": "0.1.0",
        "context": CONTEXT,
        "kinds": KINDS,
        "roles": [{"name": "worker", "writes": "changes/"}],
        "mode": "goal",
        "goal": {"role": "worker", "kind": "change"},
        "proposal": "merge_request",
        **fields,
    }


def stage(name: str, agent: str, goal: str = "Do the {input}") -> dict[str, str]:
    return {"name": name, "agent": agent, "goal": goal}


def process(**fields: object) -> dict[str, object]:
    return {
        "name": "corsar-feature",
        "description": "Takes a change request from analysis to a merged implementation.",
        "version": "0.1.0",
        "skills": [{"id": "feature", "name": "Implement a change", "description": "d"}],
        "stages": [stage("analysis", "analyst"), stage("design", "designer", "Design it.")],
        **fields,
    }


def agents(*catalogs: dict[str, object]) -> dict[str, AgentCatalog]:
    parsed = [AgentCatalog.model_validate(c) for c in catalogs]
    return {c.name: c for c in parsed}


def processes(*catalogs: dict[str, object]) -> dict[str, ProcessCatalog]:
    parsed = [ProcessCatalog.model_validate(c) for c in catalogs]
    return {c.name: c for c in parsed}


# The process file


def test_a_process_is_a_named_list_of_stages_with_a_return_limit():
    parsed = ProcessCatalog.model_validate(process())

    assert parsed.name == "corsar-feature"
    assert [(s.name, s.agent) for s in parsed.stages] == [
        ("analysis", "analyst"),
        ("design", "designer"),
    ]
    assert parsed.return_limit == 2
    assert [skill.id for skill in parsed.skills] == ["feature"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"stages": []}, "at least 1"),
        ({"stages": [stage(f"s{i}", "analyst") for i in range(11)]}, "at most 10"),
        ({"stages": [stage("analysis", "analyst")] * 2}, "twice"),
        ({"stages": [stage("Analysis", "analyst")]}, "name"),
        ({"return_limit": 6}, "return_limit"),
        ({"return_limit": -1}, "return_limit"),
        ({"stages": [stage("analysis", "analyst", "Do {task}")]}, "only {input}"),
        ({"stages": [stage("analysis", "analyst", "Do {input!r}")]}, "only {input}"),
        ({"stages": [stage("analysis", "analyst", "Do {input:>10}")]}, "only {input}"),
        ({"stages": [stage("analysis", "analyst", "Do {0}")]}, "only {input}"),
        ({"stages": [stage("analysis", "analyst", "Do {")]}, "goal"),
        ({"stages": [stage("analysis", "analyst", "")]}, "goal"),
        ({"stages": [stage("analysis", "analyst", "x" * 4_001)]}, "4000"),
        ({"rules": []}, "Extra inputs"),
    ],
)
def test_a_process_file_is_refused_unless_it_is_a_short_linear_list(fields, message):
    with pytest.raises(ValidationError, match=message):
        ProcessCatalog.model_validate(process(**fields))


def test_a_goal_renders_with_the_persons_message_and_nothing_else():
    [analysis, design] = ProcessCatalog.model_validate(process()).stages

    assert render_goal(analysis, "change of limit rules") == "Do the change of limit rules"
    assert render_goal(design, "anything") == "Design it."


def test_escaped_braces_stay_literal_in_a_goal():
    [only] = ProcessCatalog.model_validate(
        process(stages=[stage("analysis", "analyst", "Fill {{Evidence}} for {input}")])
    ).stages

    assert render_goal(only, "H-1") == "Fill {Evidence} for H-1"


def test_a_goal_that_renders_too_long_or_empty_is_refused():
    [only] = ProcessCatalog.model_validate(
        process(stages=[stage("analysis", "analyst", "{input}")])
    ).stages

    with pytest.raises(ValueError, match="4000"):
        render_goal(only, "x" * 4_001)
    with pytest.raises(ValueError, match="empty"):
        render_goal(only, "   ")


# Across the pinned catalogs


def test_a_process_over_goal_agents_sharing_one_context_is_accepted():
    check_catalogs(agents(goal_agent("analyst"), goal_agent("designer")), processes(process()))


@pytest.mark.parametrize(
    ("catalogs", "message"),
    [
        ((goal_agent("analyst"),), "stage 'design' names agent 'designer'"),
        (
            (goal_agent("analyst"), {**goal_agent("designer"), "mode": "records", "goal": None}),
            "not a goal agent",
        ),
        (
            (
                goal_agent("analyst"),
                {k: v for k, v in goal_agent("designer").items() if k != "proposal"},
            ),
            "names no proposal kind",
        ),
        (
            (
                goal_agent("analyst"),
                goal_agent(
                    "designer", context={**CONTEXT, "url": "https://git.example.com/other.git"}
                ),
            ),
            "context repository",
        ),
        (
            (goal_agent("analyst"), goal_agent("designer", context={**CONTEXT, "branch": "dev"})),
            "context repository",
        ),
    ],
)
def test_a_process_whose_stages_cannot_hand_over_is_refused(catalogs, message):
    with pytest.raises(CatalogError, match=message):
        check_catalogs(agents(*catalogs), processes(process()))


def test_a_stage_may_not_be_another_process():
    nested = process(name="outer", stages=[stage("inner", "corsar-feature")])

    with pytest.raises(CatalogError, match="is a process"):
        check_catalogs(
            agents(goal_agent("analyst"), goal_agent("designer")), processes(process(), nested)
        )


def test_an_agent_and_a_process_may_not_share_a_name():
    with pytest.raises(CatalogError, match="both an agent and a process"):
        check_catalogs(
            agents(goal_agent("analyst"), goal_agent("designer"), goal_agent("corsar-feature")),
            processes(process()),
        )


DELEGATING = {
    "roles": [{"name": "worker", "writes": "changes/", "tools": [DELEGATE_GROUP]}],
}


def test_neighbours_must_be_pinned_agents():
    delegating = goal_agent("analyst", **DELEGATING, delegates=[{"agent": "ghost", "when": "w"}])

    with pytest.raises(CatalogError, match="neighbour 'ghost'"):
        check_catalogs(agents(delegating, goal_agent("designer")), {})


def test_a_neighbour_may_not_be_a_process():
    delegating = goal_agent(
        "analyst", **DELEGATING, delegates=[{"agent": "corsar-feature", "when": "w"}]
    )

    with pytest.raises(CatalogError, match="is a process"):
        check_catalogs(agents(delegating, goal_agent("designer")), processes(process()))


# Loading a directory of catalogs


def write(root: Path, directory: str, file: str, data: dict[str, object]) -> None:
    (root / directory).mkdir(parents=True, exist_ok=True)
    (root / directory / file).write_text(yaml.safe_dump(data))


def test_a_catalogs_directory_holds_agents_and_processes_side_by_side(tmp_path):
    write(tmp_path, "analyst", "agent.yaml", goal_agent("analyst"))
    write(tmp_path, "designer", "agent.yaml", goal_agent("designer"))
    write(tmp_path, "corsar-feature", "process.yaml", process())
    (tmp_path / "context").mkdir()

    catalogs = load_catalogs(tmp_path)

    assert sorted(catalogs.agents) == ["analyst", "designer"]
    assert sorted(catalogs.processes) == ["corsar-feature"]


def test_a_directory_with_both_files_is_refused(tmp_path):
    write(tmp_path, "analyst", "agent.yaml", goal_agent("analyst"))
    write(tmp_path, "analyst", "process.yaml", process(name="analyst"))

    with pytest.raises(CatalogError, match=r"both agent\.yaml and process\.yaml"):
        load_catalogs(tmp_path)


def test_two_catalogs_with_one_name_are_refused(tmp_path):
    write(tmp_path, "analyst", "agent.yaml", goal_agent("analyst"))
    write(tmp_path, "analyst-copy", "agent.yaml", goal_agent("analyst"))

    with pytest.raises(CatalogError, match="'analyst' is defined twice"):
        load_catalogs(tmp_path)


def test_a_malformed_catalog_names_its_file(tmp_path):
    write(tmp_path, "corsar-feature", "process.yaml", process(stages=[]))

    with pytest.raises(CatalogError, match=r"process\.yaml"):
        load_catalogs(tmp_path)


def test_the_example_catalogs_load():
    catalogs = load_catalogs(Path(__file__).parents[1] / "examples")

    assert "discovery" in catalogs.agents


# The task service knows the pinned processes as the edge does (ADR 0019).


def write_catalog(root: Path, name: str, file: str, catalog: dict[str, object]) -> None:
    (root / name).mkdir()
    (root / name / file).write_text(yaml.safe_dump(catalog))


def test_the_task_service_pins_the_processes_of_its_catalogs_directory(tmp_path: Path) -> None:
    write_catalog(tmp_path, "analyst", "agent.yaml", goal_agent("analyst"))
    write_catalog(tmp_path, "designer", "agent.yaml", goal_agent("designer"))
    write_catalog(tmp_path, "corsar-feature", "process.yaml", process())
    settings = task_service_settings({**TASKS_ENV, "GOLEM_CATALOGS_DIR": str(tmp_path)})

    assert list(pinned_processes(settings)) == ["corsar-feature"]
    assert pinned_processes(task_service_settings(TASKS_ENV)) == {}


def test_the_task_service_refuses_a_process_its_stage_agents_cannot_run(tmp_path: Path) -> None:
    write_catalog(tmp_path, "corsar-feature", "process.yaml", process())
    settings = task_service_settings({**TASKS_ENV, "GOLEM_CATALOGS_DIR": str(tmp_path)})

    with pytest.raises(SettingsError, match="GOLEM_CATALOGS_DIR"):
        pinned_processes(settings)
