/**
 * VerifyKit ESM bridge for the WeChat article pipeline.
 *
 * VerifyKit's runtime loader is ESM-only, so this module is the explicit
 * language boundary: it shells out to the real Python business functions in
 * `.verifykit/bridge/local_pipeline.py` against an isolated temporary
 * workspace, and records each executed step with `kit.step()`.
 *
 * M0 steps are offline and deterministic. M5 adds two live probes that are
 * DEFAULT OFF and only run when M5_LIVE_APPROVED=1 (and named in M5_LIVE_STEPS,
 * default "deepseek,wechat"): one capped DeepSeek request and one WeChat draft
 * creation against an isolated queue. Without that gate, pytest,
 * `verifykit check` and `verifykit run` never touch the network. A local
 * step is never reported as third-party evidence; a provider-issued request id
 * is recorded via kit.external() only when the provider actually returned one.
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const BRIDGE_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(BRIDGE_DIR, "..", "..");
const SCRIPT = join(BRIDGE_DIR, "local_pipeline.py");
const M5_SCRIPT = join(BRIDGE_DIR, "m5_acceptance.py");
const PYTHON = process.env.VERIFYKIT_PYTHON || join(REPO_ROOT, ".venv", "bin", "python");
const DATE = process.env.VERIFYKIT_DATE || "2026-09-28";

// Default OFF. Only an explicit M5_LIVE_APPROVED=1 enables any outbound call.
const M5_LIVE_APPROVED = process.env.M5_LIVE_APPROVED === "1";
// Selected live probes (comma list). Default "deepseek,wechat" keeps existing
// behavior; an unset env changes nothing.
const M5_LIVE_STEPS = new Set(
  (process.env.M5_LIVE_STEPS || "deepseek,wechat")
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean),
);
const M5_TIMEOUT_MS = { deepseek: 35000, wechat: 120000 };

function liveStepEnabled(name) {
  return M5_LIVE_APPROVED && M5_LIVE_STEPS.has(name);
}

function liveSkipReason(name) {
  if (!M5_LIVE_APPROVED) return "M5_LIVE_APPROVED!=1";
  return `M5_LIVE_STEPS excludes ${name}`;
}


function runPythonStep(step, workspace) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(PYTHON, [SCRIPT, "--step", step, "--workspace", workspace, "--date", DATE], {
      cwd: REPO_ROOT,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => rejectPromise(error));
    child.on("close", (code) => {
      if (code !== 0) {
        rejectPromise(new Error(`${step} exited ${code}: ${stderr.slice(-400)}`));
        return;
      }
      const lastLine = stdout.trim().split("\n").filter(Boolean).pop() || "";
      try {
        resolvePromise(JSON.parse(lastLine));
      } catch (error) {
        rejectPromise(new Error(`${step} returned non-JSON output: ${stdout.slice(-400)}`));
      }
    });
  });
}

function runM5Step(step, runId) {
  const timeoutMs = M5_TIMEOUT_MS[step] ?? 35000;
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(
      PYTHON,
      [M5_SCRIPT, "--step", step, "--repo-root", REPO_ROOT, "--run-id", runId],
      { cwd: REPO_ROOT },
    );
    let stdout = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill("SIGKILL");
      rejectPromise(new Error(`m5 ${step} exceeded ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    // stderr is drained but intentionally not surfaced: it may carry a traceback
    // and the detail is already classified inside the JSON result.
    child.stderr.on("data", () => {});
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      rejectPromise(error);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code !== 0) {
        rejectPromise(new Error(`m5 ${step} exited ${code}`));
        return;
      }
      const lastLine = stdout.trim().split("\n").filter(Boolean).pop() || "";
      try {
        resolvePromise(JSON.parse(lastLine));
      } catch (error) {
        rejectPromise(new Error(`m5 ${step} returned non-JSON output: ${error.message}`));
      }
    });
  });
}

export async function runPipeline(ctx) {
  const { kit } = ctx;
  const workspace = mkdtempSync(join(tmpdir(), "verifykit-wechat-"));
  const enabledFlag = true;
  let business = { ranked: [] };
  try {
    await kit.step(
      "wechat.material_pool",
      {
        required: true,
        enabled: enabledFlag,
        skipReason: "WECHAT_ARTICLE_PIPELINE=false",
        input: { date: DATE, source: "bridge fixtures" },
      },
      async () => {
        const result = await runPythonStep("pool", workspace);
        return { candidates: result.candidate_count };
      },
    );

    const selection = await kit.step(
      "wechat.candidate_selection",
      {
        required: true,
        enabled: enabledFlag,
        skipReason: "WECHAT_ARTICLE_PIPELINE=false",
        input: { date: DATE },
      },
      async () => runPythonStep("select", workspace),
    );
    business = { ranked: selection?.ranked ?? [] };

    await kit.step(
      "wechat.queue_enqueue",
      {
        required: true,
        enabled: enabledFlag,
        skipReason: "WECHAT_ARTICLE_PIPELINE=false",
        input: { articles: (selection?.ranked ?? []).length },
      },
      async () => runPythonStep("enqueue", workspace),
    );

    // ── M5 live probes (default OFF) ──
    // These are not required steps: a skipped probe cannot fail the Golden Path.
    // When enabled and a probe fails, kit.degrade() records an explicit,
    // unexpected fallback (mode=mock) instead of masking the failure.
    await kit.step(
      "wechat.deepseek_live",
      {
        required: false,
        enabled: liveStepEnabled("deepseek"),
        skipReason: liveSkipReason("deepseek"),
        input: { date: DATE, max_tokens: 128, attempts: 1 },
      },
      async () => {
        const result = await runM5Step("deepseek", kit.runId);
        if (result && result.provider_request_id) {
          // Only a genuinely provider-issued id is recorded. No synthesized id.
          kit.external({
            provider: "deepseek",
            type: "chat_completion_request",
            externalId: String(result.provider_request_id),
            source: "external",
            metadata: {
              model: result.response_model_id || result.request_model_id || null,
              http_status: result.http_status ?? null,
              content_non_empty: result.content_non_empty === true,
            },
          });
        }
        if (!result || result.ok !== true) {
          kit.degrade(`m5.deepseek.${(result && result.error_class) || "unknown"}`);
        }
        return result;
      },
    );

    await kit.step(
      "wechat.draft_live",
      {
        required: false,
        enabled: liveStepEnabled("wechat"),
        skipReason: liveSkipReason("wechat"),
        input: { article: "docs/acceptance/m5-test-article.md", publish: false },
      },
      async () => {
        const result = await runM5Step("wechat", kit.runId);
        if (!result || result.ok !== true) {
          kit.degrade(`m5.wechat.${(result && result.error_class) || "unknown"}`);
        }
        return result;
      },
    );
  } finally {
    try {
      rmSync(workspace, { recursive: true, force: true });
    } catch (error) {
      // Best-effort cleanup only; a leftover tmp dir is not a business failure.
      // Log it so the fallback is visible rather than silent.
      console.warn(`verifykit bridge: temp workspace cleanup skipped (${error.message})`);
    }
  }

  return { business };
}
