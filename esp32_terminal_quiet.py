#!/usr/bin/env python3
"""
ESP32 Direct Terminal (Quiet Mode)
Filters out DBG messages so you can see command responses
"""
import serial
import sys
import time
import threading

PORT = '/dev/serial0'
BAUD = 115200

def read_thread(ser):
    """Read and print ESP32 output (filter DBG)"""
    while True:
        try:
            if ser.in_waiting:
                data = ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
                # Filter out DBG lines
                for line in data.split('\n'):
                    if line.strip() and not line.startswith('DBG'):
                        print(line)
            time.sleep(0.01)
        except:
            break

def main():
    print("=" * 60)
    print("ESP32 Direct Terminal (Quiet Mode)")
    print("=" * 60)
    print("DBG messages filtered out")
    print("Type commands and press Enter")
    print("Ctrl+C to exit")
    print("=" * 60)
    print()
    
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.1)
        time.sleep(0.5)
        ser.reset_input_buffer()
        
        reader = threading.Thread(target=read_thread, args=(ser,), daemon=True)
        reader.start()
        
        print("✅ Connected!\n")
        
        while True:
            cmd = input("CMD> ")
            if cmd.strip():
                print(f"  → Sending: {cmd}")
                ser.write(f"{cmd}\n".encode())
                time.sleep(0.2)  # Wait for response
                
    except KeyboardInterrupt:
        print("\n\n👋 Disconnected")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if 'ser' in locals():
            ser.close()

if __name__ == '__main__':
    main()
