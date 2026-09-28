import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { api } from "./api";
import {
  type Board,
  COLUMNS,
  type Task,
  columnsOf,
  cursorToAsk,
  nextBoard,
  withTask,
} from "./board";
import { boardInterval } from "./poll";
import { TaskCard } from "./TaskCard";
import { newNonce } from "./text";

export function boardKey(agent: string) {
  return ["board", agent] as const;
}

export function AgentBoard(props: { agent: string; description: string }) {
  const { agent } = props;
  const queryClient = useQueryClient();
  const board = useQuery({
    queryKey: boardKey(agent),
    // Each poll asks for what changed since the last one and merges it into what the board
    // already shows; a snapshot answer replaces it, and every sixth poll asks for one
    // (ADR 0018).
    queryFn: async () => {
      const previous = queryClient.getQueryData<Board>(boardKey(agent));
      return nextBoard(previous, await api.board(agent, cursorToAsk(previous)));
    },
    refetchInterval: (query) => boardInterval(query.state.error),
  });

  function show(task: Task) {
    queryClient.setQueryData<Board>(boardKey(agent), (current) => withTask(current, task));
  }

  const columns = board.data && columnsOf(board.data);
  const active = COLUMNS.filter((c) => c.id !== "archive");

  return (
    <main className="board">
      <header className="board-head">
        <h1 className="board-title">{agent}</h1>
        {props.description && <p className="board-description">{props.description}</p>}
      </header>
      <StartForm agent={agent} onStarted={show} />
      {board.error && (
        <p className="banner" role="alert">
          {board.error.message}
        </p>
      )}
      {board.isPending && <p className="muted">Loading the board…</p>}
      {columns && (
        <>
          <div className="columns">
            {active.map((column) => (
              <section
                key={column.id}
                className={`column column-${column.id}`}
                aria-labelledby={`column-${column.id}`}
              >
                <h2 id={`column-${column.id}`} className="column-title">
                  {column.title}
                  <span className="count">{columns[column.id].length}</span>
                </h2>
                {columns[column.id].length === 0 ? (
                  <p className="empty">{column.empty}</p>
                ) : (
                  <ul className="cards">
                    {columns[column.id].map((task) => (
                      <li key={task.id}>
                        <TaskCard agent={agent} task={task} onChanged={show} />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            ))}
          </div>
          <Archive agent={agent} tasks={columns.archive} onChanged={show} />
        </>
      )}
    </main>
  );
}

function StartForm(props: { agent: string; onStarted: (task: Task) => void }) {
  const [goal, setGoal] = useState("");
  const [nonce, setNonce] = useState(newNonce);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSending(true);
    setError(null);
    try {
      props.onStarted(await api.start(props.agent, goal.trim(), nonce));
      setGoal("");
      setNonce(newNonce());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The task did not start.");
    } finally {
      setSending(false);
    }
  }

  return (
    <form className="start" onSubmit={submit}>
      <label htmlFor="goal" className="start-label">
        New task for {props.agent}
      </label>
      <div className="start-row">
        <textarea
          id="goal"
          name="goal"
          required
          maxLength={4000}
          rows={2}
          value={goal}
          placeholder="What should the agent do? Say it completely: the agent sees only this."
          onChange={(event) => setGoal(event.target.value)}
        />
        <button type="submit" disabled={sending || goal.trim() === ""}>
          {sending ? "Starting…" : "Start"}
        </button>
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

function Archive(props: { agent: string; tasks: Task[]; onChanged: (task: Task) => void }) {
  const [open, setOpen] = useState(false);
  const older = useInfiniteQuery({
    queryKey: ["older", props.agent],
    queryFn: ({ pageParam }) => api.olderTasks(props.agent, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next ?? undefined,
    enabled: false,
  });
  const seen = new Set(props.tasks.map((t) => t.id));
  const more = (older.data?.pages ?? [])
    .flatMap((page) => page.tasks)
    .filter((task) => !seen.has(task.id) && task.column === "archive");

  return (
    <details className="archive" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        Archive <span className="count">{props.tasks.length}</span>
      </summary>
      {props.tasks.length === 0 && more.length === 0 && (
        <p className="empty">Finished tasks appear here.</p>
      )}
      <ul className="cards archive-cards">
        {[...props.tasks, ...more].map((task) => (
          <li key={task.id}>
            <TaskCard agent={props.agent} task={task} onChanged={props.onChanged} />
          </li>
        ))}
      </ul>
      {(older.hasNextPage || !older.data) && (
        <button
          type="button"
          className="quiet"
          disabled={older.isFetching}
          onClick={() => (older.data ? older.fetchNextPage() : older.refetch())}
        >
          {older.isFetching ? "Loading…" : "Show older tasks"}
        </button>
      )}
      {older.error && <p role="alert">{older.error.message}</p>}
    </details>
  );
}
