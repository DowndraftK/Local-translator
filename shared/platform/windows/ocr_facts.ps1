# System WinRT facts only; workflow, revisions, ranges and export stay in shared core.
param([string]$RequestPath,[string]$ResultPath)
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
$utf8=New-Object System.Text.UTF8Encoding $false
function Await($operation,[Type]$resultType) {
 $method=[System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {$_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1} | Select-Object -First 1
 $task=$method.MakeGenericMethod($resultType).Invoke($null,@($operation))
 if(-not $task.Wait(30000)){throw 'WinRT current page exceeded 30 seconds; caller owns cancellation process'}
 return $task.GetAwaiter().GetResult()
}
$stream=$bitmap=$converted=$null
try {
 $file=Get-Item -LiteralPath $RequestPath
 if($file.Length -gt 65536){throw 'Oversized OCR request'}
 $req=[System.IO.File]::ReadAllText($RequestPath,$utf8) | ConvertFrom-Json
 if($req.schema -ne 1){throw 'Unsupported OCR fact schema'}
 $start=[System.Diagnostics.Stopwatch]::StartNew()
 $dll=Get-Item -LiteralPath (Join-Path $env:SystemRoot 'System32\Windows.Media.Ocr.dll')
 $engineHash=(Get-FileHash -LiteralPath $dll.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
 $languages=@([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object {$_.LanguageTag})
 $resources=@(Get-ChildItem -LiteralPath (Join-Path $env:SystemRoot 'OCR') -Recurse -File | Sort-Object FullName | ForEach-Object {
  @{path=$_.FullName;bytes=$_.Length;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
 })
 $backend='winrt-ocr-v1;Windows='+[Environment]::OSVersion.Version.ToString()+';engine='+$engineHash
 $reply=@{schema=1;request=$req.request;action=$req.action;backend=$backend;complete=$true}
 if($req.action -eq 'ocr_probe') {
  $reply.availableLanguages=$languages
  $reply.profile=@{engine='Windows.Media.Ocr';engineVersion=$dll.VersionInfo.FileVersion;engineBytes=$dll.Length;engineSHA256=$engineHash;
   resources=$resources;license='Microsoft Windows system components; local reuse, no redistribution';nativeMaxEdge=[Windows.Media.Ocr.OcrEngine]::MaxImageDimension;
   coordinateSystem='pixel-top-left';confidence=$null;orientation='up';autoDeskew=$false}
 } elseif($req.action -eq 'ocr') {
  $language=if($req.direction -eq 'zh-en'){'zh-Hans-CN'}elseif($req.direction -eq 'en-zh'){'en-US'}else{throw 'Invalid OCR direction'}
  if($languages -notcontains $language){throw ('ocr_resource_missing: '+$language+'; no automatic download or language fallback')}
  $image=Get-Item -LiteralPath $req.image
  if($image.Length -ne $req.imageBytes -or (Get-FileHash -LiteralPath $image.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $req.imageSHA256){throw 'Image identity mismatch'}
  $storage=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($image.FullName)) ([Windows.Storage.StorageFile])
  $stream=Await ($storage.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap=Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  if([Math]::Max($bitmap.PixelWidth,$bitmap.PixelHeight) -gt 3000 -or $bitmap.PixelWidth*$bitmap.PixelHeight -gt 9000000){throw 'Product OCR pixel guard exceeded'}
  $converted=[Windows.Graphics.Imaging.SoftwareBitmap]::Convert($bitmap,[Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,[Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)
  $engine=[Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language($language)))
  if($null -eq $engine){throw 'Offline recognizer unavailable'}
  $result=Await ($engine.RecognizeAsync($converted)) ([Windows.Media.Ocr.OcrResult])
  $observations=New-Object System.Collections.Generic.List[object]
  $lineIndex=0
  foreach($line in $result.Lines){$wordIndex=0;foreach($word in $line.Words){
   if($observations.Count -ge 4096){throw 'Product single-page observation guard exceeded'}
   $r=$word.BoundingRect
   $observations.Add(@{id=('page'+$req.page+'-line'+$lineIndex+'-word'+$wordIndex);order=$observations.Count;line=$lineIndex;word=$wordIndex;
    text=$word.Text;confidence=$null;rect=@($r.X,$r.Y,$r.Width,$r.Height)})
   $wordIndex++
  };$lineIndex++}
  if($utf8.GetByteCount($result.Text) -gt 1048576){throw 'Product OCR text guard exceeded'}
  $raw=@{backend=$backend;request=$req.request;observations=@($observations.ToArray());pixelWidth=$bitmap.PixelWidth;pixelHeight=$bitmap.PixelHeight;
   rotation=$req.render.rotation;cropBox=$req.render.cropBox;scale=$req.render.scale;orientation='up';coordinateSystem='pixel-top-left';
   languages=@($language);textAngle=$result.TextAngle;recognitionComplete=$true;objectsReleasedAtProcessExit=$true}
  $reply.fingerprint=$req.fingerprint;$reply.page=$req.page;$reply.text=$result.Text;$reply.ocr=$raw
 } else {throw 'Unsupported OCR fact action'}
 $reply.elapsedSeconds=$start.Elapsed.TotalSeconds
 $json=$reply | ConvertTo-Json -Depth 30 -Compress
 if($utf8.GetByteCount($json) -gt 4194304){throw 'OCR facts exceeded 4MiB'}
 [System.IO.File]::WriteAllText($ResultPath+'.tmp',$json,$utf8)
 Move-Item -LiteralPath ($ResultPath+'.tmp') -Destination $ResultPath
} catch {
 [Console]::Error.WriteLine($_.Exception.ToString());exit 2
} finally {
 if($null -ne $converted){$converted.Dispose()}
 if($null -ne $bitmap){$bitmap.Dispose()}
 if($null -ne $stream){$stream.Dispose()}
}
