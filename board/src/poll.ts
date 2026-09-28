import { ApiError } from "./api";

export const BOARD_INTERVAL_MS = 10_000;
const RETRIES = 2;

// After a 429 the next poll waits as long as the backend asked, once; a success clears the
// error and the interval returns to the base (ADR 0018).
export function boardInterval(error: unknown): number | false {
  if (!(error instanceof ApiError)) return BOARD_INTERVAL_MS;
  if (error.unauthenticated) return false;
  if (error.status === 429 && error.retryAfterSeconds !== null) {
    return Math.max(error.retryAfterSeconds * 1000, BOARD_INTERVAL_MS);
  }
  return BOARD_INTERVAL_MS;
}

export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < RETRIES;
}
