# Regenerate the evidence bundle from the live archive, verify it, and publish.
#
# Run this whenever you want the public site to reflect current findings. It
# refuses to publish an archive that fails verification -- a tamper-evident
# archive that ships unverified would be worse than no archive at all.

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

Write-Output "==> classifying changes between observations"
python -m palimpsest.diff --db archive\palimpsest.db | Select-Object -First 3

Write-Output "`n==> verifying the archive"
python -m palimpsest.verify --db archive\palimpsest.db
if ($LASTEXITCODE -ne 0) {
    Write-Error "archive failed verification -- refusing to publish"
    exit 1
}

Write-Output "`n==> exporting the evidence bundle"
python -m palimpsest.report --db archive\palimpsest.db --out web\data.json

Write-Output "`n==> archive summary"
python -c "import json;d=json.load(open('web/data.json',encoding='utf-8'));o=d['overview'];c=d['churn'];print(f\"  datasets   : {o['sources_watched']} across {o['city_count']} cities\");print(f\"  sweeps     : {o['sweeps']}\");print(f\"  observations: {o['observations']:,}\");print(f\"  findings   : {c['material_changes']:,} material, {c['discarded_as_mechanism']:,} discarded as mechanism\")"

$changed = git status --porcelain web/data.json
if (-not $changed) {
    Write-Output "`nno change to the bundle; nothing to publish"
    exit 0
}

Write-Output "`n==> publishing"
git add web/data.json
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -q -m "Update evidence bundle ($stamp)`n`nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push origin main

Write-Output "`npublished. Pages will redeploy automatically."
