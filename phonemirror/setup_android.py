"""`phonemirror setup android`: install the phone app and set it up over USB.

Everything it changes is limited to the phonemirror app itself and can be undone with
`phonemirror setup android --undo`:
  * installs or updates the app (never downgrades, never removes other apps),
  * adds phonemirror control to the enabled accessibility services, keeping the others,
  * sends a pairing to the app, which asks the user to confirm.
"""

import hashlib
import os
import shutil
import tempfile
import time
import urllib.request

from .adb import Adb, AdbError
from .link import pairing as pairing_mod

PACKAGE = "io.github.nanorex.phonemirror"
SERVICE = f"{PACKAGE}/{PACKAGE}.ControlService"
SERVICE_SHORT = f"{PACKAGE}/.ControlService"
RELEASE_URL = "https://github.com/nano-rex/phone-mirror/releases/latest/download/"
APK_NAME = "phonemirror-android.apk"

USB_STEPS = """\
Connect the phone with a USB cable and turn on USB debugging:
  1. Settings > About phone > tap "Build number" 7 times (enter your PIN if asked).
  2. Settings > System > Developer options > turn on "USB debugging".
  3. Accept "Allow USB debugging?" on the phone (tick "Always allow from this computer").
You can turn USB debugging off again when setup is done; Wi-Fi mirroring does not need it."""


def ask(question, assume_yes):
    if assume_yes:
        print(f"{question} [Y/n] y")
        return True
    try:
        return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


def wait_for_device(adb, timeout=300):
    """Return a ready serial, guiding the user until one appears."""
    shown = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        devices = adb.devices()
        ready = [d for d in devices if d[1] == "device"]
        if adb.serial:
            ready = [d for d in ready if d[0] == adb.serial]
        if len(ready) == 1:
            return ready[0][0]
        if len(ready) > 1:
            raise AdbError("several phones are connected; choose one with --serial: "
                           + ", ".join(d[0] for d in ready))
        hint = USB_STEPS
        if any(d[1] == "unauthorized" for d in devices):
            hint = 'The phone is connected. Tap "Allow" on the "Allow USB debugging?" prompt.'
        if hint != shown:
            print("\n" + hint + "\n\nWaiting for the phone... (Ctrl+C to cancel)")
            shown = hint
        time.sleep(1.5)
    raise AdbError("no phone found")


def download_apk(directory):
    """Download the latest release APK and check it against its published SHA-256."""
    path = os.path.join(directory, APK_NAME)
    print(f"Downloading {APK_NAME} from the latest GitHub release...")
    with urllib.request.urlopen(RELEASE_URL + APK_NAME + ".sha256", timeout=60) as resp:
        expected = resp.read().decode().split()[0].lower()
    digest = hashlib.sha256()
    with urllib.request.urlopen(RELEASE_URL + APK_NAME, timeout=300) as resp, open(path, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            f.write(chunk)
    if digest.hexdigest() != expected:
        raise RuntimeError("downloaded APK does not match its checksum; not installing it")
    return path


def installed_version(adb):
    out = adb.run("shell", "dumpsys", "package", PACKAGE, check=False)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("versionName="):
            return line.split("=", 1)[1]
    return None


def install(adb, apk):
    out = adb.run("install", "-r", apk, check=False, timeout=300)
    if "Success" in out:
        return
    if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:
        raise AdbError("a phonemirror app signed with a different key is already installed. "
                       "Uninstall it on the phone (it only removes phonemirror and its pairing), "
                       "then run setup again.")
    if "INSTALL_FAILED_VERSION_DOWNGRADE" in out:
        print("A newer phonemirror app is already installed; keeping it.")
        return
    raise AdbError(f"installing the app failed: {out.strip()}")


def enabled_services(adb):
    value = adb.run("shell", "settings", "get", "secure", "enabled_accessibility_services").strip()
    if value in ("", "null"):
        return []
    return [s for s in value.split(":") if s]


def is_ours(entry):
    return entry in (SERVICE, SERVICE_SHORT)


def enable_control(adb):
    services = enabled_services(adb)
    if any(is_ours(s) for s in services):
        print("Control is already on.")
        return True
    new = services + [SERVICE]
    adb.run("shell", "settings", "put", "secure", "enabled_accessibility_services", ":".join(new))
    adb.run("shell", "settings", "put", "secure", "accessibility_enabled", "1")
    after = enabled_services(adb)
    # every service that was on must still be on
    if not all(s in after for s in services) or not any(is_ours(s) for s in after):
        raise AdbError("Android did not accept the change; turn on control from the phone app.")
    print("Control is on.")
    return True


def disable_control(adb):
    services = enabled_services(adb)
    kept = [s for s in services if not is_ours(s)]
    if kept == services:
        return
    if kept:
        adb.run("shell", "settings", "put", "secure", "enabled_accessibility_services", ":".join(kept))
    else:
        adb.run("shell", "settings", "delete", "secure", "enabled_accessibility_services", check=False)
        adb.run("shell", "settings", "put", "secure", "accessibility_enabled", "0")
    print("Control is off.")


def send_pairing(adb, store, port):
    pairing = pairing_mod.new_pairing()
    store.add(pairing)
    uri = pairing_mod.pairing_uri(pairing, store.pc_id, pairing_mod.computer_name(),
                                  pairing_mod.local_addresses(), port)
    # quoted for the phone's shell; the URI itself never contains a single quote
    adb.run("shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity",
            "-a", f"{PACKAGE}.PAIR", "--es", "uri", f"'{uri}'")
    print('On the phone, tap "Pair" to confirm.')


def run(serial=None, apk=None, assume_yes=False, control=True, pair=True,
        port=pairing_mod.DEFAULT_PORT, store=None):
    adb = Adb(serial)
    adb.serial = wait_for_device(adb)
    model = adb.run("shell", "getprop", "ro.product.model", check=False).strip()
    print(f"Found {model or adb.serial}.")

    current = installed_version(adb)
    tmp = None
    try:
        if apk is None:
            tmp = tempfile.mkdtemp(prefix="phonemirror-")
            apk = download_apk(tmp)
        print(("Updating" if current else "Installing") + " the phonemirror app...")
        install(adb, apk)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    if control and ask("Let this computer control the phone while mirroring "
                       "(turns on phonemirror control in Accessibility)?", assume_yes):
        enable_control(adb)
    if pair:
        send_pairing(adb, store or pairing_mod.PairingStore(), port)
    print("\nDone. Run `phonemirror app` here, then tap Start mirroring on the phone.")
    return 0


def undo(serial=None, assume_yes=False):
    adb = Adb(serial)
    adb.serial = wait_for_device(adb)
    disable_control(adb)
    if installed_version(adb) and ask("Uninstall the phonemirror app from the phone?", assume_yes):
        adb.run("uninstall", PACKAGE, check=False)
        print("Uninstalled.")
    return 0
