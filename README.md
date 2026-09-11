# VAAPI Converter

A desktop video converter for Linux that encodes on the GPU through VAAPI.

HandBrake is excellent and covers CPU encoding thoroughly, but its VAAPI support
has never materialised. This fills that gap and nothing else: every video encode
here runs on the GPU, and there is no software encoder path at all.

![screenshot](docs/screenshot.png)

## What it does

- **Hardware encoding only** — H.264, HEVC, AV1 and VP9 through VAAPI, with the
  codec list filtered to what your GPU actually reports it can encode.
- **Full GPU pipeline** — decodes on the GPU too by default, so frames never
  touch system memory. Falls back to CPU decoding on its own when the GPU can't
  handle a source format.
- **10-bit** — on by default for AV1 (anything that encodes AV1 handles 10-bit),
  available for HEVC and VP9. Falls back to 8-bit if the driver refuses.
- **Constant quality or target bitrate** — QP, or VBR with automatic headroom.
- **Visual cropping** — grab a frame, drag a box, or let `cropdetect` find the
  black bars for you.
- **Keeps every track** — all audio languages and subtitles are preserved, not
  just the first of each, which is ffmpeg's default behaviour.
- **Surround downmix** — stereo, Pro Logic II, dialogue-boosted stereo, or mono.
- **Batch queue** with per-file and overall progress parsed from ffmpeg itself.

## Requirements

- Linux with a VAAPI-capable GPU and a render node at `/dev/dri`
- Python 3.8+ with tkinter (`python3-tk` on Debian/Ubuntu, `python3-tkinter` on
  Fedora, `tk` on Arch)
- `ffmpeg` and `ffprobe` built with VAAPI support

Check your GPU's capabilities with `vainfo`, or just run the app — it reports
which VAAPI encoders your ffmpeg build has and refuses to start if there are
none.

## Running it

Run directly:

```
python3 vaapi_converter.py
```

Or install it for the current user — adds a launcher entry, registers the app
for video files, and installs nothing outside `$HOME`:

```
./install.sh
```

`./install.sh --uninstall` reverses it.

## Notes and limitations

- **Cropping forces CPU decoding.** The crop filter works on frames in system
  memory, and VAAPI has no equivalent that operates on GPU surfaces. Encoding
  still happens on the GPU.
- **Settings apply to every track.** One audio codec, one quality, one downmix
  for all audio streams in a file. Per-track control is out of scope.
- **MP3 holds two channels at most**, so a 5.1 source needs a downmix. The app
  warns about this before you start.
- **Image-based subtitles** (PGS, VobSub) can't be converted to text formats, so
  they only survive in MKV.
- **AV1 on Arc is low-power-only** by driver design; there's no setting for it
  because there's no choice to make.

## Licence

MIT — see [LICENSE](LICENSE).
