import hashlib
import io
import re
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from docx import Document as open_docx

from bidreader.schemas import ParsedBlock

SUPPORTED = {".docx", ".pdf", ".txt", ".md", ".zip"}
DOCUMENT_SUFFIXES = {".docx", ".pdf", ".txt", ".md"}


class ParseFailure(RuntimeError):
    def __init__(self, message: str, *, ledger: list[dict] | None = None, page_count: int = 0):
        super().__init__(message)
        self.ledger = ledger or []
        self.page_count = page_count


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _heading_level(style_name: str, text: str) -> int | None:
    match = re.match(r"heading\s*(\d+)", style_name, re.IGNORECASE)
    if match:
        return min(int(match.group(1)), 9)
    if len(text) < 90 and re.match(r"^(第[一二三四五六七八九十百\d]+[章节篇]|[一二三四五六七八九十]+、|\d+(?:\.\d+){0,3}[、. ]?)", text):
        return 1
    return None


def _numbered_heading_level(text: str) -> int | None:
    if len(text) > 100:
        return None
    if re.match(r"^\s*第[一二三四五六七八九十百\d]+[章节篇]", text):
        return 1
    if re.match(r"^\s*[一二三四五六七八九十]+、", text):
        return 1
    match = re.match(r"^\s*(\d+(?:\.\d+){0,4})[、. ]\s*\S", text)
    if match:
        return min(match.group(1).count("."), 4) + 1
    return None


def _new_block(kind: str, text: str, index: int, **kwargs) -> ParsedBlock:
    return ParsedBlock(block_id=f"b-{index:07d}", kind=kind, text=text.strip(), source_index=index, **kwargs)


@lru_cache(maxsize=1)
def _paddle_ocr():
    """Load PaddleOCR only when an image-only PDF page needs OCR."""
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise ParseFailure("扫描 PDF 需要安装可选 OCR 依赖；当前页已保留并转人工复核。") from exc
    return PaddleOCR(lang="ch", use_doc_orientation_classify=False,
                     use_doc_unwarping=False, use_textline_orientation=False)


def _ocr_page(image, page_no: int, page_width: float, page_height: float, block_offset: int) -> list[ParsedBlock]:
    try:
        import numpy as np
        result = _paddle_ocr().predict(np.asarray(image))
        if not result:
            return []
        payload = result[0].json
        payload = payload.get("res", payload) if isinstance(payload, dict) else {}
        texts = payload.get("rec_texts", [])
        boxes = payload.get("rec_boxes", [])
        scores = payload.get("rec_scores", [])
        scale_x = page_width / image.width
        scale_y = page_height / image.height
        items = []
        for index, (text, box) in enumerate(zip(texts, boxes, strict=False)):
            text = str(text).strip()
            if not text or len(box) != 4:
                continue
            x0, y0, x1, y1 = (float(value) for value in box)
            items.append((y0, x0, text, [x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y],
                          float(scores[index]) if index < len(scores) else None))
        items.sort(key=lambda item: (round(item[0] / 12), item[1]))
        return [_new_block("ocr_line", item[2], block_offset + index, page_no=page_no,
                           bbox=item[3])
                for index, item in enumerate(items)]
    except ParseFailure:
        raise
    except Exception as exc:
        raise ParseFailure(f"扫描页 OCR 失败：{type(exc).__name__}。该页不能自动标记为已解析。") from exc


