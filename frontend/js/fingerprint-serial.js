// Enrolls fingerprints from an R307/R305/AS608-family sensor plugged into
// the admin's computer through a USB-to-TTL serial adapter, using the
// browser's Web Serial API (Chrome/Edge on desktop, over HTTPS or
// localhost).
//
// Wiring: sensor TX -> adapter RX, sensor RX -> adapter TX, VCC -> 5V (or
// 3.3V for 3.3V modules), GND -> GND.
//
// The template is read out of the sensor (UpChar) and uploaded to the
// server; the gate's ESP32 later downloads it into its own sensor for a 1:1
// match, so nothing has to be enrolled on the gate hardware itself.

const FingerprintSensor = (function () {
  const HEADER = [0xef, 0x01];
  const ADDRESS = [0xff, 0xff, 0xff, 0xff];
  const PID_COMMAND = 0x01;
  const PID_DATA = 0x02;
  const PID_ACK = 0x07;
  const PID_END = 0x08;

  const OK = 0x00;
  const NO_FINGER = 0x02;

  const CONFIRM_MESSAGES = {
    0x01: "Communication error with the sensor",
    0x03: "Could not capture the fingerprint image",
    0x06: "Fingerprint image was too messy - clean the finger and try again",
    0x07: "Not enough fingerprint detail - press flatter and try again",
    0x0a: "The two scans didn't match - use the same finger both times",
    0x15: "Sensor has no valid image to process",
  };

  let port = null;
  let reader = null;
  let buffer = [];
  let waiters = [];

  function isSupported() {
    return "serial" in navigator;
  }

  function isConnected() {
    return port !== null;
  }

  function notifyWaiters() {
    const pending = waiters;
    waiters = [];
    pending.forEach(function (resolve) { resolve(); });
  }

  async function readLoop() {
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        for (let i = 0; i < value.length; i++) buffer.push(value[i]);
        notifyWaiters();
      }
    } catch (err) {
      // Port unplugged or closed; disconnect() cleans up.
    } finally {
      notifyWaiters();
    }
  }

  async function connect() {
    if (!isSupported()) {
      throw new Error("This browser can't talk to USB sensors. Use Chrome or Edge on a computer.");
    }
    if (port) return;
    const selected = await navigator.serial.requestPort();
    await selected.open({ baudRate: 57600 });
    port = selected;
    buffer = [];
    reader = port.readable.getReader();
    readLoop();

    const confirm = (await command([0x13, 0x00, 0x00, 0x00, 0x00])).data[0];
    if (confirm !== OK) {
      await disconnect();
      throw new Error("Sensor rejected the handshake (wrong password or not an R307-type sensor).");
    }
  }

  async function disconnect() {
    const closing = port;
    port = null;
    try {
      if (reader) await reader.cancel();
    } catch (err) { /* already closed */ }
    try {
      if (reader) reader.releaseLock();
    } catch (err) { /* already released */ }
    reader = null;
    try {
      if (closing) await closing.close();
    } catch (err) { /* already closed */ }
  }

  function buildPacket(pid, data) {
    const length = data.length + 2;
    let sum = pid + (length >> 8) + (length & 0xff);
    data.forEach(function (byte) { sum += byte; });
    return new Uint8Array(
      HEADER.concat(ADDRESS, [pid, length >> 8, length & 0xff], data, [(sum >> 8) & 0xff, sum & 0xff])
    );
  }

  async function writePacket(pid, data) {
    if (!port) throw new Error("Sensor is not connected.");
    const writer = port.writable.getWriter();
    try {
      await writer.write(buildPacket(pid, data));
    } finally {
      writer.releaseLock();
    }
  }

  function waitForData(ms) {
    return new Promise(function (resolve) {
      const timer = setTimeout(resolve, ms);
      waiters.push(function () { clearTimeout(timer); resolve(); });
    });
  }

  async function readPacket(timeoutMs) {
    const deadline = Date.now() + (timeoutMs || 2000);
    while (true) {
      // Drop anything before a packet header.
      while (buffer.length >= 2 && !(buffer[0] === HEADER[0] && buffer[1] === HEADER[1])) {
        buffer.shift();
      }
      if (buffer.length >= 9) {
        const length = (buffer[7] << 8) | buffer[8];
        if (buffer.length >= 9 + length) {
          const packet = buffer.splice(0, 9 + length);
          return { pid: packet[6], data: packet.slice(9, 9 + length - 2) };
        }
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0 || !port) {
        throw new Error("The sensor stopped responding. Check the wiring and reconnect.");
      }
      await waitForData(remaining);
    }
  }

  async function command(data, timeoutMs) {
    await writePacket(PID_COMMAND, data);
    const packet = await readPacket(timeoutMs);
    if (packet.pid !== PID_ACK) throw new Error("Unexpected reply from the sensor.");
    return packet;
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function failure(confirm) {
    return new Error(CONFIRM_MESSAGES[confirm] || "Sensor error (code 0x" + confirm.toString(16) + ")");
  }

  async function waitForFinger(onStatus, message, isCancelled) {
    onStatus(message);
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      if (isCancelled()) throw new Error("Enrollment cancelled.");
      const confirm = (await command([0x01])).data[0];
      if (confirm === OK) return;
      if (confirm !== NO_FINGER) throw failure(confirm);
      await sleep(150);
    }
    throw new Error("Timed out waiting for a finger.");
  }

  async function waitForRemoval(onStatus, isCancelled) {
    onStatus("Lift your finger off the sensor");
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      if (isCancelled()) throw new Error("Enrollment cancelled.");
      const confirm = (await command([0x01])).data[0];
      if (confirm === NO_FINGER) return;
      await sleep(150);
    }
    throw new Error("Timed out waiting for the finger to be lifted.");
  }

  async function capture(slot, onStatus, message, isCancelled) {
    // Retry poor-quality images instead of failing the whole enrollment.
    for (let attempt = 0; attempt < 4; attempt++) {
      await waitForFinger(onStatus, message, isCancelled);
      const confirm = (await command([0x02, slot])).data[0];
      if (confirm === OK) return;
      onStatus(failure(confirm).message);
      await sleep(1200);
      await waitForRemoval(onStatus, isCancelled);
    }
    throw new Error("Couldn't get a clear fingerprint after several tries.");
  }

  // Two scans -> combined template -> raw template bytes (base64).
  async function enroll(onStatus, isCancelled) {
    isCancelled = isCancelled || function () { return false; };
    if (!port) throw new Error("Connect the sensor first.");
    buffer = [];

    await capture(0x01, onStatus, "Place the finger on the sensor", isCancelled);
    await waitForRemoval(onStatus, isCancelled);
    await capture(0x02, onStatus, "Place the same finger again", isCancelled);

    onStatus("Building fingerprint template...");
    let confirm = (await command([0x05])).data[0];
    if (confirm !== OK) throw failure(confirm);

    onStatus("Reading template from the sensor...");
    confirm = (await command([0x08, 0x01])).data[0];
    if (confirm !== OK) throw failure(confirm);

    const template = [];
    while (true) {
      const packet = await readPacket(3000);
      if (packet.pid !== PID_DATA && packet.pid !== PID_END) {
        throw new Error("Unexpected packet while reading the template.");
      }
      for (let i = 0; i < packet.data.length; i++) template.push(packet.data[i]);
      if (packet.pid === PID_END) break;
    }

    let binary = "";
    template.forEach(function (byte) { binary += String.fromCharCode(byte); });
    return btoa(binary);
  }

  if (isSupported()) {
    navigator.serial.addEventListener("disconnect", function (event) {
      if (port && event.target === port) disconnect();
    });
  }

  return { isSupported, isConnected, connect, disconnect, enroll };
})();
