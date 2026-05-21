#!/usr/bin/env python3
"""
ft5dsl — A GTK4/Adwaita DSL dictionary viewer.

Features
--------
* DSL / DSL.DZ parser  →  HTML rendering via WebKitGTK
* SQLite + WAL + mmap for disk-based index (no file kept in RAM)
* FTS5 BM25 ranking for headword and full-text search
* Incremental indexing (mtime-based, per-dictionary)
* Async indexing & searching (never blocks the UI thread)
* GtkColumnView results list
* Dictionary enable/disable, import, remove
* Search history & bookmarks (persisted in library.db)
* Dark-mode–aware CSS injected into WebKit
* Full-text search toggle in preferences
"""

from __future__ import annotations

import gzip
import mmap
import os
import queue
import re
import shutil
import sqlite3
import threading
import unicodedata
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Iterator

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango
from gi.repository import WebKit


# ─────────────────────────────────────────────────────────────────────────────
#  Paths
# ─────────────────────────────────────────────────────────────────────────────

APP_ID = "io.github.ft5dsl"
DATA_DIR = Path.home() / ".local" / "share" / "ft5dsl"
CACHE_DIR = Path.home() / ".cache" / "ft5dsl"
INDEX_DIR = CACHE_DIR / "indexes"
LIBRARY_DB = CACHE_DIR / "library.db"

MAX_OPEN_MMAPS = 64
MAX_OPEN_READER_CONNS = 24
SEARCH_DEBOUNCE_MS = 500

