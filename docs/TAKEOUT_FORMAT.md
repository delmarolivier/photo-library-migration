# Google Takeout format notes

Observed in a real Google Photos Takeout from October 2026. The sample stays local in
`data-in/` and is never committed. This file records structure only: no names, dates,
places or people. The M1-02 fixture generator and the M1-04 ingest follow these rules.

## Archive layout

- An export is split into zips named `takeout-<UTC timestamp>[-<export>]-<part>.zip`. A part
  can hold nothing but `Takeout/archive_browser.html`.
- If other Google products were selected, they sit next to Photos (for example
  `Takeout/Maps/`). Ingest reads only `Takeout/Google Photos/`.
- `Takeout/Google Photos/<folder>/<files>`: exactly one folder level. Folders are albums or
  `Photos from YYYY`.
- An album folder holds `metadata.json` with `title` and `sharedAlbumComments[]`
  (`contentOwnerName`, `creationTime{timestamp, formatted}`, `liked`).

## Sidecars

- Current naming: `<media name>.supplemental-metadata.json`, for example
  `IMG_1234.JPG.supplemental-metadata.json`. Older exports use `<media name>.json`, with long
  names truncated. Neither appears in this sample, but both must still be supported.
- Duplicate names: media `IMG_1234(1).JPG` pairs with sidecar
  `IMG_1234.JPG.supplemental-metadata(1).json`. The `(N)` moves to just before `.json`.
- Edited copies have no sidecar of their own: `IMG_1234-edited.JPG` uses the original's.
  The copy's extension case can differ from the original's (`-edited.jpg` next to `.JPG`),
  and `(N)` can follow `-edited` (`IMG_1234-edited(1).jpg`). So match extensions
  case-insensitively.
- Some videos have no sidecar at all.

Fields:

| Field | Shape |
|---|---|
| `title` | media file name |
| `description` | string, often empty |
| `imageViews` | number as a string |
| `url` | string |
| `photoTakenTime`, `creationTime` | `{timestamp: epoch seconds as a string, formatted}`. `formatted` depends on the account locale (`20 Sept 2024, 10:11:12 UTC`), so never parse it. `creationTime` is the upload time. |
| `geoData` | `{latitude, longitude, altitude, latitudeSpan, longitudeSpan}`, all `0.0` when unknown |
| `geoDataExif` | same shape, present only on some items |
| `people` | `[{name}]`, face-group names, on most items |
| `googlePhotosOrigin` | `{photosDesktopUploader: {}}` or `{composition: {type: "AUTO"}}` |
| `favorited` | absent in this sample (presumably only written when true) |

## Google-made creations

Files named `-MOTION.gif` (animations), `-MIX.jpg` and `-SMILE.jpg` (and their `-edited`
copies), with `googlePhotosOrigin.composition.type == "AUTO"`. They are not camera originals.

## File names

- iPhone style: `IMG_####.JPG`, `IMG_####_#.jpg`, `IMG_####.MOV`, `IMG_####.mp4`. Extension case
  is mixed.
- `-edited` copies are very common, so edited-version handling (M2-04) is a main path.
- No Live Photo still and motion pairs here: the desktop uploader does not send motion parts.

## Not in this sample, but fixtures still need it

Truncated names, legacy `.json` sidecars, `Photos from YYYY` folders, Live Photo pairs,
placeholder dates (1970, 2000-01-01, future dates), HEIC, favourites, non-empty descriptions,
and names in other scripts (for example accented or CJK names).

## Maps export

The Maps part (saved places, contributions, commute settings) has no Timeline or location
history. Timeline now lives on the phone, so location backfill (M3-05) needs the Timeline
export from the Google Maps app.
