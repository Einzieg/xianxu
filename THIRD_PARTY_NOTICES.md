# Third-party components

The root MIT license covers original Xianxu code, not third-party components.

## Spotify Basic Pitch

Copyright 2022 Spotify AB. Apache License 2.0.
The ONNX model and adapted onset-decoding/time-alignment logic in `music_neural.py`
derive from Basic Pitch. Changes and provenance are recorded in
`models/basic-pitch/SOURCES.json`; the original license is retained at
`models/basic-pitch/LICENSE`. No endorsement by Spotify is implied.

## DD virtual input

DD is an optional separately supplied component. Upstream:
https://github.com/ddxoft/master and https://www.ddxoft.com/ .
The inspected upstream contains a binary archive and README, but no MIT LICENSE.
Its README states that the free version authenticates online when loading.
Do not infer a redistribution license from a free download, a valid signature, or
this project's MIT license. No DD binary is committed in this repository.
The installer integration uses vendor `ddc.exe` without undocumented flags.
Distributors must establish the applicable license before publishing an installer
containing the DLL, INF, CAT, SYS, or vendor installer.

## Other dependencies

Tauri, React, Python, NumPy, SciPy, ONNX Runtime, SoundFile, SoundCard, mido,
CustomTkinter, pynput, PyAutoGUI, PyInstaller and their dependencies retain their
own licenses. See the locked manifests and their distributions' license files.
PyInstaller's bootloader exception does not relicense bundled dependencies.
Personal song libraries and downloaded music/scores are intentionally excluded.
