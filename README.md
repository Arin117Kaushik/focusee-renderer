# FocuSee Renderer

Local renderer for readable FocuSee project folders.

## Setup

Run once:

```powershell
cd "C:\path\to\focusee-renderer"
python -m pip install --target vendor -r requirements.txt
```

## Render

```powershell
python .\render_focusee.py "D:\FocuSee\FocuSee Projects\Your Project.focusee" --output ".\output.mp4" --width 1600 --height 1200 --fps 15 --preset ultrafast --progress
```

To run the included sample helper despite Windows script policy:

```powershell
powershell -ExecutionPolicy Bypass -File ".\render-sample.ps1"
```

## Current Output

- Uses `resource/background.png`
- Places `recording/display-0.mp4` with padding, rounded corners, and shadow
- Overlays cursor images from `recording/cursors`
- Draws click pulse effects from `mouseclicks-0.json`
- Optionally draws typed keys with `--keystrokes`
- Applies a first-pass approximation of configured zoom tracks

For a higher-quality export, raise `--fps` to `30` and omit `--preset ultrafast`. That will take longer.