for _d in (DATA_DIR, CACHE_DIR, INDEX_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  DSL markup → HTML
# ─────────────────────────────────────────────────────────────────────────────

# Mapping of DSL inline tags to HTML
_INLINE = {
    "b": "strong",
    "i": "em",
    "u": "u",
    # NOTE: [s] is NOT strikethrough in DSL — it is the image tag, handled separately
    "sup": "sup",
    "sub": "sub",
}

# Transcription / phonetic block
_RX_TRNS = re.compile(r"\[t\](.*?)\[/t\]", re.S)
# Translation block
_RX_TRN = re.compile(r"\[trn\](.*?)\[/trn\]", re.S)
# Example block
_RX_EX = re.compile(r"\[ex\](.*?)\[/ex\]", re.S)
# Comment block
_RX_COM = re.compile(r"\[com\](.*?)\[/com\]", re.S)
# Colour tag  [c red]…[/c]  or  [c]…[/c]
_RX_C = re.compile(r"\[c(?:\s+([\w#]+))?\](.*?)\[/c\]", re.S)
# Reference / cross-link  [ref]word[/ref]  or  <<word>>
_RX_REF = re.compile(r"\[ref\](.*?)\[/ref\]|\<\<(.*?)\>\>", re.S)
# URL  [url]href[/url]  or  [url=href]text[/url]
_RX_URL = re.compile(r"\[url(?:=(.*?))?\](.*?)\[/url\]", re.S)
# Image  [s]filename[/s]
_RX_IMG = re.compile(r"\[s\](.*?)\[/s\]", re.S)
# Sound  [snd]filename[/snd]  (skip)
_RX_SND = re.compile(r"\[snd\](.*?)\[/snd\]", re.S)
# Margin / indent level  [m1]…[/m1]
_RX_MN = re.compile(r"\[m(\d?)\](.*?)\[/m\1\]", re.S)
# Generic inline open/close  (leftover unknown tags)
_RX_TAG = re.compile(r"\[/?\w[\w\d]*[^\]]*\]")


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def dsl_to_html(raw: str) -> str:
    """Convert DSL markup to an HTML fragment."""
    # Work line by line; blank lines → paragraph breaks
    lines = raw.split("\n")
    html_parts: list[str] = []

    for line in lines:
        line = _convert_dsl_line(line.lstrip())
        html_parts.append(line)

    return "<br>".join(html_parts)


def _convert_dsl_line(line: str) -> str:
    """Apply all DSL → HTML substitutions to a single line."""
    # Escape the line at the very beginning to be safe
    line = _esc(line)

    # Sound tags → remove entirely
    line = _RX_SND.sub("", line)

    # Transcription
    line = _RX_TRNS.sub(lambda m: f'<span class="trns">[{m.group(1)}]</span>', line)

    # Translation block
    line = _RX_TRN.sub(lambda m: f'<span class="trn">{_convert_inline_no_esc(m.group(1))}</span>', line)

    # Example block
    line = _RX_EX.sub(lambda m: f'<span class="ex">{_convert_inline_no_esc(m.group(1))}</span>', line)

    # Comment
    line = _RX_COM.sub(lambda m: f'<span class="com">{_convert_inline_no_esc(m.group(1))}</span>', line)

    # Colour
    def _colour(m: re.Match) -> str:
        colour = m.group(1) or "var(--accent)"
        inner = _convert_inline_no_esc(m.group(2))
        return f'<span style="color:{colour}">{inner}</span>'

    line = _RX_C.sub(_colour, line)

    # Reference / cross-link
    def _ref(m: re.Match) -> str:
        word = m.group(1) or m.group(2) or ""
        return f'<a class="ref" href="lookup:{word}">{word}</a>'

    line = _RX_REF.sub(_ref, line)

    # URL
    def _url(m: re.Match) -> str:
        href = m.group(1) or m.group(2)
        text = m.group(2)
        return f'<a href="{href}">{text}</a>'

    line = _RX_URL.sub(_url, line)

    # Image
    line = _RX_IMG.sub(lambda m: f'<img src="{m.group(1)}" alt="">', line)

    # Margin / indent
    def _margin(m: re.Match) -> str:
        level = int(m.group(1)) if m.group(1) else 1
        inner = _convert_inline_no_esc(m.group(2))
        return f'<div class="m{level}">{inner}</div>'

    line = _RX_MN.sub(_margin, line)

    # Convert any remaining inline tags in the line
    line = _convert_inline_no_esc(line)
    return line


def _convert_inline(text: str) -> str:
    """Convert simple inline DSL tags that map directly to HTML (with escaping)."""
    return _convert_inline_no_esc(_esc(text))


def _convert_inline_no_esc(text: str) -> str:
    """Convert simple inline DSL tags without escaping the input."""
    for dsl_tag, html_tag in _INLINE.items():
        text = re.sub(
            rf"\[{dsl_tag}\](.*?)\[/{dsl_tag}\]",
            lambda m, t=html_tag: f"<{t}>{m.group(1)}</{t}>",
            text,
            flags=re.S,
        )
    # Strip any remaining unknown DSL tags
    text = _RX_TAG.sub("", text)
    return text


# ─────────────────────────────────────────────────────────────────────────────
#  Mmap & Encoding Manager
# ─────────────────────────────────────────────────────────────────────────────

class MmapManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mmaps: OrderedDict[str, tuple[mmap.mmap, str]] = OrderedDict()

    def get_mmap(self, path: Path) -> tuple[mmap.mmap, str]:
        path_str = str(path.resolve())
        with self._lock:
            cached = self._mmaps.get(path_str)
            if cached is not None:
                self._mmaps.move_to_end(path_str)
                return cached

            if path_str not in self._mmaps:
                encoding = self.detect_encoding(path)
                with open(path, "rb") as f:
                    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                self._mmaps[path_str] = (mm, encoding)
                self._evict_if_needed()
            return self._mmaps[path_str]

    def _evict_if_needed(self) -> None:
        while len(self._mmaps) > MAX_OPEN_MMAPS:
            _, (mm, _) = self._mmaps.popitem(last=False)
            try:
                mm.close()
            except Exception:
                pass

    def close_all(self) -> None:
        with self._lock:
            for mm, _ in self._mmaps.values():
                try:
                    mm.close()
                except Exception:
                    pass
            self._mmaps.clear()

    def close_path(self, path: Path) -> None:
        path_str = str(path.resolve())
        with self._lock:
            if path_str in self._mmaps:
                mm, _ = self._mmaps.pop(path_str)
                try:
                    mm.close()
                except Exception:
                    pass

    @staticmethod
    def detect_encoding(path: Path) -> str:
        with open(path, "rb") as f:
            header = f.read(4)
        if header.startswith(b"\xff\xfe"):
            return "utf-16-le"
        if header.startswith(b"\xfe\xff"):
            return "utf-16-be"
        if header.startswith(b"\xef\xbb\xbf"):
            return "utf-8"
        if header.startswith(b"#\x00"):
            return "utf-16-le"
        if header.startswith(b"\x00#"):
            return "utf-16-be"
        try:
            with open(path, "rb") as f:
                content = f.read(4096)
            content.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            return "cp1251"


def ensure_uncompressed_dsl(path: Path) -> Path:
    if path.suffix != ".dz":
        return path

    name = path.name[:-3]
    if not name.endswith(".dsl"):
        name += ".dsl"

    in_place_path = path.with_name(name)
    if in_place_path.exists():
        if path.parent == DATA_DIR:
            try:
                path.unlink()
            except OSError:
                pass
        return in_place_path

    try:
        with gzip.open(path, "rb") as f_in:
            with open(in_place_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        if path.parent == DATA_DIR:
            try:
                path.unlink()
            except OSError:
                pass
        return in_place_path
    except OSError:
        dest_path = DATA_DIR / name
        if dest_path.exists():
            return dest_path
        with gzip.open(path, "rb") as f_in:
            with open(dest_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        if path.parent == DATA_DIR:
            try:
                path.unlink()
            except OSError:
                pass
        return dest_path


def iter_dsl_mmap(mm: mmap.mmap, encoding: str) -> Iterator[tuple[list[str], int, int]]:
    if encoding == "utf-16-le":
        delim = b"\n\x00"
        ws_prefixes = (b" \x00", b"\t\x00")
        comment_prefix = b"#\x00"
        r_strip = b"\r\x00"
        bom = b"\xff\xfe"
    elif encoding == "utf-16-be":
        delim = b"\x00\n"
        ws_prefixes = (b"\x00 ", b"\x00\t")
        comment_prefix = b"\x00#"
        r_strip = b"\x00\r"
        bom = b"\xfe\xff"
    else:
        delim = b"\n"
        ws_prefixes = (b" ", b"\t")
        comment_prefix = b"#"
        r_strip = b"\r"
        bom = b"\xef\xbb\xbf"

    size = len(mm)
    pos = 0
    delim_len = len(delim)
    
    if size >= len(bom) and mm[0:len(bom)] == bom:
        pos = len(bom)

    headwords = []
    article_start = -1
    last_article_end = -1
    in_header = True

    def decode_str(b: bytes) -> str:
        return b.decode(encoding, errors="replace").strip()

    while pos < size:
        next_nl = mm.find(delim, pos)
        if next_nl == -1:
            line_end = size
            next_pos = size
        else:
            line_end = next_nl
            next_pos = next_nl + delim_len

        line_bytes = mm[pos:line_end]
        
        if line_bytes.endswith(r_strip):
            line_bytes = line_bytes[:-len(r_strip)]

        is_empty = len(line_bytes) == 0

        if in_header:
            if line_bytes.startswith(comment_prefix):
                pos = next_pos
                continue
            if not is_empty:
                in_header = False

        if not in_header:
            is_space = any(line_bytes.startswith(p) for p in ws_prefixes)

            if is_space:
                if headwords:
                    if article_start == -1:
                        article_start = pos
                    last_article_end = next_pos
            elif is_empty:
                pass
            else:
                if headwords and article_start != -1:
                    yield headwords, article_start, last_article_end
                    headwords = []
                    article_start = -1
                    last_article_end = -1
                
                hw_str = decode_str(line_bytes)
                if hw_str:
                    headwords.append(hw_str)

        pos = next_pos

    if headwords and article_start != -1:
        yield headwords, article_start, last_article_end


# ─────────────────────────────────────────────────────────────────────────────
#  Database / index layer
# ─────────────────────────────────────────────────────────────────────────────

_DICT_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA page_size=8192;
VACUUM;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA mmap_size=536870912;
PRAGMA cache_size=-65536;

CREATE TABLE IF NOT EXISTS entries (
    id                  INTEGER PRIMARY KEY,
    headword            TEXT NOT NULL,
    normalized_headword TEXT NOT NULL,
    offset              INTEGER NOT NULL,
    length              INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_norm
    ON entries(normalized_headword COLLATE NOCASE);

CREATE VIRTUAL TABLE IF NOT EXISTS art_fts
    USING fts5(
        article,
        content='',
        tokenize='unicode61 remove_diacritics 2'
    );
"""

_LIB_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS dictionaries (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL UNIQUE,
    db_path         TEXT NOT NULL,
    mtime           INTEGER,
    entry_count     INTEGER DEFAULT 0,
    fulltext_enabled INTEGER DEFAULT 0,
    enabled         INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS history (
    id      INTEGER PRIMARY KEY,
    query   TEXT NOT NULL,
    ts      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS bookmarks (
    id       INTEGER PRIMARY KEY,
    headword TEXT NOT NULL,
    dict_id  INTEGER REFERENCES dictionaries(id) ON DELETE CASCADE,
    ts       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(headword, dict_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO settings VALUES ('fulltext_default', '0');
"""


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _open_reader(db_path: str) -> sqlite3.Connection:
    """Open a read-only WAL-compatible connection. Create one per thread."""
    conn = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA mmap_size=536870912")
    conn.execute("PRAGMA cache_size=-32768")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class FT5Database:
    """
    Thread-safe dictionary database.

    Library db: one writer connection protected by _lib_lock.
    Dict index files: per-thread connections via threading.local — WAL allows
    unlimited concurrent readers alongside the indexing writer, so no locking
    is needed between search threads.
    """

    def __init__(self) -> None:
        self._lib = sqlite3.connect(str(LIBRARY_DB), timeout=10.0, check_same_thread=False)
        self._lib.row_factory = sqlite3.Row
        self._lib.execute("PRAGMA busy_timeout=10000")
        self._lib.execute("PRAGMA journal_mode=WAL")
        self._lib.execute("PRAGMA synchronous=NORMAL")
        self._lib.executescript(_LIB_DDL)
        self._lib.commit()
        self._lib_lock = threading.Lock()
        self.mmap_manager = MmapManager()
        self._reader_local = threading.local()

    # ── library helpers ──────────────────────────────────────────────────────

    def get_dictionaries(self) -> list[sqlite3.Row]:
        with self._lib_lock:
            return self._lib.execute(
                "SELECT * FROM dictionaries ORDER BY name"
            ).fetchall()

    def set_enabled(self, dict_id: int, enabled: bool) -> None:
        with self._lib_lock:
            self._lib.execute(
                "UPDATE dictionaries SET enabled=? WHERE id=?",
                (int(enabled), dict_id),
            )
            self._lib.commit()

    def remove_dictionary(self, dict_id: int) -> None:
        with self._lib_lock:
            row = self._lib.execute(
                "SELECT path, db_path FROM dictionaries WHERE id=?", (dict_id,)
            ).fetchone()
            if row:
                path = row["path"]
                db_path = row["db_path"]
                self._lib.execute(
                    "DELETE FROM dictionaries WHERE id=?", (dict_id,)
                )
                self._lib.commit()
                self.mmap_manager.close_path(Path(path))
                self._close_reader_path(db_path)
                try:
                    Path(db_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lib_lock:
            row = self._lib.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lib_lock:
            self._lib.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value)
            )
            self._lib.commit()

    # ── history & bookmarks ──────────────────────────────────────────────────

    def add_history(self, query: str) -> None:
        with self._lib_lock:
            self._lib.execute(
                "INSERT INTO history(query) VALUES(?)", (query,)
            )
            # Keep last 500 entries
            self._lib.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT 500)"
            )
            self._lib.commit()

    def get_history(self, limit: int = 50) -> list[str]:
        with self._lib_lock:
            rows = self._lib.execute(
                "SELECT DISTINCT query FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [r["query"] for r in rows]

    def toggle_bookmark(self, headword: str, dict_id: int) -> bool:
        """Returns True if now bookmarked, False if removed."""
        with self._lib_lock:
            existing = self._lib.execute(
                "SELECT id FROM bookmarks WHERE headword=? AND dict_id=?",
                (headword, dict_id),
            ).fetchone()
            if existing:
                self._lib.execute(
                    "DELETE FROM bookmarks WHERE headword=? AND dict_id=?",
                    (headword, dict_id),
                )
                self._lib.commit()
                return False
            else:
                self._lib.execute(
                    "INSERT OR IGNORE INTO bookmarks(headword, dict_id) VALUES(?,?)",
                    (headword, dict_id),
                )
                self._lib.commit()
                return True

    def is_bookmarked(self, headword: str) -> bool:
        with self._lib_lock:
            return bool(
                self._lib.execute(
                    "SELECT 1 FROM bookmarks WHERE headword=?", (headword,)
                ).fetchone()
            )

    def get_bookmarks(self) -> list[sqlite3.Row]:
        with self._lib_lock:
            return self._lib.execute(
                "SELECT b.headword, d.name as dict_name "
                "FROM bookmarks b LEFT JOIN dictionaries d ON b.dict_id=d.id "
                "ORDER BY b.ts DESC"
            ).fetchall()

    # ── indexing ─────────────────────────────────────────────────────────────

    def needs_reindex(self, path: Path, fulltext: bool = False) -> bool:
        with self._lib_lock:
            row = self._lib.execute(
                "SELECT mtime, fulltext_enabled FROM dictionaries WHERE path=?",
                (str(path.resolve()),),
            ).fetchone()
        if not row:
            return True
        return (
            int(path.stat().st_mtime) != row["mtime"]
            or int(bool(fulltext)) != row["fulltext_enabled"]
        )

    def create_index(
        self,
        path: Path,
        fulltext: bool = False,
        progress_cb=None,
        cancel: threading.Event | None = None,
    ) -> int:
        db_path = INDEX_DIR / f"{path.stem}.db"

        conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout=30000")
        # Keep readers responsive while rebuilding an index in WAL mode.
        conn.execute("PRAGMA locking_mode=NORMAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=536870912")
        conn.execute("PRAGMA cache_size=-65536")
        conn.executescript(_DICT_DDL)
        # Remove legacy/larger auxiliary structures when rebuilding in compact mode.
        conn.execute("DROP INDEX IF EXISTS idx_norm_hw")
        conn.execute("DROP INDEX IF EXISTS idx_prefix")
        conn.execute("DROP TABLE IF EXISTS headword_prefix")
        conn.execute("DROP TABLE IF EXISTS hw_fts")

        conn.execute("DELETE FROM entries")
        conn.execute("INSERT INTO art_fts(art_fts) VALUES('delete-all')")

        encoding = MmapManager.detect_encoding(path)
        with open(path, "rb") as f:
            try:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            except ValueError:
                conn.close()
                return 0

            batch: list[tuple[str, str, int, int]] = []
            count = 0

            conn.execute("BEGIN")
            for headwords, article_start, article_end in iter_dsl_mmap(mm, encoding):
                if cancel and cancel.is_set():
                    conn.rollback()
                    mm.close()
                    conn.close()
                    return 0
                
                offset = article_start
                length = article_end - article_start
                for hw in headwords:
                    batch.append((hw, _normalize(hw), offset, length))
                
                if len(batch) >= 5000:
                    self._insert_batch(conn, batch, fulltext, mm, encoding)
                    count += len(batch)
                    batch.clear()
                    if progress_cb:
                        GLib.idle_add(progress_cb, count)

            if batch:
                self._insert_batch(conn, batch, fulltext, mm, encoding)
                count += len(batch)
                if progress_cb:
                    GLib.idle_add(progress_cb, count)

            conn.commit()
            conn.execute("PRAGMA optimize")
            mm.close()
            conn.close()

        with self._lib_lock:
            self._lib.execute(
                """
                INSERT OR REPLACE INTO dictionaries
                    (name, path, db_path, mtime, entry_count, fulltext_enabled, enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    path.stem,
                    str(path.resolve()),
                    str(db_path),
                    int(path.stat().st_mtime),
                    count,
                    int(fulltext),
                ),
            )
            self._lib.commit()

        return count

    @staticmethod
    def _insert_batch(
        conn: sqlite3.Connection,
        batch: list[tuple[str, str, int, int]],
        fulltext: bool,
        mm: mmap.mmap | None = None,
        encoding: str = "utf-8",
    ) -> None:
        cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM entries")
        start_id = cursor.fetchone()[0] + 1
        conn.executemany(
            "INSERT INTO entries(headword, normalized_headword, offset, length) VALUES(?,?,?,?)",
            batch,
        )
        if fulltext and mm is not None:
            fts_batch = []
            for i, (_, _, offset, length) in enumerate(batch):
                rowid = start_id + i
                article_bytes = mm[offset:offset+length]
                article_str = article_bytes.decode(encoding, errors="replace")
                fts_batch.append((rowid, article_str))
            conn.executemany(
                "INSERT INTO art_fts(rowid, article) VALUES(?,?)",
                fts_batch,
            )

    def close_connections(self):
        self.mmap_manager.close_all()
        self._close_reader_pool()
        try:
            self._lib.close()
        except Exception:
            pass

    def _reader_conn(self, db_path: str) -> sqlite3.Connection:
        if not hasattr(self._reader_local, "conns"):
            self._reader_local.conns = OrderedDict()
        conns: OrderedDict[str, sqlite3.Connection] = self._reader_local.conns
        conn = conns.get(db_path)
        if conn is None:
            conn = _open_reader(db_path)
            conns[db_path] = conn
        else:
            conns.move_to_end(db_path)
        while len(conns) > MAX_OPEN_READER_CONNS:
            _, old = conns.popitem(last=False)
            try:
                old.close()
            except Exception:
                pass
        return conn

    def _close_reader_pool(self):
        conns = getattr(self._reader_local, "conns", None)
        if not conns:
            return
        for conn in conns.values():
            try:
                conn.close()
            except Exception:
                pass
        conns.clear()

    def _close_reader_path(self, db_path: str):
        conns = getattr(self._reader_local, "conns", None)
        if not conns:
            return
        conn = conns.pop(db_path, None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    # ── search ────────────────────────────────────────────────────────────────

    def search_prefix(self, query: str, limit: int = 200) -> list[dict]:
        norm = _normalize(query)
        unique: dict[str, dict] = {}
        start = norm
        end = norm + "\uffff"

        for row in self.get_dictionaries():
            if not row["enabled"]:
                continue
            remaining = limit - len(unique)
            if remaining <= 0:
                break
            try:
                conn = self._reader_conn(row["db_path"])
                per_dict_limit = max(20, min(120, remaining * 2))
                cur = conn.execute(
                    """
                    SELECT DISTINCT headword
                    FROM entries
                    WHERE normalized_headword >= ? AND normalized_headword < ?
                    LIMIT ?
                    """,
                    (start, end, per_dict_limit),
                )
                for r in cur.fetchall():
                    rec = {
                        "headword": r[0],
                        "rank": 0.0,
                        "dict_name": row["name"],
                        "dict_id": row["id"],
                        "db_path": row["db_path"],
                    }
                    key = rec["headword"].casefold()
                    if key not in unique:
                        unique[key] = rec
            except sqlite3.Error as e:
                print(f"[search_prefix] {row['name']}: {e}")

        results = list(unique.values())
        results.sort(
            key=lambda x: (
                x["headword"].casefold() != query.casefold(),
                x["headword"],
            )
        )
        return results[:limit]

    def search_fulltext(self, query: str, limit: int = 100) -> list[dict]:
        norm = _normalize(query)
        unique: dict[str, dict] = {}

        for row in self.get_dictionaries():
            if not row["enabled"] or not row["fulltext_enabled"]:
                continue
            remaining = limit - len(unique)
            if remaining <= 0:
                break
            try:
                conn = self._reader_conn(row["db_path"])
                per_dict_limit = max(10, min(80, remaining * 2))
                cur = conn.execute(
                    """
                    SELECT e.headword, bm25(art_fts) AS rank
                    FROM art_fts
                    JOIN entries e ON art_fts.rowid = e.id
                    WHERE art_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (norm, per_dict_limit),
                )
                for r in cur.fetchall():
                    rec = {
                        "headword": r[0],
                        "rank": r[1],
                        "dict_name": row["name"],
                        "dict_id": row["id"],
                        "db_path": row["db_path"],
                    }
                    key = rec["headword"].casefold()
                    if key not in unique:
                        unique[key] = rec
            except sqlite3.Error as e:
                print(f"[search_fulltext] {row['name']}: {e}")

        results = list(unique.values())
        results.sort(key=lambda x: x["rank"])
        return results[:limit]

    def lookup_exact(self, headword: str, db_path: str | None = None) -> list[dict]:
        norm = _normalize(headword)
        results: list[dict] = []

        dicts = (
            [r for r in self.get_dictionaries() if r["db_path"] == db_path]
            if db_path
            else self.get_dictionaries()
        )

        for row in dicts:
            if not row["enabled"]:
                continue
            try:
                conn = self._reader_conn(row["db_path"])
                cur = conn.execute(
                    """
                    SELECT headword, offset, length FROM entries
                    WHERE normalized_headword = ?
                    ORDER BY id
                    """,
                    (norm,),
                )
                for r in cur.fetchall():
                    hword = r["headword"]
                    offset = r["offset"]
                    length = r["length"]
                    path = Path(row["path"])
                    try:
                        mm, enc = self.mmap_manager.get_mmap(path)
                        article_bytes = mm[offset:offset+length]
                        article_raw = article_bytes.decode(enc, errors="replace")
                        article_html = dsl_to_html(article_raw)
                        results.append({
                            "headword": hword,
                            "article": article_html,
                            "dict_name": row["name"],
                            "dict_id": row["id"],
                        })
                    except Exception as e:
                        print(f"[lookup_exact] Error reading mmap for {row['name']}: {e}")
                        continue
            except sqlite3.Error as e:
                print(f"[lookup_exact] {row['name']}: {e}")

        return results


# ─────────────────────────────────────────────────────────────────────────────
#  CSS for WebKit article view
# ─────────────────────────────────────────────────────────────────────────────

ARTICLE_CSS = """
:root {
    --bg:      #ffffff;
    --fg:      #1a1a1a;
    --accent:  #2563eb;
    --accent2: #7c3aed;
    --muted:   #6b7280;
    --border:  #e5e7eb;
    --ex-bg:   #f3f4f6;
    --trn-fg:  #065f46;
    --com-fg:  #92400e;
    --code-bg: #f9fafb;
    font-size: 15px;
}

@media (prefers-color-scheme: dark) {
    :root {
        --bg:      #1c1c1e;
        --fg:      #e5e5ea;
        --accent:  #60a5fa;
        --accent2: #a78bfa;
        --muted:   #9ca3af;
        --border:  #3a3a3c;
        --ex-bg:   #2c2c2e;
        --trn-fg:  #6ee7b7;
        --com-fg:  #fcd34d;
        --code-bg: #242426;
    }
}

* { box-sizing: border-box; }

html {
    background: var(--bg);
    color-scheme: light dark;
    min-height: 100%;
}

body {
    background: var(--bg);
    color: var(--fg);
    font-family: "Linux Libertine", "Georgia", "FreeSerif", serif;
    line-height: 1.7;
    margin: 0;
    padding: 1.2rem 1.4rem 2rem;
    min-height: 100vh;
    width: 100%;
    max-width: none;
}

.entry-card {
    margin: 0 0 1.4rem;
    padding: 1rem 1.1rem 1.1rem;
    border: 1px solid var(--border);
    border-radius: 14px;
    background:
        linear-gradient(180deg, color-mix(in srgb, var(--accent) 6%, transparent), transparent 3.2rem),
        var(--bg);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);
}

.dict-name {
    font-size: 1rem;
    font-weight: 700;
    margin: 0 0 0.35rem;
    text-align: left;
    letter-spacing: 0.01em;
}

.entry-headword {
    font-size: 1.5rem;
    font-weight: 700;
    margin: 0 0 0.5rem;
    color: var(--fg);
    line-height: 1.25;
}

.entry { margin-bottom: 0; }

strong { color: var(--fg); }
em { font-style: italic; }

.trns {
    font-family: "Charis SIL", "Gentium Plus", "Doulos SIL", monospace;
    color: var(--accent2);
    font-style: normal;
}

.trn { color: var(--trn-fg); }

.ex {
    display: block;
    background: var(--ex-bg);
    border-left: 3px solid var(--accent);
    padding: 0.25rem 0.7rem;
    margin: 0.3rem 0;
    border-radius: 0 4px 4px 0;
    font-style: italic;
    color: var(--muted);
}

.com {
    font-size: 0.88em;
    color: var(--com-fg);
}

a.ref {
    color: var(--accent);
    text-decoration: none;
    cursor: pointer;
    border-bottom: 1px dotted var(--accent);
}
a.ref:hover { border-bottom-style: solid; }

a { color: var(--accent); }

.m1 { margin-left: 0; }
.m2 { margin-left: 1.4rem; }
.m3 { margin-left: 2.8rem; }
.m4 { margin-left: 4.2rem; }

img { max-width: 100%; border-radius: 4px; margin: 0.2rem 0; }

hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 0.7rem 0 0.9rem;
}

@media (prefers-color-scheme: dark) {
    .entry-card {
        box-shadow: 0 16px 36px rgba(0, 0, 0, 0.22);
    }
}
"""


def build_article_html(results: list[dict]) -> str:
    if not results:
        return build_message_html("No results.")

    parts = [
        "<!DOCTYPE html><html><head>",
        "<meta charset='utf-8'>",
        "<meta name='color-scheme' content='light dark'>",
        f"<style>{ARTICLE_CSS}</style>",
        "</head><body>",
    ]

    for res in results:
        article = res["article"]
        # Strip dangerous tags
        article = re.sub(r"<style[^>]*>.*?</style>", "", article, flags=re.S)
        article = re.sub(r"<script[^>]*>.*?</script>", "", article, flags=re.S)
        parts.append(
            f"<section class='entry-card'>"
            f"<div class='dict-name'>{_esc(res['dict_name'])}</div>"
            f"<hr>"
            f"<h2 class='entry-headword'>{_esc(res['headword'])}</h2>"
            f"<div class='entry'>"
            f"<div class='body'>{article}</div>"
            f"</div>"
            f"</section>"
        )

    parts.append("</body></html>")
    return "".join(parts)


def build_message_html(message: str) -> str:
    return (
        "<!DOCTYPE html><html><head>"
        "<meta charset='utf-8'>"
        "<meta name='color-scheme' content='light dark'>"
        f"<style>{ARTICLE_CSS}</style>"
        f"</head><body><p style='color:var(--muted)'>{_esc(message)}</p></body></html>"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GObject model for results column view
# ─────────────────────────────────────────────────────────────────────────────

class ResultItem(GObject.Object):
    __gtype_name__ = "ResultItem"

    def __init__(self, headword: str, dict_name: str, dict_id: int, db_path: str):
        super().__init__()
        self.headword = headword
        self.dict_name = dict_name
        self.dict_id = dict_id
        self.db_path = db_path


# ─────────────────────────────────────────────────────────────────────────────
#  Preferences dialog
# ─────────────────────────────────────────────────────────────────────────────

class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, db: FT5Database, on_change=None):
        super().__init__()
        self.db = db
        self.on_change = on_change
        self.set_title("Preferences")

        # ── General page ────────────────────────────────────────────────────
        gen_page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.add(gen_page)

        search_group = Adw.PreferencesGroup(title="Search")
        gen_page.add(search_group)

        ft_row = Adw.SwitchRow(title="Full-text search by default",
                               subtitle="Index article bodies (slower indexing)")
        ft_row.set_active(db.get_setting("fulltext_default", "0") == "1")
        ft_row.connect("notify::active", self._on_fulltext_toggled)
        search_group.add(ft_row)

        # ── Dictionaries page ────────────────────────────────────────────────
        dict_page = Adw.PreferencesPage(title="Dictionaries", icon_name="accessories-dictionary-symbolic")
        self.add(dict_page)

        self.dict_group = Adw.PreferencesGroup(
            title="Installed dictionaries",
            description="Dictionaries stored in ~/.local/share/ft5dsl",
        )
        dict_page.add(self.dict_group)

        self._populate_dicts()

    def _populate_dicts(self):
        while row := self.dict_group.get_row(0):
            self.dict_group.remove(row)

        for row_data in self.db.get_dictionaries():
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(row_data["name"]),
                subtitle=GLib.markup_escape_text(
                    f"{row_data['entry_count']:,} entries  ·  {row_data['path']}"
                ),
            )

            sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            sw.set_active(bool(row_data["enabled"]))
            sw.connect(
                "notify::active",
                lambda s, _, rid=row_data["id"]: self.db.set_enabled(rid, s.get_active()),
            )
            row.add_suffix(sw)
            row.set_activatable_widget(sw)

            rm_btn = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                css_classes=["destructive-action", "flat"],
                tooltip_text="Remove from library",
            )
            rm_btn.connect(
                "clicked",
                lambda _, rid=row_data["id"]: self._remove(rid),
            )
            row.add_suffix(rm_btn)

            self.dict_group.add(row)

    def _on_fulltext_toggled(self, row, _):
        self.db.set_setting("fulltext_default", "1" if row.get_active() else "0")
        if self.on_change:
            self.on_change()

    def _remove(self, dict_id: int):
        self.db.remove_dictionary(dict_id)
        self._populate_dicts()
        if self.on_change:
            self.on_change()


