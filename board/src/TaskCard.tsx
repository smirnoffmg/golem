import { type FormEvent, useState } from "react";
import { api } from "./api";
import { ACTIVE_STATES, type Proposal, type Task } from "./board";
import { Link } from "./navigation";
import { taskPath } from "./route";
import { newNonce, since } from "./text";

const STATE_NAMES: Record<string, string> = {
  submitted: "Submitted",
  working: "Working",
  "input-required": "Needs your answer",
  "auth-required": "Needs sign-in",
  completed: "Completed",
  failed: "Failed",
  rejected: "Refused",
  canceled: "Canceled",
};

const PROPOSAL_STATES: Record<string, string> = {
  pending: "waiting for review",
  accepted: "being applied",
  applied: "merged",
  rejected: "closed",
  stale: "out of date",
  failed: "could not be applied",
};

export function stateName(state: string): string {
  return STATE_NAMES[state] ?? state;
}

export function ProposalLink(props: { proposal: Proposal }) {
  const { proposal } = props;
  const status = PROPOSAL_STATES[proposal.state] ?? proposal.state;
  if (proposal.kind === "merge_request" && proposal.url) {
    return (
      <p className="proposal">
        <a href={proposal.url} rel="noreferrer" target="_blank">
          Merge request
        </a>{" "}
        <span className="muted">{status}. Review and merge it in GitLab.</span>
      </p>
    );
  }
  return <p className="proposal muted">Proposal {status}.</p>;
}

export function TaskCard(props: { agent: string; task: Task; onChanged: (task: Task) => void }) {
  const { agent, task } = props;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function cancel() {
    setBusy(true);
    setError(null);
    try {
      props.onChanged(await api.cancel(agent, task.id));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The task was not canceled.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={`card state-${task.state}`}>
      <Link to={taskPath(agent, task.id)} className="card-goal">
        {task.goal || "(no goal text)"}
      </Link>
      <p className="card-meta">
        <span className="state">{stateName(task.state)}</span>
        <time dateTime={task.updated}>{since(task.updated)}</time>
      </p>
      {task.message && task.column === "failed" && <p className="card-message">{task.message}</p>}
      {task.proposal && <ProposalLink proposal={task.proposal} />}
      {task.column === "waiting" && <ReplyForm agent={agent} task={task} onChanged={props.onChanged} />}
      {ACTIVE_STATES.has(task.state) && (
        <button type="button" className="quiet" disabled={busy} onClick={cancel}>
          {busy ? "Canceling…" : "Cancel"}
        </button>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </article>
  );
}

export function ReplyForm(props: { agent: string; task: Task; onChanged: (task: Task) => void }) {
  const [text, setText] = useState("");
  const [nonce, setNonce] = useState(newNonce);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const id = `reply-${props.task.id}`;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSending(true);
    setError(null);
    try {
      props.onChanged(await api.reply(props.agent, props.task.id, text.trim(), nonce));
      setText("");
      setNonce(newNonce());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The answer was not sent.");
    } finally {
      setSending(false);
    }
  }

  return (
    <form className="reply" onSubmit={submit}>
      {props.task.message && <p className="card-message">{props.task.message}</p>}
      <label htmlFor={id}>Your answer</label>
      <textarea
        id={id}
        required
        maxLength={4000}
        rows={2}
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
      <button type="submit" disabled={sending || text.trim() === ""}>
        {sending ? "Sending…" : "Send answer"}
      </button>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
