<#
.SYNOPSIS
    Force deletes files and folders with permission issues on Windows
    
.DESCRIPTION
    Removes read-only attributes, takes ownership, grants permissions, and deletes
    even when access is denied. Works on stubborn files locked by processes.
    
.PARAMETER Path
    The full path to the file or folder to delete
    
.PARAMETER Force
    Skip all confirmations
    
.PARAMETER SkipProcessCheck
    Don't wait for processes to release the file
    
.EXAMPLE
    .\Force-Delete-Protected-File.ps1 -Path "C:\path\to\file.txt"
    
.EXAMPLE
    .\Force-Delete-Protected-File.ps1 -Path "C:\path\to\folder" -Force
    
.NOTES
    Requires Administrator privileges
    
.AUTHOR
    Created for removing permission-protected files
#>

#Requires -RunAsAdministrator

param(
    [Parameter(Mandatory=$true)]
    [string]$Path,
    
    [switch]$Force,
    [switch]$SkipProcessCheck
)

function Write-Header {
    param([string]$Title)
    Write-Host "`n" -ForegroundColor Cyan
    Write-Host "=" * 80 -ForegroundColor Cyan
    Write-Host $Title -ForegroundColor Cyan
    Write-Host "=" * 80 -ForegroundColor Cyan
}

function Write-Success {
    Write-Host "[✓] $($args[0])" -ForegroundColor Green
}

function Write-Warning {
    Write-Host "[!] $($args[0])" -ForegroundColor Yellow
}

function Write-ErrorMsg {
    Write-Host "[✗] $($args[0])" -ForegroundColor Red
}

function Write-Info {
    Write-Host "[i] $($args[0])" -ForegroundColor Cyan
}

function Write-Step {
    Write-Host "`n>>> $($args[0])" -ForegroundColor Magenta
}

Write-Header "Force Delete Protected File/Folder"
Write-Info "Target: $Path"

# Check if path exists
if (-not (Test-Path -Path $Path)) {
    Write-ErrorMsg "Path does not exist: $Path"
    exit 1
}

Write-Success "Path found"

# Get item info
$item = Get-Item -Path $Path -Force -ErrorAction SilentlyContinue
$isFolder = $item.PSIsContainer

Write-Info "Type: $(if ($isFolder) { 'Folder' } else { 'File' })"
Write-Info "Size: $(if ($isFolder) { 'Calculating...' } else { "$([math]::Round($item.Length / 1MB, 2)) MB" })"

if (-not $Force) {
    Write-Host "`nAbout to delete:" -ForegroundColor Yellow
    Write-Host "  $Path" -ForegroundColor Yellow
    $confirm = Read-Host "`nContinue? (yes/no)"
    if ($confirm -ne "yes") {
        Write-Host "Cancelled" -ForegroundColor Yellow
        exit 0
    }
}

# ============================================================================
# Step 1: Check for Process Locks
# ============================================================================
Write-Step "Checking for process locks"

if (-not $SkipProcessCheck) {
    $lockedProcesses = @()
    
    try {
        $allProcesses = Get-Process -ErrorAction SilentlyContinue
        
        foreach ($proc in $allProcesses) {
            try {
                $modules = $proc.Modules
                if ($null -ne $modules) {
                    foreach ($module in $modules) {
                        if ($module.FileName -like "$Path*" -or $Path -like "$($module.FileName)*") {
                            $lockedProcesses += @{
                                Process = $proc.ProcessName
                                PID = $proc.Id
                                Module = $module.FileName
                            }
                        }
                    }
                }
            } catch {
                # Ignore inaccessible processes
            }
        }
    } catch {
        Write-Info "Could not fully scan for locks: $_"
    }
    
    if ($lockedProcesses.Count -gt 0) {
        Write-Warning "File appears to be locked by these processes:"
        foreach ($lock in $lockedProcesses) {
            Write-Host "  - $($lock.Process) (PID: $($lock.PID))" -ForegroundColor Yellow
        }
        
        if (-not $Force) {
            Write-Host "`nOptions:" -ForegroundColor Cyan
            Write-Host "  1. Close the applications manually and try again"
            Write-Host "  2. Kill the processes now (may cause data loss)"
            Write-Host "  3. Cancel"
            
            $choice = Read-Host "Enter choice (1-3)"
            
            if ($choice -eq "2") {
                Write-Step "Killing locking processes"
                foreach ($lock in $lockedProcesses) {
                    try {
                        Stop-Process -Id $lock.PID -Force -ErrorAction Stop
                        Write-Success "Killed: $($lock.Process) (PID: $($lock.PID))"
                    } catch {
                        Write-Warning "Could not kill $($lock.Process): $_"
                    }
                }
                Start-Sleep -Seconds 2
            } elseif ($choice -eq "3") {
                Write-Host "Cancelled" -ForegroundColor Yellow
                exit 0
            } else {
                Write-Host "Please close the applications and run again" -ForegroundColor Yellow
                exit 0
            }
        } else {
            Write-Step "Killing locking processes (Force mode)"
            foreach ($lock in $lockedProcesses) {
                try {
                    Stop-Process -Id $lock.PID -Force -ErrorAction Stop
                    Write-Success "Killed: $($lock.Process) (PID: $($lock.PID))"
                } catch {
                    Write-Warning "Could not kill $($lock.Process): $_"
                }
            }
            Start-Sleep -Seconds 2
        }
    } else {
        Write-Success "No process locks detected"
    }
} else {
    Write-Info "Skipping process check"
}

# ============================================================================
# Step 2: Remove Read-Only Attributes
# ============================================================================
Write-Step "Removing read-only attributes"

