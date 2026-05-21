Your architecture is already very strong.
The biggest improvements now are mostly about:

* scalability
* rendering quality
* search quality
* indexing speed
* DSL compatibility
* media support
* memory behavior
* UX polish

The current design is already far beyond most GTK dictionary apps. 

The next step is evolving it from:

```text
fast desktop dictionary
```

into:

```text
high-end dictionary platform
```

---

# 1. Biggest Architectural Improvement:

# Separate Metadata Cache From FTS Cache

Currently:

```text
entries table
+
FTS table
```

live together.

Better:

```text
dict.db
    metadata/index only

dict.fts
    fulltext only
```

Why?

FTS5 grows HUGE.

Separating:

* reduces mmap pressure
* faster backup
* faster metadata search
* optional FTS loading
* easier vacuuming

Professional search systems separate these layers.

---

# 2. Add Prefix FTS Instead of Range Scan

Current prefix search:

```sql
WHERE normalized_headword >= ?
AND normalized_headword < ?
```



This is good.

But FTS5 prefix indexing is better for gigantic collections.

Example:

```sql
CREATE VIRTUAL TABLE hw_fts USING fts5(
    headword,
    prefix='2 3 4 5'
)
```

Then:

```sql
MATCH 'dict*'
```

Benefits:

* typo tolerance later
* ranking
* stemming support
* fuzzy matching
* language tokenizers

---

# 3. Use ICU Tokenizer

Right now:

```sql
tokenize='unicode61 remove_diacritics 2'
```



Problem:

`unicode61` is weak for:

* Arabic
* Indic
* Thai
* CJK

ICU tokenizer dramatically improves multilingual search.

Especially important for dictionary software.

---

# 4. Add Morphological Search

Huge upgrade.

Example:

```text
running
→ run

better
→ good
```

Can be implemented via:

* stemming
* Hunspell
* Snowball
* language analyzers

This transforms usability.

---

# 5. Add Fuzzy Search

Current system is exact/prefix only.

Add:

```text
Levenshtein
Trigram
Spellfix1
```

SQLite has:

```sql
spellfix1
```

This enables:

```text
recieve → receive
definately → definitely
```

Critical for real dictionary UX.

---

# 6. Cache Rendered HTML

Currently every lookup does:

```text
DSL decode
→ regex transforms
→ HTML generation
```

Repeatedly.

Add:

```python
LRU rendered article cache
```

Example:

```python
@lru_cache(maxsize=2048)
```

Huge speedup for repeated navigation.

Especially cross-references.

---

# 7. Replace Regex Parser With Real DSL Parser

Current parser:

```python
re.sub(...)
```



Problem:

regex parsing breaks on nested DSL tags.

Example:

```text
[b][i]word[/i][/b]
```

A stack parser is much more reliable.

Architecture:

```text
Tokenizer
   ↓
AST
   ↓
HTML renderer
```

Much cleaner.

---

# 8. Add Resource Resolver

Current image handling:

```html
<img src="filename">
```



Problem:

DSL media often lives in:

```text
dict.files/
res/
sound/
img/
```

Need:

```text
resource resolver layer
```

that maps:

```text
[s]cat.png[/s]
```

to real filesystem/media URI.

---

# 9. Add Audio Support

Currently audio ignored:

```python
_RX_SND.sub("", line)
```



This is a major missing feature.

Instead generate:

```html
<audio controls>
```

or GTK media playback.

Huge UX improvement.

---

# 10. Add Incremental FTS Updates

Currently:

```text
full rebuild
```

on reindex.

Better:

```text
detect changed entries
update only delta
```

Important for:

* live dictionary editing
* generated dictionaries
* huge libraries

---

# 11. Add Shared Search Coordinator

Currently:

```text
search thread
lookup thread
```

are independent.

You can evolve into:

```text
Task scheduler
Priority queue
Cancellation tokens
```

Benefits:

* smoother UI
* less wasted work
* cancellation during fast typing

---

# 12. Add Real Search Ranking

Current ranking:

```sql
bm25()
```



But dictionaries need domain-aware ranking.

Example weighting:

