# linux-power-tools

Two small Python scripts I run on an old laptop that serves as my always-on homelab box. The first
switches off hardware the server never uses. The second measures how much power the machine draws.
Both use only the standard library and talk to the kernel through `/sys`.

| Tool | What it does |
|---|---|
| [`devctl`](devctl/) | Turns specific USB devices (webcam, SD card reader, Bluetooth) off and back on, from a terminal UI or the command line |
| [`powertrack`](powertrack/) | Logs energy use every minute for a month and reports kWh, average watts and cost |

## devctl

A headless server has no use for its webcam, card reader or Bluetooth radio, but they stay powered
and wake the CPU. `devctl` deauthorizes them through USB sysfs (`/sys/bus/usb/devices/*/authorized`)
so the kernel drops them, and authorizes them again when you need them.

```bash
sudo ./devctl/install.sh     # install, start the daemon, turn the listed devices OFF
devctl tui                   # space = toggle, a = all on, o = all off, q = quit
devctl status
devctl on webcam
devctl off all
```

- **Only the devices you list can be touched.** They are matched by USB vendor:product ID in the
  `DEVICES` table at the top of `devctl.py`. Edit that table for your hardware.
- **Your choice survives reboots.** Devices come back authorized after a boot, so the daemon
  re-applies the saved state when it starts. The default is off.
- **Nothing is exposed on the network.** A root daemon run by systemd listens on `127.0.0.1:8765`
  only, and the unit file restricts it to loopback with `IPAddressDeny=any`. The CLI and TUI run as
  your user and call it with a bearer token from `/etc/devctl/token`, which is readable only by
  root and you. Tokens are compared in constant time.
- **Remote use goes through SSH.** Either SSH in and run `devctl tui`, or forward the port with
  `ssh -L 8765:127.0.0.1:8765 user@host` and run the TUI locally with `--token`.
- `--dry-run` pretends without writing to sysfs, for testing.

## powertrack

Answers "what does it cost to leave this laptop on all month?" with measured numbers instead of
the charger's rating.

```bash
sudo ./powertrack/powertrack.py install            # start a one-month window, sample every minute
./powertrack/powertrack.py report --rate 8.5       # usage so far, cost at 8.5 per kWh
sudo ./powertrack/powertrack.py uninstall          # stop sampling, keep the data
```

- **Reads Intel RAPL counters** in `/sys/class/powercap`: `psys` for the whole platform (CPU, GPU,
  memory, chipset) where the hardware has it, and `pkg` for the CPU package. A systemd timer takes
  one sample a minute into a CSV under `/var/lib/powertrack`.
- **Handles counter wraparound** using each counter's `max_energy_range_uj`.
- **Doesn't count time while the machine is off.** A gap longer than 10 minutes between samples (suspend,
  shutdown) is reported as unmonitored instead of being treated as zero or interpolated.
- **Estimates draw for the whole laptop.** RAPL doesn't see the screen, disk, Wi-Fi or power-supply losses.
  While the laptop runs on battery, the battery's `power_now` reading gives true total draw,
  and the difference from the CPU counter becomes a baseline for everything else. Unplug the charger
  for a few minutes to calibrate it.
- **Reports** daily kWh, average watts and hours monitored, the total so far, a projection for the
  month at the current duty cycle, and cost if you pass `--rate`.

Needs root to read RAPL, and an Intel CPU that exposes it.
