#pragma once
#include <Arduino.h>
#include <SPI.h>

class Ads1256 {
   public:
    // csPin = chip select GPIO, drdyPin = DRDY GPIO
    Ads1256(int csPin, int drdyPin);

    // Initialize the ADS1256 on the given SPI bus (e.g. SPI, SPI1).
    // Returns true on success.
    bool begin(SPIClass& spi);

    // Start continuous (RDATAC) mode. The 'ch' parameter is currently unused,
    // but kept for API compatibility / future channel control.
    void startContinuous(uint8_t ch = 0);

    // Stop continuous (RDATAC) mode.
    void stopContinuous();

    // Non-blocking:
    //  - returns true if DRDY was low and a new sample was read.
    //  - 'amps' will then contain the converted current value.
    //  - returns false if DRDY was high (no new sample yet).
    bool readCurrentFast(float& amps);

   private:
    int _cs;
    int _drdy;
    SPIClass* _spi;

    inline void csLow() { digitalWrite(_cs, LOW); }
    inline void csHigh() { digitalWrite(_cs, HIGH); }

    void sendCommand(uint8_t cmd);
    void writeRegister(uint8_t reg, uint8_t value);
    uint8_t readRegister(uint8_t reg);

    // Read one 24-bit signed conversion result from the ADS1256.
    long readData();

    inline bool drdyLow() const { return digitalRead(_drdy) == LOW; }
};