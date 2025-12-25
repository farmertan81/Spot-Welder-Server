#pragma once
#include <Arduino.h>
#include <SPI.h>

class Ads1256 {
   public:
    Ads1256(int csPin, int drdyPin);

    bool begin(SPIClass& spi);
    void startContinuous(uint8_t ch = 0);
    void stopContinuous();

    // Channel switching for voltage/current measurements
    void switchToVoltageChannel();
    void switchToCurrentChannel();
    bool zeroCalibrateRaw(uint16_t samples = 200);

    // Non‑blocking: returns true if a new sample was read and 'amps' is valid.
    bool readCurrentFast(float& amps);
    bool readVoltageFast(float& volts);

    // Public debug access
    void sendCommand(uint8_t cmd);
    void writeRegister(uint8_t reg, uint8_t value);
    uint8_t readRegister(uint8_t reg);
    long readData();  // raw signed 24‑bit

   private:
    int _cs;
    int _drdy;
    SPIClass* _spi;
    float medianOf3(float a, float b, float c);

    void csLow() { digitalWrite(_cs, LOW); }
    void csHigh() { digitalWrite(_cs, HIGH); }

    // Read 24-bit data in continuous mode (no DRDY wait, no command overhead)
    long readDataContinuous();

    bool drdyLow() const { return digitalRead(_drdy) == LOW; }
};