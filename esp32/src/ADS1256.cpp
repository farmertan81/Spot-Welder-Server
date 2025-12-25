#include "ADS1256.h"

#include <limits.h>

// ---- ADS1256 command & register definitions ----
enum {
    CMD_WAKEUP = 0x00,
    CMD_RDATA = 0x01,
    CMD_RDATAC = 0x03,
    CMD_SDATAC = 0x0F,
    CMD_RREG = 0x10,
    CMD_WREG = 0x50,
    CMD_SELFCAL = 0xF0,
    CMD_RESET = 0xFE,
    CMD_SYNC = 0xFC
};

enum {
    REG_STATUS = 0x00,
    REG_MUX = 0x01,
    REG_ADCON = 0x02,
    REG_DRATE = 0x03,
};

// Raw-code zero offset (ADC codes at "no current")
static long g_raw_zero = 0;
static bool g_has_zero = false;

Ads1256::Ads1256(int csPin, int drdyPin)
    : _cs(csPin), _drdy(drdyPin), _spi(nullptr) {}

bool Ads1256::begin(SPIClass& spi) {
    _spi = &spi;

    pinMode(_cs, OUTPUT);
    pinMode(_drdy, INPUT);
    csHigh();

    delay(200);

    // Stop continuous mode (safe)
    sendCommand(CMD_SDATAC);
    delay(20);

    // Reset
    sendCommand(CMD_RESET);
    delay(200);

    // Stop continuous mode again
    sendCommand(CMD_SDATAC);
    delay(20);

    // SYNC + WAKEUP
    sendCommand(CMD_SYNC);
    delayMicroseconds(5);
    sendCommand(CMD_WAKEUP);
    delay(50);

    // Wait for DRDY low
    unsigned long timeout = millis();
    while (digitalRead(_drdy) == HIGH) {
        if (millis() - timeout > 1000) {
            Serial.println("❌ ADS1256: DRDY timeout during init");
            return false;
        }
        delay(1);
    }

    // Registers
    writeRegister(REG_DRATE, 0xA1);  // 10000 sps
    uint8_t dr = readRegister(REG_DRATE);
    Serial.printf("ADS1256 DRATE reg = 0x%02X (expect 0xA1)\n", dr);
    writeRegister(REG_ADCON, 0x00);  // PGA = 1
    writeRegister(REG_MUX,
                  0x23);  // CURRENT default: AIN2 - AIN3 (AMC1311 diff)

    // SYNC + WAKEUP after register config
    sendCommand(CMD_SYNC);
    delayMicroseconds(5);
    sendCommand(CMD_WAKEUP);
    delay(50);

    // Self-cal
    sendCommand(CMD_SELFCAL);
    timeout = millis();
    while (digitalRead(_drdy) == HIGH) {
        if (millis() - timeout > 2000) {
            Serial.println("❌ ADS1256: Calibration timeout");
            return false;
        }
        delay(1);
    }

    // Zero not yet calibrated
    g_has_zero = false;
    g_raw_zero = 0;

    Serial.println("✅ ADS1256: initialized at 10 SPS");
    return true;
}

void Ads1256::startContinuous(uint8_t ch) {
    if (!_spi) return;

    sendCommand(CMD_SDATAC);
    delayMicroseconds(5);

    // ch=0: voltage AIN0-AINCOM
    // ch=1: current  AIN2-AIN3
    if (ch == 0) {
        writeRegister(REG_MUX, 0x08);  // AIN0 - AINCOM
    } else {
        writeRegister(REG_MUX, 0x23);  // AIN2 - AIN3
    }
    delayMicroseconds(10);

    sendCommand(CMD_RDATAC);
}

void Ads1256::stopContinuous() {
    if (!_spi) return;
    sendCommand(CMD_SDATAC);
}

void Ads1256::switchToVoltageChannel() {
    stopContinuous();
    writeRegister(REG_MUX, 0x08);  // AIN0 - AINCOM
    sendCommand(CMD_SYNC);
    delayMicroseconds(5);
    sendCommand(CMD_WAKEUP);
    delayMicroseconds(5);
    startContinuous(0);
}

