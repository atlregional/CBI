<#
.SYNOPSIS
Captures real, working SharePoint sharing links for every corridor Word
report in the CBI OneDrive/SharePoint folder, via the Microsoft Graph API.

.DESCRIPTION
The regional HTML report's "Download Report" links are built from a
predictable, path-based SharePoint URL (see SHAREPOINT_CORRIDOR_REPORTS_
BASE_URL in cbi_generate_regional_report.py) that matches the local
outputs/multi_corridor folder structure exactly. But a real SharePoint
"Copy Link" URL also carries a per-file ?d=...&csf=1&web=1&e=... token
that identifies a specific share link SharePoint generated for that file
-- there is no way to reconstruct that token from the file path alone.

If the plain path-based links in the generated report don't open
correctly for you, run this script instead. It signs you in interactively
(browser popup, your ARC Microsoft 365 account), finds every corridor
.docx under Desktop/CBI/outputs/multi_corridor in your OneDrive, creates
(or reuses, if one already exists) an organization-scoped share link for
each one -- "organization" scope means anyone signed into an
atlantaregional.org account can open it with no extra prompt, matching
"accessible only to our agency internally" -- and writes a CSV mapping
corridor slug -> real, working link.

Uses raw Microsoft Graph REST calls (Invoke-MgGraphRequest) rather than
higher-level module cmdlets (Get-MgDriveItemChild etc.), since the Graph
v1.0 REST endpoints themselves are stable across SDK versions even though
cmdlet names have shifted between Microsoft.Graph PowerShell SDK releases.
This has NOT been run/tested against the live tenant -- if a specific call
errors, the fix is almost always in the $body shape or the URL path, not
the overall approach.

.REQUIREMENTS
Install-Module Microsoft.Graph.Authentication -Scope CurrentUser
(one-time; only the Authentication submodule is needed, since everything
else here goes through Invoke-MgGraphRequest directly)

.USAGE
    .\cbi_get_sharepoint_links.ps1
    .\cbi_get_sharepoint_links.ps1 -OutputCsv "C:\some\path\links.csv"
#>

param(
    [string]$RelativeFolderPath = "Desktop/CBI/outputs/multi_corridor",
    [string]$OutputCsv = "$PSScriptRoot\..\data\sharepoint_corridor_links.csv"
)

$ErrorActionPreference = "Stop"

Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
Connect-MgGraph -Scopes "Files.Read" -NoWelcome

Write-Host "Resolving folder: $RelativeFolderPath"
$folder = Invoke-MgGraphRequest -Method GET `
    -Uri "https://graph.microsoft.com/v1.0/me/drive/root:/$RelativeFolderPath"

$corridorFolders = Invoke-MgGraphRequest -Method GET `
    -Uri "https://graph.microsoft.com/v1.0/me/drive/items/$($folder.id)/children?`$top=999"

$results = @()
foreach ($corridorFolder in $corridorFolders.value) {
    if (-not $corridorFolder.folder) { continue }

    $children = Invoke-MgGraphRequest -Method GET `
        -Uri "https://graph.microsoft.com/v1.0/me/drive/items/$($corridorFolder.id)/children?`$top=999"

    $docx = $children.value |
        Where-Object { $_.name -like "*.docx" } |
        Sort-Object lastModifiedDateTime -Descending |
        Select-Object -First 1
    if (-not $docx) {
        Write-Warning "No .docx found under $($corridorFolder.name), skipping."
        continue
    }

    $body = @{ type = "view"; scope = "organization" } | ConvertTo-Json
    $link = Invoke-MgGraphRequest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/me/drive/items/$($docx.id)/createLink" `
        -Body $body -ContentType "application/json"

    $results += [PSCustomObject]@{
        Slug     = $corridorFolder.name
        FileName = $docx.name
        Link     = $link.link.webUrl
    }
    Write-Host "  $($corridorFolder.name) -> $($link.link.webUrl)"
}

$results | Export-Csv -Path $OutputCsv -NoTypeInformation
Write-Host "Wrote $($results.Count) links to $OutputCsv"
