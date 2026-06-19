# -*- coding: utf-8 -*-
"""
png-icc-cleaner-v4.py

PNG の色管理メタデータ由来の libpng warning 対策用 GUI ツール。

主な対象:
    libpng warning: iCCP: known incorrect sRGB profile

[調査]
    指定フォルダ配下の PNG を再帰的に調べ、色管理チャンクを持つ PNG をログ表示。

[修復]
    調査で見つかった PNG を _backup にコピーしてから、Pillow で再保存して
    iCCP / sRGB / gAMA / cHRM などの色管理チャンクを落とす。

必要:
    pip install pillow

任意:
    pip install tkinterdnd2
    入っている場合だけ、フォルダのドラッグ＆ドロップに対応します。

注意:
    APNG はアニメーションを壊す可能性があるため修復対象から除外します。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import struct
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    PIL_IMPORT_ERROR = exc
else:
    PIL_IMPORT_ERROR = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
except Exception:  # pragma: no cover
    DND_FILES = None
    TkinterDnD = None

import tkinter as tk
from tkinter import filedialog, ttk


APP_TITLE = "PNG Profile Cleaner v4"
SETTINGS_FILE_NAME = "png-icc-cleaner-settings.json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BACKUP_DIR_NAME = "_backup"

# libpng warning: iCCP: known incorrect sRGB profile の主犯は基本的に iCCP。
# ただ、環境や画像生成元によって sRGB/gAMA/cHRM も絡むことがあるため、v2 では広めに扱えるようにする。
ICC_CHUNKS = {"iCCP"}
COLOR_CHUNKS = {"iCCP", "sRGB", "gAMA", "cHRM"}
METADATA_CHUNKS = {"tEXt", "zTXt", "iTXt", "eXIf", "tIME"}
ANIMATION_CHUNKS = {"acTL", "fcTL", "fdAT"}

SCAN_MODE_ICCP = "iCCPのみ（warning本命）"
SCAN_MODE_COLOR = "色管理チャンク全般（推奨）"
SCAN_MODE_METADATA = "メタデータ全般（広め）"


@dataclass(frozen=True)
class PngScanResult:
    path: Path
    relative_path: Path
    chunks: Tuple[str, ...]
    note: str = ""

    @property
    def is_apng(self) -> bool:
        return any(c in ANIMATION_CHUNKS for c in self.chunks)

    @property
    def color_chunks(self) -> Tuple[str, ...]:
        return tuple(c for c in self.chunks if c in COLOR_CHUNKS)

    @property
    def metadata_chunks(self) -> Tuple[str, ...]:
        return tuple(c for c in self.chunks if c in METADATA_CHUNKS)

    @property
    def important_chunks(self) -> Tuple[str, ...]:
        found: List[str] = []
        for name in ("iCCP", "sRGB", "gAMA", "cHRM", "tEXt", "zTXt", "iTXt", "eXIf", "tIME", "acTL"):
            if name in self.chunks and name not in found:
                found.append(name)
        return tuple(found)


@dataclass(frozen=True)
class RepairResult:
    path: Path
    relative_path: Path
    ok: bool
    skipped: bool = False
    backup_path: Optional[Path] = None
    before_chunks: Tuple[str, ...] = ()
    after_chunks: Tuple[str, ...] = ()
    message: str = ""


def normalize_dropped_path(raw: str) -> str:
    """tkinterdnd2 のドロップ文字列からパスを取り出す。"""
    raw = raw.strip()
    if not raw:
        return raw
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    if "} {" in raw:
        raw = raw.split("} {")[0].lstrip("{").rstrip("}")
    return raw


def is_under_backup_dir(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part == BACKUP_DIR_NAME for part in rel.parts)


def iter_png_files(root: Path, include_backup: bool = False) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() != ".png":
            continue
        if not include_backup and is_under_backup_dir(path, root):
            continue
        yield path


def read_png_chunk_types(path: Path) -> Tuple[Tuple[str, ...], str]:
    """PNG チャンク一覧を読む。画像デコードはしないので高速。"""
    chunks: List[str] = []
    try:
        with path.open("rb") as f:
            sig = f.read(8)
            if sig != PNG_SIGNATURE:
                return tuple(chunks), "PNG signature mismatch"

            while True:
                header = f.read(8)
                if not header:
                    return tuple(chunks), "missing IEND"
                if len(header) != 8:
                    return tuple(chunks), "truncated chunk header"

                length, chunk_type = struct.unpack(">I4s", header)
                if length > 512 * 1024 * 1024:
                    return tuple(chunks), f"suspicious chunk length: {length}"

                # データ本体は読み飛ばしでいいが、seek 非対応環境も考えて read する。
                data = f.read(length)
                crc = f.read(4)
                if len(data) != length or len(crc) != 4:
                    return tuple(chunks), "truncated chunk data"

                chunk_name = chunk_type.decode("ascii", errors="replace")
                chunks.append(chunk_name)
                if chunk_type == b"IEND":
                    return tuple(chunks), ""
    except Exception as exc:
        return tuple(chunks), f"read error: {exc}"


def scan_one_png(path: Path, root: Path) -> PngScanResult:
    chunks, note = read_png_chunk_types(path)
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    return PngScanResult(path=path, relative_path=rel, chunks=chunks, note=note)


def result_matches_mode(result: PngScanResult, mode: str) -> bool:
    if mode == SCAN_MODE_ICCP:
        return "iCCP" in result.chunks
    if mode == SCAN_MODE_METADATA:
        return bool(set(result.chunks) & (COLOR_CHUNKS | METADATA_CHUNKS))
    # default: color chunks
    return bool(set(result.chunks) & COLOR_CHUNKS)


def scan_folder(root: Path, mode: str, include_backup: bool) -> Tuple[int, List[PngScanResult], List[PngScanResult]]:
    all_results: List[PngScanResult] = []
    matches: List[PngScanResult] = []
    for path in iter_png_files(root, include_backup=include_backup):
        result = scan_one_png(path, root)
        all_results.append(result)
        if result_matches_mode(result, mode):
            matches.append(result)
    matches.sort(key=lambda r: str(r.relative_path).lower())
    all_results.sort(key=lambda r: str(r.relative_path).lower())
    return len(all_results), matches, all_results


def safe_copy_backup(src: Path, root: Path) -> Path:
    rel = src.relative_to(root)
    dst = root / BACKUP_DIR_NAME / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def _save_clean_png(src: Path, dst: Path, before_chunks: Sequence[str]) -> None:
    if Image is None:
        raise RuntimeError(f"Pillow import failed: {PIL_IMPORT_ERROR}")

    if any(c in ANIMATION_CHUNKS for c in before_chunks):
        raise RuntimeError("APNG is skipped to avoid breaking animation")

    with Image.open(src) as img:
        img.load()

        save_kwargs = {
            "format": "PNG",
            "optimize": False,
            "compress_level": 6,
        }

        # パレットPNGの透明度は info にしかないことがある。
        # 透明度だけは保持し、それ以外の icc_profile / exif / pnginfo は渡さない。
        if img.mode == "P" and "transparency" in img.info:
            save_kwargs["transparency"] = img.info["transparency"]

        clean = img.copy()
        clean.info.clear()
        clean.save(dst, **save_kwargs)


def repair_one_png(path: Path, root: Path) -> RepairResult:
    before_chunks, before_note = read_png_chunk_types(path)
    rel = path.relative_to(root)

    if any(c in ANIMATION_CHUNKS for c in before_chunks):
        return RepairResult(
            path=path,
            relative_path=rel,
            ok=False,
            skipped=True,
            before_chunks=before_chunks,
            message="APNG skipped",
        )

    try:
        backup_path = safe_copy_backup(path, root)
    except Exception as exc:
        return RepairResult(
            path=path,
            relative_path=rel,
            ok=False,
            before_chunks=before_chunks,
            message=f"backup failed: {exc}",
        )

    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".png.tmp",
            dir=str(path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)

        _save_clean_png(path, tmp_path, before_chunks)

        after_chunks, after_note = read_png_chunk_types(tmp_path)
        remaining = sorted(set(after_chunks) & COLOR_CHUNKS)
        if remaining:
            raise RuntimeError(f"color/profile chunk still exists after save: {', '.join(remaining)}")

        os.replace(tmp_path, path)
        tmp_path = None

        msg = "repaired"
        if before_note:
            msg += f" / before note: {before_note}"
        if after_note:
            msg += f" / after note: {after_note}"

        return RepairResult(
            path=path,
            relative_path=rel,
            ok=True,
            backup_path=backup_path,
            before_chunks=before_chunks,
            after_chunks=after_chunks,
            message=msg,
        )
    except Exception as exc:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        return RepairResult(
            path=path,
            relative_path=rel,
            ok=False,
            backup_path=backup_path,
            before_chunks=before_chunks,
            message=f"repair failed: {exc}",
        )



def settings_path() -> Path:
    try:
        return Path(__file__).resolve().with_name(SETTINGS_FILE_NAME)
    except Exception:
        return Path.cwd() / SETTINGS_FILE_NAME


def load_settings() -> dict:
    path = settings_path()
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    path = settings_path()
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        # 設定保存に失敗しても本処理は止めない。
        pass

def summarize_chunks(chunks: Sequence[str]) -> str:
    targets = []
    for name in ("iCCP", "sRGB", "gAMA", "cHRM", "tEXt", "zTXt", "iTXt", "eXIf", "tIME", "acTL"):
        if name in chunks:
            targets.append(name)
    return ", ".join(targets) if targets else "none"


class PngProfileCleanerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1060x740")
        self.root.minsize(860, 560)

        self.settings = load_settings()
        saved_folder = str(self.settings.get("folder", "")).strip()
        if not saved_folder or not Path(saved_folder).exists():
            saved_folder = str(Path.cwd())

        saved_mode = str(self.settings.get("scan_mode", SCAN_MODE_COLOR))
        if saved_mode not in (SCAN_MODE_ICCP, SCAN_MODE_COLOR, SCAN_MODE_METADATA):
            saved_mode = SCAN_MODE_COLOR

        self.folder_var = tk.StringVar(value=saved_folder)
        self.status_var = tk.StringVar(value="フォルダを選んで [調査] してください。")
        self.drop_status_var = tk.StringVar(value="")
        self.scan_mode_var = tk.StringVar(value=saved_mode)
        self.include_backup_var = tk.BooleanVar(value=bool(self.settings.get("include_backup", False)))
        self.rewrite_all_var = tk.BooleanVar(value=bool(self.settings.get("rewrite_all", False)))
        self.queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.current_results: List[PngScanResult] = []
        self.worker: Optional[threading.Thread] = None

        self._build_ui()
        self._setup_dnd()
        self.root.after(80, self._poll_queue)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        folder_frame = ttk.LabelFrame(outer, text="対象フォルダ")
        folder_frame.pack(fill=tk.X)

        entry = ttk.Entry(folder_frame, textvariable=self.folder_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 6), pady=8)
        self.folder_entry = entry

        ttk.Button(folder_frame, text="参照", command=self.choose_folder).pack(side=tk.LEFT, padx=(0, 8), pady=8)

        option_frame = ttk.LabelFrame(outer, text="調査条件")
        option_frame.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(option_frame, text="検出:").pack(side=tk.LEFT, padx=(8, 4), pady=8)
        mode_combo = ttk.Combobox(
            option_frame,
            textvariable=self.scan_mode_var,
            values=[SCAN_MODE_ICCP, SCAN_MODE_COLOR, SCAN_MODE_METADATA],
            state="readonly",
            width=34,
        )
        mode_combo.pack(side=tk.LEFT, padx=(0, 12), pady=8)
        mode_combo.bind("<<ComboboxSelected>>", lambda _event: self.save_current_settings())

        ttk.Checkbutton(
            option_frame,
            text="_backup も調査に含める",
            variable=self.include_backup_var,
            command=self.save_current_settings,
        ).pack(side=tk.LEFT, padx=(0, 8), pady=8)

        ttk.Checkbutton(
            option_frame,
            text="修復時に全PNGを再書き込み（検出無視）",
            variable=self.rewrite_all_var,
            command=self.save_current_settings,
        ).pack(side=tk.LEFT, padx=(0, 8), pady=8)

        action_frame = ttk.Frame(outer)
        action_frame.pack(fill=tk.X, pady=(10, 6))

        self.scan_button = ttk.Button(action_frame, text="調査", command=self.start_scan)
        self.scan_button.pack(side=tk.LEFT)

        self.repair_button = ttk.Button(action_frame, text="修復（上書き・バックアップあり）", command=self.start_repair)
        self.repair_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(action_frame, text="ログ消去", command=self.clear_log).pack(side=tk.LEFT, padx=(8, 0))

        self.progress = ttk.Progressbar(action_frame, mode="indeterminate", length=180)
        self.progress.pack(side=tk.RIGHT, padx=(8, 0))

        info = ttk.Label(
            outer,
            text=(
                "通常は『色管理チャンク全般』。検出0でも warning が残る場合は『全PNGを再書き込み』で検出を無視して処理。"
            ),
            foreground="#444444",
        )
        info.pack(anchor=tk.W, pady=(0, 4))

        if TkinterDnD is not None:
            dnd_text = "フォルダのドラッグ＆ドロップ対応: 有効"
        else:
            dnd_text = "フォルダのドラッグ＆ドロップ対応: 無効（使う場合は pip install tkinterdnd2）"
        ttk.Label(outer, text=dnd_text, foreground="#666666", textvariable=self.drop_status_var).pack(anchor=tk.W)
        self.drop_status_var.set(dnd_text)

        log_frame = ttk.LabelFrame(outer, text="ログ")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        self.log_text = tk.Text(log_frame, wrap=tk.NONE, undo=False, height=20)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        yscroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=yscroll.set)

        xscroll = ttk.Scrollbar(outer, orient=tk.HORIZONTAL, command=self.log_text.xview)
        xscroll.pack(fill=tk.X)
        self.log_text.configure(xscrollcommand=xscroll.set)

        status = ttk.Label(outer, textvariable=self.status_var, anchor=tk.W)
        status.pack(fill=tk.X, pady=(6, 0))

    def _setup_dnd(self) -> None:
        if TkinterDnD is None or DND_FILES is None:
            return
        try:
            self.folder_entry.drop_target_register(DND_FILES)
            self.folder_entry.dnd_bind("<<Drop>>", self._on_drop)
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event: object) -> None:
        data = getattr(event, "data", "")
        dropped = Path(normalize_dropped_path(data))
        if dropped.is_file():
            dropped = dropped.parent
        if dropped.exists() and dropped.is_dir():
            self.folder_var.set(str(dropped))
            self.save_current_settings()
            self.log(f"[D&D] 対象フォルダ: {dropped}")
        else:
            self.log(f"[D&D] フォルダではありません: {data}")

    def choose_folder(self) -> None:
        initial = self.folder_var.get().strip() or str(Path.cwd())
        folder = filedialog.askdirectory(initialdir=initial, title="PNGを調査するフォルダを選択")
        if folder:
            self.folder_var.set(folder)
            self.save_current_settings()

    def clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def save_current_settings(self) -> None:
        self.settings["folder"] = self.folder_var.get().strip()
        self.settings["scan_mode"] = self.scan_mode_var.get()
        self.settings["include_backup"] = bool(self.include_backup_var.get())
        self.settings["rewrite_all"] = bool(self.rewrite_all_var.get())
        save_settings(self.settings)

    def log(self, message: str = "") -> None:
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)

    def set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        self.scan_button.configure(state=state)
        self.repair_button.configure(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def get_root_folder(self) -> Optional[Path]:
        raw = self.folder_var.get().strip().strip('"')
        if not raw:
            self.log("[ERROR] フォルダが指定されていません。")
            return None
        root = Path(raw).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            self.log(f"[ERROR] フォルダが存在しません: {root}")
            return None
        return root

    def start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        root = self.get_root_folder()
        if root is None:
            return
        self.current_results = []
        self.save_current_settings()
        mode = self.scan_mode_var.get()
        include_backup = self.include_backup_var.get()
        self.set_busy(True)
        self.status_var.set("調査中...")
        self.log(f"\n[SCAN] {root}")
        self.log(f"[MODE] {mode} / include _backup: {include_backup}")
        self.worker = threading.Thread(target=self._scan_worker, args=(root, mode, include_backup), daemon=True)
        self.worker.start()

    def _scan_worker(self, root: Path, mode: str, include_backup: bool) -> None:
        try:
            png_count, results, all_results = scan_folder(root, mode=mode, include_backup=include_backup)
            self.queue.put(("scan_done", (root, png_count, results, all_results, mode)))
        except Exception as exc:
            self.queue.put(("error", f"scan failed: {exc}"))

    def start_repair(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        root = self.get_root_folder()
        if root is None:
            return
        self.save_current_settings()
        include_backup = self.include_backup_var.get()
        rewrite_all = self.rewrite_all_var.get()

        if rewrite_all:
            targets = list(iter_png_files(root, include_backup=include_backup))
            targets.sort(key=lambda p: str(p.relative_to(root)).lower())
            self.log("[REPAIR] 全PNG再書き込みモード: 調査結果を無視して対象フォルダ内のPNGを処理します。")
        else:
            if not self.current_results:
                self.log("[REPAIR] 修復対象がありません。先に [調査] するか、『全PNGを再書き込み』をONにしてください。")
                return
            targets = [r.path for r in self.current_results if r.path.exists()]

        if not targets:
            self.log("[REPAIR] 修復対象ファイルが見つかりません。")
            return

        self.set_busy(True)
        self.status_var.set("修復中...")
        self.log(f"\n[REPAIR] {len(targets)} file(s) / rewrite_all: {rewrite_all} / include _backup: {include_backup}")
        self.log(f"[BACKUP] {root / BACKUP_DIR_NAME}")
        self.worker = threading.Thread(target=self._repair_worker, args=(root, targets), daemon=True)
        self.worker.start()

    def _repair_worker(self, root: Path, targets: Sequence[Path]) -> None:
        repaired: List[RepairResult] = []
        for index, path in enumerate(targets, start=1):
            result = repair_one_png(path, root)
            repaired.append(result)
            self.queue.put(("repair_progress", (index, len(targets), result)))
        self.queue.put(("repair_done", (root, repaired)))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "scan_done":
                    self._handle_scan_done(payload)  # type: ignore[arg-type]
                elif kind == "repair_progress":
                    self._handle_repair_progress(payload)  # type: ignore[arg-type]
                elif kind == "repair_done":
                    self._handle_repair_done(payload)  # type: ignore[arg-type]
                elif kind == "error":
                    self.set_busy(False)
                    self.status_var.set("エラー")
                    self.log(f"[ERROR] {payload}")
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle_scan_done(self, payload: Tuple[Path, int, List[PngScanResult], List[PngScanResult], str]) -> None:
        root, png_count, results, all_results, mode = payload
        self.current_results = results
        self.set_busy(False)
        self.status_var.set(f"調査完了: PNG {png_count}件 / 候補 {len(results)}件")

        self.log(f"[SCAN DONE] PNG: {png_count} / candidates: {len(results)}")
        if not results:
            self.log("[OK] 指定条件に一致する PNG は見つかりませんでした。")
            self.log("[HINT] まだ warning が出る場合は、『修復時に全PNGを再書き込み』をONにして修復してください。")
            return

        apng_count = sum(1 for r in results if r.is_apng)
        if apng_count:
            self.log(f"[NOTE] APNG候補 {apng_count}件は、修復時にスキップします。")

        for r in results:
            chunk_info = summarize_chunks(r.chunks)
            note = f" / {r.note}" if r.note else ""
            self.log(f"[FOUND] {r.relative_path} / chunks: {chunk_info}{note}")

    def _handle_repair_progress(self, payload: Tuple[int, int, RepairResult]) -> None:
        index, total, result = payload
        if result.ok:
            before = summarize_chunks(result.before_chunks)
            after = summarize_chunks(result.after_chunks)
            backup_rel = result.backup_path if result.backup_path else ""
            self.log(
                f"[OK {index}/{total}] {result.relative_path} / "
                f"before: {before} -> after: {after} / backup: {backup_rel}"
            )
        elif result.skipped:
            self.log(f"[SKIP {index}/{total}] {result.relative_path} / {result.message}")
        else:
            self.log(f"[NG {index}/{total}] {result.relative_path} / {result.message}")

    def _handle_repair_done(self, payload: Tuple[Path, List[RepairResult]]) -> None:
        root, repaired = payload
        ok_count = sum(1 for r in repaired if r.ok)
        skip_count = sum(1 for r in repaired if r.skipped)
        ng_count = len(repaired) - ok_count - skip_count
        self.set_busy(False)
        self.status_var.set(f"修復完了: OK {ok_count}件 / SKIP {skip_count}件 / NG {ng_count}件")
        self.log(f"[REPAIR DONE] OK: {ok_count} / SKIP: {skip_count} / NG: {ng_count}")

        failed_paths = {r.path for r in repaired if not r.ok}
        self.current_results = [r for r in self.current_results if r.path in failed_paths]
        if ok_count:
            self.log("[NOTE] 修復後にもう一度 [調査] すると、残っている候補を確認できます。")
            self.log("[NOTE] それでもwarningが残る場合は、対象外フォルダのPNG、またはPNG以外の画像読み込みを疑ってください。")


def create_root() -> tk.Tk:
    if TkinterDnD is not None:
        return TkinterDnD.Tk()  # type: ignore[no-any-return]
    return tk.Tk()


def main() -> int:
    if Image is None:
        print("Pillow が見つかりません。先に `pip install pillow` を実行してください。", file=sys.stderr)
        print(f"詳細: {PIL_IMPORT_ERROR}", file=sys.stderr)
        return 1

    root = create_root()
    PngProfileCleanerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
