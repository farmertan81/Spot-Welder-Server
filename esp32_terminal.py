#!/usr/bin/env python3
"""
ESP32 Direct Terminal
Send commands and see responses in real-time
"""
import serial
import sys
import time
import threading

PORT = '/dev/serial0'
BAUD = 115200

def read_thread(ser):
    """Continuously read and print ESP32 output"""
    while True:
        try:
            if ser.in_waiting:
                data = ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
                print(data, end='', flush=True)
            time.sleep(0.01)
        except:
            break

def main():
    print("=" * 60)
    print("ESP32 Direct Terminal")
    print("=" * 60)
    print(f"Port: {PORT} @ {BAUD} baud")
    print("Type commands and press Enter")
    print("Ctrl+C to exit")
    print("=" * 60)
    print()
    
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.1)
        time.sleep(0.5)
        ser.reset_input_buffer()
        
        # Start read thread
        reader = threading.Thread(target=read_thread, args=(ser,), daemon=True)
        reader.start()
        
        print("✅ Connected! Listening for ESP32 output...\n")
        
        while True:
            cmd = input()
            if cmd.strip():
                ser.write(f"{cmd}\n".encode())
                
    except KeyboardInterrupt:
        print("\n\n👋 Disconnected")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if 'ser' in locals():
            ser.close()

if __name__ == '__main__':
    main()
