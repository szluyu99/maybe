# autodub — 视频自动中文配音原型

`视频/链接 → 抽音轨 → ASR → 翻译 → TTS → 变速对齐 + 混音 → mp4 + srt`

```bash
# 1. 系统依赖
apt install -y ffmpeg          # 必需, 含 ffprobe
pip install -r requirements.txt

# 2. 翻译用的 LLM (OpenAI 兼容接口)
export LLM_API_BASE=https://api.openai.com/v1
export LLM_API_KEY=sk-xxx
export LLM_MODEL=gpt-4o-mini
export WHISPER_MODEL=small     # 可选: tiny/base/small/medium/large-v3

# 3. 跑
python3 autodub.py input.mp4 -o out/dubbed.mp4 --burn-sub
python3 autodub.py 'https://...' --voice zh-CN-YunxiNeural

# 不装依赖先看编排逻辑
python3 autodub.py demo.mp4 --stub --dry-run
```

可调项: `--voice` 音色、`--bg-vol` 原声保留音量（默认 0.15）、`--lang` 源语言、
`--burn-sub` 烧字幕（会重编码视频，不加则 `-c:v copy` 秒出）。

配音时长对齐靠 `atempo`，倍速被夹在 0.7~1.6 之间；中文比英文短，多数片段会被拉慢。
如果原视频语速快、字幕密，需要在翻译阶段压字数，而不是继续拉倍速。

`edge-tts`、Whisper 模型下载、`yt-dlp` 和 LLM API 都要访问外网，
如果所在网络需要代理，自行 `export http_proxy` / `https_proxy`。
