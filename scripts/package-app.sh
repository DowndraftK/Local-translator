#!/bin/bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
configuration="${1:-release}"
if [ "$configuration" != release ] && [ "$configuration" != debug ]; then
    printf '用法：bash scripts/package-app.sh [release|debug]\n' >&2
    exit 1
fi
output_root="$project_root/dist/M0-$(date +%Y%m%d-%H%M%S)-$configuration"
app_bundle="$output_root/本地翻译器.app"
mkdir -p "$output_root"
python3 - "$output_root" "$project_root" <<'PYBUILD'
import plistlib,sys
from pathlib import Path
output,project=map(Path,sys.argv[1:]);existing=[21]
for path in (project/'dist').glob('*/build-number.txt'):
    try: existing.append(int(path.read_text()))
    except (OSError,ValueError): pass
for path in (project/'dist').glob('*/本地翻译器.app/Contents/Info.plist'):
    try: existing.append(int(plistlib.loads(path.read_bytes())['CFBundleVersion']))
    except (OSError,ValueError,KeyError,plistlib.InvalidFileException): pass
(output/'build-number.txt').write_text(str(max(existing)+1)+'\n')
PYBUILD
bash scripts/swift.sh build -c "$configuration" > "$output_root/build.log" 2>&1
build_root="$(bash scripts/swift.sh build -c "$configuration" --show-bin-path)"
mkdir -p "$app_bundle/Contents/MacOS" "$app_bundle/Contents/Resources"
cp "$build_root/LocalTranslatorApp" "$app_bundle/Contents/MacOS/LocalTranslatorApp"
cp "$build_root/translator-m0" "$app_bundle/Contents/MacOS/translator-m0"
# SwiftPM libraries contain the implementation. Preserve their resource bundles as well.
for resources in "$build_root/"*.bundle; do
    if [ -d "$resources" ]; then ditto "$resources" "$app_bundle/Contents/Resources/$(basename "$resources")"; fi
done
cp Vendor/argmax-oss-swift/LICENSE "$app_bundle/Contents/Resources/WhisperKit-LICENSE"
cp Vendor/argmax-oss-swift/NOTICES "$app_bundle/Contents/Resources/WhisperKit-NOTICES"
cp Vendor/ZIPFoundation/LICENSE "$app_bundle/Contents/Resources/ZIPFoundation-LICENSE"
mkdir -p "$app_bundle/Contents/Resources/StreamingRuntime/streaming_translator"
cp runtime/streaming_translator/*.py "$app_bundle/Contents/Resources/StreamingRuntime/streaming_translator/"
cp runtime/worker_bootstrap.py "$app_bundle/Contents/Resources/StreamingRuntime/"
cp runtime/component-catalog.json "$app_bundle/Contents/Resources/component-catalog.json"
cp artifacts/whisperlivekit-speech-repair-20261001-final/source/LICENSE "$app_bundle/Contents/Resources/WhisperLiveKit-LICENSE"

export CLANG_MODULE_CACHE_PATH="$project_root/.build/clang-module-cache"
export SWIFT_MODULECACHE_PATH="$project_root/.build/swift-module-cache"
swift scripts/MakeAppIcon.swift "$output_root/AppIcon.iconset"
iconutil -c icns "$output_root/AppIcon.iconset" -o "$app_bundle/Contents/Resources/AppIcon.icns"
python3 - "$app_bundle" "$project_root" <<'PY'
import plistlib,sys,re
from pathlib import Path
app=Path(sys.argv[1]); project=Path(sys.argv[2])
build_number=int((app.parent/'build-number.txt').read_text())
info={
    'CFBundleName':'本地翻译器', 'CFBundleDisplayName':'本地翻译器',
    'CFBundleIdentifier':'local.kevin.translator.m0', 'CFBundleExecutable':'LocalTranslatorApp',
    'CFBundlePackageType':'APPL', 'CFBundleShortVersionString':'0.2.10', 'CFBundleVersion':str(build_number),
    'CFBundleIconFile':'AppIcon', 'LSMinimumSystemVersion':'26.0',
    'LSApplicationCategoryType':'public.app-category.productivity',
    'NSHighResolutionCapable':True, 'NSPrincipalClass':'NSApplication',
    'NSMicrophoneUsageDescription':'在你主动开始录音时采集英语音频，在本机生成并保存双语字幕。',
    'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},
    'M0ResourceRoot':'',
}
with (app/'Contents/Info.plist').open('wb') as f: plistlib.dump(info,f)
# This personal build keeps source evidence in the project. Resolve the copied
# report's links so they still work from its distribution directory.
report=project/'docs/M0模型联调报告.md'
def resolve_link(match):
    value=match.group(1)
    return match.group(0) if '://' in value or value.startswith('#') else ']('+str((report.parent/value).resolve())+')'
(app.parent/'模型联调报告.md').write_text(re.sub(r'\]\(([^)]+)\)',resolve_link,report.read_text()))
PY
codesign --force --sign - "$app_bundle/Contents/MacOS/translator-m0"
codesign --force --sign - "$app_bundle"
codesign --verify --deep --strict "$app_bundle"
"$app_bundle/Contents/MacOS/translator-m0" --help > "$output_root/worker-check.txt"
cp docs/0.2.10安装与修复说明.md "$output_root/使用说明.md"

ditto -c -k --sequesterRsrc --keepParent "$app_bundle" "$output_root/本地翻译器-M0.zip"
dmg_stage="$output_root/dmg-stage"
mkdir -p "$dmg_stage"
ditto "$app_bundle" "$dmg_stage/本地翻译器.app"
ln -s /Applications "$dmg_stage/Applications"
cp "$output_root/使用说明.md" "$dmg_stage/安装说明.md"
hdiutil create -volname "本地翻译器 0.2.10" -srcfolder "$dmg_stage" -format UDZO "$output_root/本地翻译器-0.2.10.dmg" > "$output_root/dmg-create.log"
hdiutil verify "$output_root/本地翻译器-0.2.10.dmg" > "$output_root/dmg-verify.log"
printf '应用：%s\n压缩包：%s/本地翻译器-M0.zip\n' "$app_bundle" "$output_root"
