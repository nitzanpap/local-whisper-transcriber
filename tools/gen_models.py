#!/usr/bin/env python3
"""Rebuild models.json from what Hugging Face actually says today.

    python3 tools/gen_models.py

The catalogue is generated rather than hand-written because two of its fields have
to be exactly right or they are worse than absent: `size_bytes`, which is how a
download shows progress and how the disk is checked beforehand, and `sha256`,
which is the only way to tell a finished download from an interrupted one. This
refuses to write a catalogue where either is missing.

The repository revision is pinned into every URL. Without that, the file behind a
`main` URL can change under a hash we recorded last month, and the app would then
reject a download that is perfectly good.

What each entry says to a person is written here by hand, on purpose. Nobody
should have to know what a ggml quantisation is to choose how good their
transcript will be.
"""

import json
import sys
import urllib.request
from pathlib import Path

REPO = "ggerganov/whisper.cpp"
OUT = Path(__file__).resolve().parent.parent / "models.json"

# Rank is the order they are offered in. It is deliberately not size order: the
# one most people should take is first, and the largest is last because it is the
# slowest and only sometimes better.
WANTED = [
    ("ggml-large-v3-turbo.bin", {
        "name": "Large v3 Turbo",
        "description": "Accurate, and several times faster than Large v3. Start here.",
        "speed": 85, "accuracy": 88, "languages": "many", "recommended": True,
    }),
    ("ggml-small.bin", {
        "name": "Small",
        "description": "Quick, and good enough for a clear recording.",
        "speed": 93, "accuracy": 70, "languages": "many", "recommended": True,
    }),
    ("ggml-medium.bin", {
        "name": "Medium",
        "description": "Between Small and Large, in both senses.",
        "speed": 75, "accuracy": 80, "languages": "many", "recommended": False,
    }),
    ("ggml-large-v3.bin", {
        "name": "Large v3",
        "description": "The most accurate, and the slowest by some way.",
        "speed": 40, "accuracy": 92, "languages": "many", "recommended": False,
    }),
    ("ggml-large-v3-turbo-q5_0.bin", {
        "name": "Large v3 Turbo, compressed",
        "description": "Nearly Turbo, at a third of the disk and memory.",
        "speed": 87, "accuracy": 85, "languages": "many", "recommended": False,
    }),
    ("ggml-small.en.bin", {
        "name": "Small, English only",
        "description": "Quick, and better than Small if you only ever record English.",
        "speed": 93, "accuracy": 74, "languages": "en", "recommended": False,
    }),
    ("ggml-base.en.bin", {
        "name": "Base, English only",
        "description": "Fast and rough, tuned for English alone.",
        "speed": 97, "accuracy": 60, "languages": "en", "recommended": False,
    }),
    ("ggml-base.bin", {
        "name": "Base",
        "description": "Fast and rough. For a quick idea of what is on a recording.",
        "speed": 97, "accuracy": 55, "languages": "many", "recommended": False,
    }),
    ("ggml-tiny.bin", {
        "name": "Tiny",
        "description": "The fastest, and it will get names and numbers wrong.",
        "speed": 99, "accuracy": 40, "languages": "many", "recommended": False,
    }),
]


def main() -> int:
    url = f"https://huggingface.co/api/models/{REPO}?blobs=true"
    with urllib.request.urlopen(url, timeout=60) as answer:
        info = json.load(answer)
    revision = info.get("sha", "")
    if not revision:
        print("the repository did not say which revision this is", file=sys.stderr)
        return 1
    sizes = {f["rfilename"]: (f.get("size"), (f.get("lfs") or {}).get("sha256", ""))
             for f in info.get("siblings", [])}

    models = []
    for filename, said in WANTED:
        size, digest = sizes.get(filename, (None, ""))
        # Refused rather than written half-right. A catalogue entry with no hash
        # cannot tell a finished download from a truncated one, and one with no
        # size cannot show progress or check the disk first.
        if not size or not digest:
            print(f"{filename}: no {'size' if not size else 'sha256'} from "
                  f"{REPO}; refusing to write the catalogue", file=sys.stderr)
            return 1
        models.append({
            "id": filename.removeprefix("ggml-").removesuffix(".bin"),
            "filename": filename,
            "url": f"https://huggingface.co/{REPO}/resolve/{revision}/{filename}",
            "size_bytes": size,
            "sha256": digest,
            **said,
        })

    OUT.write_text(json.dumps({
        "catalog_version": 1,
        "source": REPO,
        "revision": revision,
        "models": models,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} — {len(models)} models, revision {revision[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
