// Run with Node 24+: node --test supabase/functions/clv-checkpoints/ownership.test.mjs
// Executes the real edge handler with Deno/fetch stubs; no network or database.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";
import vm from "node:vm";

test("legacy edge observer excludes experiment rows before any grading or fetch", async () => {
  const source = (await readFile(new URL("index.ts", import.meta.url), "utf8"))
    .replace('import "jsr:@supabase/functions-js/edge-runtime.d.ts";', "");
  let handler;
  const requests = [];
  const environment = {
    SUPABASE_URL: "https://example.supabase.co",
    SUPABASE_SERVICE_ROLE_KEY: "dummy-service-role",
    SUPABASE_ANON_KEY: "dummy-publishable",
  };
  const baseRow = {
    platform: "polymarket", market_id: "tc-temp-test", outcome_id: "yes", side: "YES",
    status_15m: "pending", status_1h: "observed", status_close: "observed",
    due_15m: null, due_1h: null, due_close: null,
  };
  const context = vm.createContext({
    URL, Request, Response, AbortSignal, TextEncoder, console,
    Deno: { env: { get: (name) => environment[name] }, serve: (callback) => { handler = callback; } },
    fetch: async (input, options = {}) => {
      const url = new URL(input);
      assert.equal(url.hostname, "example.supabase.co", "must not fetch an exchange book for excluded rows");
      requests.push({ url, options });
      if (options.method === "PATCH") {
        assert.equal(url.searchParams.get("candidate_id"), "eq.legacy");
        return Response.json([{ candidate_id: "legacy" }]);
      }
      assert.equal(url.searchParams.get("metadata->>experiment_id"), "is.null");
      // Simulate a stale query proxy returning an excluded experiment anyway.
      return Response.json([
        { ...baseRow, candidate_id: "forward", metadata: { experiment_id: "weather-forward-v1" } },
        { ...baseRow, candidate_id: "legacy", metadata: {} },
      ]);
    },
  });
  vm.runInContext(stripTypeScriptTypes(source), context);
  const response = await handler(new Request("https://example.test", {
    method: "POST", headers: { apikey: "dummy-publishable" },
  }));
  assert.equal(response.status, 200);
  const summary = await response.json();
  assert.equal(summary.skipped_experiment, 1);
  assert.equal(summary.unavailable, 1);
  assert.equal(requests.length, 2);
});
