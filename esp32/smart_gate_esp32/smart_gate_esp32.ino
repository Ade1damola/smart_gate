/*
 * ============================================================
 * SMART GATE SECURITY SYSTEM - ESP32-S3-N16R8
 * ============================================================
 *
 * Board: ESP32-S3 Dev Module
 * PSRAM: OPI PSRAM
 * Flash Size: 16MB
 *
 * This ESP32 handles:
 *   - Receives registered-vehicle alerts from the ANPR computer (local HTTP)
 *   - Buzzes and displays on OLED to notify the guard
 *   - Downloads the vehicle owner's fingerprint templates from the server
 *     and matches the driver's finger against them on the sensor (1:1)
 *   - Falls back to an OTP on the keypad when the driver isn't the owner
 *   - Reports every step to the hosted server (Render) for the live monitor
 *   - Controls the gate servo motor
 *
 * Fingerprints are NOT stored on this sensor. They're enrolled from any
 * spare sensor through the admin web page and kept on the server, so the
 * gate hardware is never needed during registration and a replacement
 * sensor works immediately.
 *
 * Gate flow for a registered vehicle:
 *   1. Driver scans a finger (always first).
 *   2. Owner's finger  -> gate opens.
 *      Anyone else     -> driver enters the owner's OTP on the keypad.
 *
 * USB enroll mode (when there's no spare sensor): press D on the keypad
 * while idle and confirm with #. The ESP32 restarts as a plain USB <->
 * sensor bridge, so the admin page in Chrome can enroll fingerprints
 * using this gate's sensor over the USB cable. Press D again to return to
 * normal gate operation. The mode is saved, so it survives the reset some
 * boards do when Chrome opens the port.
 *
 * Wiring (ESP32-S3-N16R8):
 *   R307 Fingerprint: TX→GPIO18, RX→GPIO17, VCC→5V, GND→GND
 *   4x4 Keypad:       Rows→GPIO 4,5,6,7  Cols→GPIO 10,11,12,13
 *   Grove OLED 1.12" V2 (SH1107, 128x128): SDA→GPIO8, SCL→GPIO9, VCC→3.3V, GND→GND
 *   Buzzer:            +→GPIO47, -→GND
 *   Servo SG90:        Signal→GPIO15, VCC→5V, GND→GND
 * ============================================================
 */

#include <WiFi.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <Adafruit_Fingerprint.h>
#include <Keypad.h>
#include <ESP32Servo.h>
#include <Preferences.h>
#include "mbedtls/base64.h"


// ============================================================
// CONFIGURATION - CHANGE THESE TO MATCH YOUR SETUP
// ============================================================

// WIFI_SSID, WIFI_PASS, DEVICE_API_KEY live in secrets.h (gitignored, never
// committed). Copy secrets.h.example to secrets.h and fill in your real
// values before building.
#include "secrets.h"

// Hosted server URL (your Render web service)
const char* SERVER_URL = "https://verigate-ry5y.onrender.com";

// If using local Flask server on laptop for testing, use this instead:
// const char* SERVER_URL = "http://192.168.1.50:5000";

// DEVICE_API_KEY (from secrets.h) is sent as the X-Device-Key header on
// every hosted-server call. Must exactly match the DEVICE_API_KEY
// environment variable set on the server.


// ============================================================
// PIN DEFINITIONS (ESP32-S3-N16R8 safe pins only)
// ============================================================

// Fingerprint sensor (UART via Serial1)
#define FP_RX_PIN   18    // Fingerprint TX → ESP32 GPIO18 (our RX)
#define FP_TX_PIN   17    // Fingerprint RX → ESP32 GPIO17 (our TX)

// Keypad 4x4
#define ROW1_PIN    4
#define ROW2_PIN    5
#define ROW3_PIN    6
#define ROW4_PIN    7
#define COL1_PIN    10
#define COL2_PIN    11
#define COL3_PIN    12
#define COL4_PIN    13

// OLED display (I2C)
#define OLED_SDA    8
#define OLED_SCL    9
#define OLED_ADDR   0x3C // Grove OLED 1.12" fixed I2C address
#define OLED_POWER_UP_MS 500

// Buzzer
#define BUZZER_PIN  47

// Servo motor (gate barrier)
#define SERVO_PIN   15


// ============================================================
// TIMING / LIMITS
// ============================================================

#define VERIFICATION_TIMEOUT_MS  90000   // Whole verification must finish in 90 s
#define OTP_ENTRY_TIMEOUT_MS     45000   // Time to type one OTP
#define GATE_OPEN_DURATION_MS    5000    // Keep gate open for 5 seconds
#define BUZZER_BEEP_MS           300     // Single beep duration
#define OTP_LENGTH               6       // 6-digit OTP
#define MAX_OTP_ATTEMPTS         3
#define HEARTBEAT_INTERVAL_MS    60000   // "Still online" ping while idle

