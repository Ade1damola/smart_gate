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
 *   - Receives staff vehicle alerts from the Raspberry Pi (local HTTP)
 *   - Buzzes and displays on OLED to notify the guard
 *   - Reads fingerprint for owner verification
 *   - Reads OTP from keypad for non-owner verification
 *   - Sends verification requests to the hosted server (PythonAnywhere)
 *   - Controls the gate servo motor
 *   - Logs events to the hosted server
 * 
 * Wiring (ESP32-S3-N16R8):
 *   R307 Fingerprint: TX→GPIO18, RX→GPIO17, VCC→5V, GND→GND
 *   4x4 Keypad:       Rows→GPIO 4,5,6,7  Cols→GPIO 10,11,12,13
 *   OLED SSD1306:      SDA→GPIO8, SCL→GPIO9, VCC→3.3V, GND→GND
 *   Buzzer:            +→GPIO47, -→GND
 *   Servo SG90:        Signal→GPIO15, VCC→5V, GND→GND
 * ============================================================
 */

#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_Fingerprint.h>
#include <Keypad.h>
#include <ESP32Servo.h>


// ============================================================
// CONFIGURATION - CHANGE THESE TO MATCH YOUR SETUP
// ============================================================

// Wi-Fi credentials (campus Wi-Fi or hotspot)
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";

// Hosted server URL (your Render web service)
const char* SERVER_URL = "https://YOUR_APP_NAME.onrender.com";

// If using local Flask server on laptop for testing, use this instead:
// const char* SERVER_URL = "http://192.168.1.50:5000";

// Shared secret sent as the X-Device-Key header on every hosted-server call.
// Must exactly match the DEVICE_API_KEY environment variable set on the
// server. Leave as "" only while testing against a local server that has no
// DEVICE_API_KEY configured.
const char* DEVICE_API_KEY = "YOUR_DEVICE_API_KEY";


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
#define OLED_WIDTH  128
#define OLED_HEIGHT 64
#define OLED_ADDR   0x3C

// Buzzer
#define BUZZER_PIN  47

// Servo motor (gate barrier)
#define SERVO_PIN   15

// Built-in RGB LED (available on GPIO48 for status)
#define RGB_LED_PIN 48


// ============================================================
// TIMING CONSTANTS
// ============================================================

#define VERIFICATION_TIMEOUT_MS  30000   // 30 seconds to verify before timeout
#define GATE_OPEN_DURATION_MS    5000    // Keep gate open for 5 seconds
#define BUZZER_BEEP_MS           300     // Single beep duration
#define OTP_LENGTH               6       // 6-digit OTP


// ============================================================
// HARDWARE OBJECTS
// ============================================================

// OLED display
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);

// Fingerprint sensor on Serial1
Adafruit_Fingerprint finger = Adafruit_Fingerprint(&Serial1);

// Keypad setup
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

// Servo motor
Servo gateServo;

// Local web server (receives alerts from Raspberry Pi)
WebServer localServer(80);


// ============================================================
// SYSTEM STATE
// ============================================================

bool awaitingVerification = false;
String pendingStaffId = "";
String pendingOwnerName = "";
String pendingPlate = "";
int pendingFingerprintId = -1;
String pendingEventType = "EXIT";
unsigned long verificationStartTime = 0;


// ============================================================
// DISPLAY FUNCTIONS
// ============================================================

void displayMessage(String line1, String line2, String line3) {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);

    // Line 1: large text
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println(line1);

    // Line 2: medium text
    display.setCursor(0, 22);
    display.println(line2);

    // Line 3: small text
    display.setCursor(0, 44);
    display.println(line3);

    display.display();
}

void displayLargeOTP(String otp, int digits_entered) {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);

    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println("ENTER OTP:");

    // Show entered digits as large text
    display.setTextSize(2);
    display.setCursor(10, 25);

    // Show entered digits and underscores for remaining
    for (int i = 0; i < OTP_LENGTH; i++) {
        if (i < digits_entered) {
            display.print(otp[i]);
        } else {
            display.print("_");
        }
        if (i < OTP_LENGTH - 1) display.print(" ");
    }

    display.setTextSize(1);
    display.setCursor(0, 52);
    display.println("# = Confirm  * = Clear");

    display.display();
}


// ============================================================
// BUZZER FUNCTIONS
// ============================================================

