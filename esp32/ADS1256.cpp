#include "ADS1256.h"

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
    CMD_SYNC = 0xFC,
    CMD_STANDBY = 0xFD
};

enum {
    REG_STATUS = 0x00,
    REG_MUX = 0x01,
    REG_ADCON = 0x02,
    REG_DRATE = 0x03,
    REG_IO = 0x04,
    REG_OFC0 = 0x05,
    REG_OFC1 = 0x06,
    REG_OFC2 = 0x07,
    REG_FSC0 = 0x08,
    REG_FSC1 = 0x09,
    REG_FSC2 = 0x0A
};

Ads1256::Ads1256(int csPin, int drdyPin)
    : _cs(csPin), _drdy(drdyPin), _spi(nullptr) {}

// ===================== Low–level helpers =====================
// NOTE: csLow(), csHigh(), drdyLow() are inline in ADS1256.h

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
    Serial.printf("ADS WREG reg=0x%02X val=0x%02X\n", reg, value);

    _spi->beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
    csLow();
    _spi->transfer(CMD_WREG | (reg & 0x0F));  // command + reg addr
    _spi->transfer(0x00);                     // write 1 register
    _spi->transfer(value);
    csHigh();
    _spi->endTransaction();
    delayMicroseconds(5);  // t11 guard time
}

uint8_t Ads1256::readRegister(uint8_t reg) {
    if (!_spi) return 0;

    _spi->beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
    csLow();
    _spi->transfer(CMD_RREG | (reg & 0x0F));  // command + reg addr
    _spi->transfer(0x00);                     // read 1 register
    delayMicroseconds(5);
    uint8_t v = _spi->transfer(0xFF);
    csHigh();
    _spi->endTransaction();

    Serial.printf("ADS RREG reg=0x%02X -> 0x%02X\n", reg, v);
    return v;
}

// In RDATAC mode we do NOT send CMD_RDATA—just clock out 24 bits
long Ads1256::readData() {
    if (!_spi) return 0;

    _spi->beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
    csLow();
    uint8_t b0 = _spi->transfer(0xFF);
    uint8_t b1 = _spi->transfer(0xFF);
    uint8_t b2 = _spi->transfer(0xFF);
    csHigh();
    _spi->endTransaction();

    long raw = ((long)b0 << 16) | ((long)b1 << 8) | b2;

    // Proper 24‑bit sign extension
    if (raw & 0x800000) {
        raw |= 0xFF000000;
    }

    return raw;
}

// ===================== Public API =====================

bool Ads1256::begin(SPIClass& spi) {
    _spi = &spi;

    pinMode(_cs, OUTPUT);
    pinMode(_drdy, INPUT);
    csHigh();

    // Kill any stray RDATAC first
    sendCommand(CMD_SDATAC);
    delay(2);

    sendCommand(CMD_RESET);
    delay(5);
    // Per datasheet: must be in SDATAC before WREG
    sendCommand(CMD_SDATAC);
    delay(5);

    uint8_t status = readRegister(REG_STATUS);
    Serial.printf("ADS STATUS after reset = 0x%02X\n", status);

    // DRATE: 0xF0 (30 kSPS), ADCON: 0x00 (PGA=1, CLKOUT off), MUX: AIN2–AIN3
    writeRegister(REG_DRATE, 0xF0);
    writeRegister(REG_ADCON, 0x00);
    writeRegister(REG_MUX, 0x23);

    uint8_t muxRead = readRegister(REG_MUX);
    uint8_t drateRd = readRegister(REG_DRATE);
    uint8_t adconRd = readRegister(REG_ADCON);
    Serial.printf("ADS init: MUX=0x%02X DRATE=0x%02X ADCON=0x%02X\n", muxRead,
                  drateRd, adconRd);

    // Self-calibration
    sendCommand(CMD_SELFCAL);
    unsigned long t0 = millis();
    while (!drdyLow() && (millis() - t0) < 1000) {
        delay(1);
    }

    return true;
}

void Ads1256::startContinuous(uint8_t ch) {
    if (!_spi) return;

    // Must be in SDATAC before changing config
    sendCommand(CMD_SDATAC);
    delayMicroseconds(5);

    // Ignore ch for now; force to AIN2–AIN3 diff (0x23)
    writeRegister(REG_MUX, 0x23);
    delayMicroseconds(10);

    uint8_t muxRead2 = readRegister(REG_MUX);
    Serial.printf("ADS MUX in startContinuous = 0x%02X\n", muxRead2);

    // Now enter RDATAC
    sendCommand(CMD_RDATAC);
}

void Ads1256::stopContinuous() {
    if (!_spi) return;
    sendCommand(CMD_SDATAC);
}

bool Ads1256::readCurrentFast(float& amps) {
    if (!_spi) return false;
    if (!drdyLow()) return false;  // no new sample yet

    long raw = readData();

    // TEMP DEBUG: print first few raw values
    static int dbgCount = 0;
    if (dbgCount < 20) {
        Serial.printf("ADS RAW=%ld\n", raw);
        dbgCount++;
    }

    // ADC scaling
    const float VREF = 2.5f;
    const float FS = 8388607.0f;  // 2^23 - 1

    // From datasheet: full-scale = ±2*VREF/PGA, here PGA=1 → ±5V
    float v_in = (float)raw * (VREF / FS);  // ~±2.5 V at code ±FS

    // Calibrated current scaling:
    // First pass: 0.7 A (ADS) -> 11.15 A  => 31860 A/V
    // Second pass: 11.4 A (ADS) -> 20.89 A => x1.833 factor
    static const float CURRENT_PER_VOLT = 58400.0f;  // A/V

    float current = v_in * CURRENT_PER_VOLT;

    amps = current;
    return true;
}