# PyInstaller spec:  pyinstaller kiwiremote.spec
# Produces two self-contained executables in dist/ on Windows and Linux,
# or a shared bundle at dist/KiwiSDR Remote.app on macOS:
#   "KiwiSDR Remote"   windowed GUI
#   "kiwiremote-cli"   console program for --headless and 'ctl' (on Windows the GUI exe has no console)
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

KC = os.path.join("third_party", "kiwiclient")
if not os.path.isfile(os.path.join(KC, "kiwi", "client.py")):
    raise SystemExit("Run 'python fetch_kiwiclient.py' first")
sys.path.insert(0, KC)

hidden = (["kiwi", "kiwi.client", "kiwi.worker", "kiwi.rigctld", "kiwi.wavreader",
           "kiwi.wsclient", "chunk"]
          + collect_submodules("mod_pywebsocket") + collect_submodules("kiwiremote"))
for optional in ("samplerate", "av"):
    try:
        __import__(optional)
        hidden.append(optional)
    except ImportError:
        print("NOTE: optional package %r not installed; build will lack it" % optional)

a = Analysis(["run_kiwiremote.py"], pathex=[".", KC], hiddenimports=hidden,
             excludes=["tkinter"], noarchive=False)
pyz = PYZ(a.pure)
assets = os.path.join(SPECPATH, "assets")
if sys.platform == "darwin":
    gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="KiwiSDR Remote",
              console=False, upx=False)
    cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="kiwiremote-cli",
              console=True, upx=False)
    coll = COLLECT(gui, cli, a.binaries, a.datas, name="KiwiSDR Remote", upx=False)
    # The shared collection can inherit the console launcher's background flag.
    app = BUNDLE(coll, name="KiwiSDR Remote.app", bundle_identifier="net.kiwiremote.app",
                 icon=os.path.join(assets, "kiwiremote.icns"),
                 info_plist={"LSBackgroundOnly": False, "NSHighResolutionCapable": True})
else:
    icon = os.path.join(assets, "kiwiremote.ico") if sys.platform == "win32" else None
    gui = EXE(pyz, a.scripts, a.binaries, a.datas, name="KiwiSDR Remote",
              console=False, upx=False, icon=icon)
    cli = EXE(pyz, a.scripts, a.binaries, a.datas, name="kiwiremote-cli",
              console=True, upx=False)
