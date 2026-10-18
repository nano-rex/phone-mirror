"""Command line entry point."""

import argparse
import shutil
import subprocess
import sys

from . import __version__


def list_devices():
    from .adb import Adb, AdbError

    found = False
    try:
        for serial, state, model in Adb().devices():
            found = True
            print(f"android  {serial:<24} {state:<12} {model}")
    except AdbError as exc:
        print(f"android  ({exc})")
    for tool, args in (("idevice_id", ["-l"]), ("ios", ["list"])):
        path = shutil.which(tool)
        if not path:
            continue
        out = subprocess.run([path] + args, capture_output=True, text=True, timeout=15).stdout
        for line in out.split():
            line = line.strip("[]{}\",")
            if len(line) >= 24 and line.replace("-", "").isalnum():
                found = True
                print(f"ios      {line}")
        break
    else:
        print("ios      (install libimobiledevice or go-ios to list iOS devices)")
    return 0 if found else 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="phonemirror",
        description="Mirror and control an Android or iOS device from this computer.")
    parser.add_argument("--version", action="version", version=f"phonemirror {__version__}")
    parser.add_argument("--no-toolbar", action="store_true", help="hide the button bar")
    parser.add_argument("--fullscreen", action="store_true")
    sub = parser.add_subparsers(dest="platform")

    sub.add_parser("devices", help="list connected Android and iOS devices")

    w = sub.add_parser("app", help="mirror the phonemirror Android app over Wi-Fi (no adb needed)")
    w.add_argument("--port", type=int, default=27199, help="TCP port to listen on (default 27199)")

    st = sub.add_parser("setup", help="set up a phone over USB")
    st_sub = st.add_subparsers(dest="setup_platform", required=True)
    sa = st_sub.add_parser("android", help="install the app, turn on control and pair, over adb")
    sa.add_argument("-s", "--serial")
    sa.add_argument("--apk", help="install this APK instead of downloading the latest release")
    sa.add_argument("-y", "--yes", action="store_true", help="answer yes to the questions")
    sa.add_argument("--no-control", action="store_true", help="do not turn on control")
    sa.add_argument("--undo", action="store_true",
                    help="turn phonemirror control off and offer to uninstall the app")

    a = sub.add_parser("android", help="mirror an Android device over adb (USB or Wi-Fi)")
    a.add_argument("-s", "--serial", help="adb serial (see `phonemirror devices`)")
    a.add_argument("--connect", metavar="HOST:PORT",
                   help="run `adb connect HOST:PORT` first (wireless debugging)")
    a.add_argument("-m", "--max-size", type=int, default=1600,
                   help="limit the longer video side in pixels, 0 = native (default 1600)")
    a.add_argument("-b", "--bit-rate", default="8M", help="video bit rate (default 8M)")
    a.add_argument("--max-fps", type=int, default=60, help="0 = unlimited (default 60)")
    a.add_argument("--codec", choices=("h264", "hevc", "av1"), default="h264")
    a.add_argument("-n", "--no-control", action="store_true", help="mirror only")
    a.add_argument("-w", "--stay-awake", action="store_true",
                   help="keep the device awake while plugged in")
    a.add_argument("-S", "--turn-screen-off", action="store_true",
                   help="turn the phone's own screen off while mirroring")
    a.add_argument("-t", "--show-touches", action="store_true")
    a.add_argument("--power-off-on-close", action="store_true")
    a.add_argument("--server", help="path to a scrcpy-server 5.0.1 file (default: bundled)")

    i = sub.add_parser("ios", help="mirror an iOS device through WebDriverAgent")
    i.add_argument("--wda-url", default="http://127.0.0.1:8100",
                   help="WebDriverAgent URL (default http://127.0.0.1:8100)")
    i.add_argument("--mjpeg-url", help="MJPEG stream URL (default: WDA host, port 9100)")
    i.add_argument("-u", "--udid", help="device UDID for port forwarding")
    i.add_argument("--no-forward", action="store_true",
                   help="do not start iproxy/go-ios port forwarding automatically")
    i.add_argument("--fps", type=int, default=30, help="MJPEG frame rate (default 30)")
    i.add_argument("--scale", type=int, default=50,
                   help="MJPEG scaling percent, 1-100 (default 50)")
    i.add_argument("--quality", type=int, default=60, help="JPEG quality 1-100 (default 60)")
    return parser


def make_backend(args):
    if args.platform == "app":
        from .link.backend import AppBackend
        return AppBackend(port=args.port)
    if args.platform == "android":
        from .android.client import AndroidBackend
        return AndroidBackend(
            serial=args.serial, max_size=args.max_size, bit_rate=args.bit_rate,
            max_fps=args.max_fps, codec=args.codec, control=not args.no_control,
            stay_awake=args.stay_awake, show_touches=args.show_touches,
            turn_screen_off=args.turn_screen_off,
            power_off_on_close=args.power_off_on_close, connect=args.connect,
            server_path=args.server)
    from .ios.wda import IosBackend
    return IosBackend(wda_url=args.wda_url, mjpeg_url=args.mjpeg_url, udid=args.udid,
                      forward=not args.no_forward, fps=args.fps, scale=args.scale,
                      quality=args.quality)


def autodetect():
    """Pick android when adb sees a device, otherwise ios when WDA answers."""
    from .adb import Adb, AdbError
    try:
        if any(state == "device" for _, state, _ in Adb().devices()):
            return "android"
    except AdbError:
        pass
    from .ios.wda import WdaClient, WdaError
    try:
        WdaClient().status(timeout=2)
        return "ios"
    except WdaError:
        return None


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.platform == "devices":
        return list_devices()
    if args.platform == "setup":
        from . import setup_android
        from .adb import AdbError
        try:
            if args.undo:
                return setup_android.undo(args.serial, args.yes)
            return setup_android.run(args.serial, args.apk, args.yes, control=not args.no_control)
        except (AdbError, OSError, RuntimeError) as exc:
            print(f"phonemirror: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    if args.platform is None:
        # adb device, then WebDriverAgent, otherwise wait for the phone app over Wi-Fi
        platform = autodetect() or "app"
        args = parser.parse_args((argv or sys.argv[1:]) + [platform])

    backend = None
    try:
        backend = make_backend(args)
        backend.start()
    except Exception as exc:
        if backend:
            backend.close()
        print(f"phonemirror: {exc}", file=sys.stderr)
        return 1
    from .viewer import Viewer
    try:
        return Viewer(backend, toolbar=not args.no_toolbar, fullscreen=args.fullscreen).run()
    except KeyboardInterrupt:
        return 0
    finally:
        backend.close()
        if backend.error:
            print(f"phonemirror: {backend.error}", file=sys.stderr)
