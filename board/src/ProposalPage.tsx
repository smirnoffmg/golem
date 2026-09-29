import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { type Decision, type DiffRun, type ProposalDetail, api } from "./api";
import { Link } from "./navigation";
import {
  decisionProblem,
  kindName,
  mayDecide,
  outcomeText,
  previewOf,
  stateText,
} from "./proposal";
import { MAX_REASON_CHARS } from "./process";
import { REVIEW_PATH, agentPath } from "./route";
import { since } from "./text";

export function ProposalPage(props: { proposalId: string }) {
  const queryClient = useQueryClient();
  const key = ["proposal", props.proposalId] as const;
  const proposal = useQuery({ queryKey: key, queryFn: () => api.proposal(props.proposalId) });

  function decided(shown: ProposalDetail) {
    queryClient.setQueryData(key, shown);
    // What waits for the person changed: the board, the queue and the counts ask again.
    void queryClient.invalidateQueries({ queryKey: ["board", shown.agent] });
    void queryClient.invalidateQueries({ queryKey: ["review-queue"] });
    void queryClient.invalidateQueries({ queryKey: ["review-counts"] });
  }

  return (
    <main className="task-page proposal-page">
      <p>
        <Link to={REVIEW_PATH}>Back to what waits for review</Link>
      </p>
      {proposal.isPending && <p className="muted">Loading the proposal…</p>}
      {proposal.error && (
        <p className="banner" role="alert">
          {proposal.error.message}
        </p>
      )}
      {proposal.data && (
        <ProposalView
          proposal={proposal.data}
          onDecided={decided}
          // Someone decided first, or the proposal moved: show it as it is now.
          onRefused={() => void queryClient.invalidateQueries({ queryKey: key })}
        />
      )}
    </main>
  );
}

type Decided = { onDecided: (p: ProposalDetail) => void; onRefused: () => void };

function ProposalView(props: { proposal: ProposalDetail } & Decided) {
  const { proposal } = props;
  const fields = previewOf(proposal.kind, proposal.payload);
  return (
    <>
      <p className="card-kind">{kindName(proposal.kind)}</p>
      <h1 className="task-goal">{proposal.summary}</h1>
      <dl className="facts">
        <dt>State</dt>
        <dd className="state">{stateText(proposal.state)}</dd>
        <dt>Agent</dt>
        <dd>
          <Link to={agentPath(proposal.agent)}>{proposal.agent}</Link>
        </dd>
        <dt>For</dt>
        <dd>{proposal.owner}</dd>
        <dt>Proposed</dt>
        <dd>
          <time dateTime={proposal.createdAt}>{since(proposal.createdAt)}</time>
        </dd>
        {proposal.decidedBy && (
          <>
            <dt>Decided by</dt>
            <dd>{proposal.decidedBy}</dd>
          </>
        )}
        {proposal.reason && (
          <>
            <dt>Reason</dt>
            <dd>{proposal.reason}</dd>
          </>
        )}
      </dl>
      {proposal.state !== "pending" && (
        <p className={`outcome outcome-${proposal.state}`} role="status">
          {outcomeText(proposal.state, proposal.detail)}
        </p>
      )}
      {fields.length > 0 && (
        <dl className="preview">
          {fields.map((field) => (
            <div key={field.label} className={field.long ? "preview-long" : undefined}>
              <dt>{field.label}</dt>
              <dd>{field.long ? <pre>{field.value}</pre> : field.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {proposal.kind === "wiki_edit" && <PageDiff proposal={proposal} />}
      {proposal.kind === "merge_request" && proposal.url && (
        <p>
          <a href={proposal.url} rel="noreferrer" target="_blank">
            Open the merge request
          </a>{" "}
          <span className="muted">and merge or close it in GitLab.</span>
        </p>
      )}
      {mayDecide(proposal) && (
        <DecisionForm proposal={proposal} onDecided={props.onDecided} onRefused={props.onRefused} />
      )}
      {proposal.report && (
        <section>
          <h2>What the run found</h2>
          <pre className="report-text">{proposal.report}</pre>
        </section>
      )}
    </>
  );
}

function PageDiff(props: { proposal: ProposalDetail }) {
  const { diff, liveError } = props.proposal;
  if (diff === null) {
    return (
      <p className="muted">
        {liveError
          ? `The page as it is now could not be read: ${liveError}`
          : "The page is not compared now: the proposal is decided."}
      </p>
    );
  }
  return (
    <section>
      <h2>Changes to the page as it is now</h2>
      <ol className="diff" aria-label="Changes to the page">
        {diff.map((run, index) => (
          <DiffLines key={index} run={run} />
        ))}
      </ol>
    </section>
  );
}

const MARKS = { equal: " ", insert: "+", delete: "−" } as const;
const SAYS = { equal: "unchanged", insert: "added", delete: "removed" } as const;

function DiffLines(props: { run: DiffRun }) {
  const { run } = props;
  if (run.op === "fold") {
    return (
      <li className="diff-fold">
        {run.count} unchanged {run.count === 1 ? "line" : "lines"}
      </li>
    );
  }
  return (
    <>
      {run.lines.map((line, index) => (
        <li key={index} className={`diff-line diff-${run.op}`}>
          <span className="diff-mark" aria-label={SAYS[run.op]}>
            {MARKS[run.op]}
          </span>
          <code>{line}</code>
        </li>
      ))}
    </>
  );
}

function DecisionForm(props: { proposal: ProposalDetail } & Decided) {
  const { proposal } = props;
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const decide = useMutation({
    mutationFn: (decision: Decision) => api.decide(proposal.id, decision, reason),
    onSuccess: props.onDecided,
    onError: props.onRefused,
  });
  const id = `reason-${proposal.id}`;

  function submit(decision: Decision) {
    const found = decisionProblem(decision, reason, proposal.stage);
    setProblem(found);
    if (found === null) decide.mutate(decision);
  }

  return (
    <form className="reply decision" onSubmit={(event) => event.preventDefault()}>
      <label htmlFor={id}>
        {proposal.stage ? "Reason, if you reject it (required)" : "Reason, if you reject it"}
      </label>
      <textarea
        id={id}
        maxLength={MAX_REASON_CHARS}
        rows={2}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      <div className="resolution-actions">
        <button type="button" disabled={decide.isPending} onClick={() => submit("accept")}>
          {decide.isPending && decide.variables === "accept" ? "Applying…" : "Accept"}
        </button>
        <button
          type="button"
          className="quiet"
          disabled={decide.isPending}
          onClick={() => submit("reject")}
        >
          {decide.isPending && decide.variables === "reject" ? "Rejecting…" : "Reject"}
        </button>
      </div>
      {(problem ?? decide.error?.message) && (
        <p className="form-error" role="alert">
          {problem ?? decide.error?.message}
        </p>
      )}
    </form>
  );
}
