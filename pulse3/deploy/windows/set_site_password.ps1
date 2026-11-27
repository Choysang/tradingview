# Set or change the site login password. Run it in an interactive SSH session (ssh -t ... powershell -File C:\pulse3\set_site_password.ps1).
# The password is typed twice and only its scrypt hash is stored (C:\pulse3\secrets\site_password.txt); a new password logs out all sessions.
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
& "$root\app\.venv\Scripts\python.exe" -m pulse.workers.site_password
exit $LASTEXITCODE
