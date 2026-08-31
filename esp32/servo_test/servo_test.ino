/*
 * Standalone servo test - isolates the gate servo from everything else
 * (Wi-Fi, OLED, keypad, fingerprint) to check the wiring/power on its own.
 *
 * Wiring: Signal -> GPIO15, VCC -> 5V, GND -> GND (same as smart_gate_esp32.ino)
 *
 * Flash this, open Serial Monitor at 115200 baud. The servo should sweep
 * 0 -> 90 -> 0 degrees every 3 seconds, and each step prints to serial.
 */

#include <ESP32Servo.h>

#define SERVO_PIN 15

Servo gateServo;

void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("[SERVO TEST] Attaching servo on GPIO15...");

    gateServo.attach(SERVO_PIN);
    gateServo.write(0);
    Serial.println("[SERVO TEST] Attached. Starting sweep loop.");
}

void loop() {
    Serial.println("[SERVO TEST] -> 90 degrees (open)");
    gateServo.write(90);
    delay(1500);

    Serial.println("[SERVO TEST] -> 0 degrees (closed)");
    gateServo.write(0);
    delay(1500);
}
