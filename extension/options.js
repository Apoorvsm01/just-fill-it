document.body.classList.add(chrome.runtime.getURL("").startsWith("safari-web-extension://") ? "glass" : "m3");

const $ = (id) => document.getElementById(id);

chrome.storage.local.get({ port: 8767, token: "" }, (v) => {
  $("port").value = v.port;
  $("token").value = v.token;
});

$("save").onclick = async () => {
  const port = Number($("port").value) || 8767;
  const token = $("token").value.trim();
  await chrome.storage.local.set({ port, token });
  const status = $("status");
  status.className = "";
  status.textContent = "Testing…";
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/ping`, { headers: { Authorization: `Bearer ${token}` } });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      status.className = "ok";
      status.textContent = "Connected. Open a job page and click the extension (or press Alt+Shift+J).";
    } else {
      status.className = "bad";
      status.textContent = data.error || `HTTP ${res.status}`;
    }
  } catch {
    status.className = "bad";
    status.textContent = "Can't reach the helper. Is it running? (uv run just-fill-it)";
  }
};
