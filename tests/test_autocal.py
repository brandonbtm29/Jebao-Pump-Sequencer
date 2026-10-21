"""Tests for balance.py (parsing) and autocal.py (the automatic calibration routine).

The pump and balance are pretend ones driven by a fast clock, so a 4-minute run takes
milliseconds.
"""

import asyncio
import csv

import pytest

import autocal
import balance

# --- parsing balance lines ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "grams", "stable"),
    [
        ("S S     100.123 g\r\n", 100.123, True),
        ("S D      -0.050 g", -0.05, False),
        ("S S   1250.5 mg", 1.2505, True),
        ("S S  0.25010 kg", 250.1, True),
    ],
)
def test_mettler_lines(line, grams, stable):
    r = balance.parse_mt_sics(line)
    assert r.grams == pytest.approx(grams) and r.stable is stable


def test_mettler_other_replies():
    assert balance.parse_mt_sics("ES") is None
    assert balance.parse_mt_sics("S I") is None  # busy
    with pytest.raises(balance.BalanceError, match="overloaded"):
        balance.parse_mt_sics("S +")
    with pytest.raises(balance.BalanceError, match="grams"):
        balance.parse_mt_sics("S S   1.0 lb")


@pytest.mark.parametrize(
    ("line", "grams", "stable"),
    [
        ("  12.345 g\r\n", 12.345, True),
        ("-  1.20 g", -1.2, True),  # sign separated from the digits
        ("ST,GS,+0012.345,g", 12.345, True),
        ("US,GS,+0012.300,g", 12.3, False),
        ("150", 150.0, True),  # no unit: grams
    ],
)
def test_streamed_lines(line, grams, stable):
    r = balance.parse_stream(line)
    assert r.grams == pytest.approx(grams) and r.stable is stable


def test_streamed_junk():
    assert balance.parse_stream("") is None
    assert balance.parse_stream("----") is None


def test_list_ports_works_without_hardware():
    assert isinstance(balance.list_ports(), list)


# --- the maths ---------------------------------------------------------------------------


def test_fit_line():
    ts = [0, 1, 2, 3, 4]
    slope, intercept, r2 = autocal.fit_line(ts, [1 + 2 * t for t in ts])
    assert slope == pytest.approx(2) and intercept == pytest.approx(1) and r2 == pytest.approx(1)
    _s, _i, r2 = autocal.fit_line(ts, [0, 5, 1, 6, 2])
    assert r2 < 0.5
    with pytest.raises(ValueError):
        autocal.fit_line([1], [1])


def test_settings_round_trip():
    s = autocal.Settings(volume_ml=5, retries=3, auto_save=False)
    back = autocal.Settings.from_dict({**s.to_dict(), "junk": 1, "min_r2": "bad"})
    assert back.volume_ml == 5 and back.retries == 3 and back.auto_save is False and back.min_r2 == 0.99


# --- a pretend rig -----------------------------------------------------------------------


class Rig:
    """A fast clock, a pretend balance and pretend pumps (one flow rate per head)."""

    def __init__(self, rates, noise=0.0, start_delay=0.0, stop_delay=0.0, ack_ok=True, off_ok=True):
        self.t = 0.0
        self.bal = autocal_balance = balance.SimulatedBalance(clock=self.clock, noise=noise)
        autocal_balance.tick()
        self.rates = rates  # (did, ch) -> g/s
        self.start_delay, self.stop_delay = start_delay, stop_delay
        self.ack_ok, self.off_ok = ack_ok, off_ok
        self.commands = []
        self.on_at = self.off_at = None
        self.change_at = None  # (time after ON, new g/s): flow that changes mid-run
        self.saved = {}

    def clock(self):
        return self.t

    async def sleep(self, dt):
        steps = max(1, int(round(dt / 0.1)))
        for _ in range(steps):
            self.t += dt / steps
            if self.on_at is not None and self.t >= self.on_at:
                self.bal.flowing = True
                if self.change_at and self.t >= self.on_at + self.change_at[0]:
                    self.bal.rate_g_s = self.change_at[1]
            if self.off_at is not None and self.t >= self.off_at:
                self.bal.flowing = False
                self.on_at = self.off_at = None
            self.bal.tick()
        await asyncio.sleep(0)

    async def set_head(self, did, ch, on):
        self.commands.append((did, ch, on))
        if on:
            if not self.ack_ok:
                return False
            self.bal.rate_g_s = self.rates[(did, ch)]
            self.on_at = self.t + self.start_delay
            return True
        if not self.off_ok:
            return False
        if self.stop_delay:
            self.off_at = self.t + self.stop_delay  # the cloud passes OFF on late
        else:
            self.on_at = None
            self.bal.flowing = False
        return True

    def save(self, did, ch, rate):
        self.saved[(did, ch)] = rate

    def calibrator(self, **settings):
        s = autocal.Settings(**{"volume_ml": 2.0, **settings})
        cal = autocal.AutoCalibrator(
            self.bal, self.set_head, s, save=self.save, clock=self.clock, sleep=self.sleep
        )
        for did, ch in self.rates:
            cal.add(did, ch, f"{did} · CH{ch}", old_rate=1.0)
        return cal


