# lanip.ps1 — печатает LAN-адрес этой машины для start_game.bat.
#
# Зачем отдельный файл: тот же код однострочником внутри .bat ломался на экранировании
# кавычек (и `for /f` собирал пустоту либо мусор вида «\ 172.27.240.1 \»). Здесь один
# вывод — один адрес, без лишних строк.
#
# Берём только домашние сети: 192.168.* и 10.*. Прочие (169.254 — сети нет,
# 198.18 — VPN-туннель, 172.27 — Hyper-V) с телефона недоступны, а стоят в
# списке первыми и раньше перехватывали выбор. Нет адреса — пустой вывод,
# и launcher сам откатится на 127.0.0.1.
$ErrorActionPreference = 'SilentlyContinue'

$ip = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -match '^(192\.168\.|10\.)' -and -not $_.SkipAsSource } |
    Select-Object -First 1 -ExpandProperty IPAddress

if ($ip) { Write-Output $ip }
