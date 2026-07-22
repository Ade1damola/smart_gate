(function () {
  if (getToken()) {
    window.location.href = "dashboard.html";
    return;
  }

  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("login-error");
  const submitBtn = form.querySelector("button[type=submit]");

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    errorBox.hidden = true;

    const staff_id = document.getElementById("staff_id").value.trim();
    const password = document.getElementById("password").value;

    submitBtn.disabled = true;
    submitBtn.textContent = "Logging in...";

    const { ok, data } = await apiFetch("/api/login", {
      method: "POST",
      body: JSON.stringify({ staff_id, password }),
    });

    if (ok && data.success) {
      setToken(data.token);
      window.location.href = "dashboard.html";
      return;
    }

    errorBox.textContent = data.message || "Login failed. Check your staff ID and password.";
    errorBox.hidden = false;
    submitBtn.disabled = false;
    submitBtn.textContent = "Log In";
  });
})();
