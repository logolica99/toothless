const BASE = document.querySelector('meta[name="toothless-base-path"]').content;
document.getElementById("setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("setup-error"); error.textContent = "";
  const form = event.currentTarget;
  const code = document.getElementById("setup-code").value;
  const username = document.getElementById("setup-username").value;
  const password = document.getElementById("setup-password").value;
  try {
    const response = await fetch(`${BASE}/app/api/auth/bootstrap`, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, username, password }),
    });
    document.getElementById("setup-code").value = "";
    document.getElementById("setup-password").value = "";
    if (!response.ok) throw new Error((await response.text()).slice(0, 240) || "Owner setup authorization was rejected.");
    form.reset(); location.assign(`${BASE}/app/`);
  } catch (cause) { error.textContent = cause.message; }
});
