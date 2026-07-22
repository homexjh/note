import sys
from faster_whisper import WhisperModel

audio_path = sys.argv[1]
model_size = sys.argv[2] if len(sys.argv) > 2 else "base"

print("Loading model:", model_size, flush=True)
model = WhisperModel(model_size, device="cpu", compute_type="int8")

print("Transcribing...", flush=True)
segments, info = model.transcribe(
    audio_path,
    language="zh",
    beam_size=5,
)

print(f"Detected language: {info.language} (prob={info.language_probability:.2f})", flush=True)
print("=== TRANSCRIPT ===", flush=True)

full = []
for seg in segments:
    line = f"[{seg.start:.1f}s -> {seg.end:.1f}s] {seg.text}"
    print(line, flush=True)
    full.append(line)

out = audio_path + ".transcript.txt"
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(full))
print(f"\nSaved to: {out}", flush=True)
