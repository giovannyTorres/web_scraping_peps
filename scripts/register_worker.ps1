param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][string]$DataDirectory,
    [string]$Name = 'PEPS-MX-Worker'
)
# Ejecutar sólo al decidir mantener recolección autorizada en esta máquina.
$ErrorActionPreference = 'Stop'
$ExePath = (Resolve-Path -LiteralPath $Executable).Path
$DataPath = (Resolve-Path -LiteralPath $DataDirectory).Path
if ($ExePath.Contains('"') -or $DataPath.Contains('"')) { throw 'Ruta inválida' }
$Arguments = '--data-dir "' + $DataPath + '" --online worker --daily --workers 4 --seconds 240 --max-jobs 1000'
$Action = New-ScheduledTaskAction -Execute $ExePath -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Procesa la cola PEPS; respeta fuentes permitidas y pausas persistidas' -Force