#define MAX_TEMPLATES            6       // Owner fingerprints held per vehicle
#define MAX_TEMPLATE_BYTES       1024    // R307 templates are 512 bytes
#define MAX_LEGACY_IDS           6


// ============================================================
// HARDWARE OBJECTS
// ============================================================

// Grove OLED 1.12" V2 is an SH1107 128x128 panel; U8g2 has a driver made
// for Seeed's version (V1 was a 96x96 SSD1327 and needs a different one).
U8G2_SH1107_SEEED_128X128_F_HW_I2C display(U8G2_R0, U8X8_PIN_NONE, OLED_SCL, OLED_SDA);
Adafruit_Fingerprint finger = Adafruit_Fingerprint(&Serial1);

const byte ROWS = 4;
const byte COLS = 4;
char keys[ROWS][COLS] = {
    {'1', '2', '3', 'A'},
    {'4', '5', '6', 'B'},
    {'7', '8', '9', 'C'},
    {'*', '0', '#', 'D'}
};
byte rowPins[ROWS] = {ROW1_PIN, ROW2_PIN, ROW3_PIN, ROW4_PIN};
byte colPins[COLS] = {COL1_PIN, COL2_PIN, COL3_PIN, COL4_PIN};
Keypad keypad = Keypad(makeKeymap(keys), rowPins, colPins, ROWS, COLS);

Servo gateServo;

// Local web server (receives alerts from the ANPR computer)
WebServer localServer(80);


// ============================================================
// SYSTEM STATE
// ============================================================

enum VerifyPhase {
    PHASE_LOAD_TEMPLATES,   // fetch the owner's fingerprints from the server
    PHASE_FINGERPRINT,      // waiting for the driver's finger
    PHASE_OTP               // driver isn't the owner: waiting for an OTP
};

bool awaitingVerification = false;
VerifyPhase phase = PHASE_LOAD_TEMPLATES;
String pendingStaffId = "";
String pendingOwnerName = "";
String pendingPlate = "";
String pendingEventType = "EXIT";
unsigned long verificationStartTime = 0;
int otpAttempts = 0;

// A scan only counts when a finger is *placed*: the sensor must have read
// "no finger" since the last scan. R307 sensors can report a finger that
// isn't there (bright light, a smudge on the glass); without this, that
// phantom reading was scanned over and over as "not the owner".
bool fingerArmed = false;
unsigned long sensorLastClear = 0;
bool stuckSensorWarned = false;

// Owner's fingerprint templates, downloaded per vehicle.
uint8_t templates[MAX_TEMPLATES][MAX_TEMPLATE_BYTES];
size_t templateLengths[MAX_TEMPLATES];
int templateIds[MAX_TEMPLATES];
int templateCount = 0;

// Fingerprints enrolled the old way, in this sensor's own library.
int legacyIds[MAX_LEGACY_IDS];
int legacyCount = 0;

uint16_t sensorPacketLength = 128;
unsigned long lastHeartbeat = 0;

Preferences settings;
bool usbEnrollMode = false;


// ============================================================
// DISPLAY FUNCTIONS
// ============================================================

// 128x128 screen: a bold title, a divider, then body text in a 6px-wide
// font (21 characters per row), wrapped onto extra rows when needed.
#define BODY_CHARS     21
#define BODY_ROW_PX    14

void drawTitle(const String& title) {
    display.setFont(u8g2_font_7x14B_tf);
    display.drawStr(0, 14, title.c_str());
    display.drawHLine(0, 19, 128);
}

// Draws text from the given baseline, wrapping at spaces. Returns the
// baseline after the last row drawn.
int drawWrapped(const String& text, int baseline) {
    display.setFont(u8g2_font_6x12_tf);
    String rest = text;
    rest.trim();
    while (rest.length() > 0) {
        String row = rest;
        if (rest.length() > BODY_CHARS) {
            int cut = rest.lastIndexOf(' ', BODY_CHARS);
            if (cut <= 0) cut = BODY_CHARS;
            row = rest.substring(0, cut);
            rest = rest.substring(cut);
            rest.trim();
        } else {
            rest = "";
        }
        display.drawStr(0, baseline, row.c_str());
        baseline += BODY_ROW_PX;
    }
    return baseline;
}

void displayMessage(String line1, String line2, String line3) {
    display.clearBuffer();
    drawTitle(line1);
    int baseline = drawWrapped(line2, 38);
    drawWrapped(line3, baseline + 8);
    display.sendBuffer();
}

