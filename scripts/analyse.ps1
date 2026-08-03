# Full analysis pass over the archive.
#
# Volatility is measured FROM observed changes, and the diff engine then USES
# that measurement -- so the analysis runs twice. The first pass is a probe: it
# exists to reveal which columns the publisher rewrites wholesale and which
# sources have keys that do not identify stable entities. The second pass is the
# one whose output is reported.
#
# Running only the first pass produces confident nonsense: 15,483 "revisions"
# that are mostly a clock field counting up.

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

Write-Output "==> pass 1: probe (classify changes without volatility knowledge)"
# The probe must start genuinely blind. Leaving a previous run's volatility
# measurement in place makes pass 1 filter out exactly the movement pass 1 exists
# to observe, and the re-measurement then finds nothing at all.
python scripts\reset_analysis.py
python -m palimpsest.diff --db archive\palimpsest.db --min-significance 2.0 | Select-Object -First 2
python scripts\record_stage.py archive\palimpsest.db probe

Write-Output "`n==> measuring which columns are recomputed, and which keys are stable"
python -m palimpsest.volatility --db archive\palimpsest.db

Write-Output "`n==> pass 2: reclassify with volatility applied"
# Only the classifications are discarded here. The volatility measurement just
# taken is what pass 2 is for.
python -c "import sqlite3;c=sqlite3.connect('archive/palimpsest.db');c.execute('DELETE FROM changes');c.execute('DELETE FROM diff_runs');c.commit()"
python -m palimpsest.diff --db archive\palimpsest.db --min-significance 2.0 | Select-Object -First 2
python scripts\record_stage.py archive\palimpsest.db controlled

Write-Output "`n==> done"