void beepSuccess() {
    // Single long beep = success
    digitalWrite(BUZZER_PIN, HIGH);
    delay(BUZZER_BEEP_MS);
    digitalWrite(BUZZER_PIN, LOW);
}

void beepAlert() {
    // Two short beeps = staff vehicle detected, guard attention
    for (int i = 0; i < 2; i++) {
        digitalWrite(BUZZER_PIN, HIGH);
        delay(150);
        digitalWrite(BUZZER_PIN, LOW);
        delay(100);
    }
}

void beepError() {
    // Three rapid beeps = access denied
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

    gateServo.write(90);               // Lift barrier (90 degrees)
    delay(GATE_OPEN_DURATION_MS);      // Hold open
    gateServo.write(0);                // Lower barrier (0 degrees)

    Serial.println("[GATE] Closed.");
    displayMessage("SMART GATE", "System ready", "Waiting for vehicle...");
}


// ============================================================
// FINGERPRINT FUNCTIONS
// ============================================================

int scanFingerprint() {
    /*
     * Attempts to read and match a fingerprint.
     * Returns the matched template ID if found, or -1 if no match / no finger.
     * Non-blocking: returns immediately if no finger is on the sensor.
     */
    uint8_t p = finger.getImage();
    if (p != FINGERPRINT_OK) {
        return -1;  // No finger detected or error
    }

    p = finger.image2Tz();
    if (p != FINGERPRINT_OK) {
        Serial.println("[FP] Image conversion failed.");
        return -1;
    }

    p = finger.fingerFastSearch();
    if (p == FINGERPRINT_OK) {
        Serial.print("[FP] Match found! Template ID: ");
        Serial.print(finger.fingerID);
        Serial.print(" | Confidence: ");
        Serial.println(finger.confidence);
        return finger.fingerID;
    }

    Serial.println("[FP] No match found.");
    return -1;
}

bool enrollFingerprint(int id) {
    /*
     * Enrolls a new fingerprint at the given template ID.
     * Used during staff registration by the admin.
     * Blocks until enrollment is complete or fails.
     */
    Serial.print("[FP] Enrolling fingerprint at ID: ");
    Serial.println(id);
    displayMessage("ENROLL FINGER", "Place finger on", "scanner...");

    // First scan
    int p = -1;
    while (p != FINGERPRINT_OK) {
        p = finger.getImage();
        delay(100);
    }

    p = finger.image2Tz(1);
    if (p != FINGERPRINT_OK) return false;

    displayMessage("ENROLL FINGER", "Remove finger", "");
    delay(2000);

    // Wait for finger removal
    while (finger.getImage() != FINGERPRINT_NOFINGER) {
        delay(100);
    }

    displayMessage("ENROLL FINGER", "Place same finger", "again...");

    // Second scan
    p = -1;
    while (p != FINGERPRINT_OK) {
        p = finger.getImage();
        delay(100);
    }

    p = finger.image2Tz(2);
    if (p != FINGERPRINT_OK) return false;

    // Create model from the two scans
    p = finger.createModel();
    if (p != FINGERPRINT_OK) return false;

    // Store the model at the given ID
    p = finger.storeModel(id);
    if (p == FINGERPRINT_OK) {
        Serial.println("[FP] Enrollment successful!");
        displayMessage("ENROLL SUCCESS", "Fingerprint saved", "");
        beepSuccess();
        delay(1500);
        return true;
    }

    return false;
}


// ============================================================
// KEYPAD FUNCTIONS
// ============================================================

String readOTPFromKeypad() {
    /*
     * Waits for the user to enter a 6-digit OTP on the keypad.
     * Shows progress on the OLED display.
     * '#' confirms, '*' clears all entered digits.
     * Returns the OTP string, or empty string if timeout.
     */
    String otp = "";
    unsigned long startTime = millis();

    displayLargeOTP(otp, 0);

    while (millis() - startTime < VERIFICATION_TIMEOUT_MS) {
        char key = keypad.getKey();

        if (key) {
            if (key >= '0' && key <= '9') {
                // Digit pressed
                if (otp.length() < OTP_LENGTH) {
                    otp += key;
                    displayLargeOTP(otp, otp.length());
                    Serial.print("[KEYPAD] Digit entered: ");
                    Serial.println(key);
                }

                // Auto-submit when 6 digits entered
                if (otp.length() == OTP_LENGTH) {
                    delay(300);  // Brief pause so user sees all digits
                    return otp;
                }

            } else if (key == '*') {
                // Clear all digits
                otp = "";
                displayLargeOTP(otp, 0);
                Serial.println("[KEYPAD] Cleared.");

            } else if (key == '#') {
                // Confirm (even if less than 6 digits, in case of error)
                if (otp.length() == OTP_LENGTH) {
                    return otp;
                } else {
                    displayMessage("ENTER OTP", "Need 6 digits", "Try again...");
                    delay(1000);
                    displayLargeOTP(otp, otp.length());
                }
            }
            // A, B, C, D keys are ignored
        }

        delay(50);  // Small delay to avoid busy-waiting
    }

    // Timeout
    return "";
}


