# Registers a Task Scheduler job that starts the co-pilot at user logon.
$projectDir = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectDir ".venv\Scripts\pythonw.exe"   # pythonw = no console window
$runScript = Join-Path $projectDir "run.py"

if (-not (Test-Path $python)) {
    Write-Error "venv not found at $python - run setup first (see README)."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$runScript`"" -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName "crypto-copilot" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Personal crypto alerts co-pilot (Telegram)" -Force

Write-Host "Registered. It will start at next logon. Start now with:"
Write-Host "  Start-ScheduledTask -TaskName crypto-copilot"
