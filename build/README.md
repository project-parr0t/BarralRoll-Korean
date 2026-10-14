# R5 reproducible build

This directory contains the title-specific glue needed to reproduce the R5
release artifact. It deliberately does not contain game data, packaged patch
payloads, copied SDK files, or the third-party Noto font binary.

## Inputs

- A clean checkout of `milkyway0308/parr0t` at the commit recorded in
  `instance.json`.
- An unmodified Steam build of Barrel Roll matching every source binding in
  `instance.json`.
- `repak` and `retoc` executables matching the recorded versions and hashes.
- The exact Noto Sans KR build-input asset attached to the GitHub Release.
- Python 3.11+ with `requirements.lock` installed.

## Command

```powershell
python build/build.py `
  --game-root "C:\Program Files (x86)\Steam\steamapps\common\Barrel Roll\v1_1_0" `
  --parr0t-repo C:\src\parr0t `
  --noto-font C:\inputs\NotoSansKR-VF-2.04-build-input.ttf `
  --repak C:\tools\repak.exe `
  --retoc C:\tools\retoc.exe `
  --output out
```

The build is read-only with respect to the game directory. It validates every
declared input, extracts into the output directory, reconstructs reviewed
localization input from the canonical checkpoint, rebuilds the patch PAK and
installer ZIP, and rejects any byte mismatch with the recorded R5 outputs.
Only hashes, counts, and pass/fail metadata are printed.