// ============================================================
// SERVER COMMUNICATION FUNCTIONS
// ============================================================

bool verifyFingerprintOnServer(String staffId, int fingerprintId, String plate, String eventType) {
    /*
     * Sends fingerprint verification result to the hosted server.
     * The R307 sensor does the actual matching locally.
     * We just tell the server which template ID matched.
     */
    HTTPClient http;
    String url = String(SERVER_URL) + "/api/verify_fingerprint";

    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Key", DEVICE_API_KEY);

    JsonDocument doc;
    doc["staff_id"] = staffId;
    doc["fingerprint_template_id"] = String(fingerprintId);
    doc["plate_number"] = plate;
    doc["event_type"] = eventType;

    String body;
    serializeJson(doc, body);

    Serial.print("[HTTP] POST ");
    Serial.println(url);

    int httpCode = http.POST(body);

    if (httpCode == 200) {
        String response = http.getString();
        Serial.print("[HTTP] Response: ");
        Serial.println(response);

        JsonDocument respDoc;
        deserializeJson(respDoc, response);
        bool success = respDoc["success"].as<bool>();

        http.end();
        return success;
    }

    Serial.print("[HTTP] Error: ");
    Serial.println(httpCode);
    http.end();
    return false;
}

bool verifyOTPOnServer(String staffId, String otpCode, String plate, String eventType) {
    /*
     * Sends the entered OTP to the hosted server for verification.
     * The server checks if the OTP is valid, belongs to the right
     * staff account, and hasn't expired.
     */
    HTTPClient http;
    String url = String(SERVER_URL) + "/api/verify_otp";

    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Key", DEVICE_API_KEY);

    JsonDocument doc;
    doc["staff_id"] = staffId;
    doc["otp_code"] = otpCode;
    doc["plate_number"] = plate;
    doc["event_type"] = eventType;

    String body;
    serializeJson(doc, body);

    Serial.print("[HTTP] POST ");
    Serial.println(url);

    int httpCode = http.POST(body);

    if (httpCode == 200) {
        String response = http.getString();
        Serial.print("[HTTP] Response: ");
        Serial.println(response);

        JsonDocument respDoc;
        deserializeJson(respDoc, response);
        bool success = respDoc["success"].as<bool>();
        String message = respDoc["message"].as<String>();

        http.end();

        if (success) {
            return true;
        } else {
            // Show the server's error message on OLED
            displayMessage("OTP FAILED", message, "");
            return false;
        }
    }

    Serial.print("[HTTP] Error: ");
    Serial.println(httpCode);
    http.end();
    return false;
}

void logEventToServer(String staffId, String plate, String method,
                      String eventType, String status, String details) {
    /*
     * Sends a general event log to the hosted server.
     * Used for failed attempts, timeouts, etc.
     */
    HTTPClient http;
    String url = String(SERVER_URL) + "/api/log_event";

    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Key", DEVICE_API_KEY);

    JsonDocument doc;
    doc["staff_id"] = staffId;
    doc["plate_number"] = plate;
    doc["method"] = method;
    doc["event_type"] = eventType;
    doc["status"] = status;
    doc["details"] = details;

    String body;
    serializeJson(doc, body);

    int httpCode = http.POST(body);
    http.end();

    Serial.print("[LOG] Event logged. HTTP: ");
    Serial.println(httpCode);
}

