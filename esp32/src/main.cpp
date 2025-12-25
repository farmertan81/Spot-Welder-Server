#include <Adafruit_NeoPixel.h>
#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <esp_wifi.h>
#include <math.h>

#include "../lib/INA226/INA226.h"

// -------------------- I²C Mutex --------------------
SemaphoreHandle_t i2c_mutex = NULL;

// -------------------- Pin Definitions --------------------
#define FET_CHARGE 5
#define FET_WELD 4
#define PEDAL_PIN 6
#define I2C_SDA 2
#define I2C_SCL 3
#define THERM_PIN 7
#define LED_PIN 21
#define BUTTON_PIN 1

// -------------------- WiFi / TCP Settings ----------------
const char* ssid = "Jaime's Wi-Fi Network";
const char* password = "jackaustin";

WiFiServer server(8888);
WiFiClient client;

// -------------------- INA226 (cell voltage only) ---------
INA226 ina(0x40);       // Pack voltage
INA226 inaCell1(0x41);  // Node 1
INA226 inaCell2(0x44);  // Node 2

// -------------------- LED & Button -----------------------
Adafruit_NeoPixel led(1, LED_PIN, NEO_RGB + NEO_KHZ800);
bool system_enabled = true;
volatile bool welding_now = false;

// -------------------- Battery monitoring -----------------
float vpack = 0.0;
float current_charge = 0.0;
unsigned long last_battery_read = 0;
const unsigned long BATTERY_READ_INTERVAL = 100;

const float V_NODE1_SCALE = 1.290;
const float V_NODE2_SCALE = 1.490;
const float VPACK_SCALE = 1.000;

// -------------------- Battery thresholds -----------------
const float CHARGE_LIMIT = 9.16;
const float CHARGE_RESUME = 8.70;
const float HARD_LIMIT = 9.2;
const float MIN_WELD_VOLTAGE = 0;

// -------------------- Weld settings ----------------------
uint8_t weld_mode = 1;  // 1=single, 2=double, 3=triple
uint16_t weld_d1 = 10;
uint16_t weld_gap1 = 0;
uint16_t weld_d2 = 0;
uint16_t weld_gap2 = 0;
uint16_t weld_d3 = 0;
uint8_t weld_power_pct = 100;  // 50-100%
bool preheat_enabled = false;
uint16_t preheat_ms = 20;
uint8_t preheat_pct = 30;

uint16_t weld_duration_ms = 10;  // Keep for backward compatibility
unsigned long last_weld_time = 0;
const unsigned long WELD_COOLDOWN = 500;

// SAFETY: Hard cap (can be adjusted for testing)
const uint16_t MAX_WELD_DURATION_MS = 200;

// -------------------- PWM Settings -----------------------
#define PWM_CHANNEL 0
#define PWM_FREQ 2000      // 2 kHz
#define PWM_RESOLUTION 10  // 10-bit (0-1023)

// -------------------- Thermistor -------------------------
float temperature_c = NAN;
float temp_ema = NAN;
float temp_last_valid = NAN;

const float SERIES_RESISTOR = 10000.0f;
const float THERMISTOR_NOMINAL = 173000.0f;
const float TEMPERATURE_NOMINAL = 20.0f;
const float BETA_COEFF = 3950.0f;
const float TEMP_EMA_ALPHA = 0.05f;
const float TEMP_OUTLIER_THRESH = 5.0f;

// -------------------- Forward declarations ---------------
void fireWeld(int durationMs);
void updateBattery();
void updateTemperature();
void controlCharger();
void processCommand(String cmd);
String buildStatus();
void sendToPi(const String& msg);