def run(cal):
    asyncio.run(cal.run())
    return cal


def test_a_good_head_passes_and_is_saved():
    rig = Rig({("A", 1): 0.04, ("A", 2): 0.08}, noise=0.002)
    cal = run(rig.calibrator())
    a1, a2 = cal.results
    assert a1.status == a2.status == autocal.SAVED
    assert a1.rate == pytest.approx(0.04 / 0.997 * 60, rel=0.01)
    assert a2.rate == pytest.approx(0.08 / 0.997 * 60, rel=0.01)
    assert rig.saved[("A", 1)] == pytest.approx(a1.rate)
    att = a1.last
    assert att.r2 > 0.99 and att.diff_pct < 2 and len(a1.attempts) == 1
    assert att.volume_ml == pytest.approx(2.0, rel=0.1)  # ran until about 2 mL arrived
    assert rig.commands == [("A", 1, True), ("A", 1, False), ("A", 2, True), ("A", 2, False)]  # one at a time
    assert cal.finished and cal.current is None


def test_without_auto_save_it_waits_for_you():
    rig = Rig({("A", 1): 0.04})
    cal = run(rig.calibrator(auto_save=False))
    assert cal.results[0].status == autocal.PASSED and rig.saved == {}


def test_cloud_delays_are_measured_and_dont_change_the_rate():
    # Like Brandon's CH4: the pump starts 5 s after ON is confirmed and keeps going 19 s
    # after OFF is sent. The flow rate (slope) is still right, the delays are reported,
    # and the Δmass rate (commanded time) shows the mismatch for the record.
    rig = Rig({("A", 1): 0.55}, noise=0.003, start_delay=5.0, stop_delay=19.0)
    cal = run(rig.calibrator(volume_ml=100, ignore_s=15, settle_s=30, tolerance_pct=2))
    r = cal.results[0]
    a = r.last
    assert r.status == autocal.SAVED and len(r.attempts) == 1
    assert a.slope_rate == pytest.approx(0.55 / 0.997 * 60, rel=0.002)
    assert rig.saved[("A", 1)] == pytest.approx(a.slope_rate)
    assert a.start_delay == pytest.approx(5.0, abs=0.3)
    assert a.stop_delay == pytest.approx(19.0, abs=0.3)
    assert a.dmass_rate > a.slope_rate * 1.05  # 14 s of extra flow over a ~3 min run
    assert a.delay_note == "started 5 s late, stopped 19 s late"
    # the end mass was taken after the late stop, so nothing was missed
    assert a.end_g - a.start_g == pytest.approx(0.55 * (a.on_time + 14), rel=0.005)


def test_late_stop_longer_than_settle_is_waited_out():
    # Settle is 5 s but the pump only stops 19 s after OFF: the routine keeps waiting
    # until the reading is still, so the next head starts on a still balance.
    rig = Rig({("A", 1): 0.1, ("A", 2): 0.1}, stop_delay=19.0)
    cal = run(rig.calibrator(volume_ml=5, settle_s=5, ignore_s=2))
    first, second = cal.results
    assert first.status == second.status == autocal.SAVED
    assert first.last.stop_delay == pytest.approx(19.0, abs=0.5)
    assert second.last.start_delay == pytest.approx(0.0, abs=0.5)


def test_flow_that_changes_mid_run_fails():
    rig = Rig({("A", 1): 0.05})
    rig.change_at = (60.0, 0.04)  # e.g. a bubble or the tubing slipping, a minute in
    cal = run(rig.calibrator(volume_ml=6, ignore_s=3, retries=0))
    r = cal.results[0]
    assert r.status == autocal.FAILED and "first and second half" in r.note
    assert r.last.steady_pct > 2


