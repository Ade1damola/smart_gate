(function () {
  requireLogin();

  const plateEl = document.getElementById("plate-number");
  const logBody = document.getElementById("log-body");

  document.getElementById("logout-btn").addEventListener("click", logout);

  function splitDateTime(isoString) {
    const date = new Date(isoString);
    if (isNaN(date.getTime())) {
      return { date: isoString, time: "" };
    }
    return {
      date: date.toLocaleDateString([], { dateStyle: "medium" }),
      time: date.toLocaleTimeString([], { timeStyle: "short" }),
    };
  }

  function renderLog(entries) {
    logBody.innerHTML = "";
    if (!entries || entries.length === 0) {
      logBody.innerHTML = '<tr><td colspan="5" class="empty-message">No activity recorded yet</td></tr>';
      return;
    }
    entries.forEach(function (entry) {
      const { date, time } = splitDateTime(entry.timestamp);
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + date + "</td>" +
        "<td>" + time + "</td>" +
        '<td><span class="badge badge-' + entry.event_type + '">' + (entry.event_type === "entry" ? "In" : "Out") + "</span></td>" +
        "<td>" + (entry.method === "otp" ? "OTP" : "Fingerprint") + "</td>" +
        '<td><span class="badge badge-' + entry.status + '">' + entry.status + "</span></td>";
      logBody.appendChild(tr);
    });
  }

  async function loadLog() {
    const { ok, status, data } = await apiFetch("/api/activity_log");
    if (status === 401) {
      logout();
      return;
    }
    if (!ok || !data.success) {
      logBody.innerHTML = '<tr><td colspan="5" class="empty-message">Could not load activity log</td></tr>';
      return;
    }
    plateEl.textContent = "Plate: " + data.plate_number;
    renderLog(data.log);
  }

  loadLog();
  showAdminTabIfAdmin();
})();
