import { describe, expect, it } from "vitest";
import {
  type BoardResponse,
  SNAPSHOT_EVERY,
  type Task,
  columnsOf,
  cursorToAsk,
  nextBoard,
  snapshotNext,
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

function response(tasks: Task[], complete: boolean, cursor = "c1"): BoardResponse {
  return { agent: "discovery", complete, cursor, tasks };
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
  // A proposal's or a process's change does not move its task's timestamp, so only a
  // snapshot shows it (ADR 0018).
  it("asks for a snapshot first", () => {
    expect(cursorToAsk(undefined)).toBeUndefined();
  });

  it("asks for deltas, then a snapshot after SNAPSHOT_EVERY of them", () => {
    let board = nextBoard(undefined, response([task("a")], true, "c0"));
    for (let delta = 1; delta < SNAPSHOT_EVERY; delta += 1) {
      expect(cursorToAsk(board)).toBe(board.cursor);
      board = nextBoard(board, response([], false, `c${delta}`));
    }

    expect(cursorToAsk(board)).toBeUndefined();
    board = nextBoard(board, response([task("a")], true, "c9"));
    expect(cursorToAsk(board)).toBe("c9");
  });

  it("asks for a snapshot next once something changed that deltas cannot see", () => {
    const board = nextBoard(undefined, response([task("a")], true, "c0"));

    expect(cursorToAsk(snapshotNext(board))).toBeUndefined();
    expect(snapshotNext(undefined)).toBeUndefined();
  });
});