# ─────────────────────────────────────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.set_title("ft5dsl")
        self.set_default_size(1040, 720)

        self.db = FT5Database()
        self._search_queue: queue.Queue[tuple[int, str, bool] | None] = queue.Queue()
        self._search_token = 0
        self._search_debounce_id: int | None = None
        self._index_cancel = threading.Event()
        self._index_thread: threading.Thread | None = None
        self._index_lock = threading.Lock()
        self._current_headword: str | None = None
        self._current_dict_id: int | None = None

        # ── lookup worker ────────────────────────────────────────────────────
        self._lookup_queue = queue.Queue()
        self._lookup_request_id = 0
        self._lookup_thread = threading.Thread(target=self._lookup_worker, daemon=True)
        self._lookup_thread.start()
        self._search_worker_thread = threading.Thread(target=self._search_worker, daemon=True)
        self._search_worker_thread.start()

        # ── model ────────────────────────────────────────────────────────────
        self.model = Gio.ListStore(item_type=ResultItem)
        self._selection = Gtk.SingleSelection(model=self.model)
        self._selection.set_autoselect(False)
        self._selection.set_can_unselect(True)

        # ── layout ───────────────────────────────────────────────────────────
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        # Search entry
        self.search_entry = Gtk.SearchEntry(
            placeholder_text="Search…",
            hexpand=True,
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", self._on_search_activate)
        header.set_title_widget(self.search_entry)

        # Header buttons
        import_btn = Gtk.Button(
            icon_name="document-open-symbolic",
            tooltip_text="Import dictionary file",
        )
        import_btn.connect("clicked", self._on_import)
        header.pack_start(import_btn)

        import_dir_btn = Gtk.Button(
            icon_name="folder-open-symbolic",
            tooltip_text="Add dictionary directory",
        )
        import_dir_btn.connect("clicked", self._on_import_dir)
        header.pack_start(import_dir_btn)

        prefs_btn = Gtk.Button(
            icon_name="preferences-system-symbolic",
            tooltip_text="Preferences",
        )
        prefs_btn.connect("clicked", self._on_prefs)
        header.pack_end(prefs_btn)

        bookmark_btn = Gtk.Button(
            icon_name="starred-symbolic",
            tooltip_text="Bookmarks",
        )
        bookmark_btn.connect("clicked", self._on_bookmarks)
        header.pack_end(bookmark_btn)

        history_btn = Gtk.Button(
            icon_name="document-open-recent-symbolic",
            tooltip_text="Search history",
        )
        history_btn.connect("clicked", self._on_history)
        header.pack_end(history_btn)

        # ── paned content ────────────────────────────────────────────────────
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        paned.set_position(300)
        toolbar.set_content(paned)

        # Left: column view
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.fulltext_toggle = Gtk.ToggleButton(
            icon_name="edit-find-symbolic",
            tooltip_text="Full-text search",
            css_classes=["flat"],
        )
        self.fulltext_toggle.set_active(
            self.db.get_setting("fulltext_default", "0") == "1"
        )
        self.fulltext_toggle.connect("toggled", lambda *_: self._trigger_search())

        toolbar2 = Adw.ToolbarView()
        sub_header = Adw.HeaderBar(show_back_button=False)
        sub_header.set_decoration_layout("")
        self.result_count_label = Gtk.Label(label="", css_classes=["dim-label"])
        sub_header.set_title_widget(self.result_count_label)
        sub_header.pack_end(self.fulltext_toggle)
        toolbar2.add_top_bar(sub_header)

        col_view = Gtk.ColumnView(model=self._selection)
        col_view.set_single_click_activate(True)
        col_view.set_show_column_separators(False)
        col_view.set_show_row_separators(True)
        col_view.add_css_class("data-table")
        col_view.connect("activate", self._on_result_activated)

        hw_col = Gtk.ColumnViewColumn(title="Headword")
        hw_factory = Gtk.SignalListItemFactory()
        hw_factory.connect("setup", self._hw_setup)
        hw_factory.connect("bind", self._hw_bind)
        hw_col.set_factory(hw_factory)
        hw_col.set_expand(True)
        col_view.append_column(hw_col)

        scroll_l = Gtk.ScrolledWindow(vexpand=True)
        scroll_l.set_child(col_view)
        toolbar2.set_content(scroll_l)
        left_box.append(toolbar2)
        paned.set_start_child(left_box)

        # Right: WebKit + toolbar
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        art_toolbar = Adw.ToolbarView()
        art_header = Adw.HeaderBar(show_back_button=False)
        art_header.set_decoration_layout("")

        self.bm_btn = Gtk.Button(
            icon_name="non-starred-symbolic",
            tooltip_text="Bookmark this word",
            css_classes=["flat"],
        )
        self.bm_btn.connect("clicked", self._on_bookmark_current)
        art_header.pack_end(self.bm_btn)

        self.headword_label = Gtk.Label(label="", css_classes=["heading"])
        art_header.set_title_widget(self.headword_label)
        art_toolbar.add_top_bar(art_header)

        ctx = WebKit.WebContext.get_default()
        ctx.set_cache_model(WebKit.CacheModel.DOCUMENT_VIEWER)

        self.webview = WebKit.WebView()
        self.webview.set_vexpand(True)
        nav_policy = self.webview.get_settings()
        nav_policy.set_enable_javascript(False)
        nav_policy.set_enable_page_cache(True)
        nav_policy.set_enable_back_forward_navigation_gestures(False)
        self.webview.connect("decide-policy", self._on_nav_policy)

        art_toolbar.set_content(self.webview)
        right_box.append(art_toolbar)

        paned.set_end_child(right_box)

        # Status / spinner overlay
        self.spinner = Gtk.Spinner()
        self.status_label = Gtk.Label(label="")
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_box.append(self.spinner)
        status_box.append(self.status_label)
        status_box.set_halign(Gtk.Align.CENTER)
        toolbar.add_bottom_bar(status_box)

        # Start background indexing
        self._index_async()

        # File monitor for auto-reindex on changes
        self._monitor = Gio.File.new_for_path(str(DATA_DIR)).monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        self._monitor.connect("changed", self._on_dict_dir_changed)

    # ── Column view factories ────────────────────────────────────────────────

    @staticmethod
    def _hw_setup(factory, item):
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        label.set_max_width_chars(30)
        item.set_child(label)

    @staticmethod
    def _hw_bind(factory, item):
        obj: ResultItem = item.get_item()
        item.get_child().set_text(obj.headword)

    # ── Indexing ─────────────────────────────────────────────────────────────

    def _index_async(self):
        with self._index_lock:
            # Cancel any currently running index pass before starting a new one.
            self._index_cancel.set()
            self._index_cancel = threading.Event()
            cancel = self._index_cancel

        def _run():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            files = [
                p for p in DATA_DIR.iterdir()
                if p.suffix in (".dsl", ".dz")
            ]
            fulltext = self.db.get_setting("fulltext_default", "0") == "1"

            dsl_files = []
            for p in files:
                try:
                    dsl_path = ensure_uncompressed_dsl(p)
                    dsl_files.append(dsl_path)
                except Exception as exc:
                    print(f"Error preparing {p.name}: {exc}")

            try:
                registered = self.db.get_dictionaries()
                for r in registered:
                    p = Path(r["path"])
                    if p.exists() and p not in dsl_files:
                        dsl_files.append(p)
            except Exception as e:
                print(f"Error fetching registered dictionaries: {e}")

            for path in dsl_files:
                if cancel.is_set():
                    break
                if not self.db.needs_reindex(path, fulltext=fulltext):
                    continue
                GLib.idle_add(self._set_status, f"Indexing {path.name}…", True)
                try:
                    count = self.db.create_index(path, fulltext=fulltext,
                                                  cancel=cancel)
                    GLib.idle_add(
                        self._set_status,
                        f"Indexed {path.name} ({count:,} entries)",
                        False,
                    )
                except Exception as exc:
                    if not cancel.is_set():
                        GLib.idle_add(self._set_status, f"Error: {exc}", False)

            if not cancel.is_set():
                GLib.idle_add(self._set_status, "", False)

        t = threading.Thread(target=_run, daemon=True)
        with self._index_lock:
            self._index_thread = t
        t.start()

    def _set_status(self, msg: str, spinning: bool):
        self.status_label.set_text(msg)
        if spinning:
            self.spinner.start()
        else:
            self.spinner.stop()

    def _on_dict_dir_changed(self, monitor, file, other_file, event_type):
        if event_type in (Gio.FileMonitorEvent.CHANGED,
                          Gio.FileMonitorEvent.CREATED,
                          Gio.FileMonitorEvent.DELETED):
            self._index_async()

    # ── Search ───────────────────────────────────────────────────────────────

    def _on_search_changed(self, entry):
        self._schedule_search()

    def _on_search_activate(self, entry):
        query = entry.get_text().strip()
        if query:
            self.db.add_history(query)
        self._schedule_search(immediate=True)

    def _schedule_search(self, immediate: bool = False):
        if self._search_debounce_id is not None:
            GLib.source_remove(self._search_debounce_id)
            self._search_debounce_id = None
        if immediate:
            self._trigger_search()
            return

        self._search_debounce_id = GLib.timeout_add(
            SEARCH_DEBOUNCE_MS, self._debounced_search_cb
        )

    def _debounced_search_cb(self):
        self._search_debounce_id = None
        self._trigger_search()
        return False

    def _trigger_search(self):
        query = self.search_entry.get_text().strip()
        self._search_token += 1
        token = self._search_token

        GLib.idle_add(self.model.remove_all)
        GLib.idle_add(self.result_count_label.set_text, "")

        if not query:
            self.headword_label.set_text("")
            self._show_html(build_message_html("Type a word to search."))
            return

        fulltext = self.fulltext_toggle.get_active()
        self._search_queue.put((token, query, fulltext))

    def _search_worker(self):
        while True:
            item = self._search_queue.get()
            if item is None:
                self.db._close_reader_pool()
                self._search_queue.task_done()
                return

            token, query, fulltext = item
            while True:
                try:
                    nxt = self._search_queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self.db._close_reader_pool()
                    self._search_queue.task_done()
                    self._search_queue.task_done()
                    return
                token, query, fulltext = nxt
                self._search_queue.task_done()

            if fulltext:
                results = self.db.search_fulltext(query)
            else:
                results = self.db.search_prefix(query)

            GLib.idle_add(self._apply_search_results, token, query, results)
            self._search_queue.task_done()

    def _apply_search_results(self, token: int, query: str, results: list[dict]):
        if token != self._search_token:
            return
        if query != self.search_entry.get_text().strip():
            return
        self._populate_results(results)

    def _populate_results(self, results: list[dict]):
        self.model.remove_all()
        for r in results:
            self.model.append(
                ResultItem(r["headword"], r["dict_name"], r["dict_id"], r["db_path"])
            )
        n = len(results)
        self.result_count_label.set_text(f"{n} result{'s' if n != 1 else ''}")
        query = self.search_entry.get_text().strip()
        exact_idx = next(
            (
                i for i, r in enumerate(results)
                if r["headword"].casefold() == query.casefold()
            ),
            None,
        )
        self.headword_label.set_text("")
        if n == 0:
            self._show_html(build_message_html("No matching headwords."))
        elif exact_idx is not None:
            self._select_and_load(exact_idx)
        else:
            self._show_html(build_message_html("Select a headword to load entries."))

    # ── Article display ──────────────────────────────────────────────────────

    def _on_result_activated(self, view, position: int):
        self._select_and_load(position)

    def _select_and_load(self, position: int):
        item: ResultItem | None = self.model.get_item(position)
        if item is None:
            return
        self._selection.set_selected(position)
        self._load_article(item.headword, item.dict_id, item.db_path)

    def _load_article(self, headword: str, dict_id: int, db_path: str | None = None):
        self._current_headword = headword
        self._current_dict_id = dict_id
        self._lookup_request_id += 1
        request_id = self._lookup_request_id
        self.headword_label.set_text(headword)

        starred = self.db.is_bookmarked(headword)
        self.bm_btn.set_icon_name(
            "starred-symbolic" if starred else "non-starred-symbolic"
        )

        while not self._lookup_queue.empty():
            try:
                self._lookup_queue.get_nowait()
            except queue.Empty:
                break
        self._lookup_queue.put((headword, db_path, request_id))

    def _lookup_worker(self):
        while True:
            headword, preferred_db_path, request_id = self._lookup_queue.get()
            try:
                while not self._lookup_queue.empty():
                    headword, preferred_db_path, request_id = self._lookup_queue.get()

                if preferred_db_path:
                    fast_results = self.db.lookup_exact(headword, preferred_db_path)
                    if self._lookup_request_id == request_id:
                        GLib.idle_add(self._show_html, build_article_html(fast_results))

                results = self.db.lookup_exact(headword, None)
                if self._lookup_request_id == request_id:
                    GLib.idle_add(self._show_html, build_article_html(results))
            except Exception as e:
                print(f"[lookup_worker] Error: {e}")
            finally:
                self._lookup_queue.task_done()

    def _show_html(self, html: str):
        base_uri = GLib.filename_to_uri(str(DATA_DIR), None)
        self.webview.load_html(html, base_uri)

    def _on_nav_policy(self, wv, decision, decision_type):
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            req = action.get_request()
            uri = req.get_uri()
            if uri.startswith("lookup:"):
                word = uri[7:]
                self.search_entry.set_text(word)
                self._trigger_search()
                decision.ignore()
                return True
        return False

    def _on_preferences_changed(self):
        wants_fulltext = self.db.get_setting("fulltext_default", "0") == "1"
        if self.fulltext_toggle.get_active() != wants_fulltext:
            self.fulltext_toggle.set_active(wants_fulltext)
        else:
            self._schedule_search(immediate=True)
        self._index_async()

    # ── Bookmark ─────────────────────────────────────────────────────────────

    def _on_bookmark_current(self, btn):
        if not self._current_headword:
            return
        added = self.db.toggle_bookmark(
            self._current_headword, self._current_dict_id or 0
        )
        btn.set_icon_name("starred-symbolic" if added else "non-starred-symbolic")

    # ── Import ───────────────────────────────────────────────────────────────

    def _on_import(self, btn):
        dialog = Gtk.FileDialog(title="Import DSL dictionary")
        f = Gtk.FileFilter()
        f.set_name("DSL dictionaries (*.dsl, *.dsl.dz)")
        f.add_pattern("*.dsl")
        f.add_pattern("*.dz")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(f)
        dialog.set_filters(filters)

        dialog.open(self, None, self._on_import_done)

    def _on_import_done(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return

        src = Path(gfile.get_path())
        dest = DATA_DIR / src.name
        if src != dest:
            import shutil
            shutil.copy2(src, dest)

        fulltext = self.db.get_setting("fulltext_default", "0") == "1"

        def _run():
            GLib.idle_add(self._set_status, f"Preparing {dest.name}…", True)
            try:
                dsl_path = ensure_uncompressed_dsl(dest)
                count = self.db.create_index(dsl_path, fulltext=fulltext)
                GLib.idle_add(
                    self._set_status,
                    f"Done — {count:,} entries",
                    False,
                )
                GLib.idle_add(self._trigger_search)
            except Exception as exc:
                GLib.idle_add(self._set_status, f"Error: {exc}", False)

        threading.Thread(target=_run, daemon=True).start()

    def _on_import_dir(self, btn):
        dialog = Gtk.FileDialog(title="Add dictionary directory")
        dialog.select_folder(self, None, self._on_import_dir_done)

    def _on_import_dir_done(self, dialog, result):
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return

        src_dir = Path(gfile.get_path())
        fulltext = self.db.get_setting("fulltext_default", "0") == "1"

        def _run():
            GLib.idle_add(self._set_status, "Scanning directory...", True)
            files = []
            for root, _, filenames in os.walk(src_dir):
                for f in filenames:
                    p = Path(root) / f
                    if p.suffix in (".dsl", ".dz"):
                        if ".zip" in p.name or ".part" in p.name:
                            continue
                        files.append(p)

            if not files:
                GLib.idle_add(self._set_status, "No dictionary files found.", False)
                return

            total = len(files)
            for idx, p in enumerate(files, 1):
                if self._index_cancel.is_set():
                    break
                GLib.idle_add(self._set_status, f"Preparing {p.name} ({idx}/{total})…", True)
                try:
                    dsl_path = ensure_uncompressed_dsl(p)
                    if self.db.needs_reindex(dsl_path, fulltext=fulltext):
                        GLib.idle_add(self._set_status, f"Indexing {dsl_path.name} ({idx}/{total})…", True)
                        count = self.db.create_index(dsl_path, fulltext=fulltext, cancel=self._index_cancel)
                        GLib.idle_add(self._set_status, f"Indexed {dsl_path.name} ({count:,} entries)", False)
                except Exception as exc:
                    print(f"Error importing {p.name}: {exc}")
                    GLib.idle_add(self._set_status, f"Error on {p.name}: {exc}", False)

            GLib.idle_add(self._set_status, "Finished importing directory", False)
            GLib.idle_add(self._trigger_search)

        threading.Thread(target=_run, daemon=True).start()

    # ── History popup ─────────────────────────────────────────────────────────

    def _on_history(self, btn):
        history = self.db.get_history()
        self._show_word_list_popover(btn, "Recent searches", history)

    def _on_bookmarks(self, btn):
        bms = [r["headword"] for r in self.db.get_bookmarks()]
        self._show_word_list_popover(btn, "Bookmarks", bms)

    def _show_word_list_popover(self, relative_to, title: str, words: list[str]):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_size_request(220, -1)

        label = Gtk.Label(label=f"<b>{_esc(title)}</b>", use_markup=True,
                          css_classes=["heading"], xalign=0)
        label.set_margin_start(8)
        label.set_margin_top(8)
        label.set_margin_bottom(4)
        box.append(label)

        if not words:
            box.append(Gtk.Label(label="(empty)", css_classes=["dim-label"],
                                 margin_start=8, margin_bottom=8))
        else:
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            lb.add_css_class("boxed-list")
            for w in words[:50]:
                row = Adw.ActionRow(title=w, activatable=True)
                row.connect(
                    "activated",
                    lambda _, word=w: (
                        self.search_entry.set_text(word),
                        self._trigger_search(),
                        pop.popdown(),
                    ),
                )
                lb.append(row)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_max_content_height(400)
            scroll.set_propagate_natural_height(True)
            scroll.set_child(lb)
            box.append(scroll)

        pop.set_child(box)
        pop.set_parent(relative_to)
        pop.popup()

    # ── Preferences ──────────────────────────────────────────────────────────

    def _on_prefs(self, btn):
        dlg = PreferencesDialog(self.db, on_change=self._on_preferences_changed)
        dlg.present(self)


# ─────────────────────────────────────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────────────────────────────────────

class FT5Application(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = MainWindow(self)
        win.present()

    def do_shutdown(self):
        # Close all resources (mmap files and SQLite connections) before shutting down.
        try:
            win = self.props.active_window
            if win is not None and hasattr(win, "db"):
                if hasattr(win, "_search_queue"):
                    win._search_queue.put(None)
                win.db.close_connections()
        except Exception:
            pass
        Gio.Application.do_shutdown(self)


if __name__ == "__main__":
    import sys
    Adw.init()
    app = FT5Application()
    try:
        sys.exit(app.run(sys.argv))
    except KeyboardInterrupt:
        app.quit()
        sys.exit(130)
