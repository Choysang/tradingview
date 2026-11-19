# pulse3 NATIVE 7x24 news recorder (NOT registered: switched on only at the M6 cutover, replacing Pulse3NewsImport's legacy-news step).
# Polls Tonghuashun (10 s), Eastmoney (30 s) and CLS (30 s) like the old recorder, 08:00-22:00 Asia/Shanghai on weekdays, one client per host
# (<= 1 request/second per host), and stops a host for the day on the first block (403/429/456/501) without a retry. Message versions become
# document revisions of the source native_news_recorder; claims are extracted after every poll that stored something.
# Cutover registration (main thread): a task starting this script at 08:00 weekdays (single instance, IgnoreNew) PLUS the existing alert
# steps of run_news_import.ps1 (alerts scan / deliver, every 10 minutes) without its legacy-news steps.
# NEVER run it while the old system's news recorder is still polling from the same IP (two systems must not double the requests).
. C:\pulse3\pulse3_env.ps1
if (-not $cutover) { 'refused: the old recorder still runs (cutover_state.json is not cutover)'; exit 4 }
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\news_record_$day.log"
Add-Content $log "== $(Get-Date -Format s) rec_news record"
& $py -m pulse.workers.rec_news --news-dir "$root\var\native_news" record --until 22:00 *>> $log
if ($LASTEXITCODE -ne 0) { Add-Content $log "!! rec_news failed exit $LASTEXITCODE (2 = a host blocked us and was stopped for the day)" }
