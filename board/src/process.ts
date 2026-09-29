// What a process card says of its process (ADR 0019), as pure functions of the BFF's view.

import type { Resolution } from "./api";
import type { ProcessView, Task } from "./board";

export const MAX_REASON_CHARS = 4000;

const FAILURES: Record<string, string> = {
  return_limit: "The stage was rejected more times than the process allows.",
  stale_limit: "The stage's target kept changing under it; the process gave up.",
  ended_by_owner: "You ended the process.",
  reported: "A stage finished without a proposal.",
  no_proposal: "A stage finished without a proposal.",
  failed: "A stage's run failed.",
  stage_canceled: "A stage was canceled.",
};

export function stageText(process: ProcessView): string {
  return `Stage ${process.index + 1} of ${process.count}: ${process.stage}`;
}

export function attemptText(process: ProcessView): string | null {
  return process.attempt > 0 ? `Attempt ${process.attempt + 1} of ${process.maxAttempts}` : null;
}

export function staleText(process: ProcessView): string | null {
  if (process.staleReruns <= 0) return null;
  const times = process.staleReruns === 1 ? "once" : `${process.staleReruns} times`;
  return `Rerun ${times} because its target changed`;
}

export function failureText(process: ProcessView): string | null {
  if (!process.reason) return null;
  return FAILURES[process.reason] ?? process.reason;
}

export function resolutionProblem(action: Resolution, reason: string): string | null {
  if (reason.length > MAX_REASON_CHARS) return "Keep the reason under 4000 characters.";
  if (action === "rerun" && reason.trim() === "") return "Say why the stage should run again.";
  return null;
}

// What a task in "Waiting for me" asks of its owner, wherever the task is shown.
export function answerForm(task: Task): "resolution" | "reply" | null {
  if (task.column !== "waiting") return null;
  return task.process?.state === "needs_reason" ? "resolution" : "reply";
}
