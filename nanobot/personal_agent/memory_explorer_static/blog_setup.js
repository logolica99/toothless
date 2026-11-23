const byId = (id) => document.getElementById(id);
let csrf = "";

async function refresh() {
  const response = await fetch("api/status", { credentials: "same-origin" });
  if (!response.ok) throw new Error("Setup session expired; request a new /blog setup code.");
  const state = await response.json();
  byId("connections").textContent = JSON.stringify(state, null, 2);
}

byId("sign-in").onclick = async () => {
  const response = await fetch("authorize", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: byId("code").value }),
  });
  byId("code").value = "";
  if (!response.ok) {
    byId("status").textContent = "Code invalid or expired. Request a fresh code in Telegram.";
    return;
  }
  const csrfCookie = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith("toothless_blog_csrf="));
  csrf = decodeURIComponent(csrfCookie?.slice("toothless_blog_csrf=".length) || "");
  byId("login").style.display = "none";
  byId("enroll").style.display = "block";
  await refresh();
};

byId("save").onclick = async () => {
  const secret = byId("secret").value;
  byId("secret").value = "";
  byId("status").textContent = "Validating…";
  const response = await fetch("api/credentials", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Toothless-CSRF": csrf },
    body: JSON.stringify({ credential: byId("kind").value, secret }),
  });
  byId("status").textContent = response.ok
    ? "Validated and stored in macOS login Keychain."
    : (await response.text()).slice(0, 240);
  await refresh().catch((error) => { byId("status").textContent = error.message; });
};

byId("remove").onclick = async () => {
  const name = byId("kind").value;
  const response = await fetch(`api/credentials/${encodeURIComponent(name)}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-Toothless-CSRF": csrf },
  });
  byId("status").textContent = response.ok
    ? "Removed from local Keychain. Revoke the credential at its provider too."
    : (await response.text()).slice(0, 240);
  await refresh().catch((error) => { byId("status").textContent = error.message; });
};
