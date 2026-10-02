(function () {
  requireLogin();

  const LIVE_POLL_MS = 1500;
  const CAMERA_POLL_MS = 1000;
  // Once a vehicle's verification has finished, keep it on screen this long
  // before the banner goes back to "Waiting for vehicle".
  const LINGER_SECONDS = 45;

  const el = function (id) { return document.getElementById(id); };
  let renderedKey = null;
  let newestEventId = null;

  // --- clock / full screen ------------------------------------------------

  function tickClock() {
    el("clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  setInterval(tickClock, 1000);
  tickClock();

  el("fullscreen-btn").addEventListener("click", function () {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(function () {});
  });

  // --- camera ---------------------------------------------------------------
  // Load each frame off-screen and swap it in when ready, so the picture
  // never flashes blank between frames.

  function loadFrame() {
    const frame = new Image();
    frame.onload = function () {
      el("camera-img").src = frame.src;
      el("camera-img").hidden = false;
      el("camera-placeholder").hidden = true;
      setTimeout(loadFrame, CAMERA_POLL_MS);
    };
    frame.onerror = function () { setTimeout(loadFrame, CAMERA_POLL_MS * 3); };
    frame.src = authUrl("/api/admin/camera/latest.jpg?t=" + Date.now());
  }

  function showDevices(devices) {
    [["chip-camera", devices.camera], ["chip-gate", devices.gate]].forEach(function (pair) {
      el(pair[0]).querySelector(".dot").className = "dot " + (pair[1].online ? "dot-ok" : "dot-bad");
    });
    const age = devices.camera.frame_seconds_ago;
    const live = age !== null && age < 10;
    el("live-dot").classList.toggle("stale", !live);
    el("camera-label").textContent = age === null ? "NO FEED" : live ? "LIVE" : "FEED STALLED (" + age + "s)";
  }

  // --- current vehicle ------------------------------------------------------

  function setBanner(tone, label, headline, sub) {
    el("state").className = "m-state" + (tone ? " state-" + tone : "");
    el("state-label").textContent = label;
    el("state-headline").textContent = headline;
    el("state-sub").textContent = sub || "";
  }

  function picture(src, alt) {
    return src ? '<img alt="' + escapeHtml(alt) + '" src="' + src + '" />' : '<div class="m-noimg">No photo</div>';
  }

  function sessionHtml(session, lingering) {
    const det = session.detection;
    const owner = session.owner;
    const parts = [];

    if (lingering) {
      parts.push('<div style="color: var(--m-muted); font-size: 0.8rem; letter-spacing: 0.08em;">LAST VEHICLE</div>');
    }
    parts.push(
      '<div class="m-plate-row"><span class="m-plate">' + escapeHtml(det.plate_number) + "</span>" +
      '<span class="m-direction">' + escapeHtml(directionLabel(det.event_type)) + "</span>" +
      '<span class="pill" style="margin-left: auto;">' + formatTime(det.timestamp) + "</span></div>"
    );

    if (owner) {
      const vehicle = owner.vehicles[0];
      parts.push(
        '<div class="m-owner">' +
        picture(owner.has_photo ? authUrl("/api/admin/users/" + encodeURIComponent(owner.staff_id) + "/photo") : null, owner.name) +
        '<div><div class="name">' + escapeHtml(owner.name) + "</div>" +
        '<div class="meta">' + escapeHtml(owner.category_label) + " &middot; ID " + escapeHtml(owner.staff_id) + "</div>" +
        '<div class="meta">' + escapeHtml(owner.department) + "</div></div></div>"
      );
      if (vehicle) {
        parts.push(
          '<div class="m-vehicle-facts"><strong>' + escapeHtml([vehicle.colour, vehicle.make, vehicle.model].filter(Boolean).join(" ")) + "</strong>" +
          (vehicle.features ? "<div>" + escapeHtml(vehicle.features) + "</div>" : "") + "</div>"
        );
      }
      parts.push(
        '<div class="m-compare">' +
        "<figure>" + picture(det.has_snapshot ? authUrl("/api/admin/events/" + det.id + "/snapshot") : null, "Camera frame") +
        "<figcaption>At the gate now</figcaption></figure>" +
        "<figure>" + picture(vehicle && vehicle.has_photo ? authUrl("/api/admin/vehicles/" + encodeURIComponent(vehicle.vehicle_id) + "/photo") : null, "Registered vehicle") +
        "<figcaption>Registered vehicle</figcaption></figure></div>"
      );
    } else if (det.has_snapshot) {
      parts.push(
        '<div class="m-compare"><figure>' + picture(authUrl("/api/admin/events/" + det.id + "/snapshot"), "Camera frame") +
        "<figcaption>Camera frame</figcaption></figure></div>"
      );
    }

    parts.push(
      '<ul class="m-steps">' + session.steps.map(function (step) {
        return '<li class="' + describeGateEvent(step).step + '"><time>' + formatTime(step.timestamp) + "</time>" + escapeHtml(step.message) + "</li>";
      }).join("") + "</ul>"
    );
    return parts.join("");
  }

  function showSession(session, serverTime) {
    if (!session) {
      setBanner("", "Gate status", "Waiting for vehicle", "Camera is watching for plates");
      el("session-body").innerHTML = '<p style="color: var(--m-muted);">No vehicles seen yet.</p>';
      return;
    }

    const state = gateSessionState(session);
    const lastStep = session.steps[session.steps.length - 1];
    const idleFor = (new Date(serverTime) - new Date(lastStep.timestamp)) / 1000;
    const lingering = state.done && idleFor > LINGER_SECONDS;

    if (lingering) setBanner("", "Gate status", "Waiting for vehicle", "Camera is watching for plates");
    else setBanner(state.tone, "Current vehicle", state.headline, state.sub);
    el("session").classList.toggle("dim", lingering);

    // Rebuild the panel (and reload its photos) only when something changed.
    const key = lastStep.id + ":" + lingering;
    if (key !== renderedKey) {
      renderedKey = key;
      el("session-body").innerHTML = sessionHtml(session, lingering);
    }
  }

  // --- stats + feed ---------------------------------------------------------

  function showStats(stats) {
    const tiles = [
      ["Entries", stats.entries], ["Exits", stats.exits], ["Denied", stats.denied],
      ["Vehicles seen", stats.detections], ["Visitors", stats.visitors], ["Registered", stats.users],
    ];
    el("stats").innerHTML = tiles.map(function (tile) {
      return '<div class="m-stat"><div class="v">' + tile[1] + '</div><div class="l">' + tile[0] + "</div></div>";
    }).join("");
  }

  function showFeed(events) {
    if (!events.length) {
      el("feed").innerHTML = '<p style="color: var(--m-muted);">No gate activity yet today.</p>';
      return;
    }
    const previousNewest = newestEventId;
    newestEventId = events[0].id;
    el("feed").innerHTML = events.map(function (event) {
      const isNew = previousNewest !== null && event.id > previousNewest;
      const who = event.owner_name ? escapeHtml(event.owner_name) + " &middot; " : "";
      return '<div class="m-feed-item' + (isNew ? " fresh" : "") + '">' +
        "<time>" + formatTime(event.timestamp) + "</time>" +
        '<span class="p">' + escapeHtml(event.plate_number || "--") + "</span>" +
        '<span class="msg">' + who + escapeHtml(event.message) + "</span>" +
        gateEventPill(event) + "</div>";
    }).join("");
  }

  // --- polling --------------------------------------------------------------

  async function refresh() {
    const { ok, status, data } = await apiFetch("/api/admin/live");
    if (status === 401 || status === 403) {
      logout();
      return;
    }
    if (ok && data.success) {
      el("connection").textContent = "Connected · updated " + formatTime(data.server_time);
      showDevices(data.devices);
      showSession(data.session, data.server_time);
      showStats(data.stats);
      showFeed(data.events);
    } else {
      el("connection").textContent = "Server unreachable - retrying...";
    }
    setTimeout(refresh, LIVE_POLL_MS);
  }

  refresh();
  loadFrame();
})();
