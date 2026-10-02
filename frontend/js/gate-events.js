// How gate events (see GateEvent in backend/models.py) are labelled on the
// admin dashboard and the live monitor.

function describeGateEvent(event) {
  const ok = event.status === "success";
  switch (event.kind) {
    case "detection":
      return event.status === "registered"
        ? { tone: "info", label: "Registered", step: "wait" }
        : { tone: "accent", label: "Visitor", step: "ok" };
    case "awaiting_fingerprint":
      return { tone: "warn", label: "Scan finger", step: "wait" };
    case "fingerprint":
      return ok ? { tone: "ok", label: "Owner verified", step: "ok" } : { tone: "warn", label: "Not owner", step: "wait" };
    case "otp":
      return ok ? { tone: "ok", label: "OTP granted", step: "ok" } : { tone: "bad", label: "OTP denied", step: "bad" };
    case "timeout":
      return { tone: "bad", label: "Timed out", step: "bad" };
    default:
      return ok ? { tone: "ok", label: "Note", step: "ok" } : { tone: "bad", label: "Alert", step: "bad" };
  }
}

function gateEventPill(event) {
  const info = describeGateEvent(event);
  return '<span class="pill pill-' + info.tone + '">' + escapeHtml(info.label) + "</span>";
}

// Overall state of a gate session (one vehicle), from its latest step.
function gateSessionState(session) {
  const steps = session.steps || [];
  const last = steps[steps.length - 1] || session.detection;
  const ok = last.status === "success";
  switch (last.kind) {
    case "detection":
      return last.status === "registered"
        ? { tone: "wait", headline: "Registered vehicle", sub: "Alerting the gate for verification", done: false }
        : { tone: "visitor", headline: "Visitor - gate opened", sub: "Plate not registered on the platform", done: true };
    case "awaiting_fingerprint":
      return { tone: "wait", headline: "Waiting for fingerprint", sub: last.message, done: false };
    case "fingerprint":
      return ok
        ? { tone: "ok", headline: "Access granted", sub: "Owner verified by fingerprint", done: true }
        : { tone: "wait", headline: "Not the owner - OTP required", sub: "Driver must enter the owner's OTP", done: false };
    case "otp":
      return ok
        ? { tone: "ok", headline: "Access granted", sub: "Driver verified by OTP", done: true }
        : { tone: "bad", headline: "OTP rejected", sub: last.message, done: false };
    case "timeout":
      return { tone: "bad", headline: "Timed out - gate closed", sub: "No successful verification", done: true };
    default:
      return { tone: ok ? "ok" : "bad", headline: ok ? "Update" : "Alert", sub: last.message, done: true };
  }
}

function directionLabel(eventType) {
  if (eventType === "entry") return "Entry";
  if (eventType === "exit") return "Exit";
  return "";
}
