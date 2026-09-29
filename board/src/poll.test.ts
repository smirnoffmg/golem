import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { BOARD_INTERVAL_MS, boardInterval, shouldRetry } from "./poll";

const limited = new ApiError(429, "rate_limited", "Too many.", 23);

describe("boardInterval", () => {
  it("polls every 10 s while things go well", () => {
    expect(boardInterval(null)).toBe(BOARD_INTERVAL_MS);
    expect(BOARD_INTERVAL_MS).toBe(10_000);
  });

  it("waits as long as a 429 asks", () => {
    expect(boardInterval(limited)).toBe(23_000);
  });

  it("never polls faster than the base interval", () => {
    expect(boardInterval(new ApiError(429, "rate_limited", "", 1))).toBe(BOARD_INTERVAL_MS);
  });

  it("stops after a 401: the browser is on its way to sign in", () => {
    expect(boardInterval(new ApiError(401, "unauthenticated", ""))).toBe(false);
  });

  it("keeps the base interval after other failures", () => {
    expect(boardInterval(new ApiError(502, "edge_failed", ""))).toBe(BOARD_INTERVAL_MS);
  });
});

describe("shouldRetry", () => {
  it("does not retry a refusal", () => {
    for (const status of [400, 401, 403, 404, 409, 429]) {
      expect(shouldRetry(0, new ApiError(status, "x", ""))).toBe(false);
    }
  });

  it("retries a server or network failure twice", () => {
    const failure = new ApiError(502, "edge_failed", "");
    expect(shouldRetry(0, failure)).toBe(true);
    expect(shouldRetry(1, failure)).toBe(true);
    expect(shouldRetry(2, failure)).toBe(false);
    expect(shouldRetry(0, new ApiError(0, "network", ""))).toBe(true);
  });
});
