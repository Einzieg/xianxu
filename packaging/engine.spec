from pathlib import Path
from importlib.metadata import distributions
from PyInstaller.utils.hooks import collect_submodules
import sys

root = Path(SPECPATH).parent
model = root / "models" / "basic-pitch"
licenses = []
for distribution in distributions():
    for file in distribution.files or []:
        if any(word in file.name.lower() for word in ("license", "licence", "copying", "copyright", "notice")):
            source = Path(distribution.locate_file(file))
            if source.is_file():
                licenses.append((str(source), "licenses/" + distribution.metadata["Name"] + "/" + str(file.parent)))
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    licenses.append((str(python_license), "licenses/Python"))
a = Analysis(
    [str(root / "music_service.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(model / name), "models/basic-pitch") for name in ("nmp.onnx", "LICENSE", "SOURCES.json")] + licenses,
    hiddenimports=["main", "pynput.keyboard._win32", "pynput.mouse._win32"] + collect_submodules("scipy._external"),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="rock-music-engine",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="rock-music-engine")
