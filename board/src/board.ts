// The board's model: tasks by id, merged from snapshots and deltas. The transport (polling
// today, server-sent events later) only delivers the same two shapes (ADR 0018).

export type Column = "in_progress" | "waiting" | "review" | "failed" | "archive";

export type Proposal = { id: string; kind: string; state: string; url: string | null };

// An open proposal the person may decide: of their own task, or of an agent they review.
export type ProposalCard = {
  id: string;
  taskId: string;
  agent: string;
  kind: string;
  state: string;
  column: Column;
  summary: string;
  url: string | null;
  owner: string;
  createdAt: string;
  decidedBy: string | null;
  decidedAt: string | null;
};

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
  proposals: ProposalCard[];
};

export type Board = {
  cursor: string;
  tasks: Readonly<Record<string, Task>>;
  proposals: readonly ProposalCard[];
};

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
  // The open set comes whole with every answer: a proposal's change can come without its task's.
  return { cursor: response.cursor, tasks, proposals: response.proposals ?? [] };
}

// Open proposals that are not of one of the person's tasks: those they decide as a reviewer.
export function reviewCards(board: Board): ProposalCard[] {
  const mine = new Set(Object.keys(board.tasks));
  return board.proposals
    .filter((proposal) => !mine.has(proposal.taskId))
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt) || a.id.localeCompare(b.id));
}

export function cursorToAsk(board: Board | undefined): string | undefined {
  return board?.cursor;
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
  const open = new Map(board.proposals.map((proposal) => [proposal.id, proposal]));
  for (const task of Object.values(board.tasks)) {
    const own = task.proposal;
    const newer = own ? open.get(own.id) : undefined;
    const shown = own && newer ? { ...task, proposal: { ...own, state: newer.state } } : task;
    columns[KNOWN.has(task.column) ? task.column : "in_progress"].push(shown);
  }
  for (const tasks of Object.values(columns)) {
    tasks.sort((a, b) => b.updated.localeCompare(a.updated) || a.id.localeCompare(b.id));
  }
  return columns;
}

export const ACTIVE_STATES = new Set(["submitted", "working", "input-required", "auth-required"]);
