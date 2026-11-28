<#
.SYNOPSIS
  Insights Kit の「アイテム別の進捗表」(CSV) を GitHub から取ってきて、指定したフォルダに保存します。

.DESCRIPTION
  社内の Windows PC で、タスク スケジューラから毎朝動かすためのスクリプトです (このファイルはリポジトリには置きません)。
  - 保存先は複数指定できます。SharePoint は、OneDrive で同期したフォルダを指定します (保存すると OneDrive がアップロードします)。
    サーバーの共有フォルダ (\\server\share\...) も指定できます。
  - GitHub に残っている CSV (30 日分) のうち、保存先にまだ無い日の分を保存します。すでにあるファイルは上書きしません。
  - 通信先は GitHub (api.github.com) と、GitHub がダウンロード用に返す一時的な URL だけです。ほかへは送信しません。
  - トークンは Windows の DPAPI で暗号化してファイルに保存します (この PC のこのユーザーだけが読めます)。

.PARAMETER Repo
  ワークフローを置いたリポジトリ (例: acme/portal)。

.PARAMETER Destination
  保存先のフォルダ。複数ある場合は、カンマで並べるか、セミコロン (;) で区切った 1 つの文字列にします。

.PARAMETER TokenFile
  暗号化したトークンのファイル。既定はこのスクリプトと同じフォルダの insights-kit-token.txt。

.PARAMETER SaveToken
  トークンを入力して暗号化して保存します (最初の 1 回だけ)。

.PARAMETER RegisterTask
  このスクリプトを、平日の毎朝 -At の時刻に動くタスクとしてタスク スケジューラに登録します。

.PARAMETER At
  -RegisterTask で登録する時刻 (既定 09:30。キットは 09:00 に動くので、その後にします)。

.PARAMETER LogFile
  ログの保存先。既定はこのスクリプトと同じフォルダの insights-kit-csv.log。

.EXAMPLE
  .\Get-InsightsKitCsv.ps1 -SaveToken
  (最初の 1 回) トークンを貼り付けて保存します。

.EXAMPLE
  .\Get-InsightsKitCsv.ps1 -Repo acme/portal -Destination "C:\Users\taro\Contoso\PMO - 進捗表"
  SharePoint (OneDrive で同期したフォルダ) に保存します。

.EXAMPLE
  .\Get-InsightsKitCsv.ps1 -Repo acme/portal -Destination "C:\Users\taro\Contoso\PMO - 進捗表", "\\fileserver\pm\進捗表"
  SharePoint とサーバーの両方に保存します。

.EXAMPLE
  .\Get-InsightsKitCsv.ps1 -Repo acme/portal -Destination "C:\Users\taro\Contoso\PMO - 進捗表" -RegisterTask
  平日 09:30 に自動で動くよう、タスク スケジューラに登録します。
#>
[CmdletBinding()]
param(
    [string]$Repo,
    [string[]]$Destination,
    [string]$TokenFile = (Join-Path $PSScriptRoot 'insights-kit-token.txt'),
    [switch]$SaveToken,
    [switch]$RegisterTask,
    [string]$At = '09:30',
    [string]$LogFile = (Join-Path $PSScriptRoot 'insights-kit-csv.log')
)

$ErrorActionPreference = 'Stop'
$CsvPattern = '^item-progress-\d{4}-\d{2}-\d{2}\.csv$'

