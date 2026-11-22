#!/usr/bin/env python3
"""Merge existing XMLTV guides for private M3U channels, without exposing source URLs."""
from __future__ import annotations

import argparse
import copy
import gzip
import json
import os
import re
import tempfile
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from iptv_multi import channels_from_text

MAX_SOURCE_BYTES = 150 * 1024 * 1024
ALIASES = {"ё": "е", "й": "и"}
QUALITY = re.compile(r"\b(?:uhd|fhd|hd|sd|4k|8k|hevc|h265|h264|backup|резерв)\b", re.I)


def normalize(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).casefold()
    for old, new in ALIASES.items():
        name = name.replace(old, new)
    name = QUALITY.sub(" ", name)
    return re.sub(r"[^\w]+", "", name)


def read_source(source: str) -> bytes:
    if source.startswith(("https://", "http://")):
        with urllib.request.urlopen(urllib.request.Request(
            source, headers={"User-Agent": "Tvtest-EPG/1.0"}
        ), timeout=45) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
    else:
        with open(source, "rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("EPG source too large")
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    if len(data) > MAX_SOURCE_BYTES * 8:
        raise ValueError("Uncompressed EPG source too large")
    return data


def merge(playlist: str, sources: list[bytes], aliases: dict[str, str] | None = None):
    channels = channels_from_text(playlist)
    aliases = aliases or {}
    # The same channel name can appear multiple times (HD/SD, provider groups).
    by_name = defaultdict(list)
    for channel in channels:
        by_name[normalize(channel.name)].append(channel)
    output = ET.Element("tv", {"generator-info-name": "Tvtest EPG merger"})
    programmes = defaultdict(list)
    matched = set()
    seen_programmes = set()
    source_errors = 0

    # Resolve explicit aliases once, never once per programme. On large EPG
    # feeds (hundreds of thousands of events) the previous O(events*channels)
    # lookup could appear to hang.
    by_alias_id = defaultdict(list)
    for channel in channels:
        mapped_id = aliases.get(channel.name)
        if mapped_id:
            by_alias_id[mapped_id].append(channel)

    for raw in sources:
        try:
            root = ET.fromstring(raw)
            if root.tag != "tv":
                raise ValueError("Not XMLTV")
        except (ET.ParseError, ValueError):
            source_errors += 1
            continue
        source_matches = dict(by_alias_id)
        for node in root.findall("channel"):
            source_id = node.get("id", "")
            if source_id in source_matches:
                continue
            names = [source_id] + [
                n.text or "" for n in node.findall("display-name")
            ]
            for name in names:
                candidates = by_name.get(normalize(name))
                if candidates:
                    source_matches[source_id] = candidates
                    break
        for programme in root.findall("programme"):
            source_id = programme.get("channel", "")
            if source_id not in source_matches:
                # XMLTV allows a programme to reference an unlisted channel.
                source_matches[source_id] = by_name.get(normalize(source_id), [])
            candidates = source_matches[source_id]
            if not candidates:
                continue
            if not programme.get("start") or not programme.findtext("title"):
                continue
            for channel in candidates:
                key = (channel.id, programme.get("start"))
                if key in seen_programmes:
                    continue
                seen_programmes.add(key)
                clone = copy.deepcopy(programme)
                clone.set("channel", channel.id)
                programmes[channel.id].append(clone)
                matched.add(channel.id)

    for channel in channels:
        if channel.id not in matched:
            continue
        element = ET.SubElement(output, "channel", {"id": channel.id})
        ET.SubElement(element, "display-name").text = channel.name
    count = 0
    for channel in channels:
        for item in sorted(programmes[channel.id], key=lambda p: p.get("start", "")):
            output.append(item)
            count += 1
    return ET.tostring(output, encoding="utf-8", xml_declaration=True), {
        "channels_total": len(channels),
        "channels_with_epg": len(matched),
        "programmes": count,
        "invalid_sources": source_errors,
    }


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=".epg-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist", type=Path, required=True)
    parser.add_argument("--source", action="append", required=True,
                        help="XMLTV URL or file; repeat for multiple guides, first wins on duplicates")
    parser.add_argument("--aliases", type=Path, help='JSON: {"Our channel name": "XMLTV channel id"}')
    parser.add_argument("--output", type=Path, default=Path("epg.xml.gz"))
    args = parser.parse_args()
    aliases = json.loads(args.aliases.read_text(encoding="utf-8")) if args.aliases else {}
    raw_sources = []
    failures = 0
    for source in args.source:
        try:
            raw_sources.append(read_source(source))
        except (OSError, ValueError, EOFError, gzip.BadGzipFile) as err:
            failures += 1
            print(f"Skipping unavailable EPG source: {type(err).__name__}")
    if not raw_sources:
        parser.error("No readable EPG sources; existing output left intact")
    xml, report = merge(args.playlist.read_text(encoding="utf-8-sig"), raw_sources, aliases)
    report["unavailable_sources"] = failures
    if not report["programmes"]:
        parser.error("No matching EPG programmes; existing output left intact")
    data = gzip.compress(xml, mtime=0) if args.output.name.endswith(".gz") else xml
    atomic_write(args.output, data)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