def parse_docx(path: Path) -> tuple[list[ParsedBlock], list[dict]]:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            expanded_size = sum(entry.file_size for entry in entries)
            if len(entries) > 10_000 or expanded_size > 512 * 1024 * 1024:
                raise ParseFailure("DOCX 包含过多条目或解压后体积超过安全上限。")
            if any(entry.file_size > 0 and entry.file_size / max(entry.compress_size, 1) > 250 for entry in entries):
                raise ParseFailure("DOCX 内部条目压缩比异常，已阻止解包。")
            if "[Content_Types].xml" not in archive.namelist() or "word/document.xml" not in archive.namelist():
                raise ParseFailure("文件扩展名为 DOCX，但内容不是有效的 Word 文档。")
    except zipfile.BadZipFile as exc:
        raise ParseFailure("DOCX 文件不是有效的 ZIP/Open XML 容器。") from exc
    try:
        document = open_docx(path)
    except Exception as exc:
        raise ParseFailure(f"DOCX 无法读取：{exc}") from exc
    blocks: list[ParsedBlock] = []
    sections: list[dict] = []
    heading_stack: list[str] = []
    # Preserve original paragraph/table ordering from the DOCX body XML.
    elements: list[tuple[int, str, object]] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            from docx.text.paragraph import Paragraph
            elements.append((0, "p", Paragraph(child, document)))
        elif tag == "tbl":
            from docx.table import Table
            elements.append((0, "tbl", Table(child, document)))
    table_count = 0
    for _, kind, element in elements:
        if kind == "p":
            text = element.text.strip()
            if not text:
                continue
            level = _heading_level(element.style.name if element.style else "", text)
            if level is not None:
                heading_stack = heading_stack[: level - 1] + [text]
                sections.append({"title": text, "level": level, "section_path": heading_stack.copy(), "block_id": f"b-{len(blocks):07d}"})
            blocks.append(_new_block("heading" if level else "paragraph", text, len(blocks), section_path=heading_stack.copy()))
        else:
            table_id = f"t-{table_count:05d}"
            table_count += 1
            for row_no, row in enumerate(element.rows):
                cells = [re.sub(r"\s+", " ", cell.text).strip() for cell in row.cells]
                if not any(cells):
                    continue
                blocks.append(_new_block("table_row", " | ".join(cells), len(blocks), section_path=heading_stack.copy(), table_id=table_id, row_index=row_no, cells=cells))
    return blocks, sections


def parse_pdf(path: Path, max_pages: int, *, ocr_enabled: bool = True, ocr_dpi: int = 180) -> tuple[list[ParsedBlock], list[dict], list[dict], list[dict]]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise ParseFailure("解析 PDF 需要安装 pdfplumber。") from exc
    blocks: list[ParsedBlock] = []
    pages: list[dict] = []
    ledger: list[dict] = []
    sections: list[dict] = []
    heading_stack: list[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) > max_pages:
                raise ParseFailure(f"PDF 页数 {len(pdf.pages)} 超过配置上限 {max_pages}。")
            for page_no, page in enumerate(pdf.pages, 1):
                pages.append({"page_no": page_no, "width": page.width, "height": page.height})
                words = page.extract_words(x_tolerance=2, y_tolerance=3, use_text_flow=True) or []
                for word in words:
                    text = word.get("text", "").strip()
                    if not text:
                        continue
                    blocks.append(_new_block("word", text, len(blocks), page_no=page_no, bbox=[word["x0"], word["top"], word["x1"], word["bottom"]]))
                page_text = page.extract_text(layout=False) or ""
                # Group PDF words into text lines so evidence has a useful bbox.
                lines: dict[int, list[dict]] = {}
                for word in words:
                    lines.setdefault(round(word["top"] / 3), []).append(word)
                for line_words in lines.values():
                    line_words.sort(key=lambda item: item["x0"])
                    line = " ".join(item["text"] for item in line_words).strip()
                    if line:
                        bbox = [min(item["x0"] for item in line_words), min(item["top"] for item in line_words),
                                max(item["x1"] for item in line_words), max(item["bottom"] for item in line_words)]
                        level = _numbered_heading_level(line)
                        heading_match = level is not None
                        if heading_match:
                            heading_stack = heading_stack[:level - 1] + [line]
                        block = _new_block("heading" if heading_match else "line", line, len(blocks), page_no=page_no, bbox=bbox,
                                           section_path=heading_stack.copy())
                        blocks.append(block)
                        if heading_match:
                            sections.append({"title": line, "level": level, "section_path": heading_stack.copy(), "block_id": block.block_id, "page_no": page_no})
                # Preserve table rows and cell-level regions for later criterion extraction.
                try:
                    tables = page.find_tables()
                except (AttributeError, ValueError):
                    tables = []
                for table_index, table in enumerate(tables):
                    table_id = f"p{page_no}-t{table_index:03d}"
                    for row_index, row in enumerate(table.rows):
                        row_text: list[str] = []
                        for cell_index, cell_bbox in enumerate(row.cells):
                            if cell_bbox is None:
                                continue
                            x0, top, x1, bottom = cell_bbox
                            cell_text = (page.crop((x0, top, x1, bottom)).extract_text() or "").strip()
                            row_text.append(cell_text)
                            if cell_text:
                                blocks.append(_new_block("table_cell", cell_text, len(blocks), page_no=page_no,
                                                         bbox=[x0, top, x1, bottom], table_id=table_id,
                                                         row_index=row_index, cell_index=cell_index, section_path=heading_stack.copy()))
                        if any(row_text):
                            actual_cells = [cell for cell in row.cells if cell is not None]
                            bbox = [min(cell[0] for cell in actual_cells), min(cell[1] for cell in actual_cells),
                                    max(cell[2] for cell in actual_cells), max(cell[3] for cell in actual_cells)] if actual_cells else list(table.bbox)
                            blocks.append(_new_block("table_row", " | ".join(row_text), len(blocks), page_no=page_no,
                                                     bbox=bbox, table_id=table_id, row_index=row_index, section_path=heading_stack.copy(), cells=row_text))
                ocr_blocks: list[ParsedBlock] = []
                reason = "页面未检测到可提取文本；需 OCR 或人工复核。"
                if not page_text.strip() and ocr_enabled:
                    try:
                        rendered = page.to_image(resolution=ocr_dpi).original
                        ocr_blocks = _ocr_page(rendered, page_no, page.width, page.height, len(blocks))
                        blocks.extend(ocr_blocks)
                    except ParseFailure as exc:
                        reason = str(exc)
                    except Exception as exc:  # noqa: BLE001 - isolate a page failure and keep its ledger entry
                        reason = f"页面渲染/OCR不可用：{type(exc).__name__}。"
                state = "done" if page_text.strip() or ocr_blocks else "failed_review"
                reason = None if state == "done" else reason
                ledger.append({"object_id": f"page-{page_no:05d}", "kind": "page", "state": state, "reason": reason})
    except ParseFailure:
        raise
    except Exception as exc:
        raise ParseFailure(f"PDF 解析失败：{exc}") from exc
    if not any(block.kind in {"line", "ocr_line"} for block in blocks):
        raise ParseFailure("PDF 未提取到文本。该扫描件需要 OCR，系统已阻止将其标记为解析成功。",
                           ledger=ledger, page_count=len(pages))
    return blocks, sections, pages, ledger