void displayLargeOTP(String otp) {
    display.clearBuffer();
    drawTitle("ENTER OTP");

    String digits = "";
    for (int i = 0; i < OTP_LENGTH; i++) {
        digits += i < (int)otp.length() ? otp[i] : '_';
    }
    display.setFont(u8g2_font_10x20_tf);
    display.drawStr((128 - OTP_LENGTH * 10) / 2, 66, digits.c_str());

    drawWrapped("#=Confirm  *=Clear", 104);
    display.sendBuffer();
}

void showIdleScreen() {
    /*
     * The screen shown whenever the gate isn't mid-verification. Always
     * reflects live Wi-Fi state so the guard has a way to tell the system
     * is offline without needing a Serial Monitor.
     */
    if (WiFi.status() == WL_CONNECTED) {
        displayMessage("SMART GATE", "System ready", "Waiting for vehicle...");
    } else {
        displayMessage("SMART GATE", "OFFLINE", "No server verification");
    }
}

void showFingerPrompt() {
    displayMessage("SCAN FINGER", pendingPlate, "Driver: place finger");
}

void showOtpPrompt() {
    displayMessage("NOT THE OWNER", "Enter owner's OTP", "Owner: rescan finger");
}


// ============================================================
// BUZZER FUNCTIONS
// ============================================================

void beepSuccess() {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(BUZZER_BEEP_MS);
    digitalWrite(BUZZER_PIN, LOW);
}

void beepAlert() {
    for (int i = 0; i < 2; i++) {
        digitalWrite(BUZZER_PIN, HIGH);
        delay(150);
        digitalWrite(BUZZER_PIN, LOW);
        delay(100);
    }
}

void beepError() {
    for (int i = 0; i < 3; i++) {
        digitalWrite(BUZZER_PIN, HIGH);
        delay(100);
        digitalWrite(BUZZER_PIN, LOW);
        delay(80);
    }
}


// ============================================================
// SERVO (GATE) FUNCTIONS
// ============================================================

void openGate() {
    Serial.println("[GATE] Opening...");
    displayMessage("ACCESS GRANTED", "Gate opening...", "");
    beepSuccess();

    gateServo.write(180);              // Lift barrier
    delay(GATE_OPEN_DURATION_MS);
    gateServo.write(0);                // Lower barrier

    Serial.println("[GATE] Closed.");
    showIdleScreen();
}


// ============================================================
// RAW SENSOR PROTOCOL
// ------------------------------------------------------------
// The Adafruit library has no calls for loading a template into the
// sensor (DownChar) or comparing its two buffers (Match), so those speak
// the R307 packet protocol directly on Serial1.
// ============================================================

#define FP_PID_COMMAND 0x01
#define FP_PID_DATA    0x02
#define FP_PID_ACK     0x07
#define FP_PID_END     0x08

void fpWritePacket(uint8_t pid, const uint8_t* data, uint16_t len) {
    uint16_t length = len + 2;
    uint16_t sum = pid + (length >> 8) + (length & 0xFF);
    const uint8_t header[] = {0xEF, 0x01, 0xFF, 0xFF, 0xFF, 0xFF, pid,
                              (uint8_t)(length >> 8), (uint8_t)(length & 0xFF)};
    Serial1.write(header, sizeof(header));
    for (uint16_t i = 0; i < len; i++) {
        sum += data[i];
    }
    Serial1.write(data, len);
    Serial1.write((uint8_t)(sum >> 8));
    Serial1.write((uint8_t)(sum & 0xFF));
}

int fpReadByte(unsigned long deadline) {
    while (millis() < deadline) {
        if (Serial1.available()) return Serial1.read();
        delay(1);
    }
    return -1;
}

// Reads one acknowledgement packet. Returns the payload length (confirm
// code first) or -1 on timeout / malformed reply.
int fpReadAck(uint8_t* payload, int maxLen, unsigned long timeoutMs) {
    unsigned long deadline = millis() + timeoutMs;
    int previous = -1;
    while (true) {
        int b = fpReadByte(deadline);
        if (b < 0) return -1;
        if (previous == 0xEF && b == 0x01) break;
        previous = b;
    }
    uint8_t meta[7];  // 4 address bytes, pid, 2 length bytes
    for (int i = 0; i < 7; i++) {
        int b = fpReadByte(deadline);
        if (b < 0) return -1;
        meta[i] = b;
    }
    int length = (meta[5] << 8) | meta[6];
    int payloadLen = length - 2;
    if (meta[4] != FP_PID_ACK || payloadLen < 1 || payloadLen > maxLen) return -1;
    for (int i = 0; i < length; i++) {   // payload + 2 checksum bytes
        int b = fpReadByte(deadline);
        if (b < 0) return -1;
        if (i < payloadLen) payload[i] = b;
    }
    return payloadLen;
}

