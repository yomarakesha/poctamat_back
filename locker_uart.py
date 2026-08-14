"""
Direct locker control over USB-TTL — talks straight to the red 24-channel board,
bypassing the terminal mainboard entirely.

ZERO dependencies. Uses the Windows Win32 serial API via ctypes (no pyserial,
no pip, works offline). Windows only.

Wiring (USB-TTL adapter -> red board '主机口' host header):
    adapter TX  -> board RXD
    adapter RX  -> board TXD   (needed only for sniff/replies)
    adapter GND -> board GND
    Do NOT connect the adapter's VCC. Board is powered by its own 24V.

Serial: 9600 8N1.

Frame (documented):
    0x8A  <addr>  <lock>  <cmd>  <xor>
        addr : board address. Board1 = 0x01 (cells 1-21), Board2 = 0x02 (22-43)
        lock : channel 1..24 on that board
        cmd  : 0x11 = open  (from recon notes, NOT yet confirmed on the wire)
        xor  : XOR of the 4 preceding bytes

Usage:
    py locker_uart.py --list                    # find the COM port
    py locker_uart.py --port COM3 --sniff       # listen only, learn real bytes
    py locker_uart.py --port COM3 --cell 5      # open logical cell 5
    py locker_uart.py --port COM3 --addr 1 --lock 5
    py locker_uart.py --port COM3 --cell 5 --dry  # print frame, send nothing
"""
import sys
import time
import argparse
import ctypes
from ctypes import wintypes

BAUD = 9600
START = 0x8A
CMD_OPEN = 0x11
BOARD1_MAX = 21   # cells 1..21 on board1
BOARD2_MAX = 43   # cells 22..43 on board2

# ---- Win32 constants ----
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
NOPARITY = 0
ONESTOPBIT = 0
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Declare prototypes so 64-bit HANDLEs are not truncated to 32-bit ints.
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetCommState.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
_k32.SetCommState.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
_k32.SetCommTimeouts.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
_k32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          wintypes.LPDWORD, wintypes.LPVOID]
_k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                           wintypes.LPDWORD, wintypes.LPVOID]


class DCB(ctypes.Structure):
    _fields_ = [
        ("DCBlength", wintypes.DWORD),
        ("BaudRate", wintypes.DWORD),
        ("fBits", wintypes.DWORD),   # packed bitfield (fBinary, fParity, ...)
        ("wReserved", wintypes.WORD),
        ("XonLim", wintypes.WORD),
        ("XoffLim", wintypes.WORD),
        ("ByteSize", ctypes.c_byte),
        ("Parity", ctypes.c_byte),
        ("StopBits", ctypes.c_byte),
        ("XonChar", ctypes.c_char),
        ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char),
        ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char),
        ("wReserved1", wintypes.WORD),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", wintypes.DWORD),
        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
        ("ReadTotalTimeoutConstant", wintypes.DWORD),
        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
        ("WriteTotalTimeoutConstant", wintypes.DWORD),
    ]


class Win32Serial:
    """Minimal 8N1 serial port using the Win32 API. Context-manager friendly."""

    def __init__(self, port, baud=BAUD, read_timeout_ms=200):
        name = port if port.upper().startswith("\\\\.\\") else "\\\\.\\" + port
        h = _k32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None,
                             OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise OSError(f"cannot open {port} (WinError {err}). "
                          f"Wrong port, busy, or driver missing?")
        self.h = h
        self._configure(baud)
        self._set_timeouts(read_timeout_ms)

    def _configure(self, baud):
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not _k32.GetCommState(self.h, ctypes.byref(dcb)):
            self.close()
            raise OSError("GetCommState failed")
        dcb.BaudRate = baud
        dcb.ByteSize = 8
        dcb.Parity = NOPARITY
        dcb.StopBits = ONESTOPBIT
        dcb.fBits = 0x0001  # fBinary=1, everything else off (no flow control)
        if not _k32.SetCommState(self.h, ctypes.byref(dcb)):
            self.close()
            raise OSError("SetCommState failed")

    def _set_timeouts(self, read_ms):
        t = COMMTIMEOUTS()
        t.ReadIntervalTimeout = 50
        t.ReadTotalTimeoutMultiplier = 0
        t.ReadTotalTimeoutConstant = read_ms
        t.WriteTotalTimeoutMultiplier = 0
        t.WriteTotalTimeoutConstant = 1000
        _k32.SetCommTimeouts(self.h, ctypes.byref(t))

    def reset_input(self):
        # PURGE_RXCLEAR | PURGE_RXABORT
        _k32.PurgeComm(self.h, 0x0008 | 0x0002)

    def write(self, data):
        written = wintypes.DWORD(0)
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        if not _k32.WriteFile(self.h, buf, len(data), ctypes.byref(written), None):
            raise OSError(f"WriteFile failed (WinError {ctypes.get_last_error()})")
        return written.value

    def read(self, n=64):
        buf = (ctypes.c_char * n)()
        got = wintypes.DWORD(0)
        if not _k32.ReadFile(self.h, buf, n, ctypes.byref(got), None):
            raise OSError(f"ReadFile failed (WinError {ctypes.get_last_error()})")
        return bytes(buf[:got.value])

    def close(self):
        if getattr(self, "h", None):
            _k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def list_ports():
    """Enumerate COM ports from the registry (no dependencies)."""
    import winreg
    ports = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except FileNotFoundError:
        print("No serial ports found. Plug in the USB-TTL adapter.")
        return
    try:
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            ports.append((value, name))
            i += 1
    finally:
        winreg.CloseKey(key)
    if not ports:
        print("No serial ports found. Plug in the USB-TTL adapter.")
        return
    print("Serial ports:")
    for dev, src in ports:
        print(f"  {dev:8}  ({src})")


