param (
    [Parameter(Mandatory=$true, HelpMessage="Enter the username of the account you want to delete.")]
    [string]$OldUser
)

# Check for Admin rights
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Please run this script as an Administrator!"
    exit
}

Write-Host "--- Starting Cleanup for User: $OldUser ---" -ForegroundColor Cyan

# 1. Remove the User Profile (This deletes the C:\Users folder and Registry Hive)
Write-Host "Removing Windows User Profile..." -ForegroundColor Yellow
$Profile = Get-CimInstance -Class Win32_UserProfile | Where-Object { $_.LocalPath -like "*\$OldUser" }

if ($Profile) {
    $Profile | Remove-CimInstance
    Write-Host "Profile for $OldUser deleted successfully." -ForegroundColor Green
} else {
    Write-Warning "No profile found for $OldUser in the Registry. Checking local user list..."
}

# 2. Remove the Local User Account
if (Get-LocalUser -Name $OldUser -ErrorAction SilentlyContinue) {
    Remove-LocalUser -Name $OldUser
    Write-Host "User account $OldUser removed from the system." -ForegroundColor Green
}

# 3. System-wide Temp & Cache Cleanup
Write-Host "Cleaning system-wide temporary files..." -ForegroundColor Yellow
$TempPaths = @(
    "C:\Windows\Temp\*", 
    "C:\Windows\Prefetch\*", 
    "C:\Windows\SoftwareDistribution\Download\*"
)

foreach ($path in $TempPaths) {
    Get-ChildItem -Path $path -Recurse -ErrorAction SilentlyContinue | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
}

Write-Host "Cleanup Complete! The system is now lean and mean." -ForegroundColor Green
Write-Host "Pro Tip: Run 'cipher /w:C:' next to overwrite the deleted space." -ForegroundColor White