function Write-Log([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    if ($LogFile) { Add-Content -Path $LogFile -Value $line -Encoding UTF8 }
}

function Get-Token {
    if (Test-Path -LiteralPath $TokenFile) {
        $secure = Get-Content -LiteralPath $TokenFile | ConvertTo-SecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    if ($env:INSIGHTS_KIT_TOKEN) { return $env:INSIGHTS_KIT_TOKEN }
    throw "トークンがありません。先に「.\Get-InsightsKitCsv.ps1 -SaveToken」で保存してください ($TokenFile)"
}

function Invoke-GitHubApi([string]$Path, [string]$Token) {
    $headers = @{
        Authorization          = "Bearer $Token"
        Accept                 = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
        'User-Agent'           = 'insights-kit-csv-download'
    }
    return Invoke-RestMethod -Uri "https://api.github.com$Path" -Headers $headers -UseBasicParsing
}

function Save-Artifact([string]$Url, [string]$Token, [string]$OutFile) {
    # GitHub はダウンロード用の一時的な URL にリダイレクトする。Windows PowerShell 5.1 はリダイレクト先にも
    # トークンを送ってしまい失敗するため、リダイレクトは自分でたどり、リダイレクト先にはトークンを送らない。
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.AllowAutoRedirect = $false
    $req.Headers['Authorization'] = "Bearer $Token"
    $req.Headers['X-GitHub-Api-Version'] = '2022-11-28'
    $req.Accept = 'application/vnd.github+json'
    $req.UserAgent = 'insights-kit-csv-download'
    $resp = $req.GetResponse()
    try {
        $code = [int]$resp.StatusCode
        $location = $resp.Headers['Location']
    } finally { $resp.Close() }
    if ($code -ge 300 -and $code -lt 400 -and $location) {
        if (-not $location.StartsWith('https://')) { throw "想定外のダウンロード先です: $location" }
        Invoke-WebRequest -Uri $location -OutFile $OutFile -UseBasicParsing -Headers @{ 'User-Agent' = 'insights-kit-csv-download' }
    } else {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -Headers @{
            Authorization = "Bearer $Token"; 'User-Agent' = 'insights-kit-csv-download' }
    }
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    if ($SaveToken) {
        $secure = Read-Host -AsSecureString 'GitHub のトークンを貼り付けて Enter (画面には表示されません)'
        $secure | ConvertFrom-SecureString | Set-Content -LiteralPath $TokenFile -Encoding ASCII
        Write-Host "保存しました: $TokenFile (この PC のこのユーザーだけが読めるように暗号化しています)"
        exit 0
    }

    if ($Repo -notmatch '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$') { throw '-Repo には owner/name の形で、ワークフローを置いたリポジトリを指定してください (例: acme/portal)' }
    $dests = @($Destination | ForEach-Object { $_ -split ';' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($dests.Count -eq 0) { throw '-Destination で保存先のフォルダを 1 つ以上指定してください' }

    if ($RegisterTask) {
        $destArg = ($dests -join ';')
        $argLine = '-NoProfile -ExecutionPolicy RemoteSigned -File "{0}" -Repo {1} -Destination "{2}" -TokenFile "{3}" -LogFile "{4}"' -f `
            $PSCommandPath, $Repo, $destArg, $TokenFile, $LogFile
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable  # PC が止まっていた日は、起動後に動かす
        Register-ScheduledTask -TaskName 'Insights Kit CSV' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "タスク「Insights Kit CSV」を登録しました (平日 $At。ログオン中に動きます)"
        exit 0
    }

    $token = Get-Token
    foreach ($d in $dests) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }

    # 取得できる CSV の一覧 (同じ日の CSV が複数あれば、最後の実行の分)
    $artifacts = @()
    for ($page = 1; $page -le 10; $page++) {
        $res = Invoke-GitHubApi "/repos/$Repo/actions/artifacts?per_page=100&page=$page" $token
        $artifacts += @($res.artifacts)
        if (@($res.artifacts).Count -lt 100) { break }
    }
    $latest = $artifacts | Where-Object { $_.name -match $CsvPattern -and -not $_.expired } |
        Group-Object name | ForEach-Object { $_.Group | Sort-Object created_at -Descending | Select-Object -First 1 } |
        Sort-Object name

    $saved = 0
    $tmpDir = Join-Path ([IO.Path]::GetTempPath()) 'insights-kit-csv'
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
    foreach ($a in $latest) {
        $missing = @($dests | Where-Object { -not (Test-Path -LiteralPath (Join-Path $_ $a.name)) })
        if ($missing.Count -eq 0) { continue }
        $tmp = Join-Path $tmpDir $a.name
        Save-Artifact $a.archive_download_url $token $tmp
        foreach ($d in $missing) {
            Copy-Item -LiteralPath $tmp -Destination (Join-Path $d $a.name)
            Write-Log "保存: $(Join-Path $d $a.name)"
            $saved++
        }
        Remove-Item -LiteralPath $tmp -Force
    }
    Write-Log ("完了: {0} 件保存 (GitHub にある CSV {1} 日分、保存先 {2} か所)" -f $saved, @($latest).Count, $dests.Count)
    exit 0
} catch {
    $msg = $_.Exception.Message
    if ($msg -match '\(404\)|\(401\)|\(403\)') {
        $msg += ' (リポジトリ名が違うか、トークンの期限切れ・権限不足 (Actions: Read-only) の可能性があります)'
    }
    Write-Log "エラー: $msg"
    exit 1
}
