const $ = (id) => document.getElementById(id);
const BASE = document.querySelector('meta[name="toothless-base-path"]').content;
let conversationId = "";
let selectedProjectId = "";
let selectedProjectName = "";

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, { credentials: "same-origin", cache: "no-store", ...options });
  if (response.status === 401) {
    $("console").classList.add("hidden"); $("login").classList.remove("hidden");
    throw new Error("Session expired; request a fresh /web code.");
  }
  if (!response.ok) throw new Error((await response.text()).slice(0, 300) || `Request failed (${response.status})`);
  return response.json();
}

function csrfOptions(method, body) {
  const token = document.cookie.split("; ").find((part) => part.startsWith("toothless_web_csrf="))?.split("=").slice(1).join("=") || "";
  return { method, headers: { "Content-Type": "application/json", "X-Toothless-CSRF": decodeURIComponent(token) }, body: JSON.stringify(body) };
}

async function status() {
  const data = await request("/app/api/status");
  const connected = data.database === "connected";
  $("connection-pill").textContent = connected ? "● Supabase connected" : "● Storage not connected";
  $("connection-pill").classList.toggle("connected", connected);
  $("storage-gate").classList.toggle("hidden", connected);
  $("chat").classList.toggle("hidden", !connected);
  $("message").disabled = !connected;
  $("composer").querySelector("button").disabled = !connected;
  if (connected) await loadHistory();
}

async function loadHistory() {
  try {
    const data = await request("/app/api/conversations");
    const history = $("history"); history.replaceChildren();
    data.conversations.forEach((item) => {
      const button = document.createElement("button"); button.textContent = item.title;
      button.addEventListener("click", () => openConversation(item.id, item.title)); history.append(button);
    });
    if (!conversationId && data.conversations[0]) await openConversation(data.conversations[0].id, data.conversations[0].title);
  } catch (error) { $("connect-message").textContent = error.message; }
}

async function openConversation(id, title) {
  conversationId = id; $("chat-title").textContent = title;
  const { messages } = await request(`/app/api/conversations/${encodeURIComponent(id)}/messages`);
  const root = $("messages"); root.replaceChildren();
  messages.forEach((message) => {
    const item = document.createElement("article"); item.className = `message ${message.role}`;
    const label = document.createElement("small"); label.textContent = message.role === "user" ? "YOU" : message.role === "system-visible-event" ? "STATUS" : "TOOTHLESS";
    const text = document.createElement("p"); text.textContent = message.content;
    item.append(label, text); root.append(item);
  });
  root.scrollTop = root.scrollHeight;
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("login-error").textContent = "";
  try {
    const response = await fetch(`${BASE}/app/authorize`, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: $("code").value }),
    });
    if (!response.ok) throw new Error("That one-use code is invalid or expired.");
    $("login").classList.add("hidden"); $("console").classList.remove("hidden"); await status();
  } catch (error) { $("login-error").textContent = error.message; }
});

$("password-login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("password-login-error").textContent = "";
  try {
    const response = await fetch(`${BASE}/app/api/auth/login`, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("username").value, password: $("password").value }),
    });
    $("password").value = "";
    if (!response.ok) throw new Error("Username or password is incorrect.");
    $("login").classList.add("hidden"); $("console").classList.remove("hidden"); await status();
  } catch (error) { $("password-login-error").textContent = error.message; }
});

$("connect").addEventListener("click", async () => {
  const input = $("dsn"); const dsn = input.value; input.value = "";
  $("connect-message").textContent = "Verifying TLS and database schema…";
  try {
    const result = await request("/app/api/connect", csrfOptions("POST", { dsn }));
    $("connect-message").textContent = `Connected. Runtime credential stored in ${result.secret_storage}.`;
    await status();
  } catch (error) { $("connect-message").textContent = error.message; }
});

$("migrate-keychain").addEventListener("click", async () => {
  $("connect-message").textContent = "Checking for an existing legacy Keychain connection…";
  try {
    const result = await request("/app/api/migrate-keychain", csrfOptions("POST", {}));
    $("connect-message").textContent = `Connection migrated to ${result.secret_storage}. The legacy Keychain item was left unchanged.`;
    await status();
  } catch (error) { $("connect-message").textContent = error.message; }
});

$("new-chat").addEventListener("click", async () => {
  try {
    const title = selectedProjectName ? `${selectedProjectName} conversation` : "New conversation";
    const result = await request("/app/api/conversations", csrfOptions("POST", { title, project_id: selectedProjectId || null }));
    await loadHistory(); await openConversation(result.id, title);
  } catch (error) { $("connect-message").textContent = error.message; }
});

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!conversationId) return;
  const text = $("message").value.trim(); if (!text) return;
  try {
    await request(`/app/api/conversations/${encodeURIComponent(conversationId)}/messages`, csrfOptions("POST", {
      content: text, client_request_id: crypto.randomUUID(),
    }));
    $("message").value = ""; await openConversation(conversationId, $("chat-title").textContent);
  } catch (error) { $("connect-message").textContent = error.message; }
});

$("logout").addEventListener("click", async () => {
  try { await request("/app/api/auth/logout", csrfOptions("POST", {})); } finally { location.reload(); }
});

$("security-button").addEventListener("click", () => $("security-dialog").showModal());
$("close-security").addEventListener("click", () => $("security-dialog").close());
$("logout-all").addEventListener("click", async () => {
  if (!window.confirm("Log out Toothless on every device?")) return;
  try { await request("/app/api/auth/logout-all", csrfOptions("POST", {})); } finally { location.reload(); }
});
$("password-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("security-message").textContent = "";
  const current = $("current-password").value; const replacement = $("new-password").value;
  try {
    await request("/app/api/auth/password", csrfOptions("POST", { current_password: current, new_password: replacement }));
    $("password-form").reset(); $("security-message").textContent = "Password changed. Other sessions were revoked.";
  } catch (error) { $("security-message").textContent = error.message; }
});

(async () => {
  try {
    const response = await fetch(`${BASE}/app/api/auth/session`, { credentials: "same-origin", cache: "no-store" });
    if (response.ok) {
      const data = await response.json();
      $("setup-link").classList.toggle("hidden", data.administrator_configured);
      if (!data.administrator_configured && data.storage === "connected") $("setup-link").classList.remove("hidden");
      if (data.authenticated) {
      $("login").classList.add("hidden"); $("console").classList.remove("hidden"); await status();
      }
    }
  } catch { /* A missing database leaves the explicit Telegram fallback available. */ }
})();

$("projects-button").addEventListener("click", async () => {
  try {
    const { projects } = await request("/app/api/projects"); const list = $("projects-list"); list.replaceChildren();
    projects.forEach((project) => {
      const button = document.createElement("button"); button.textContent = project.name;
      button.addEventListener("click", async () => {
        selectedProjectId = project.id; selectedProjectName = project.name; $("chat-title").textContent = `${project.name} · New conversation`;
      });
      list.append(button);
    });
    list.classList.toggle("hidden"); $("new-project").classList.toggle("hidden");
  }
  catch (error) { $("connect-message").textContent = error.message; }
});

$("new-project").addEventListener("click", async () => {
  const name = window.prompt("Project name"); if (!name?.trim()) return;
  try {
    await request("/app/api/projects", csrfOptions("POST", { name: name.trim(), description: "" }));
    $("projects-list").classList.add("hidden"); $("new-project").classList.add("hidden");
  } catch (error) { $("connect-message").textContent = error.message; }
});
