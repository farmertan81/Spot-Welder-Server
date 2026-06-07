import struct
import time

class INA226Reader:
    def __init__(self, i2c, addr, vbus_scale=1.0):
        self.i2c = i2c
        self.addr = addr
        self.vbus_scale = vbus_scale
        self.VBUS_LSB = 0.00125
        
    def read_voltage(self):
        try:
            raw = struct.unpack('>H', self.i2c.readfrom_mem(self.addr, 0x02, 2))[0]
            return (raw * self.VBUS_LSB) * self.vbus_scale
        except:
            return None

def read_cells_ina226(i2c):
    """Read cell voltages from 3 INA226s"""
    VBUS_SCALE = {0x44: 0.9682, 0x41: 0.9977, 0x40: 0.9982}
    
    ina1 = INA226Reader(i2c, 0x44, VBUS_SCALE[0x44])
    ina2 = INA226Reader(i2c, 0x41, VBUS_SCALE[0x41])
    ina3 = INA226Reader(i2c, 0x40, VBUS_SCALE[0x40])
    
    V1 = ina1.read_voltage()
    V2 = ina2.read_voltage()
    V3 = ina3.read_voltage()
    
    if V1 is None or V2 is None or V3 is None:
        return None
        
    return {
        "V1": V1,
        "V2": V2,
        "V3": V3,
        "C1": V1,
        "C2": V2 - V1,
        "C3": V3 - V2
    }