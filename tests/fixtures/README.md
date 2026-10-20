# Test fixtures

Every Takeout used by the tests is **generated**, never real. The generator lives in
`src/photomigrate/testing/` rather than here, so it gets the same strict typing and 90%
coverage gate as production code. Real samples (`data-in/`) are not fixtures: the media
guard blocks them, and they are never committed.

## Using the standard Takeout

```python
def test_something(standard_takeout_zips):  # session fixture in tests/conftest.py
    manifest = standard_takeout_zips  # 3 zip parts in a tmp dir, read-only
    for item in manifest.with_quirk("edited"):
        original_sidecar = manifest.metadata_sidecar(item)
```

`TakeoutManifest` is the ground truth. Each media file is an `Item` with its path, sidecar
path, true capture time (`taken`), upload time (`created`), the capture time embedded in the
file (`embedded_taken`), location, description, favourite and people. It also records
relations (`copy_of`, `edit_of`, `live_partner`, `near_duplicate_of`, `google_creation`) and
quirk tags. `manifest.parts` says which zip each file is in. `manifest.to_json()` dumps it all.

## Building a custom Takeout

```python
from photomigrate.testing.takeout import TakeoutBuilder

b = TakeoutBuilder(seed=1)
photo = b.photo("Photos from 2019", "IMG_1234.JPG", taken=when, favorited=True)
b.copy_to(photo, "Holiday")  # the same item in an album folder
b.edited(photo, lang="fr")  # IMG_1234-modifié.JPG, no sidecar of its own
manifest = b.write_zips(tmp_path, parts=2)  # or b.write_tree(tmp_path)
```

Output is deterministic per seed, byte for byte (zips included). Videos need `ffmpeg` on
PATH.

## Quirks in the standard Takeout

| Quirk tag | Example in the standard Takeout |
|---|---|
| `supplemental-sidecar` / `legacy-sidecar` | `IMG_1001.JPG.supplemental-metadata.json` / `IMG_0500.jpg.json` |
| `truncated-name`, `truncated-sidecar` | 47-char media name, 51-char sidecar (`….jp.json`); the full name is in the sidecar `title` |
| `duplicate-index` | `IMG_1004(1).JPG` ↔ `IMG_1004.JPG.supplemental-metadata(1).json`, `IMG_0500(1).jpg` ↔ `IMG_0500.jpg(1).json` |
| `edited`, `edited-<lang>`, `uses-original-sidecar` | `IMG_1001-edited.JPG`, `IMG_0500-modifié.jpg`, `IMG_0500-bearbeitet(1).jpg`, `IMG_1004-edited(1).JPG` |
| `edited-extension-case` | `IMG_1005-edited.jpg` next to `IMG_1005.JPG` |
| `truncated-edit-suffix` | `…ABCD-ed.jpg` (the `-edited` suffix cut by the 47-char limit) |
| `live-photo`, `live-photo-motion` | `IMG_2001.HEIC` + `IMG_2001.MOV` (motion without sidecar), `IMG_2002.JPG` + `IMG_2002.MP4` (legacy sidecars on both) |
| `missing-sidecar` | `IMG_1006.JPG`, `IMG_1011.MOV` |
| `placeholder-date` | sidecar timestamp `0` (1970), EXIF 1980-01-01 and 2000-01-01 |
| `future-date` | `Photos from 2037/IMG_1008.JPG` |
| `upload-date-as-taken` | `IMG_20120304_101112.jpg`: no EXIF, sidecar date = upload time, real date only in the name |
| `no-exif`, `no-exif-date` | screenshots, the messaging forward, Google creations |
| `album-copy` | `IMG_1001.JPG` in `Été 2019`, `Best of` and `Family`, byte-identical with its own sidecar copies |
| `near-duplicate-resized` / `-recompressed` / `-heic-vs-jpeg` | `IMG-20190702-WA0003.jpg` (half size, EXIF stripped), `received_1001.jpeg` (quality 25), `Exports/IMG_1003.jpg` (JPEG of a HEIC) |
| `google-creation-motion` / `-mix` / `-smile` | `IMG_1001-MOTION.gif`, `IMG_1001-MIX.jpg`, `IMG_1003-SMILE.jpg` (+ `-SMILE-edited.jpg`), `composition.type: AUTO` |
| `favourite`, `people`, `unicode-description`, `geo-in-exif`, `no-geo` | `IMG_1001.JPG` (Nice, Alice and Bob, "Plage à Nice 🏖️"), `IMG_1003.HEIC` ("東京タワー") |

It also contains:
- 3 zip parts, with media and sidecars dealt into different parts and `archive_browser.html`
  in part 1;
- album `metadata.json` files, with a description and shared-album comments;
- `en_GB` and `en_US` formatted dates, and desktop, mobile and auto upload origins;
- a Google Maps file outside `Google Photos/` that ingest must ignore.

Size: about 250 KB for 40 media items and 76 files; it generates in about 0.3 s.

The naming rules seen in the real sample, and the ones modelled on public reports, are
listed in [docs/TAKEOUT_FORMAT.md](../../docs/TAKEOUT_FORMAT.md).
