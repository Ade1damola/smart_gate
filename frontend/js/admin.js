(function () {
  requireLogin();

  const FINGER_LABELS = ["Right thumb", "Right index", "Left thumb", "Left index", "Right middle", "Left middle"];

  let categories = {};
  let stopPolling = null;

  document.getElementById("logout-btn").addEventListener("click", logout);

  // ---------------------------------------------------------------------
  // Session + routing
  // ---------------------------------------------------------------------

  async function guardAdmin() {
    const { ok, data } = await apiFetch("/api/admin/me");
    if (!ok || !data.success) {
      logout();
      return false;
    }
    document.getElementById("admin-name").textContent = data.name;
    return true;
  }

  function showView(name) {
    document.querySelectorAll("[data-view]").forEach(function (section) {
      section.hidden = section.dataset.view !== name;
    });
  }

  function setActiveNav(name) {
    document.querySelectorAll("[data-nav]").forEach(function (link) {
      link.classList.toggle("active", link.dataset.nav === name);
    });
  }

  function route() {
    if (stopPolling) {
      stopPolling();
      stopPolling = null;
    }
    const hash = window.location.hash.replace(/^#/, "") || "overview";
    const [name, param] = hash.split("/");
    const id = param ? decodeURIComponent(param) : "";
    window.scrollTo(0, 0);

    if (name === "users") {
      setActiveNav("users"); showView("users"); loadUsers();
    } else if (name === "user" && id) {
      setActiveNav("users"); showView("user"); loadUserDetail(id);
    } else if (name === "register") {
      setActiveNav("register"); showView("register"); openForm(null);
    } else if (name === "edit" && id) {
      setActiveNav("users"); showView("register"); openForm(id);
    } else if (name === "logs") {
      setActiveNav("logs"); showView("logs"); loadLogs();
    } else if (name === "detections") {
      setActiveNav("detections"); showView("detections"); loadDetections();
    } else if (name === "guide") {
      setActiveNav("guide"); showView("guide");
    } else {
      setActiveNav("overview"); showView("overview"); stopPolling = startOverview();
    }
  }

  function poll(fn, ms) {
    let timer = null;
    let stopped = false;
    async function tick() {
      await fn();
      if (!stopped) timer = setTimeout(tick, ms);
    }
    tick();
    return function () {
      stopped = true;
      clearTimeout(timer);
    };
  }

  // ---------------------------------------------------------------------
  // Shared rendering helpers
  // ---------------------------------------------------------------------

  function initials(name) {
    return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map(function (part) { return part[0]; }).join("").toUpperCase();
  }

  function avatarHtml(user) {
    if (user.has_photo) {
      return '<img class="avatar" alt="" src="' + authUrl("/api/admin/users/" + encodeURIComponent(user.staff_id) + "/photo") + '" />';
    }
    return '<span class="avatar">' + escapeHtml(initials(user.name)) + "</span>";
  }

  function vehicleSummary(vehicle) {
    if (!vehicle) return "";
    return [vehicle.colour, vehicle.make, vehicle.model].filter(Boolean).join(" ");
  }

  function statusPill(status) {
    return status === "success"
      ? '<span class="pill pill-ok">Granted</span>'
      : '<span class="pill pill-bad">Denied</span>';
  }

  function methodLabel(method) {
    const value = (method || "").toLowerCase();
    if (value === "otp") return "OTP";
    if (value === "fingerprint") return "Fingerprint";
    if (value === "none") return "None";
    return method || "--";
  }

  function ageText(seconds) {
    if (seconds == null) return "never";
    if (seconds < 60) return seconds + "s ago";
    if (seconds < 3600) return Math.floor(seconds / 60) + " min ago";
    return Math.floor(seconds / 3600) + " h ago";
  }

  // ---------------------------------------------------------------------
  // Overview
  // ---------------------------------------------------------------------

  function startOverview() {
    const stopStats = poll(loadOverview, 5000);
    const stopCamera = poll(refreshOverviewCamera, 2000);
    return function () {
      stopStats();
      stopCamera();
    };
  }

  async function loadOverview() {
    const { ok, status, data } = await apiFetch("/api/admin/stats");
    if (status === 403) return logout();
    if (!ok || !data.success) return;

    const stats = data.stats;
    const tiles = [
      ["Registered users", stats.users],
      ["Vehicles", stats.vehicles],
      ["Entries today", stats.entries],
      ["Exits today", stats.exits],
      ["Denied today", stats.denied],
      ["Vehicles seen today", stats.detections],
      ["Visitors today", stats.visitors],
    ];
    document.getElementById("stat-grid").innerHTML = tiles.map(function (tile) {
      return '<div class="stat"><div class="stat-label">' + tile[0] + '</div><div class="stat-value">' + tile[1] + "</div></div>";
    }).join("");

    const devices = data.devices;
    document.getElementById("device-list").innerHTML =
      deviceRow("Gate controller (ESP32)", devices.gate) +
      deviceRow("ANPR camera", devices.camera);
    document.getElementById("camera-age").textContent =
      devices.camera.frame_seconds_ago == null ? "No feed" : "Frame " + ageText(devices.camera.frame_seconds_ago);

    const list = document.getElementById("overview-events");
    if (!data.recent_events.length) {
      list.innerHTML = '<li class="empty">No gate activity recorded yet</li>';
      return;
    }
    list.innerHTML = data.recent_events.map(eventItemHtml).join("");
  }

  function deviceRow(label, device) {
    return "<li><span>" + label + '</span><span><span class="dot ' + (device.online ? "dot-ok" : "dot-bad") + '"></span>' +
      (device.online ? "Online" : "Offline") + ' <span class="muted" style="display:inline">&middot; ' + ageText(device.seconds_ago) + "</span></span></li>";
  }

  function eventItemHtml(event) {
    const who = event.owner_name ? escapeHtml(event.owner_name) + " &middot; " : "";
    return '<li class="event-item">' +
      '<span class="event-time">' + formatTime(event.timestamp) + "</span>" +
      "<div><span class=\"event-plate\">" + escapeHtml(event.plate_number || "--") + "</span> " +
      '<span class="muted" style="display:inline">' + escapeHtml(directionLabel(event.event_type)) + "</span>" +
      '<div class="event-message">' + who + escapeHtml(event.message) + "</div></div>" +
      gateEventPill(event) + "</li>";
  }

  function refreshOverviewCamera() {
    return new Promise(function (resolve) {
      const holder = document.getElementById("overview-camera");
      const img = new Image();
      img.onload = function () {
        holder.innerHTML = "";
        holder.appendChild(img);
        resolve();
      };
      img.onerror = function () {
        holder.textContent = "No camera feed yet";
        resolve();
      };
      img.alt = "Gate camera";
      img.src = authUrl("/api/admin/camera/latest.jpg?t=" + Date.now());
    });
  }

  // ---------------------------------------------------------------------
  // Users list
  // ---------------------------------------------------------------------

  const userSearch = document.getElementById("user-search");
  const userCategory = document.getElementById("user-category");
  let searchTimer = null;
  userSearch.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadUsers, 250);
  });
  userCategory.addEventListener("change", loadUsers);

  function fillCategorySelects() {
    const options = Object.keys(categories).map(function (key) {
      return '<option value="' + key + '">' + escapeHtml(categories[key]) + "</option>";
    }).join("");
    const current = userCategory.value;
    userCategory.innerHTML = '<option value="">All categories</option>' + options;
    userCategory.value = current;
    document.getElementById("f-category").innerHTML = '<option value="">Choose...</option>' + options;
  }

  async function loadUsers() {
    const rows = document.getElementById("user-rows");
    const { ok, status, data } = await apiFetch("/api/admin/users?q=" + encodeURIComponent(userSearch.value.trim()));
    if (status === 403) return logout();
    if (!ok || !data.success) {
      rows.innerHTML = '<tr><td colspan="5" class="empty">Could not load users</td></tr>';
      return;
    }
    if (Object.keys(categories).length === 0) {
      categories = data.categories;
      fillCategorySelects();
    }

    const users = data.users.filter(function (user) {
      return !userCategory.value || user.category === userCategory.value;
    });
    if (!users.length) {
      rows.innerHTML = '<tr><td colspan="5" class="empty">No users found</td></tr>';
      return;
    }
    rows.innerHTML = users.map(function (user) {
      const vehicle = user.vehicles[0];
      return '<tr class="clickable" data-id="' + escapeHtml(user.staff_id) + '">' +
        '<td><div class="user-cell">' + avatarHtml(user) + "<div><strong>" + escapeHtml(user.name) + "</strong><small>" + escapeHtml(user.staff_id) + "</small></div></div></td>" +
        "<td>" + escapeHtml(user.category_label) + "</td>" +
        "<td>" + escapeHtml(user.department || "--") + "</td>" +
        "<td>" + (vehicle ? '<span class="plate">' + escapeHtml(vehicle.plate_number) + "</span><br /><small class=\"muted\">" + escapeHtml(vehicleSummary(vehicle)) + "</small>" : "--") + "</td>" +
        "<td>" + (user.fingerprint_count
          ? '<span class="pill pill-ok">' + user.fingerprint_count + " enrolled</span>"
          : '<span class="pill pill-warn">None</span>') + "</td></tr>";
    }).join("");
    rows.querySelectorAll("tr[data-id]").forEach(function (row) {
      row.addEventListener("click", function () {
        window.location.hash = "user/" + encodeURIComponent(row.dataset.id);
      });
    });
  }

  // ---------------------------------------------------------------------
  // User detail
  // ---------------------------------------------------------------------

  async function loadUserDetail(staffId) {
    const container = document.getElementById("user-detail");
    container.innerHTML = '<p class="empty">Loading...</p>';
    const { ok, status, data } = await apiFetch("/api/admin/users/" + encodeURIComponent(staffId));
    if (status === 403) return logout();
    if (!ok || !data.success) {
      container.innerHTML = '<p class="empty">' + escapeHtml(data.message || "Could not load this user") + "</p>";
      return;
    }
    const user = data.user;
    const vehicle = user.vehicles[0];
    const photoBust = "?v=" + Date.now();

    container.innerHTML =
      '<div class="panel"><div class="profile-head">' +
        (user.has_photo
          ? '<img class="profile-photo" alt="Passport photo" src="' + authUrl("/api/admin/users/" + encodeURIComponent(user.staff_id) + "/photo" + photoBust) + '" />'
          : '<div class="profile-photo"></div>') +
        "<div><h1>" + escapeHtml(user.name) + "</h1>" +
        '<span class="pill pill-accent">' + escapeHtml(user.category_label) + "</span> " +
        '<span class="pill">ID ' + escapeHtml(user.staff_id) + "</span>" +
        '<p class="muted" style="margin-top: 8px;">' + escapeHtml(user.department) + "</p></div>" +
        '<div class="profile-actions">' +
          '<a class="button" href="#edit/' + encodeURIComponent(user.staff_id) + '">Edit details</a>' +
          '<button class="button button-danger" id="delete-user" type="button">Remove user</button>' +
        "</div>" +
      "</div></div>" +

      '<div class="detail-grid">' +
        '<div class="panel"><h2 style="margin-bottom: 12px;">Contact</h2><dl class="facts">' +
          "<dt>Email</dt><dd>" + escapeHtml(user.email || "--") + "</dd>" +
          "<dt>Telephone</dt><dd>" + escapeHtml(user.phone_number || "--") + "</dd>" +
          "<dt>Registered</dt><dd>" + escapeHtml(user.created_time ? formatDateTime(user.created_time) : "--") + "</dd>" +
        "</dl></div>" +

        '<div class="panel"><h2 style="margin-bottom: 12px;">Vehicle</h2>' +
          (vehicle
            ? (vehicle.has_photo ? '<img class="vehicle-photo" alt="Vehicle" src="' + authUrl("/api/admin/vehicles/" + encodeURIComponent(vehicle.vehicle_id) + "/photo" + photoBust) + '" />' : "") +
              '<dl class="facts">' +
              '<dt>Plate</dt><dd><span class="plate">' + escapeHtml(vehicle.plate_number) + "</span></dd>" +
              "<dt>Brand</dt><dd>" + escapeHtml(vehicle.make || "--") + "</dd>" +
              "<dt>Model</dt><dd>" + escapeHtml(vehicle.model || "--") + "</dd>" +
              "<dt>Colour</dt><dd>" + escapeHtml(vehicle.colour || "--") + "</dd>" +
              "<dt>Features</dt><dd>" + escapeHtml(vehicle.features || "--") + "</dd></dl>"
            : '<p class="empty">No vehicle registered</p>') +
        "</div>" +

        '<div class="panel"><h2>Fingerprints</h2>' +
          '<p class="muted">Stored on the server and checked by the gate when this vehicle arrives.</p>' +
          '<ul class="fp-list" id="detail-fp-list"></ul>' +
          '<div id="detail-fp-enroll" style="margin-top: 14px;"></div>' +
        "</div>" +

        '<div class="panel"><h2 style="margin-bottom: 8px;">Recent gate activity</h2>' +
          '<div class="table-wrap"><table class="data-table"><tbody>' +
          (data.recent_activity.length
            ? data.recent_activity.map(function (log) {
              return "<tr><td>" + formatDateTime(log.timestamp) + "</td><td>" + escapeHtml(directionLabel(log.event_type)) +
                "</td><td>" + methodLabel(log.method) + "</td><td>" + statusPill(log.status) + "</td></tr>";
            }).join("")
            : '<tr><td class="empty">No activity yet</td></tr>') +
          "</tbody></table></div></div>" +
      "</div>";

    renderDetailFingerprints(user);

    createFingerprintEnroller(document.getElementById("detail-fp-enroll"), async function (label, template) {
      const result = await apiFetch("/api/admin/users/" + encodeURIComponent(user.staff_id) + "/fingerprints", {
        method: "POST",
        body: JSON.stringify({ label: label, template: template }),
      });
      if (!result.ok || !result.data.success) {
        throw new Error(result.data.message || "Could not save the fingerprint.");
      }
      user.fingerprints.push(result.data.fingerprint);
      renderDetailFingerprints(user);
    });

    document.getElementById("delete-user").addEventListener("click", async function () {
      if (!confirm("Remove " + user.name + " and their vehicle and fingerprints from the platform? Their access history is kept.")) return;
      const result = await apiFetch("/api/admin/users/" + encodeURIComponent(user.staff_id), { method: "DELETE" });
      if (result.ok && result.data.success) {
        window.location.hash = "users";
      } else {
        alert(result.data.message || "Could not remove this user.");
      }
    });
  }

  function renderDetailFingerprints(user) {
    const list = document.getElementById("detail-fp-list");
    let html = user.fingerprints.map(function (fp) {
      return '<li><span><strong>' + escapeHtml(fp.label || "Finger") + '</strong> <span class="muted" style="display:inline">&middot; enrolled ' +
        formatDateTime(fp.created_time) + '</span></span><button class="button button-small button-danger" data-fp="' + fp.id + '" type="button">Remove</button></li>';
    }).join("");
    if (user.legacy_fingerprint_ids.length) {
      html += '<li><span><strong>Gate sensor slots ' + escapeHtml(user.legacy_fingerprint_ids.join(", ")) +
        '</strong> <span class="muted" style="display:inline">&middot; enrolled the old way, on the gate itself</span></span>' +
        '<button class="button button-small button-danger" data-legacy="1" type="button">Unlink</button></li>';
    }
    list.innerHTML = html || '<li><span class="muted">No fingerprints enrolled. The driver will need an OTP at the gate.</span></li>';

    list.querySelectorAll("[data-fp]").forEach(function (button) {
      button.addEventListener("click", async function () {
        if (!confirm("Remove this fingerprint?")) return;
        const result = await apiFetch("/api/admin/users/" + encodeURIComponent(user.staff_id) + "/fingerprints/" + button.dataset.fp, { method: "DELETE" });
        if (result.ok && result.data.success) {
          user.fingerprints = user.fingerprints.filter(function (fp) { return String(fp.id) !== button.dataset.fp; });
          renderDetailFingerprints(user);
        }
      });
    });
    list.querySelectorAll("[data-legacy]").forEach(function (button) {
      button.addEventListener("click", async function () {
        if (!confirm("Stop accepting this user's fingerprints stored on the gate sensor?")) return;
        const result = await apiFetch("/api/admin/users/" + encodeURIComponent(user.staff_id) + "/legacy_fingerprints", { method: "DELETE" });
        if (result.ok && result.data.success) {
          user.legacy_fingerprint_ids = [];
          renderDetailFingerprints(user);
        }
      });
    });
  }

  // ---------------------------------------------------------------------
  // Fingerprint enrollment widget (Web Serial sensor on this computer)
  // ---------------------------------------------------------------------

  function createFingerprintEnroller(container, onCaptured) {
    if (!FingerprintSensor.isSupported()) {
      container.innerHTML = '<div class="fp-box"><p class="muted">Fingerprint enrollment needs Chrome or Edge on a computer, with the sensor plugged in over USB.</p></div>';
      return;
    }

    container.innerHTML =
      '<div class="fp-box">' +
        '<div class="fp-controls">' +
          '<button type="button" class="button" data-connect></button>' +
          '<select data-label>' + FINGER_LABELS.map(function (label) { return "<option>" + label + "</option>"; }).join("") + "</select>" +
          '<button type="button" class="button button-primary" data-scan>Scan finger</button>' +
          '<button type="button" class="button" data-cancel hidden>Cancel</button>' +
        "</div>" +
        '<div class="fp-status" data-status hidden></div>' +
      "</div>";

    const connectBtn = container.querySelector("[data-connect]");
    const labelSelect = container.querySelector("[data-label]");
    const scanBtn = container.querySelector("[data-scan]");
    const cancelBtn = container.querySelector("[data-cancel]");
    const statusEl = container.querySelector("[data-status]");
    let cancelled = false;
    let busy = false;

    function setStatus(text, kind) {
      statusEl.hidden = !text;
      statusEl.className = "fp-status" + (kind ? " " + kind : "");
      statusEl.innerHTML = (kind === "busy" ? '<span class="spinner"></span>' : "") + "<span>" + escapeHtml(text) + "</span>";
    }

    function refresh() {
      const connected = FingerprintSensor.isConnected();
      connectBtn.textContent = connected ? "Disconnect sensor" : "Connect sensor";
      connectBtn.disabled = busy;
      scanBtn.disabled = busy || !connected;
      labelSelect.disabled = busy;
      cancelBtn.hidden = !busy;
    }

    connectBtn.addEventListener("click", async function () {
      try {
        if (FingerprintSensor.isConnected()) {
          await FingerprintSensor.disconnect();
          setStatus("");
        } else {
          setStatus("Connecting...", "busy");
          await FingerprintSensor.connect();
          setStatus("Sensor connected. Choose a finger and press Scan.", "done");
        }
      } catch (err) {
        if (err && err.name === "NotFoundError") setStatus("");
        else setStatus(err.message || "Could not connect to the sensor.", "error");
      }
      refresh();
    });

    cancelBtn.addEventListener("click", function () { cancelled = true; });

    scanBtn.addEventListener("click", async function () {
      busy = true;
      cancelled = false;
      refresh();
      const label = labelSelect.value;
      try {
        const template = await FingerprintSensor.enroll(
          function (message) { setStatus(message, "busy"); },
          function () { return cancelled; }
        );
        setStatus("Saving...", "busy");
        await onCaptured(label, template);
        setStatus(label + " captured. Scan another finger as a backup.", "done");
        // Move on to the next finger so a second scan is one click away.
        labelSelect.selectedIndex = (labelSelect.selectedIndex + 1) % FINGER_LABELS.length;
      } catch (err) {
        setStatus(err.message || "Enrollment failed.", "error");
      }
      busy = false;
      refresh();
    });

    refresh();
  }

  // ---------------------------------------------------------------------
  // Register / edit form
  // ---------------------------------------------------------------------

  const form = document.getElementById("user-form");
  const formError = document.getElementById("form-error");
  const formSubmit = document.getElementById("form-submit");
  const registerResult = document.getElementById("register-result");
  const passportInput = document.getElementById("f-passport_photo");
  const vehicleInput = document.getElementById("f-vehicle_photo");
  const FIELD_NAMES = ["staff_id", "name", "category", "department", "email", "phone_number",
    "plate_number", "vehicle_make", "vehicle_model", "vehicle_colour", "vehicle_features"];

  let editingId = null;
  let pendingFingerprints = [];

  function setPreview(previewId, src, fallback) {
    const preview = document.getElementById(previewId);
    if (src) {
      preview.innerHTML = '<img alt="" src="' + src + '" />';
      preview.classList.add("has-image");
    } else {
      preview.textContent = fallback;
      preview.classList.remove("has-image");
    }
  }

  passportInput.addEventListener("change", function () {
    const file = passportInput.files[0];
    setPreview("passport-preview", file ? URL.createObjectURL(file) : null, "Passport photo");
  });
  vehicleInput.addEventListener("change", function () {
    const file = vehicleInput.files[0];
    setPreview("vehicle-preview", file ? URL.createObjectURL(file) : null, "Vehicle photo");
  });

  function renderPendingFingerprints() {
    const container = document.getElementById("form-fingerprint");
    let list = container.querySelector(".fp-list");
    if (!list) {
      list = document.createElement("ul");
      list.className = "fp-list";
      container.appendChild(list);
    }
    list.innerHTML = pendingFingerprints.map(function (fp, index) {
      return "<li><span><strong>" + escapeHtml(fp.label) + '</strong> <span class="muted" style="display:inline">&middot; captured, saved when you submit</span></span>' +
        '<button type="button" class="button button-small button-danger" data-remove="' + index + '">Remove</button></li>';
    }).join("");
    list.querySelectorAll("[data-remove]").forEach(function (button) {
      button.addEventListener("click", function () {
        pendingFingerprints.splice(Number(button.dataset.remove), 1);
        renderPendingFingerprints();
      });
    });
  }

  async function openForm(staffId) {
    editingId = staffId;
    pendingFingerprints = [];
    form.reset();
    formError.hidden = true;
    registerResult.hidden = !registerResult.dataset.keep;
    delete registerResult.dataset.keep;
    setPreview("passport-preview", null, "Passport photo");
    setPreview("vehicle-preview", null, "Vehicle photo");

    if (Object.keys(categories).length === 0) {
      const { data } = await apiFetch("/api/admin/users?q=__none__");
      if (data && data.categories) {
        categories = data.categories;
        fillCategorySelects();
      }
    }

    const idInput = document.getElementById("f-staff_id");
    document.getElementById("fingerprint-section").hidden = Boolean(staffId);
    if (!staffId) {
      document.getElementById("form-title").textContent = "Register user";
      document.getElementById("form-subtitle").textContent =
        "Anyone with a vehicle who is permanently on campus: staff, residents, shop owners.";
      formSubmit.textContent = "Register user";
      document.getElementById("form-cancel").href = "#users";
      idInput.disabled = false;
      createFingerprintEnroller(document.getElementById("form-fingerprint"), async function (label, template) {
        pendingFingerprints.push({ label: label, template: template });
        renderPendingFingerprints();
      });
      return;
    }

    document.getElementById("form-title").textContent = "Edit user";
    document.getElementById("form-subtitle").textContent = "Leave a photo empty to keep the current one.";
    formSubmit.textContent = "Save changes";
    document.getElementById("form-cancel").href = "#user/" + encodeURIComponent(staffId);
    idInput.disabled = true;

    const { ok, data } = await apiFetch("/api/admin/users/" + encodeURIComponent(staffId));
    if (!ok || !data.success) {
      formError.textContent = data.message || "Could not load this user.";
      formError.hidden = false;
      return;
    }
    const user = data.user;
    const vehicle = user.vehicles[0] || {};
    const values = {
      staff_id: user.staff_id, name: user.name, category: user.category, department: user.department,
      email: user.email, phone_number: user.phone_number, plate_number: vehicle.plate_number || user.plate_number,
      vehicle_make: vehicle.make, vehicle_model: vehicle.model, vehicle_colour: vehicle.colour, vehicle_features: vehicle.features,
    };
    FIELD_NAMES.forEach(function (name) {
      document.getElementById("f-" + name).value = values[name] || "";
    });
    if (user.has_photo) {
      setPreview("passport-preview", authUrl("/api/admin/users/" + encodeURIComponent(user.staff_id) + "/photo?v=" + Date.now()), "");
    }
    if (vehicle.has_photo) {
      setPreview("vehicle-preview", authUrl("/api/admin/vehicles/" + encodeURIComponent(vehicle.vehicle_id) + "/photo?v=" + Date.now()), "");
    }
  }

  // Shrink photos in the browser before upload: phone cameras produce
  // multi-megabyte images, and these are stored in the database.
  function compressImage(file, maxDimension, quality) {
    return new Promise(function (resolve, reject) {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = function () {
        const scale = Math.min(1, maxDimension / Math.max(img.width, img.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        canvas.toBlob(function (blob) {
          if (blob) resolve(blob);
          else reject(new Error("Could not process the image."));
        }, "image/jpeg", quality);
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error("That file isn't an image this browser can read."));
      };
      img.src = url;
    });
  }

  function showFormError(message) {
    formError.textContent = message;
    formError.hidden = false;
    formError.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    formError.hidden = true;

    const missing = [];
    FIELD_NAMES.forEach(function (name) {
      const input = document.getElementById("f-" + name);
      if (input.required && !input.disabled && !input.value.trim()) {
        missing.push(document.querySelector('label[for="f-' + name + '"]').firstChild.textContent.trim());
      }
    });
    if (!editingId && !passportInput.files[0]) missing.push("Passport photograph");
    if (!editingId && !vehicleInput.files[0]) missing.push("Vehicle snapshot");
    if (missing.length) {
      showFormError("Please fill in: " + missing.join(", "));
      return;
    }

    formSubmit.disabled = true;
    formSubmit.textContent = "Saving...";

    try {
      const body = new FormData();
      FIELD_NAMES.forEach(function (name) {
        body.append(name, document.getElementById("f-" + name).value.trim());
      });
      if (passportInput.files[0]) body.append("passport_photo", await compressImage(passportInput.files[0], 800, 0.85), "passport.jpg");
      if (vehicleInput.files[0]) body.append("vehicle_photo", await compressImage(vehicleInput.files[0], 1280, 0.82), "vehicle.jpg");
      body.append("fingerprints", JSON.stringify(pendingFingerprints));

      const path = editingId ? "/api/admin/users/" + encodeURIComponent(editingId) : "/api/admin/users";
      const { ok, data } = await apiFetch(path, { method: "POST", body: body });
      if (!ok || !data.success) {
        showFormError(data.message || "Could not save this user.");
        return;
      }

      if (editingId) {
        window.location.hash = "user/" + encodeURIComponent(editingId);
        return;
      }
      registerResult.innerHTML =
        '<p class="form-success" style="margin: 0 0 10px;">' + escapeHtml(data.user.name) + " is registered" +
        (pendingFingerprints.length ? " with " + pendingFingerprints.length + " fingerprint(s)" : "") + ".</p>" +
        "<p class=\"muted\">Their temporary password for the staff portal:</p>" +
        '<span class="code-display">' + escapeHtml(data.default_password) + "</span>" +
        '<p class="muted">They should log in with their ID number and use "Forgot password?" to set their own.</p>' +
        '<p style="margin-top: 10px;"><a href="#user/' + encodeURIComponent(data.staff_id) + '">View profile &rarr;</a></p>';
      registerResult.dataset.keep = "1";
      openForm(null);
      window.scrollTo(0, 0);
    } catch (err) {
      showFormError(err.message || "Could not save this user.");
    } finally {
      formSubmit.disabled = false;
      formSubmit.textContent = editingId ? "Save changes" : "Register user";
    }
  });

  // ---------------------------------------------------------------------
  // Access log
  // ---------------------------------------------------------------------

  const logFilters = document.getElementById("log-filters");
  let logTimer = null;
  logFilters.addEventListener("input", function () {
    clearTimeout(logTimer);
    logTimer = setTimeout(loadLogs, 300);
  });
  logFilters.addEventListener("submit", function (event) { event.preventDefault(); });

  async function loadLogs() {
    const rows = document.getElementById("log-rows");
    const params = new URLSearchParams(new FormData(logFilters));
    const { ok, status, data } = await apiFetch("/api/admin/logs?" + params.toString());
    if (status === 403) return logout();
    if (!ok || !data.success) {
      rows.innerHTML = '<tr><td colspan="7" class="empty">Could not load the access log</td></tr>';
      return;
    }
    if (!data.log.length) {
      rows.innerHTML = '<tr><td colspan="7" class="empty">No matching entries</td></tr>';
      return;
    }
    rows.innerHTML = data.log.map(function (log) {
      const user = log.owner_name
        ? '<a href="#user/' + encodeURIComponent(log.staff_id) + '">' + escapeHtml(log.owner_name) + "</a><br /><small class=\"muted\">" + escapeHtml(log.staff_id) + "</small>"
        : escapeHtml(log.staff_id || "--");
      return "<tr><td>" + formatDateTime(log.timestamp) + "</td><td>" + user + "</td>" +
        "<td>" + (log.plate_number ? '<span class="plate">' + escapeHtml(log.plate_number) + "</span>" : "--") + "</td>" +
        "<td>" + escapeHtml(directionLabel(log.event_type)) + "</td><td>" + methodLabel(log.method) + "</td>" +
        "<td>" + statusPill(log.status) + "</td><td class=\"muted\">" + escapeHtml(log.details) + "</td></tr>";
    }).join("");
  }

  // ---------------------------------------------------------------------
  // Detections
  // ---------------------------------------------------------------------

  const detectionSearch = document.getElementById("detection-search");
  let detectionTimer = null;
  detectionSearch.addEventListener("input", function () {
    clearTimeout(detectionTimer);
    detectionTimer = setTimeout(loadDetections, 300);
  });

  async function loadDetections() {
    const grid = document.getElementById("detection-grid");
    const { ok, status, data } = await apiFetch("/api/admin/detections?q=" + encodeURIComponent(detectionSearch.value.trim()));
    if (status === 403) return logout();
    if (!ok || !data.success) {
      grid.innerHTML = '<p class="empty">Could not load detections</p>';
      return;
    }
    if (!data.detections.length) {
      grid.innerHTML = '<p class="empty">No plates detected yet</p>';
      return;
    }
    grid.innerHTML = data.detections.map(function (event) {
      const snap = event.has_snapshot
        ? '<img loading="lazy" alt="Frame for ' + escapeHtml(event.plate_number) + '" src="' + authUrl("/api/admin/events/" + event.id + "/snapshot") + '" />'
        : "No snapshot kept";
      const who = event.owner_name
        ? '<a href="#user/' + encodeURIComponent(event.staff_id) + '">' + escapeHtml(event.owner_name) + "</a>"
        : '<span class="muted" style="display:inline">Not registered</span>';
      return '<div class="detection-card"><div class="snap">' + snap + '</div><div class="body">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;"><span class="plate">' + escapeHtml(event.plate_number) + "</span>" + gateEventPill(event) + "</div>" +
        "<div>" + who + "</div>" +
        '<div class="muted">' + formatDateTime(event.timestamp) + (event.event_type ? " &middot; " + escapeHtml(directionLabel(event.event_type)) : "") + "</div>" +
        "</div></div>";
    }).join("");
  }

  // ---------------------------------------------------------------------
  // Testing guide (temporary): in-page section links. The hash is already
  // used for routing, so these scroll instead of changing it.
  // ---------------------------------------------------------------------

  document.querySelectorAll("[data-jump]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      event.preventDefault();
      document.getElementById(link.dataset.jump).scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  // ---------------------------------------------------------------------

  guardAdmin().then(function (ok) {
    if (!ok) return;
    window.addEventListener("hashchange", route);
    route();
  });
})();
