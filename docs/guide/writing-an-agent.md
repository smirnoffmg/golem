# Writing an agent

For agent authors. An agent is two Git repositories: a **catalog** that says what the agent
does (its roles, their instructions, the rules that pick the next piece of work), and a
**context repository** of records it works on. A run reads both, lets one role change one
record, and proposes the change as a merge request. Every change to the catalog passes a
quality gate before it merges.

The example throughout is [examples/discovery](../../examples/discovery), which works on the
records of [examples/context](../../examples/context). Commands run from the root of a Golem
checkout with `uv sync` done. `tests/test_docs.py` runs the lead example and compares its
output with this page; the golden-set example is a test of the default suite; the CLI outputs
below are from runs of the commands shown.

## The catalog

```
agent.yaml            name, context repository, kinds of records, roles, rules
roles/<role>.md       each role's instructions
skills/<name>/SKILL.md  optional skills, offered to every role
evals/                the golden set: one directory per case, and baseline.json
.gitlab-ci.yml        the quality gate
```

### `agent.yaml`

```yaml
name: discovery                 # lowercase slug; the agent's name everywhere
description: Turns a product problem space into evidenced hypotheses and reviewed solutions.
version: 0.1.0
context:                        # the context repository
  url: https://gitlab.internal/product/discovery-context.git
  branch: main
skills:                         # shown on the agent card and in the UI
  - {id: research, name: Research a hypothesis, description: ..., tags: [discovery]}
kinds:                          # the kinds of records, their statuses and sections
  - name: hypothesis
    initial: proposed           # the only status a role may give a record it creates
    statuses: [proposed, validated, rejected]
    sections: [Problem, Evidence]
roles:
  - name: researcher            # lowercase slug; instructions in roles/researcher.md
    writes: hypotheses/         # the only directory the role may change
    tools: [tracker.read, wiki.read]   # tool groups of the platform's MCP registry
rules:                          # in priority order
  - role: researcher
    kind: hypothesis
    statuses: [proposed]
    conditions:
      - {type: empty_section, section: Evidence}
```

| Field | Meaning |
| --- | --- |
| `name` | lowercase letters, digits and hyphens, starting with a letter |
| `context.url`, `context.branch` | where the records are; runs clone this branch and push proposals next to it |
| `skills` | `id`, `name`, `description`, `tags`: what the agent card advertises; not the `SKILL.md` files |
| `kinds` | every `kind` of record the rules name, with its allowed `statuses` and `sections` |
| `roles` | `name`, `writes` (a directory of the context repository), `tools` (dotted tool group names, each once, default none) |
| `rules` | `role`, `kind`, `statuses`, `conditions` |

A rule, a condition or a status that names something not declared above is an error when the
catalog is loaded, so a typo fails at once instead of leaving a rule idle for ever
([catalog.py](../../src/golem/catalog.py)).

### How the next piece of work is chosen

Every run starts with the **lead**, a function of the context repository and nothing else
([runtime/lead.py](../../src/golem/runtime/lead.py)). It takes the rules in order; for each,
the records of the rule's kind in one of its statuses whose conditions all hold. A record with
an open proposal is **pending** and skipped: a branch `golem/<record id>/<run id>` exists in
the context repository. The first rule with a record left wins, and its target is the one
with the lowest id in natural order (`H-2` before `H-10`). With nothing left, the run is
**idle** and says why for each rule.

| Condition | Holds while |
| --- | --- |
| `{type: empty_section, section: Evidence}` | the target's `## Evidence` section is empty (only whitespace and HTML comments) |
| `{type: no_linked, kind: solution, statuses: [proposed, accepted]}` | no record of that kind in those statuses links to the target |

The caller's goal does not choose the target. It reaches the role as part of its brief and can
steer how the role works; the lead has already picked what it works on.

### Role instructions

