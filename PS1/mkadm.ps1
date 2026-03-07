param (
    [Parameter(Mandatory=$true, HelpMessage="Username for the new owner.")]
    [string]$NewUser,

    [Parameter(Mandatory=$false, HelpMessage="Password for the new account (Optional).")]
    [string]$Password = ""
)

# 1. Prepare Password Object
$SecurePassword = ConvertTo-SecureString $Password -AsPlainText -Force

# 2. Create the Local User
Write-Host "Creating user: $NewUser..." -ForegroundColor Cyan
if (-not (Get-LocalUser -Name $NewUser -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name $NewUser -Password $SecurePassword -FullName "New Owner" -Description "Local Admin Account" -Force
    
    # 3. Add to Administrators Group
    Add-LocalGroupMember -Group "Administrators" -Member $NewUser
    Write-Host "Success! User '$NewUser' is now a Local Administrator." -ForegroundColor Green
} else {
    Write-Warning "User '$NewUser' already exists."
}

Write-Host "Next Step: Log out and log into '$NewUser' before running the cleanup script." -ForegroundColor White
