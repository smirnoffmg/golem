# Golden set of the discovery agent

Every merge request to this catalog runs these cases through the runtime and fails its pipeline
when the gate does not pass ([ADR 0006](../../../docs/adr/0006-evaluation-in-ci-first.md)). A
case is a situation the agent must keep handling: a context repository, a goal, and what the
run has to produce.

```sh
python -m golem.evaluation run --catalog . --cases evals \
  --threshold 0.8 --baseline evals/baseline.json --report eval-report.json
```

## Layout

```
evals/
  baseline.json                  case id -> passed on the last merged version (optional)
  <case id>/
    case.yaml                    goal and expectations
    context/                     the whole context repository the run starts from
      hypotheses/H-2.md          records in the usual format (examples/context/README.md)
```

A case id is the directory name and matches `^[a-z0-9][a-z0-9-]*$`. Every directory under
`evals/` that does not start with a dot is a case and needs both `case.yaml` and `context/`.

## `case.yaml`

```yaml
goal: Work the discovery backlog      # required: the run's goal, as a caller would send it
pending: [H-3]                        # optional: targets that already have an open proposal
expect:
  outcome: proposed                   # required: proposed | idle | invalid
  role: researcher                    # optional: the role the lead must pick
  target: H-2                         # optional: the record the lead must pick
  checks:                             # optional, every key optional
    sections_filled:                  # record id -> sections that must be non-empty
      H-2: [Evidence]
    files_changed_under: hypotheses/  # every changed file must be under this directory
    must_contain:                     # record id -> phrases, case-insensitive
      H-2: [supports, refutes]
    must_not_contain:                 # record id -> phrases, case-insensitive
      H-4: [supports]
  delegates: [checker]                # optional: the neighbours the role must ask; [] for none
```

- **What the checks read.** What production would publish: the proposal branch the run pushed,
  or the unchanged base branch when it pushed nothing (`idle`, `invalid`, `failed`). A section
  is empty by the context format's rule: only whitespace and HTML comments.
- **Phrases** are matched against the record's whole file, front matter included, so
  `"links: [H-3]"` checks a link and `"status: accepted"` a status.
- **`delegates`** checks routing ([ADR 0019](../../../docs/adr/0019-processes.md)): which of
  the agent's neighbours the role asked through `delegate_to_agent`, in any order and however
  often. The calls are recorded, not sent, so no child run starts. Without the key routing is
  not checked.
- **`pending`** creates a branch `golem/<id>/pending` for each id, as an open merge request does.
- **Unknown keys are errors**, and every error names the `case.yaml` it comes from: a misspelt
  check would otherwise never run and the case would pass for the wrong reason.

A case passes when the outcome, role and target match and every check holds; the report lists
every failed check, not only the first, together with the runtime's own reasons (a validator's
violation, a model error). An exception in the role runner fails its case, not the evaluation.

## The gate

The gate passes when the pass rate is at least the threshold **and** no case that passed in
`baseline.json` fails now. A regression fails the gate even above the threshold, so a change
cannot trade a working case for the slack under it. Cases added or removed since the baseline
are not regressions; they show in the merge request's diff. Without a baseline file only the
threshold applies.

After a merge, write the new baseline from the merged version's report and commit it:

```sh
python -m golem.evaluation baseline --report eval-report.json --out evals/baseline.json
```

Exit codes of `run`: 0 the gate passed, 1 it failed, 2 a usage or configuration error (bad
arguments, a malformed case or baseline, missing `GOLEM_MODEL_GATEWAY_URL`, `GOLEM_MODEL_KEY`
or `GOLEM_MODEL`).

## Cases here

| Case | Situation | Expected |
| --- | --- | --- |
| `researcher-fills-evidence` | H-2 proposed with empty Evidence, H-4 rejected | researcher fills H-2's Evidence with findings for and against, only in `hypotheses/` |
| `designer-answers-validated-hypothesis` | H-3 validated, no solution yet | designer adds S-1 linking H-3 with Approach and Risks, Review left to the reviewer |
| `reviewer-reviews-proposed-solution` | S-1 proposed with empty Review | reviewer fills Review with a recommendation, status untouched |
| `everything-pending-is-idle` | every target already has an open proposal | idle |

The checks are structural: they see that a section was filled and a phrase is there, not
whether the evidence is good. A model judge is the next step and is not part of the pilot.
