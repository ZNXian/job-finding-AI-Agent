# -*- coding: utf-8 -*-
"""场景入口：将本地路径上的 txt/md 或 简历图(png/jpg/webp) 或 pdf 转为纯文本，供 llm_prepare_scene_decision。"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import FrozenSet

from config import log
from utils.files import read_txt_file
from services.vlm_services import extract_resume_plain_text_from_image

_IMAGE_SUFFIXES: FrozenSet[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})

# multipart 上传允许的扩展名（须与 ingest 能力一致）
ALLOWED_SCENE_UPLOAD_SUFFIXES: FrozenSet[str] = frozenset(
    {".txt", ".md", ".text", ".png", ".jpg", ".jpeg", ".webp", ".pdf"}
)

# PDF：全文文本短于此阈值则改渲染页图走 VLM
_PDF_TEXT_MIN_CHARS = 80
# PDF：最多渲染并调用 VLM 的页数（防成本与超时）
_PDF_MAX_VLM_PAGES = 3


def _collapse_blank_lines(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t


def _ingest_pdf(path: Path) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise ValueError("处理 PDF 需要安装 pymupdf：pip install pymupdf") from e

    try:
        doc = fitz.open(path)
    except Exception as e:
        raise ValueError(f"无法打开 PDF: {e}") from e

    try:
        parts: list[str] = []
        for i in range(len(doc)):
            try:
                parts.append(doc.load_page(i).get_text("text") or "")
            except Exception:
                parts.append("")
        merged = _collapse_blank_lines("\n".join(parts))
        if len(merged) >= _PDF_TEXT_MIN_CHARS:
            return merged

        out_chunks: list[str] = []
        n = min(len(doc), _PDF_MAX_VLM_PAGES)
        if n == 0:
            raise ValueError("PDF 无页面可读")

        for i in range(n):
            page = doc.load_page(i)
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            except Exception as ex:
                log.warning("resume_ingest: PDF 页 %s 渲染失败: %s", i, ex)
                continue
            fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="resume_pdf_")
            os.close(fd)
            try:
                pix.save(tmp_path)
                chunk = extract_resume_plain_text_from_image(tmp_path)
                if chunk.strip():
                    out_chunks.append(chunk.strip())
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if not out_chunks:
            raise ValueError(
                "PDF 抽取文本过短且 VLM 逐页解析无有效内容；请检查是否为扫描件、"
                "是否已配置 DASHSCOPE_API_KEY / VLM_MODEL，或换用 txt/png 上传"
            )
        return _collapse_blank_lines("\n\n---\n\n".join(out_chunks))
    finally:
        doc.close()


def ingest_user_document_to_text(path: str) -> str:
    """
    按扩展名解析为 UTF-8 纯文本。失败时抛出 ValueError（中文消息供 HTTP 返回）。
    """
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"文件不存在: {path}")

    suf = p.suffix.lower()
    if suf in (".txt", ".md", ".text") or suf == "":
        try:
            text = read_txt_file(str(p))
        except Exception as e:
            raise ValueError(f"读取文本文件失败: {e}") from e
        if not (text or "").strip():
            raise ValueError("文本文件内容为空")
        return _collapse_blank_lines(text)

    if suf in _IMAGE_SUFFIXES:
        raw = extract_resume_plain_text_from_image(p)
        if not (raw or "").strip():
            raise ValueError(
                "简历图 VLM 解析结果为空；请检查图片清晰度、DASHSCOPE_API_KEY 与 VLM_MODEL 配置"
            )
        return _collapse_blank_lines(raw)

    if suf == ".pdf":
        return _ingest_pdf(p)

    # 未知后缀：尝试按 UTF-8 文本读（兼容无扩展名简历）
    try:
        raw = p.read_text(encoding="utf-8", errors="strict")
    except Exception:
        raise ValueError(
            f"不支持的文件类型 {suf!r}；请使用 .txt / .md / .png / .jpg / .jpeg / .webp / .pdf"
        )
    if not raw.strip():
        raise ValueError("文件内容为空或不是有效 UTF-8 文本")
    return _collapse_blank_lines(raw)
