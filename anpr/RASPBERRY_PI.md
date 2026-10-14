# Raspberry Pi ANPR Deployment

This deploys `live_capture.py` as a continuously running service using the
Raspberry Pi camera stack. The steps assume Raspberry Pi OS 64-bit and an
OV5647 camera module with its ribbon cable seated correctly. An IR-sensitive
camera needs suitable IR illumination after dark; the camera alone does not
light the scene.

## Install the camera stack

Enable the camera in Raspberry Pi OS if prompted, then install Picamera2 and
OpenCV from the OS packages. Keep the Python virtual environment linked to OS
packages so it can import Picamera2:

```sh
sudo apt update
sudo apt install -y git python3-venv python3-picamera2 python3-opencv
rpicam-hello --list-cameras
```

The OV5647 should appear in the camera list. Test a preview with
`rpicam-hello -t 5000` before setting up ANPR.

## Install the ANPR application

Clone this repository to `/opt/smart_gate` (or change the paths in the service
unit to match your chosen location), then create the environment with access to
the system camera libraries:

```sh
sudo mkdir -p /opt/smart_gate
sudo chown "$USER":"$USER" /opt/smart_gate
git clone --branch main https://github.com/Ade1damola/smart_gate.git /opt/smart_gate
cd /opt/smart_gate
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r anpr/requirements-pi.txt
```

If the repository is private, authenticate Git using your normal secure Git
credential method. Do not put a GitHub token in the clone URL or this guide.

## Configure device access

Create the shared local configuration file from the safe template and restrict
it to the `gateadmin` account (or the account that owns the checkout):

```sh
cp .env.example .env
chmod 600 .env
nano .env
```

Set real values only in `.env`. Configure `SERVER_URL`, `ESP32_URL`,
`DEVICE_API_KEY`, and unique `DEFAULT_STAFF_PASSWORD` and
`DEFAULT_ADMIN_PASSWORD` values. The template's hostnames and credentials are
placeholders and must be replaced. The backend and ANPR client load this file;
the systemd service also reads it directly. Keep `.env` private and never
commit it.

`SERVER_URL` should point to the reachable Flask server. `ESP32_URL` must
resolve to the gate controller on the local network. The device key must match
the server's `DEVICE_API_KEY`; without it, the server rejects detection
requests and the gate remains closed.

The ESP32 is compiled separately and cannot read the Pi's `.env`. Copy
`esp32/smart_gate_esp32/secrets.h.example` to
`esp32/smart_gate_esp32/secrets.h`, then set its Wi-Fi, `SERVER_URL`, and
matching `DEVICE_API_KEY` values there. That header is gitignored.

## Enable the service

The checked-in service runs as `gateadmin`. If your Linux account has a
different name, edit the `User=` and `Group=` settings in the unit before
installing it. Then run:

```sh
sudo cp /opt/smart_gate/anpr/deploy/smart-gate-anpr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smart-gate-anpr
sudo systemctl status smart-gate-anpr
```

Follow runtime logs with:

```sh
sudo journalctl -u smart-gate-anpr -f
```

The service restarts after process exits and starts on boot. To deploy a later
code update, update the checkout, reinstall `anpr/requirements-pi.txt` if it
changed, and restart the service:

```sh
cd /opt/smart_gate
git pull
.venv/bin/pip install -r anpr/requirements-pi.txt
sudo systemctl restart smart-gate-anpr
```

## Camera tuning

The default capture resolution is 1280x720. Override it in
`/etc/smart-gate/anpr.env` if the Pi needs a lighter workload, for example:

```ini
CAPTURE_WIDTH=960
CAPTURE_HEIGHT=540
```

Other supported controls include `SCAN_INTERVAL_SECONDS`, `PREFILTER`,
`CONFIRM_READS`, `COOLDOWN_SECONDS`, and `CAMERA_PUSH_INTERVAL`; see the
configuration list at the top of `live_capture.py`. Validate plate reads with
the camera mounted at its final position and lighting. The included OCR
pipeline is not a guarantee of recognition accuracy for every plate, angle,
distance, exposure, or night-illumination setup.

## Compare OCR performance on the Pi

Install the Pi requirements after updating the checkout:

```sh
.venv/bin/pip install -r anpr/requirements-pi.txt
```

Place representative, non-sensitive plate test images in a directory on the
Pi. These are test inputs only; the repository's `anpr/test_images/` directory
is intentionally gitignored. Use the same images and duration for both runs.
Run the benchmark while the Pi is otherwise idle, with its final cooling and
camera enclosure in place:

```sh
cd /opt/smart_gate/anpr
../.venv/bin/python benchmark_pi_ocr.py --engine both --image-dir ./pi_test_images --duration 300
```

The script warms up each engine, then processes the image set repeatedly for
five measured minutes per engine in separate processes. It prints mean and
median image latency, average CPU percentage normalized across all logical
cores, peak process RSS memory, and peak CPU temperature sampled from Linux
thermal sysfs. A missing temperature reading means the OS did not expose a
thermal-zone value. EasyOCR downloads its model on first use and uses
significantly more memory; allow internet access and disk space on its first
run. Keep the Pi plugged into stable power and do not run both engines
simultaneously during this comparison.