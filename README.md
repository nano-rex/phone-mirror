# phone-mirror

Mirror and control an Android or iOS device from a Windows, macOS or Linux computer.

- **Android**: low-latency H.264/H.265/AV1 video over USB or Wi-Fi, touch, drag, scroll, keyboard,
  hardware buttons and two-way clipboard. It runs the [scrcpy](https://github.com/Genymobile/scrcpy)
  server on the phone (bundled, no root, no app to install) and decodes the stream itself.
- **iOS**: screen mirroring and control through
  [WebDriverAgent](https://github.com/appium/WebDriverAgent) (WDA): tap, long press, swipe,
  scroll, typing, Home, volume and lock.

One window, the same controls for both platforms, written in Python with
[PyAV](https://pyav.basswood-io.com/) for decoding and [pygame-ce](https://pyga.me/) for the window.

## Install

Python 3.9 or newer.

```sh
pip install git+https://github.com/nano-rex/phone-mirror
```

or from a clone:

```sh
git clone https://github.com/nano-rex/phone-mirror
cd phone-mirror
python -m venv .venv
.venv/bin/pip install .        # Windows: .venv\Scripts\pip install .
```

PyAV and pygame-ce ship prebuilt wheels for Windows, macOS and Linux, so no compiler or FFmpeg
install is needed.

## Usage

```sh
phonemirror              # picks the connected Android device, or WDA if it answers
phonemirror devices      # list Android (adb) and iOS devices
phonemirror android      # Android over adb
phonemirror ios          # iOS through WebDriverAgent
```

### Controls

| Input | Action |
|---|---|
| Left click / drag | Touch, swipe, long press |
| Right click | Back (Android; also wakes the screen) |
| Middle click | Home |
| Mouse wheel | Scroll |
| Typing | Text into the focused field |
| Ctrl+V (Cmd+V on macOS) | Paste the computer clipboard on the device |
| Ctrl+C / Ctrl+X | Copy / cut on the device; Android sends it back to the computer clipboard |
| Alt+H / Alt+B / Alt+S | Home / Back / Recent apps |
| Alt+P | Power (iOS: lock / unlock) |
| Alt+Up / Alt+Down | Volume up / down |
| Alt+N / Alt+Shift+N | Open / close the notification panel (Android) |
| Alt+R | Rotate the device (Android) |
| Alt+O / Alt+Shift+O | Turn the phone's own screen off / on while mirroring continues (Android) |
| Alt+F | Fullscreen |
| Alt+W | Resize the window to remove the black borders |

Alt means the **left** Alt key (Option on macOS), so AltGr keeps typing characters such as `@`.
The button bar at the bottom has the same actions; hide it with `--no-toolbar`.

## Android setup

1. Install Android **platform-tools** (it contains `adb`) and make sure `adb` is on `PATH`
   (or set `ADB=/path/to/adb`):
   - Windows: `winget install Google.PlatformTools`, or unzip the
     [download](https://developer.android.com/tools/releases/platform-tools)
   - macOS: `brew install android-platform-tools`
   - Linux: `sudo apt install adb` (Debian/Ubuntu) or `sudo dnf install android-tools` (Fedora)
2. On the phone: Settings → About phone → tap **Build number** 7 times, then enable
   **Developer options → USB debugging**. Some Xiaomi/MIUI phones also need
   **USB debugging (Security settings)** for input control.
3. Connect the cable, accept the "Allow USB debugging?" prompt, then run `phonemirror android`.

Requires Android 5.0 or newer (audio is not mirrored).

**Wi-Fi**: on Android 11+, enable **Wireless debugging**, pair once with
`adb pair HOST:PAIRPORT`, then `phonemirror android --connect HOST:PORT`. On older versions,
connect by USB, run `adb tcpip 5555`, unplug, then `phonemirror android --connect PHONE_IP:5555`.

Useful options:

```
-s, --serial SERIAL      choose a device when several are connected
-m, --max-size 1600      longer video side in pixels, 0 = native resolution
-b, --bit-rate 8M        video bit rate
    --max-fps 60
    --codec h264|hevc|av1
-n, --no-control         mirror only
-S, --turn-screen-off    keep the phone's own screen off while mirroring
-w, --stay-awake         keep the phone awake while plugged in
-t, --show-touches
```

## iOS setup

Apple does not offer a public way to inject input into an iPhone from a computer, so control
goes through WebDriverAgent, the UI-testing agent used by Appium. WDA streams the screen as MJPEG
on port 9100 and takes commands on port 8100. phonemirror talks to both.

1. **Install WebDriverAgent on the device (once, needs a Mac with Xcode)**
   ```sh
   git clone https://github.com/appium/WebDriverAgent
   cd WebDriverAgent
   open WebDriverAgent.xcodeproj
   ```
   In Xcode select the **WebDriverAgentRunner** target, set your Apple ID team under
   *Signing & Capabilities* (change the bundle id if Xcode asks), then run it on the phone:
   ```sh
   xcodebuild -project WebDriverAgent.xcodeproj -scheme WebDriverAgentRunner \
     -destination 'id=<UDID>' test
   ```
   Trust the developer certificate on the phone (Settings → General → VPN & Device Management)
   and enable **Developer Mode** (Settings → Privacy & Security) on iOS 16+.
   Free Apple IDs need to re-sign every 7 days.

2. **Start WDA** whenever you want to mirror:
   - macOS: the `xcodebuild ... test` command above (leave it running).
   - Windows / Linux: once WDA is installed on the phone, start it with
     [go-ios](https://github.com/danielpaulus/go-ios) (`ios runwda`) or
     [pymobiledevice3](https://github.com/doronz88/pymobiledevice3). iOS 17+ also needs
     `ios tunnel start` (administrator/root). Windows needs Apple's USB driver, which comes
     with iTunes or the Apple Devices app.

3. **Forward the ports and connect**: `phonemirror ios` starts `iproxy` (libimobiledevice) or
   go-ios `ios forward` for ports 8100 and 9100 automatically when WDA is not already reachable
   on localhost. Pass `-u UDID` when several devices are connected, or forward yourself:
   ```sh
   iproxy 8100:8100 &
   iproxy 9100:9100 &
   phonemirror ios
   ```
   If the phone is on the same Wi-Fi, you can skip forwarding:
   `phonemirror ios --wda-url http://PHONE_IP:8100`.

iOS options: `--fps 30`, `--scale 50` (MJPEG size in percent of the screen), `--quality 60`.
Higher values look sharper and cost more Wi-Fi/USB bandwidth.

**iOS limits**: there is no Back or Recent apps button. Each touch is sent once you release the
mouse (tap, long press or the recorded swipe path), so drags show up on the phone a moment after
you make them. Text goes through the WDA keyboard, so a text field has to be focused first.

## How it works

```
Android:  phone (scrcpy-server 5.0.1, MediaCodec) --adb forward--> video socket  -> PyAV decode -> window
                                                     <------------ control socket <- touch/keys/clipboard
iOS:      phone (WebDriverAgent) --:9100 MJPEG--> JPEG frames -> window
                                 <--:8100 HTTP--- tap / actions / keys / buttons
```

- `phonemirror/android/protocol.py` – scrcpy 5.0.1 wire format (control messages, device
  messages, session and packet headers), checked against scrcpy's own serialization tests.
- `phonemirror/android/client.py` – pushes the server to `/data/local/tmp`, starts it with
  `app_process`, opens the video and control sockets over an `adb forward` tunnel and decodes video.
- `phonemirror/ios/wda.py` – WDA session, MJPEG reader and the gesture mapping.
- `phonemirror/viewer.py` – the pygame window, letterboxing and the input mapping.

## Development

```sh
pip install -e .
python -m unittest discover -s tests -t .
```

The tests run without a phone: a fake scrcpy server streams real H.264 (encoded with PyAV's
libx264) to the Android backend and checks the decoded frame and the control bytes it sends back;
a fake WDA HTTP/MJPEG server checks the iOS requests; the viewer is driven headless with SDL's
dummy video driver.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).

`phonemirror/android/scrcpy-server-v5.0.1` is the unmodified server from the
[scrcpy v5.0.1 release](https://github.com/Genymobile/scrcpy/releases/tag/v5.0.1)
(SHA-256 `764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536`),
Copyright Genymobile and Romain Vimont, Apache License 2.0
(see [phonemirror/android/SCRCPY-LICENSE](phonemirror/android/SCRCPY-LICENSE)).
