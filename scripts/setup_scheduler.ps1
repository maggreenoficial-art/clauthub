# Registra tarefa agendada para atualizar metricas 1x por dia as 08:00
$TaskName = "VisuCliente-AtualizarMetricas"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source

if (-not $Python) {
    Write-Error "Python nao encontrado. Instale Python 3 e tente novamente."
    exit 1
}

$Script = Join-Path $ProjectRoot "scripts\update_metrics.py"
$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At "08:00"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Busca seguidores Instagram e recalcula investimentos automaticamente"

Write-Host "Tarefa $TaskName criada - execucao diaria as 08:00"
Write-Host "Para rodar agora: python scripts\update_metrics.py"
