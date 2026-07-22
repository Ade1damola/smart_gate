(function () {
  const requestForm = document.getElementById("request-form");
  const requestErrorEl = document.getElementById("request-error");
  const requestSubmitBtn = requestForm.querySelector("button[type=submit]");

  const resetForm = document.getElementById("reset-form");
  const requestSuccessEl = document.getElementById("request-success");
  const resetErrorEl = document.getElementById("reset-error");
  const resetSuccessEl = document.getElementById("reset-success");
  const resetSubmitBtn = resetForm.querySelector("button[type=submit]");

  let staffId = "";

  requestForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    requestErrorEl.hidden = true;

    staffId = document.getElementById("staff_id").value.trim();

    requestSubmitBtn.disabled = true;
    requestSubmitBtn.textContent = "Sending...";

    const { ok, data } = await apiFetch("/api/request_password_reset", {
      method: "POST",
      body: JSON.stringify({ staff_id: staffId }),
    });

    requestSubmitBtn.disabled = false;
    requestSubmitBtn.textContent = "Send Reset Code";

    if (!ok) {
      requestErrorEl.textContent = "Could not reach the server. Please try again.";
      requestErrorEl.hidden = false;
      return;
    }

    requestSuccessEl.textContent = data.message;
    requestForm.hidden = true;
    resetForm.hidden = false;
  });

  resetForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    resetErrorEl.hidden = true;
    resetSuccessEl.hidden = true;

    const otp_code = document.getElementById("otp_code").value.trim();
    const new_password = document.getElementById("new_password").value;

    resetSubmitBtn.disabled = true;
    resetSubmitBtn.textContent = "Resetting...";

    const { ok, data } = await apiFetch("/api/reset_password", {
      method: "POST",
      body: JSON.stringify({ staff_id: staffId, otp_code, new_password }),
    });

    if (ok && data.success) {
      resetSuccessEl.textContent = data.message + " Redirecting to login...";
      resetSuccessEl.hidden = false;
      setTimeout(function () {
        window.location.href = "login.html";
      }, 1500);
      return;
    }

    resetErrorEl.textContent = data.message || "Could not reset password.";
    resetErrorEl.hidden = false;
    resetSubmitBtn.disabled = false;
    resetSubmitBtn.textContent = "Reset Password";
  });
})();
