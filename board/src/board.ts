// The board's model: tasks by id, merged from snapshots and deltas. The transport (polling
// today, server-sent events later) only delivers the same two shapes (ADR 0018).

export type Column = "in_progress" | "waiting" | "review" | "failed" | "archive";

export type Proposal = { id: string; kind: string; state: string; url: string | null };

// A process's task shows where its process stands (ADR 0019); counts are as the BFF sends them.
export type ProcessView = {
  state: string;
  stage: string;
  index: number;
  count: number;
  attempt: number;
  maxAttempts: number;
  staleReruns: number;
  reason: string | null;
};

export type Task = {
  id: string;
  state: string;
  column: Column;
  goal: string;
  message: string;
  updated: string;
  proposal: Proposal | null;
  process: ProcessView | null;
};

export type BoardResponse = {
  agent: string;
  complete: boolean;
  cursor: string;
  tasks: Task[];
};

// `deltas`: how many deltas since the last snapshot.
export type Board = { cursor: string; deltas: number; tasks: Readonly<Record<string, Task>> };

// A proposal's or a process's change leaves its task's timestamp alone, so a delta never
// carries it; a snapshot costs the backend the same one call, and every sixth poll is one.
export const SNAPSHOT_EVERY = 6;

export const COLUMNS: readonly { id: Column; title: string; empty: string }[] = [
  { id: "waiting", title: "Waiting for me", empty: "Nothing needs your answer." },
  { id: "review", title: "To review", empty: "No results wait for review." },
  { id: "in_progress", title: "In progress", empty: "No task is running." },
  { id: "failed", title: "Failed", empty: "No task has failed." },
  { id: "archive", title: "Archive", empty: "Finished tasks appear here." },
];

const KNOWN = new Set<string>(COLUMNS.map((c) => c.id));

export function nextBoard(previous: Board | undefined, response: BoardResponse): Board {
  const base = response.complete || previous === undefined ? {} : previous.tasks;
  const tasks: Record<string, Task> = { ...base };
  for (const task of response.tasks) tasks[task.id] = task;
  const deltas = response.complete || previous === undefined ? 0 : previous.deltas + 1;
  return { cursor: response.cursor, deltas, tasks };
}

export function cursorToAsk(board: Board | undefined): string | undefined {
  return board === undefined || board.deltas >= SNAPSHOT_EVERY - 1 ? undefined : board.cursor;
}

export function snapshotNext(board: Board | undefined): Board | undefined {
  return board && { ...board, deltas: SNAPSHOT_EVERY };
}

export function withTask(board: Board | undefined, task: Task): Board | undefined {
  return board && { ...board, tasks: { ...board.tasks, [task.id]: task } };
}

export function columnsOf(board: Board): Record<Column, Task[]> {
  const columns: Record<Column, Task[]> = {
    in_progress: [],
    waiting: [],
    review: [],
    failed: [],
    archive: [],
  };
  for (const task of Object.values(board.tasks)) {
    columns[KNOWN.has(task.column) ? task.column : "in_progress"].push(task);
  }
  for (const tasks of Object.values(columns)) {
    tasks.sort((a, b) => b.updated.localeCompare(a.updated) || a.id.localeCompare(b.id));
  }
  return columns;
}

export const ACTIVE_STATES = new Set(["submitted", "working", "input-required", "auth-required"]);
