// Shared helpers for talking to the Flask backend and managing the session
// token. Pages are served from the same origin as the API (the Flask server
// today, the ESP32's web server later), so plain relative paths work.
//
// Admin pages mark themselves with <body data-area="admin">. Admin and staff
// sessions use separate tokens and separate login pages, so neither side's
// pages ever lead to the other's login.

const IS_ADMIN_AREA = document.body.dataset.area === "admin";
const TOKEN_KEY = IS_ADMIN_AREA ? "admin_token" : "staff_token";
const LOGIN_PAGE = IS_ADMIN_AREA ? "admin-login.html" : "login.html";

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
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
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

// For <img src>, which can't send an Authorization header.
function authUrl(path) {
  return path + (path.indexOf("?") === -1 ? "?" : "&") + "token=" + encodeURIComponent(getToken() || "");
}

function requireLogin() {
  if (!getToken()) {
    window.location.href = LOGIN_PAGE;
  }
}

function logout() {
  apiFetch("/api/logout", { method: "POST" });
  clearToken();
  window.location.href = LOGIN_PAGE;
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDateTime(isoString) {
  if (!isoString) return "--";
  const date = new Date(isoString);
  if (isNaN(date.getTime())) return isoString;
  return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function formatTime(isoString) {
  if (!isoString) return "--";
  const date = new Date(isoString);
  if (isNaN(date.getTime())) return isoString;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