def parse_text(path: Path) -> tuple[list[ParsedBlock], list[dict]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseFailure("文本编码无法识别，请转换为 UTF-8 或上传 DOCX/PDF。") from exc
    blocks: list[ParsedBlock] = []
    sections: list[dict] = []
    stack: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        level = 1 if path.suffix.lower() == ".md" and line.startswith("#") else _heading_level("", line)
        if level:
            title = line.lstrip("# ")
            stack = stack[: level - 1] + [title]
            sections.append({"title": title, "level": level, "section_path": stack.copy(), "block_id": f"b-{len(blocks):07d}"})
            kind = "heading"
        else:
            kind = "paragraph"
        blocks.append(_new_block(kind, line, len(blocks), section_path=stack.copy()))
    return blocks, sections


def _zip_member_name(name: str) -> str:
    try:
        return name.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _parse_archive(path: Path, max_pages: int, *, ocr_enabled: bool, ocr_dpi: int) -> dict:
    blocks: list[ParsedBlock] = []
    sections: list[dict] = []
    pages: list[dict] = []
    ledger: list[dict] = []
    warnings: list[str] = []
    counts = {"entries": 0, "expanded": 0, "documents": 0, "skipped": 0}

    def visit(data: bytes, archive_name: str, depth: int = 0) -> None:
        if depth > 4:
            raise ParseFailure("ZIP 嵌套超过 4 层，已停止解析。")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ParseFailure(f"压缩包无效：{archive_name}") from exc
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                counts["entries"] += 1
                counts["expanded"] += info.file_size
                if counts["entries"] > 10_000 or counts["expanded"] > 512 * 1024 * 1024:
                    raise ParseFailure("压缩包条目数或解压后总体积超过安全上限。", ledger=ledger)
                if info.file_size / max(info.compress_size, 1) > 250:
                    raise ParseFailure(f"压缩包内条目压缩比异常，已停止解析：{info.filename}", ledger=ledger)
                member = _zip_member_name(info.filename).replace("\\", "/")
                parts = Path(member).parts
                if member.startswith("/") or ".." in parts:
                    ledger.append({"object_id": f"archive-entry-{counts['entries']}", "kind": "archive_entry",
                                   "state": "failed_review", "reason": "压缩包成员路径不安全，未读取。", "source_file": member})
                    continue
                source_file = f"{archive_name}!/{member}"
                suffix = Path(member).suffix.lower()
                if suffix == ".zip":
                    visit(archive.read(info), source_file, depth + 1)
                    continue
                if suffix not in DOCUMENT_SUFFIXES:
                    counts["skipped"] += 1
                    if suffix in {".doc", ".xlsx", ".xls", ".sign"}:
                        ledger.append({"object_id": f"archive-entry-{counts['entries']}", "kind": suffix[1:] or "file",
                                       "state": "failed_review", "reason": "当前解析器不支持此附件格式，未纳入自动分析。",
                                       "source_file": source_file})
                    continue
                if counts["documents"] >= 200:
                    raise ParseFailure("压缩包中可解析文件超过 200 个，已停止解析。", ledger=ledger)
                counts["documents"] += 1
                prefix = f"d-{counts['documents']:04d}-"
                try:
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                        temp_file.write(archive.read(info))
                        temp_path = Path(temp_file.name)
                    try:
                        if suffix == ".docx":
                            parsed_blocks, parsed_sections = parse_docx(temp_path)
                            parsed_pages: list[dict] = []
                            parsed_ledger = [{"object_id": b.block_id, "kind": b.kind, "state": "done", "reason": None} for b in parsed_blocks]
                        elif suffix == ".pdf":
                            if temp_path.read_bytes()[:5] != b"%PDF-":
                                raise ParseFailure("PDF 文件头无效。")
                            parsed_blocks, parsed_sections, parsed_pages, parsed_ledger = parse_pdf(
                                temp_path, max_pages, ocr_enabled=ocr_enabled, ocr_dpi=ocr_dpi
                            )
                        else:
                            parsed_blocks, parsed_sections = parse_text(temp_path)
                            parsed_pages = []
                            parsed_ledger = [{"object_id": b.block_id, "kind": b.kind, "state": "done", "reason": None} for b in parsed_blocks]
                        if not parsed_blocks:
                            raise ParseFailure("文档没有可解析文字或结构。")
                    finally:
                        temp_path.unlink(missing_ok=True)
                    id_map = {block.block_id: f"{prefix}{block.block_id}" for block in parsed_blocks}
                    for block in parsed_blocks:
                        block.block_id = id_map[block.block_id]
                        block.table_id = f"{prefix}{block.table_id}" if block.table_id else None
                        block.source_file = source_file
                        blocks.append(block)
                    for section in parsed_sections:
                        section["block_id"] = id_map.get(section.get("block_id"), section.get("block_id"))
                        section["source_file"] = source_file
                        sections.append(section)
                    for page in parsed_pages:
                        pages.append({**page, "source_file": source_file})
                    for item in parsed_ledger:
                        item["object_id"] = f"{prefix}{item['object_id']}"
                        item["source_file"] = source_file
                        ledger.append(item)
                    if suffix == ".docx":
                        warnings.append(f"{source_file}：页码需经固定版本文档渲染后映射。")
                except ParseFailure as exc:
                    ledger.append({"object_id": prefix.rstrip("-"), "kind": suffix[1:], "state": "failed_review",
                                   "reason": str(exc), "source_file": source_file})
                    warnings.append(f"{source_file} 未能解析，需人工复核。")

    visit(path.read_bytes(), path.name)
    if not blocks:
        raise ParseFailure("压缩包中没有可解析的 DOCX、PDF、TXT 或 Markdown 附件。", ledger=ledger)
    if counts["skipped"]:
        warnings.append(f"压缩包内有 {counts['skipped']} 个不支持的附件（例如旧版 DOC 或表格文件），未纳入自动分析。")
    chunks: list[dict] = []
    current: list[ParsedBlock] = []
    char_count = 0
    for block in blocks:
        if current and char_count + len(block.text) > 1600:
            chunks.append({"chunk_id": f"chunk-{len(chunks):06d}", "block_ids": [item.block_id for item in current],
                           "section_path": current[-1].section_path, "text": "\n".join(item.text for item in current)})
            current, char_count = [], 0
        current.append(block)
        char_count += len(block.text)
    if current:
        chunks.append({"chunk_id": f"chunk-{len(chunks):06d}", "block_ids": [item.block_id for item in current],
                       "section_path": current[-1].section_path, "text": "\n".join(item.text for item in current)})
    package_match = next((match for block in blocks
                          if "技术规范书" in (block.source_file or "")
                          and (match := re.search(r"结算审核\s*包\s*\d+", block.text))), None)
    if package_match is None:
        package_match = next((match for block in blocks
                              if (match := re.search(r"结算审核\s*包\s*\d+", block.text))), None)
    return {"blocks": [block.model_dump() for block in blocks], "sections": sections, "pages": pages,
            "chunks": chunks, "ledger": ledger, "parser": "nested-zip+docx/pdf/text",
            "warnings": warnings[:40], "scope_hint": package_match.group(0) if package_match else None,
            "archive_stats": counts, "document_id": str(uuid4()), "sha256": file_hash(path)}


def parse_document(path: Path, max_pages: int, *, ocr_enabled: bool = True, ocr_dpi: int = 180) -> dict:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ParseFailure(f"暂不支持 {suffix or '无扩展名'}。当前支持 DOCX、PDF、TXT、Markdown 和 ZIP 文件包。")
    if suffix == ".zip":
        return _parse_archive(path, max_pages, ocr_enabled=ocr_enabled, ocr_dpi=ocr_dpi)
    pages: list[dict] = []
    if suffix == ".docx":
        blocks, sections = parse_docx(path)
        ledger = [{"object_id": block.block_id, "kind": block.kind, "state": "done", "reason": None} for block in blocks]
        for section in sections:
            section["page_no"] = None
        parser = "python-docx"
        warnings = ["DOCX 原生段落定位已记录；页码和页面坐标需固定版本渲染器后建立映射。"]
    elif suffix == ".pdf":
        if path.read_bytes()[:5] != b"%PDF-":
            raise ParseFailure("PDF 文件头无效。")
        blocks, sections, pages, ledger = parse_pdf(path, max_pages, ocr_enabled=ocr_enabled, ocr_dpi=ocr_dpi)
        parser = "pdfplumber+paddleocr-optional"
        warnings = ["一页或多页扫描内容未能 OCR，必须人工复核。"] if any(item["state"] != "done" for item in ledger) else []
    else:
        blocks, sections = parse_text(path)
        ledger = [{"object_id": block.block_id, "kind": block.kind, "state": "done", "reason": None} for block in blocks]
        parser = "utf-8-text"
        warnings = ["纯文本没有原始分页坐标；证据以段落锚点和原文片段定位。"]
    if not blocks:
        raise ParseFailure("文档中没有可解析的文字或结构对象。")
    chunks = []
    current: list[ParsedBlock] = []
    char_count = 0
    for block in blocks:
        text_length = len(block.text)
        if current and char_count + text_length > 1600:
            chunks.append({"chunk_id": f"chunk-{len(chunks):06d}", "block_ids": [item.block_id for item in current],
                           "section_path": current[-1].section_path, "text": "\n".join(item.text for item in current)})
            current, char_count = [], 0
        current.append(block)
        char_count += text_length
    if current:
        chunks.append({"chunk_id": f"chunk-{len(chunks):06d}", "block_ids": [item.block_id for item in current],
                       "section_path": current[-1].section_path, "text": "\n".join(item.text for item in current)})
    return {"blocks": [block.model_dump() for block in blocks], "sections": sections, "pages": pages, "chunks": chunks,
            "ledger": ledger, "parser": parser, "warnings": warnings,
            "document_id": str(uuid4()), "sha256": file_hash(path)}