bool pushFingerprintIdToServer(String staffId, int fingerprintId) {
    /*
     * Tells the hosted server which template ID a staff member was just
     * enrolled at, so the admin-created record and the physically-enrolled
     * fingerprint are linked.
     */
    HTTPClient http;
    String url = String(SERVER_URL) + "/api/device/staff/" + staffId + "/fingerprint";

    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Key", DEVICE_API_KEY);

    JsonDocument doc;
    doc["fingerprint_template_id"] = String(fingerprintId);

    String body;
    serializeJson(doc, body);

    int httpCode = http.POST(body);
    bool ok = (httpCode == 200);

    Serial.print("[ENROLL] Server sync HTTP: ");
    Serial.println(httpCode);
    http.end();
    return ok;
}


// ============================================================
// LOCAL WEB SERVER ROUTES (receives alerts from Raspberry Pi)
// ============================================================

void handleStaffAlert() {
    /*
     * Called by the Raspberry Pi when ANPR detects a staff vehicle.
     * The Pi already checked with the hosted server and got the staff info.
     * Now it tells the ESP32 to alert the guard and start verification.
     *
     * Expected request from Pi:
     *   GET /staff_alert?staff_id=STF001&name=Dr.+Okonkwo&plate=AAB-234GH
     *       &fingerprint_id=1&event_type=EXIT
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

    // Store the pending verification details
    pendingStaffId = localServer.arg("staff_id");
    pendingOwnerName = localServer.arg("name");
    pendingPlate = localServer.arg("plate");
    pendingFingerprintId = localServer.arg("fingerprint_id").toInt();
    pendingEventType = localServer.hasArg("event_type") ?
                       localServer.arg("event_type") : "EXIT";

    // Activate verification mode
    awaitingVerification = true;
    verificationStartTime = millis();

    Serial.println("\n========================================");
    Serial.println("[ALERT] STAFF VEHICLE DETECTED");
    Serial.print("  Plate: ");
    Serial.println(pendingPlate);
    Serial.print("  Owner: ");
    Serial.println(pendingOwnerName);
    Serial.print("  Staff ID: ");
    Serial.println(pendingStaffId);
    Serial.print("  FP ID: ");
    Serial.println(pendingFingerprintId);
    Serial.println("========================================");

    // Alert the guard
    beepAlert();
    displayMessage("STAFF VEHICLE",
                   pendingPlate,
                   pendingOwnerName);

    // Respond to the Pi
    localServer.send(200, "application/json",
        "{\"status\":\"ALERT_SENT\",\"message\":\"Guard notified\"}");
}

void handleStatus() {
    /*
     * Simple status endpoint so the Pi can check if the ESP32 is alive.
     * GET /status
     */
    String status = awaitingVerification ? "BUSY" : "READY";
    String response = "{\"status\":\"" + status + "\",\"ip\":\"" +
                      WiFi.localIP().toString() + "\"}";
    localServer.send(200, "application/json", response);
}

void handleEnrollRequest() {
    /*
     * Triggered by the admin to enroll a new fingerprint for a staff member
     * already created in the hosted admin panel.
     * GET /enroll?id=5&staff_id=STAFF004
     */
    if (!localServer.hasArg("id") || !localServer.hasArg("staff_id")) {
        localServer.send(400, "application/json",
            "{\"error\":\"Missing fingerprint id or staff_id\"}");
        return;
    }

    int fpId = localServer.arg("id").toInt();
    String staffId = localServer.arg("staff_id");
    localServer.send(200, "application/json",
        "{\"status\":\"ENROLLING\",\"message\":\"Place finger on sensor\"}");

    // This blocks until enrollment is complete
    bool success = enrollFingerprint(fpId);

    if (success) {
        Serial.print("[ENROLL] Success at ID ");
        Serial.println(fpId);

        // Link this template ID to the staff record on the hosted server.
        bool synced = pushFingerprintIdToServer(staffId, fpId);
        if (!synced) {
            Serial.println("[ENROLL] WARNING: fingerprint enrolled locally but");
            Serial.println("         the server was not updated. Retry the sync.");
            displayMessage("ENROLLED LOCALLY", "Server sync failed", "Retry later");
            beepError();
            delay(2000);
        }
    } else {
        Serial.println("[ENROLL] Failed.");
        displayMessage("ENROLL FAILED", "Try again", "");
        beepError();
    }

    delay(2000);
    displayMessage("SMART GATE", "System ready", "Waiting for vehicle...");
}


// ============================================================
// MAIN VERIFICATION LOOP
// ============================================================

