# 6. Evaluate catalog merge requests in the CI job first

## Status

Accepted, 2026-09-24

## Context

An agent's catalog is its code: a change to a prompt, a rule or a role changes what the agent
does. [ADR 0005](0005-pilot-and-target.md) puts the gate on catalog merge requests into the
pilot. Its target form is an A2A task: the CI job, an A2A client, submits "evaluate this
catalog version", the orchestrator runs the golden set in Jobs, a judge scores the results and
Langfuse experiments compare them with the merged version.

That form needs parts the pilot does not have yet: an evaluation workflow in the orchestrator,
a CI identity the edge accepts (an open question in the architecture), a judge and a trace
store. Waiting for all of them would leave the first catalog changes ungated. The gate itself
needs little: significant prompt changes "should go through a pull request review process, just
like code changes", with the reviewer checking "that the updated prompt has been tested against
your evaluation dataset, that the change does not introduce new failure modes" (*Building
Complex Multi-Agent Systems*, p. 119 (PDF 148)); and regression testing "ensures that the latest
updates or changes ... do not regress (perform worse) relative to the baseline" (*Learning
LangChain*, PDF 505).

## Decision

In the pilot the catalog repository's CI job runs the evaluation itself, with the Golem image
and `python -m golem.evaluation run`. It is not an A2A task and does not run in a Golem Job.

- **Same runtime.** Each golden-set case runs through `golem.runtime.main.run`, the function a
  Job runs, against two local bare repositories seeded from the catalog under test and the
  case's context. The lead, the brief, the role runner and the validators are production code.
- **Structural checks, no judge yet.** A case states the expected outcome, role and target and
  checks the published branch: sections filled, files changed only under a directory, phrases
  present or absent. The format is in `examples/discovery/evals/README.md`.
- **Gate.** The pass rate must reach the threshold (0.8 by default) and no case that passed in
  the baseline may fail. The baseline, `evals/baseline.json` in the catalog repository, maps case
  ids to passed; it is written from the merged version's report with
  `python -m golem.evaluation baseline` and committed. Without it only the threshold applies.
- **Pipeline.** The job runs in merge request pipelines only
  (`rules: - if: $CI_PIPELINE_SOURCE == "merge_request_event"`); the project enables "Pipelines
  must succeed". A catalog repository's configuration is `examples/discovery/.gitlab-ci.yml`:

  ```yaml
  evaluate:
    image: $GOLEM_IMAGE
    rules:
      - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    script:
      - >
        python -m golem.evaluation run --catalog . --cases evals --threshold 0.8
        --baseline evals/baseline.json --report eval-report.json
    artifacts:
      when: always
      paths: [eval-report.json]
  ```

Exit codes: 0 the gate passed, 1 it failed, 2 a usage or configuration error.

## Consequences

- Catalog changes are gated from the first release, with no orchestrator workflow, CI identity,
  judge or trace store in place.
- The CI runner needs network access to the model gateway and a gateway key
  (`GOLEM_MODEL_KEY`). Merge request pipelines run on unprotected branches, so the variable
  cannot be protected, and anyone who can open a merge request can change the pipeline and read
  it. The key must be one of its own, with a small budget and only the evaluation's models.
- Golem's isolation and quotas do not apply to evaluation yet: no default-deny egress, no
  branch-only token, no admission quota, no Job limits. The roles still write only under their
  directory through the runner's path permissions, and everything happens in throwaway local
  repositories; the context repository is never pushed to.
- The baseline lives in the merge request's own branch, so a change to `evals/baseline.json`
  or to the threshold is a change to the gate and is reviewed as one.
- The checks are structural: they see that a section was filled, not whether the evidence is
  good. Pass rates are not compared with Langfuse experiments.
- Moving evaluation into a Job, as the C4 target draws it, keeps the CLI: the orchestrator's
  evaluation workflow starts a Job with the same image and command, and the CI job becomes the
  A2A client that waits for its verdict.
