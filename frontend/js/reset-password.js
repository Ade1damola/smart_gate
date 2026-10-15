(function () {
  const resetForm = document.getElementById("reset-form");
  const resetErrorEl = document.getElementById("reset-error");
  const resetSuccessEl = document.getElementById("reset-success");
  const resetSubmitBtn = resetForm.querySelector("button[type=submit]");

  const token = new URLSearchParams(window.location.search).get("token") || "";
  // Drop the token from the address bar so it isn't left in history or
  // leaked through the Referer header.
  window.history.replaceState(null, "", window.location.pathname);

  if (!token) {
    resetForm.hidden = true;
    document.getElementById("missing-token").hidden = false;
    return;
  }

  resetForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    resetErrorEl.hidden = true;

    const new_password = document.getElementById("new_password").value;
    const confirm_password = document.getElementById("confirm_password").value;
    if (new_password !== confirm_password) {
      resetErrorEl.textContent = "The passwords don't match.";
      resetErrorEl.hidden = false;
      return;
    }

    resetSubmitBtn.disabled = true;
    resetSubmitBtn.textContent = "Resetting...";

    const { ok, data } = await apiFetch("/api/reset_password", {
      method: "POST",
      body: JSON.stringify({ token, new_password }),
    });

    if (ok && data.success) {
      resetForm.hidden = true;
      resetSuccessEl.textContent = data.message + " Redirecting to login...";
      resetSuccessEl.hidden = false;
      setTimeout(function () {
        window.location.href = "login.html";
      }, 1500);
      return;
    }

    resetErrorEl.innerHTML = "";
    resetErrorEl.append(
      (data.message || "Could not reset password.") + " ",
    );
    const link = document.createElement("a");
    link.href = "forgot-password.html";
    link.textContent = "Request a new link.";
    resetErrorEl.append(link);
    resetErrorEl.hidden = false;
    resetSubmitBtn.disabled = false;
    resetSubmitBtn.textContent = "Reset Password";
  });
})();