void processVerification() {
    /*
     * Called repeatedly from loop() while awaitingVerification is true.
     * Checks for fingerprint scan or keypad input.
     * The guard has approached the vehicle and is facilitating this.
     */

    // Check timeout
    if (millis() - verificationStartTime > VERIFICATION_TIMEOUT_MS) {
        Serial.println("[VERIFY] Timeout - no verification received.");
        displayMessage("TIMEOUT", "No verification", "Gate stays closed");
        beepError();

        logEventToServer(pendingStaffId, pendingPlate, "NONE",
                         pendingEventType, "FAIL", "Verification timeout");

        awaitingVerification = false;
        delay(3000);
        displayMessage("SMART GATE", "System ready", "Waiting for vehicle...");
        return;
    }

    // After the initial alert, show the verification prompt
    static bool promptShown = false;
    if (!promptShown) {
        delay(2000);  // Let the guard read the alert first
        displayMessage("VERIFY DRIVER",
                       "Finger=Owner",
                       "0-9=Enter OTP");
        promptShown = true;
    }

    // --- Check for fingerprint ---
    int fpResult = scanFingerprint();

    if (fpResult >= 0) {
        // A fingerprint was detected and matched a template
        Serial.print("[VERIFY] Fingerprint matched ID: ");
        Serial.println(fpResult);

        if (fpResult == pendingFingerprintId) {
            // It's the owner!
            Serial.println("[VERIFY] Owner verified by fingerprint.");
            displayMessage("OWNER VERIFIED", pendingOwnerName, "Opening gate...");

            bool serverOk = verifyFingerprintOnServer(
                pendingStaffId, fpResult, pendingPlate, pendingEventType);

            if (serverOk) {
                openGate();
            } else {
                // Server rejected but fingerprint matched locally
                // This shouldn't happen normally, but handle it
                displayMessage("SERVER ERROR", "Contact admin", "");
                beepError();
                delay(3000);
            }
        } else {
            // Fingerprint matched someone, but not the car owner
            Serial.println("[VERIFY] Fingerprint does not match owner.");
            displayMessage("NOT THE OWNER", "Use OTP instead", "");
            beepError();

            logEventToServer(pendingStaffId, pendingPlate, "FINGERPRINT",
                             pendingEventType, "FAIL",
                             "FP ID " + String(fpResult) + " != owner ID " +
                             String(pendingFingerprintId));

            delay(2000);
            displayMessage("VERIFY DRIVER",
                           "Finger=Owner",
                           "0-9=Enter OTP");
        }

        promptShown = false;
        if (fpResult == pendingFingerprintId) {
            awaitingVerification = false;
            displayMessage("SMART GATE", "System ready", "Waiting for vehicle...");
        }
        return;
    }

    // --- Check for keypad input (OTP) ---
    char key = keypad.getKey();

    if (key && key >= '0' && key <= '9') {
        // First digit of OTP entered — collect the rest
        Serial.println("[VERIFY] OTP entry started.");

        // Put the first digit into the OTP string
        String otp = String(key);
        displayLargeOTP(otp, 1);

        // Read the remaining digits
        unsigned long otpStartTime = millis();
        while (otp.length() < OTP_LENGTH &&
               (millis() - otpStartTime) < VERIFICATION_TIMEOUT_MS) {

            char nextKey = keypad.getKey();

            if (nextKey) {
                if (nextKey >= '0' && nextKey <= '9') {
                    otp += nextKey;
                    displayLargeOTP(otp, otp.length());

                } else if (nextKey == '*') {
                    // Clear and restart
                    otp = "";
                    displayLargeOTP(otp, 0);

                } else if (nextKey == '#' && otp.length() == OTP_LENGTH) {
                    break;  // Confirm
                }
            }

            // Auto-submit when 6 digits reached
            if (otp.length() == OTP_LENGTH) {
                delay(300);
                break;
            }

            delay(50);
        }

        if (otp.length() == OTP_LENGTH) {
            Serial.print("[VERIFY] OTP entered: ");
            Serial.println(otp);

            displayMessage("VERIFYING OTP", otp, "Please wait...");

            bool serverOk = verifyOTPOnServer(
                pendingStaffId, otp, pendingPlate, pendingEventType);

            if (serverOk) {
                Serial.println("[VERIFY] OTP verified! Non-owner access granted.");
                displayMessage("OTP VERIFIED", "Access granted", "Opening gate...");
                delay(500);
                openGate();
            } else {
                Serial.println("[VERIFY] OTP rejected.");
                displayMessage("ACCESS DENIED", "Invalid or expired", "OTP");
                beepError();
                delay(3000);
                displayMessage("VERIFY DRIVER",
                               "Finger=Owner",
                               "0-9=Enter OTP");
            }
        } else {
            // OTP entry timed out or was incomplete
            Serial.println("[VERIFY] OTP entry incomplete.");
            displayMessage("OTP INCOMPLETE", "Try again", "");
            delay(2000);
            displayMessage("VERIFY DRIVER",
                           "Finger=Owner",
                           "0-9=Enter OTP");
        }

        promptShown = false;
        if (otp.length() == OTP_LENGTH) {
            awaitingVerification = false;
            displayMessage("SMART GATE", "System ready", "Waiting for vehicle...");
        }
    }
}


