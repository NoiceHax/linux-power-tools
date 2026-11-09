#!/usr/bin/env python3
"""powertrack - log and report this machine's energy use over a one-month window.

Source of truth is the Intel RAPL counters in /sys/class/powercap:
  psys  = whole platform (CPU+GPU+memory+PCH...) - best available "total"
  pkg   = CPU package only
Neither includes screen backlight/charger losses on every model, so treat the
numbers as "computer internals" rather than wall-socket draw. Battery power_now
is also logged (only meaningful while discharging).

Commands (reading RAPL needs root, so install/sample run as root):
  sudo ./powertrack.py install [--months 1]   start the monthly window now + enable 1-min systemd timer
  sudo ./powertrack.py uninstall              stop the timer (data is kept)
  ./powertrack.py report [--rate 8.5]         usage so far (--rate = price per kWh, any currency)
  ./powertrack.py sample                      take one sample (what the timer runs)
"""
import argparse, csv, datetime as dt, json, os, shutil, subprocess, sys, time

DATA_DIR = "/var/lib/powertrack"
LOG = f"{DATA_DIR}/samples.csv"
STATE = f"{DATA_DIR}/state.json"
BIN = "/usr/local/bin/powertrack"
RAPL = "/sys/class/powercap"
BAT = "/sys/class/power_supply/BAT0"
MAX_GAP = 600  # seconds; longer gaps (suspend/off) are not counted, reported as gaps
FIELDS = ["ts", "psys_uj", "pkg_uj", "bat_status", "bat_power_uw"]


def rd(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def rapl(name):
    for d in sorted(os.listdir(RAPL)):
        p = f"{RAPL}/{d}"
        if d.startswith("intel-rapl:") and d.count(":") == 1 and rd(f"{p}/name").startswith(name):
            return rd(f"{p}/energy_uj"), int(rd(f"{p}/max_energy_range_uj", "0") or 0)
    return "", 0


def sample():
    if os.geteuid() != 0:
        sys.exit("sample must run as root (RAPL counters are root-only)")
    os.makedirs(DATA_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o755)
    psys, mx = rapl("psys")
    pkg, mx2 = rapl("package")
    st = load_state() or {}
    st["max_uj"] = mx or mx2 or 262143328850
    save_state(st)
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        w.writerow([int(time.time()), psys, pkg, rd(f"{BAT}/status"), rd(f"{BAT}/power_now")])
    os.chmod(LOG, 0o644)


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return None


def save_state(st):
    with open(STATE, "w") as f:
        json.dump(st, f)
    os.chmod(STATE, 0o644)


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def install(months):
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    os.makedirs(DATA_DIR, exist_ok=True)
    now = dt.datetime.now().astimezone()
    st = load_state() or {}
    st.update(start=int(now.timestamp()), end=int(add_months(now, months).timestamp()))
    save_state(st)
    shutil.copy(os.path.abspath(__file__), BIN)
    os.chmod(BIN, 0o755)
    open("/etc/systemd/system/powertrack.service", "w").write(
        f"[Unit]\nDescription=Record energy counters\n\n[Service]\nType=oneshot\nExecStart={BIN} sample\n")
    open("/etc/systemd/system/powertrack.timer", "w").write(
        "[Unit]\nDescription=Sample energy counters every minute\n\n"
        "[Timer]\nOnBootSec=30s\nOnUnitActiveSec=60s\nAccuracySec=1s\n\n[Install]\nWantedBy=timers.target\n")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "powertrack.timer"], check=True)
    sample()
    print(f"Tracking from {now:%Y-%m-%d %H:%M} to {dt.datetime.fromtimestamp(st['end']):%Y-%m-%d %H:%M}.")
    print(f"Report any time with: powertrack report   (data: {LOG})")


def uninstall():
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    subprocess.run(["systemctl", "disable", "--now", "powertrack.timer"])
    for p in ("/etc/systemd/system/powertrack.timer", "/etc/systemd/system/powertrack.service"):
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["systemctl", "daemon-reload"])
    print("Timer removed. Data kept in", DATA_DIR)


