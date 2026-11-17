/** Persist in-flight AI Agent work so leaving /work does not lose the reply. */

export type AiPendingJob = {
  sessionId: string;
  runId: string;
  command: string;
  startedAt: number;
};

const KEY = "yw-ai-pending-job";
/** Active conversation — cleared only by New Chat so follow-ups reuse the same session. */
const ACTIVE_SESSION_KEY = "yw-ai-active-session";

export function readAiPending(): AiPendingJob | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AiPendingJob;
    if (!parsed?.sessionId || !parsed?.runId || !parsed?.command) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function writeAiPending(job: AiPendingJob): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(job));
    writeActiveSession(job.sessionId);
  } catch {
    /* ignore */
  }
}

export function clearAiPending(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

export function readActiveSession(): string | null {
  try {
    const id = sessionStorage.getItem(ACTIVE_SESSION_KEY);
    return id && id.trim() ? id.trim() : null;
  } catch {
    return null;
  }
}

export function writeActiveSession(sessionId: string): void {
  try {
    if (sessionId && sessionId !== "pending") {
      sessionStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    }
  } catch {
    /* ignore */
  }
}

export function clearActiveSession(): void {
  try {
    sessionStorage.removeItem(ACTIVE_SESSION_KEY);
  } catch {
    /* ignore */
  }
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
