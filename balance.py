"""Read a lab balance over a serial (RS-232 or USB) port.

Two kinds of balance are supported, at 9600 baud, 8 data bits, no parity, 1 stop bit:

- **Mettler Toledo** (MT-SICS): the app asks for each reading with ``SI`` ("send the
  weight now"), so no special balance setting is needed besides the baud rate.
- **Bonvoisin**: the balance sends readings by itself. Set it to continuous output
  ("Continuous", "Auto-Print" or "Stream"). Other balances that print a number and a
  unit on each line usually work with this setting too.

The parsing follows Balance-Streamer-GUI. Readings are kept in grams. A background
thread does the reading, so callers just look at :meth:`SerialBalance.samples`.
No Qt here, so it can be tested on its own. pyserial is imported only when a real
port is opened.
"""

import collections
import logging
import re
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("JebaoGUI.balance")

BAUD = 9600
BRANDS = {
    "mettler": "Mettler Toledo (MT-SICS)",
    "bonvoisin": "Bonvoisin (continuous output)",
}
_TO_GRAMS = {"g": 1.0, "mg": 0.001, "kg": 1000.0}
_EOL = re.compile(rb"\r\n|\r|\n")


@dataclass
class Reading:
    grams: float
    stable: bool = True


class BalanceError(Exception):
    """A problem with the balance, with a message that can be shown as is."""


# --- parsing one line --------------------------------------------------------------------

_MT = re.compile(r"^S\s+([SD])\s+([-+]?\s*\d+(?:\.\d+)?)\s+(\S+)")
_NUMBER = re.compile(r"([-+]?)\s*(\d+(?:\.\d+)?)")
_UNIT = re.compile(r"([a-zA-Z]+)\s*$")


def _grams(value, unit):
    factor = _TO_GRAMS.get((unit or "g").strip().lower())
    if factor is None:
        raise BalanceError(f"The balance is showing {unit!r}. Set it to grams.")
    return value * factor


def parse_mt_sics(line):
    """One reply to ``SI``, e.g. ``S S     100.123 g`` (stable) or ``S D ...`` (still moving).

    Returns a Reading, or None for a line that isn't a weight. Raises BalanceError for
    replies that mean the balance can't weigh (over or under its range).
    """
    line = (line or "").strip()
    m = _MT.match(line)
    if m:
        status, value, unit = m.groups()
        return Reading(_grams(float(value.replace(" ", "")), unit), status == "S")
    if line in ("S +", "S+"):
        raise BalanceError("The balance is overloaded. Empty the beaker or use a larger balance.")
    if line in ("S -", "S-"):
        raise BalanceError("The balance is below zero range. Check the pan, then tare it.")
    return None


def parse_stream(line):
    """A line a balance sends by itself, e.g. ``+  12.345 g`` or ``ST,GS,+0012.345,g``.

    Lines marked unstable (``US``) count as not stable. Lines with no number give None.
    """
    line = (line or "").strip()
    m = _NUMBER.search(line)
    if not m:
        return None
    sign, digits = m.groups()
    value = float(digits) * (-1 if sign == "-" else 1)
    tail = line[m.end() :]
    u = _UNIT.search(tail)
    stable = not line.upper().startswith("US")
    return Reading(_grams(value, u.group(1) if u else "g"), stable)


# --- ports -------------------------------------------------------------------------------