try {
    if ($isFolder) {
        # Remove read-only from folder and all contents
        Get-ChildItem -Path $Path -Recurse -Force -ErrorAction SilentlyContinue | 
            ForEach-Object {
                if ($_.Attributes -band [System.IO.FileAttributes]::ReadOnly) {
                    $_.Attributes = $_.Attributes -band -bnot [System.IO.FileAttributes]::ReadOnly
                }
            }
    }
    
    # Remove from the item itself
    $item = Get-Item -Path $Path -Force
    if ($item.Attributes -band [System.IO.FileAttributes]::ReadOnly) {
        $item.Attributes = $item.Attributes -band -bnot [System.IO.FileAttributes]::ReadOnly
        Write-Success "Removed read-only attribute"
    } else {
        Write-Info "Item was not read-only"
    }
} catch {
    Write-Warning "Could not remove read-only attributes: $_"
}

# ============================================================================
# Step 3: Take Ownership
# ============================================================================
Write-Step "Taking ownership of file/folder"

try {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    Write-Info "Current user: $currentUser"
    
    # Use takeown command
    $pathEscaped = "`"$Path`""
    $output = cmd /c "takeown /f $pathEscaped /r /d y 2>&1" | Out-String
    
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Ownership transferred to: $currentUser"
    } else {
        Write-Warning "Takeown exit code: $LASTEXITCODE"
        Write-Info "Output: $output"
    }
} catch {
    Write-Warning "Could not take ownership via cmd: $_"
}

# ============================================================================
# Step 4: Grant Full Permissions
# ============================================================================
Write-Step "Granting full permissions"

try {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    
    # Method 1: Using icacls
    Write-Info "Using icacls to grant permissions..."
    $pathEscaped = "`"$Path`""
    $output = cmd /c "icacls $pathEscaped /grant `"$currentUser`:F`" /t /c 2>&1" | Out-String
    
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Full permissions granted via icacls"
    } else {
        Write-Warning "Icacls exit code: $LASTEXITCODE"
        Write-Info "Output: $output"
    }
    
    # Method 2: Using PowerShell ACL (backup method)
    Write-Info "Applying PowerShell ACL permissions..."
    try {
        $acl = Get-Acl -Path $Path
        $permission = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $currentUser,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]::ContainerInherit + [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.SetAccessRule($permission)
        Set-Acl -Path $Path -AclObject $acl
        Write-Success "PowerShell ACL permissions applied"
    } catch {
        Write-Warning "PowerShell ACL method failed: $_"
    }
} catch {
    Write-Warning "Could not grant permissions: $_"
}

# ============================================================================
# Step 5: Delete Item
# ============================================================================
Write-Step "Deleting file/folder"

$deleteSuccess = $false

# Method 1: PowerShell Remove-Item
try {
    Write-Info "Attempting standard deletion..."
    Remove-Item -Path $Path -Recurse -Force -ErrorAction Stop
    Write-Success "Item deleted successfully!"
    $deleteSuccess = $true
} catch {
    Write-Warning "Standard deletion failed: $_"
    
    # Method 2: Using cmd.exe del command
    if (-not $isFolder) {
        try {
            Write-Info "Attempting del command..."
            $pathEscaped = "`"$Path`""
            $output = cmd /c "del /f /q $pathEscaped 2>&1"
            
            if (-not (Test-Path -Path $Path)) {
                Write-Success "Item deleted via del command"
                $deleteSuccess = $true
            } else {
                Write-Warning "Del command did not remove the file"
            }
        } catch {
            Write-Warning "Del command failed: $_"
        }
    }
    
    # Method 3: Using cmd.exe rmdir command (for folders)
    if ($isFolder -and -not $deleteSuccess) {
        try {
            Write-Info "Attempting rmdir command..."
            $pathEscaped = "`"$Path`""
            $output = cmd /c "rmdir /s /q $pathEscaped 2>&1"
            
            if (-not (Test-Path -Path $Path)) {
                Write-Success "Folder deleted via rmdir command"
                $deleteSuccess = $true
            } else {
                Write-Warning "Rmdir command did not remove the folder"
            }
        } catch {
            Write-Warning "Rmdir command failed: $_"
        }
    }
    
    # Method 4: Using .NET DirectoryInfo (nuclear option)
    if (-not $deleteSuccess) {
        try {
            Write-Info "Attempting .NET deletion method..."
            
            if ($isFolder) {
                $dirInfo = New-Object System.IO.DirectoryInfo($Path)
                $dirInfo.Delete($true)
            } else {
                $fileInfo = New-Object System.IO.FileInfo($Path)
                $fileInfo.Delete()
            }
            
            if (-not (Test-Path -Path $Path)) {
                Write-Success "Item deleted via .NET method"
                $deleteSuccess = $true
            } else {
                Write-Warning ".NET deletion did not remove the item"
            }
        } catch {
            Write-Warning ".NET deletion failed: $_"
        }
    }
}

# ============================================================================
# Step 6: Verify Deletion
# ============================================================================
Write-Step "Verifying deletion"

if (Test-Path -Path $Path) {
    Write-ErrorMsg "Item still exists at: $Path"
    
    # Last resort options
    Write-Host "`nLast Resort Options:" -ForegroundColor Yellow
    Write-Host "  1. Restart Windows in Safe Mode and try deletion"
    Write-Host "  2. Use third-party tools: Unlocker, LockHunter, or Eraser"
    Write-Host "  3. Boot from live USB and delete from there"
    Write-Host "  4. Check if antivirus is scanning the file"
    
    exit 1
} else {
    Write-Success "Item successfully deleted!"
    Write-Success "Path no longer exists: $Path"
    
    Write-Header "SUCCESS"
    Write-Info "The file/folder has been completely removed from the system"
}

Write-Host "`n"