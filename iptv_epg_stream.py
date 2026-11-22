#!/usr/bin/env python3
"""Memory-bounded XMLTV merger for large private channel guides.

The output and deduplication database live in temporary files on disk. It
never holds the entire source feed, programme catalogue or output in RAM.
"""
from __future__ import annotations

import gzip
import os
import sqlite3
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from iptv_multi import channels_from_text

MAX_COMPRESSED = 150 * 1024 * 1024
MAX_UNCOMPRESSED = 1200 * 1024 * 1024


class LimitedReader:
    def __init__(self, reader, limit: int):
        self.reader = reader
        self.limit = limit
        self.size = 0

    def read(self, size=-1):
        data = self.reader.read(size)
        self.size += len(data)
        if self.size > self.limit:
            raise ValueError("EPG input exceeds permitted size")
        return data


class PrefixReader:
    def __init__(self, prefix: bytes, reader):
        self.prefix = prefix
        self.reader = reader

    def read(self, size=-1):
        if size < 0:
            part, self.prefix = self.prefix, b""
            return part + self.reader.read()
        part, self.prefix = self.prefix[:size], self.prefix[size:]
        return part + self.reader.read(size - len(part))


@contextmanager
def open_epg(source: str):
    if source.startswith(("http://", "https://")):
        handle = urllib.request.urlopen(
            urllib.request.Request(source, headers={"User-Agent": "Tvtest-EPG/1.0"}),
            timeout=45,
        )
    else:
        handle = open(source, "rb")
    with handle:
        compressed = LimitedReader(handle, MAX_COMPRESSED)
        prefix = compressed.read(2)
        replay = PrefixReader(prefix, compressed)
        if prefix == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=replay, mode="rb") as guide:
                yield LimitedReader(guide, MAX_UNCOMPRESSED)
        else:
            yield LimitedReader(replay, MAX_UNCOMPRESSED)


def normalize(name: str) -> str:
    import re
    import unicodedata

    name = unicodedata.normalize("NFKC", name).casefold()
    name = name.replace("ё", "е").replace("й", "и")
    name = re.sub(r"\b(?:uhd|fhd|hd|sd|4k|8k|hevc|h265|h264|backup|резерв)\b", " ", name)
    return re.sub(r"[^\w]+", "", name)


def build_epg(playlist: str, sources: list[str], output: Path,
              aliases: dict[str, str] | None = None) -> dict:
    """Write an atomic XMLTV/XMLTV.gz guide; preserve old file on failure."""
    channels = channels_from_text(playlist)
    aliases = aliases or {}
    by_name = defaultdict(list)
    by_alias = defaultdict(list)
    for channel in channels:
        by_name[normalize(channel.name)].append(channel)
        if aliases.get(channel.name):
            by_alias[aliases[channel.name]].append(channel)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    matched: set[str] = set()
    programmes = 0
    scanned = 0

    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".epg-build-") as folder:
        temp = Path(folder)
        db = sqlite3.connect(temp / "dedup.sqlite")
        try:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("PRAGMA cache_size=-4096")
            db.execute("CREATE TABLE seen (channel TEXT NOT NULL, start TEXT NOT NULL, "
                       "PRIMARY KEY (channel, start)) WITHOUT ROWID")
            with (temp / "epg.tmp").open("wb") as raw:
                if output.name.endswith(".gz"):
                    writer = gzip.GzipFile(filename="", fileobj=raw, mode="wb",
                                           compresslevel=5, mtime=0)
                else:
                    writer = raw
                try:
                    writer.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
                    writer.write(b'<tv generator-info-name="Tvtest EPG merger">\n')
                    # Declaring channels without programmes is valid XMLTV.
                    # Keep this as a streaming operation before listings arrive.
                    for channel in channels:
                        node = ET.Element("channel", {"id": channel.id})
                        ET.SubElement(node, "display-name").text = channel.name
                        writer.write(ET.tostring(node, encoding="utf-8") + b"\n")

                    for index, source in enumerate(sources, start=1):
                        print(f"Streaming EPG {index}/{len(sources)}...", flush=True)
                        source_ids = dict(by_alias)
                        root = None
                        with open_epg(source) as stream:
                            for event, item in ET.iterparse(stream, events=("start", "end")):
                                if root is None and event == "start":
                                    root = item
                                    if root.tag != "tv":
                                        raise ValueError("Not an XMLTV guide")
                                if event != "end":
                                    continue
                                if item.tag == "channel":
                                    source_id = item.get("id", "")
                                    if source_id not in source_ids:
                                        for name in [source_id] + [
                                            el.text or "" for el in item.findall("display-name")
                                        ]:
                                            candidates = by_name.get(normalize(name))
                                            if candidates:
                                                source_ids[source_id] = candidates
                                                break
                                    item.clear()
                                    root.clear()
                                elif item.tag == "programme":
                                    scanned += 1
                                    source_id = item.get("channel", "")
                                    if source_id not in source_ids:
                                        # Some feeds omit a <channel> declaration.
                                        source_ids[source_id] = by_name.get(
                                            normalize(source_id), []
                                        )
                                    candidates = source_ids[source_id]
                                    start = item.get("start")
                                    if start and item.findtext("title") and candidates:
                                        for channel in candidates:
                                            row = db.execute(
                                                "INSERT OR IGNORE INTO seen(channel,start) "
                                                "VALUES(?,?)", (channel.id, start)
                                            )
                                            if row.rowcount:
                                                item.set("channel", channel.id)
                                                writer.write(ET.tostring(item, encoding="utf-8"))
                                                writer.write(b"\n")
                                                programmes += 1
                                                matched.add(channel.id)
                                    item.clear()
                                    root.clear()
                                    if scanned % 25000 == 0:
                                        db.commit()
                                        print(f"Scanned {scanned:,} listings; "
                                              f"matched {programmes:,}", flush=True)
                        db.commit()
                    writer.write(b"</tv>\n")
                finally:
                    if writer is not raw:
                        writer.close()
                raw.flush()
                os.fsync(raw.fileno())
            if not programmes:
                raise ValueError("No matching XMLTV programmes; old guide preserved")
            os.replace(temp / "epg.tmp", output)
        finally:
            db.close()

    return {
        "channels_total": len(channels),
        "channels_with_epg": len(matched),
        "programmes": programmes,
        "scanned_programmes": scanned,
    }
