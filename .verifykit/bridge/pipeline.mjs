/**
 * VerifyKit ESM bridge for the WeChat article pipeline.
 *
 * VerifyKit's runtime loader is ESM-only, so this module is the explicit
 * language boundary: it shells out to the real Python business functions in
 * `.verifykit/bridge/local_pipeline.py` against an isolated temporary
 * workspace, and records each executed step with `kit.step()`.
 *
 * M0 scope: offline, deterministic local steps only. No network call is made
 * and no local step is ever reported as third-party evidence. Real provider
 * evidence (DeepSeek / WeChat) requires an actual provider-issued request id and
 * is added in M5.
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const BRIDGE_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(BRIDGE_DIR, "..", "..");
const SCRIPT = join(BRIDGE_DIR, "local_pipeline.py");
const PYTHON = process.env.VERIFYKIT_PYTHON || join(REPO_ROOT, ".venv", "bin", "python");
const DATE = process.env.VERIFYKIT_DATE || "2026-09-28";

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