def test_noisy_balance_fails_on_r2():
    rig = Rig({("A", 1): 0.01}, noise=0.1)
    cal = run(rig.calibrator(volume_ml=0.5, retries=0))
    r = cal.results[0]
    assert r.status == autocal.FAILED and "R²" in r.note and len(r.attempts) == 1


def test_no_flow_fails_that_head_and_goes_on():
    rig = Rig({("A", 1): 0.0, ("A", 2): 0.04})
    cal = run(rig.calibrator())
    bad, good = cal.results
    assert bad.status == autocal.FAILED and "No flow" in bad.note and len(bad.attempts) == 1
    assert good.status == autocal.SAVED
    assert ("A", 1, False) in rig.commands


def test_stop_turns_the_head_off():
    rig = Rig({("A", 1): 0.04, ("A", 2): 0.04})
    cal = rig.calibrator()

    async def go():
        task = asyncio.ensure_future(cal.run())
        while cal.phase != "running":
            await asyncio.sleep(0)
        cal.stop()
        await task

    asyncio.run(go())
    assert rig.commands[-1] == ("A", 1, False)
    assert [r.status for r in cal.results] == [autocal.STOPPED, autocal.STOPPED]
    assert rig.saved == {}


def test_pump_that_wont_start_stops_everything():
    rig = Rig({("A", 1): 0.04, ("A", 2): 0.04}, ack_ok=False)
    cal = run(rig.calibrator())
    assert cal.results[0].status == autocal.FAILED and "Couldn't start" in cal.results[0].note
    assert cal.results[1].status == autocal.STOPPED
    # the ON may have reached the pump even though it timed out, so OFF is sent anyway
    assert rig.commands == [("A", 1, True), ("A", 1, False)]


def test_busy_head_is_skipped():
    rig = Rig({("A", 1): 0.04, ("A", 2): 0.04})
    cal = rig.calibrator()
    cal.is_busy = lambda did, ch: ch == 1
    run(cal)
    assert cal.results[0].status == autocal.FAILED and "Busy" in cal.results[0].note
    assert cal.results[1].status == autocal.SAVED
    assert ("A", 1, True) not in rig.commands


def test_a_leftover_pause_does_not_hang_the_next_run():
    rig = Rig({("A", 1): 0.04})
    cal = rig.calibrator()

    async def pause_during_last_head():
        task = asyncio.ensure_future(cal.run())
        while cal.phase != "running":
            await asyncio.sleep(0)
        cal.pause_after_head()  # pressed during the last head: nothing left to pause before
        await task

    asyncio.run(pause_during_last_head())
    cal.requeue(cal.results[0])
    asyncio.run(asyncio.wait_for(cal.run(), 5))  # Redo: must not wait for a Resume
    assert cal.results[0].status == autocal.SAVED


def test_save_error_keeps_the_result():
    rig = Rig({("A", 1): 0.04})
    cal = rig.calibrator()

    def broken(*_a):
        raise OSError("disk full")

    cal.save = broken
    run(cal)
    r = cal.results[0]
    assert r.status == autocal.PASSED and "saving failed" in r.note


def test_pump_that_wont_stop_is_reported_loudly():
    rig = Rig({("A", 1): 0.04}, off_ok=False)
    cal = run(rig.calibrator())
    assert "Couldn't stop" in cal.results[0].note
    assert [c for c in rig.commands if c[2] is False] == [("A", 1, False)] * 3  # tried three times


def test_silent_balance_stops_the_run():
    rig = Rig({("A", 1): 0.04})
    rig.bal.tick = lambda: None  # no more readings
    cal = run(rig.calibrator())
    assert cal.results[0].status == autocal.FAILED and "stopped sending" in cal.results[0].note
    assert ("A", 1, True) not in rig.commands


