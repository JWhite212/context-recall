import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { ToastProvider } from "../components/common/Toast";

/** Fresh QueryClient with retries off — for tests that render the provider inline. */
export function makeTestQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

/**
 * Universal RTL `wrapper`: QueryClient + ToastProvider + MemoryRouter.
 * Each provider is inert for components that don't consume it, so one
 * shape serves every component test (previously 14 files carried their
 * own near-identical copy).
 */
export function makeWrapper() {
  const client = makeTestQueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}