void fpFlushInput() {
    while (Serial1.available()) Serial1.read();
}

// Loads a template into the sensor's CharBuffer2.
bool fpLoadTemplate(const uint8_t* data, size_t len) {
    fpFlushInput();
    const uint8_t command[] = {0x09, 0x02};
    fpWritePacket(FP_PID_COMMAND, command, sizeof(command));
    uint8_t ack[4];
    if (fpReadAck(ack, sizeof(ack), 1000) < 1 || ack[0] != 0x00) {
        Serial.println("[FP] Sensor refused template download.");
        return false;
    }
    size_t offset = 0;
    while (offset < len) {
        size_t chunk = min((size_t)sensorPacketLength, len - offset);
        bool last = offset + chunk >= len;
        fpWritePacket(last ? FP_PID_END : FP_PID_DATA, data + offset, chunk);
        offset += chunk;
    }
    Serial1.flush();
    delay(20);
    return true;
}

// Compares CharBuffer1 (live finger) with CharBuffer2 (loaded template).
bool fpMatchBuffers(int& score) {
    fpFlushInput();
    const uint8_t command[] = {0x03};
    fpWritePacket(FP_PID_COMMAND, command, sizeof(command));
    uint8_t ack[4];
    int len = fpReadAck(ack, sizeof(ack), 1000);
    if (len < 3) return false;
    score = (ack[1] << 8) | ack[2];
    return ack[0] == 0x00;
}


// ============================================================
// FINGERPRINT VERIFICATION
// ============================================================

/*
 * Checks the finger currently on the sensor against the owner.
 * Returns -1 if there's no (usable) finger, 0 if it's someone else,
 * 1 if it's the owner (matchedRef/score describe which template matched).
 */
int checkDriverFinger(String& matchedRef, int& score) {
    uint8_t p = finger.getImage();
    if (p == FINGERPRINT_NOFINGER) {
        if (stuckSensorWarned) {
            if (phase == PHASE_OTP) showOtpPrompt();
            else showFingerPrompt();
        }
        fingerArmed = true;
        sensorLastClear = millis();
        stuckSensorWarned = false;
        return -1;
    }
    if (p != FINGERPRINT_OK) return -1;

    if (!fingerArmed) {
        // "Finger" present but never lifted: either the last finger is
        // still there, or the sensor is seeing something that isn't a
        // finger. Tell the guard if it goes on for a while.
        if (!stuckSensorWarned && millis() - sensorLastClear > 8000) {
            Serial.println("[FP] Sensor keeps reporting a finger - lift finger / clean the glass.");
            displayMessage("LIFT FINGER", "Sensor reads a finger", "Lift it / clean glass");
            stuckSensorWarned = true;
        }
        return -1;
    }

    // Make sure it's still there a moment later, so a flicker isn't scanned.
    delay(80);
    if (finger.getImage() != FINGERPRINT_OK) return -1;
    fingerArmed = false;   // one result per placement
    Serial.println("[FP] Finger placed - checking.");

    if (finger.image2Tz(1) != FINGERPRINT_OK) {
        displayMessage("SCAN FINGER", "Unclear print", "Lift, press flat again");
        delay(800);
        return -1;
    }

    displayMessage("CHECKING...", pendingPlate, "Keep finger still");

    for (int i = 0; i < templateCount; i++) {
        if (fpLoadTemplate(templates[i], templateLengths[i]) && fpMatchBuffers(score)) {
            matchedRef = "db:" + String(templateIds[i]);
            return 1;
        }
    }

    if (legacyCount > 0 && finger.fingerFastSearch() == FINGERPRINT_OK) {
        for (int i = 0; i < legacyCount; i++) {
            if (legacyIds[i] == finger.fingerID) {
                matchedRef = "legacy:" + String(finger.fingerID);
                score = finger.confidence;
                return 1;
            }
        }
    }

    score = 0;
    return 0;
}



// ============================================================
// SERVER COMMUNICATION
// ============================================================

void beginServerRequest(HTTPClient& http, String path, uint16_t timeoutMs) {
    http.begin(String(SERVER_URL) + path);
    // Render's free tier can take 30-60 s to wake from idle; the default
    // ~5 s timeout would wrongly read that as a failure.
    http.setTimeout(timeoutMs);
    http.addHeader("X-Device-Key", DEVICE_API_KEY);
    http.addHeader("X-Device-Name", "gate");
}

