$pythonScript = "C:\Users\fubar\Documents\repo\SCRIPTS\PY\cropmuka.py"

$rootPath = "."

$foldersWithImages = Get-ChildItem -Path $rootPath -Recurse -File -Include *.jpg, *.jpeg | 
Select-Object -ExpandProperty DirectoryName -Unique

Write-Host "Found $($foldersWithImages.Count) folders containing images." -ForegroundColor Cyan

foreach ($folder in $foldersWithImages) {
        Write-Host "Processing: $folder" -ForegroundColor Yellow
        Set-Location -Path $folder
        python $pythonScript
        Write-Host "Done with $folder" -ForegroundColor Green
}

Write-Host "All passport folders processed!" -ForegroundColor White -BackgroundColor DarkGreen
