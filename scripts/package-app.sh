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
cp artifacts/whisperlivekit-mps-20260915/source/LICENSE "$app_bundle/Contents/Resources/WhisperLiveKit-LICENSE"

export CLANG_MODULE_CACHE_PATH="$project_root/.build/clang-module-cache"
export SWIFT_MODULECACHE_PATH="$project_root/.build/swift-module-cache"
swift scripts/MakeAppIcon.swift "$output_root/AppIcon.iconset"
iconutil -c icns "$output_root/AppIcon.iconset" -o "$app_bundle/Contents/Resources/AppIcon.icns"
python3 - "$app_bundle" "$project_root" <<'PY'
import plistlib,sys,re
from pathlib import Path
app=Path(sys.argv[1]); project=Path(sys.argv[2])
info={
    'CFBundleName':'本地翻译器', 'CFBundleDisplayName':'本地翻译器',
    'CFBundleIdentifier':'local.kevin.translator.m0', 'CFBundleExecutable':'LocalTranslatorApp',
    'CFBundlePackageType':'APPL', 'CFBundleShortVersionString':'0.2.2', 'CFBundleVersion':'5',
    'CFBundleIconFile':'AppIcon', 'LSMinimumSystemVersion':'26.0',
    'LSApplicationCategoryType':'public.app-category.productivity',
    'NSHighResolutionCapable':True, 'NSPrincipalClass':'NSApplication',
    'NSMicrophoneUsageDescription':'在你主动开始录音时采集英语音频，在本机生成并保存双语字幕。',
    'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},
    'M0ResourceRoot':str(project/'models'),
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
cat > "$output_root/使用说明.txt" <<'TXT'
本地翻译器 — 0.2.2 录音与恢复测试版

双击“本地翻译器.app”打开。适用于当前 Apple Silicon Mac，要求 macOS 26 或更新。
应用内提供：文字双向翻译、文档/图片提取及选段翻译、英语音频/视频连续转写与中文翻译。
录音页面新增主动麦克风输入、英文先显示、独立中文队列、保存任务、补译/失败重试、按片段回放、英文纠错和 TXT/SRT/VTT 导出。
“录后重新校对”根据完整保存的录音生成独立新版本并翻译，保留原字幕以便比较。长录音的流式结果可能漏词，建议录后校对再复核。
麦克风仅在点击开始并允许系统权限后采集；真人麦克风质量仍需实际验收。

模型没有重复装入应用：文字翻译使用本机 Ollama 中的 HY-MT2，语音读取项目 models 文件夹。
若服务未运行，打开“本机资源”并点击“启动本地服务”。
若模型移动了位置，打开“本机资源”重新选择包含 whisper-coreml 的 models 文件夹。
流式路径另需同一项目目录中准备的 Python 环境、WhisperLiveKit 补丁源码和 MLX large-v3-turbo 权重，详见项目 runtime/README.md。
本包可在这台 Mac 上移动使用；换到其他机器仍需另行准备 Ollama 和模型。

这是使用本机临时签名生成的自用测试包，未经过 Developer ID 公证或公开分发验收。
当前仍有识别错词、模型条件误译和估计时间戳交叠等问题。请核对原文与录音，勿把“处理完成”等同于质量验收。
新的录音任务保存在 ~/Library/Application Support/LocalTranslator/Recordings。可暂停/继续录音；设备变化或休眠会暂停，请手动继续。
“仅录音，稍后识别”可先保存音频；中断任务可点击“继续识别”，从最近已保存的安全位置重做尾部。若没有检查点，会从头识别；旧尾部保存在数据库恢复记录中。
超过 200 段的字幕可分页浏览，导出始终包含全部段落。
文字、文档及原生引擎对照仍使用各自的临时测试目录。
TXT
ditto -c -k --sequesterRsrc --keepParent "$app_bundle" "$output_root/本地翻译器-M0.zip"
printf '应用：%s\n压缩包：%s/本地翻译器-M0.zip\n' "$app_bundle" "$output_root"
