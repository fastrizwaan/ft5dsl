This app is essentially a **hybrid dictionary engine + desktop browser UI** optimized for very large `.dsl` dictionaries.

The architecture is built around four major layers:

1. **Dictionary storage layer** (`.dsl/.dsl.dz`)
2. **SQLite/FTS5 indexing layer**
3. **Async search + lookup pipeline**
4. **GTK4/WebKit rendering UI**

The entire system is designed so that:

* dictionary files stay on disk
* RAM usage stays low
* searches remain instant
* rendering is modern HTML/CSS
* UI never blocks

The uploaded source file is here: 

---

# 1. High-Level Architecture

```text
┌────────────────────────────────────┐
│            GTK4 UI                │
│ SearchEntry / ColumnView / WebKit │
└────────────────────────────────────┘
                 │
                 ▼
┌────────────────────────────────────┐
│        Async Worker Threads        │
│ search_worker / lookup_worker      │
└────────────────────────────────────┘
                 │
                 ▼
┌────────────────────────────────────┐
│        FT5Database Layer           │
│ SQLite + FTS5 + WAL + mmap         │
└────────────────────────────────────┘
                 │
                 ▼
┌────────────────────────────────────┐
│       DSL Dictionary Files         │
│        .dsl / .dsl.dz              │
└────────────────────────────────────┘
```

---

# 2. Storage Layout

The app uses two separate storage locations:

```text
~/.local/share/ft5dsl/
```

Contains:

* `.dsl`
* `.dsl.dz`

These are the actual dictionaries.

Defined here: 

---

```text
~/.cache/ft5dsl/
```

Contains:

* SQLite indexes
* search cache
* bookmarks
* history

Defined here: 

---

# 3. Dictionary Processing Pipeline

When a dictionary is imported:

```text
.dsl.dz
   ↓
decompress if needed
   ↓
memory-map file
   ↓
parse DSL structure
   ↓
extract:
    - headwords
    - article offsets
    - article lengths
   ↓
store in SQLite index
```

---

# 4. Why mmap Is Used

This is one of the most important architectural decisions.

Instead of loading entire dictionaries into RAM:

```python
mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
```



The app memory-maps the dictionary file.

That means:

* OS loads pages on demand
* near-zero startup memory
* instant random access
* works well with gigantic dictionaries

---

# 5. Offset-Based Lookup System

This is extremely efficient.

The app does NOT store full articles in SQLite.

Instead it stores:

```sql
offset
length
```

Schema: 

Each dictionary entry points to a byte region inside the original `.dsl`.

So lookup becomes:

```text
find headword in SQLite
    ↓
get offset + length
    ↓
slice mmap region
    ↓
decode only that article
```

Code: 

This avoids:

* duplicating dictionary content
* massive SQLite files
* RAM explosion

---

# 6. Parsing Architecture

The parser is streaming-based.

Core parser:

```python
iter_dsl_mmap()
```



It scans line-by-line directly from mmap.

The parser identifies:

* headword lines
* article blocks
* article byte ranges

without loading the whole dictionary.

---

# 7. Incremental Indexing

The app tracks:

```sql
mtime
fulltext_enabled
```

inside `library.db`.

Schema: 

Before indexing:

```python
needs_reindex()
```

checks:

* file modification time
* fulltext mode

So unchanged dictionaries are skipped.

Very important for huge collections.

---

# 8. SQLite Architecture

There are actually TWO SQLite systems.

---

## A. Global Library Database

```text
library.db
```

Contains:

* installed dictionaries
* settings
* history
* bookmarks

---

## B. Per-Dictionary Index Databases

Each dictionary gets:

```text
~/.cache/ft5dsl/indexes/DICTNAME.db
```

These contain:

* headword index
* FTS5 index

This separation is smart because:

* corruption isolated
* smaller DBs
* easier reindexing
* parallel readers

---

# 9. WAL Mode

The app uses:

```sql
PRAGMA journal_mode=WAL;
```



WAL is critical because it allows:

```text
writer thread indexing
        +
multiple concurrent readers
```

without blocking.

Perfect for dictionary apps.

---

# 10. FTS5 Search Architecture

The app uses:

```sql
CREATE VIRTUAL TABLE art_fts USING fts5(...)
```



There are actually TWO search systems:

---

## A. Prefix Search (Fast Headword Search)

Used for normal lookup.

```sql
WHERE normalized_headword >= ?
AND normalized_headword < ?
```



This behaves like:

```text
starts with
```

and is extremely fast due to indexed normalized headwords.

---

## B. Fulltext Search

