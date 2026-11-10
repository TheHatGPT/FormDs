<#
.SYNOPSIS
    One-command build verification, GitHub push, and minimal-footprint VM deploy for Form D's Scout Bot.

.EXAMPLES
    ./ship.ps1 -VmOnly -FirstTimeSetup # Stop local .exe -> Upload runtime files + .env + formd_state.db + deploy_vm.sh -> Start 24/7 VM service
    ./ship.ps1 -VmOnly                 # Push updated runtime .py files straight to VM and restart service
    ./ship.ps1 "Add new feature"       # Commit & push to GitHub -> Push runtime files to VM
#>

param(
    [Parameter(Position = 0)]
    [string]$Message = "",
    [switch]$VmOnly,
    [switch]$GitOnly,
    [switch]$InstallDeps,
    [switch]$FirstTimeSetup
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Config = @{
    REMOTE_DIR    = "~/formd-scout"
    SERVICE_NAME  = "formd-scout"
    VM_SSH_TARGET = ""
    VM_SSH_KEY    = ""
    VM_NAME       = ""
    VM_ZONE       = "us-central1-a"
}

$EnvFiles = @(
    (Join-Path (Split-Path $PSScriptRoot -Parent) ".cloud_vm.env"),
    (Join-Path $PSScriptRoot ".deploy.env")
)

foreach ($EnvPath in $EnvFiles) {
    if (Test-Path $EnvPath) {
        Get-Content $EnvPath | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $parts = $line.Split("=", 2)
                $key = $parts[0].Trim()
                $val = $parts[1].Trim().Trim('"').Trim("'")
                if (-not [string]::IsNullOrWhiteSpace($val)) {
                    $Config[$key] = $val
                }
            }
        }
    }
}

$RuntimePyFiles = @(
    "auditor.py",
    "bot.py",
    "config.py",
    "filters.py",
    "integrations.py",
    "models.py",
    "sources.py",
    "storage.py",
    "requirements.txt"
)

$FilesToPush = @($RuntimePyFiles)
if ($FirstTimeSetup) {
    if (Test-Path (Join-Path $PSScriptRoot "deploy_vm.sh")) { $FilesToPush += "deploy_vm.sh" }
    if (Test-Path (Join-Path $PSScriptRoot ".env")) { $FilesToPush += ".env" }
    if (Test-Path (Join-Path $PSScriptRoot "formd_state.db")) { $FilesToPush += "formd_state.db" }
}

if (-not $VmOnly) {
    Write-Host "==> Syncing repository to GitHub..." -ForegroundColor Cyan
    if (-not (Test-Path (Join-Path $PSScriptRoot ".git"))) {
        Write-Warning "Git is not initialized yet. Run 'git init -b main' or use -VmOnly."
    } else {
        git add -A
        $status = git status --porcelain
        if ($status) {
            if ([string]::IsNullOrWhiteSpace($Message)) {
                $Message = "Update $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
            }
            git commit -m $Message
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        $remotes = git remote
        if ($remotes -contains "origin") {
            git push -u origin main
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            Write-Host "✅ Pushed latest commit to GitHub (origin/main)." -ForegroundColor Green
        }
    }
}

if (-not $GitOnly) {
    $RemoteDir = $Config["REMOTE_DIR"]
    $ServiceName = $Config["SERVICE_NAME"]
    $SshTarget = $Config["VM_SSH_TARGET"]
    $SshKey = $Config["VM_SSH_KEY"]

    Write-Host "==> Stopping local FormDScoutBot.exe so only the 24/7 Cloud VM instance runs..." -ForegroundColor Cyan
    Stop-Process -Name FormDScoutBot -Force -ErrorAction SilentlyContinue

    Write-Host "==> Deploying $($FilesToPush.Count) items to $SshTarget ($RemoteDir)..." -ForegroundColor Cyan
    $SshArgs = @("-o", "StrictHostKeyChecking=accept-new")
    $ScpArgs = @("-r", "-o", "StrictHostKeyChecking=accept-new")
    if (-not [string]::IsNullOrWhiteSpace($SshKey)) {
        $SshArgs += @("-i", $SshKey)
        $ScpArgs += @("-i", $SshKey)
    }

    & ssh @SshArgs $SshTarget "mkdir -p $RemoteDir"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & scp @ScpArgs @FilesToPush "${SshTarget}:${RemoteDir}/"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if ($FirstTimeSetup) {
        $RemoteCmd = "cd $RemoteDir && sed -i 's/\r$//' deploy_vm.sh && chmod +x deploy_vm.sh && ./deploy_vm.sh"
    } else {
        $PipStep = if ($InstallDeps) {
            "echo 'Updating dependencies (no cache)...' && $RemoteDir/.venv/bin/pip install --no-cache-dir -r $RemoteDir/requirements.txt && "
        } else { "" }
        $RemoteCmd = "${PipStep}sudo systemctl restart $ServiceName && sudo systemctl status $ServiceName --no-pager -l -n 5"
    }

    & ssh @SshArgs $SshTarget $RemoteCmd
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "✅ Live VM updated and '$ServiceName' running 24/7!" -ForegroundColor Green
}