def list_ports():
    """[(device, description)] for the serial ports on this computer ([] if pyserial is missing)."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    ports = []
    for p in sorted(lp.comports(), key=lambda p: p.device):
        desc = p.description if p.description and p.description != "n/a" else ""
        ports.append((p.device, desc))
    return ports


# --- reading -----------------------------------------------------------------------------


class SerialBalance:
    """Reads a balance in a background thread and keeps the recent readings.

    ``samples()`` gives (time, grams, stable) tuples, with ``time`` from time.monotonic().
    """

    KEEP = 20000  # about an hour at 5 readings a second

    def __init__(self, port, brand, clock=time.monotonic):
        if brand not in BRANDS:
            raise ValueError(f"Unknown balance type {brand!r}")
        self.port, self.brand = port, brand
        self.clock = clock
        self._ser = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._samples = collections.deque(maxlen=self.KEEP)
        self.error = ""  # the last problem, in plain words ("" when fine)

    # opening and closing

    def open(self):
        try:
            import serial
        except ImportError as e:
            raise BalanceError(
                "The serial-port package (pyserial) isn't installed. Run Setup again, "
                "or: python -m pip install -e ."
            ) from e
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=BAUD,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.3,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except (serial.SerialException, OSError, ValueError) as e:
            raise BalanceError(f"Couldn't open {self.port}: {e}") from e
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, name=f"balance {self.port}", daemon=True)
        self._thread.start()
        log.info("Balance connected on %s (%s).", self.port, BRANDS[self.brand])

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001 - the port may already be gone
                log.debug("Closing %s failed", self.port, exc_info=True)
            self._ser = None

    @property
    def is_open(self):
        return self._thread is not None and self._thread.is_alive()

    # the reading thread

    def _read_loop(self):
        parse = parse_mt_sics if self.brand == "mettler" else parse_stream
        lines = self._poll_lines() if self.brand == "mettler" else self._stream_lines()
        complained = False
        while not self._stop.is_set():
            try:
                line = next(lines)
                if line is None:
                    continue
                reading = parse(line)
                if reading is None:
                    continue
                self.add(reading)
                self.error = ""
                complained = False
            except BalanceError as e:
                self.error = str(e)
                time.sleep(0.2)
            except Exception as e:  # noqa: BLE001 - unplugged cable, port gone, ...
                self.error = f"Lost contact with the balance on {self.port}: {e}"
                if not complained:
                    log.warning("Reading the balance on %s failed; retrying", self.port, exc_info=True)
                    complained = True
                time.sleep(0.5)
                lines = self._poll_lines() if self.brand == "mettler" else self._stream_lines()

    def _poll_lines(self):
        """Mettler Toledo: ask with SI, then wait (up to 1 s) for one whole reply line."""
        while True:
            self._ser.reset_input_buffer()
            self._ser.write(b"SI\r\n")
            buf = b""
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not _EOL.search(buf):
                buf += self._ser.read(max(1, self._ser.in_waiting))
            m = _EOL.search(buf)
            yield buf[: m.start()].decode("ascii", errors="ignore") if m else None

    def _stream_lines(self):
        """Balances that send by themselves: only whole lines count. The first one is
        dropped, since the balance may have been half-way through it when we connected."""
        buf = b""
        first = True
        while True:
            buf += self._ser.read(max(1, self._ser.in_waiting))
            *done, buf = _EOL.split(buf)
            if len(buf) > 4096:  # no line ends at all: wrong settings, start over
                buf = b""
            got = False
            for raw in done:
                if first:
                    first = False
                    continue
                if raw.strip():
                    got = True
                    yield raw.decode("ascii", errors="ignore")
            if not got:
                yield None

    # what callers use

    def add(self, reading):
        with self._lock:
            self._samples.append((self.clock(), reading.grams, reading.stable))

    def samples(self, since=None):
        """[(time, grams, stable)], all of them or those at or after ``since``."""
        with self._lock:
            data = list(self._samples)
        if since is None:
            return data
        return [s for s in data if s[0] >= since]

    def latest(self):
        with self._lock:
            return self._samples[-1] if self._samples else None

    def fresh(self, max_age=2.0):
        """True if a reading arrived in the last ``max_age`` seconds."""
        last = self.latest()
        return last is not None and self.clock() - last[0] <= max_age


class SimulatedBalance(SerialBalance):
    """A pretend balance for tests: liquid arrives while ``flowing`` is set.

    ``tick()`` adds one reading at the current clock time. ``rate_g_s`` is the flow while
    on; ``noise`` adds a fixed wobble pattern.
    """

    def __init__(self, clock, rate_g_s=0.04, start_g=150.0, noise=0.0):
        super().__init__("SIM", "bonvoisin", clock=clock)
        self.mass = start_g
        self.rate_g_s = rate_g_s
        self.noise = noise
        self.flowing = False
        self._last = clock()
        self._n = 0
        self.opened = False

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    @property
    def is_open(self):
        return self.opened

    def tick(self):
        now = self.clock()
        if self.flowing:
            self.mass += self.rate_g_s * (now - self._last)
        self._last = now
        self._n += 1
        wobble = self.noise * ((self._n * 7919) % 13 - 6) / 6.0
        self.add(Reading(self.mass + wobble, True))
