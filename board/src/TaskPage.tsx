import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";
import { boardKey } from "./AgentBoard";
import { ACTIVE_STATES } from "./board";
import { Link } from "./navigation";
import { boardInterval } from "./poll";
import { agentPath } from "./route";
import { ProposalLink, ReplyForm, stateName } from "./TaskCard";
import { since } from "./text";

export function TaskPage(props: { agent: string; taskId: string }) {
  const { agent, taskId } = props;
  const queryClient = useQueryClient();
  const key = ["task", agent, taskId] as const;
  const task = useQuery({
    queryKey: key,
    queryFn: () => api.task(agent, taskId),
    refetchInterval: (query) =>
      query.state.data && !ACTIVE_STATES.has(query.state.data.state)
        ? false
        : boardInterval(query.state.error),
  });
  const [error, setError] = useState<string | null>(null);

  function changed() {
    void queryClient.invalidateQueries({ queryKey: key });
    void queryClient.invalidateQueries({ queryKey: boardKey(agent) });
  }

  async function cancel() {
    setError(null);
    try {
      await api.cancel(agent, taskId);
      changed();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The task was not canceled.");
    }
  }

  return (
    <main className="task-page">
      <p>
        <Link to={agentPath(agent)}>Back to the {agent} board</Link>
      </p>
      {task.isPending && <p className="muted">Loading the task…</p>}
      {task.error && (
        <p className="banner" role="alert">
          {task.error.message}
        </p>
      )}
      {task.data && (
        <>
          <h1 className="task-goal">{task.data.goal}</h1>
          <dl className="facts">
            <dt>State</dt>
            <dd className="state">{stateName(task.data.state)}</dd>
            <dt>Updated</dt>
            <dd>
              <time dateTime={task.data.updated}>{since(task.data.updated)}</time>
            </dd>
            <dt>Task</dt>
            <dd className="task-id">{task.data.id}</dd>
          </dl>
          {task.data.message && <p className="card-message">{task.data.message}</p>}
          {task.data.proposal && <ProposalLink proposal={task.data.proposal} />}
          {task.data.column === "waiting" && (
            <ReplyForm agent={agent} task={task.data} onChanged={changed} />
          )}
          {ACTIVE_STATES.has(task.data.state) && (
            <button type="button" className="quiet" onClick={cancel}>
              Cancel the task
            </button>
          )}
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          {task.data.history.length > 0 && (
            <section>
              <h2>Messages</h2>
              <ol className="history">
                {task.data.history.map((message, index) => (
                  <li key={index} className={`message message-${message.role}`}>
                    <span className="message-role">{message.role === "user" ? "You" : agent}</span>
                    <p className="message-text">{message.text}</p>
                  </li>
                ))}
              </ol>
            </section>
          )}
          {task.data.artifacts.length > 0 && (
            <section>
              <h2>Results</h2>
              {task.data.artifacts.map((artifact, index) => (
                <figure key={index} className="artifact">
                  <figcaption>{artifact.name}</figcaption>
                  <pre>{artifact.text}</pre>
                </figure>
              ))}
            </section>
          )}
        </>
      )}
    </main>
  );
}