`roles/<role>.md` is the role's system prompt. The runtime adds the run's goal, the target
record with its empty sections, every linked record, and three rules: fill the target's empty
sections, change no `status`, write only under the role's directory
([deepagents_runner.py](../../src/golem/runtime/deepagents_runner.py)). The role has file tools
over the context repository, no shell, and its tool groups. Good instructions, like
[roles/researcher.md](../../examples/discovery/roles/researcher.md):

- say what to read, what to write where, and in what form;
- repeat that a `status` is a human decision, and ask for a recommendation in the text instead;
- forbid inventing sources, and say what to write when nothing was found;
- end with "finish with a two or three sentence summary": it becomes the body of the proposal's commit
  message and the `summary` of the run's report.

### Skills

A directory `skills/<name>/` with a `SKILL.md` in the Agent Skills format (YAML front matter
with `name`, equal to the directory's name, and `description`, then instructions) is offered
to every role; the role reads it when its description fits the task. Supporting files next to
`SKILL.md` are readable too.

## The context repository

One record per Markdown file, with YAML front matter (`id`, `kind`, `status`, optional `links`)
and level-2 sections; the full format and its validation are in
[examples/context/README.md](../../examples/context/README.md). A section is empty while it
holds only whitespace and HTML comments, so a template leaves a comment as the prompt:

```markdown
---
id: H-2
kind: hypothesis
status: proposed
---
# Teams cannot find last quarter's decisions

## Problem

Decisions live in meeting notes scattered across spaces, so teams repeat old discussions.

## Evidence

<!-- What supports or refutes the problem? Link sources: interviews, metrics, tickets. -->
```

Statuses are changed by people, in a merge request of their own
([reviewing-proposals.md](reviewing-proposals.md)).

## What validation checks

Before anything is pushed, the role's change must pass every check
([runtime/validate.py](../../src/golem/runtime/validate.py)); otherwise the run is `invalid`,
nothing is pushed, and the task says why:

| Check | Message |
| --- | --- |
| every changed file is under the role's `writes` | `<path> is outside the role's writes directory <dir>` |
| the context repository still parses | `the context no longer builds: <file>: <problem>` |
| no record changed its status | `<id> changed status from '<a>' to '<b>'; status changes are human decisions` |
| a new record starts in its kind's `initial` status | `new record <id> has status '<a>'; a new <kind> starts as '<initial>'` |
| something changed | `the role changed no files` |
| the target, or a changed record linking to it, changed | `neither the target <id> (<path>) nor a changed record linking to it changed` |

A run is also bounded: 60 model calls for the role and every subagent it starts together,
`GOLEM_MODEL_TIMEOUT_SECONDS` per call, replies of at most 1 MiB, and the Job's deadline.

## Tools

A role's `tools` name **tool groups** of the platform's MCP registry: `tracker.read` (Jira:
`search_issues`, `get_issue`) and `wiki.read` (Confluence: `search_pages`, `get_page`), read
only. Two things must agree for a role to get a group:

- the platform grants the group to the agent (`agent-tools.yaml`, the operator's file,
  [configuration.md](../operations/configuration.md#agent-tools));
- the role names it.

A role naming a group the registry does not have fails the run before the first model call;
one naming a group the agent is not granted fails when the MCP server refuses its run token.
Tool results are cut at 200 000 characters, and a call that takes longer than 60 s returns an
error to the model.

### Delegating to another agent

One tool group is served by the runtime itself, not an MCP server: `agents.delegate`, with one
tool, `delegate_to_agent(agent, goal)`. It asks another agent to work on `goal` in a run of its
own ([ADR 0014](../adr/0014-golem-as-an-a2a-node.md)). A role gets it only by naming it:

```yaml
roles:
  - name: planner
    writes: plans/
    tools: [agents.delegate, wiki.read]
```

- **It does not wait.** The tool returns at once with the child task's id and its state, for
  example `Delegated to discovery: task 1f0c…, state TASK_STATE_WORKING.` The child proposes
  its own merge request. Tell the role, in its instructions, to write the task id into the
  record it changes, so the reviewer of its proposal sees what was delegated. A role that
  needs the child's result before it can go on cannot be written yet.
- **The goal is all the child gets.** Write it self-contained: the child sees nothing of the
  delegating run, only its own catalog and context.
- **A retry is not a second child.** The same run, agent and goal always reach the same child
  run; to delegate twice, give two goals.
- **The platform decides.** The call goes through the edge like any caller's: the call
  registry must list `agent:<your agent>` for the called agent
  ([configuration.md](../operations/configuration.md#call-registry)); a chain may not call an
  agent already in it, nor go deeper than the platform's limit; the child runs for the person
  who started the first run and counts against that chain's budget. A refusal comes back to
  the model as the tool's answer (for example `Refused by the edge: not_allowed: ...` or
  `state TASK_STATE_REJECTED` with the budget's reason), not as a failed run.
- The child task lists the chain in its metadata (`"chain": ["planner"]`), so whoever reads it
  sees which agents took part.

## Try it locally

**Which role runs next.** The lead is a pure function, so it runs without a model or a
cluster. On the example records it picks the researcher for `H-2`; while `H-2` has an open
proposal, the designer for `H-3`; with everything pending, nothing:

<!-- run: lead -->
```sh
uv run python -c '
from pathlib import Path
from golem.catalog import load_catalog
from golem.runtime.lead import decide
from golem.runtime.snapshot import build_snapshot
catalog = load_catalog(Path("examples/discovery/agent.yaml"))
context = Path("examples/context")
print(decide(catalog.rules, build_snapshot(context, pending=frozenset())))
print(decide(catalog.rules, build_snapshot(context, pending=frozenset({"H-2"}))))
print(decide(catalog.rules, build_snapshot(context, pending=frozenset({"H-2", "H-3", "S-1"}))))
'
```

```
Command(role='researcher', target_id='H-2')
Command(role='designer', target_id='H-3')
Idle(reasons=('researcher: 1 hypothesis in status proposed meet the conditions, all pending (0 did not meet them)', 'designer: 1 hypothesis in status validated meet the conditions, all pending (1 did not meet them)', 'reviewer: 1 solution in status proposed meet the conditions, all pending (0 did not meet them)'))
```

Point it at your own `agent.yaml` and a checkout of your context repository to see what the
next run will do. `build_snapshot` fails with the file and the problem on a malformed record;
run it in the context repository's own CI.

**The golden set without a model.** `tests/test_evaluation_examples.py` runs the discovery
golden set through the real runtime, lead, validators and Git, with fake roles that do what
each role's instructions ask, and checks the gate:

<!-- run: golden-set -->
```sh
uv run pytest tests/test_evaluation_examples.py -v
```

```
tests/test_evaluation_examples.py::test_the_golden_set_has_the_documented_cases PASSED
tests/test_evaluation_examples.py::test_roles_that_follow_their_instructions_pass_the_gate PASSED
tests/test_evaluation_examples.py::test_a_role_writing_outside_its_directory_fails_its_case_and_the_gate PASSED
tests/test_evaluation_examples.py::test_a_case_that_passed_in_the_baseline_is_a_regression_above_the_threshold PASSED
```

Copy its `DiscoveryRoles` to check your own cases' shape before you spend model calls on them:
a case that fails with roles that do exactly the right thing is a wrong case.

## The golden set

`evals/<case id>/` holds `case.yaml` (the goal and what the run must produce) and `context/`
(the whole context repository the run starts from). The format, the checks and the gate are
in [examples/discovery/evals/README.md](../../examples/discovery/evals/README.md). A good set
has one case per rule, one where everything is pending (idle), and a case for each mistake you
have seen a model make; checks are structural (sections filled, files under a directory,
phrases present or absent), not a judgement of quality.

## The CI gate

Every merge request to the catalog runs the golden set with a real model
([ADR 0006](../adr/0006-evaluation-in-ci-first.md)). The job is
[examples/discovery/.gitlab-ci.yml](../../examples/discovery/.gitlab-ci.yml); the project needs:

| CI/CD variable | Value |
| --- | --- |
| `GOLEM_IMAGE` | the Golem image, as the platform runs it |
| `GOLEM_MODEL_GATEWAY_URL` | the gateway's OpenAI-compatible base URL |
| `GOLEM_MODEL` | the model alias the roles run on |
| `GOLEM_MODEL_KEY` | masked; a key of its own with a small budget (anyone who can open a merge request can read it) |

and "Pipelines must succeed" in its merge request settings. The command, runnable from the
catalog's directory anywhere the image's Python is:

```sh
python -m golem.evaluation run --catalog . --cases evals \
  --threshold 0.8 --baseline evals/baseline.json --report eval-report.json
```

Exit 0 when the pass rate reaches the threshold and no case that passed in the baseline fails
now, 1 when not, 2 on a usage or configuration error (for example the three gateway settings
missing). After a merge, record what the merged version passes, and commit it:

<!-- run: baseline -->
```sh
python -m golem.evaluation baseline --report eval-report.json --out evals/baseline.json
```

```json
{
  "designer-answers-validated-hypothesis": false,
  "everything-pending-is-idle": true,
  "researcher-fills-evidence": false,
  "reviewer-reviews-proposed-solution": false
}
```

### Known gap: roles with tools fail in CI

The job above has no MCP registry and no run token: the platform issues run tokens only to
running runs, and its MCP servers accept nothing else. A role that names tools therefore fails
before its first model call, and so does its case. On the discovery catalog, whose three roles
name tools, the gate cannot pass today. Its output, the same with any gateway since no model is
called (the baseline above was written from it):

```
CASE                                   RESULT  OUTCOME  ROLE        TARGET  TIME
designer-answers-validated-hypothesis  FAIL    failed   designer    H-3     0.4s
  - outcome: expected proposed, got failed
  - sections_filled: S-1 is not a record of the result
  - must_contain: S-1 is not a record of the result
    runtime: ToolAccessError: role 'designer' names tool groups ['wiki.read'] that are not in the MCP registry
everything-pending-is-idle             pass    idle     -           -       0.4s
researcher-fills-evidence              FAIL    failed   researcher  H-2     0.4s
  - outcome: expected proposed, got failed
  - sections_filled: H-2 section 'Evidence' is empty
    runtime: ToolAccessError: role 'researcher' names tool groups ['tracker.read', 'wiki.read'] that are not in the MCP registry
reviewer-reviews-proposed-solution     FAIL    failed   reviewer    S-1     0.4s
  - outcome: expected proposed, got failed
  - sections_filled: S-1 section 'Review' is empty
  - must_contain: S-1 does not contain 'recommendation'
    runtime: ToolAccessError: role 'reviewer' names tool groups ['wiki.read'] that are not in the MCP registry
Pass rate 1/4 (25%), threshold 80%.
Gate: FAILED
```

The runner reads `GOLEM_MCP_REGISTRY` and `GOLEM_RUN_TOKEN` in CI as it does in a Job, so MCP
servers serving the same tool names for evaluation would close the gap; none is provided. Until
then, gate catalogs whose roles use no tools, or agree with the platform team on how to run the
evaluation.

## Register the agent with the platform

The platform team adds the agent to its configuration
([configuration.md](../operations/configuration.md#configmaps)): the call registry (who may
start it, and `agent:<name>` in the entries of the agents its roles delegate to), the catalogs
file (repository and revision), the tool grant, the GitLab project of its context repository,
optionally a Jira label, the agent card, and the UI's or Mattermost's
agent list. The bot account needs Reporter on the catalog and Developer on the context
repository ([install.md, step 5](../operations/install.md#5-set-up-gitlab-the-model-gateway-and-the-trace-store)).
A catalog change reaches runs as soon as it is merged into the revision the catalogs file names;
the agent card changes when the edge restarts.