void Ads1256::switchToCurrentChannel() {
    stopContinuous();
    writeRegister(REG_MUX, 0x23);  // AIN2 - AIN3
    sendCommand(CMD_SYNC);
    delayMicroseconds(5);
    sendCommand(CMD_WAKEUP);
    delayMicroseconds(5);
    startContinuous(1);
}

// Median filter helper
float Ads1256::medianOf3(float a, float b, float c) {
    if (a > b) {
        if (b > c)
            return b;
        else if (a > c)
            return a;
        else
            return c;
    } else {
        if (a > c)
            return a;
        else if (b > c)
            return c;
        else
            return b;
    }
}

bool Ads1256::zeroCalibrateRaw(uint16_t samples) {
    if (!_spi) return false;

    long long sum = 0;
    uint16_t got = 0;

    for (uint16_t i = 0; i < samples; i++) {
        long raw = readDataContinuous();
        if (raw == LONG_MIN) continue;
        sum += raw;
        got++;
        delayMicroseconds(200);
    }

    if (got < (samples / 2)) {
        g_has_zero = false;
        return false;
    }

    g_raw_zero = (long)(sum / got);
    g_has_zero = true;
    return true;
}

bool Ads1256::readCurrentFast(float& amps) {
    if (!_spi) return false;

    long raw = readDataContinuous();
    if (raw == LONG_MIN) return false;  // no fresh sample
    if (!g_has_zero) return false;      // must zero-cal first

    // remove ADC baseline in codes
    raw -= g_raw_zero;

    const float VREF = 2.5f;
    const float FS = 8388607.0f;
    float v_adc = (float)raw * (VREF / FS);  // zeroed differential volts

    const float SHUNT_R = 0.00005f;  // 50 µΩ
    const float AMC_GAIN = 1.0f;

    float raw_current = v_adc / (AMC_GAIN * SHUNT_R);

    // Optional light filtering (keep your median-of-3 behavior)
    static float prev1 = 0;
    static float prev2 = 0;
    float filtered_current = medianOf3(prev2, prev1, raw_current);
    prev2 = prev1;
    prev1 = raw_current;

    amps = filtered_current;
    return true;
}

// ---- Low-level helpers ----

void Ads1256::sendCommand(uint8_t cmd) {
    if (!_spi) return;
    _spi->beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
    csLow();
    _spi->transfer(cmd);
    csHigh();
    _spi->endTransaction();
}

void Ads1256::writeRegister(uint8_t reg, uint8_t value) {
    if (!_spi) return;
    _spi->beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
    csLow();
    _spi->transfer(CMD_WREG | (reg & 0x0F));
    _spi->transfer(0x00);  // write 1 register
    _spi->transfer(value);
    csHigh();
    _spi->endTransaction();
    delayMicroseconds(5);
}

uint8_t Ads1256::readRegister(uint8_t reg) {
    if (!_spi) return 0;
    _spi->beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
    csLow();
    _spi->transfer(CMD_RREG | (reg & 0x0F));
    _spi->transfer(0x00);  // read 1 register
    delayMicroseconds(5);
    uint8_t v = _spi->transfer(0xFF);
    csHigh();
    _spi->endTransaction();
    return v;
}

long Ads1256::readData() {
    if (!_spi) return LONG_MIN;

    _spi->beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
    csLow();
    uint8_t b0 = _spi->transfer(0xFF);
    uint8_t b1 = _spi->transfer(0xFF);
    uint8_t b2 = _spi->transfer(0xFF);
    csHigh();
    _spi->endTransaction();

    long raw = ((long)b0 << 16) | ((long)b1 << 8) | b2;
    if (raw & 0x800000) raw |= 0xFF000000;  // sign extend 24-bit
    return raw;
}

    long Ads1256::readDataContinuous() {
        if (!_spi) return LONG_MIN;

        // Non-blocking: if no new conversion ready, return immediately
        if (digitalRead(_drdy) == HIGH) {
            return LONG_MIN;
        }

        _spi->beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
        csLow();
        uint8_t b0 = _spi->transfer(0xFF);
        uint8_t b1 = _spi->transfer(0xFF);
        uint8_t b2 = _spi->transfer(0xFF);
        csHigh();
        _spi->endTransaction();

        long raw = ((long)b0 << 16) | ((long)b1 << 8) | b2;
        if (raw & 0x800000) raw |= 0xFF000000;
        return raw;
}