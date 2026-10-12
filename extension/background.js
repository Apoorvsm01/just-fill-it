// Service worker: talks to the local apply-helper server (content scripts can't reach 127.0.0.1
// because of the page's CORS rules), keeps the run history, and re-opens the panel after every page
// load in tabs where you opened it ("pinned" until you close it with ×).

const DEFAULTS = { port: 8767, token: "" };
const MAX_RUNS = 10;

async function settings() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { port: Number(stored.port) || DEFAULTS.port, token: String(stored.token || "").trim() };
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const { port, token } = await settings();
  let response;
  try {
    response = await fetch(`http://127.0.0.1:${port}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new Error(
      "Can't reach the Just Fill it! helper. Start it with: uv run just-fill-it " +
        "(or install the login item: sh scripts/install_helper.sh)."
    );
  }
  if (raw) {
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `file download failed (${response.status})`);
    }
    return response;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.details) ? ": " + data.details.map((d) => `${d.loc?.join(".")} ${d.msg}`).join("; ") : "";
    const error = new Error((data.error || `HTTP ${response.status}`) + detail);
    error.status = response.status;
    throw error;
  }
  return data;
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

// ---------- run history (global, survives browser and helper restarts) ----------
async function getRuns() {
  return (await chrome.storage.local.get({ runs: [] })).runs;
}

async function saveRuns(runs) {
  await chrome.storage.local.set({ runs: runs.slice(-MAX_RUNS) });
}

const busy = (run) => run.status === "queued" || run.status === "running";

async function pollRuns() {
  const runs = await getRuns();
  let changed = false;
  for (const [i, run] of runs.entries()) {
    if (!busy(run)) continue;
    try {
      const task = await api(`/api/tasks/${run.id}`);
      runs[i] = { ...run, ...task, finishedAt: busy(task) ? null : Date.now() };
    } catch (err) {
      if (err.status === 404) {
        runs[i] = { ...run, status: "failed", error: "The helper restarted during this run; start it again.", finishedAt: Date.now() };
      } else {
        continue; // helper briefly unreachable: keep waiting
      }
    }
    changed = true;
  }
  if (changed) await saveRuns(runs);
  return runs;
}

// ---------- pinned tabs: re-open the panel after each page load ----------
const pinKey = (tabId) => `pinned:${tabId}`;

async function isPinned(tabId) {
  return Boolean((await chrome.storage.session.get(pinKey(tabId)))[pinKey(tabId)]);
}

async function inject(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    return true;
  } catch (err) {
    console.warn("Just Fill it!: can't run on this page", err);
    return false;
  }
}

chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  if (info.status === "complete" && (await isPinned(tabId))) await inject(tabId);
});

chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(pinKey(tabId)));

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id) return;
  await chrome.storage.session.set({ [pinKey(tab.id)]: true });
  await inject(tab.id); // a second click on an open panel toggles it closed (content.js unpins)
});

// ---------- messages from the panel ----------
async function handle(message, sender) {
  const tabId = sender.tab?.id;
  switch (message.type) {
    case "ping":
      return api("/api/ping");
    case "generate": {
      const task = await api("/api/generate", { method: "POST", body: message.job });
      const runs = await getRuns();
      runs.push({
        ...task,
        title: message.job.title,
        company: message.job.company,
        pageUrl: message.job.url,
        startedAt: Date.now(),
        finishedAt: null,
      });
      await saveRuns(runs);
      return runs;
    }
    case "runs":
      return pollRuns();
    case "dismiss": {
      const runs = (await getRuns()).filter((r) => r.id !== message.id);
      await saveRuns(runs);
      return runs;
    }
    case "file": {
      const response = await api(`/api/file?path=${encodeURIComponent(message.path)}`, { raw: true });
      return {
        base64: toBase64(await response.arrayBuffer()),
        type: response.headers.get("Content-Type") || "application/octet-stream",
      };
    }
    case "unpin":
      if (tabId) await chrome.storage.session.remove(pinKey(tabId));
      return true;
    case "pin":
      if (tabId) await chrome.storage.session.set({ [pinKey(tabId)]: true });
      return true;
    case "openOptions":
      return chrome.runtime.openOptionsPage();
    default:
      throw new Error(`unknown message ${message.type}`);
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handle(message, sender).then(
    (result) => sendResponse({ ok: true, result }),
    (err) => sendResponse({ ok: false, error: String(err?.message || err) })
  );
  return true; // async response
});