```text
exact match        +1000
prefix match       +500
shorter words      +100
popular dictionary +50
fulltext hit       +10
```

This massively improves results.

---

# 13. Add Search Suggestions Cache

Currently every keystroke queries SQLite.

Instead:

```text
small in-memory trie
```

for:

* autocomplete
* top prefixes
* recent searches

Very fast UX.

---

# 14. Add WebKit Resource Interception

Huge future upgrade.

WebKit supports custom URI schemes.

Example:

```text
dsl://image/cat.png
dsl://audio/test.mp3
```

This is MUCH cleaner than relative paths.

Allows:

* sandboxed resources
* internal media serving
* cache control
* custom loaders

Professional browser architecture.

---

# 15. Add DOM-Level Theming

Current CSS injected globally.

Better:

```text
dictionary theme layer
+
global theme layer
```

Allows:

* per-dictionary styling
* user CSS
* typography themes
* dyslexia mode
* compact mode

---

# 16. Add Virtualized Result Model

Currently:

```python
Gio.ListStore
```

holds all results. 

For gigantic searches:

better:

```text
paged result model
```

Only load visible rows.

---

# 17. Use Snapshot Rendering Instead Of Full Reload

Current:

```python
webview.load_html()
```



This fully reloads DOM every lookup.

Better:

```javascript
replace innerHTML only
```

or WebKit user content manager.

Much smoother navigation.

---

# 18. Add Multi-Dictionary Merge Rendering

Current rendering:

```text
dictionary A
dictionary B
dictionary C
```

serially.

Could evolve into:

```text
merged semantic view
```

Example:

```text
Headword
 ├ pronunciation
 ├ definitions
 ├ examples
 ├ etymology
 └ translations
```

Very advanced feature.

---

# 19. Add Dictionary Metadata Standard

Right now dictionary metadata weak.

Add:

```json
meta.json
```

with:

```json
{
  "name": "",
  "lang_from": "",
  "lang_to": "",
  "icon": "",
  "version": "",
  "license": ""
}
```

This becomes important later.

---

# 20. Add Parallel Indexing

Currently indexes one dictionary at a time.

Can parallelize:

```text
N dictionaries
→ N worker threads
```

Massive improvement for big libraries.

---

# 21. Add Memory Budget Manager

Right now:

```python
MAX_OPEN_MMAPS = 64
```



Static limits are weak.

Better:

```text
adaptive memory budget
```

based on:

* RAM size
* pressure
* dictionary usage

---

# 22. Add Binary Headword Cache

Major speedup.

Store:

```text
normalized headwords
```

inside:

```text
compact binary trie
```

This makes autocomplete nearly instant.

SQLite still used for lookup.

Very strong hybrid architecture.

---

# 23. Add Sandboxed HTML Sanitizer

Current sanitization:

```python
re.sub("<script"...)
```



Not enough.

Should use:

* HTML parser
* whitelist sanitizer

Important for hostile dictionaries.

---

# 24. Add Background Preloading

After selecting a word:

preload:

```text
next result
previous result
linked references
```

Makes navigation feel instant.

---

# 25. Biggest Long-Term Upgrade:

# Move To Structured AST Rendering

Current pipeline:

```text
DSL
→ regex
→ HTML
```

Future ideal:

```text
DSL
→ tokenizer
→ AST
→ semantic nodes
→ HTML renderer
```

This unlocks:

* export formats
* EPUB
* PDF
* custom themes
* semantic search
* accessibility
* annotations
* inline widgets
* structured dictionaries

This is the real “next-generation dictionary engine” direction.

---

# Highest-Impact Improvements (Priority Order)

If prioritizing effort vs benefit:

| Priority | Improvement              |
| -------- | ------------------------ |
| 1        | audio/media resolver     |
| 2        | rendered HTML cache      |
| 3        | fuzzy search             |
| 4        | proper DSL parser        |
| 5        | FTS prefix search        |
| 6        | resource URI scheme      |
| 7        | structured AST           |
| 8        | incremental FTS          |
| 9        | adaptive caching         |
| 10       | semantic merge rendering |

These would dramatically elevate the app.
