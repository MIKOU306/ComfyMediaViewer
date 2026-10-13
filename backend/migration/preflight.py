"""Read-only inventory and JSON integrity checks for a CMV data directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any


MAX_JSON_BYTES = 16 * 1024 * 1024
_PERSISTENT_NAMES = {
    "settings.json", "favorites.json", "favorites.seq.json", "tags.json",
    "recent.json", "marks.json", "nsfw_manual.json", "index.json",
}
_DERIVED_NAMES = {"prompt_index.json", "nsfw_index.json"}
_BACKUP_SUFFIXES = (".bak", ".backup", ".old", ".orig", ".save", ".copy")


class _ObjectPairs:
    """JSON object retaining pairs so duplicate keys remain observable."""

    __slots__ = ("pairs",)

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        self.pairs = pairs


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attrs = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(reparse and attrs & reparse)


def _classify(rel: str, is_dir: bool) -> str:
    parts = Path(rel).parts
    lowered = tuple(p.casefold() for p in parts)
    name = lowered[-1] if lowered else ""
    if "thumbs" in lowered:
        return "excluded" if is_dir else "derived"
    if "nsfw_index" in lowered or name in _DERIVED_NAMES:
        return "derived"
    if name.endswith(_BACKUP_SUFFIXES) or any(
        token in name for token in (".backup.", ".bak.", ".old.", "backup")
    ):
        return "backup"
    if name in _PERSISTENT_NAMES or "trash" in lowered:
        return "persistent"
    if is_dir:
        return "unknown"
    return "unknown"


def _pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _duplicate_keys(value: Any, pointer: str = "") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, _ObjectPairs):
        counts: dict[str, int] = {}
        for key, _ in value.pairs:
            counts[key] = counts.get(key, 0) + 1
        for key, count in counts.items():
            if count > 1:
                found.append({
                    "code": "duplicate_json_key",
                    "pointer": pointer,
                    "key": key,
                    "occurrences": count,
                })
        for key, child in value.pairs:
            found.extend(_duplicate_keys(child, f"{pointer}/{_pointer_part(key)}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_duplicate_keys(child, f"{pointer}/{index}"))
    return found


def _json_issues(rel: str, suffix: str, size: int, raw: bytes | None,
                 kind: str = "unknown") -> list[dict[str, Any]]:
    # CMV3-LOCAL-FIX(D1, 2026-10-12) —— 派生缓存豁免 JSON 判据。
    # 上游对**所有** `.json` 后缀文件一律做「大小 ≤ MAX_JSON_BYTES + 可解析 + 无重复键」
    # 三道判据。但 `prompt_index.json` / `nsfw_index.*` 已被 _classify 判为 **derived**
    # （可重建的派生缓存），且**完全不参与导入**：它们既不在 import_legacy 的
    # `_IMPORTED_JSON`（settings/favorites/favorites.seq/tags/marks），也不在
    # `_RETAINED_NAMES`（recent/nsfw_manual/nsfw_index）。
    # ⇒ 一个「不参与导入、坏了还能重建」的缓存，却因为超过 16MB 阻断整条迁移：
    #   create_snapshot 的 `before['ready']` 为假 ⇒ SnapshotError('Preflight issues
    #   block backup') ⇒ prepare_migration 失败。
    # 实测（2026-10-12，主人真库只读副本）：prompt_index.json 20.2MB +
    #   backend/cmv-data/prompt_index.json 17.9MB ⇒ ready=False，3.0.0 迁移迁不动。
    # 处置：派生缓存**只保留 sha256**（供 source_gate 核对「源未变」仍需逐字节哈希），
    #   不再做大小/结构判据。非派生文件判据一字不改。
    if kind == "derived":
        return []
    if suffix.casefold() != ".json":
        return []
    if size > MAX_JSON_BYTES:
        return [{"code": "json_too_large", "path": rel, "size": size,
                 "limit": MAX_JSON_BYTES, "blocks_migration": True}]
    if raw is None or len(raw) > MAX_JSON_BYTES:
        return [{"code": "json_too_large", "path": rel,
                 "size_at_least": len(raw) if raw is not None else size,
                 "limit": MAX_JSON_BYTES, "blocks_migration": True}]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [{"code": "invalid_utf8", "path": rel, "offset": exc.start,
                 "blocks_migration": True}]
    try:
        parsed = json.loads(text, object_pairs_hook=_ObjectPairs)
    except json.JSONDecodeError as exc:
        return [{"code": "invalid_json", "path": rel, "line": exc.lineno,
                 "column": exc.colno, "detail": exc.msg,
                 "blocks_migration": True}]
    issues = _duplicate_keys(parsed)
    for issue in issues:
        issue["path"] = rel
        issue["blocks_migration"] = True
    return issues


def scan_manifest(data_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Inventory a data tree and report integrity issues without modifying it.

    Files are hashed incrementally. JSON documents are read only when at or under
    ``MAX_JSON_BYTES``; larger JSON is still hashed and reported as blocked —
    **except derived caches** (``prompt_index.json`` / ``nsfw_index.*``): they never
    take part in the import and stay hashed only. See ``_json_issues`` (D1).
    Symbolic links and Windows reparse points are recorded but never traversed.
    """
    root = Path(data_dir).expanduser().absolute()
    files: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    if any(_is_reparse_or_symlink(part) for part in (root, *root.parents)):
        issues.append({"code": "symlink_root", "path": ".", "blocks_migration": True})
        return {"version": 1, "dataDir": str(root), "files": files, "issues": issues,
                "excluded": excluded, "ready": False}
    if not root.exists() or not root.is_dir():
        issues.append({"code": "data_dir_unavailable", "path": str(root),
                       "blocks_migration": True})
        return {"version": 1, "dataDir": str(root), "files": files, "issues": issues,
                "excluded": excluded, "ready": False}

    def walk(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            rel = directory.relative_to(root).as_posix() or "."
            issues.append({"code": "unreadable_directory", "path": rel,
                           "detail": str(exc), "blocks_migration": True})
            return
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            if _is_reparse_or_symlink(path):
                files.append({"path": rel, "bytes": 0, "sha256": "", "kind": "unknown",
                              "status": "error"})
                issues.append({"code": "symlink_rejected", "path": rel,
                               "blocks_migration": True})
                continue
            if rel == ".cmv-writer.lock":
                excluded.append({"path": rel, "reason": "runtime process lock"})
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                files.append({"path": rel, "bytes": 0, "sha256": "", "kind": "unknown",
                              "status": "error"})
                issues.append({"code": "stat_failed", "path": rel, "detail": str(exc),
                               "blocks_migration": True})
                continue
            if stat.S_ISDIR(info.st_mode):
                classification = _classify(rel, True)
                if classification == "excluded":
                    excluded.append({"path": rel, "reason": "thumbnail cache"})
                    continue
                walk(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                files.append({"path": rel, "bytes": int(info.st_size), "sha256": "",
                              "kind": "unknown", "status": "error"})
                issues.append({"code": "unsupported_file_type", "path": rel,
                               "blocks_migration": True})
                continue
            digest = hashlib.sha256()
            # D1：classification 提前算（_classify 是纯函数，无副作用），
            # 派生缓存连 JSON 前缀都不收集 —— 省 16MB 级的内存与解析开销。
            classification = _classify(rel, False)
            json_prefix = (bytearray()
                           if path.suffix.casefold() == ".json" and classification != "derived"
                           else None)
            try:
                actual_bytes = 0
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        actual_bytes += len(chunk)
                        if json_prefix is not None and len(json_prefix) <= MAX_JSON_BYTES:
                            json_prefix.extend(chunk[:MAX_JSON_BYTES + 1 - len(json_prefix)])
            except OSError as exc:
                files.append({"path": rel, "bytes": int(info.st_size), "sha256": "",
                              "kind": classification, "status": "error"})
                issues.append({"code": "unreadable", "path": rel, "detail": str(exc),
                               "blocks_migration": True})
                continue
            try:
                after = path.stat()
                changed = (actual_bytes != info.st_size or after.st_size != info.st_size or
                           after.st_mtime_ns != info.st_mtime_ns)
            except OSError:
                changed = True
            files.append({"path": rel, "bytes": actual_bytes, "sha256": digest.hexdigest(),
                          "kind": classification, "status": "error" if changed else "ok"})
            if changed:
                issues.append({"code": "source_changed_during_scan", "path": rel,
                               "blocks_migration": True})
            issues.extend(_json_issues(rel, path.suffix, info.st_size,
                                       bytes(json_prefix) if json_prefix is not None else None,
                                       classification))

    walk(root)
    return {"version": 1, "dataDir": str(root), "files": files,
            "issues": issues, "excluded": excluded,
            "ready": not any(issue.get("blocks_migration", True) for issue in issues)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only CMV data preflight inventory")
    parser.add_argument("--data-dir", required=True, help="source data directory")
    parser.add_argument("--output", required=True, help="report JSON path outside source tree")
    args = parser.parse_args(argv)

    source = Path(args.data_dir).expanduser().absolute()
    output = Path(args.output).expanduser().absolute()
    if any(_is_reparse_or_symlink(part) for part in (output, *output.parents)):
        parser.error("--output must not traverse symbolic links or reparse points")
    try:
        common = os.path.commonpath((str(source), str(output)))
    except ValueError:
        common = ""
    if common.casefold() == str(source).casefold():
        parser.error("--output must be outside the source data tree")
    report = scan_manifest(source)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    except OSError as exc:
        parser.error(f"cannot write report: {exc}")
    print(json.dumps({"ready": report["ready"], "files": len(report["files"]),
                      "issues": len(report["issues"]), "output": str(output)},
                     ensure_ascii=False))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

