# Pull the droplet's data archive to this machine.
#
# The droplet keeps a rolling 14-day window and exports everything older
# to date-partitioned gzipped files. Those files are append-only and
# complete once the day has passed, so this only fetches what is missing
# -- there is no rsync on Windows and re-copying finished days would be
# wasted bandwidth on a growing archive.
#
# The exception is the most recent file in each folder, which is still
# being written to. That one is always re-fetched.
#
# Runs from this machine and connects OUT to the droplet, so nothing has
# to be opened up on the home network.

param(
    [string]$Destination = "D:\kalshi-data",
    [string]$RemoteHost  = "root@68.183.104.17",
    [string]$RemoteRoot  = "/root/kalshi_weather_bot/data",
    [int]$BulkThreshold  = 50
)

$ErrorActionPreference = "Stop"

function Write-Status($message) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $message)
}

Write-Status "listing remote files"
$listing = & ssh -o BatchMode=yes -o ConnectTimeout=20 $RemoteHost `
    "find $RemoteRoot -type f -name '*.gz' -printf '%P\t%s\n'"
if ($LASTEXITCODE -ne 0) {
    throw "could not reach $RemoteHost - check the SSH key"
}

$remote = @{}
foreach ($line in $listing) {
    if (-not $line) { continue }
    $parts = $line -split "`t"
    if ($parts.Count -ge 2) { $remote[$parts[0]] = [int64]$parts[1] }
}
Write-Status "$($remote.Count) remote files"

# The newest file in each folder is still being appended to, so its size
# on disk is not final and a size match does not mean it is complete.
$newestPerFolder = @{}
foreach ($rel in $remote.Keys) {
    $folder = Split-Path $rel -Parent
    if (-not $newestPerFolder.ContainsKey($folder) -or
        $rel -gt $newestPerFolder[$folder]) {
        $newestPerFolder[$folder] = $rel
    }
}

$toFetch = @()
foreach ($rel in $remote.Keys | Sort-Object) {
    $local = Join-Path $Destination $rel
    $folder = Split-Path $rel -Parent
    $isNewest = ($newestPerFolder[$folder] -eq $rel)
    if (-not (Test-Path $local)) {
        $toFetch += $rel
    } elseif ($isNewest) {
        $toFetch += $rel          # still growing; take it again
    } elseif ((Get-Item $local).Length -ne $remote[$rel]) {
        $toFetch += $rel          # size drifted: re-fetch rather than guess
    }
}

if ($toFetch.Count -eq 0) {
    Write-Status "nothing to fetch - already current"
    exit 0
}
Write-Status "fetching $($toFetch.Count) file(s)"

# Each scp is its own SSH handshake, which is fine for a daily catch-up
# of a handful of files and badly wrong for a first sync of 1,472. Past a
# threshold, stream the whole set through one connection as a tar
# instead: one handshake, and the files compress as a batch.
if ($toFetch.Count -gt $BulkThreshold) {
    Write-Status "over $BulkThreshold files - streaming as a single archive"
    if (-not (Test-Path $Destination)) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }
    # Build the tar remotely and fetch it as a file. Piping the stream
    # straight through PowerShell corrupts it -- 5.1 cannot carry binary
    # from a native command through a pipeline, and Set-Content -Encoding
    # Byte rejects the string it gets handed.
    $tarPath = Join-Path $Destination "_bulk.tar"
    $remoteTar = "/tmp/kalshi-data-bulk.tar"
    & ssh -o BatchMode=yes $RemoteHost "cd $RemoteRoot && tar cf $remoteTar ."
    if ($LASTEXITCODE -eq 0) {
        & scp -q -o BatchMode=yes "${RemoteHost}:$remoteTar" $tarPath
        & ssh -o BatchMode=yes $RemoteHost "rm -f $remoteTar"
    }
    if (Test-Path $tarPath) {
        & tar -xf $tarPath -C $Destination
        Remove-Item $tarPath -Force
        $total = (Get-ChildItem $Destination -Recurse -File |
                  Measure-Object -Property Length -Sum)
        Write-Status ("local archive: {0} files, {1} MB" -f $total.Count,
                      [math]::Round($total.Sum / 1MB, 1))
        exit 0
    }
    Write-Warning "bulk transfer failed - falling back to per-file"
}

$fetched = 0
$bytes = 0
foreach ($rel in $toFetch) {
    $local = Join-Path $Destination $rel
    $dir = Split-Path $local -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    # Download beside the target and move into place, so an interrupted
    # transfer never leaves a truncated file that the next run would
    # mistake for a complete one.
    $tmp = "$local.part"
    & scp -q -o BatchMode=yes "${RemoteHost}:$RemoteRoot/$rel" $tmp
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "failed: $rel"
        if (Test-Path $tmp) { Remove-Item $tmp -Force }
        continue
    }
    Move-Item -Path $tmp -Destination $local -Force
    $fetched++
    $bytes += (Get-Item $local).Length
}

$mb = [math]::Round($bytes / 1MB, 1)
Write-Status "fetched $fetched file(s), $mb MB"

$total = (Get-ChildItem $Destination -Recurse -File -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum)
Write-Status ("local archive: {0} files, {1} MB" -f $total.Count,
              [math]::Round($total.Sum / 1MB, 1))
