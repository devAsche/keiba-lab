# 競馬用のWindowsタスクを登録する（Museが SSH から1回だけ実行する）
#   powershell -ExecutionPolicy Bypass -File "C:\AI model\keiba-lab\scripts\windows\register_tasks.ps1"
# 登録するもの（どれも時刻なし。Museが決まった時刻に  schtasks /run /tn "<名前>"  で起動する）:
#   Keiba-Raceday  開催日のオッズ記録（開催のない日はすぐ終わる）
#   Keiba-Nightly  過去結果の追加収集と仮想購入の精算（最大6時間）
#   Keiba-Preday   翌日のレースのオッズを1回だけ記録
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

function Register-KeibaTask($name, $bat, $hours, $description) {
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours $hours) -MultipleInstances IgnoreNew
  $action = New-ScheduledTaskAction -Execute "$here\$bat" -WorkingDirectory $here
  Register-ScheduledTask -TaskName $name -Action $action -Settings $settings -Principal $principal `
    -Description $description -Force | Out-Null
}

Register-KeibaTask "Keiba-Raceday" "run_raceday.bat" 13 "keiba-lab: 開催日のオッズ記録。Museが schtasks /run で起動する"
Register-KeibaTask "Keiba-Nightly" "run_nightly.bat" 7  "keiba-lab: 過去結果の収集と仮想購入の精算。Museが schtasks /run で起動する"
Register-KeibaTask "Keiba-Preday"  "run_preday.bat"  1  "keiba-lab: 翌日のオッズを1回記録。Museが schtasks /run で起動する"
Write-Output "OK: Keiba-Raceday / Keiba-Nightly / Keiba-Preday (on demand)"
