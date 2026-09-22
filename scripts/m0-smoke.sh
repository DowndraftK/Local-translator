#!/bin/bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
run_dir="artifacts/smoke-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$run_dir"
bash scripts/swift.sh build > "$run_dir/build.log" 2>&1
binary="$(bash scripts/swift.sh build --show-bin-path)/translator-m0"
"$binary" doctor --output "$run_dir/doctor.json"

# Two passes measure a first request and a subsequent request. A first request is
# not necessarily a cold OS/model cache; the JSON records Ollama's load duration.
for size in 1.8b 7b; do
    for pass in first warm; do
        "$binary" translate --input fixtures/translation-en.txt --model "hy-mt2:$size-q8" --output "$run_dir/text-$size-$pass.json"
    done
    "$binary" translate --input fixtures/translation-zh.txt --model "hy-mt2:$size-q8" --direction zh-en --output "$run_dir/text-$size-zh-en.json"
done

"$binary" seal-speech --model-folder models/whisper-coreml/openai_whisper-small.en --tokenizer-folder models/whisper-tokenizer-small.en --output "$run_dir/small-en-resources.json"
"$binary" seal-speech --model-folder models/whisper-coreml/openai_whisper-large-v3-v20240930_turbo --tokenizer-folder models/whisper-tokenizer --output "$run_dir/turbo-resources.json"

# No microphone access. Supply a recording path relative to the project, or an
# absolute path; otherwise use macOS's installed offline English voice.
audio_input="${1:-$run_dir/synthetic-english.aiff}"
if [ "$#" -eq 0 ]; then
    say -v Samantha -r 155 -f fixtures/translation-en.txt -o "$audio_input"
fi
"$binary" transcribe --input "$audio_input" --resources "$run_dir/small-en-resources.json" --output "$run_dir/small-en-asr.json"
"$binary" transcribe --input "$audio_input" --resources "$run_dir/turbo-resources.json" --output "$run_dir/turbo-asr.json"
"$binary" transcribe --input "$audio_input" --resources "$run_dir/turbo-resources.json" --model hy-mt2:1.8b-q8 --output "$run_dir/turbo-bilingual.json"
"$binary" replay --input "$audio_input" --resources "$run_dir/turbo-resources.json" --model hy-mt2:1.8b-q8 --output "$run_dir/turbo-replay.json"
printf '测试结果：%s/%s\n请人工核对译文与音频；命令成功不代表质量验收通过。\n' "$project_root" "$run_dir"
