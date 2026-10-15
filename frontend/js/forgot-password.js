(function () {
  const requestForm = document.getElementById("request-form");
  const requestErrorEl = document.getElementById("request-error");
  const requestSuccessEl = document.getElementById("request-success");
  const requestSubmitBtn = requestForm.querySelector("button[type=submit]");

  requestForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    requestErrorEl.hidden = true;

    const email = document.getElementById("email").value.trim();

    requestSubmitBtn.disabled = true;
    requestSubmitBtn.textContent = "Sending...";

    const { ok, data } = await apiFetch("/api/request_password_reset", {
      method: "POST",
      body: JSON.stringify({ email }),
    });

    requestSubmitBtn.disabled = false;
    requestSubmitBtn.textContent = "Send Reset Link";

    if (!ok) {
      requestErrorEl.textContent = "Could not reach the server. Please try again.";
      requestErrorEl.hidden = false;
      return;
    }

    requestSuccessEl.textContent = data.message + " Check your inbox (and spam folder).";
    requestSuccessEl.hidden = false;
    requestForm.hidden = true;
  });
})();
