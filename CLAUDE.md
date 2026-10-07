# Project context

A Tkinter front-end for hardware video encoding via VAAPI. Single file,
`vaapi_converter.py`, roughly 1400 lines, standard library only.

## Scope

This is deliberately **not** a general video converter. HandBrake already covers
CPU encoding well; this exists because HandBrake has no VAAPI support. Every
video encode goes through the GPU. There is no software encoder path, and adding
one would defeat the purpose.

Stream copying ("Keep as-is") is kept, because remuxing isn't encoding and
shouldn't need the GPU. Audio always encodes on the CPU — no GPU exposes audio
encoders.

## Design decisions worth preserving

**Codec lists are filtered to what the GPU reports.** On startup a background
thread runs `ffmpeg -encoders` and collects everything matching `*_vaapi`. The
dropdown only offers those. If nothing is found, the app says which piece is
missing (no VAAPI encoders vs no render node) and refuses to start rather than
queuing a job that will die.

**Failures step down rather than abort.** `_run_one` builds a ladder of
attempts: as configured, then CPU decoding, then 8-bit. Each rung logs why.
Identical commands are skipped. This exists because GPU decode support is
narrower than GPU encode support, and because 10-bit is refused by some drivers.

**Pixel formats are normalised explicitly.** The GPU path uses
`scale_vaapi=format=nv12` (or `p010` for 10-bit) because 10-bit sources decode
to P010 surfaces that an 8-bit encoder rejects. The CPU-decode path uses
`format=…,hwupload`. This was a real bug before it was a precaution.

**Cropping forces CPU decoding.** The crop filter can't touch GPU surfaces.
`build_command` handles this; the GPU-decode checkbox greys out when a crop is
set.

**One setting applies to all tracks.** Audio codec, quality, and downmix apply
to every audio stream. Settings that need per-track judgement (AC-3 dynamic
range was considered and rejected) don't belong here — the app can't act on the
nuance. Either add real per-track UI or leave it alone.

**Streams are mapped explicitly.** `-map 0:V:0 -map 0:a? -map 0:s?` plus `0:t?`
for MKV attachments. Without this ffmpeg silently keeps one stream per type and
drops the other language tracks. `0:V` (capital) excludes attached cover art.

**Hints explain mechanisms, not just labels.** Each setting has a grey line
under it saying what actually happens — what a quality tier resolves to per
codec, why a container rejects certain subtitles. Keep this up for new settings;
it's most of what makes the app usable without documentation.

## Testing

There's no display in most agent environments, so Tk can't be instantiated. The
approach that works: stub `tkinter` with a fake module exposing `StringVar`-like
classes and a catch-all widget, put it on `PYTHONPATH`, then call
`build_command` and the hint methods with a fake `self` carrying the right
attributes. This catches the majority of real bugs, since nearly all the logic
lives in command construction.

Always verify command shapes across the matrix: each container × each codec ×
GPU/CPU decode × 8/10-bit × quality/bitrate × each downmix mode.

## Conventions

- Standard library only. No pip dependencies, ever — the point is that it runs
  anywhere ffmpeg does. The one exception is optional: drag-and-drop onto the
  window uses the `tkdnd` Tcl extension (a distro package) when it's present,
  and the app works unchanged without it.
- Comments explain *why*, particularly around ffmpeg's non-obvious behaviour.
- Desktop scaling: `detect_scale` / `apply_scaling` exist because Tk assumes
  96 DPI. ttk indicator sizes need `indicatorsize` set explicitly; fonts follow
  `tk scaling`. Anything sized in raw pixels (crop preview) multiplies by
  `self.scale`.

## Possible next steps

- Split into modules if it keeps growing (command building, UI, crop dialog).
- Per-track audio settings, if the single-setting limit becomes a real problem.
- A screenshot for the README (`docs/screenshot.png`).