# ---- protocol ----
def xor(byts):
    x = 0
    for b in byts:
        x ^= b
    return x


def build_open(addr, lock, cmd=CMD_OPEN):
    frame = bytes([START, addr, lock, cmd])
    return frame + bytes([xor(frame)])


def cell_to_addr_lock(cell):
    """Map logical cell (1..43) to (board_addr, channel). Verify with --sniff."""
    if 1 <= cell <= BOARD1_MAX:
        return 1, cell
    if BOARD1_MAX < cell <= BOARD2_MAX:
        return 2, cell - BOARD1_MAX
    raise ValueError(f"cell must be 1..{BOARD2_MAX}")


def hex_str(byts):
    return " ".join(f"{b:02X}" for b in byts)


def _annotate(buf):
    if len(buf) == 5 and buf[0] == START:
        addr, lock, cmd, chk = buf[1], buf[2], buf[3], buf[4]
        ok = "OK" if chk == xor(buf[:4]) else f"BAD (calc {xor(buf[:4]):02X})"
        print(f"           -> start=8A addr={addr} lock={lock} cmd=0x{cmd:02X} xor={ok}")


def sniff(port, baud=BAUD):
    """Passive listen. Sends nothing. Wire adapter RX -> board TXD, GND -> GND.
    Trigger opens from the native panel; real frames print here."""
    print(f"Sniffing {port} @ {baud} 8N1. Ctrl-C to stop.")
    print("Trigger opens from the native panel; real frames print below.\n")
    with Win32Serial(port, baud=baud, read_timeout_ms=200) as ser:
        buf = bytearray()
        last = time.time()
        try:
            while True:
                chunk = ser.read(64)
                now = time.time()
                if chunk:
                    buf.extend(chunk)
                    last = now
                elif buf and (now - last) > 0.05:
                    print(f"{time.strftime('%H:%M:%S')}  {hex_str(buf)}")
                    _annotate(buf)
                    buf.clear()
        except KeyboardInterrupt:
            if buf:
                print(f"{time.strftime('%H:%M:%S')}  {hex_str(buf)}")
            print("\nStopped.")


def send_open(port, addr, lock, cmd, dry, baud=BAUD):
    frame = build_open(addr, lock, cmd)
    print(f"frame: {hex_str(frame)}  (addr={addr} lock={lock} cmd=0x{cmd:02X}) @ {baud}")
    if dry:
        print("DRY: nothing sent.")
        return
    with Win32Serial(port, baud=baud, read_timeout_ms=200) as ser:
        ser.reset_input()
        ser.write(frame)
        time.sleep(0.15)
        reply = ser.read(64)
        if reply:
            print(f"reply: {hex_str(reply)}")
        else:
            print("no reply (many boards stay silent on open — check the lock).")


def main():
    ap = argparse.ArgumentParser(description="Direct USB-TTL locker control (stdlib only)")
    ap.add_argument("--port", help="serial port, e.g. COM3")
    ap.add_argument("--baud", type=int, default=BAUD, help=f"baud rate (default {BAUD})")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--sniff", action="store_true", help="passive listen, sends nothing")
    ap.add_argument("--cell", type=int, help="logical cell 1..43 (auto board+channel)")
    ap.add_argument("--addr", type=int, help="board address (1 or 2), with --lock")
    ap.add_argument("--lock", type=int, help="channel 1..24 on the board, with --addr")
    ap.add_argument("--cmd", type=lambda s: int(s, 0), default=CMD_OPEN,
                    help="command byte (default 0x11 open)")
    ap.add_argument("--dry", action="store_true", help="print frame, send nothing")
    args = ap.parse_args()

    if args.list:
        return list_ports()
    if not args.port:
        return ap.error("--port required (use --list to find it)")
    if args.sniff:
        return sniff(args.port, args.baud)

    if args.cell is not None:
        addr, lock = cell_to_addr_lock(args.cell)
    elif args.addr is not None and args.lock is not None:
        addr, lock = args.addr, args.lock
    else:
        return ap.error("give --cell N, or --addr A --lock L, or --sniff")

    send_open(args.port, addr, lock, args.cmd, args.dry, args.baud)


if __name__ == "__main__":
    main()
