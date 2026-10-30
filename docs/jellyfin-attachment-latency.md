# Jellyfin font attachments — why playback start waits 5-8 seconds

Jellyfin serves the first play of an MKV with many embedded fonts slowly. The player fetches
every font before it renders ASS subtitles, and the last fetch returns 5 to 8 seconds after the
first. Issue [#6657](https://github.com/DanielH2018/server/issues/6657) records the symptom
from M6 A4's 168h edge soak. Those bursts are the three `traefik_latency` DOWN episodes
[#6649](https://github.com/DanielH2018/server/issues/6649) investigated. The latency check is
right to report them, so jellyfin stays outside `TRAEFIK_STREAM_SERVICES`.

## The cause, from jellyfin's own log

The burst at 2026-10-30 14:43Z is the first play of *The Quintessential Quintuplets Movie*. The
pod logs in `America/Chicago` (the Deployment's `TZ`), so it appears at 09:43 local. Between
09:43:39 and 09:43:47, `AttachmentExtractor` ran 49 separate `ffmpeg -dump_attachment:<n>
"<font>"` processes, one per font, at about six per second. A second burst at 09:49:46 to
09:49:54 ran 30 more for the first play of a TV episode.

The runs are serial, not parallel. Jellyfin 10.11.11's
`MediaBrowser.MediaEncoding/Attachments/AttachmentExtractor.cs` serves
`GET /Videos/<id>/<id>/Attachments/<n>` through `ExtractAttachment`, which takes a keyed lock
on the media source's attachment folder before it checks for the file. The client's parallel
requests queue on that one lock. Each request waits for every font queued before it, which is
why the slowest requests in a burst take 5 to 8 seconds.

Only the first play of a file pays this cost. `ExtractAttachment` writes each font to
`/config/data/data/attachments/<prefix>/<media source id>/` on the `jellyfin-config` volume,
and a later request finds the file and skips `ffmpeg`.

## Why no server setting fixes it

Neither Jellyfin 10.11.11 nor its `master` branch (checked 2026-10-30) has a setting that
extracts attachments ahead of playback. `LibraryOptions` offers scan-time extraction for chapter
images and `trickplay` images only. `EncodingOptions` has `EnableSubtitleExtraction`, which is on
by default and governs subtitles, not attachments. No scheduled task extracts attachments.

Jellyfin does have a bulk path. `ExtractAllAttachments` runs one `ffmpeg -dump_attachment:t ""`
for every font in the file, finishes in about one second, and writes into the same folder
`ExtractAttachment` checks. Only `TranscodeManager` calls it, when it re-encodes the video to burn subtitles
in. The log shows that case at 13:29 and 14:46 local on 2026-10-30, each with one
bulk run and no per-font runs. The attachment endpoint never takes the bulk path, in 10.11.11 or
on `master`.

A fix therefore has to make one bulk extraction happen per file before or at its first play.
[#6662](https://github.com/DanielH2018/server/issues/6662) lists the options.
