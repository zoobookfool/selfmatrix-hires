<#
.SYNOPSIS
  Semi-automatic launcher for selfmatrix-hires participants (Windows).

.DESCRIPTION
  Reads a key=value config file (see hires.conf.example) and launches
  jacktrip with the assembled connection command. See docs/requirements.md
  §4.4. Works on Windows PowerShell 5.1 and PowerShell 7+.

.PARAMETER Config
  Path to the config file. Defaults to hires.conf next to this script.

.PARAMETER DryRun
  Print the command that would be run (PASSWORD masked) and exit without
  launching jacktrip.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Config,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Config) {
    $Config = Join-Path $ScriptDir 'hires.conf'
}

if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
    Write-Error "設定ファイルが見つかりません: $Config`n運用者から受け取った hires.conf をこのスクリプトと同じディレクトリに置くか、パスを引数で指定してください。雛形は hires.conf.example を参照してください。"
    exit 1
}

# Warn if the config file (which may contain PASSWORD) grants access beyond
# the owner. Windows ACLs are not POSIX bits, so we check for identities
# other than the owner/Administrators/SYSTEM having explicit access.
try {
    $acl = Get-Acl -LiteralPath $Config
    $owner = $acl.Owner
    $broadAccess = $acl.Access | Where-Object {
        $_.IdentityReference -notin @($owner, 'BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM') -and
        $_.AccessControlType -eq 'Allow' -and
        ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::Read)
    }
    if ($broadAccess) {
        $names = ($broadAccess | ForEach-Object { $_.IdentityReference.Value }) -join ', '
        Write-Warning "$Config は所有者以外 ($names) からも読み取り可能です。PASSWORD を書いている場合はアクセス許可の見直しを推奨します。"
    }
} catch {
    # ACL inspection is best-effort; do not block the connection on failure.
}

$settings = @{
    HOST         = ''
    SAMPLE_RATE  = '192000'
    BIT_RES      = '24'
    CHANNELS     = '2'
    QUEUE        = '8'
    USERNAME     = ''
    PASSWORD     = ''
    AUDIO_DEVICE = ''
    EXTRA_ARGS   = ''
}

foreach ($line in Get-Content -LiteralPath $Config) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) {
        continue
    }
    $idx = $trimmed.IndexOf('=')
    if ($idx -lt 0) {
        continue
    }
    $key = $trimmed.Substring(0, $idx).Trim()
    $value = $trimmed.Substring($idx + 1).Trim()
    if ($settings.ContainsKey($key)) {
        $settings[$key] = $value
    } else {
        Write-Warning "未知の設定キーを無視します: $key"
    }
}

if (-not $settings.HOST) {
    Write-Error "設定ファイルに HOST が指定されていません: $Config"
    exit 1
}
if (-not $settings.USERNAME) {
    Write-Error "設定ファイルに USERNAME が指定されていません: $Config"
    exit 1
}

$jacktripCmd = Get-Command jacktrip -ErrorAction SilentlyContinue
if (-not $jacktripCmd) {
    Write-Error "jacktrip コマンドが見つかりません。`nインストール手順: https://jacktrip.github.io/jacktrip/Install/"
    exit 1
}

$jacktripArgs = @(
    '-C', $settings.HOST,
    '-T', $settings.SAMPLE_RATE,
    '-b', $settings.BIT_RES,
    '-n', $settings.CHANNELS,
    '--udprt',
    '-q', $settings.QUEUE,
    '-R',
    '-A',
    '--username', $settings.USERNAME
)

if ($settings.PASSWORD) {
    $jacktripArgs += @('--password', $settings.PASSWORD)
}
if ($settings.AUDIO_DEVICE) {
    $jacktripArgs += @('--audiodevice', $settings.AUDIO_DEVICE)
}
if ($settings.EXTRA_ARGS) {
    # Intentional word-splitting: EXTRA_ARGS is documented in
    # hires.conf.example as a space-separated argument list.
    $jacktripArgs += ($settings.EXTRA_ARGS -split '\s+' | Where-Object { $_ -ne '' })
}

function Format-ArgForDisplay {
    param([string]$Value)
    if ($Value -match '\s') {
        return '"' + $Value + '"'
    }
    return $Value
}

function Write-MaskedCommand {
    # Mask by position (the argument right after --password), not by value —
    # value matching would also hide other fields that happen to share the
    # same string.
    $parts = @('jacktrip')
    $prev = ''
    foreach ($a in $jacktripArgs) {
        if ($prev -eq '--password') {
            $parts += '*****'
        } else {
            $parts += (Format-ArgForDisplay $a)
        }
        $prev = $a
    }
    Write-Output ($parts -join ' ')
}

if ($DryRun) {
    Write-MaskedCommand
    exit 0
}

& jacktrip @jacktripArgs
exit $LASTEXITCODE
