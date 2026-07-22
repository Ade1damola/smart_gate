(function () {
  requireLogin();

  const staffNameEl = document.getElementById("staff-name");
  const form = document.getElementById("add-staff-form");
  const errorEl = document.getElementById("add-staff-error");
  const resultEl = document.getElementById("add-staff-result");
  const defaultPasswordEl = document.getElementById("default-password");
  const submitBtn = form.querySelector("button[type=submit]");

  document.getElementById("logout-btn").addEventListener("click", logout);

  async function guardAdmin() {
    const { ok, status, data } = await apiFetch("/api/dashboard");
    if (status === 401) {
      logout();
      return;
    }
    if (!ok || !data.success || !data.is_admin) {
      window.location.href = "dashboard.html";
      return;
    }
    staffNameEl.textContent = data.name;
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    errorEl.hidden = true;
    resultEl.hidden = true;

    const payload = {
      staff_id: document.getElementById("staff_id").value.trim(),
      name: document.getElementById("name").value.trim(),
      phone_number: document.getElementById("phone_number").value.trim(),
      plate_number: document.getElementById("plate_number").value.trim(),
      fingerprint_template_id: document.getElementById("fingerprint_template_id").value.trim(),
    };

    submitBtn.disabled = true;
    submitBtn.textContent = "Adding...";

    const { ok, data } = await apiFetch("/api/admin/add_staff", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    if (ok && data.success) {
      defaultPasswordEl.textContent = data.default_password;
      resultEl.hidden = false;
      form.reset();
    } else {
      errorEl.textContent = data.message || "Could not add staff member.";
      errorEl.hidden = false;
    }

    submitBtn.disabled = false;
    submitBtn.textContent = "Add Staff";
  });

  guardAdmin();
})();
