#!/bin/bash
# Download the Google Takeout parts one at a time onto the external drive.
#
# Each part is downloaded to a scratch folder on the internal disk (with a progress bar,
# resuming if the connection drops), copied to the drive, checked there by SHA-256, and only
# then removed from the scratch folder. Then the next part starts, so the internal disk only
# ever holds one part. A part already on the drive with its checksum file is skipped, so the
# script can simply be run again after any interruption.
#
# Usage (on the Air, in tmux):
#   scripts/download-takeout.sh --urls ~/takeout-urls.txt --cookies ~/takeout-cookies.txt \
#       --dest /Volumes/Photos/takeout [--scratch ~/takeout-scratch]
#
# --urls     a text file with the download links, one per line, in order. On
#            takeout.google.com > Manage exports, right-click each part's Download button >
#            Copy link. Blank lines and lines starting with # are ignored.
# --cookies  your signed-in Google session, which the links need: either a cookies.txt file
#            exported with a browser extension (Netscape format), or a file holding the
#            "cookie:" request header copied from the browser's developer tools. Keep it out of
#            the repo, and remove it when the downloads are done: it lets anyone who has it act
#            as you on Google until you sign out.
# --dest     the folder on the external drive.
# --scratch  the folder on the internal disk for the part being downloaded
#            (default ~/takeout-scratch).
#
# Works with macOS's bash 3.2.

set -euo pipefail

usage() {
    sed -n '10,24p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

die() {
    echo "Error: $*" >&2
    exit 1
}

URLS="" COOKIES="" DEST="" SCRATCH="$HOME/takeout-scratch"
while [ $# -gt 0 ]; do
    case "$1" in
        --urls) URLS="${2:-}"; shift 2 ;;
        --cookies) COOKIES="${2:-}"; shift 2 ;;
        --dest) DEST="${2:-}"; shift 2 ;;
        --scratch) SCRATCH="${2:-}"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done
[ -n "$URLS" ] && [ -n "$COOKIES" ] && [ -n "$DEST" ] || usage
[ -f "$URLS" ] || die "no links file at $URLS"
[ -f "$COOKIES" ] || die "no cookies file at $COOKIES"
[ -d "$DEST" ] || die "$DEST does not exist (is the drive attached?)"
command -v curl > /dev/null || die "curl is needed"
mkdir -p "$SCRATCH"

# A browser extension's cookies.txt has tab-separated lines; a copied header is one string.
if grep -q "$(printf '\t')" "$COOKIES"; then
    COOKIE_ARG="$COOKIES"
else
    COOKIE_ARG="$(sed -E 's/^[Cc]ookie:[[:space:]]*//' "$COOKIES" | tr -d '\r\n')"
fi

sha256() {
    if command -v shasum > /dev/null; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        sha256sum "$1" | cut -d' ' -f1
    fi
}

free_kb() {
    df -Pk "$1" | awk 'NR == 2 { print $4 }'
}

size_of() {
    wc -c < "$1" | tr -d ' '
}

human() {
    awk -v b="$1" 'BEGIN { split("B KB MB GB TB PB", u); i = 1
        while (b >= 1024 && i < 6) { b /= 1024; i++ }; printf "%.1f %s", b, u[i] }'
}

has_room() {  # folder, bytes: the part plus 1 GB to spare
    [ "$(free_kb "$1")" -gt $(( $2 / 1024 + 1048576 )) ]
}

LINKS=()
while IFS= read -r line || [ -n "$line" ]; do
    line="$(echo "$line" | tr -d '\r' | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
    case "$line" in ""|\#*) continue ;; esac
    LINKS+=("$line")
done < "$URLS"
TOTAL=${#LINKS[@]}
[ "$TOTAL" -gt 0 ] || die "no links in $URLS"

n=0
for url in "${LINKS[@]}"; do
    n=$((n + 1))
    # Ask for the first byte only: the headers give the part's name and size.
    headers="$(curl -sS -L -b "$COOKIE_ARG" -r 0-0 -D - -o /dev/null "$url")" \
        || die "part $n: the link did not answer (expired? copy it again from Takeout)"
    headers="$(echo "$headers" | tr -d '\r')"
    name="$( (echo "$headers" | grep -i '^content-disposition:' || true) | tail -1 \
        | sed -E 's/.*filename\*?=("|UTF-8'"''"')?([^";]+)"?.*/\2/')"
    case "$name" in
        *.zip) ;;
        *) die "part $n: Google sent a web page, not a zip. Sign in to Google again in the
browser (and confirm your password if it asks), export fresh cookies, and run this again." ;;
    esac
    size="$( (echo "$headers" | grep -i '^content-range:' || true) | tail -1 \
        | sed -E 's#.*/([0-9]+)$#\1#')"
    [ -n "$size" ] || size="$( (echo "$headers" | grep -i '^content-length:' || true) | tail -1 \
        | awk '{ print $2 }')"
    [ -n "$size" ] || die "part $n: Google did not say how big the part is; run this again"
    label="[$n/$TOTAL] $name ($(human "$size"))"

    if [ -f "$DEST/$name" ] && [ -f "$DEST/$name.sha256" ] && [ "$(size_of "$DEST/$name")" = "$size" ]; then
        echo "$label: already on the drive, skipped"
        continue
    fi
    has_room "$SCRATCH" "$size" || die "$label: not enough free space in $SCRATCH"
    has_room "$DEST" "$size" || die "$label: not enough free space in $DEST"

    echo "$label: downloading"
    partial="$SCRATCH/$name.part"
    tries=0
    while [ ! -f "$partial" ] || [ "$(size_of "$partial")" != "$size" ]; do
        tries=$((tries + 1))
        [ "$tries" -le 5 ] || die "$label: the download keeps failing; run this again later"
        curl -L --fail -b "$COOKIE_ARG" -C - --progress-bar -o "$partial" "$url" \
            || echo "  the connection dropped; resuming ($tries of 5)"
    done
    [ "$(head -c 2 "$partial")" = "PK" ] || die "$label: what arrived is not a zip"
    mv "$partial" "$SCRATCH/$name"
    sum="$(sha256 "$SCRATCH/$name")"

    echo "$label: copying to $DEST"
    if command -v rsync > /dev/null; then
        rsync --progress "$SCRATCH/$name" "$DEST/$name.copying"
    else
        cp "$SCRATCH/$name" "$DEST/$name.copying"
    fi
    [ "$(sha256 "$DEST/$name.copying")" = "$sum" ] \
        || die "$label: the copy on the drive does not match; the download stays in $SCRATCH"
    mv "$DEST/$name.copying" "$DEST/$name"
    echo "$sum  $name" > "$DEST/$name.sha256"
    # The verified copy is on the drive: the scratch copy can go.
    rm "$SCRATCH/$name"
    echo "$label: on the drive, checked (sha256 $sum)"
done

echo "All $TOTAL parts are on $DEST."
