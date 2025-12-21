#ifndef INA226_H
#define INA226_H

#include <Arduino.h>
#include <Wire.h>

// INA226 Registers
#define INA226_REG_CONFIG 0x00
#define INA226_REG_SHUNT_V 0x01
#define INA226_REG_BUS_V 0x02
#define INA226_REG_POWER 0x03
#define INA226_REG_CURRENT 0x04
#define INA226_REG_CALIBRATION 0x05

class INA226 {
   public:
    INA226(uint8_t addr) : _addr(addr), _wire(nullptr) {}

    bool begin(TwoWire* wire = &Wire) {
        _wire = wire;

        // Check if device responds
        _wire->beginTransmission(_addr);
        if (_wire->endTransmission() != 0) {
            return false;
        }

        // Reset
        writeRegister(INA226_REG_CONFIG, 0x8000);
        delay(20);

        // CONFIG: AVG=16, VBUSCT=1.1ms, VSHCT=1.1ms, MODE=cont shunt+bus
        uint16_t config = (0b010 << 9) | (0b100 << 6) | (0b100 << 3) | 0b111;
        writeRegister(INA226_REG_CONFIG, config);
        delay(10);

        return true;
    }

    void configure(uint16_t config) {
        writeRegister(INA226_REG_CONFIG, config);
    }

    void setCalibration(uint16_t cal_value) {
        writeRegister(INA226_REG_CALIBRATION, cal_value);
    }

    uint16_t getCalibration() { return readRegister(INA226_REG_CALIBRATION); }

    float readBusVoltage() {
        uint16_t raw = readRegister(INA226_REG_BUS_V);
        return raw * 0.00125;  // 1.25mV per bit
    }

    float readShuntVoltage() {
        uint16_t raw = readRegister(INA226_REG_SHUNT_V);
        int16_t signed_raw = (int16_t)raw;
        return signed_raw * 0.0025;  // 2.5µV per bit, return in mV
    }

    float readCurrent() {
        uint16_t raw = readRegister(INA226_REG_CURRENT);
        int16_t signed_raw = (int16_t)raw;
        // Current LSB depends on calibration
        // For 0.2mΩ shunt, 20A max: LSB = 0.0006103515625 A
        return signed_raw * 0.0006103515625;
    }

    float readPower() {
        uint16_t raw = readRegister(INA226_REG_POWER);
        // Power LSB = 25 * Current LSB
        return raw * 0.0006103515625 * 25.0;
    }

   private:
    TwoWire* _wire;
    uint8_t _addr;

    void writeRegister(uint8_t reg, uint16_t value) {
        _wire->beginTransmission(_addr);
        _wire->write(reg);
        _wire->write((value >> 8) & 0xFF);
        _wire->write(value & 0xFF);
        _wire->endTransmission();
    }

    uint16_t readRegister(uint8_t reg) {
        _wire->beginTransmission(_addr);
        _wire->write(reg);
        _wire->endTransmission(false);

        _wire->requestFrom(_addr, (uint8_t)2);
        if (_wire->available() >= 2) {
            uint16_t value = _wire->read() << 8;
            value |= _wire->read();
            return value;
        }
        return 0;
    }
};

#endif