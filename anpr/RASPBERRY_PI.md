# Raspberry Pi ANPR Setup

This guide sets up the Raspberry Pi to read number plates from its camera and
send them to the Smart Gate server. When you finish, plate recognition runs in
the background, starts automatically on boot and restarts itself if it crashes.

**You need:**

- A Raspberry Pi running **Raspberry Pi OS (64-bit)**, connected to the internet
- An **OV5647 camera module**, with its ribbon cable firmly seated
- An **IR light** if the gate must work at night (an IR camera can't light the
  scene by itself)
- The server's address and its `DEVICE_API_KEY`

**Overview:**

1. [Check the camera](#step-1-check-the-camera)
2. [Download the code](#step-2-download-the-code)
3. [Add your settings](#step-3-add-your-settings)
4. [Start the service](#step-4-start-the-service)

The commands below assume the Pi's user account is called `gateadmin`. If yours
is different, see the note in Step 4.

---

## Step 1: Check the camera

Install the camera and image libraries:

```sh
sudo apt update
sudo apt install -y git python3-venv python3-picamera2 python3-opencv
```

Confirm the Pi can see the camera:

```sh
rpicam-hello --list-cameras
```

✅ **Expected:** the output lists an `ov5647` camera.

Then open a 5-second preview to check the picture:

```sh
rpicam-hello -t 5000
```

❌ **No camera listed?** Power off the Pi, reseat the ribbon cable at both ends
(the metal contacts must face the right way), and try again.

---

## Step 2: Download the code

This puts the code in `/opt/smart_gate` and installs its Python packages:

```sh
sudo mkdir -p /opt/smart_gate
sudo chown "$USER":"$USER" /opt/smart_gate
git clone --branch main https://github.com/Ade1damola/smart_gate.git /opt/smart_gate
cd /opt/smart_gate
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r anpr/requirements-pi.txt
```

> `--system-site-packages` lets Python use the camera library installed in
> Step 1. Don't leave it out.

If the repository is private, Git will ask you to sign in. Use your normal Git
sign-in method. **Never paste a GitHub token into the clone URL.**

---

## Step 3: Add your settings

Create your settings file from the template and make it private:

```sh
cd /opt/smart_gate
cp .env.example .env
chmod 600 .env
nano .env
```

Replace the placeholder values in `.env`:

| Setting | What to put |
|---|---|
| `SERVER_URL` | The Smart Gate server's address, e.g. `https://your-server.onrender.com` |
| `ESP32_URL` | The gate controller's address on the local network |
| `DEVICE_API_KEY` | The **same** key the server uses. If it doesn't match, the server rejects every plate and the gate stays shut. |
| `CAMERA_BACKEND` | Leave as `picamera2` |
| `DEFAULT_STAFF_PASSWORD`, `DEFAULT_ADMIN_PASSWORD` | Only needed if this Pi also runs the backend server |

Save with **Ctrl+O**, then **Enter**, and exit with **Ctrl+X**.

> 🔒 `.env` holds secrets. Never commit it or share it.

**The ESP32 gate controller has its own settings file.** It can't read the
Pi's `.env`. On the computer you program the ESP32 from, copy
`esp32/smart_gate_esp32/secrets.h.example` to `secrets.h` in the same folder,
fill in the Wi-Fi name and password, `SERVER_URL` and the same
`DEVICE_API_KEY`, and then upload the sketch.

---

## Step 4: Start the service

> **Not using `gateadmin`?** Before running the commands below, open
> `anpr/deploy/smart-gate-anpr.service` and change `User=gateadmin` to your
> username.

Install the service and start it:

```sh
sudo cp /opt/smart_gate/anpr/deploy/smart-gate-anpr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smart-gate-anpr
sudo systemctl status smart-gate-anpr
```

✅ **Expected:** the status shows `active (running)` in green. Press **q** to
exit.

Then watch the live log while a car (or a printed plate) is in front of the
camera:

```sh
sudo journalctl -u smart-gate-anpr -f
```

Press **Ctrl+C** to stop watching. That's it, the Pi is set up.

---

## Everyday commands

| To... | Run |
|---|---|
| Check it's running | `sudo systemctl status smart-gate-anpr` |
| Watch the live log | `sudo journalctl -u smart-gate-anpr -f` |
| Restart it (e.g. after editing `.env`) | `sudo systemctl restart smart-gate-anpr` |
| Stop it | `sudo systemctl stop smart-gate-anpr` |

### Installing an update

```sh
cd /opt/smart_gate
git pull
.venv/bin/pip install -r anpr/requirements-pi.txt
sudo systemctl restart smart-gate-anpr
```

---

## Troubleshooting

| Problem | What to check |
|---|---|
| Camera not listed in Step 1 | Reseat the ribbon cable with the Pi powered off. |
| Status shows `failed` | Run `sudo journalctl -u smart-gate-anpr -n 50` and read the last error. |
| Log says the device key is invalid or missing | `DEVICE_API_KEY` in `.env` doesn't match the server's. Fix it, then restart. |
| Can't reach the server | Check `SERVER_URL` in `.env` and that the Pi is online. |
| Plates detected but the gate doesn't open | Check `ESP32_URL`, and that the ESP32's `secrets.h` uses the same key. |
| Pi runs hot or slow | Lower the capture resolution (see below). |
| Plates misread at night | Add or reposition the IR light. |

---

## Optional: tuning the camera

Add any of these to `/opt/smart_gate/.env`, then restart the service.

| Setting | Default | What it does |
|---|---|---|
| `CAPTURE_WIDTH` / `CAPTURE_HEIGHT` | `1280` / `720` | Image size. Use `960` / `540` to lighten the Pi's load. |
| `SCAN_INTERVAL_SECONDS` | `0.5` | Time between plate scans |
| `PREFILTER` | `motion` | Only scans when something moves |
| `CONFIRM_READS` | `2` | Matching reads needed before a plate counts |
| `COOLDOWN_SECONDS` | `30` | Wait before the same plate is sent again |
| `CAMERA_PUSH_INTERVAL` | `1` | Seconds between live frames sent to the admin monitor |

The full list is at the top of `anpr/live_capture.py`.

Always test plate reading with the camera in its **final position and
lighting**. Accuracy depends on angle, distance, exposure and night lighting,
so it isn't guaranteed for every setup.

---

## Optional: comparing OCR engines

`benchmark_pi_ocr.py` measures how fast each text-recognition engine runs on
this Pi, and how much CPU, memory and heat it uses.

1. Install the extra benchmark packages. Install PyTorch first, from its
   CPU-only source. Without that step, pip downloads the GPU build, which is
   several GB of NVIDIA libraries the Pi can't use.

   ```sh
   cd /opt/smart_gate
   .venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
   .venv/bin/pip install -r anpr/requirements-pi-benchmark.txt
   ```

2. Put some test plate photos in a folder on the Pi, for example
   `/opt/smart_gate/anpr/pi_test_images`. Don't use photos of real people's
   plates you shouldn't keep, and don't commit them.
3. Leave the Pi idle, in its final case and cooling, on stable power.
4. Run:

   ```sh
   cd /opt/smart_gate/anpr
   ../.venv/bin/python benchmark_pi_ocr.py --engine both --image-dir ./pi_test_images --duration 300
   ```

This tests each engine for 5 minutes, one after the other, so the full run
takes a little over 10 minutes. It then prints for each engine:

- Average and median time per image
- Average CPU use (across all cores)
- Peak memory use
- Peak CPU temperature (blank if the Pi doesn't report it)

The first EasyOCR run downloads a model, so it needs internet access and some
free disk space. EasyOCR also uses much more memory than the other engine.