def report(rate):
    st = load_state()
    if not st or "start" not in st or not os.path.exists(LOG):
        sys.exit("No data yet. Run: sudo ./powertrack.py install")
    start, end, mx = st["start"], st["end"], st.get("max_uj", 262143328850)
    rows = []
    with open(LOG) as f:
        for r in csv.DictReader(f):
            try:
                rows.append((int(r["ts"]), int(r["psys_uj"] or -1), int(r["pkg_uj"] or -1),
                             r.get("bat_status", ""), int(r.get("bat_power_uw") or 0)))
            except (ValueError, KeyError):
                pass
    rows = [r for r in rows if start <= r[0] <= end]
    if len(rows) < 2:
        sys.exit("Not enough samples yet - wait a couple of minutes.")
    # psys is bogus on some chips (this i5-6200U): it can't legitimately be below the CPU package.
    use_psys = all(r[1] >= 0 for r in rows) and (
        (rows[-1][1] - rows[0][1]) % mx >= (rows[-1][2] - rows[0][2]) % mx)
    idx = 1 if use_psys else 2
    days, tracked, gap = {}, 0, 0
    bat_j = pkg_j = bat_s = 0  # intervals where the battery is discharging = true whole-laptop draw
    for a, b in zip(rows, rows[1:]):
        dt_s = b[0] - a[0]
        if dt_s > MAX_GAP:
            gap += dt_s
            continue
        d = b[idx] - a[idx]
        if d < 0:
            d += mx  # counter wrapped
        if a[3] == "Discharging" and b[3] == "Discharging" and a[4] > 0 and b[4] > 0:
            dp = b[2] - a[2]
            dp += mx if dp < 0 else 0
            bat_j += (a[4] + b[4]) / 2 * dt_s / 1e6  # uW*s -> J
            pkg_j += dp / 1e6
            bat_s += dt_s
        day = dt.datetime.fromtimestamp(b[0]).date()
        j, s = days.get(day, (0.0, 0))
        days[day] = (j + d / 1e6, s + dt_s)
        tracked += dt_s
    if tracked == 0:
        sys.exit("Not enough time between samples yet - try again in a minute.")
    total_j = sum(j for j, _ in days.values())
    kwh = total_j / 3.6e6
    print(f"Source: {'psys (whole platform)' if use_psys else 'package (CPU only)'}")
    print(f"Window: {dt.datetime.fromtimestamp(start):%Y-%m-%d %H:%M} -> {dt.datetime.fromtimestamp(end):%Y-%m-%d %H:%M}\n")
    print(f"{'Date':<12}{'kWh':>8}{'avg W':>8}{'hours':>7}")
    for day in sorted(days):
        j, s = days[day]
        print(f"{day!s:<12}{j/3.6e6:>8.3f}{(j/s if s else 0):>8.1f}{s/3600:>7.1f}")
    print(f"\nTotal so far : {kwh:.3f} kWh over {tracked/3600:.1f} h monitored (avg {total_j/tracked:.1f} W)")
    if gap:
        print(f"Unmonitored  : {gap/3600:.1f} h (off/suspended/gaps, not counted)")
    whole = None
    if bat_s >= 120:
        extra_w = max(0.0, (bat_j - pkg_j) / bat_s)  # non-CPU draw: screen, disk, wifi, RAM, VRM losses
        whole = kwh + extra_w * tracked / 3.6e6
        print(f"\nOn battery   : {bat_s/3600:.2f} h discharging, measured {bat_j/bat_s:.1f} W whole-laptop "
              f"(CPU {pkg_j/bat_s:.1f} W + other {extra_w:.1f} W)")
        print(f"Whole-laptop : ~{whole:.3f} kWh so far (CPU energy + {extra_w:.1f} W non-CPU baseline x monitored time)")
    else:
        print("\nWhole-laptop : no battery-discharge data yet; unplug the charger for a few minutes to calibrate.")
    elapsed = min(time.time(), end) - start
    if time.time() < end and tracked:
        # project using average power over monitored time, scaled to the monitored fraction of the window
        proj = kwh / tracked * (tracked / elapsed) * (end - start)
        print(f"Month est.   : ~{proj:.2f} kWh (at current monitored duty cycle)")
        kwh_est = proj
        if whole:
            wproj = whole / tracked * (tracked / elapsed) * (end - start)
            print(f"Whole month  : ~{wproj:.2f} kWh whole-laptop estimate")
            kwh_est = wproj
    else:
        kwh_est = kwh
    if rate:
        print(f"Cost so far  : {kwh*rate:.2f}   (month est. {kwh_est*rate:.2f}) at {rate}/kWh")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install"); p.add_argument("--months", type=int, default=1)
    sub.add_parser("uninstall"); sub.add_parser("sample")
    p = sub.add_parser("report"); p.add_argument("--rate", type=float, help="price per kWh")
    a = ap.parse_args()
    {"install": lambda: install(a.months), "uninstall": uninstall, "sample": sample,
     "report": lambda: report(a.rate)}[a.cmd]()
