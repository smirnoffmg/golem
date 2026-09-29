import { describe, expect, it } from "vitest";
import type { ProcessView, Task } from "./board";
import {
  answerForm,
  attemptText,
  failureText,
  resolutionProblem,
  stageText,
  staleText,
} from "./process";

function view(overrides: Partial<ProcessView> = {}): ProcessView {
  return {
    state: "running",
    stage: "design",
    index: 1,
    count: 3,
    attempt: 0,
    maxAttempts: 3,
    staleReruns: 0,
    reason: null,
    ...overrides,
  };
}

describe("a process card", () => {
  it("counts stages from one", () => {
    expect(stageText(view())).toBe("Stage 2 of 3: design");
  });

  it("names the attempt only once a stage has been rerun", () => {
    expect(attemptText(view({ attempt: 0 }))).toBeNull();
    expect(attemptText(view({ attempt: 1 }))).toBe("Attempt 2 of 3");
  });

  it("says how often the target changed under the stage", () => {
    expect(staleText(view({ staleReruns: 0 }))).toBeNull();
    expect(staleText(view({ staleReruns: 1 }))).toBe("Rerun once because its target changed");
    expect(staleText(view({ staleReruns: 2 }))).toBe("Rerun 2 times because its target changed");
  });

  it("explains why a process failed in words, and passes an unknown reason through", () => {
    expect(failureText(view({ state: "failed", reason: "return_limit" }))).toBe(
      "The stage was rejected more times than the process allows.",
    );
    expect(failureText(view({ state: "failed", reason: "ended_by_owner" }))).toBe(
      "You ended the process.",
    );
    expect(failureText(view({ state: "failed", reason: "caller over quota" }))).toBe(
      "caller over quota",
    );
    expect(failureText(view({ reason: null }))).toBeNull();
  });
});

describe("the answer to a process waiting for a reason", () => {
  it("needs a reason to rerun the stage", () => {
    expect(resolutionProblem("rerun", "   ")).toBe("Say why the stage should run again.");
    expect(resolutionProblem("rerun", "Cover the API.")).toBeNull();
  });

  it("needs no reason to end the process", () => {
    expect(resolutionProblem("end", "")).toBeNull();
  });

  it("keeps a reason within 4000 characters", () => {
    expect(resolutionProblem("rerun", "x".repeat(4001))).toBe(
      "Keep the reason under 4000 characters.",
    );
  });
});

describe("the answer a waiting task asks for", () => {
  function waiting(process: ProcessView | null): Task {
    return {
      id: "t-1",
      state: "working",
      column: "waiting",
      goal: "g",
      message: "",
      updated: "2026-09-28T10:00:00Z",
      proposal: null,
      process,
    };
  }

  it("is a reason for a process waiting for one, on the card and on the task's page alike", () => {
    expect(answerForm(waiting(view({ state: "needs_reason" })))).toBe("resolution");
  });

  it("is a reply for a task waiting for input", () => {
    expect(answerForm({ ...waiting(null), state: "input-required" })).toBe("reply");
  });

  it("is none for a task not waiting", () => {
    expect(answerForm({ ...waiting(null), column: "in_progress" })).toBeNull();
  });
});
