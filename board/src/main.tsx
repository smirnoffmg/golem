import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ApiError } from "./api";
import { App } from "./App";
import { shouldRetry } from "./poll";
import "./styles.css";

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError(error, query) {
      // A session that ends while someone looks: sign in again. The session query itself
      // answers "signed out" instead, so the landing page shows rather than a redirect loop.
      if (error instanceof ApiError && error.unauthenticated && query.queryKey[0] !== "session") {
        window.location.assign("/login");
      }
    },
  }),
  defaultOptions: {
    queries: { retry: shouldRetry, refetchIntervalInBackground: false },
    mutations: { retry: false },
  },
});

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </StrictMode>,
  );
}
