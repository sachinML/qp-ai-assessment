import re
import json
from pathlib import Path
from typing import List, Dict, Iterable, Tuple

from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
)


def recommend_chunk_params(
    model_ctx_tokens: int = 8192,
    contexts_to_send: int = 2,
    ctx_share_for_context: float = 0.6,
    overlap_ratio: float = 0.15,
    chars_per_token: float = 4.0,
) -> Tuple[int, int]:
    """
    Recommend (chunk_size_chars, overlap_chars) from model constraints.
    - model_ctx_tokens: total context window for your LLM
    - contexts_to_send: how many chunks you'll send to the model (e.g., top-2)
    - ctx_share_for_context: fraction of window you want to allocate to retrieved context (vs question/system etc.)
    - overlap_ratio: typically 0.10–0.20
    - chars_per_token: rough avg; 1 token ≈ 4 chars (English/Markdown)
    """
    usable_tokens = int(model_ctx_tokens * ctx_share_for_context)
    tokens_per_chunk = usable_tokens // max(1, contexts_to_send)
    # Convert tokens to characters
    chunk_size_chars = int(tokens_per_chunk * chars_per_token)
    # Clamp to sensible bounds
    chunk_size_chars = max(600, min(chunk_size_chars, 1500))  # typical sweet spot for docs
    overlap_chars = max(80, int(chunk_size_chars * overlap_ratio))
    return chunk_size_chars, overlap_chars



CODE_BLOCK_RE = re.compile(r"```.*?\n.*?```", re.DOTALL)

def _find_tables(md: str) -> List[Tuple[int, int]]:
    """
    Detect contiguous Markdown table blocks (lines starting with '|').
    Returns character spans [(start, end), ...].
    """
    lines = md.splitlines(True)
    # map each line -> (text, start, end) in char positions
    pos = 0
    line_spans = []
    for ln in lines:
        line_spans.append((ln, pos, pos + len(ln)))
        pos += len(ln)

    spans = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            start_i = i
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                i += 1
            start_char = line_spans[start_i][1]
            end_char = line_spans[i - 1][2]
            spans.append((start_char, end_char))
        else:
            i += 1
    return spans

def _atomic_spans(text: str) -> List[Tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in CODE_BLOCK_RE.finditer(text)]
    spans += _find_tables(text)
    # merge overlapping/adjacent
    spans.sort()
    merged: List[List[int]] = []
    for s, e in spans:
        if not merged or s > merged[-1][1]:
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)
    return [(s, e) for s, e in merged]

def _segment_by_spans(text: str, spans: List[Tuple[int, int]]):
    segs = []
    cur = 0
    for s, e in spans:
        if cur < s:
            segs.append(("normal", text[cur:s]))
        segs.append(("atomic", text[s:e]))
        cur = e
    if cur < len(text):
        segs.append(("normal", text[cur:]))
    return segs


def _split_normal_segment(s: str, chunk_size: int, overlap: int) -> List[str]:
    pieces = []
    start = 0
    L = len(s)
    while start < L:
        end = min(start + chunk_size, L)
        # Prefer to end on paragraph/line/space boundaries
        cut = s.rfind("\n\n", start, end)
        if cut == -1:
            cut = s.rfind("\n", start, end)
        if cut == -1:
            cut = s.rfind(" ", start, end)
        if cut == -1 or cut <= start:
            cut = end
        piece = s[start:cut].strip()
        if piece:
            pieces.append(piece)
        # back up by overlap chars
        start = max(cut - overlap, start + 1)
    return pieces

