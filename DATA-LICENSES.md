# Data licenses and attribution

The builder code in this repository is MIT-licensed (see [LICENSE](LICENSE)).
The **packs it produces** contain third-party data under the licenses below.
Each pack records its license, license URL, attribution and source in its
`meta` table, and the same fields appear in every `manifest.json` entry, so
an app can show attribution without hard-coding it.

## Wikipedia packs (`wiki-pt-*`, `wiki-en-*`)

Wikipedia, The Free Encyclopedia — **Wikipedia contributors**

* Portuguese: https://pt.wikipedia.org/
* English: https://en.wikipedia.org/
* Text license: Creative Commons Attribution-ShareAlike 4.0 International
  (CC BY-SA 4.0), https://creativecommons.org/licenses/by-sa/4.0/
* Source: the Wikimedia CirrusSearch content-index dumps,
  https://dumps.wikimedia.org/other/cirrus_search_index/ (the dump date of
  each edition is recorded in the pack meta `source_date` and the manifest
  `source.dump_date`).
* Wikipedia is a registered trademark of the Wikimedia Foundation, Inc. This
  project is not affiliated with or endorsed by the Wikimedia Foundation.

**Changes made** (required notice under CC BY-SA 4.0): each abstract is the
article's opening text (the text before its first section heading) with
whitespace normalized, leftovers of removed templates scrubbed (empty
parentheses, stray separators), and truncated to whole sentences of at most
600 characters (or cut at a word boundary with "…" when the first sentence
alone is longer). Titles are unchanged. Redirect titles are used only as
search keys. No text was otherwise altered.

**ShareAlike:** the packs, as adaptations of Wikipedia text, are distributed
under CC BY-SA 4.0. Anyone redistributing a pack must keep this license and
the attribution above.

## English dictionary (`dict-en`)

**Open English WordNet** — https://en-word.net/ ,
https://github.com/globalwordnet/english-wordnet

* License: Creative Commons Attribution 4.0 International (CC BY 4.0),
  https://creativecommons.org/licenses/by/4.0/
* Citation: John P. McCrae, Alexandre Rademaker, Francis Bond, Ewa Rudnicka
  and Christiane Fellbaum (2019), "English WordNet 2019 — An Open-Source
  WordNet for English", Proceedings of the 10th Global WordNet Conference.

Open English WordNet is a fork of **Princeton WordNet 3.0**.
Princeton WordNet: George A. Miller (1995), "WordNet: A Lexical Database for
English", Communications of the ACM Vol. 38, No. 11.
WordNet 3.0 license: https://wordnet.princeton.edu/license-and-commercial-use
WordNet 3.0 Copyright 2006 by Princeton University. All rights reserved.
THIS SOFTWARE AND DATABASE IS PROVIDED "AS IS" AND PRINCETON UNIVERSITY
MAKES NO REPRESENTATIONS OR WARRANTIES, EXPRESS OR IMPLIED.

## Portuguese dictionary (`dict-pt`)

**OpenWordnet-PT (OWN-PT)**, a wordnet for Brazilian Portuguese —
https://github.com/own-pt/openWordnet-PT

* License: Creative Commons Attribution 4.0 International (CC BY 4.0),
  https://creativecommons.org/licenses/by/4.0/
* Citation: Valeria de Paiva, Alexandre Rademaker and Gerard de Melo (2012),
  "OpenWordNet-PT: An Open Brazilian Wordnet for Reasoning", Proceedings of
  COLING 2012.

OpenWordnet-PT is constructed based on Princeton WordNet 3.0 (see the
Princeton WordNet attribution above).

## Dictionary modifications

The source wordnets (WN-LMF XML) are converted to compact SQLite: lemmas
normalized (NFC, lowercase, trimmed, diacritics kept), senses flattened to
definition + up to 5 synonyms + one example on the first sense, at most 10
senses per (lemma, part of speech), lemmas of three or more words dropped,
definitions capped at 300 characters and examples at 120. Search keys
(fold_v1) and a sense-count ranking are added for type-ahead. No definitions
were otherwise altered.
