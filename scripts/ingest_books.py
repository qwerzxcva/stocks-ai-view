#!/usr/bin/env python3
"""ingest_books.py — 把用户自备的正版电子书解析成可核对的原文文本

为什么要有这一步：docs/books/ 的卡片来源长期卡在"模型蒸馏"——32 本书里只有 3 本
公版英文原著能整本核对。其余中文书的正版电子版只有用户自己手上有，去抓微信读书/
京东读书的正文等于绕过付费与 DRM，不做。所以入口只有一个：用户把正版 EPUB/PDF 放进
`~/workspace/books/`，本脚本负责解析成按章切分的纯文本，供卡片逐条回填"原文核对"。

输入：`~/workspace/books/**/*.epub|*.pdf`（跳过 cache/、raw/）
输出：`~/.aharou/knowledge/sources/books/<slug>.txt`
      `~/.aharou/knowledge/sources/books/INGEST_INDEX.json`（sha256 → 已解析记录）

用法：
    python3 scripts/ingest_books.py                    # 解析 books/ 下全部电子书
    python3 scripts/ingest_books.py --dry-run          # 只列出会发现什么
    python3 scripts/ingest_books.py --force            # 忽略 sha256 幂等，重新解析

EPUB 只依赖标准库（zipfile + OPF spine + HTMLParser）；PDF 依赖 pypdf，缺了会明确
报错而不是自动装包。扫描版 PDF 抽不出文字时如实报 0 字符并以非零退出，不假装成功。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import sys
import time
import zipfile
from html import unescape
from html.parser import HTMLParser

DEFAULT_SRC = os.path.expanduser("~/workspace/books")
DEFAULT_OUT = os.path.expanduser("~/.aharou/knowledge/sources/books")
INDEX_NAME = "INGEST_INDEX.json"
SKIP_DIRS = {"cache", "raw"}
BOOK_EXTS = (".epub", ".pdf")

BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "blockquote", "pre", "td", "th",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        # convert_charrefs=False 是必须的：若先转实体，正文里的 "&lt;" 会变成 "<"，
        # HTMLParser 会把后续内容当成标签，导致 "</p>" 这种残渣混进正文。
        super().__init__(convert_charrefs=False)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._skip:
            self._chunks.append(unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if not self._skip:
            self._chunks.append(unescape(f"&#{name};"))

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name).strip("_")
    return slug or "book"


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _xml_attr(tag: str, attr: str) -> str:
    match = re.search(rf'{attr}\s*=\s*"([^"]*)"', tag)
    return match.group(1) if match else ""


def read_epub(path: str) -> tuple[str, list[str]]:
    """返回 (书名, 按 spine 顺序的章节正文列表)。"""
    with zipfile.ZipFile(path) as zf:
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
        # 必须限定 <rootfile 后跟空白，否则会先匹配到父节点 <rootfiles>
        rootfile = _xml_attr(re.search(r"<rootfile\s[^>]*>", container).group(0), "full-path")
        base = posixpath.dirname(rootfile)
        opf = zf.read(rootfile).decode("utf-8", "replace")

        title_match = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""

        manifest: dict[str, tuple[str, str]] = {}
        for item in re.finditer(r"<item\b[^>]*>", opf):
            tag = item.group(0)
            manifest[_xml_attr(tag, "id")] = (_xml_attr(tag, "href"), _xml_attr(tag, "media-type"))

        chapters: list[str] = []
        for ref in re.finditer(r"<itemref\b[^>]*>", opf):
            href, media = manifest.get(_xml_attr(ref.group(0), "idref"), ("", ""))
            if not href or ("html" not in media and not href.endswith((".xhtml", ".html", ".htm"))):
                continue
            member = posixpath.normpath(posixpath.join(base, href)) if base else href
            try:
                body = zf.read(member).decode("utf-8", "replace")
            except KeyError:
                continue
            text = html_to_text(body)
            if len(text) >= 20:  # 封面/版权页只有几个字，不进正文；章节标题页仍保留
                chapters.append(text)

    if not chapters:
        raise ValueError("EPUB 里没抽到正文（可能是纯图片版）")
    return title or os.path.splitext(os.path.basename(path))[0], chapters


def read_pdf(path: str) -> tuple[str, list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # 明确报错，不自动装包
        raise SystemExit("缺少 pypdf，无法解析 PDF：pip install pypdf（或先转成 EPUB）") from exc

    try:
        reader = PdfReader(path)
    except Exception as exc:  # pypdf 对损坏文件抛的是自己的 PdfStreamError，不是 ValueError
        raise ValueError(f"PDF 打不开：{exc}") from exc
    title = ""
    try:
        title = (reader.metadata or {}).get("/Title", "") or ""
    except Exception:
        title = ""

    pages: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
        if text:
            pages.append(text)

    total = sum(len(p) for p in pages)
    if total < 200:
        raise ValueError(f"PDF 只抽出 {total} 字符，疑似扫描版（需先 OCR，本脚本不做）")
    return title.strip() or os.path.splitext(os.path.basename(path))[0], pages


def render(title: str, source: str, digest: str, units: list[str], unit_label: str) -> str:
    head = [
        f"书名：{title}",
        f"来源文件：{os.path.basename(source)}",
        f"sha256：{digest}",
        f"解析时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"篇幅：{len(units)} {unit_label}，{sum(len(u) for u in units)} 字符",
        "说明：本文件由用户自备的正版电子书解析而来，仅用于卡片判据的原文核对。",
        "",
    ]
    body = []
    for i, unit in enumerate(units, 1):
        body.append(f"\n\n===== {unit_label} {i} =====\n\n{unit}")
    return "\n".join(head) + "".join(body).lstrip()


def discover(src: str) -> list[str]:
    found: list[str] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.lower().endswith(BOOK_EXTS) and not name.startswith("._"):
                found.append(os.path.join(root, name))
    return sorted(found)


def load_index(out_dir: str) -> dict:
    path = os.path.join(out_dir, INDEX_NAME)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_index(out_dir: str, index: dict) -> None:
    path = os.path.join(out_dir, INDEX_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="解析用户自备的 EPUB/PDF 成正文章节文本")
    ap.add_argument("--src", default=DEFAULT_SRC, help=f"电子书目录（默认 {DEFAULT_SRC}）")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"输出目录（默认 {DEFAULT_OUT}）")
    ap.add_argument("--force", action="store_true", help="忽略 sha256 幂等，重新解析")
    ap.add_argument("--dry-run", action="store_true", help="只列出会发现的书，不写文件")
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        print(f"源目录不存在：{args.src}", file=sys.stderr)
        return 2

    books = discover(args.src)
    if not books:
        print(f"{args.src} 下没发现 EPUB/PDF。把正版电子书放进去再跑。")
        return 0

    os.makedirs(args.out, exist_ok=True)
    index = load_index(args.out)
    failures: list[tuple[str, str]] = []

    for path in books:
        name = os.path.relpath(path, args.src)
        digest = sha256_of(path)
        known = index.get(digest)
        if known and os.path.exists(os.path.join(args.out, known["output"])) and not args.force:
            print(f"[skip] {name} — 已解析过（{known['output']}，{known['chars']} 字符）")
            continue
        if args.dry_run:
            print(f"[dry]  {name} — {digest[:12]}")
            continue

        try:
            if path.lower().endswith(".epub"):
                title, units = read_epub(path)
                label = "章节"
            else:
                title, units = read_pdf(path)
                label = "页组"
        except Exception as exc:  # 单本坏文件不能带倒整轮扫描
            failures.append((name, str(exc)))
            print(f"[fail] {name} — {exc}", file=sys.stderr)
            continue

        text = render(title, path, digest, units, label)
        out_name = f"{slugify(title)}.txt"
        with open(os.path.join(args.out, out_name), "w", encoding="utf-8") as fh:
            fh.write(text)

        index[digest] = {
            "source": name,
            "title": title,
            "output": out_name,
            "chars": len(text),
            "units": len(units),
            "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        print(f"[ok]   {name} → {out_name}（{title}，{len(units)} {label}，{len(text)} 字符）")

    if not args.dry_run:
        save_index(args.out, index)

    if failures:
        print(f"\n{len(failures)} 本失败：" + "；".join(f"{n}（{e}）" for n, e in failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