// -------------------- Cell reading helpers ---------------
bool readCellsOnce(float& V1, float& V2, float& V3, float& C1, float& C2,
                   float& C3) {
    if (xSemaphoreTake(i2c_mutex, pdMS_TO_TICKS(100)) != pdTRUE) {
        return false;
    }

    Wire.beginTransmission(0x41);
    Wire.write(0x02);
    Wire.endTransmission(false);
    Wire.requestFrom((uint8_t)0x41, (uint8_t)2);
    uint16_t raw1 = (Wire.read() << 8) | Wire.read();
    float v_node1_raw = raw1 * 0.00125;

    Wire.beginTransmission(0x44);
    Wire.write(0x02);
    Wire.endTransmission(false);
    Wire.requestFrom((uint8_t)0x44, (uint8_t)2);
    uint16_t raw2 = (Wire.read() << 8) | Wire.read();
    float v_node2_raw = raw2 * 0.00125;

    xSemaphoreGive(i2c_mutex);

    float v_node1 = v_node1_raw * V_NODE1_SCALE;
    float v_node2 = v_node2_raw * V_NODE2_SCALE;
    float v_pack = vpack;

    V1 = v_node1;
    V2 = v_node2;
    V3 = v_pack;

    C1 = v_node1;
    C2 = v_node2 - v_node1;
    C3 = v_pack - v_node2;

    return true;
}

// -------------------- Thermistor helpers -----------------
float readThermistorOnce() {
    int raw = analogRead(THERM_PIN);
    if (raw <= 0) raw = 1;
    if (raw >= 4095) raw = 4094;

    float v = 3.3f * ((float)raw / 4095.0f);
    float r_therm = SERIES_RESISTOR * (v / (3.3f - v));

    float steinhart = r_therm / THERMISTOR_NOMINAL;
    steinhart = logf(steinhart);
    steinhart /= BETA_COEFF;
    steinhart += 1.0f / (TEMPERATURE_NOMINAL + 273.15f);
    steinhart = 1.0f / steinhart;
    steinhart -= 273.15f;

    return steinhart;
}

void updateTemperature() {
    float t_raw = readThermistorOnce();
    if (!isfinite(t_raw)) return;

    if (!isfinite(temp_last_valid)) {
        temp_last_valid = t_raw;
        temp_ema = t_raw;
        temperature_c = t_raw;
        return;
    }

    float diff = fabsf(t_raw - temp_last_valid);
    if (diff > TEMP_OUTLIER_THRESH) return;

    temp_last_valid = t_raw;

    if (!isfinite(temp_ema)) {
        temp_ema = t_raw;
    } else {
        temp_ema = TEMP_EMA_ALPHA * t_raw + (1.0f - TEMP_EMA_ALPHA) * temp_ema;
    }

    temperature_c = temp_ema;
}

// ---- Helper: Build STATUS line -------------------
String buildStatus() {
    bool charge_on = digitalRead(FET_CHARGE);
    String state = charge_on ? "ON" : "OFF";

    // Temperature string
    String t_str;
    if (isfinite(temperature_c)) {
        t_str = String(temperature_c, 1);  // 1 decimal
    } else {
        t_str = "NaN";
    }

    unsigned long now = millis();
    long cooldown_ms = (long)(WELD_COOLDOWN - (now - last_weld_time));
    if (cooldown_ms < 0) cooldown_ms = 0;

    // Apply 0.2A threshold to ignore noise
    float display_current = (abs(current_charge) < 0.2) ? 0.0 : current_charge;

    String status = "STATUS";
    status += ",enabled=1";
    status += ",state=" + state;
    status += ",vpack=" + String(vpack, 3);
    status += ",i=" + String(display_current, 3);
    status += ",temp=" + t_str;
    status += ",cooldown_ms=" + String(cooldown_ms);
    status += ",pulse_ms=" + String(weld_duration_ms);
    status += ",power_pct=" + String(weld_power_pct);
    status += ",preheat_en=" + String(preheat_enabled ? 1 : 0);
    status += ",preheat_ms=" + String(preheat_ms);
    status += ",preheat_pct=" + String(preheat_pct);
    status += ",mode=" + String(weld_mode);
    status += ",d1=" + String(weld_d1);
    status += ",gap1=" + String(weld_gap1);
    status += ",d2=" + String(weld_d2);
    status += ",gap2=" + String(weld_gap2);
    status += ",d3=" + String(weld_d3);

    return status;
}