// POSTs JSON and returns the response's "success" field (false on any error).
bool postJson(String path, JsonDocument& doc, String* messageOut = nullptr) {
    HTTPClient http;
    beginServerRequest(http, path, 30000);
    http.addHeader("Content-Type", "application/json");
    String body;
    serializeJson(doc, body);

    Serial.print("[HTTP] POST ");
    Serial.println(path);
    int httpCode = http.POST(body);
    bool success = false;
    if (httpCode > 0) {
        String response = http.getString();
        Serial.print("[HTTP] ");
        Serial.print(httpCode);
        Serial.print(" ");
        Serial.println(response);
        JsonDocument respDoc;
        if (!deserializeJson(respDoc, response)) {
            success = httpCode == 200 && respDoc["success"].as<bool>();
            if (messageOut) *messageOut = respDoc["message"].as<String>();
        }
    } else {
        Serial.print("[HTTP] Error: ");
        Serial.println(httpCode);
    }
    http.end();
    return success;
}

/*
 * Downloads the vehicle owner's fingerprint templates. Also tells the
 * server the gate is now waiting for a fingerprint (for the live monitor).
 */
bool fetchOwnerTemplates() {
    templateCount = 0;
    legacyCount = 0;

    HTTPClient http;
    beginServerRequest(http, "/api/device/users/" + pendingStaffId + "/fingerprints?plate=" +
                       pendingPlate + "&event_type=" + pendingEventType, 30000);
    int httpCode = http.GET();
    if (httpCode != 200) {
        Serial.print("[FP] Could not download templates. HTTP: ");
        Serial.println(httpCode);
        http.end();
        return false;
    }

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, http.getStream());
    http.end();
    if (err) {
        Serial.print("[FP] Bad template response: ");
        Serial.println(err.c_str());
        return false;
    }

    for (JsonObject item : doc["templates"].as<JsonArray>()) {
        if (templateCount >= MAX_TEMPLATES) break;
        const char* encoded = item["template"];
        if (!encoded) continue;
        size_t decodedLen = 0;
        int rc = mbedtls_base64_decode(templates[templateCount], MAX_TEMPLATE_BYTES, &decodedLen,
                                       (const unsigned char*)encoded, strlen(encoded));
        if (rc != 0 || decodedLen == 0) continue;
        templateLengths[templateCount] = decodedLen;
        templateIds[templateCount] = item["id"] | 0;
        templateCount++;
    }
    for (int id : doc["legacy_ids"].as<JsonArray>()) {
        if (legacyCount < MAX_LEGACY_IDS) legacyIds[legacyCount++] = id;
    }

    Serial.print("[FP] Owner templates: ");
    Serial.print(templateCount);
    Serial.print(" (+");
    Serial.print(legacyCount);
    Serial.println(" on gate sensor)");
    return true;
}

bool reportFingerprintResult(bool matched, String matchedRef, int score) {
    JsonDocument doc;
    doc["staff_id"] = pendingStaffId;
    doc["plate_number"] = pendingPlate;
    doc["event_type"] = pendingEventType;
    doc["matched"] = matched;
    doc["template_ref"] = matchedRef;
    doc["score"] = score;
    return postJson("/api/verify_fingerprint", doc);
}

bool verifyOTPOnServer(String otpCode, String& message) {
    JsonDocument doc;
    doc["staff_id"] = pendingStaffId;
    doc["otp_code"] = otpCode;
    doc["plate_number"] = pendingPlate;
    doc["event_type"] = pendingEventType;
    return postJson("/api/verify_otp", doc, &message);
}

void logEventToServer(String method, String status, String details) {
    JsonDocument doc;
    doc["staff_id"] = pendingStaffId;
    doc["plate_number"] = pendingPlate;
    doc["method"] = method;
    doc["event_type"] = pendingEventType;
    doc["status"] = status;
    doc["details"] = details;
    postJson("/api/log_event", doc);
}

void sendHeartbeat() {
    // Short timeout: this runs while idle and must not hold up an incoming
    // vehicle alert for long. Regular pings also keep Render's free tier
    // awake, so verification calls don't hit a cold start.
    HTTPClient http;
    beginServerRequest(http, "/api/device/heartbeat", 3000);
    int httpCode = http.POST("");
    http.end();
    Serial.print("[HEARTBEAT] HTTP: ");
    Serial.println(httpCode);
}


// ============================================================
// LOCAL WEB SERVER ROUTES (called by the ANPR computer)
// ============================================================

