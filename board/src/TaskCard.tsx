import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { api } from "./api";
import { ACTIVE_STATES, type Proposal, type Task } from "./board";
import { Link } from "./navigation";
import { kindName, stateText } from "./proposal";
import { proposalPath, taskPath } from "./route";
import {
  MAX_REASON_CHARS,
  answerForm,
  attemptText,
  failureText,
  resolutionProblem,
  stageText,
  staleText,
} from "./process";
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

export function ProposalLink(props: { proposal: Proposal; inProcess?: boolean }) {
  const { proposal } = props;
  const status = PROPOSAL_STATES[proposal.state] ?? proposal.state;
  if (proposal.kind === "merge_request" && proposal.url) {
    return (
      <p className="proposal">
        <a href={proposal.url} rel="noreferrer" target="_blank">
          Merge request
        </a>{" "}
        <span className="muted">
          {status}.{proposal.state === "pending" && <> Review and merge it in GitLab.</>}
          {/* The process reruns the stage with the closing comment as its reason (ADR 0019). */}
          {props.inProcess && proposal.state === "pending" && (
            <> To reject it, close it with a comment saying why.</>
          )}
        </span>
      </p>
    );
  }
  return (
    <p className="proposal">
      <Link to={proposalPath(proposal.id)}>{kindName(proposal.kind)}</Link>{" "}
      <span className="muted">{stateText(proposal.state)}.</span>
    </p>
  );
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
      {task.process && <ProcessLines task={task} />}
      {task.message && task.column === "failed" && !task.process?.reason && (
        <p className="card-message">{task.message}</p>
      )}
      {task.proposal && <ProposalLink proposal={task.proposal} inProcess={task.process !== null} />}
      {answerForm(task) === "resolution" && <ResolutionForm agent={agent} task={task} />}
      {answerForm(task) === "reply" && (
        <ReplyForm agent={agent} task={task} onChanged={props.onChanged} />
      )}
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

function ProcessLines(props: { task: Task }) {
  const process = props.task.process;
  if (!process) return null;
  const attempt = attemptText(process);
  const stale = staleText(process);
  const failure = failureText(process);
  return (
    <>
      <p className="process-stage">{stageText(process)}</p>
      {(attempt || stale) && (
        <p className="card-meta">
          {attempt && <span>{attempt}</span>}
          {stale && <span>{stale}</span>}
        </p>
      )}
      {failure && props.task.column === "failed" && <p className="card-message">{failure}</p>}
    </>
  );
}

export function ResolutionForm(props: { agent: string; task: Task }) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [ending, setEnding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const id = `reason-${props.task.id}`;

  async function resolve(action: "rerun" | "end") {
    const problem = resolutionProblem(action, reason);
    if (problem) {
      setError(problem);
      return;
    }
    setSending(true);
    setError(null);
    try {
      await api.resolve(props.task.id, action, action === "rerun" ? reason.trim() : undefined);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["board", props.agent] }),
        queryClient.invalidateQueries({ queryKey: ["task", props.agent, props.task.id] }),
      ]);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The answer was not sent.");
    } finally {
      setSending(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void resolve("rerun");
  }

  return (
    <form className="reply resolution" onSubmit={submit}>
      <p className="card-message">
        The stage&apos;s merge request was closed without a comment. Say why, and the stage runs
        again with your reason; or end the process.
      </p>
      <label htmlFor={id}>Why was it rejected?</label>
      <textarea
        id={id}
        maxLength={MAX_REASON_CHARS}
        rows={2}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      <div className="resolution-actions">
        <button type="submit" disabled={sending || reason.trim() === ""}>
          {sending && !ending ? "Sending…" : "Rerun the stage"}
        </button>
        {ending ? (
          <>
            <button
              type="button"
              className="danger"
              disabled={sending}
              onClick={() => void resolve("end")}
            >
              {sending ? "Ending…" : "End the process for good"}
            </button>
            <button
              type="button"
              className="quiet"
              disabled={sending}
              onClick={() => setEnding(false)}
            >
              Keep it
            </button>
          </>
        ) : (
          <button
            type="button"
            className="quiet"
            disabled={sending}
            onClick={() => setEnding(true)}
          >
            End the process
          </button>
        )}
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
