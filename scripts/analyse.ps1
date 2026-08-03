# Full analysis pass over the archive.
#
# Volatility is measured FROM observed changes, and the diff engine then USES
# that measurement -- so the analysis runs twice. The first pass is a probe: it
# exists to reveal which columns the publisher rewrites wholesale, which fields
# are merely other fields' arithmetic, and which sources have keys that do not
# identify stable entities. The second pass is the one whose output is reported.
#
# Running only the first pass produces confident nonsense: 15,483 "revisions"
# that are mostly a clock field counting up.

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

# PowerShell does not treat a non-zero exit from a native command as an error,
# so a step can crash and the pipeline carry on regardless. That happened: the
# volatility measurement died on a NameError, the run reported success, and the
# results were silently those of an unmeasured archive. Every step is checked.
function Invoke-Step {
    param([string]$Label, [scriptblock]$Body)
    Write-Output $Label
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Error "FAILED (exit $LASTEXITCODE): $Label"
        exit 1
    }
}

Invoke-Step "==> pass 1: probe (classify changes without volatility knowledge)" {
    # The probe must start genuinely blind. Leaving a previous run's measurement
    # in place makes pass 1 filter out exactly the movement pass 1 exists to
    # observe, and the re-measurement then finds nothing at all.
    python scripts\reset_analysis.py
}
Invoke-Step "" { python -m palimpsest.diff --db archive\palimpsest.db --min-significance 2.0 }
Invoke-Step "" { python scripts\record_stage.py archive\palimpsest.db probe }

Invoke-Step "`n==> measuring publisher behaviour: recomputed columns, derived fields, key stability" {
    python -m palimpsest.volatility --db archive\palimpsest.db
}

Invoke-Step "`n==> pass 2: reclassify with that knowledge applied" {
    # Only the classifications are discarded here. The measurement just taken is
    # what pass 2 is for.
    python -c "import sqlite3;c=sqlite3.connect('archive/palimpsest.db');c.execute('DELETE FROM changes');c.execute('DELETE FROM diff_runs');c.commit()"
}
Invoke-Step "" { python -m palimpsest.diff --db archive\palimpsest.db --min-significance 2.0 }
Invoke-Step "" { python scripts\record_stage.py archive\palimpsest.db controlled }

Write-Output "`n==> done"
