$ErrorActionPreference = 'Stop'

$repo = 'E:\UCI-BUCI'
$file = 'UNENDLESS-CODING-MASTER.md'

Set-Location -LiteralPath $repo

# Verify correct repository
$remote = (git remote get-url origin).Trim()

if ($remote -notmatch 'chatgptopenaiagi/UCI-BUCI(\.git)?$') {
    throw "Unexpected repository: $remote"
}

# Read the original directive from clipboard
$content = Get-Clipboard -Raw

if (
    -not $content.StartsWith('UNENDLESS CODING') -or
    -not $content.Contains('BEGIN CONTINUOUS AUTONOMOUS ENGINEERING.')
) {
    throw 'Clipboard does not contain the complete original directive.'
}

# Preserve any existing version
if (Test-Path -LiteralPath $file) {
    Copy-Item $file "$file.backup" -Force
}

# Save original text without modifying its contents
[System.IO.File]::WriteAllText(
    (Join-Path $repo $file),
    $content,
    [System.Text.UTF8Encoding]::new($false)
)

# Confirm saved bytes match clipboard content
$saved = [System.IO.File]::ReadAllText(
    (Join-Path $repo $file),
    [System.Text.Encoding]::UTF8
)

if ($saved -cne $content) {
    throw 'Text verification failed.'
}

# Commit only the master directive
git add -- $file

git -c user.name='chatgptopenaiagi' `
    -c user.email='207417324+chatgptopenaiagi@users.noreply.github.com' `
    commit -m 'Preserve original UNENDLESS CODING universal directive'

if ($LASTEXITCODE -ne 0) {
    throw 'Git commit failed.'
}

# Publish current branch
$branch = (git branch --show-current).Trim()

git push -u origin $branch

if ($LASTEXITCODE -ne 0) {
    throw 'GitHub push failed.'
}

# Verify remote commit
$localCommit = (git rev-parse HEAD).Trim()

$remoteCommit = (
    gh api "repos/chatgptopenaiagi/UCI-BUCI/branches/$branch" `
        --jq '.commit.sha'
).Trim()

if ($localCommit -ne $remoteCommit) {
    throw 'Remote verification failed.'
}

Write-Host "`nUNENDLESS CODING MASTER DIRECTIVE PUBLISHED!" -ForegroundColor Green
Write-Host "Commit: $localCommit"
Write-Host "Branch: $branch"

Get-FileHash $file -Algorithm SHA256