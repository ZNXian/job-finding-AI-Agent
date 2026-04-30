from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from config import log

router = APIRouter(tags=["config"])


def _ensure_localhost(request: Request) -> None:
    host = (getattr(request.client, "host", None) or "").strip().lower()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="仅允许本机访问该页面/接口（localhost/127.0.0.1）")


@dataclass(frozen=True)
class EnvField:
    key: str
    default: str
    help: str
    example: str | None
    group: str
    is_secret: bool


_RE_ASSIGN = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>.*)$")
_RE_COMMENT_ASSIGN = re.compile(r"^\s*#\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>.*)$")


def _guess_secret(key: str) -> bool:
    k = key.upper()
    return any(x in k for x in ("PASSWORD", "API_KEY", "APIKEY", "SECRET", "TOKEN", "KEY"))


def _config_dir() -> Path:
    # 对齐 config.py: _CONFIG_DIR = Path(__file__).resolve().parent
    return Path(__file__).resolve().parents[1]


def _env_example_path() -> Path:
    return _config_dir() / ".env.example"


def _parse_env_example(text: str) -> tuple[list[EnvField], list[str]]:
    """解析 .env.example 为字段 schema，同时保留模板行用于写回生成。"""
    lines = text.splitlines()
    fields: dict[str, EnvField] = {}

    group = "默认"
    help_buf: list[str] = []
    template_lines: list[str] = []

    def flush_help() -> str:
        nonlocal help_buf
        h = "\n".join(s.strip() for s in help_buf if s.strip()).strip()
        help_buf = []
        return h

    for raw in lines:
        line = raw.rstrip("\n")
        template_lines.append(line)

        # 分组：形如 "# ---" 或 "# =====" 作为分隔；紧跟的注释作为 group 标题
        if line.strip().startswith("#"):
            # 如果是 “# -----” 这类分隔线，不直接作为 help
            sep = line.strip().lstrip("#").strip()
            if sep and all(ch in "-=_*" for ch in sep) and len(sep) >= 3:
                # 保留当前 help_buf，不清空，等下一条注释决定
                continue
            # 用形如 "# xxxx" 且前后有分隔线的方式写的组标题：这里做一个简单启发式
            if len(sep) >= 2 and not _RE_COMMENT_ASSIGN.match(line) and not _RE_ASSIGN.match(sep):
                # 作为 help 文本累积；是否作为 group 由下一次遇到空行/字段时决定
                help_buf.append(sep)
            continue

        if not line.strip():
            # 空行：如果 help_buf 看起来像组标题，就刷新成 group
            if help_buf and len(help_buf) <= 2:
                candidate = " / ".join(help_buf).strip()
                if candidate:
                    group = candidate
                help_buf = []
            continue

        m = _RE_ASSIGN.match(line)
        if m:
            key = m.group("key")
            val = m.group("val") or ""
            help_text = flush_help()
            fields[key] = EnvField(
                key=key,
                default=val,
                help=help_text,
                example=None,
                group=group,
                is_secret=_guess_secret(key),
            )
            continue

        mc = _RE_COMMENT_ASSIGN.match(line)
        if mc:
            key = mc.group("key")
            val = mc.group("val") or ""
            # commented assignment: 作为字段的示例值（若字段已存在则补 example）
            if key in fields:
                prev = fields[key]
                if not prev.example and val.strip():
                    fields[key] = EnvField(
                        key=prev.key,
                        default=prev.default,
                        help=prev.help,
                        example=val,
                        group=prev.group,
                        is_secret=prev.is_secret,
                    )
            else:
                # 未在模板中以 KEY= 出现：也作为可选字段（默认空，example=val）
                help_text = flush_help()
                fields[key] = EnvField(
                    key=key,
                    default="",
                    help=help_text,
                    example=val if val.strip() else None,
                    group=group,
                    is_secret=_guess_secret(key),
                )
            continue

        # 其它非注释/非赋值行：清空 help 缓冲，避免误挂到下一项
        help_buf = []

    # 按出现顺序输出：先按 template_lines 扫一遍 key；剩余按 key 排序附后
    ordered: list[EnvField] = []
    seen: set[str] = set()
    for line in template_lines:
        mm = _RE_ASSIGN.match(line) or _RE_COMMENT_ASSIGN.match(line)
        if not mm:
            continue
        k = mm.group("key")
        if k in seen or k not in fields:
            continue
        ordered.append(fields[k])
        seen.add(k)
    for k in sorted(set(fields) - seen):
        ordered.append(fields[k])
    return ordered, template_lines


class EnvSchemaResponse(BaseModel):
    example_path: str
    fields: list[dict[str, Any]]


