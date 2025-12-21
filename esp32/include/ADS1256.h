#ifndef ADS1256_H
#define ADS1256_H

#include <Arduino.h>
#include <SPI.h>

// ADS1256 Commands
#define CMD_WAKEUP   0x00
#define CMD_RDATA    0x01
#define CMD_RDATAC   0x03
#define CMD_SDATAC   0x0F
#define CMD_RREG     0x10
#define CMD_WREG     0x50
#define CMD_SELFCAL  0xF0
#define CMD_SYNC     0xFC
#define CMD_RESET    0xFE

// ADS1256 Registers
#define REG_STATUS   0x00
#define REG_MUX      0x01
#define REG_ADCON    0x02
#define REG_DRATE    0x03

// Data rates
#define DRATE_30000SPS 0xF0
#define DRATE_15000SPS 0xE0
#define DRATE_7500SPS  0xD0
#define DRATE_3750SPS  0xC0
#define DRATE_2000SPS  0xB0
#define DRATE_1000SPS  0xA1

// MUX settings
#define MUX_AIN2_AIN3 0x23  // P=AIN2, N=AIN3 (shunt via AMC1311)

class ADS1256 {
public:
    ADS1256(SPIClass* spi, uint8_t cs_pin, uint8_t drdy_pin);
    bool begin();
    float readCurrent();
    int32_t readRaw();
    
private:
    SPIClass* _spi;
    uint8_t _cs_pin;
    uint8_t _drdy_pin;
    
    // Calibration constants
    float Vref = 2.5;           // Internal reference
    float gain_adc = 1.0;       // PGA gain
    float FS = 8388607.0;       // 24-bit signed max
    float Rsh = 50e-6;          // 50 µΩ shunt
    float G_AMC = 1.0;          // AMC1311B gain
    float volts_per_code;
    
    void writeCmd(uint8_t cmd);
    void writeReg(uint8_t reg, uint8_t value);
    uint8_t readReg(uint8_t reg);
    void waitDRDY(uint32_t timeout_us = 100000);
    void csLow();
    void csHigh();
};

#endif
