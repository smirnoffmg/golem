import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import type { Board, BoardResponse, Task } from "./board";
import { boardKey, boardQueryFn, showStarted } from "./boardCache";

function task(id: string): Task {
  return {
    id,
    state: "working",
    column: "in_progress",
    goal: `goal ${id}`,
    message: "",
    updated: "2026-09-29T10:00:00Z",
    proposal: null,
    process: null,
  };
}

function snapshot(...tasks: Task[]): BoardResponse {
  return { agent: "discovery", complete: true, cursor: "c", tasks, proposals: [] };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

describe("showStarted", () => {
  it("keeps a started task when a poll sent before the start answers after it", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const started = task("new");
    const first = deferred<BoardResponse>();
    const poll = deferred<BoardResponse>();
    const after = deferred<BoardResponse>();
    const answers = [first, poll, after];
    const fetchBoard = () => {
      const next = answers.shift();
      if (next === undefined) throw new Error("the board was asked more often than expected");
      return next.promise;
    };
    const observer = new QueryObserver(client, {
      queryKey: boardKey("discovery"),
      queryFn: boardQueryFn(client, "discovery", fetchBoard),
    });
    const unsubscribe = observer.subscribe(() => {});
    first.resolve(snapshot(task("old")));
    await observer.refetch({ cancelRefetch: false });

    const polled = observer.refetch();
    const shown = showStarted(client, "discovery", started);
    // The poll was listed before the task existed, so its answer does not have it.
    poll.resolve(snapshot(task("old")));
    after.resolve(snapshot(task("old"), started));
    await Promise.all([polled, shown]);

    const board = client.getQueryData<Board>(boardKey("discovery"));
    expect(Object.keys(board?.tasks ?? {}).sort()).toEqual(["new", "old"]);
    unsubscribe();
  });
});
