# Writes the OpenAI key to pulse3's protected folder (#199). Run on the server, for example from the PC:
#   ssh -t -i <key> <user>@<server> powershell -NoProfile -ExecutionPolicy Bypass -File C:\pulse3\set_openai_key.ps1
# The key is typed at a hidden prompt, so it never appears in a command line, a history file or a log.
# Plain UTF-8 without a byte order mark, one line. pulse3_env.ps1 prefers this file over the old C:\pulse copy.
param([string]$Path = 'C:\pulse3\secrets\openai.key')

$ErrorActionPreference = 'Stop'
$secure = Read-Host 'OpenAI key' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if (-not $key -or $key -match '[^\x21-\x7E]') {
    throw 'The key is empty or has characters a key cannot have. Nothing was written.'
}
$dir = Split-Path $Path
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
[IO.File]::WriteAllText($Path, $key, (New-Object Text.UTF8Encoding $false))
$written = [IO.File]::ReadAllBytes($Path).Length
if ($written -ne $key.Length) {
    throw "Wrote $written bytes for a key of $($key.Length) characters."
}
"OK: $written characters written to $Path on $env:COMPUTERNAME"