void handleStaffAlert() {
    /*
     * Called when ANPR reads a registered vehicle's plate.
     *   GET /staff_alert?staff_id=STF001&name=Dr.+Okonkwo&plate=AAB-234GH&event_type=EXIT
     */
    if (awaitingVerification) {
        localServer.send(409, "application/json",
            "{\"error\":\"Already processing a vehicle\"}");
        return;
    }

    if (!localServer.hasArg("staff_id") || !localServer.hasArg("plate")) {
        localServer.send(400, "application/json",
            "{\"error\":\"Missing staff_id or plate\"}");
        return;
    }

    pendingStaffId = localServer.arg("staff_id");
    pendingOwnerName = localServer.arg("name");
    pendingPlate = localServer.arg("plate");
    pendingEventType = localServer.hasArg("event_type") ? localServer.arg("event_type") : "EXIT";

    awaitingVerification = true;
    phase = PHASE_LOAD_TEMPLATES;
    otpAttempts = 0;
    fingerArmed = false;
    sensorLastClear = millis();
    stuckSensorWarned = false;
    verificationStartTime = millis();

    Serial.println("\n========================================");
    Serial.println("[ALERT] REGISTERED VEHICLE DETECTED");
    Serial.print("  Plate: ");
    Serial.println(pendingPlate);
    Serial.print("  Owner: ");
    Serial.println(pendingOwnerName);
    Serial.print("  Staff ID: ");
    Serial.println(pendingStaffId);
    Serial.println("========================================");

    // Respond first - the template download can take a while.
    localServer.send(200, "application/json",
        "{\"status\":\"ALERT_SENT\",\"message\":\"Guard notified\"}");

    beepAlert();
    displayMessage("STAFF VEHICLE", pendingPlate, pendingOwnerName);
}

void handleStatus() {
    String status = awaitingVerification ? "BUSY" : "READY";
    String response = "{\"status\":\"" + status + "\",\"ip\":\"" +
                      WiFi.localIP().toString() + "\"}";
    localServer.send(200, "application/json", response);
}

void handleOpenGate() {
    /*
     * Called when ANPR reads a plate that isn't registered (visitor) - no
     * verification needed, just open up.
     */
    if (awaitingVerification) {
        localServer.send(409, "application/json",
            "{\"error\":\"Already processing a registered vehicle\"}");
        return;
    }

    Serial.println("[GATE] Visitor vehicle - opening directly.");
    localServer.send(200, "application/json", "{\"status\":\"OPENING\"}");
    openGate();
}


// ============================================================
// MAIN VERIFICATION LOOP
// ============================================================

void endVerification() {
    awaitingVerification = false;
    templateCount = 0;
    legacyCount = 0;
    lastHeartbeat = millis();
    showIdleScreen();
}

void grantOwnerAccess(String matchedRef, int score) {
    Serial.print("[VERIFY] Owner verified (");
    Serial.print(matchedRef);
    Serial.println(").");
    displayMessage("OWNER VERIFIED", pendingOwnerName, "Confirming...");

    if (reportFingerprintResult(true, matchedRef, score)) {
        openGate();
    } else {
        displayMessage("SERVER ERROR", "Could not confirm", "Contact admin");
        beepError();
        delay(3000);
    }
    endVerification();
}

// Collects an OTP starting from the first digit pressed. Returns "" if
// the entry was abandoned or timed out.
String readOTP(char firstDigit) {
    String otp = String(firstDigit);
    displayLargeOTP(otp);
    unsigned long start = millis();

    while (millis() - start < OTP_ENTRY_TIMEOUT_MS) {
        char key = keypad.getKey();
        if (key >= '0' && key <= '9' && otp.length() < OTP_LENGTH) {
            otp += key;
            displayLargeOTP(otp);
            if (otp.length() == OTP_LENGTH) {
                delay(300);  // let the driver see the last digit
                return otp;
            }
        } else if (key == '*') {
            otp = "";
            displayLargeOTP(otp);
        } else if (key == '#' && otp.length() == OTP_LENGTH) {
            return otp;
        }
        delay(30);
    }
    return "";
}

void handleOtpEntry(char firstDigit);

