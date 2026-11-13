const API = "http://127.0.0.1:8765/api/cookies/raw";

const $ = (id) => document.getElementById(id);

function show(text, kind) {
  const el = $("msg");
  el.textContent = text;
  el.className = "msg on " + (kind || "ok");
}

async function getTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function collectCookies() {
  const tab = await getTab();
  if (!tab || !/^https?:/i.test(tab.url || "")) {
    throw new Error("请先打开一个网站页面");
  }
  const u = new URL(tab.url);

  const merged = new Map();
  const put = (list) => {
    (list || []).forEach((c) => {
      merged.set(c.domain + "|" + c.path + "|" + c.name, c);
    });
  };

  put(await chrome.cookies.getAll({ url: tab.url }));
  const bare = u.hostname.replace(/^www\./, "");
  for (const d of new Set([u.hostname, bare, "." + bare])) {
    try {
      put(await chrome.cookies.getAll({ domain: d }));
    } catch (e) { /* 某些域会抛错，忽略 */ }
  }

  return { host: u.hostname, cookies: [...merged.values()] };
}

async function sync() {
  const btn = $("go");
  btn.disabled = true;
  btn.textContent = "同步中…";
  try {
    const { host, cookies } = await collectCookies();
    if (!cookies.length) {
      throw new Error("这个网站没有 cookie，先确认已登录");
    }
    const r = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, cookies })
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "保存失败");
    const names = (d.status?.platforms || [])
      .filter((p) => p.ok).map((p) => p.name);
    show(`已同步 ${d.count} 条 cookie` +
      (names.length ? `\n已连接：${names.join("、")}` : ""), "ok");
  } catch (e) {
    const m = String(e.message || e);
    if (m.includes("Failed to fetch")) {
      show("连不上本地程序，确认它正在运行（127.0.0.1:8765）", "err");
    } else {
      show(m, "err");
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "同步到本地提取器";
  }
}

(async () => {
  try {
    const tab = await getTab();
    const u = new URL(tab.url);
    $("host").innerHTML = "当前网站：<b>" + u.hostname + "</b>";
  } catch (e) {
    $("host").textContent = "请在网页上使用";
  }
})();

$("go").addEventListener("click", sync);
