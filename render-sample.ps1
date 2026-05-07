$ErrorActionPreference = "Stop"

$Python = "python"
$ToolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Project = "D:\FocuSee\FocuSee Projects\Your Project.focusee"
$Output = Join-Path $ToolRoot "sample-output.mp4"

& $Python (Join-Path $ToolRoot "render_focusee.py") $Project --output $Output --width 1600 --height 1200 --fps 15 --preset ultrafast --progress