def chunk_markdown_preserve_blocks(
    md_text: str,
    base_metadata: Dict = None,
    chunk_size: int = 900,
    overlap: int = 130,
) -> List[Document]:
    """
    Chunk Markdown while preserving fenced code blocks and markdown tables intact.
    Greedy packing: never splits an 'atomic' (code/table) segment.
    """
    base_metadata = base_metadata or {}
    spans = _atomic_spans(md_text)
    segments = _segment_by_spans(md_text, spans)

    parts: List[str] = []
    kinds: List[str] = []
    for kind, seg in segments:
        if kind == "atomic":
            parts.append(seg)
            kinds.append("atomic")
        else:
            subs = _split_normal_segment(seg, chunk_size, overlap)
            parts.extend(subs)
            kinds.extend(["normal"] * len(subs))

    chunks: List[Document] = []
    buf = ""
    for part, kind in zip(parts, kinds):
        if not buf:
            buf = part
            continue
        if len(buf) + 1 + len(part) > chunk_size:
            chunks.append(Document(page_content=buf, metadata=base_metadata.copy()))
            buf = part
        else:
            buf = buf + ("\n" if not buf.endswith("\n") else "") + part
    if buf.strip():
        chunks.append(Document(page_content=buf, metadata=base_metadata.copy()))
    return chunks



def chunk_pdf_markdown_items(
    markdown_items: Iterable[str],
    names: Iterable[str] = None,
    chunk_size: int = 900,
    overlap: int = 130,
    headers_to_split_on = (("#", "chapter"), ("##", "section"), ("###", "subsection")),
) -> List[Document]:
    """
    markdown_items: list/iterable of Markdown texts (strings).
    names: optional same-length iterable for 'source' metadata (e.g., file names).
    """
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=list(headers_to_split_on),
        strip_headers=False,
    )

    out_docs: List[Document] = []
    if names is None:
        names = [f"md_{i}" for i, _ in enumerate(markdown_items)]

    for md_text, name in zip(markdown_items, names):
        # Step 1: header-aware split (returns Documents with chapter/section metadata)
        section_docs = header_splitter.split_text(md_text)
        for d in section_docs:
            meta = {"source": name, **d.metadata}
            # Hierarchical path for filtering/attribution
            path = " / ".join([
                meta.get("chapter", ""),
                meta.get("section", ""),
                meta.get("subsection", ""),
            ]).strip(" /")
            if path:
                meta["path"] = path

            # Step 2: preserve code/tables while chunking by size
            chunks = chunk_markdown_preserve_blocks(
                d.page_content, base_metadata=meta, chunk_size=chunk_size, overlap=overlap
            )
            out_docs.extend(chunks)
    return out_docs



def save_chunks_jsonl(chunks: List[Document], out_path: str):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for d in chunks:
            f.write(json.dumps(
                {"text": d.page_content, "metadata": d.metadata},
                ensure_ascii=False
            ) + "\n")

def save_chunks_json(chunks: List[Document], out_path: str, pretty: bool = False):
    data = [{"text": d.page_content, "metadata": d.metadata} for d in chunks]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2 if pretty else None)



if __name__ == "__main__":

    default_chunk, default_overlap = recommend_chunk_params(
        model_ctx_tokens=8192,
        contexts_to_send=2,
        ctx_share_for_context=0.6,
        overlap_ratio=0.15,
        chars_per_token=4.0,
    )
    # You can override:
    chunk_size = min(1200, default_chunk)  # cap at 1200 for precision
    overlap = max(120, default_overlap)

    # === B) Load Markdown files (already extracted from PDFs) ===
    # Replace these with your actual files or pass strings directly
    input_paths = ["output.md"]  # <-- put your MD file paths here
    md_texts = []
    names = []
    for p in input_paths:
        p = Path(p)
        names.append(p.stem)
        md_texts.append(p.read_text(encoding="utf-8"))

    # === C) Chunk ===
    chunks = chunk_pdf_markdown_items(
        markdown_items=md_texts,
        names=names,
        chunk_size=chunk_size,
        overlap=overlap,
        headers_to_split_on=(("#", "chapter"), ("##", "section"), ("###", "subsection")),
    )
    print(f"Created {len(chunks)} chunks (chunk_size={chunk_size}, overlap={overlap})")

    # === D) Save ===
    out_jsonl = "data/chunks.md.jsonl"
    save_chunks_jsonl(chunks, out_jsonl)
    print(f" Saved JSONL -> {out_jsonl}")

    # Optional pretty JSON (for inspection)
    save_chunks_json(chunks, "data/chunks.md.json", pretty=True)
