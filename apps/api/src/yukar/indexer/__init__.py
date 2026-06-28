"""Indexer — repo knowledge base (tree-sitter + FAISS + embedder).

Sub-modules
-----------
languages   — extension → language name mapping
splitter    — tree-sitter / line-based code chunker
embedder    — Embedder protocol + BedrockTitanEmbedder + FakeEmbedder
faiss_store — FAISS index + chunks.jsonl persistence
summarizer  — repo structure summary + stats generation
service     — use-case façade: reindex_repo, search, get_status
watcher     — watchfiles-based file monitor with debounce + reindex
"""

from __future__ import annotations
