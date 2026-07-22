// Shared helpers for talking to the Flask backend and managing the session
// token. Pages are served from the same origin as the API (the Flask server
// today, the ESP32's web server later), so plain relative paths work.

const TOKEN_KEY = "staff_token";

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

async function apiFetch(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  const token = getToken();
  if (token) {
    headers["Authorization"] = "Bearer " + token;
  }
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  let response;
  try {
    response = await fetch(path, Object.assign({}, options, { headers }));
  } catch (err) {
    return { ok: false, status: 0, data: { success: false, message: "Could not reach the server." } };
  }

  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, data };
}

function requireLogin() {
  if (!getToken()) {
    window.location.href = "login.html";
  }
}

function logout() {
  clearToken();
  window.location.href = "login.html";
}

async function showAdminTabIfAdmin() {
  const adminTab = document.getElementById("admin-nav-tab");
  if (!adminTab) return;
  const { ok, data } = await apiFetch("/api/dashboard");
  if (ok && data.success && data.is_admin) {
    adminTab.hidden = false;
  }
}

function formatDateTime(isoString) {
  if (!isoString) return "--";
  const date = new Date(isoString);
  if (isNaN(date.getTime())) return isoString;
  return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
