# PowerShell tab completion for `make` in this repo.
#
# Enables completions like:
#   make u<Tab>            -> make up
#   make up oa<Tab>        -> make up oauth2-proxy
#   make up sandbox san<Tab> -> make up sandbox sandbox-runner
#
# One-shot (current session only):
#   . 'C:\Users\Amber Price\Desktop\claude usage\scripts\completion.ps1'
#
# Permanent (append to $PROFILE):
#   Add-Content $PROFILE ". 'C:\Users\Amber Price\Desktop\claude usage\scripts\completion.ps1'"

Register-ArgumentCompleter -Native -CommandName make, gmake -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $verbs = @(
        'setup', 'network', 'up', 'down', 'clean', 'very-clean',
        'build', 'logs', 'help', 'completion', 'completion-bash',
        'list-stacks', 'list-services'
    )
    $stackVerbs = @('up', 'down', 'clean', 'very-clean', 'build', 'logs', 'list-services')

    # Positional tokens already on the line (skip `make` itself, flags, VAR=VAL).
    $tokens = @($commandAst.CommandElements |
        Select-Object -Skip 1 |
        ForEach-Object { $_.ToString() } |
        Where-Object { $_ -and $_ -notmatch '^-' -and $_ -notmatch '=' })

    # The word currently being typed shows up as the tail token — drop it so
    # we don't count it as a completed positional.
    if ($tokens.Count -gt 0 -and $tokens[-1] -eq $wordToComplete) {
        if ($tokens.Count -eq 1) { $tokens = @() }
        else { $tokens = $tokens[0..($tokens.Count - 2)] }
    }

    $verb  = if ($tokens.Count -ge 1) { $tokens[0] } else { $null }
    $stack = if ($tokens.Count -ge 2) { $tokens[1] } else { $null }

    $candidates = @()

    if (-not $verb) {
        $candidates = $verbs
    }
    elseif ($stackVerbs -contains $verb) {
        if (-not $stack) {
            $out = & make -s list-stacks 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $candidates = ($out -join ' ') -split '\s+' | Where-Object { $_ }
            }
        }
        else {
            $out = & make -s list-services $stack 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $candidates = ($out -join ' ') -split '\s+' | Where-Object { $_ }
            }
        }
    }

    $candidates |
        Where-Object { $_ -like "$wordToComplete*" } |
        Sort-Object -Unique |
        ForEach-Object {
            [System.Management.Automation.CompletionResult]::new(
                $_, $_, 'ParameterValue', $_
            )
        }
}
