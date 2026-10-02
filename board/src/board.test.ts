import { describe, expect, it } from "vitest";
import {
  type BoardResponse,
  type ProposalCard,
  type Task,
  columnsOf,
  cursorToAsk,
  nextBoard,
  reviewCards,
} from "./board";

function task(id: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    state: "working",
    column: "in_progress",
    goal: `goal ${id}`,
    message: "",
    updated: "2026-09-28T10:00:00Z",
    proposal: null,
    process: null,
    ...overrides,
  };
}

function response(
  tasks: Task[],
  complete: boolean,
  cursor = "c1",
  proposals: ProposalCard[] = [],
): BoardResponse {
  return { agent: "discovery", complete, cursor, tasks, proposals };
}

function proposal(id: string, taskId: string, overrides: Partial<ProposalCard> = {}): ProposalCard {
  return {
    id,
    taskId,
    agent: "discovery",
    kind: "desk_reply",
    state: "pending",
    column: "review",
    summary: `Reply ${id}`,
    url: null,
    owner: "user:bob",
    createdAt: "2026-09-29T10:00:00+00:00",
    decidedBy: null,
    decidedAt: null,
    ...overrides,
  };
}

describe("nextBoard", () => {
  it("takes a snapshot as the whole board", () => {
    const board = nextBoard(undefined, response([task("a"), task("b")], true));

    expect(Object.keys(board.tasks).sort()).toEqual(["a", "b"]);
    expect(board.cursor).toBe("c1");
  });

  it("merges a delta into the board, replacing tasks it has seen", () => {
    const before = nextBoard(undefined, response([task("a"), task("b")], true, "c1"));

    const after = nextBoard(
      before,
      response([task("b", { state: "completed", column: "archive" }), task("c")], false, "c2"),
    );

    expect(Object.keys(after.tasks).sort()).toEqual(["a", "b", "c"]);
    expect(after.tasks.b?.state).toBe("completed");
    expect(after.cursor).toBe("c2");
  });

  it("drops what a later snapshot no longer has", () => {
    const before = nextBoard(undefined, response([task("a"), task("b")], true));

    const after = nextBoard(before, response([task("b")], true, "c3"));

    expect(Object.keys(after.tasks)).toEqual(["b"]);
  });

  it("does not change the board it was given", () => {
    const before = nextBoard(undefined, response([task("a")], true));

    nextBoard(before, response([task("z")], false));

    expect(Object.keys(before.tasks)).toEqual(["a"]);
  });
});

describe("columnsOf", () => {
  it("groups tasks by the column the backend assigned, newest first", () => {
    const board = nextBoard(
      undefined,
      response(
        [
          task("old", { updated: "2026-09-28T09:00:00Z" }),
          task("new", { updated: "2026-09-28T11:00:00Z" }),
          task("wait", { state: "input-required", column: "waiting" }),
          task("done", { state: "canceled", column: "archive" }),
        ],
        true,
      ),
    );

    const columns = columnsOf(board);

    expect(columns.in_progress.map((t) => t.id)).toEqual(["new", "old"]);
    expect(columns.waiting.map((t) => t.id)).toEqual(["wait"]);
    expect(columns.archive.map((t) => t.id)).toEqual(["done"]);
    expect(columns.review).toEqual([]);
    expect(columns.failed).toEqual([]);
  });

  it("puts a column it does not know in progress rather than losing the task", () => {
    const board = nextBoard(
      undefined,
      response([task("x", { column: "somewhere-new" as Task["column"] })], true),
    );

    expect(columnsOf(board).in_progress.map((t) => t.id)).toEqual(["x"]);
  });
});

describe("cursorToAsk", () => {
  it("asks for a snapshot first", () => {
    expect(cursorToAsk(undefined)).toBeUndefined();
  });

  // A proposal's or a process's change moves its task's timestamp, so deltas carry it
  // (ADR 0018): no snapshot is needed after the first.
  it("asks for deltas from the last cursor from then on", () => {
    let board = nextBoard(undefined, response([task("a")], true, "c0"));
    for (let delta = 1; delta < 20; delta += 1) {
      expect(cursorToAsk(board)).toBe(board.cursor);
      board = nextBoard(board, response([], false, `c${delta}`));
    }

    expect(cursorToAsk(board)).toBe("c19");
  });
});

describe("the proposals on a board", () => {
  it("replaces the open set whole with every answer, a delta too", () => {
    const before = nextBoard(undefined, response([], true, "c1", [proposal("p1", "t1")]));

    const after = nextBoard(before, response([], false, "c2", [proposal("p2", "t2")]));

    expect(after.proposals.map((p) => p.id)).toEqual(["p2"]);
  });

  it("shows as cards of their own only the proposals of tasks that are not mine", () => {
    const mine = task("t1", {
      state: "completed",
      column: "review",
      proposal: { id: "p1", kind: "desk_reply", state: "pending", url: null },
    });
    const board = nextBoard(
      undefined,
      response([mine], true, "c1", [
        proposal("p1", "t1"),
        proposal("p2", "t-of-bob", { createdAt: "2026-09-29T09:00:00+00:00" }),
        proposal("p3", "t-of-carol", { createdAt: "2026-09-29T11:00:00+00:00" }),
      ]),
    );

    expect(reviewCards(board).map((p) => p.id)).toEqual(["p3", "p2"]);
  });

  it("gives a task the open set's newer state of its own proposal", () => {
    const mine = task("t1", {
      state: "completed",
      column: "review",
      proposal: { id: "p1", kind: "desk_reply", state: "pending", url: null },
    });
    const board = nextBoard(
      undefined,
      response([mine], true, "c1", [proposal("p1", "t1", { state: "failed" })]),
    );

    expect(columnsOf(board).review[0]?.proposal?.state).toBe("failed");
  });
});