// ============================================================
// SETUP
// ============================================================

void setup() {
    // Serial monitor for debugging
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n\n========================================");
    Serial.println("  SMART GATE SYSTEM - Starting up...");
    Serial.println("========================================\n");

    // --- Initialize buzzer ---
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);
    Serial.println("[INIT] Buzzer: OK");

    // --- Initialize OLED display ---
    Wire.begin(OLED_SDA, OLED_SCL);
    if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
        Serial.println("[INIT] OLED: FAILED!");
        // Continue anyway — system can work without display
    } else {
        Serial.println("[INIT] OLED: OK");
    }
    displayMessage("SMART GATE", "Starting up...", "");

    // --- Initialize fingerprint sensor ---
    Serial1.begin(57600, SERIAL_8N1, FP_RX_PIN, FP_TX_PIN);
    finger.begin(57600);

    if (finger.verifyPassword()) {
        Serial.print("[INIT] Fingerprint sensor: OK (");
        finger.getTemplateCount();
        Serial.print(finger.templateCount);
        Serial.println(" templates stored)");
    } else {
        Serial.println("[INIT] Fingerprint sensor: NOT FOUND!");
        Serial.println("       Check wiring: TX→GPIO18, RX→GPIO17");
    }

    // --- Initialize servo ---
    gateServo.attach(SERVO_PIN);
    gateServo.write(0);  // Start in closed position
    Serial.println("[INIT] Servo: OK (closed position)");

    // --- Initialize keypad ---
    // Keypad library handles pin modes automatically
    Serial.println("[INIT] Keypad: OK");

    // --- Connect to Wi-Fi ---
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
        Serial.print("[WIFI] Server: ");
        Serial.println(SERVER_URL);

        displayMessage("WIFI CONNECTED",
                       WiFi.localIP().toString(),
                       "");
    } else {
        Serial.println("\n[WIFI] CONNECTION FAILED!");
        Serial.println("       System will work offline (no server verification)");
        displayMessage("WIFI FAILED", "Offline mode", "Check credentials");
    }

    // --- Set up local web server routes ---
    localServer.on("/staff_alert", HTTP_GET, handleStaffAlert);
    localServer.on("/status", HTTP_GET, handleStatus);
    localServer.on("/enroll", HTTP_GET, handleEnrollRequest);
    localServer.begin();
    Serial.println("[SERVER] Local web server started on port 80");

    // --- Startup complete ---
    delay(1000);
    beepSuccess();
    displayMessage("SMART GATE", "System ready", "Waiting for vehicle...");

    Serial.println("\n========================================");
    Serial.println("  SYSTEM READY");
    Serial.print("  ESP32 IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("  Server:   ");
    Serial.println(SERVER_URL);
    Serial.println("  Listening for Pi alerts on /staff_alert");
    Serial.println("========================================\n");
}


// ============================================================
// MAIN LOOP
// ============================================================

void loop() {
    // Always handle incoming HTTP requests from the Pi
    localServer.handleClient();

    // If a staff vehicle has been detected, run verification
    if (awaitingVerification) {
        processVerification();
    }

    // Check Wi-Fi connection and reconnect if needed
    static unsigned long lastWifiCheck = 0;
    if (millis() - lastWifiCheck > 10000) {  // Check every 10 seconds
        lastWifiCheck = millis();
        if (WiFi.status() != WL_CONNECTED) {
            Serial.println("[WIFI] Disconnected! Reconnecting...");
            WiFi.reconnect();
        }
    }

    delay(10);  // Small delay to prevent watchdog timer reset
}