def test_pause_after_head_and_requeue():
    rig = Rig({("A", 1): 0.04, ("A", 2): 0.04})
    cal = rig.calibrator()

    async def go():
        task = asyncio.ensure_future(cal.run())
        while cal.phase != "running":
            await asyncio.sleep(0)
        cal.pause_after_head()  # the first head finishes, then it waits
        while not cal.paused:
            await asyncio.sleep(0)
        assert cal.results[0].status == autocal.SAVED and ("A", 2, True) not in rig.commands
        cal.pause_after_head(False)
        await task

    asyncio.run(go())
    assert all(r.status == autocal.SAVED for r in cal.results)
    cal.requeue(cal.results[0])
    assert (
        cal.results[-1].did == "A" and cal.results[-1].ch == 1 and cal.results[-1].status == autocal.WAITING
    )
    run(cal)
    assert cal.results[-1].status == autocal.SAVED


def test_estimates():
    s = autocal.Settings(volume_ml=10, settle_s=5)
    # 10 mL at 2.5 mL/min = 240 s, plus 12 s of settling and averaging
    assert autocal.estimate_seconds(s, [2.5]) == pytest.approx(252)
    assert autocal.estimate_seconds(s, [0]) == pytest.approx(s.max_run_s + 12)


def test_csv_files(tmp_path):
    rig = Rig({("A", 1): 0.04})
    cal = run(rig.calibrator())
    path = autocal.write_csv(str(tmp_path / "cal.csv"), cal.results, cal.settings, "SIM")
    rows = list(csv.reader(open(path, encoding="utf-8")))
    header = rows[2]
    row = dict(zip(header, rows[3], strict=True))
    assert row["channel"] == "1" and row["passed"] == "True" and float(row["dmass_rate_mL_min"]) > 2
    raw = list(csv.reader(open(tmp_path / "cal_raw.csv", encoding="utf-8")))
    assert raw[0][:3] == ["head", "try", "seconds_from_pump_on"] and len(raw) > 100


def test_a_balance_hiccup_does_not_stop_the_run():
    rig = Rig({("A", 1): 0.04})
    rig.bal.error = "Lost contact with the balance on SIM: glitch"  # readings keep coming
    cal = run(rig.calibrator())
    assert cal.results[0].status == autocal.SAVED


class FakeSerial:
    """A serial port. A streaming balance sends ``chunks`` in order; a Mettler answers each
    SI with the next one."""

    def __init__(self, chunks, answer_si=False):
        self.chunks = [c.encode() for c in chunks]
        self.answer_si = answer_si
        self.pending = b""
        self.written = []

    @property
    def in_waiting(self):
        if not self.answer_si and not self.pending and self.chunks:
            self.pending = self.chunks.pop(0)
        return len(self.pending)

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, data):
        self.written.append(data)
        if self.answer_si and self.chunks:
            self.pending = self.chunks.pop(0)

    def read(self, n):
        import time

        time.sleep(0.002)
        if not self.answer_si and not self.pending and self.chunks:
            self.pending = self.chunks.pop(0)
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def close(self):
        pass


def _read_with(brand, chunks):
    import threading
    import time

    bal = balance.SerialBalance("COM9", brand)
    bal._ser = FakeSerial(chunks, answer_si=brand == "mettler")
    bal._thread = threading.Thread(target=bal._read_loop, daemon=True)
    bal._thread.start()
    deadline = time.monotonic() + 5
    while (bal._ser.chunks or bal._ser.pending) and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    ser = bal._ser
    bal.close()
    return bal, ser


def test_mettler_is_asked_for_each_reading():
    bal, ser = _read_with("mettler", ["S S     10.000 g\r\n", "ES\r\n", "S D     10.050 g\r\n"])
    assert ser.written and set(ser.written) == {b"SI\r\n"}
    assert [round(g, 3) for _t, g, _s in bal.samples()] == [10.0, 10.05]
    assert bal.samples()[1][2] is False  # still moving


def test_streaming_balance_is_only_listened_to():
    # joined mid-line ("0.1 g" is the end of a line sent before we connected): dropped
    bal, ser = _read_with("bonvoisin", ["0.1 g\r\n", "+  5.00 g\r\n", "junk\r\n", "+  5.10 g\r\n"])
    assert ser.written == []
    assert [g for _t, g, _s in bal.samples()] == [5.0, 5.1]


def test_lines_split_across_reads_are_joined():
    bal, _ser = _read_with("bonvoisin", ["x\r", "+  15", "0.1 g\r", "+  150.2", " g\r"])
    assert [g for _t, g, _s in bal.samples()] == [150.1, 150.2]


def test_overload_is_reported():
    bal, _ser = _read_with("mettler", ["S +\r\n"])
    assert "overloaded" in bal.error