@router.get("/config/env_schema", response_model=EnvSchemaResponse)
def get_env_schema(request: Request) -> EnvSchemaResponse:
    _ensure_localhost(request)
    p = _env_example_path()
    if not p.exists():
        raise HTTPException(status_code=404, detail="未找到 .env.example")
    text = p.read_text(encoding="utf-8")
    fields, _ = _parse_env_example(text)
    return EnvSchemaResponse(
        example_path=str(p),
        fields=[
            {
                "key": f.key,
                "default": f.default,
                "help": f.help,
                "example": f.example,
                "group": f.group,
                "is_secret": f.is_secret,
            }
            for f in fields
        ],
    )


class EnvReadResponse(BaseModel):
    ok: bool
    env_path: str
    exists: bool
    values: dict[str, str]


def _env_path() -> Path:
    return _config_dir() / ".env"


def _parse_env_text(text: str) -> dict[str, str]:
    """极简 .env 解析：支持 KEY=value；忽略空行与注释行；后写覆盖前写。"""
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = str(raw).strip()
        if not line or line.startswith("#"):
            continue
        m = _RE_ASSIGN.match(line)
        if not m:
            continue
        k = m.group("key")
        v = m.group("val") or ""
        out[str(k).strip()] = str(v)
    return out


@router.get("/config/read_env", response_model=EnvReadResponse)
def read_env(request: Request) -> EnvReadResponse:
    _ensure_localhost(request)
    p = _env_path()
    if not p.exists():
        return EnvReadResponse(ok=True, env_path=str(p), exists=False, values={})
    try:
        txt = p.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取 .env 失败: {e}")
    return EnvReadResponse(ok=True, env_path=str(p), exists=True, values=_parse_env_text(txt))


class EnvWriteRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)
    backup: bool = True


class EnvWriteResponse(BaseModel):
    ok: bool
    env_path: str
    backup_path: str | None = None
    written_keys: list[str]


def _sanitize_value(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    # dotenv 一行一个 key=value；去掉换行，避免写入破坏格式
    s = s.replace("\r", "").replace("\n", " ").strip()
    return s


def _render_env_from_template(template_lines: list[str], values: dict[str, str]) -> tuple[str, list[str]]:
    provided = {k: _sanitize_value(v) for k, v in (values or {}).items() if str(k).strip()}
    written_keys: set[str] = set()

    out_lines: list[str] = []
    for line in template_lines:
        m = _RE_ASSIGN.match(line)
        if m:
            key = m.group("key")
            if key in provided:
                out_lines.append(f"{key}={provided[key]}")
                written_keys.add(key)
            else:
                out_lines.append(line)
            continue
        mc = _RE_COMMENT_ASSIGN.match(line)
        if mc:
            key = mc.group("key")
            if key in provided and provided[key] != "":
                out_lines.append(f"{key}={provided[key]}")
                written_keys.add(key)
            else:
                out_lines.append(line)
            continue
        out_lines.append(line)

    # 未出现在模板中的 key：追加到末尾
    extra = [k for k in sorted(set(provided) - written_keys)]
    if extra:
        out_lines.append("")
        out_lines.append("# 额外字段（不在 .env.example 中）：")
        for k in extra:
            out_lines.append(f"{k}={provided[k]}")
            written_keys.add(k)

    return "\n".join(out_lines).rstrip() + "\n", sorted(written_keys)


@router.post("/config/write_env", response_model=EnvWriteResponse)
def write_env(request: Request, payload: EnvWriteRequest) -> EnvWriteResponse:
    _ensure_localhost(request)
    base = _config_dir()
    env_path = base / ".env"
    ex_path = _env_example_path()
    if not ex_path.exists():
        raise HTTPException(status_code=404, detail="未找到 .env.example，无法按模板写入")

    template_text = ex_path.read_text(encoding="utf-8")
    fields, template_lines = _parse_env_example(template_text)
    allowed_keys = {f.key for f in fields}

    # 允许额外 key，但会记录提示（前端可用这个信息显示 warning）
    raw_values = payload.values or {}
    cleaned_values = {str(k).strip(): _sanitize_value(v) for k, v in raw_values.items() if str(k).strip()}
    unknown = sorted(set(cleaned_values) - allowed_keys)
    if unknown:
        log.info("config ui: received %s unknown keys: %s", len(unknown), ", ".join(unknown[:20]))

    env_text, written = _render_env_from_template(template_lines, cleaned_values)

    backup_path: Path | None = None
    if payload.backup and env_path.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = base / f".env.bak.{ts}"
        backup_path.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")

    env_path.write_text(env_text, encoding="utf-8")
    log.info("config ui: wrote .env (%s keys) to %s", len(written), env_path)
    return EnvWriteResponse(
        ok=True,
        env_path=str(env_path),
        backup_path=str(backup_path) if backup_path else None,
        written_keys=written,
    )

