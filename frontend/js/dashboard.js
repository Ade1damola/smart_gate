(function () {
  requireLogin();

  const staffNameEl = document.getElementById("staff-name");
  const plateEl = document.getElementById("plate-number");
  const otpForm = document.getElementById("otp-form");
  const timeLimitSelect = document.getElementById("time-limit");
  const customMinutesInput = document.getElementById("custom-minutes");
  const otpResult = document.getElementById("otp-result");
  const otpCodeEl = document.getElementById("otp-code");
  const otpExpiryEl = document.getElementById("otp-expiry");
  const otpErrorEl = document.getElementById("otp-error");
  const activeOtpList = document.getElementById("active-otp-list");
  const recentActivityList = document.getElementById("recent-activity-list");
  const otpSubmitBtn = otpForm.querySelector("button[type=submit]");

  document.getElementById("logout-btn").addEventListener("click", logout);

  timeLimitSelect.addEventListener("change", function () {
    customMinutesInput.hidden = timeLimitSelect.value !== "custom";
  });

  function renderActiveOtps(otps) {
    activeOtpList.innerHTML = "";
    if (!otps || otps.length === 0) {
      activeOtpList.innerHTML = '<li class="empty-message">No active OTPs</li>';
      return;
    }
    otps.forEach(function (otp) {
      const li = document.createElement("li");
      li.className = "otp-list-item";

      const info = document.createElement("div");
      info.innerHTML =
        '<span class="otp-list-code">' + otp.otp_code + "</span>" +
        '<span class="subtitle">Expires ' + formatDateTime(otp.expiry_time) + "</span>";

      const revokeBtn = document.createElement("button");
      revokeBtn.className = "btn btn-danger";
      revokeBtn.textContent = "Revoke";
      revokeBtn.addEventListener("click", function () {
        revokeOtp(otp.otp_code);
      });

      li.appendChild(info);
      li.appendChild(revokeBtn);
      activeOtpList.appendChild(li);
    });
  }

  function renderRecentActivity(entries) {
    recentActivityList.innerHTML = "";
    if (!entries || entries.length === 0) {
      recentActivityList.innerHTML = '<li class="empty-message">No activity yet</li>';
      return;
    }
    entries.forEach(function (entry) {
      const li = document.createElement("li");
      li.className = "activity-list-item";
      li.innerHTML =
        "<span>" + formatDateTime(entry.timestamp) + "</span>" +
        '<span class="badge badge-' + entry.event_type + '">' + entry.event_type + "</span>" +
        "<span>" + entry.method + "</span>" +
        '<span class="badge badge-' + entry.status + '">' + entry.status + "</span>";
      recentActivityList.appendChild(li);
    });
  }

  async function loadDashboard() {
    const { ok, status, data } = await apiFetch("/api/dashboard");
    if (status === 401) {
      logout();
      return;
    }
    if (!ok || !data.success) {
      staffNameEl.textContent = "Could not load dashboard";
      return;
    }
    staffNameEl.textContent = data.name;
    plateEl.textContent = "Plate: " + data.plate_number;
    renderActiveOtps(data.active_otps);
    renderRecentActivity(data.recent_activity);
  }

  async function revokeOtp(code) {
    const { ok, data } = await apiFetch("/api/revoke_otp", {
      method: "POST",
      body: JSON.stringify({ otp_code: code }),
    });
    if (ok && data.success) {
      loadDashboard();
    } else {
      alert(data.message || "Could not revoke OTP");
    }
  }

  otpForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    otpErrorEl.hidden = true;
    otpResult.hidden = true;

    let minutes = timeLimitSelect.value;
    if (minutes === "custom") {
      minutes = customMinutesInput.value;
    }
    minutes = parseInt(minutes, 10);
    if (!minutes || minutes <= 0) {
      otpErrorEl.textContent = "Enter a valid number of minutes.";
      otpErrorEl.hidden = false;
      return;
    }

    otpSubmitBtn.disabled = true;
    otpSubmitBtn.textContent = "Generating...";

    const { ok, data } = await apiFetch("/api/generate_otp", {
      method: "POST",
      body: JSON.stringify({ time_limit: minutes }),
    });

    if (ok && data.success) {
      otpCodeEl.textContent = data.otp_code;
      otpExpiryEl.textContent = "Expires " + formatDateTime(data.expiry_time);
      otpResult.hidden = false;
      loadDashboard();
    } else {
      otpErrorEl.textContent = data.message || "Could not generate OTP";
      otpErrorEl.hidden = false;
    }

    otpSubmitBtn.disabled = false;
    otpSubmitBtn.textContent = "Generate OTP";
  });

  loadDashboard();
})();