Uses FTS5:

```sql
WHERE art_fts MATCH ?
```



With:

```sql
bm25(art_fts)
```

ranking. 

So results are relevance-ranked.

---

# 11. Why Headwords Are Normalized

The app normalizes with:

```python
unicodedata.normalize("NFKC", text).casefold().strip()
```



This solves:

* Unicode variants
* case differences
* compatibility characters
* accent issues

So search becomes reliable.

---

# 12. Rendering Pipeline

Rendering is:

```text
DSL markup
   ↓
HTML conversion
   ↓
CSS theming
   ↓
WebKitGTK render
```

---

# 13. DSL → HTML Engine

Core converter:

```python
dsl_to_html()
```



It converts DSL tags:

```text
[b] → <strong>
[i] → <em>
[ref] → links
[s] → images
[t] → phonetics
```

etc.

---

# 14. Why WebKitGTK Is Used

The article viewer is:

```python
WebKit.WebView()
```



This is massively important.

Instead of manually rendering rich text in GTK:

the app uses a real HTML engine.

Benefits:

* CSS styling
* image support
* proper typography
* scalable rendering
* dark mode
* hyperlink support
* HTML layout
* media support potential

This is why the rendering quality is much higher than typical GTK text widgets.

---

# 15. Cross-Reference Navigation

DSL links:

```text
<<word>>
[ref]word[/ref]
```

become:

```html
<a href="lookup:word">
```



WebKit intercepts:

```python
lookup:
```

URIs and triggers new searches.

Code: 

Very elegant architecture.

---

# 16. Async Threading Architecture

The UI thread NEVER searches directly.

Instead:

```text
GTK UI
   ↓
Queue
   ↓
Worker thread
   ↓
SQLite search
   ↓
GLib.idle_add()
   ↓
Update UI safely
```

This prevents GTK freezing.

---

# 17. Search Worker

Thread:

```python
_search_worker()
```



Handles:

* prefix search
* fulltext search
* result ranking

entirely off the UI thread.

---

# 18. Lookup Worker

Separate thread:

```python
_lookup_worker()
```



Handles:

* article extraction
* HTML generation
* rendering prep

Again:
UI never blocks.

---

# 19. Search Debouncing

Searches are delayed:

```python
SEARCH_DEBOUNCE_MS = 500
```



Meaning:

typing:

```text
a
ab
abc
abcd
```

does NOT trigger 4 searches instantly.

Instead it waits until typing pauses.

Huge CPU savings.

---

# 20. Reader Connection Pool

Each thread gets:

```python
thread-local SQLite connections
```

using:

```python
threading.local()
```



This avoids:

* SQLite thread conflicts
* reconnect overhead
* global locking

---

# 21. mmap Cache

The app caches active mmap objects:

```python
OrderedDict[str, tuple[mmap.mmap, str]]
```



LRU eviction:

```python
MAX_OPEN_MMAPS = 64
```



Prevents:

* too many open files
* excessive kernel mappings

---

# 22. UI Architecture

GTK4 + Libadwaita UI structure:

```text
ApplicationWindow
 └── ToolbarView
      └── Paned
           ├── Search results
           └── WebKit article view
```

Modern GTK4 design.

---

# 23. Result List Rendering

Uses:

```python
Gtk.ColumnView
```



instead of old:

```text
GtkTreeView
```

Important because ColumnView:

* virtualizes rows
* scales better
* modern GTK4 API
* smoother scrolling

---

# 24. Dark Mode Integration

The HTML CSS uses:

```css
@media (prefers-color-scheme: dark)
```



So WebKit automatically follows GNOME dark mode.

---

# 25. Security Model

The app disables JavaScript:

```python
nav_policy.set_enable_javascript(False)
```



This is VERY important.

It prevents:

* malicious DSL HTML
* JS injection
* arbitrary script execution

while still allowing HTML rendering.

---

# 26. Performance Characteristics

This architecture scales extremely well because:

| Operation       | Complexity      |
| --------------- | --------------- |
| headword lookup | near O(log n)   |
| article load    | O(article size) |
| startup         | tiny            |
| RAM usage       | very low        |
| rendering       | GPU accelerated |
| indexing        | streaming       |

---

# 27. Why This Architecture Is Strong

This design combines:

* mmap efficiency
* SQLite reliability
* FTS5 search power
* WebKit rendering quality
* GTK4 responsiveness

without requiring:

* giant in-memory indexes
* custom rendering engines
* proprietary cache formats

It is actually very close architecturally to how professional desktop dictionary systems are built.
