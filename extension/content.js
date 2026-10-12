// In-page apply panel. Opened from the toolbar button (or Alt+Shift+J); after that the background
// re-injects it on every page load in that tab until you close it with ×. Lives in a shadow root so
// the page's CSS can't touch it, and in the page itself so a finished PDF can be dragged straight
// onto the form's upload area.
(() => {
  if (window.__jobHunterPanel) {
    window.__jobHunterPanel.toggle();
    return;
  }

  const MAX_TEXT = 150000;
  // Safari gets the Liquid Glass look; Chrome keeps the plain panel.
  const GLASS = chrome.runtime.getURL("").startsWith("safari-web-extension://");
  // Chrome (and other Chromium browsers) get Google's Material 3.
  const SHOWN_RUNS = 5;
  const send = (message) =>
    new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(message, (reply) => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
        if (!reply?.ok) return reject(new Error(reply?.error || "no reply from the extension"));
        resolve(reply.result);
      });
    });
  const store = {
    get: (defaults) => new Promise((resolve) => {
      try {
        chrome.storage.local.get(defaults, resolve);
      } catch {
        resolve(defaults);
      }
    }),
    set: (values) => {
      try {
        chrome.storage.local.set(values);
      } catch {}
    },
  };

  // ---------- job extraction ----------
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const textOf = (sel) => {
    for (const s of sel.split(",")) {
      const el = document.querySelector(s.trim());
      if (el && clean(el.innerText)) return el.innerText.trim();
    }
    return "";
  };

  function fromJsonLd() {
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data;
      try {
        data = JSON.parse(script.textContent);
      } catch {
        continue;
      }
      const items = [].concat(data, data?.["@graph"] || []).flat();
      const job = items.find((d) => d && [].concat(d["@type"]).includes("JobPosting"));
      if (!job) continue;
      const place = [].concat(job.jobLocation || [])[0]?.address || {};
      return {
        title: clean(job.title),
        company: clean(job.hiringOrganization?.name),
        location: clean([place.addressLocality, place.addressRegion, place.addressCountry?.name || place.addressCountry].filter(Boolean).join(", ")),
        text: String(job.description || ""),
        source: "the page's structured job data",
      };
    }
    return null;
  }

  function fromLinkedIn() {
    if (!location.hostname.endsWith("linkedin.com")) return null;
    const text = textOf("#job-details, .jobs-description__content, .show-more-less-html__markup, .jobs-box__html-content");
    if (!text) return null;
    return {
      title: clean(textOf(".job-details-jobs-unified-top-card__job-title, .top-card-layout__title, .jobs-unified-top-card__job-title, h1")),
      company: clean(textOf(".job-details-jobs-unified-top-card__company-name, .topcard__org-name-link, .jobs-unified-top-card__company-name")),
      location: clean(textOf(".job-details-jobs-unified-top-card__primary-description-container .tvm__text, .topcard__flavor--bullet")),
      text,
      source: "the LinkedIn job panel",
    };
  }

  function fromPage() {
    const main = textOf("main, [role=main], article") || document.body.innerText;
    const site = document.querySelector('meta[property="og:site_name"]')?.content;
    return {
      title: clean(textOf("h1") || document.title),
      company: clean(site || location.hostname.replace(/^www\.|^jobs\.|^careers\./, "").split(".")[0]),
      location: "",
      text: main,
      source: "the page text",
    };
  }

  function fromSelection() {
    const text = String(window.getSelection() || "").trim();
    return text.length >= 200 ? text : "";
  }

  function extractJob() {
    const job = fromJsonLd() || fromLinkedIn() || fromPage();
    const selected = fromSelection();
    if (selected) Object.assign(job, { text: selected, source: "your text selection" });
    job.text = job.text.slice(0, MAX_TEXT);
    return job;
  }

  // ---------- file helpers ----------
  const fileCache = new Map();
  async function getFile(file) {
    if (!fileCache.has(file.path)) {
      const { base64, type } = await send({ type: "file", path: file.path });
      const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
      fileCache.set(file.path, new File([bytes], file.name, { type: type.split(";")[0] }));
    }
    return fileCache.get(file.path);
  }

  function inputContext(input) {
    const bits = [input.name, input.id, input.getAttribute("aria-label"), input.accept];
    if (input.id) bits.push(document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText);
    let el = input;
    for (let i = 0; i < 4 && el; i++, el = el.parentElement) bits.push(clean(el.innerText).slice(0, 200));
    return bits.filter(Boolean).join(" ").toLowerCase();
  }

  function pickInput(kind) {
    const inputs = [...document.querySelectorAll('input[type="file"]')].filter((i) => !i.disabled);
    if (!inputs.length) return null;
    const want = kind === "cover_letter" ? /cover/ : /resume|résumé|\bcv\b/;
    const avoid = kind === "cover_letter" ? /resume|résumé|\bcv\b/ : /cover/;
    return (
      inputs.find((i) => want.test(inputContext(i)) && !avoid.test(inputContext(i))) ||
      inputs.find((i) => want.test(inputContext(i))) ||
      inputs.find((i) => !avoid.test(inputContext(i))) ||
      inputs[0]
    );
  }

  function attachTo(input, file) {
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // When a page's drop zone ignores the file on a drag (some read only real OS drags), put it into
  // the nearest file input instead.
  let dragging = null;
  document.addEventListener(
    "drop",
    (event) => {
      if (!dragging || event.dataTransfer?.files?.length) return;
      let el = event.target instanceof Element ? event.target : null;
      let input = null;
      for (let i = 0; i < 6 && el && !input; i++, el = el.parentElement) input = el.querySelector?.('input[type="file"]');
      input = input || pickInput(dragging.kind);
      if (input) {
        event.preventDefault();
        attachTo(input, dragging.file);
      }
    },
    true
  );

  // ---------- UI ----------
  const host = document.createElement("div");
  host.style.cssText = "all: initial; position: fixed; z-index: 2147483647; right: 16px; bottom: 16px;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
  <style>
    :host { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#d2d2d7; --accent:#0a66c2; --ok:#1a7f37; --bad:#c62828; --chip:#f2f5f9; }
    @media (prefers-color-scheme: dark) { :host { --bg:#1e1f22; --fg:#f2f2f2; --muted:#a1a1a6; --line:#3a3b3f; --accent:#4c9aff; --ok:#3fb950; --bad:#ff6b6b; --chip:#2a2c31; } }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .panel { width: 360px; max-height: calc(100vh - 32px); overflow: auto; background: var(--bg); color: var(--fg);
      border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 12px 32px rgba(0,0,0,.22); padding: 14px; font-size: 13px; }
    .pill { display: none; align-items: center; gap: 8px; font: 600 13px -apple-system, BlinkMacSystemFont, sans-serif; cursor: pointer;
      background: var(--accent); color: #fff; border: none; border-radius: 999px; padding: 9px 14px; box-shadow: 0 6px 18px rgba(0,0,0,.25); }
    :host(.min) .panel { display: none; }
    :host(.min) .pill { display: inline-flex; }
    header { display:flex; align-items:center; gap:4px; margin-bottom:10px; }
    header b { flex:1; font-size:14px; }
    button { font: inherit; cursor: pointer; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); padding: 6px 10px; }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; flex:1; padding: 9px 10px; }
    button:disabled { opacity: .5; cursor: default; }
    button.icon { border: none; padding: 2px 7px; font-size: 16px; color: var(--muted); background: transparent; }
    label { display:block; color: var(--muted); font-size: 11px; margin: 8px 0 3px; text-transform: uppercase; letter-spacing: .04em; }
    input, textarea { width:100%; font: inherit; color: var(--fg); background: var(--bg); border:1px solid var(--line); border-radius:8px; padding:7px 8px; }
    textarea { min-height: 76px; resize: vertical; }
    .row { display:flex; gap:8px; margin-top:10px; }
    .meta { color: var(--muted); font-size: 12px; margin-top: 6px; display:flex; gap:6px; align-items:center; flex-wrap: wrap; }
    .meta button { padding: 2px 8px; font-size: 12px; }
    h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 16px 0 0; }
    .task { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 10px; }
    .task .top { display:flex; align-items:center; gap:8px; }
    .task .top b { flex:1; }
    .sub { color: var(--muted); font-size: 12px; margin-top: 2px; overflow:hidden; text-overflow: ellipsis; white-space: nowrap; }
    .sub a { color: inherit; }
    .status { color: var(--muted); font-size: 12px; }
    .status.done { color: var(--ok); } .status.failed { color: var(--bad); }
    .spin { width: 12px; height: 12px; border: 2px solid rgba(127,127,127,.4); border-top-color: currentColor; border-radius: 50%; animation: s 0.9s linear infinite; }
    @keyframes s { to { transform: rotate(360deg); } }
    .chip { display:flex; align-items:center; gap:6px; background: var(--chip); border:1px solid var(--line); border-radius: 8px; padding: 6px 8px; margin-top: 8px; cursor: grab; }
    .chip span { flex:1; overflow:hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chip button { padding: 3px 8px; font-size: 12px; }
    .hint { color: var(--muted); font-size: 11px; margin-top: 4px; }
    .err { color: var(--bad); white-space: pre-wrap; margin-top: 6px; }
    details { margin-top: 6px; } summary { cursor: pointer; color: var(--muted); }
    pre { white-space: pre-wrap; font-size: 12px; background: var(--chip); padding: 8px; border-radius: 8px; max-height: 220px; overflow: auto; }
    .notice { background: var(--chip); border-radius: 8px; padding: 8px; margin-bottom: 8px; }
    .more { margin-top: 10px; width: 100%; }
    .empty { color: var(--muted); font-size: 12px; margin-top: 6px; }
    .opts { display:flex; align-items:center; gap:10px; margin-top: 10px; flex-wrap: wrap; }
    .seg { display:inline-flex; border:1px solid var(--line); border-radius: 8px; overflow:hidden; }
    .seg button { border:none; border-radius:0; padding: 5px 12px; font-size: 12px; background: transparent; }
    .seg button[aria-pressed="true"] { background: var(--accent); color:#fff; }
    .check { display:inline-flex; align-items:center; gap:6px; font-size: 12px; color: var(--fg); margin: 0; text-transform:none; letter-spacing:0; cursor:pointer; }
    .check input { width:auto; margin:0; }
    :host(.glass) .seg { border-radius: 999px; background: var(--fill); border: none; padding: 2px; }
    :host(.glass) .seg button { color: var(--fg); }
    :host(.glass) .seg button { border-radius: 999px; border:none; box-shadow:none; background: transparent; }
    :host(.glass) .seg button[aria-pressed="true"] { background: linear-gradient(180deg, rgba(64,156,255,.95), rgba(10,132,255,.92)); color:#fff; box-shadow: inset 0 1px 0 rgba(255,255,255,.45); }

    /* ---------- Material 3 (Chrome) ---------- */
    :host(.m3) { --primary:#0b57d0; --on-primary:#fff; --primary-container:#d3e3fd; --on-primary-container:#041e49;
      --secondary-container:#c2e7ff; --on-secondary-container:#001d35;
      --surface:#fff; --surface-low:#f8fafd; --surface-c:#f0f4f9; --surface-high:#e9eef6;
      --on-surface:#1f1f1f; --on-surface-variant:#444746; --outline:#747775; --outline-variant:#c4c7c5;
      --error:#b3261e; --success:#146c2e;
      --fg:var(--on-surface); --muted:var(--on-surface-variant); --line:var(--outline-variant); --accent:var(--primary);
      --ok:var(--success); --bad:var(--error); --chip:var(--surface-c); --bg:var(--surface);
      --elev3: 0 1px 3px rgba(0,0,0,.3), 0 4px 8px 3px rgba(0,0,0,.15); }
    @media (prefers-color-scheme: dark) { :host(.m3) { --primary:#a8c7fa; --on-primary:#062e6f; --primary-container:#0842a0; --on-primary-container:#d3e3fd;
      --secondary-container:#004a77; --on-secondary-container:#c2e7ff;
      --surface:#1f1f1f; --surface-low:#1b1b1b; --surface-c:#282a2c; --surface-high:#333537;
      --on-surface:#e3e3e3; --on-surface-variant:#c4c7c5; --outline:#8e918f; --outline-variant:#444746;
      --error:#f2b8b5; --success:#6dd58c; } }
    :host(.m3) * { font-family: "Google Sans Text", "Google Sans", Roboto, "Segoe UI", system-ui, sans-serif; }
    :host(.m3) .panel { width: 372px; padding: 20px; border-radius: 28px; border: none; background: var(--surface-low);
      color: var(--on-surface); box-shadow: var(--elev3); font-size: 14px; }
    :host(.m3) header b { font-family: "Google Sans", Roboto, sans-serif; font-size: 22px; font-weight: 400; letter-spacing: 0; }
    :host(.m3) button { position: relative; overflow: hidden; border-radius: 20px; height: 40px; padding: 0 16px; border: 1px solid var(--outline);
      background: transparent; color: var(--primary); font-weight: 500; font-size: 14px; letter-spacing: .1px; }
    :host(.m3) button::after { content: ""; position: absolute; inset: 0; background: currentColor; opacity: 0; transition: opacity .15s; pointer-events: none; }
    :host(.m3) button:hover:not(:disabled)::after { opacity: .08; }
    :host(.m3) button:active:not(:disabled)::after { opacity: .12; }
    :host(.m3) button.primary { background: var(--primary); color: var(--on-primary); border: none; padding: 0 20px; }
    :host(.m3) button.primary:hover:not(:disabled) { box-shadow: 0 1px 2px rgba(0,0,0,.3), 0 1px 3px 1px rgba(0,0,0,.15); }
    :host(.m3) button:disabled { opacity: .38; }
    :host(.m3) button.icon { border: none; width: 40px; height: 40px; padding: 0; border-radius: 50%; color: var(--on-surface-variant); font-size: 18px; }
    :host(.m3) label { text-transform: none; letter-spacing: .1px; font-size: 12px; font-weight: 500; color: var(--on-surface-variant); margin: 12px 0 4px; }
    :host(.m3) input, :host(.m3) textarea { border-radius: 4px; border: 1px solid var(--outline); background: transparent; color: var(--on-surface);
      padding: 10px 12px; font-size: 14px; }
    :host(.m3) input:hover, :host(.m3) textarea:hover { border-color: var(--on-surface); }
    :host(.m3) input:focus, :host(.m3) textarea:focus { outline: none; border: 2px solid var(--primary); padding: 9px 11px; }
    :host(.m3) .seg { border: 1px solid var(--outline); border-radius: 20px; height: 40px; }
    :host(.m3) .seg button { border: none; border-radius: 0; height: 38px; padding: 0 14px; color: var(--on-surface); }
    :host(.m3) .seg button + button { border-left: 1px solid var(--outline); }
    :host(.m3) .seg button[aria-pressed="true"] { background: var(--secondary-container); color: var(--on-secondary-container); }
    :host(.m3) .seg button[aria-pressed="true"]::before { content: "✓ "; }
    :host(.m3) .check { font-size: 14px; color: var(--on-surface); font-weight: 400; }
    :host(.m3) .check input { accent-color: var(--primary); width: 18px; height: 18px; }
    :host(.m3) h3 { text-transform: none; letter-spacing: .1px; font-size: 14px; font-weight: 500; color: var(--on-surface); margin-top: 20px; }
    :host(.m3) .task { border-top: none; margin-top: 8px; padding: 12px 16px; border-radius: 12px; background: var(--surface-high); }
    :host(.m3) .chip { border-radius: 8px; height: 40px; padding: 0 4px 0 12px; background: var(--surface); border: 1px solid var(--outline-variant); }
    :host(.m3) .chip button { height: 32px; padding: 0 12px; border-radius: 16px; border: none; }
    :host(.m3) .meta button { height: 32px; border-radius: 8px; padding: 0 12px; border-color: var(--outline); }
    :host(.m3) pre, :host(.m3) .notice { border-radius: 12px; background: var(--surface-c); }
    :host(.m3) .more { margin-top: 12px; }
    :host(.m3) .spin { border-color: var(--secondary-container); border-top-color: var(--primary); }
    :host(.m3) .pill { border-radius: 16px; height: 56px; padding: 0 20px; background: var(--primary-container); color: var(--on-primary-container);
      box-shadow: var(--elev3); font: 500 14px "Google Sans", Roboto, sans-serif; }

    /* ---------- Liquid Glass (Safari) ---------- */
    :host(.glass) { --fg:#1d1d1f; --muted:rgba(60,60,67,.72); --line:rgba(255,255,255,.55); --accent:#0a84ff; --ok:#248a3d; --bad:#d70015;
      --glass:rgba(250,251,255,.62); --glass-strong:rgba(255,255,255,.78); --glass-soft:rgba(118,118,128,.12);
      --fill:rgba(118,118,128,.12); --fill-hover:rgba(118,118,128,.2); --tile:rgba(255,255,255,.55); --tile-shadow:0 1px 3px rgba(0,0,0,.08);
      --rim:rgba(255,255,255,.75); --rim-low:rgba(255,255,255,.18); --shadow:0 24px 60px rgba(0,0,0,.22), 0 2px 8px rgba(0,0,0,.08); }
    @media (prefers-color-scheme: dark) { :host(.glass) { --fg:#f5f5f7; --muted:rgba(235,235,245,.66); --line:rgba(255,255,255,.16); --accent:#0a84ff; --ok:#30d158; --bad:#ff453a;
      --glass:rgba(36,36,40,.55); --glass-strong:rgba(72,72,78,.7); --glass-soft:rgba(118,118,128,.24);
      --fill:rgba(118,118,128,.24); --fill-hover:rgba(118,118,128,.34); --tile:rgba(255,255,255,.07); --tile-shadow:0 1px 3px rgba(0,0,0,.3);
      --rim:rgba(255,255,255,.28); --rim-low:rgba(255,255,255,.06); --shadow:0 24px 60px rgba(0,0,0,.5), 0 2px 8px rgba(0,0,0,.3); } }
    :host(.glass) * { font-family: -apple-system, "SF Pro Text", BlinkMacSystemFont, sans-serif; }
    :host(.glass) .panel { position: relative; width: 372px; padding: 16px; border-radius: 28px; color: var(--fg);
      background: var(--glass); border: 1px solid var(--rim);
      -webkit-backdrop-filter: blur(30px) saturate(190%); backdrop-filter: blur(30px) saturate(190%);
      box-shadow: inset 0 1px 0 var(--rim), inset 0 -1px 0 var(--rim-low), var(--shadow); }
    :host(.glass) .panel::before { content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
      background: linear-gradient(140deg, rgba(255,255,255,.42) 0%, rgba(255,255,255,0) 38%, rgba(255,255,255,0) 72%, rgba(255,255,255,.14) 100%); }
    :host(.glass) .panel > * { position: relative; }
    :host(.glass) header b { font-size: 15px; font-weight: 600; letter-spacing: -.01em; }
    :host(.glass) button { border-radius: 999px; border: none; background: var(--fill); color: var(--accent); font-weight: 500;
      -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); box-shadow: inset 0 1px 0 var(--rim-low);
      transition: transform .12s ease, background .2s ease, box-shadow .2s ease; }
    :host(.glass) button:hover:not(:disabled) { background: var(--fill-hover); }
    :host(.glass) button:active:not(:disabled) { transform: scale(.97); }
    :host(.glass) button.primary { color: #fff; border-color: rgba(255,255,255,.35); padding: 10px 12px;
      background: linear-gradient(180deg, rgba(64,156,255,.95), rgba(10,132,255,.92));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.45), 0 6px 16px rgba(10,132,255,.32); }
    :host(.glass) button.primary:hover:not(:disabled) { background: linear-gradient(180deg, rgba(84,168,255,.98), rgba(10,132,255,.96)); }
    :host(.glass) button.icon { border: none; background: var(--fill); box-shadow: none; width: 28px; height: 28px; padding: 0;
      color: var(--muted); font-size: 14px; }
    :host(.glass) button.icon:hover { background: var(--fill-hover); }
    :host(.glass) input, :host(.glass) textarea { border-radius: 12px; border: 1px solid transparent; background: var(--fill);
      color: var(--fg); padding: 8px 12px; }
    :host(.glass) input:focus, :host(.glass) textarea:focus { outline: none; border-color: rgba(10,132,255,.6); box-shadow: 0 0 0 4px rgba(10,132,255,.18); }
    :host(.glass) label, :host(.glass) h3 { text-transform: none; letter-spacing: 0; font-size: 12px; font-weight: 600; }
    :host(.glass) .task { border-top: none; margin-top: 10px; padding: 12px; border-radius: 20px; background: var(--tile);
      border: 1px solid var(--rim); box-shadow: inset 0 1px 0 var(--rim), var(--tile-shadow); }
    :host(.glass) .chip { border-radius: 999px; padding: 4px 4px 4px 12px; background: var(--fill); border: none; box-shadow: none; }
    :host(.glass) .chip button { background: var(--glass-strong); box-shadow: var(--tile-shadow); }
    :host(.glass) .chip button { padding: 4px 10px; }
    :host(.glass) pre, :host(.glass) .notice { border-radius: 14px; background: var(--glass-soft); }
    :host(.glass) .pill { border-radius: 999px; padding: 10px 16px; color: var(--fg); background: var(--glass); border: 1px solid var(--rim);
      -webkit-backdrop-filter: blur(24px) saturate(190%); backdrop-filter: blur(24px) saturate(190%);
      box-shadow: inset 0 1px 0 var(--rim), var(--shadow); font-family: -apple-system, sans-serif; }
  </style>
  <button class="pill" id="pill" title="Open Just Fill it!">Just Fill it! <span id="pillstatus"></span></button>
  <div class="panel">
    <header><b>Just Fill it!</b>
      <button class="icon" id="opts" title="Settings">⚙</button>
      <button class="icon" id="min" title="Minimize (stays on this tab)">–</button>
      <button class="icon" id="close" title="Close (stops reopening on this tab)">×</button>
    </header>
    <div id="notice"></div>
    <label for="title">Job title</label><input id="title">
    <label for="company">Company</label><input id="company">
    <div class="meta"><span id="captured"></span><button id="recapture" title="Select the job description on the page first, or just re-read the page">Re-read page</button></div>
    <label for="notes">Your instructions for Claude (optional)</label>
    <textarea id="notes" placeholder="e.g. lead with design systems, keep the letter under 80 words, mention I can start in two weeks"></textarea>
    <div class="opts">
      <span class="seg" role="group" aria-label="Model">
        <button id="fast" aria-pressed="true" title="Sonnet: quicker and lighter on usage">Fast</button>
        <button id="best" aria-pressed="false" title="Opus: strongest writing, slower">Best</button>
      </span>
      <label class="check" title="Cover letters only: look up the company's recent work (a little slower)"><input type="checkbox" id="research" checked> Research company</label>
    </div>
    <div class="row"><button class="primary" id="resume">Create resume</button><button class="primary" id="letter">Create cover letter</button></div>
    <h3>Recent resumes and cover letters</h3>
    <div id="tasks"></div>
  </div>`;
  host.classList.add(GLASS ? "glass" : "m3");
  document.documentElement.appendChild(host);
  const $ = (id) => root.getElementById(id);

  // job fields
  let job = extractJob();
  function showJob() {
    $("title").value = job.title;
    $("company").value = job.company;
    const words = job.text.split(/\s+/).filter(Boolean).length;
    $("captured").textContent = `${words.toLocaleString()} words from ${job.source}`;
  }
  showJob();
  $("recapture").onclick = () => {
    job = extractJob();
    showJob();
  };
  // Single-page sites (LinkedIn) change the job without a page load: re-read when the URL changes.
  let lastHref = location.href;
  setInterval(() => {
    if (location.href === lastHref) return;
    lastHref = location.href;
    setTimeout(() => {
      job = extractJob();
      showJob();
    }, 1200);
  }, 1000);

  // instructions draft
  store.get({ notesDraft: "" }).then((v) => ($("notes").value = v.notesDraft || ""));
  $("notes").addEventListener("input", () => store.set({ notesDraft: $("notes").value }));

  // model + research choices (remembered)
  let model = "fast";
  function setModel(next) {
    model = next;
    $("fast").setAttribute("aria-pressed", String(model === "fast"));
    $("best").setAttribute("aria-pressed", String(model === "best"));
    store.set({ model });
  }
  $("fast").onclick = () => setModel("fast");
  $("best").onclick = () => setModel("best");
  $("research").onchange = () => store.set({ research: $("research").checked });
  store.get({ model: "fast", research: true }).then((v) => {
    setModel(v.model === "best" ? "best" : "fast");
    $("research").checked = v.research !== false;
  });

  // minimize / close
  function setMin(min) {
    host.classList.toggle("min", min);
    store.set({ minimized: min });
  }
  store.get({ minimized: false }).then((v) => host.classList.toggle("min", Boolean(v.minimized)));
  $("min").onclick = () => setMin(true);
  $("pill").onclick = () => setMin(false);
  $("close").onclick = () => toggle(false);
  $("opts").onclick = () => send({ type: "openOptions" });

  function notice(text) {
    $("notice").innerHTML = text ? `<div class="notice err"></div>` : "";
    if (text) $("notice").firstChild.textContent = text;
  }

  async function checkServer() {
    try {
      await send({ type: "ping" });
      notice("");
    } catch (err) {
      notice(err.message);
    }
  }

  async function generate(kind) {
    const payload = {
      kind,
      url: location.href,
      title: $("title").value.trim(),
      company: $("company").value.trim(),
      location: job.location || "",
      text: job.text,
      notes: $("notes").value.trim(),
      model,
      research: $("research").checked,
    };
    if (!payload.title || !payload.company) return notice("Fill in the job title and company first.");
    if (payload.text.length < 200) return notice("Couldn't find the job description. Select it on the page, then click Re-read page.");
    for (const b of [$("resume"), $("letter")]) b.disabled = true;
    try {
      renderRuns(await send({ type: "generate", job: payload }));
      notice("");
      schedulePoll();
    } catch (err) {
      notice(err.message);
    } finally {
      for (const b of [$("resume"), $("letter")]) b.disabled = false;
    }
  }
  $("resume").onclick = () => generate("resume");
  $("letter").onclick = () => generate("cover_letter");

  // ---------- run history ----------
  const label = { resume: "Resume", cover_letter: "Cover letter" };
  const isBusy = (r) => r.status === "queued" || r.status === "running";
  let showAll = false;

  function ago(ms) {
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(ms).toLocaleDateString();
  }

  function renderRuns(runs) {
    if (!Array.isArray(runs)) {
      // An older background script (extension not reloaded since an update) answers differently.
      notice("The extension was updated: open chrome://extensions and click reload on Job Hunter Apply, then reload this page.");
      runs = [];
    }
    const box = $("tasks");
    box.innerHTML = "";
    const newest = [...runs].reverse();
    const busyCount = newest.filter(isBusy).length;
    $("pillstatus").textContent = busyCount ? `· ${busyCount} running` : "";
    if (!newest.length) {
      box.innerHTML = `<div class="empty">Nothing yet. Your last 10 runs will stay here across pages and restarts.</div>`;
      return;
    }
    const visible = showAll ? newest : newest.slice(0, SHOWN_RUNS);
    for (const run of visible) box.appendChild(runRow(run));
    if (newest.length > SHOWN_RUNS) {
      const more = document.createElement("button");
      more.className = "more";
      more.textContent = showAll ? "Show fewer" : `Show ${newest.length - SHOWN_RUNS} more`;
      more.onclick = () => {
        showAll = !showAll;
        renderRuns(runs);
      };
      box.appendChild(more);
    }
  }

  function runRow(run) {
    const el = document.createElement("div");
    el.className = "task";
    const busy = isBusy(run);
    el.innerHTML = `<div class="top">${busy ? '<div class="spin"></div>' : ""}<b></b><span class="status"></span><button class="icon" title="Remove from history">×</button></div><div class="sub"></div>`;
    el.querySelector("b").textContent = (label[run.kind] || run.kind) + (run.model === "best" ? " · Best" : run.model === "fast" ? " · Fast" : "");
    const status = el.querySelector(".status");
    status.className = `status ${run.status}`;
    if (busy) {
      const secs = run.elapsed_seconds || 0;
      status.textContent = `${run.status} · ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    } else {
      status.textContent = `${run.status === "done" ? "ready" : run.status} · ${ago(run.finishedAt || run.startedAt)}`;
    }
    const sub = el.querySelector(".sub");
    const where = [run.company, run.title].filter(Boolean).join(" · ");
    if (run.pageUrl) {
      const a = Object.assign(document.createElement("a"), { href: run.pageUrl, target: "_blank", rel: "noopener", textContent: where || run.pageUrl });
      a.title = "Open the job page";
      sub.appendChild(a);
    } else {
      sub.textContent = where;
    }
    el.querySelector("button").onclick = async () => renderRuns(await send({ type: "dismiss", id: run.id }));
    for (const file of run.files || []) el.appendChild(fileChip(run, file));
    if (run.files?.some((f) => f.name.endsWith(".pdf")))
      el.insertAdjacentHTML("beforeend", `<div class="hint">Drag a file onto the upload area, or click Attach.</div>`);
    if (run.error) {
      const err = document.createElement("div");
      err.className = "err";
      err.textContent = run.error;
      el.appendChild(err);
    }
    if (run.report) {
      const d = document.createElement("details");
      d.innerHTML = "<summary>Claude's report</summary><pre></pre>";
      d.querySelector("pre").textContent = run.report;
      el.appendChild(d);
    }
    return el;
  }

  function fileChip(run, file) {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.draggable = true;
    chip.title = file.path;
    chip.innerHTML = `<span></span><button>Attach</button><button>Save</button>`;
    chip.querySelector("span").textContent = `📄 ${file.name}`;
    const [attach, save] = chip.querySelectorAll("button");
    // Fetch on hover so dragstart (which must be synchronous) already has the File.
    chip.addEventListener("mouseenter", () => getFile(file).catch((err) => notice(err.message)), { once: true });
    chip.addEventListener("dragstart", (event) => {
      const f = fileCache.get(file.path);
      if (!f) {
        event.preventDefault();
        getFile(file).catch((err) => notice(err.message));
        return;
      }
      dragging = { file: f, kind: run.kind };
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.items.add(f);
    });
    chip.addEventListener("dragend", () => setTimeout(() => (dragging = null), 0));
    attach.onclick = async () => {
      const input = pickInput(run.kind);
      if (!input) return notice("No file upload field on this page yet. Open the application form, then click Attach (or drag the file).");
      try {
        attachTo(input, await getFile(file));
        attach.textContent = "Attached ✓";
      } catch (err) {
        notice(err.message);
      }
    };
    save.onclick = async () => {
      try {
        const url = URL.createObjectURL(await getFile(file));
        const a = Object.assign(document.createElement("a"), { href: url, download: file.name });
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
      } catch (err) {
        notice(err.message);
      }
    };
    return chip;
  }

  let timer = null;
  function schedulePoll() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 3000);
  }
  async function refresh() {
    try {
      const runs = await send({ type: "runs" });
      renderRuns(runs);
      if (runs.some(isBusy)) schedulePoll();
    } catch (err) {
      notice(err.message);
    }
  }
  // Another tab finished or started a run: redraw from storage.
  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === "local" && changes.runs) {
        renderRuns(changes.runs.newValue || []);
        if ((changes.runs.newValue || []).some(isBusy)) schedulePoll();
      }
    });
  } catch {}

  function toggle(show) {
    const visible = host.style.display !== "none";
    const next = show === undefined ? !visible : show;
    host.style.display = next ? "" : "none";
    send({ type: next ? "pin" : "unpin" }).catch(() => {});
    if (next) {
      checkServer();
      refresh();
    }
  }

  window.__jobHunterPanel = { toggle };
  checkServer();
  refresh();
})();