void processVerification() {
    if (millis() - verificationStartTime > VERIFICATION_TIMEOUT_MS) {
        Serial.println("[VERIFY] Timeout.");
        displayMessage("TIMEOUT", "No verification", "Gate stays closed");
        beepError();
        logEventToServer("none", "fail", "Verification timeout");
        delay(3000);
        endVerification();
        return;
    }

    // --- 1. Get the owner's fingerprints ---
    if (phase == PHASE_LOAD_TEMPLATES) {
        displayMessage("STAFF VEHICLE", pendingPlate, "Loading owner...");
        bool loaded = fetchOwnerTemplates();
        if (loaded && templateCount + legacyCount > 0) {
            phase = PHASE_FINGERPRINT;
            showFingerPrompt();
        } else {
            // No fingerprint on file (or server unreachable): OTP only.
            phase = PHASE_OTP;
            displayMessage(loaded ? "NO FINGERPRINT" : "FP UNAVAILABLE", "on file for owner", "Enter OTP");
        }
        return;
    }

    // --- 3. OTP step: the keypad is checked first, so key presses are never
    //        lost while the sensor is being read. (The keypad is only live
    //        once a fingerprint has been tried - see step 2.)
    if (phase == PHASE_OTP) {
        char key = keypad.getKey();
        if (key >= '0' && key <= '9') {
            handleOtpEntry(key);
            return;
        }
    }

    // --- 2. Fingerprint always comes first (the owner can also still
    //        scan during the OTP step) ---
    String matchedRef;
    int score = 0;
    int fingerResult = checkDriverFinger(matchedRef, score);

    if (fingerResult == 1) {
        grantOwnerAccess(matchedRef, score);
        return;
    }

    if (fingerResult == 0) {
        Serial.println("[VERIFY] Finger is not the owner's.");
        beepError();
        if (phase == PHASE_FINGERPRINT) {
            // First non-owner scan: record it and move on to the OTP step.
            displayMessage("NOT THE OWNER", "Recording...", "");
            reportFingerprintResult(false, "", 0);
            phase = PHASE_OTP;
        }
        showOtpPrompt();
    }
}

void handleOtpEntry(char firstDigit) {
    String otp = readOTP(firstDigit);
    if (otp.length() != OTP_LENGTH) {
        displayMessage("OTP INCOMPLETE", "Try again", "");
        delay(1500);
        showOtpPrompt();
        return;
    }

    displayMessage("VERIFYING OTP", otp, "Please wait...");
    String message;
    if (verifyOTPOnServer(otp, message)) {
        Serial.println("[VERIFY] OTP accepted.");
        displayMessage("OTP VERIFIED", "Opening gate...", "");
        delay(500);
        openGate();
        endVerification();
        return;
    }

    otpAttempts++;
    Serial.print("[VERIFY] OTP rejected: ");
    Serial.println(message);
    beepError();
    if (otpAttempts >= MAX_OTP_ATTEMPTS) {
        displayMessage("ACCESS DENIED", "Too many attempts", "Gate stays closed");
        logEventToServer("otp", "fail", "Verification failed: too many OTP attempts");
        delay(3000);
        endVerification();
        return;
    }
    displayMessage("OTP REJECTED", message.length() ? message : "Invalid or expired",
                   String(MAX_OTP_ATTEMPTS - otpAttempts) + " tries left");
    delay(2500);
    showOtpPrompt();
}


// ============================================================
// USB ENROLL MODE
// ============================================================

void setUsbEnrollMode(bool enabled) {
    settings.putBool("usb_enroll", enabled);
    displayMessage(enabled ? "USB ENROLL MODE" : "GATE MODE", "Restarting...", "");
    delay(800);
    ESP.restart();
}

// Called when D is pressed while the gate is idle.
void confirmUsbEnrollMode() {
    displayMessage("USB ENROLL MODE?", "#=Yes  *=No", "Gate stops working");
    unsigned long start = millis();
    while (millis() - start < 10000) {
        char key = keypad.getKey();
        if (key == '#') setUsbEnrollMode(true);
        if (key == '*') break;
        delay(30);
    }
    showIdleScreen();
}

// Passes bytes straight between the USB port and the fingerprint sensor,
// so the admin page in Chrome can drive the sensor as if it were plugged
// into the laptop. Nothing else may be printed to Serial in this mode -
// it would corrupt the sensor's packets.
void runUsbBridge() {
    while (Serial.available()) Serial1.write(Serial.read());
    while (Serial1.available()) Serial.write(Serial1.read());
    if (keypad.getKey() == 'D') setUsbEnrollMode(false);
}

void setupUsbBridge() {
    // Same baud rate the admin page opens the port at, and the sensor's.
    Serial.begin(57600);
    Serial1.begin(57600, SERIAL_8N1, FP_RX_PIN, FP_TX_PIN);
    gateServo.attach(SERVO_PIN);
    gateServo.write(0);
    displayMessage("USB ENROLL MODE", "Use admin page", "D = back to gate");
    beepAlert();
}


// ============================================================
// SETUP
// ============================================================

bool initDisplay() {
    /*
     * The Grove OLED has no reset pin wired, and it ignores commands for a
     * moment after power-on - starting it immediately leaves the panel
     * white or garbled until the next reset. Give it time, and retry once.
     */
    delay(OLED_POWER_UP_MS);
    for (int attempt = 0; attempt < 2; attempt++) {
        display.begin();   // also starts I2C on OLED_SDA/OLED_SCL
        Wire.beginTransmission(OLED_ADDR);
        if (Wire.endTransmission() == 0) {
            display.clearBuffer();
            display.sendBuffer();
            return true;
        }
        delay(OLED_POWER_UP_MS);
    }
    return false;
}

