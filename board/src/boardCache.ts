import type { QueryClient } from "@tanstack/react-query";
import { type Board, type BoardResponse, type Task, cursorToAsk, nextBoard, withTask } from "./board";

export function boardKey(agent: string) {
  return ["board", agent] as const;
}

// Each poll asks for what changed since the last one and merges it into what the board already
// shows; a snapshot answer replaces it (ADR 0018).
export function boardQueryFn(
  client: QueryClient,
  agent: string,
  fetchBoard: (agent: string, since?: string) => Promise<BoardResponse>,
) {
  return async (): Promise<Board> => {
    const previous = client.getQueryData<Board>(boardKey(agent));
    return nextBoard(previous, await fetchBoard(agent, cursorToAsk(previous)));
  };
}

// A poll sent before the start was listed without the new task, and its answer would overwrite
// the card when it lands after it; so it is cancelled first and the board asked again after.
export async function showStarted(client: QueryClient, agent: string, task: Task): Promise<void> {
  const key = boardKey(agent);
  await client.cancelQueries({ queryKey: key });
  client.setQueryData<Board>(key, (current) => withTask(current, task));
  await client.invalidateQueries({ queryKey: key });
}
