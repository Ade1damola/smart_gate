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
git clone --branch admin-dashboard-server-fingerprints https://github.com/Ade1damola/smart_gate.git /opt/smart_gate
cd /opt/smart_gate
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r anpr/requirements-pi.txt
```

If the repository is private, authenticate Git using your normal secure Git
credential method. Do not put a GitHub token in the clone URL or this guide.

## Configure device access

Create the service environment file outside the repository:

```sh
sudo install -d -m 0750 /etc/smart-gate
sudo install -m 0600 /dev/null /etc/smart-gate/anpr.env
sudo nano /etc/smart-gate/anpr.env
```

Set the values for this installation (replace the example addresses and key):

```ini
CAMERA_BACKEND=picamera2
SERVER_URL=https://verigate-ry5y.onrender.com
ESP32_URL=http://verigate.local
DEVICE_API_KEY=replace-with-the-server-device-key
ANPR_EVENT_TYPE=entry
```

`SERVER_URL` should be the reachable Flask server URL. `ESP32_URL` must resolve
to the gate controller on the local network. The device key must match the
server's `DEVICE_API_KEY`; without it, the server rejects detection requests
and the gate remains closed. Keep this file private and never commit it.

## Enable the service

The checked-in service assumes the Linux account is `pi`. If your account has a
different name, edit `User=pi` in the unit before installing it. Then run:

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