void setup() {
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);
    bool displayOk = initDisplay();

    settings.begin("gate", false);
    usbEnrollMode = settings.getBool("usb_enroll", false);
    if (usbEnrollMode) {
        setupUsbBridge();
        return;
    }

    Serial.begin(115200);
    delay(1000);
    Serial.println("\n\n========================================");
    Serial.println("  SMART GATE SYSTEM - Starting up...");
    Serial.println("========================================\n");

    Serial.println("[INIT] Buzzer: OK");
    Serial.println(displayOk ? "[INIT] OLED: OK" : "[INIT] OLED: NOT FOUND - check wiring (SDA→GPIO8, SCL→GPIO9)");
    displayMessage("SMART GATE", "Starting up...", "");

    gateServo.attach(SERVO_PIN);
    gateServo.write(0);
    Serial.println("[INIT] Servo: OK (closed position)");

    Serial.println("[INIT] Keypad: OK");

    // --- Wi-Fi ---
    displayMessage("SMART GATE", "Connecting to", "Wi-Fi...");
    Serial.print("[WIFI] Connecting to ");
    Serial.print(WIFI_SSID);

    WiFi.begin(WIFI_SSID, WIFI_PASS);
    WiFi.setAutoReconnect(true);

    int wifiAttempts = 0;
    while (WiFi.status() != WL_CONNECTED && wifiAttempts < 40) {
        delay(500);
        Serial.print(".");
        wifiAttempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\n[WIFI] Connected!");
        Serial.print("[WIFI] IP Address: ");
        Serial.println(WiFi.localIP());

        if (MDNS.begin("verigate")) {
            Serial.println("[MDNS] Reachable at http://verigate.local");
        } else {
            Serial.println("[MDNS] Failed to start!");
        }
        displayMessage("WIFI CONNECTED", WiFi.localIP().toString(), "");
    } else {
        Serial.println("\n[WIFI] CONNECTION FAILED!");
        displayMessage("WIFI FAILED", "Offline mode", "Check credentials");
    }

    // --- Fingerprint sensor ---
    displayMessage("SMART GATE", "Checking", "fingerprint...");
    Serial1.begin(57600, SERIAL_8N1, FP_RX_PIN, FP_TX_PIN);
    finger.begin(57600);

    if (finger.verifyPassword()) {
        if (finger.getParameters() == FINGERPRINT_OK && finger.packet_len >= 32) {
            sensorPacketLength = finger.packet_len;
        }
        Serial.print("[INIT] Fingerprint sensor: OK (packet size ");
        Serial.print(sensorPacketLength);
        Serial.println(")");
    } else {
        Serial.println("[INIT] Fingerprint sensor: NOT FOUND!");
        Serial.println("       Check wiring: TX→GPIO18, RX→GPIO17");
    }

    // --- Local web server routes ---
    localServer.on("/staff_alert", HTTP_GET, handleStaffAlert);
    localServer.on("/status", HTTP_GET, handleStatus);
    localServer.on("/open_gate", HTTP_GET, handleOpenGate);
    localServer.begin();
    Serial.println("[SERVER] Local web server started on port 80");

    if (WiFi.status() == WL_CONNECTED) {
        sendHeartbeat();
    }
    lastHeartbeat = millis();

    delay(1000);
    beepSuccess();
    showIdleScreen();

    Serial.println("\n========================================");
    Serial.println("  SYSTEM READY");
    Serial.print("  ESP32 IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("  Server:   ");
    Serial.println(SERVER_URL);
    Serial.println("========================================\n");
}


// ============================================================
// MAIN LOOP
// ============================================================

void loop() {
    if (usbEnrollMode) {
        runUsbBridge();
        return;
    }

    localServer.handleClient();

    if (awaitingVerification) {
        processVerification();
    } else {
        if (keypad.getKey() == 'D') {
            confirmUsbEnrollMode();
        }
        if (WiFi.status() == WL_CONNECTED && millis() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
            lastHeartbeat = millis();
            sendHeartbeat();
        }
    }

    // Check Wi-Fi connection and reconnect if needed
    static unsigned long lastWifiCheck = 0;
    static bool wifiWasConnected = true;
    if (millis() - lastWifiCheck > 10000) {
        lastWifiCheck = millis();
        bool wifiConnected = (WiFi.status() == WL_CONNECTED);

        if (!wifiConnected) {
            Serial.println("[WIFI] Disconnected! Reconnecting...");
            WiFi.reconnect();
        }

        // Refresh the OLED the moment connectivity changes.
        if (wifiConnected != wifiWasConnected && !awaitingVerification) {
            showIdleScreen();
        }
        wifiWasConnected = wifiConnected;
    }

    delay(10);
}