// -------------------- Helper: Send line to Pi ------------
void sendToPi(const String& msg) {
    if (client && client.connected()) {
        client.println(msg);
        Serial.printf("[TCP] TX: %s\n", msg.c_str());
    } else {
        Serial.printf("[TCP] Not connected, drop: %s\n", msg.c_str());
    }
}

// -------------------- Battery / Charger ------------------
void updateBattery() {
    if (xSemaphoreTake(i2c_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        // Read pack voltage (bus voltage)
        float raw_vpack = ina.readBusVoltage();
        vpack = raw_vpack * VPACK_SCALE;

        // Read shunt voltage directly (register 0x01)
        Wire.beginTransmission(0x40);
        Wire.write(0x01);  // Shunt voltage register
        Wire.endTransmission(false);
        Wire.requestFrom((uint8_t)0x40, (uint8_t)2);

        if (Wire.available() >= 2) {
            int16_t raw_shunt = (Wire.read() << 8) | Wire.read();

            // Convert to voltage: LSB = 2.5 µV
            float shunt_voltage_v = raw_shunt * 0.0000025;  // 2.5 µV per bit

            // Calculate current: I = V / R
            // R = 0.0002 Ω (0.2 mΩ)
            float calculated_current = shunt_voltage_v / 0.0002;

            // Negative because charging flows opposite direction
            current_charge = -calculated_current;

            // Debug output (remove after testing)
            static unsigned long last_debug = 0;
            if (millis() - last_debug >= 2000) {
                last_debug = millis();
                Serial.printf("🔍 Shunt: raw=0x%04X (%.6fV) → I=%.3fA\n",
                              (uint16_t)raw_shunt, shunt_voltage_v,
                              calculated_current);
            }
        }

        xSemaphoreGive(i2c_mutex);
    }
}
void controlCharger() {
    if (!system_enabled || welding_now) {
        digitalWrite(FET_CHARGE, LOW);
        return;
    }

    static int high_count = 0;

    if (vpack >= HARD_LIMIT) {
        high_count++;
    } else {
        high_count = 0;
    }

    if (high_count >= 2) {
        digitalWrite(FET_CHARGE, LOW);
        Serial.printf("⚠️ HARD LIMIT - Charging OFF (vpack=%.2fV)\n", vpack);
        return;
    }

    if (vpack >= CHARGE_LIMIT) {
        digitalWrite(FET_CHARGE, LOW);
    } else if (vpack < CHARGE_RESUME) {
        digitalWrite(FET_CHARGE, HIGH);
    }
}

// -------------------- Weld Logic (WITH PWM + PREHEAT) ----
void fireWeld(int durationMs) {
    if (!system_enabled) {
        Serial.println("WELD: blocked (system disabled)");
        return;
    }
    if (welding_now) {
        Serial.println("WELD: blocked (already welding)");
        return;
    }

    // SAFETY: Hard cap
    if (durationMs < 1) durationMs = 1;
    if (durationMs > MAX_WELD_DURATION_MS) {
        Serial.printf("⚠️ WELD: requested %d ms, capped to %d ms\n", durationMs,
                      MAX_WELD_DURATION_MS);
        durationMs = MAX_WELD_DURATION_MS;
    }

    welding_now = true;

    // HARD INTERLOCK: ensure charger is OFF
    digitalWrite(FET_CHARGE, LOW);
    ledcWrite(PWM_CHANNEL, 0);  // Ensure PWM is off
    delayMicroseconds(2000);    // deadtime

    uint32_t total_start_us = micros();

    // ========== PREHEAT PHASE (if enabled) ==========
    if (preheat_enabled && preheat_ms > 0) {
        Serial.printf("🔥 PREHEAT: %dms @ %d%%\n", preheat_ms, preheat_pct);
        // Set PWM duty for preheat
        uint16_t preheat_duty = (preheat_pct * 1023) / 100;
        ledcWrite(PWM_CHANNEL, preheat_duty);

        // Preheat pulse
        delayMicroseconds(preheat_ms * 1000UL);

        // Turn off between preheat and main pulse (5ms gap)
        ledcWrite(PWM_CHANNEL, 0);
        delayMicroseconds(5000);
    }

    // ========== MAIN WELD PULSE ==========
    Serial.printf("⚡ MAIN PULSE: %dms @ %d%%\n", durationMs, weld_power_pct);

    // Set PWM duty for main pulse
    uint16_t weld_duty = (weld_power_pct * 1023) / 100;
    ledcWrite(PWM_CHANNEL, weld_duty);

    // Main weld pulse (precise timing)
    uint32_t weld_start_us = micros();
    noInterrupts();
    delayMicroseconds(durationMs * 1000UL);
    asm volatile("nop");
    interrupts();
    uint32_t weld_actual_us = micros() - weld_start_us;

    // ========== CLEANUP ==========
    ledcWrite(PWM_CHANNEL, 0);  // Turn off PWM
    welding_now = false;

    uint32_t total_actual_us = micros() - total_start_us;

    Serial.printf(
        "✅ WELD_COMPLETE: main pulse = %lu us (%.2f ms), total = %lu us (%.2f "
        "ms)\n",
        (unsigned long)weld_actual_us, weld_actual_us / 1000.0f,
        (unsigned long)total_actual_us, total_actual_us / 1000.0f);

    last_weld_time = millis();

    // Send weld complete message to Pi
    String weld_msg = "WELD_COMPLETE";
    weld_msg += ",duration_ms=" + String(weld_actual_us / 1000.0f, 2);
    weld_msg += ",total_ms=" + String(total_actual_us / 1000.0f, 2);
    weld_msg += ",power_pct=" + String(weld_power_pct);
    if (preheat_enabled) {
        weld_msg += ",preheat_ms=" + String(preheat_ms);
        weld_msg += ",preheat_pct=" + String(preheat_pct);
    }
    sendToPi(weld_msg);
}

// ---- Command Parser ----
void processCommand(String cmd) {
    Serial.printf("[CMD] Processing: %s\n", cmd.c_str());

    if (cmd.startsWith("SET_PULSE,")) {
        // Parse: SET_PULSE,mode,d1,gap1,d2,gap2,d3
        int idx = 10;  // skip "SET_PULSE,"
        int commaPos;

        // mode
        commaPos = cmd.indexOf(',', idx);
        if (commaPos > 0) {
            weld_mode = cmd.substring(idx, commaPos).toInt();
            idx = commaPos + 1;
        }

        // d1
        commaPos = cmd.indexOf(',', idx);
        if (commaPos > 0) {
            weld_d1 = cmd.substring(idx, commaPos).toInt();
            idx = commaPos + 1;
        }

        // gap1
        commaPos = cmd.indexOf(',', idx);
        if (commaPos > 0) {
            weld_gap1 = cmd.substring(idx, commaPos).toInt();
            idx = commaPos + 1;
        }

        // d2
        commaPos = cmd.indexOf(',', idx);
        if (commaPos > 0) {
            weld_d2 = cmd.substring(idx, commaPos).toInt();
            idx = commaPos + 1;
        }

        // gap2
        commaPos = cmd.indexOf(',', idx);
        if (commaPos > 0) {
            weld_gap2 = cmd.substring(idx, commaPos).toInt();
            idx = commaPos + 1;
        }

        // d3 (last one, no comma)
        weld_d3 = cmd.substring(idx).toInt();

        // Update legacy weld_duration_ms for fireWeld()
        weld_duration_ms = weld_d1;

        Serial.printf(
            "✅ Pulse settings: mode=%d d1=%d gap1=%d d2=%d gap2=%d d3=%d\n",
            weld_mode, weld_d1, weld_gap1, weld_d2, weld_gap2, weld_d3);
        sendToPi("ACK:SET_PULSE," + String(weld_mode));
    } else if (cmd.startsWith("SET_POWER,")) {
        weld_power_pct = cmd.substring(10).toInt();
        if (weld_power_pct < 50) weld_power_pct = 50;
        if (weld_power_pct > 100) weld_power_pct = 100;
        Serial.printf("✅ Weld power set to %d%%\n", weld_power_pct);
        sendToPi("ACK:SET_POWER," + String(weld_power_pct));
    } else if (cmd.startsWith("SET_PREHEAT,")) {
        // Parse: SET_PREHEAT,enabled,duration,power
        int idx = 12;  // skip "SET_PREHEAT,"
        int commaPos;

        // enabled
        commaPos = cmd.indexOf(',', idx);
        if (commaPos > 0) {
            preheat_enabled = (cmd.substring(idx, commaPos).toInt() == 1);
            idx = commaPos + 1;
        }

        // duration
        commaPos = cmd.indexOf(',', idx);
        if (commaPos > 0) {
            preheat_ms = cmd.substring(idx, commaPos).toInt();
            idx = commaPos + 1;
        }

        // power (last one)
        preheat_pct = cmd.substring(idx).toInt();

        Serial.printf("✅ Preheat: %s, %dms, %d%%\n",
                      preheat_enabled ? "ON" : "OFF", preheat_ms, preheat_pct);
        sendToPi("ACK:SET_PREHEAT," + String(preheat_enabled ? 1 : 0));
    } else if (cmd == "FIRE") {
        Serial.println("⚠️ FIRE command ignored (pedal‑only mode)");
    } else if (cmd == "CHARGE_ON") {
        digitalWrite(FET_CHARGE, HIGH);
        Serial.println("✅ Charging ON (manual)");
        sendToPi("ACK:CHARGE_ON");
    } else if (cmd == "CHARGE_OFF") {
        digitalWrite(FET_CHARGE, LOW);
        Serial.println("✅ Charging OFF (manual)");
        sendToPi("ACK:CHARGE_OFF");
    } else if (cmd == "STATUS") {
        String status = buildStatus();
        sendToPi(status);
    }
}

// -------------------- LED update -------------------------
void updateLED() {
    if (!system_enabled) {
        led.setPixelColor(0, led.Color(255, 0, 0));  // Red = disabled
    } else if (welding_now) {
        led.setPixelColor(0, led.Color(255, 255, 255));  // White = welding
    } else if (digitalRead(FET_CHARGE)) {
        led.setPixelColor(0, led.Color(0, 0, 255));  // Blue = charging
    } else {
        led.setPixelColor(0, led.Color(0, 255, 0));  // Green = ready
    }
    led.show();
}

// -------------------- Button handler ---------------------
void handleButton() {
    static unsigned long press_start = 0;
    static unsigned long last_release = 0;
    static bool was_pressed = false;
    static bool waiting_for_double = false;
    static unsigned long double_tap_window = 0;

    bool is_pressed = (digitalRead(BUTTON_PIN) == LOW);

    if (is_pressed && !was_pressed) {
        press_start = millis();
    } else if (!is_pressed && was_pressed) {
        unsigned long press_duration = millis() - press_start;
        unsigned long time_since_last = millis() - last_release;

        if (press_duration >= 2000) {
            Serial.println("🔘 Long press → entering deep sleep");
            waiting_for_double = false;
            esp_deep_sleep_start();
        } else if (press_duration >= 50) {
            if (waiting_for_double && time_since_last <= 500) {
                Serial.println("🔘 Double tap → RESETTING ESP32...");
                delay(100);
                ESP.restart();
            } else {
                waiting_for_double = true;
                double_tap_window = millis();
            }
        }

        last_release = millis();
    }

    if (waiting_for_double && (millis() - double_tap_window > 500)) {
        system_enabled = !system_enabled;
        Serial.printf("🔘 Single tap → system %s\n",
                      system_enabled ? "ENABLED" : "DISABLED");

        if (!system_enabled) {
            digitalWrite(FET_CHARGE, LOW);
        }

        waiting_for_double = false;
    }

    was_pressed = is_pressed;
}

// -------------------- Setup ------------------------------
// -------------------- Setup ------------------------------
void setup() {
    Serial.begin(115200);
    delay(2000);

    // Create I²C mutex
    i2c_mutex = xSemaphoreCreateMutex();
    if (i2c_mutex == NULL) {
        Serial.println("❌ Failed to create I²C mutex!");
    } else {
        Serial.println("✅ I²C mutex created");
    }

    Serial.println();
    Serial.println("*****************************");
    Serial.println("***  SETUP() HAS STARTED  ***");
    Serial.println("*****************************");
    Serial.flush();

    Serial.println("\n=== ESP32 Weld Controller v3.1 - WITH PWM + PREHEAT ===");
    Serial.printf("⚠️ SAFETY: Max weld duration capped at %d ms\n",
                  MAX_WELD_DURATION_MS);

    // Set all OTHER pins first (NOT FET_CHARGE yet!)
    pinMode(FET_WELD, OUTPUT);
    digitalWrite(FET_WELD, LOW);
    pinMode(PEDAL_PIN, INPUT_PULLUP);
    pinMode(THERM_PIN, INPUT);
    pinMode(BUTTON_PIN, INPUT_PULLUP);
    pinMode(LED_PIN, OUTPUT);

    led.begin();
    led.setBrightness(50);
    led.setPixelColor(0, led.Color(255, 255, 0));  // Yellow = booting
    led.show();

    // Setup PWM for FET_WELD
    ledcSetup(PWM_CHANNEL, PWM_FREQ, PWM_RESOLUTION);
    ledcAttachPin(FET_WELD, PWM_CHANNEL);
    ledcWrite(PWM_CHANNEL, 0);
    Serial.printf("✅ PWM initialized: %d Hz, %d-bit resolution\n", PWM_FREQ,
                  PWM_RESOLUTION);

    analogReadResolution(12);

    Wire.begin(I2C_SDA, I2C_SCL);
    Wire.setClock(50000);
    Wire.setTimeOut(1000);

    Serial.println("\n=== I²C Device Scan ===");
    uint8_t addrs[] = {0x40, 0x44, 0x41};
    for (uint8_t addr : addrs) {
        Wire.beginTransmission(addr);
        uint8_t err = Wire.endTransmission();
        Serial.printf("I2C addr 0x%02X -> %s\n", addr,
                      err == 0 ? "OK" : "NO ACK");
        delay(10);
    }

    if (xSemaphoreTake(i2c_mutex, portMAX_DELAY) == pdTRUE) {
        if (ina.begin(&Wire)) {
            Serial.println("✅ INA226 (pack) initialized");

            // ==== MANUAL INA226 CONFIGURATION ====
            Wire.beginTransmission(0x40);
            Wire.write(0x00);
            Wire.write(0x45);
            Wire.write(0x27);
            Wire.endTransmission();
            delay(10);

            uint16_t cal_value = 41967;
            Wire.beginTransmission(0x40);
            Wire.write(0x05);
            Wire.write((cal_value >> 8) & 0xFF);
            Wire.write(cal_value & 0xFF);
            Wire.endTransmission();
            delay(10);

            Serial.printf("   Calibration: 0x%04X (%d)\n", cal_value,
                          cal_value);
            Serial.printf("   Expected: 0xA3EF (41967)\n");

            Wire.beginTransmission(0x40);
            Wire.write(0x00);
            Wire.endTransmission(false);
            Wire.requestFrom((uint8_t)0x40, (uint8_t)2);
            uint16_t config = (Wire.read() << 8) | Wire.read();
            Serial.printf("   INA Config: 0x%04X\n", config);
            delay(10);

            if (inaCell1.begin(&Wire)) {
                Serial.println("✅ INA226 cell1 (0x41) initialized");
            } else {
                Serial.println("⚠️ INA226 cell1 (0x41) init failed");
            }
            delay(10);

            if (inaCell2.begin(&Wire)) {
                Serial.println("✅ INA226 cell2 (0x44) initialized");
            } else {
                Serial.println("⚠️ INA226 cell2 (0x44) init failed");
            }
            delay(10);

            updateBattery();
            updateTemperature();
            Serial.printf("   Initial Vpack: %.2fV\n", vpack);
            Serial.printf("   Initial Current: %.2fA\n", current_charge);
            if (isfinite(temperature_c)) {
                Serial.printf("   Initial Temp: %.1fC\n", temperature_c);
            }

            controlCharger();
        } else {
            Serial.println("⚠️ INA226 init failed - charging disabled");
        }

        xSemaphoreGive(i2c_mutex);
    } else {
        Serial.println("❌ Failed to acquire I²C mutex in setup!");
    }

    Serial.println("\n=== INA Raw Debug ===");
    delay(100);

    if (xSemaphoreTake(i2c_mutex, portMAX_DELAY) == pdTRUE) {
        Wire.beginTransmission(0x40);
        Wire.write(0x02);
        Wire.endTransmission(false);
        Wire.requestFrom((uint8_t)0x40, (uint8_t)2);
        if (Wire.available() >= 2) {
            uint16_t raw = (Wire.read() << 8) | Wire.read();
            Serial.printf("0x40: raw=0x%04X  voltage=%.3fV\n", raw,
                          raw * 0.00125);
        }
        delay(10);

        Wire.beginTransmission(0x41);
        Wire.write(0x02);
        Wire.endTransmission(false);
        Wire.requestFrom((uint8_t)0x41, (uint8_t)2);
        if (Wire.available() >= 2) {
            uint16_t raw = (Wire.read() << 8) | Wire.read();
            Serial.printf("0x41: raw=0x%04X  voltage=%.3fV\n", raw,
                          raw * 0.00125);
        }
        delay(10);

        Wire.beginTransmission(0x44);
        Wire.write(0x02);
        Wire.endTransmission(false);
        Wire.requestFrom((uint8_t)0x44, (uint8_t)2);
        if (Wire.available() >= 2) {
            uint16_t raw = (Wire.read() << 8) | Wire.read();
            Serial.printf("0x44: raw=0x%04X  voltage=%.3fV\n", raw,
                          raw * 0.00125);
        }

        xSemaphoreGive(i2c_mutex);
    }

    Serial.println("====\n");

    // ⚡⚡⚡ WiFi INITIALIZATION (causes power surge) ⚡⚡⚡
    WiFi.mode(WIFI_STA);
    esp_wifi_set_protocol(
        WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

    Serial.print("Connecting to: ");
    Serial.println(ssid);

    WiFi.begin(ssid, password);
    WiFi.setTxPower(WIFI_POWER_8_5dBm);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 40) {
        delay(500);
        Serial.print(".");
        attempts++;

        if (attempts % 10 == 0) {
            Serial.print(" [");
            Serial.print((int)WiFi.status());
            Serial.print("] ");
        }
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\n✅ WiFi CONNECTED!");
        Serial.print("IP: ");
        Serial.println(WiFi.localIP());

        Serial.println("Starting TCP server on port 8888...");
        server.begin();
        server.setNoDelay(true);
    } else {
        Serial.println("\n❌ WiFi FAILED!");
    }

    // ⚡⚡⚡ NOW SET FET_CHARGE (after WiFi power surge is done) ⚡⚡⚡
    pinMode(FET_CHARGE, OUTPUT);
    digitalWrite(FET_CHARGE, LOW);
    Serial.println("✅ FET_CHARGE initialized (GPIO 5 set LOW)");

    Serial.println("\n=== WELD CONTROLLER READY ===");
    Serial.printf("Default settings:\n");
    Serial.printf("  Pulse: %dms @ %d%%\n", weld_duration_ms, weld_power_pct);
    Serial.printf("  Preheat: %s", preheat_enabled ? "ENABLED" : "DISABLED");
    if (preheat_enabled) {
        Serial.printf(" (%dms @ %d%%)", preheat_ms, preheat_pct);
    }
    Serial.println();
    Serial.println("====\n");
}

// -------------------- Loop -------------------------------
void loop() {
    if (WiFi.status() == WL_CONNECTED) {
        if (!client || !client.connected()) {
            // Clean up old connection
            if (client) {
                client.stop();
                Serial.println("[TCP] Old connection closed");
            }

            // Accept new connection
            WiFiClient newClient = server.available();
            if (newClient) {
                client = newClient;
                client.setNoDelay(true);
                Serial.println("[TCP] Client connected from " +
                               client.remoteIP().toString());

                // Send initial config on connect
                String config = buildStatus();
                sendToPi(config);
            }
        } else if (client.available()) {
            String line = client.readStringUntil('\n');
            line.trim();
            if (line.length() > 0) {
                Serial.printf("[TCP] RX: %s\n", line.c_str());
                processCommand(line);
            }
        }
    }

    if (millis() - last_battery_read >= BATTERY_READ_INTERVAL) {
        last_battery_read = millis();
        updateBattery();
        updateTemperature();
        controlCharger();

        static unsigned long last_print = 0;
        if (millis() - last_print >= 2000) {
            last_print = millis();

            // Determine charging state based on voltage thresholds
            bool should_charge =
                (vpack < CHARGE_RESUME && vpack < CHARGE_LIMIT);
            bool charging_active =
                should_charge && !welding_now && system_enabled;
            // Apply 0.2A threshold to ignore noise
            float display_current =
                (abs(current_charge) < 0.2) ? 0.0 : current_charge;

            Serial.printf("📊 Vpack=%.2fV I=%.2fA %s", vpack, current_charge,
                          charging_active ? "⚡CHARGING" : "⏸️IDLE");
            if (isfinite(temperature_c)) {
                Serial.printf("  Temp=%.1fC\n", temperature_c);
            } else {
                Serial.println();
            }

            String status = buildStatus();
            sendToPi(status);

            float V1, V2, V3, C1, C2, C3;
            if (readCellsOnce(V1, V2, V3, C1, C2, C3)) {
                char buf[160];
                snprintf(
                    buf, sizeof(buf),
                    "CELLS,V1=%.3f,V2=%.3f,V3=%.3f,C1=%.3f,C2=%.3f,C3=%.3f", V1,
                    V2, V3, C1, C2, C3);
                sendToPi(String(buf));
            }
        }
    }

    static int pedal_last_raw = HIGH;
    static int pedal_stable = HIGH;
    static unsigned long pedal_last_change_ms = 0;
    const unsigned long PEDAL_DEBOUNCE_MS = 40;

    int pedal_raw = digitalRead(PEDAL_PIN);
    unsigned long now = millis();

    if (pedal_raw != pedal_last_raw) {
        pedal_last_change_ms = now;
        pedal_last_raw = pedal_raw;
    }

    if ((now - pedal_last_change_ms) >= PEDAL_DEBOUNCE_MS) {
        if (pedal_raw != pedal_stable) {
            int prev_stable = pedal_stable;
            pedal_stable = pedal_raw;

            if (prev_stable == HIGH && pedal_stable == LOW) {
                if (now - last_weld_time >= WELD_COOLDOWN) {
                    Serial.println("🦶 Pedal pressed -> trigger weld");
                    fireWeld(weld_duration_ms);
                } else {
                    Serial.println("🦶 Pedal pressed but cooldown active");
                }
            }
        }
    }

    handleButton();
    updateLED();

    delay(10);
}