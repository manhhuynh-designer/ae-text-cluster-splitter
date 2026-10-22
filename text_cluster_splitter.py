#!/usr/bin/env python3
"""
Interactive Text Cluster & Mask Splitter for Adobe After Effects
- Detects words, Vietnamese diacritics, and icons on transparent PNG text layers
- Automatically recognizes Vietnamese text using cross-platform VietOCR (PyTorch Seq2Seq)
  + Polarity-Adaptive Letter Face Extraction (works identically on macOS, Windows, and Linux)
  to segment touching words and name each After Effects layer after the exact text it contains
- Computes anisotropic Voronoi seam masks that snake cleanly between lines/diacritics
  while preserving outer glow and horizontal lens flares
- Supports 3 interfaces simultaneously:
  1) Native After Effects ScriptUI Window / Dockable Panel (with live clickable image preview inside AE!)
  2) Adobe CEP Extension Panel (Window > Extensions > Text Cluster Splitter)
  3) Local HTML5 Canvas Web UI (http://127.0.0.1:8765)
"""

import os
import re
import sys
import glob
import json
import math
import shutil
import base64
import tempfile
import unicodedata
import subprocess
import time
import webbrowser
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt

# Windows: hide the console window of every helper subprocess (AfterFX.exe -r, powershell, pip)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("Đ", "D").replace("đ", "d").upper()


def resolve_mac_path(fp: str) -> str:
    """Resolves file path across macOS NFC/NFD Unicode normalization."""
    if not fp:
        return ""
    fp = unquote(fp).strip()
    if os.path.exists(fp):
        return fp
    for form in ("NFD", "NFC"):
        norm_p = unicodedata.normalize(form, fp)
        if os.path.exists(norm_p):
            return norm_p
    parent = os.path.dirname(fp)
    base_nfc = unicodedata.normalize("NFC", os.path.basename(fp))
    for form in ("NFD", "NFC"):
        p_norm = unicodedata.normalize(form, parent)
        if os.path.isdir(p_norm):
            for entry in os.listdir(p_norm):
                if unicodedata.normalize("NFC", entry) == base_nfc:
                    return os.path.join(p_norm, entry)
    return fp


# ============================================================================
# 1. AFTER EFFECTS LIVE BRIDGE HELPERS
# ============================================================================

def get_ae_path():
    """Detects After Effects executable path on macOS and Windows."""
    if sys.platform == "darwin":
        matches = glob.glob("/Applications/Adobe After Effects */Adobe After Effects *.app/Contents/MacOS/After Effects")
        if matches:
            return sorted(matches, reverse=True)[0]
        return None
    else:
        win_paths = sorted(glob.glob(r"C:\Program Files\Adobe\Adobe After Effects *\Support Files\AfterFX.exe"), reverse=True)
        return win_paths[0] if win_paths else None


def get_ae_user_script_dirs():
    """Returns possible user script/startup directories for AE on macOS and Windows."""
    dirs = []
    if sys.platform == "darwin":
        home = Path.home()
        for base in [
            home / "Library" / "Preferences" / "Adobe" / "After Effects",
            home / "Library" / "Application Support" / "Adobe" / "After Effects",
        ]:
            if base.exists():
                for ver_dir in base.glob("2*"):
                    dirs.append(ver_dir)
    else:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            ae_user_dir = Path(appdata) / "Adobe" / "After Effects"
            if ae_user_dir.exists():
                for ver_dir in ae_user_dir.glob("2*"):
                    dirs.append(ver_dir)
    return dirs


def copy_to_user_scripts(jsx_content, filename="AE_Text_Cluster_Splitter.jsx"):
    """Copies script into AE Scripts folder for direct UI execution."""
    last_target = None
    for ver_dir in get_ae_user_script_dirs():
        scripts_folder = ver_dir / "Scripts"
        try:
            scripts_folder.mkdir(parents=True, exist_ok=True)
            target_file = scripts_folder / filename
            target_file.write_text(jsx_content, encoding="utf-8")
            last_target = target_file
        except Exception:
            pass
    return last_target


def bring_app_to_front(app_name="Adobe After Effects"):
    try:
        if sys.platform == "darwin":
            ae_exe = get_ae_path()
            ae_app = "Adobe After Effects 2026"
            if ae_exe and ".app" in ae_exe:
                ae_app = ae_exe.split(".app")[0].split("/")[-1]
            subprocess.run(["osascript", "-e", f'tell application "{ae_app}" to activate'], capture_output=True)
        else:
            # Fire-and-forget: PowerShell cold start is ~0.5-1.5 s on Windows, never block the HTTP response on it
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                 f"(New-Object -ComObject WScript.Shell).AppActivate('{app_name}')"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW
            )
    except Exception:
        pass


def run_ae_jsx(jsx_code: str, timeout: float = 8.0) -> str:
    """Executes ExtendScript in the running After Effects instance and returns output string."""
    out_file = Path(tempfile.gettempdir()) / "ae_splitter_out.json"
    if out_file.exists():
        try:
            out_file.unlink()
        except Exception:
            pass

    jsx_file = Path(tempfile.gettempdir()) / "ae_splitter_cmd.jsx"
    jsx_file.write_text(jsx_code, encoding="utf-8")
    jsx_path_str = str(jsx_file).replace("\\", "/")

    if sys.platform == "darwin":
        ae_app = "Adobe After Effects 2026"
        ae_exe = get_ae_path()
        if ae_exe and ".app" in ae_exe:
            ae_app = ae_exe.split(".app")[0].split("/")[-1]
        apple_script = f'tell application "{ae_app}" to DoScript "$.evalFile(\'{jsx_path_str}\')"'
        subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
    else:
        ae_exe = get_ae_path()
        if ae_exe:
            # AfterFX.exe -r hands the script to the running AE instance and may return BEFORE it finishes
            try:
                subprocess.run([ae_exe, "-r", str(jsx_file)], capture_output=True, text=True,
                               timeout=timeout, creationflags=_NO_WINDOW)
            except subprocess.TimeoutExpired:
                pass

    # Poll for the result file (Windows hand-off is asynchronous)
    t_end = time.time() + timeout
    while True:
        if out_file.exists():
            try:
                txt = out_file.read_text(encoding="utf-8").strip()
                if txt:
                    return txt
            except Exception:
                pass
        if sys.platform == "darwin" or time.time() > t_end:
            break
        time.sleep(0.05)
    return ""


def get_active_ae_layer_info() -> dict:
    """Queries After Effects for the currently selected layer or FootageItem."""
    out_path = str(Path(tempfile.gettempdir()) / "ae_splitter_out.json").replace("\\", "/")
    jsx = f"""
    (function() {{
        var f = new File("{out_path}");
        f.encoding = "UTF-8";
        f.open("w");

        function isImageFile(fileObj) {{
            if (!fileObj) return false;
            var nm = fileObj.name.toLowerCase();
            return (nm.indexOf(".png") !== -1 || nm.indexOf(".psd") !== -1 || nm.indexOf(".jpg") !== -1 || nm.indexOf(".jpeg") !== -1 || nm.indexOf(".webp") !== -1 || nm.indexOf(".tif") !== -1 || nm.indexOf(".tiff") !== -1 || nm.indexOf(".bmp") !== -1);
        }}
        function isImageLayer(l) {{
            return (l && l.source && l.source.file && isImageFile(l.source.file));
        }}
        function isSplitLayer(l) {{
            return (l && (l.comment === "AE_TEXT_CLUSTER_SPLIT" || l.name.indexOf("[Cụm ") === 0));
        }}

        var comp = null;
        var targetLayer = null;
        var directFile = null;
        var directItem = null;
        var act = app.project.activeItem;

        if (act && (act instanceof CompItem)) {{
            comp = act;
            if (comp.selectedLayers && comp.selectedLayers.length > 0) {{
                for (var s = 0; s < comp.selectedLayers.length; s++) {{
                    var sl = comp.selectedLayers[s];
                    if (isImageLayer(sl)) {{
                        targetLayer = sl;
                        break;
                    }}
                    if (sl.source && (sl.source instanceof CompItem)) {{
                        var subComp = sl.source;
                        for (var k = 1; k <= subComp.numLayers; k++) {{
                            if (isImageLayer(subComp.layer(k))) {{
                                comp = subComp;
                                targetLayer = subComp.layer(k);
                                break;
                            }}
                        }}
                        if (targetLayer) break;
                    }}
                }}
            }}
            if (!targetLayer) {{
                for (var i = 1; i <= comp.numLayers; i++) {{
                    var l = comp.layer(i);
                    if (isImageLayer(l) && !isSplitLayer(l)) {{
                        targetLayer = l;
                        break;
                    }}
                }}
            }}
            if (!targetLayer) {{
                for (var i2 = 1; i2 <= comp.numLayers; i2++) {{
                    var l2 = comp.layer(i2);
                    if (isImageLayer(l2)) {{
                        targetLayer = l2;
                        break;
                    }}
                }}
            }}
        }}

        if (!targetLayer && act && (act instanceof FootageItem) && act.file && isImageFile(act.file)) {{
            directFile = act.file.fsName.replace(/\\\\/g, "/");
            directItem = act;
        }}

        if (!targetLayer && !directFile && app.project.selection && app.project.selection.length > 0) {{
            for (var p = 0; p < app.project.selection.length; p++) {{
                var selItem = app.project.selection[p];
                if (selItem instanceof FootageItem && selItem.file && isImageFile(selItem.file)) {{
                    directFile = selItem.file.fsName.replace(/\\\\/g, "/");
                    directItem = selItem;
                    break;
                }}
                if (selItem instanceof CompItem) {{
                    for (var j = 1; j <= selItem.numLayers; j++) {{
                        var lj = selItem.layer(j);
                        if (isImageLayer(lj) && !isSplitLayer(lj)) {{
                            comp = selItem;
                            targetLayer = lj;
                            break;
                        }}
                    }}
                    if (targetLayer) break;
                }}
            }}
        }}

        if (targetLayer) {{
            var filePath = targetLayer.source.file.fsName.replace(/\\\\/g, "/");
            var compName = comp ? comp.name.replace(/"/g, '\\\\"') : "";
            var layerName = targetLayer.name.replace(/"/g, '\\\\"');
            var srcName = (targetLayer.source && targetLayer.source.name) ? targetLayer.source.name.replace(/"/g, '\\\\"') : "";
            f.write('{{"ok": true, "compName": "' + compName + '", "layerName": "' + layerName + '", "sourceName": "' + srcName + '", "layerWidth": ' + targetLayer.width + ', "layerHeight": ' + targetLayer.height + ', "layerIndex": ' + targetLayer.index + ', "filePath": "' + filePath + '"}}');
            f.close();
            return;
        }}

        if (directFile) {{
            var cName = comp ? comp.name.replace(/"/g, '\\\\"') : "Project Panel";
            var dSrcName = (directItem && directItem.name) ? directItem.name.replace(/"/g, '\\\\"') : "";
            var dw = directItem ? directItem.width : 0;
            var dh = directItem ? directItem.height : 0;
            f.write('{{"ok": true, "compName": "' + cName + '", "layerName": "' + dSrcName + '", "sourceName": "' + dSrcName + '", "layerWidth": ' + dw + ', "layerHeight": ' + dh + ', "layerIndex": 0, "filePath": "' + directFile + '"}}');
            f.close();
            return;
        }}

        f.write('{{"ok": false, "error": "Hãy click chọn 1 Layer PNG/PSD trên Timeline hoặc trong Project Panel!"}}');
        f.close();
    }})();
    """
    raw = run_ae_jsx(jsx)
    if not raw:
        return {"ok": False, "error": "Không kết nối được với After Effects."}
    try:
        data = json.loads(raw)
        if data.get("ok") and data.get("filePath"):
            data["filePath"] = resolve_mac_path(data["filePath"])
        return data
    except Exception as e:
        return {"ok": False, "error": f"Lỗi đọc dữ liệu từ AE: {e}"}


# ============================================================================
# 2. CROSS-PLATFORM VIETOCR & SMART WORD / DIACRITIC / VORONOI SEAM ENGINE
# ============================================================================

BGR_PALETTE = [
    (248, 189, 56),   # #38bdf8 Sky Blue
    (128, 222, 74),   # #4ade80 Green
    (60, 146, 251),   # #fb923c Orange
    (182, 114, 244),  # #f472b6 Pink
    (250, 139, 167),  # #a78bfa Purple
    (21, 204, 250),   # #facc15 Yellow
    (191, 212, 45),   # #2dd4bf Teal
    (113, 113, 248),  # #f87171 Red
]

_VIETOCR_PREDICTOR = None

VN_WORD_FIXES = {
    "MỆ": "MẸ", "ME": "MẸ", "BIM": "BỈM", "BÌM": "BỈM",
    "CHONG": "CHÓNG", "CHỐNG": "CHÓNG", "KIP": "KỊP",
    "ĐÔ": "ĐỒ", "ĐỐ": "ĐỒ", "ĐỔ": "ĐỒ", "ĐỖ": "ĐỒ",
    "VỚ": "VỚI", "GIẶI": "GIẶT", "GIAT": "GIẶT", "NHẠNH": "NHANH",
    "AO": "ÁO", "ĐE": "ĐỂ", "ĐỀ": "ĐỂ", "ĐÊ": "ĐỂ", "CO": "CÓ",
    "PHỐT": "PHÚT", "PHOT": "PHÚT", "PHUT": "PHÚT", "CUNG": "CỨNG",
    "ĐÀI": "DÀI", "TỐO": "TỐC", "TOO": "TỐC", "TOC": "TỐC",
    "HOI": "HÔI", "MOC": "MỐC", "MOCI": "MỐC!", "MỐCI": "MỐC!", "LAU": "LÂU", "SACH": "SẠCH",
    "KHU": "KHỬ", "MUI": "MÙI", "VUOT": "VƯỢT", "TROI": "TRỘI", "TRỘN": "TRỘI!", "CHI": "CHỈ",
    "VET": "VẾT", "BAN": "BẨN", "BẦN": "BẨN", "DAU": "ĐẦU",
    "BOT": "BỌT", "CUC": "CỰC", "LIÊN": "LIỀN",
    "CHOP": "CHỚP", "NHOANG": "NHOÁNG", "NUOC": "NƯỚC",
    "MANH": "MẠNH", "LAN": "LẦN", "XA": "XẢ", "DANH": "ĐÁNH",
    "TUC": "TỨC", "THI": "THÌ", "CONG": "CÔNG", "NGHE": "NGHỆ",
    "DOC": "ĐỘC", "QUYEN": "QUYỀN", "MOT": "MỘT", "MA": "MÀ", "VA": "VÀ",
    "SIỀU": "SIÊU", "SIEU": "SIÊU", "ĐẮM": "ĐẪM",
    "GIAY": "GIẤY", "QUY": "QUỲ", "TIM": "TÍM", "DIU": "DỊU", "NHE": "NHẸ",
    "CHAC": "CHẮC", "LANH": "LÀNH", "TINH": "TÍNH", "TIN": "TÍN", "HOC": "HỌC",
    "LÊU": "YÊU?", "LEU": "YÊU?", "CẬN": "CẶN", "CAN": "CẦN", "DỊNH": "DÍNH",
}

VIETOCR_VOCAB_CHARS = (
    "aAàÀảẢãÃáÁạẠăĂằẰẳẲẵẴắẮặẶâÂầẦẩẨẫẪấẤậẬbBcCdDđĐeEèÈẻẺẽẼéÉẹẸêÊềỀểỂễỄếẾệỆ"
    "fFgGhHiIìÌỉỈĩĨíÍịỊjJkKlLmMnNoOòÒỏỎõÕóÓọỌôÔồỒổỔỗỖốỐộỘơƠờỜởỞỡỠớỚợỢ"
    "pPqQrRsStTuUùÙủỦũŨúÚụỤưƯừỪửỬữỮứỨựỰvVwWxXyYỳỲỷỶỹỸýÝỵỴzZ"
    "0123456789!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ "
)
VIETOCR_I2C = {i + 4: c for i, c in enumerate(VIETOCR_VOCAB_CHARS)}


class VietOCROnnxPredictor:
    """Lightweight cross-platform ONNXRuntime VietOCR (vgg_seq2seq) predictor (zero PyTorch dependency)."""

    def __init__(self, enc_path: str, dec_path: str):
        import onnxruntime as ort
        self.enc_sess = ort.InferenceSession(enc_path, providers=["CPUExecutionProvider"])
        self.dec_sess = ort.InferenceSession(dec_path, providers=["CPUExecutionProvider"])

    def predict_with_conf(self, pil_img) -> tuple:
        from PIL import Image
        img = pil_img.convert("RGB")
        w, h = img.size
        new_w = math.ceil((32.0 * w / float(max(1, h))) / 10.0) * 10
        new_w = max(32, min(512, int(new_w)))
        img = img.resize((new_w, 32), Image.LANCZOS)
        inp = (np.asarray(img).transpose(2, 0, 1).astype(np.float32) / 255.0)[np.newaxis, ...]
        enc_out, hidden = self.enc_sess.run(None, {"img": inp})
        tgt = np.array([1], dtype=np.int64)
        chars = []
        probs = []
        for _ in range(64):
            logits, hidden = self.dec_sess.run(None, {"tgt": tgt, "hidden": hidden, "encoder_outputs": enc_out})
            l = logits[0]
            exp_l = np.exp(l - np.max(l))
            p = exp_l / np.sum(exp_l)
            tok = int(np.argmax(l))
            if tok == 2:
                break
            if tok in VIETOCR_I2C:
                chars.append(VIETOCR_I2C[tok])
                probs.append(float(p[tok]))
            tgt = np.array([tok], dtype=np.int64)
        return "".join(chars), (float(np.mean(probs)) if probs else 0.0)

    def predict(self, pil_img) -> str:
        txt, _ = self.predict_with_conf(pil_img)
        return txt

    def predict_batch(self, pil_images: list) -> list:
        return [self.predict(im) for im in pil_images]


HF_MODEL_BASE_URL = "https://huggingface.co/manhhuynhsd/ae-text-cluster-splitter-onnx/resolve/main"


def ensure_onnx_models() -> tuple:
    """Ensures vietocr_encoder.onnx and vietocr_decoder.onnx exist in models/, downloading from Hugging Face if missing."""
    import urllib.request
    model_dir = Path(__file__).resolve().parent / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    enc_path = model_dir / "vietocr_encoder.onnx"
    dec_path = model_dir / "vietocr_decoder.onnx"

    for fname, fpath, min_bytes in [
        ("vietocr_encoder.onnx", enc_path, 10_000_000),
        ("vietocr_decoder.onnx", dec_path, 1_000_000),
    ]:
        if not fpath.exists() or fpath.stat().st_size < min_bytes:
            url = f"{HF_MODEL_BASE_URL}/{fname}"
            print(f"⏳ Đang tải model ONNX từ Hugging Face: {url} ...", flush=True)
            tmp_dl = fpath.with_suffix(".onnx.tmp")
            try:
                urllib.request.urlretrieve(url, str(tmp_dl))
                tmp_dl.replace(fpath)
                print(f"✅ Đã tải xong {fname} ({fpath.stat().st_size / (1024*1024):.1f} MB)", flush=True)
            except Exception as e:
                print(f"⚠️ Lỗi tải {fname} từ Hugging Face: {e}", flush=True)
                if tmp_dl.exists():
                    try:
                        tmp_dl.unlink()
                    except Exception:
                        pass
    return enc_path, dec_path


def get_vietocr_predictor():
    """Lazily initializes and returns the lightweight ONNXRuntime VietOCR predictor."""
    global _VIETOCR_PREDICTOR
    if _VIETOCR_PREDICTOR is not None:
        return _VIETOCR_PREDICTOR

    enc_path, dec_path = ensure_onnx_models()
    if not enc_path.exists() or not dec_path.exists():
        return None

    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "onnxruntime"], capture_output=True, timeout=60)
        except Exception:
            return None

    try:
        _VIETOCR_PREDICTOR = VietOCROnnxPredictor(str(enc_path), str(dec_path))
        return _VIETOCR_PREDICTOR
    except Exception:
        return None


def clean_ocr_line_tokens(raw_line: str, hint_words: list) -> list:
    """Normalizes raw VietOCR line output into clean Vietnamese word tokens."""
    s = unicodedata.normalize("NFC", raw_line).upper().strip()
    s = re.sub(r"[:;]\s*[A-Z0-9]*$", "!", s)
    if "BIM" in strip_accents(s):
        s = re.sub(r"\s+\d+$", "?", s)
    # Collapse repeated decorative tail characters (e.g. NHOANGGGGI -> NHOÁNG!)
    s = re.sub(r"([A-ZÀ-Ỹ])\1{2,}[I1!]*$", r"\1!", s)
    # Convert trailing digit 3/7 attached to a word at the end of a question line (e.g. XA3 -> XA?)
    s = re.sub(r"([A-ZÀ-Ỹ]{2,})[37]$", r"\1?", s)
    s = re.sub(r"(\d+)([A-ZÀ-Ỹ]+)", r"\1 \2", s)
    s = re.sub(r"(OMO)([A-ZÀ-Ỹ]+)", r"\1 \2", s)
    s = re.sub(r"([A-ZÀ-Ỹ]+)(OMO)", r"\1 \2", s)
    s = re.sub(r"(C[ẶẬA]N)(B[ỌO]T)", r"\1 \2", s)
    tokens = [t for t in re.split(r"\s+", s) if t]
    out_tokens = []
    for t in tokens:
        punct = "".join(c for c in t if c in "!?.,")
        core_w = re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", t)
        base = strip_accents(core_w)
        if not base:
            continue
        if core_w.endswith("I") and len(base) >= 3 and base[:-1] in ("CHAM", "LO", "THOM", "DONG"):
            core_w = core_w[:-1]
            base = base[:-1]
            punct = "!"

        # Check if token is a concatenation of 2 or 3 consecutive hint words
        matched_multi = None
        for n_join in (3, 2):
            for i in range(len(hint_words) - n_join + 1):
                seq = hint_words[i:i + n_join]
                joined = "".join(strip_accents(w) for w in seq)
                if joined == base:
                    if len(seq) == 1 and core_w != base and seq[0] == strip_accents(seq[0]):
                        matched_multi = [core_w + punct]
                    else:
                        matched_multi = [VN_WORD_FIXES.get(w, w) for w in seq]
                        matched_multi[-1] += punct
                    break
            if matched_multi:
                break
        if matched_multi:
            out_tokens.extend(matched_multi)
            continue

        if core_w in VN_WORD_FIXES:
            core_w = VN_WORD_FIXES[core_w]
            base = strip_accents(core_w)
        for hw in hint_words:
            hw_base = strip_accents(hw)
            if hw_base == base:
                # Only overwrite core_w with hw if hw actually has accents (don't strip VietOCR's accents!)
                if hw != hw_base:
                    core_w = hw
                break
            elif len(base) >= 3 and len(hw_base) == len(base) and hw_base[0] == base[0] and sum(c1 != c2 for c1, c2 in zip(hw_base, base)) <= 1:
                core_w = VN_WORD_FIXES.get(hw, hw)
                break
        if core_w in VN_WORD_FIXES:
            core_w = VN_WORD_FIXES[core_w]
        out_tokens.append(core_w + punct)

    # Fold trailing 3D exclamation mark misread as a single-letter token ('V', 'I', 'Y', '1', 'L') into the preceding word
    if len(out_tokens) >= 2 and out_tokens[-1] in ("V", "I", "Y", "1", "L") and strip_accents(out_tokens[-2]) in ("TAT", "LO", "CHAM", "DONG"):
        out_tokens.pop()
        if not out_tokens[-1].endswith("!"):
            out_tokens[-1] += "!"

    # Second pass: check if any 2 adjacent OCR tokens have a shifted boundary vs 2 consecutive hint words (e.g. "DỄH II" vs "DE HOI")
    if len(out_tokens) >= 2 and len(hint_words) >= 2:
        k = 0
        while k < len(out_tokens) - 1:
            t1, t2 = out_tokens[k], out_tokens[k + 1]
            p2 = "".join(c for c in t2 if c in "!?.,")
            b12 = strip_accents(re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", t1) + re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", t2))
            replaced = False
            for i in range(len(hint_words) - 1):
                hw1, hw2 = hint_words[i], hint_words[i + 1]
                hb12 = strip_accents(hw1) + strip_accents(hw2)
                if len(b12) >= 4 and len(b12) == len(hb12) and b12[0] == hb12[0] and sum(c1 != c2 for c1, c2 in zip(b12, hb12)) <= 1:
                    out_tokens[k] = VN_WORD_FIXES.get(hw1, hw1)
                    out_tokens[k + 1] = VN_WORD_FIXES.get(hw2, hw2) + p2
                    replaced = True
                    break
            k += 2 if replaced else 1

    # Third pass: contextual Vietnamese bigram disambiguation
    bigram_fixes = {
        ("CÙNG", "ĐẦU"): ("CỨNG", "ĐẦU"),
        ("DE", "LÂU"): ("ĐỂ", "LÂU"),
        ("DE", "HÔI"): ("DỄ", "HÔI"),
        ("BỌT", "LẦN"): ("MỘT", "LẦN"),
        ("CHẲNG", "ĐỢI"): ("CHÀNG", "ĐỢI"),
        ("CÓ", "CHA"): ("CÓ", "CHẮC"),
        ("DI", "HỌC"): ("ĐI", "HỌC"),
        ("DI", "HOC"): ("ĐI", "HỌC"),
        ("TỰ", "TÍN"): ("TỰ", "TIN"),
        ("THỨ", "NGHIỆM"): ("THỬ", "NGHIỆM"),
        ("Ó", "CHẮC"): ("CÓ", "CHẮC"),
        ("BÊ", "YÊU"): ("BÉ", "YÊU"),
        ("HỌC", "VE"): ("HỌC", "VỀ"),
        ("VƯỢT", "TRỌI"): ("VƯỢT", "TRỘI"),
    }
    for k in range(len(out_tokens) - 1):
        w1 = re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", out_tokens[k])
        w2 = re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", out_tokens[k + 1])
        if (w1, w2) in bigram_fixes:
            nw1, nw2 = bigram_fixes[(w1, w2)]
            out_tokens[k] = out_tokens[k].replace(w1, nw1, 1)
            out_tokens[k + 1] = out_tokens[k + 1].replace(w2, nw2, 1)

    return out_tokens


def extract_psd_layer_bgra(file_path: str, layer_name: str = "", source_name: str = "",
                           layer_w: int = 0, layer_h: int = 0) -> tuple:
    """Extracts the target RGBA sub-layer from a .psd file using psd-tools and returns (img_bgra, preview_b64, matched_src_name)."""
    try:
        from psd_tools import PSDImage
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "psd-tools"], capture_output=True, timeout=60)
        from psd_tools import PSDImage
    from PIL import Image

    psd = PSDImage.open(file_path)
    sub_name = ""
    if source_name and "/" in source_name:
        sub_name = unicodedata.normalize("NFC", source_name.split("/")[0].strip())
    elif layer_name:
        sub_name = unicodedata.normalize("NFC", layer_name.strip())

    target = None
    all_layers = list(psd.descendants())

    # 1. Exact name + exact AE layer dimensions match (Retain Layer Sizes)
    if sub_name and layer_w > 0 and layer_h > 0:
        for lyr in all_layers:
            l_nm = unicodedata.normalize("NFC", (lyr.name or "").strip())
            if l_nm == sub_name and lyr.size == (layer_w, layer_h) and not lyr.is_group():
                target = lyr
                break
        if target is None:
            for lyr in all_layers:
                l_nm = unicodedata.normalize("NFC", (lyr.name or "").strip())
                if l_nm == sub_name and lyr.size == (layer_w, layer_h):
                    target = lyr
                    break

    # 2. Exact AE layer dimensions match if layer was renamed in AE timeline
    if target is None and layer_w > 0 and layer_h > 0 and (layer_w, layer_h) != psd.size:
        for lyr in all_layers:
            if lyr.size == (layer_w, layer_h) and not lyr.is_group():
                target = lyr
                break
        if target is None:
            for lyr in all_layers:
                if lyr.size == (layer_w, layer_h):
                    target = lyr
                    break

    # 3. Name match (e.g. imported as Document Size or selected from Project Panel)
    if target is None and sub_name:
        for lyr in all_layers:
            l_nm = unicodedata.normalize("NFC", (lyr.name or "").strip())
            if l_nm == sub_name and not lyr.is_group() and lyr.size[0] > 0 and lyr.size[1] > 0:
                target = lyr
                break
        if target is None:
            for lyr in all_layers:
                l_nm = unicodedata.normalize("NFC", (lyr.name or "").strip())
                if l_nm == sub_name and lyr.size[0] > 0 and lyr.size[1] > 0:
                    target = lyr
                    break

    # 4. Fallback when opening a .psd directly without layer context: pick "COPY" / "TEXT" layer
    if target is None:
        for lyr in all_layers:
            l_nm = unicodedata.normalize("NFC", (lyr.name or "").strip()).upper()
            if l_nm in ("COPY", "TEXT", "HEADLINE", "TITLE") and not lyr.is_group() and lyr.size[0] > 80:
                target = lyr
                break

    if target is not None:
        pil_img = (target.topil() if not target.is_group() else target.composite()).convert("RGBA")
        # If AE layer has full PSD document dimensions (imported as Composition without Retain Layer Sizes), place at layer offset
        if layer_w == psd.width and layer_h == psd.height and pil_img.size != psd.size:
            canvas = Image.new("RGBA", psd.size, (0, 0, 0, 0))
            canvas.paste(pil_img, (int(target.left), int(target.top)), pil_img)
            pil_img = canvas
        matched_src = source_name or f"{target.name}/{os.path.basename(file_path)}"
    else:
        pil_img = psd.composite().convert("RGBA")
        matched_src = source_name or os.path.basename(file_path)

    rgba_arr = np.asarray(pil_img)
    img_bgra = cv2.cvtColor(rgba_arr, cv2.COLOR_RGBA2BGRA)
    if np.percentile(img_bgra[:, :, 3], 5) >= 250:
        img_bgra[:, :, 3] = synthesize_alpha_from_bgr(img_bgra[:, :, :3])
    ok_enc, png_buf = cv2.imencode(".png", img_bgra)
    b64_str = "data:image/png;base64," + base64.b64encode(png_buf.tobytes()).decode("ascii") if ok_enc else ""
    return img_bgra, b64_str, matched_src


def synthesize_alpha_from_bgr(img_bgr: np.ndarray) -> np.ndarray:
    """Synthesizes an alpha channel (255 on solid text/3D stroke, 1..220 on soft glow, 0 on background)
    for 3-channel RGB images (.jpg/.jpeg/.bmp) or flattened opaque layers regardless of background color."""
    h, w = img_bgr.shape[:2]
    c_sz = max(4, min(h, w) // 25)
    corners = np.concatenate([
        img_bgr[:c_sz, :c_sz].reshape(-1, 3),
        img_bgr[:c_sz, w - c_sz:].reshape(-1, 3),
        img_bgr[h - c_sz:, :c_sz].reshape(-1, 3),
        img_bgr[h - c_sz:, w - c_sz:].reshape(-1, 3),
    ], axis=0).astype(np.float32)
    bg_med = np.median(corners, axis=0)

    img_f = img_bgr.astype(np.float32)
    diff_max = np.max(np.abs(img_f - bg_med.reshape(1, 1, 3)), axis=2)

    corner_diff = np.max(np.abs(corners - bg_med.reshape(1, 3)), axis=1)
    bg_noise = float(np.percentile(corner_diff, 85)) if corner_diff.size > 0 else 4.0
    floor_val = max(6.0, min(20.0, bg_noise * 1.6))

    fg = np.maximum(0.0, diff_max - floor_val)
    fg_active = fg[fg > 5.0]
    core_ref = float(np.clip(np.percentile(fg_active, 60), 40.0, 160.0)) if fg_active.size > 0 else 80.0

    # Soft outer glow gets alpha 1..220 so Voronoi masks preserve the glow without merging into solid core
    alpha = np.clip((fg / core_ref) * 220.0, 0.0, 220.0).astype(np.uint8)
    alpha[fg < 3.0] = 0

    # Solid core (255) from sharp 3x3 morphological edges + interior hole filling
    k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    grad3 = np.max(cv2.dilate(img_bgr, k3).astype(np.int16) - cv2.erode(img_bgr, k3).astype(np.int16), axis=2)
    sharp_ring = ((grad3 >= 26) & (diff_max >= max(22.0, floor_val * 1.8))).astype(np.uint8)
    sharp_ring = cv2.morphologyEx(sharp_ring, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))

    inv_bg = (sharp_ring == 0).astype(np.uint8)
    num_bg, lbl_bg, stats_bg, _ = cv2.connectedComponentsWithStats(inv_bg)
    border_labels = (
        set(np.unique(lbl_bg[0, :])) | set(np.unique(lbl_bg[-1, :])) |
        set(np.unique(lbl_bg[:, 0])) | set(np.unique(lbl_bg[:, -1]))
    )
    solid_core = sharp_ring.copy()
    max_hole_area = int(h * w * 0.25)
    for lbl in range(1, num_bg):
        if lbl not in border_labels and stats_bg[lbl, cv2.CC_STAT_AREA] < max_hole_area:
            solid_core[lbl_bg == lbl] = 1

    solid_core = cv2.morphologyEx(solid_core, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    alpha[solid_core > 0] = 255
    return alpha


class ImageClusterState:
    def __init__(self):
        self.file_path = None
        self.layer_name = ""
        self.source_name = ""
        self.layer_w = 0
        self.layer_h = 0
        self.img_bgra = None
        self.h = 0
        self.w = 0
        self.alpha = None
        self.face_bin = None
        self.core_mask = None
        self.label_map = None
        self.blocks = []
        self.selected_block_id = None
        self.custom_cluster_names = {}
        self.history = []
        self.preview_b64 = ""
        self.outer_padding = 85
        self.smoothness = 3.0
        self.h_anisotropy = 0.35
        self.preview_scale = 1.0
        self.preview_off_x = 0
        self.preview_off_y = 0
        self.preview_counter = 0
        self.preview_png_path = str(Path(tempfile.gettempdir()) / "ae_splitter_ui_preview_0.png")

    def push_history(self):
        if self.label_map is None:
            return
        snap = (
            [dict(b, polys=list(b.get("polys", []))) for b in self.blocks],
            self.label_map.copy(),
            self.selected_block_id,
            dict(self.custom_cluster_names)
        )
        self.history.append(snap)
        if len(self.history) > 15:
            self.history.pop(0)

    def undo(self) -> bool:
        if not self.history:
            return False
        blocks, lmap, sel_id, cnames = self.history.pop()
        self.blocks = blocks
        self.label_map = lmap
        self.selected_block_id = sel_id
        self.custom_cluster_names = cnames
        return True

    def load_image(self, file_path: str, split_pinch: bool = True,
                   layer_name: str = "", source_name: str = "",
                   layer_w: int = 0, layer_h: int = 0):
        file_path = resolve_mac_path(file_path)
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        self.history = []
        self.selected_block_id = None
        self.custom_cluster_names = {}

        if file_path.lower().endswith(".psd"):
            img, b64_str, matched_src = extract_psd_layer_bgra(
                file_path, layer_name=layer_name, source_name=source_name,
                layer_w=int(layer_w or 0), layer_h=int(layer_h or 0)
            )
            self.file_path = file_path
            self.layer_name = layer_name or (matched_src.split("/")[0] if "/" in matched_src else "")
            self.source_name = matched_src
            self.layer_w = int(layer_w or img.shape[1])
            self.layer_h = int(layer_h or img.shape[0])
            self.img_bgra = img
            self.h, self.w = img.shape[:2]
            self.alpha = img[:, :, 3]
            self.preview_b64 = b64_str
        else:
            img = cv2.imread(file_path, cv2.IMREAD_UNCHANGED)
            if img is None:
                from PIL import Image
                pil_in = Image.open(file_path).convert("RGBA")
                img = cv2.cvtColor(np.asarray(pil_in), cv2.COLOR_RGBA2BGRA)
            if img.ndim == 2:
                img_bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                syn_a = synthesize_alpha_from_bgr(img_bgr)
                img = np.dstack([img_bgr, syn_a])
            elif img.shape[2] == 3:
                syn_a = synthesize_alpha_from_bgr(img)
                img = np.dstack([img, syn_a])
            elif img.shape[2] == 4 and np.percentile(img[:, :, 3], 5) >= 250:
                img = img.copy()
                img[:, :, 3] = synthesize_alpha_from_bgr(img[:, :, :3])

            self.file_path = file_path
            self.layer_name = layer_name
            self.source_name = source_name
            self.layer_w = int(layer_w or img.shape[1])
            self.layer_h = int(layer_h or img.shape[0])
            self.img_bgra = img
            self.h, self.w = img.shape[:2]
            self.alpha = img[:, :, 3]

            ok_enc, png_buf = cv2.imencode(".png", self.img_bgra)
            if ok_enc:
                self.preview_b64 = "data:image/png;base64," + base64.b64encode(png_buf.tobytes()).decode("ascii")
            else:
                with open(file_path, "rb") as f:
                    self.preview_b64 = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")

        self._detect_blocks_scaled(split_pinch=split_pinch)

    def _detect_blocks_scaled(self, split_pinch: bool = True, target_text_h: float = 140.0):
        """Runs `_detect_blocks` at a normalized working resolution. Detection thresholds are tuned for letters
        roughly 80-300 px tall, so huge 4K/8K/11K layers are downscaled to ~1600 px and tiny layers (<80 px letters)
        are upscaled, then the label map is mapped back to native resolution so masks align pixel-perfectly."""
        max_dim = max(self.w, self.h)
        s = 1.0
        if max_dim > 2400:
            s = 1600.0 / float(max_dim)
        else:
            self._detect_blocks(split_pinch=split_pinch)
            txt_h = [b["h"] for b in self.blocks if not b.get("is_icon")]
            med_h = float(np.median(txt_h)) if txt_h else 0.0
            if max_dim < 32 or max_dim >= 1600 or (med_h >= 80.0):
                return
            s = target_text_h / med_h if med_h > 0 else 2.5
            s = max(1.5, min(4.0, s, 2400.0 / max_dim))
            if s < 1.2:
                return

        orig = (self.img_bgra, self.alpha, self.w, self.h)
        interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
        up = cv2.resize(self.img_bgra, (int(round(self.w * s)), int(round(self.h * s))), interpolation=interp)
        self.img_bgra = up
        self.alpha = up[:, :, 3]
        self.h, self.w = up.shape[:2]
        try:
            self._detect_blocks(split_pinch=split_pinch)
        finally:
            up_label = self.label_map
            up_face = self.face_bin
            up_core = self.core_mask
            self.img_bgra, self.alpha, self.w, self.h = orig

        def _down(m):
            return cv2.resize(m.astype(np.int32 if m.dtype != np.uint8 else np.uint8), (self.w, self.h), interpolation=cv2.INTER_NEAREST) if m is not None else None

        self.label_map = cv2.resize(up_label.astype(np.float32), (self.w, self.h), interpolation=cv2.INTER_NEAREST).astype(np.int32)
        if s < 1.0:
            nat_thresh = max(180, int(self.alpha.max()) - 25) if self.alpha.size > 0 else 200
            self.label_map[self.alpha < nat_thresh] = 0
        self.face_bin = _down(up_face)
        self.core_mask = _down(up_core)
        new_blocks = []
        min_cnt_area = max(8, int(round(8.0 / (s * s))))
        approx_eps = max(1.5, round(1.5 / s, 1))
        for b in self.blocks:
            mask_b = self.label_map == b["id"]
            ys, xs = np.where(mask_b)
            if len(xs) == 0:
                continue
            bx, by = int(xs.min()), int(ys.min())
            cnts, _ = cv2.findContours(mask_b.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            polys = []
            for cnt in cnts:
                if cv2.contourArea(cnt) >= min_cnt_area:
                    approx = cv2.approxPolyDP(cnt, approx_eps, True)
                    polys.append([[int(p[0][0]), int(p[0][1])] for p in approx])
            nb = dict(b)
            nb.update({
                "x": bx, "y": by, "w": int(xs.max() - bx + 1), "h": int(ys.max() - by + 1),
                "area": int(len(xs)),
                "cx": round(float(xs.mean()), 1), "cy": round(float(ys.mean()), 1),
                "polys": polys
            })
            new_blocks.append(nb)
        self.blocks = new_blocks

    def _partition_mask_by_cuts(self, mask: np.ndarray, face_bin: np.ndarray, cuts_abs: list) -> list:
        """Splits `mask` into len(cuts_abs)+1 pieces at vertical x-cuts, but keeps each letter-face connected
        component whole (assigned to the piece holding >= 70% of its pixels) so slanted / overhanging glyphs
        (e.g. italic 'Y', splash logo arms, 'Ặ' dots) are never sliced by a straight vertical line."""
        n_pieces = len(cuts_abs) + 1
        ys, xs = np.where(mask)
        if len(xs) == 0:
            return [np.zeros_like(mask) for _ in range(n_pieces)]
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        m_c = mask[y0:y1, x0:x1]
        cols = np.arange(x0, x1)
        piece_col = np.searchsorted(np.asarray(sorted(cuts_abs)), cols, side="right")
        piece = np.where(m_c, piece_col[None, :], -1).astype(np.int32)

        fm = (m_c & (face_bin[y0:y1, x0:x1] > 0)).astype(np.uint8)
        if np.any(fm):
            ncomp, lab = cv2.connectedComponents(fm, connectivity=8)
            for ci in range(1, ncomp):
                cm = lab == ci
                cnt = np.bincount(piece[cm], minlength=n_pieces)
                tot = float(cnt.sum())
                if tot <= 0:
                    continue
                best = int(np.argmax(cnt))
                if cnt[best] / tot >= 0.70:
                    piece[cm] = best
            # Non-face fringe pixels (3D extrusion / dilation) follow their nearest letter-face pixel
            _, inds = distance_transform_edt(fm == 0, return_indices=True)
            near_piece = piece[inds[0], inds[1]]
            fringe = m_c & (fm == 0)
            piece[fringe] = near_piece[fringe]

        out = []
        for p in range(n_pieces):
            full = np.zeros_like(mask)
            full[y0:y1, x0:x1] = (piece == p)
            out.append(full)
        return out

    def _align_line_blobs_to_words(self, ln: list, words_li: list, face_bin: np.ndarray, core: np.ndarray) -> list:
        """Aligns M horizontal character/word blobs in `ln` to K recognized OCR words `words_li`
        using Dynamic Programming (for M > K) and Smooth Valley Profile Splitting (for M < K)."""
        if not ln:
            return []
        ln.sort(key=lambda c: c["x"])
        K = max(1, len(words_li))

        # Case 1: More blobs than words (M > K) -> e.g. individual 3D letters grouped into K words via DP
        if len(ln) > K:
            M = len(ln)
            if K == 1:
                merged_mask = np.zeros_like(ln[0]["mask"])
                for b in ln:
                    merged_mask |= b["mask"]
                ys, xs = np.where(merged_mask)
                return [{
                    "mask": merged_mask,
                    "x": int(xs.min()), "y": int(ys.min()),
                    "w": int(xs.max() - xs.min() + 1), "h": int(ys.max() - ys.min() + 1),
                    "cx": float(xs.mean()), "cy": float(ys.mean()),
                    "text": words_li[0], "is_icon": False
                }]

            gaps = [max(-10.0, float(ln[j + 1]["x"] - (ln[j]["x"] + ln[j]["w"]))) for j in range(M - 1)]
            max_gap = max(max(gaps), 1.0)
            blob_widths = [float(b["w"]) for b in ln]
            tot_w = max(sum(blob_widths), 1.0)
            char_lens = [max(1.0, float(len(re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", w)) or len(w))) for w in words_li]
            tot_chars = max(sum(char_lens), 1.0)

            INF = 1e9
            dp = [[INF] * (M + 1) for _ in range(K + 1)]
            parent_ptr = [[0] * (M + 1) for _ in range(K + 1)]
            dp[0][0] = 0.0

            for k in range(1, K + 1):
                c_frac = char_lens[k - 1] / tot_chars
                for m in range(k, M - (K - k) + 1):
                    for prev_m in range(k - 1, m):
                        grp_w_frac = sum(blob_widths[prev_m:m]) / tot_w
                        grp_n_frac = float(m - prev_m) / float(M)
                        gap_reward = (gaps[m - 1] / max_gap) if m < M else 0.0
                        cost = (
                            2.6 * abs(grp_w_frac - c_frac) +
                            1.4 * abs(grp_n_frac - c_frac) -
                            1.35 * gap_reward
                        )
                        cand = dp[k - 1][prev_m] + cost
                        if cand < dp[k][m]:
                            dp[k][m] = cand
                            parent_ptr[k][m] = prev_m

            groups = []
            curr_m = M
            for k in range(K, 0, -1):
                prev_m = parent_ptr[k][curr_m]
                groups.append((prev_m, curr_m, words_li[k - 1]))
                curr_m = prev_m
            groups.reverse()

            merged_ln = []
            for a_idx, b_idx, w_txt in groups:
                sub_blobs = ln[a_idx:b_idx]
                merged_mask = np.zeros_like(sub_blobs[0]["mask"])
                for sb in sub_blobs:
                    merged_mask |= sb["mask"]
                ys, xs = np.where(merged_mask)
                merged_ln.append({
                    "mask": merged_mask,
                    "x": int(xs.min()), "y": int(ys.min()),
                    "w": int(xs.max() - xs.min() + 1), "h": int(ys.max() - ys.min() + 1),
                    "cx": float(xs.mean()), "cy": float(ys.mean()),
                    "text": w_txt, "is_icon": False
                })
            return merged_ln

        # Case 2: Fewer blobs than words (M < K) -> Split wide fused 3D blocks at true inter-word valleys
        if len(ln) < K:
            counts = [1] * len(ln)
            rem = K - len(ln)
            while rem > 0:
                best_j = max(range(len(ln)), key=lambda j: ln[j]["w"] / float(counts[j]))
                counts[best_j] += 1
                rem -= 1
            new_ln = []
            w_cursor = 0
            for j, s in enumerate(ln):
                cnt = counts[j]
                sub_words = words_li[w_cursor:w_cursor + cnt]
                w_cursor += cnt
                if cnt == 1:
                    s["text"] = sub_words[0]
                    s["is_icon"] = False
                    new_ln.append(s)
                else:
                    sx, sy, sw_box, sh_box = s["x"], s["y"], s["w"], s["h"]
                    f_slice = (face_bin[sy:sy + sh_box, sx:sx + sw_box] > 0) & s["mask"][sy:sy + sh_box, sx:sx + sw_box]
                    c_slice = (core[sy:sy + sh_box, sx:sx + sw_box] > 0) & s["mask"][sy:sy + sh_box, sx:sx + sw_box]
                    col_face = np.sum(f_slice, axis=0).astype(np.float32)
                    col_core = np.sum(c_slice, axis=0).astype(np.float32)
                    profile = 0.78 * col_face + 0.22 * col_core
                    if len(profile) >= 5:
                        profile = np.convolve(profile, np.ones(5, dtype=np.float32) / 5.0, mode="same")
                    max_prof = max(float(profile.max()), 1.0)

                    char_lens = [max(1, len(re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", w)) or len(w)) for w in sub_words]
                    tot_chars = float(sum(char_lens))
                    cuts = [0]
                    cum = 0
                    win_half = max(42, int(sw_box * 0.14))
                    for r_i in range(cnt - 1):
                        cum += char_lens[r_i]
                        expected_x = int(sw_box * (cum / tot_chars))
                        s_x1 = max(cuts[-1] + 24, expected_x - win_half)
                        s_x2 = min(sw_box - 24, expected_x + win_half)
                        if s_x2 > s_x1:
                            xs_cand = np.arange(s_x1, s_x2)
                            dist_pen = 0.22 * ((xs_cand - expected_x) / float(win_half)) ** 2
                            score = (profile[s_x1:s_x2] / max_prof) + dist_pen
                            cut_rel = s_x1 + int(np.argmin(score))
                        else:
                            cut_rel = expected_x
                        cuts.append(cut_rel)
                    cuts.append(sw_box)
                    sub_masks = self._partition_mask_by_cuts(s["mask"], face_bin, [sx + c for c in cuts[1:-1]])
                    for r_i in range(cnt):
                        c_x1, c_x2 = sx + cuts[r_i], sx + cuts[r_i + 1]
                        sub_m = sub_masks[r_i]
                        if not np.any(sub_m):
                            sub_m = np.zeros_like(s["mask"])
                            sub_m[sy:sy + sh_box, c_x1:c_x2] = True
                        ys_s, xs_s = np.where(sub_m)
                        bx_s, bx_e = int(xs_s.min()), int(xs_s.max()) + 1
                        by_s, by_e = int(ys_s.min()), int(ys_s.max()) + 1
                        new_ln.append({
                            "mask": sub_m,
                            "x": bx_s, "y": by_s, "w": max(1, bx_e - bx_s), "h": max(1, by_e - by_s),
                            "cx": float(xs_s.mean()),
                            "cy": float(ys_s.mean()),
                            "text": sub_words[r_i], "is_icon": False
                        })
            return new_ln

        for j, s in enumerate(ln):
            s["text"] = words_li[j]
            s["is_icon"] = False
        return ln

    def _verify_and_refine_line_words(self, aligned_ln: list, words_li: list, norm_gray: np.ndarray,
                                      face_bin: np.ndarray, core: np.ndarray, viet_pred) -> list:
        """
        AI-in-the-Loop Word Verification & Boundary Refinement:
        Evaluates each split word block with VietOCR and checks for:
        1. Overlapping neighbor blocks (e.g. slanted 3D letters where top hat connected left and bottom connected right)
        2. Severe width/character-ratio skew (e.g. 'ĐÁNH BA' + 'Y' instead of 'ĐÁNH' + 'BAY')
        3. OCR boundary shifts (e.g. block k reads 'VẾ' and block k+1 reads 'TBẨN' instead of 'VẾT' | 'BẨN')
        When a boundary error is detected between adjacent blocks (k, k+1), performs AI-Guided Valley Search
        testing candidate vertical valleys with VietOCR to lock onto the true inter-word gap.
        """
        from PIL import Image
        K = len(aligned_ln)
        if K <= 1 or viet_pred is None or len(words_li) != K:
            return aligned_ln

        def _crop_block_pil(b_mask, bx, by, bw, bh):
            y1 = max(0, by - 32)
            y2 = min(self.h, by + bh + 18)
            x1 = max(0, bx - 8)
            x2 = min(self.w, bx + bw + 8)
            sub_m = cv2.dilate(b_mask[y1:y2, x1:x2].astype(np.uint8), np.ones((15, 11), np.uint8))
            arr = np.where(sub_m > 0, norm_gray[y1:y2, x1:x2], 255)
            arr_pad = np.pad(arr, ((8, 8), (10, 10)), mode="constant", constant_values=255)
            return Image.fromarray(cv2.cvtColor(arr_pad, cv2.COLOR_GRAY2RGB))

        def _clean_base(s):
            return strip_accents(re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", s or "")).upper()

        for k in range(K - 1):
            b1, b2 = aligned_ln[k], aligned_ln[k + 1]
            exp1 = _clean_base(words_li[k])
            exp2 = _clean_base(words_li[k + 1])
            if not exp1 or not exp2:
                continue

            # Check 1: Do b1 and b2 overlap horizontally, or is their width ratio skewed vs character count ratio?
            ov_x = (b1["x"] + b1["w"]) - b2["x"]
            tot_w = float(max(1, b1["w"] + b2["w"]))
            tot_c = float(max(1, len(exp1) + len(exp2)))
            w_frac1 = b1["w"] / tot_w
            c_frac1 = len(exp1) / tot_c
            geom_skew = (ov_x > 16) or (abs(w_frac1 - c_frac1) > 0.17)

            # Check 2: Ask AI (VietOCR) to read b1 and b2 individually
            got1_raw, conf1 = viet_pred.predict_with_conf(_crop_block_pil(b1["mask"], b1["x"], b1["y"], b1["w"], b1["h"]))
            got2_raw, conf2 = viet_pred.predict_with_conf(_crop_block_pil(b2["mask"], b2["x"], b2["y"], b2["w"], b2["h"]))
            got1 = _clean_base(got1_raw)
            got2 = _clean_base(got2_raw)

            # Determine if AI detected a boundary shift between b1 and b2
            ai_exact_match = (got1 == exp1 and got2 == exp2) and (ov_x <= 16)
            if ai_exact_match:
                continue

            boundary_shifted = geom_skew or (
                (len(got1) > len(exp1) and len(got2) < len(exp2)) or
                (len(got1) < len(exp1) and len(got2) > len(exp2)) or
                (got1 != exp1 and got2 != exp2)
            )
            if not boundary_shifted:
                continue

            # Perform AI-Guided Valley Search on the union of (b1, b2)
            u_mask = b1["mask"] | b2["mask"]
            u_ys, u_xs = np.where(u_mask)
            if len(u_xs) == 0:
                continue
            ux, uy = int(u_xs.min()), int(u_ys.min())
            uw, uh = int(u_xs.max() - ux + 1), int(u_ys.max() - uy + 1)
            if uw < 48:
                continue

            f_slice = (face_bin[uy:uy + uh, ux:ux + uw] > 0) & u_mask[uy:uy + uh, ux:ux + uw]
            c_slice = (core[uy:uy + uh, ux:ux + uw] > 0) & u_mask[uy:uy + uh, ux:ux + uw]
            col_face = np.sum(f_slice, axis=0).astype(np.float32)
            col_core = np.sum(c_slice, axis=0).astype(np.float32)
            profile = 0.82 * col_face + 0.18 * col_core
            if len(profile) >= 5:
                profile = np.convolve(profile, np.ones(5, dtype=np.float32) / 5.0, mode="same")
            max_prof = max(float(profile.max()), 1.0)

            expected_rel = int(uw * c_frac1)
            win_half = max(45, int(uw * 0.24))
            s_x1 = max(24, expected_rel - win_half)
            s_x2 = min(uw - 24, expected_rel + win_half)
            if s_x2 <= s_x1:
                continue

            xs_cand = np.arange(s_x1, s_x2)
            dist_pen = 0.20 * ((xs_cand - expected_rel) / float(win_half)) ** 2
            valley_scores = (profile[s_x1:s_x2] / max_prof) + dist_pen

            # Pick up to 3 well-separated local valley minima to evaluate with AI (VietOCR)
            cand_rels = []
            order_idx = np.argsort(valley_scores)
            for idx_v in order_idx:
                r_x = int(s_x1 + idx_v)
                if all(abs(r_x - prev_r) >= 22 for prev_r in cand_rels):
                    cand_rels.append(r_x)
                if len(cand_rels) >= 3:
                    break

            best_cut_rel = cand_rels[0]
            best_total_cost = 1e9

            for r_x in cand_rels:
                cut_abs = ux + r_x
                m1, m2 = self._partition_mask_by_cuts(u_mask, face_bin, [cut_abs])
                if not np.any(m1) or not np.any(m2):
                    continue
                ys_a, xs_a = np.where(m1)
                ys_b, xs_b = np.where(m2)
                g1_raw, c1 = viet_pred.predict_with_conf(_crop_block_pil(m1, int(xs_a.min()), uy, int(xs_a.max() - xs_a.min() + 1), uh))
                g2_raw, c2 = viet_pred.predict_with_conf(_crop_block_pil(m2, int(xs_b.min()), uy, int(xs_b.max() - xs_b.min() + 1), uh))
                g1 = _clean_base(g1_raw)
                g2 = _clean_base(g2_raw)

                len_err = abs(len(g1) - len(exp1)) + abs(len(g2) - len(exp2))
                exact_bonus = (1.5 if g1 == exp1 else 0.0) + (1.5 if g2 == exp2 else 0.0)
                prefix_bonus = (0.6 if (g1[:1] == exp1[:1] and g2[:1] == exp2[:1]) else 0.0)
                v_score = float(valley_scores[r_x - s_x1])
                total_cost = v_score * 1.4 + len_err * 0.85 - exact_bonus - prefix_bonus - 0.25 * (c1 + c2)
                if total_cost < best_total_cost:
                    best_total_cost = total_cost
                    best_cut_rel = r_x

            cut_abs = ux + best_cut_rel
            m1, m2 = self._partition_mask_by_cuts(u_mask, face_bin, [cut_abs])
            if np.any(m1) and np.any(m2):
                ys1, xs1 = np.where(m1)
                ys2, xs2 = np.where(m2)
                b1["mask"] = m1
                b1["x"], b1["y"] = int(xs1.min()), int(ys1.min())
                b1["w"], b1["h"] = int(xs1.max() - b1["x"] + 1), int(ys1.max() - b1["y"] + 1)
                b1["cx"], b1["cy"] = float(xs1.mean()), float(ys1.mean())

                b2["mask"] = m2
                b2["x"], b2["y"] = int(xs2.min()), int(ys2.min())
                b2["w"], b2["h"] = int(xs2.max() - b2["x"] + 1), int(ys2.max() - b2["y"] + 1)
                b2["cx"], b2["cy"] = float(xs2.mean()), float(ys2.mean())

        return aligned_ln

    def _detect_blocks(self, split_pinch: bool = True):
        from PIL import Image
        a = self.alpha
        max_a = int(a.max()) if a.size > 0 else 255
        thresh = max(240, max_a - 5)

        b_ch, g_ch, r_ch = self.img_bgra[:, :, 0], self.img_bgra[:, :, 1], self.img_bgra[:, :, 2]
        lum = 0.114 * b_ch + 0.587 * g_ch + 0.299 * r_ch

        # 1. Extract opaque core mask & distance transform
        core = (a >= thresh).astype(np.uint8)
        core = cv2.morphologyEx(core, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        self.core_mask = core
        dist = cv2.distanceTransform(core, cv2.DIST_L2, 5)

        # 2. Spatially adaptive polarity normalization (extracts inner letter face regardless of white-on-blue, cyan-on-blue, or flat 2D vector style)
        core_lums = lum[core > 0]
        if core_lums.size > 100:
            p10_l, p90_l = np.percentile(core_lums, [10, 90])
            # Flat 2D vector text = uniform colour AND crisp alpha edges. Glowing/shadowed 3D text can also have a
            # uniform face colour, but a wide band of semi-transparent glow around the core: it must keep the 3D path.
            # (Only count soft pixels > 4 px away from the opaque core, so 1-2 px anti-aliasing / downscale edges don't count.)
            vis_px = int(np.count_nonzero(a > 10))
            d_out = cv2.distanceTransform((1 - core).astype(np.uint8), cv2.DIST_L2, 3)
            glow_frac = float(np.count_nonzero((a > 10) & (d_out > 4))) / float(max(1, vis_px))
            is_flat_mono = bool((p90_l - p10_l) < 25.0 and glow_frac < 0.15)
        else:
            is_flat_mono = False

        speed_bar_mask = np.zeros((self.h, self.w), dtype=np.uint8)
        if is_flat_mono:
            face_bin = (a >= 128).astype(np.uint8)
            norm_gray = (255 - a).astype(np.uint8)
            min_f_sol = 0.10
        else:
            inner_core = (dist >= 4).astype(np.uint8)
            deep_mask = (dist >= 6).astype(np.float32)
            rim_mask = ((dist >= 1) & (dist <= 4)).astype(np.float32)
            local_inner_lum = cv2.blur(lum * deep_mask, (121, 81)) / (cv2.blur(deep_mask, (121, 81)) + 1e-5)
            local_rim_lum = cv2.blur(lum * rim_mask, (121, 81)) / (cv2.blur(rim_mask, (121, 81)) + 1e-5)
            is_colored_bright = (local_inner_lum > local_rim_lum + 35.0) & (local_rim_lum < 112.0)
            thr_lo = np.where(is_colored_bright, 125.0, 145.0)
            span_b = np.where(is_colored_bright, 60.0, 75.0)
            bright_face = np.clip((lum - thr_lo) / span_b, 0.0, 1.0)
            dark_face = np.clip((145.0 - lum) / 70.0, 0.0, 1.0)
            face = np.where(local_inner_lum > 145.0, bright_face, dark_face) * inner_core
            norm_gray = (255.0 * (1.0 - face)).astype(np.uint8)
            face_bin = (norm_gray < 140).astype(np.uint8)
            min_f_sol = 0.22

            # Strip horizontal optical lens-flare streaks, thin specular lines, and hollow 3D bevel highlight arcs
            nf_cln, lf_cln, sf_cln, _ = cv2.connectedComponentsWithStats(face_bin)
            for i_c in range(1, nf_cln):
                xc, yc, wc, hc, ac = sf_cln[i_c]
                sol_c = ac / float(max(1, wc * hc))
                if (
                    ac < 25 or
                    (wc / float(max(1, hc)) >= 4.0 and hc <= max(28, int(self.h * 0.032))) or
                    (hc <= max(12, int(self.h * 0.014)) and wc / float(max(1, hc)) >= 2.2) or
                    (sol_c < 0.21 and ac < 3500 and max(wc, hc) >= 28)
                ):
                    if wc >= 100 and hc >= 12 and ac >= 1400 and sol_c >= 0.72 and wc / float(max(1, hc)) >= 4.0:
                        speed_bar_mask[lf_cln == i_c] = 1
                    face_bin[lf_cln == i_c] = 0
                    norm_gray[lf_cln == i_c] = 255

        # 3. Detect character/word blobs, attach large 3D diacritics, and filter 4-pointed star icons
        self.face_bin = face_bin.copy()

        icon_seeds = []
        if np.any(speed_bar_mask):
            sb_grp = cv2.dilate(speed_bar_mask, cv2.getStructuringElement(cv2.MORPH_RECT, (35, 35)))
            nsb, lsb, ssb, csb = cv2.connectedComponentsWithStats(sb_grp)
            for isb in range(1, nsb):
                m_sb = (lsb == isb) & (speed_bar_mask > 0)
                ys_sb, xs_sb = np.where(m_sb)
                if len(xs_sb) == 0:
                    continue
                bx0, by0 = int(xs_sb.min()), int(ys_sb.min())
                bw0, bh0 = int(xs_sb.max() - bx0 + 1), int(ys_sb.max() - by0 + 1)
                icon_seeds.append({
                    "mask": m_sb,
                    "x": bx0, "y": by0, "w": bw0, "h": bh0,
                    "body_y": by0, "body_h": bh0, "body_cy": float(ys_sb.mean()),
                    "cx": float(xs_sb.mean()), "cy": float(ys_sb.mean()),
                    "text": "✨", "is_icon": True
                })
        # 3A. Pre-filter 4-pointed star icons directly on undilated face_bin so stars close to text never fuse during w_bin dilation
        if not is_flat_mono:
            nf0, lf0, sf0, cf0 = cv2.connectedComponentsWithStats(face_bin)
            for i in range(1, nf0):
                x, y, w, h, area = sf0[i]
                if not (900 <= area < 16000 and w < 195 and h < 240 and 0.62 <= (w / float(max(1, h))) <= 1.38):
                    continue
                box_sol = area / float(max(1, w * h))
                if box_sol > 0.43:
                    continue
                m_sub = (lf0[y:y + h, x:x + w] == i).astype(np.uint8)
                cnts_i, _ = cv2.findContours(m_sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                hull_a = cv2.contourArea(cv2.convexHull(cnts_i[0])) if cnts_i else float(area)
                hull_sol = area / float(max(1.0, hull_a))
                if hull_sol > 0.71:
                    continue
                sym_h = np.sum(m_sub & np.fliplr(m_sub)) / float(max(1, np.sum(m_sub | np.fliplr(m_sub))))
                sym_v = np.sum(m_sub & np.flipud(m_sub)) / float(max(1, np.sum(m_sub | np.flipud(m_sub))))
                is_tilted_star = (hull_sol <= 0.62 and box_sol <= 0.38 and min(sym_h, sym_v) >= 0.32 and max(sym_h, sym_v) >= 0.52)
                if (sym_h < 0.45 or sym_v < 0.45) and not is_tilted_star:
                    continue
                same_baseline_peers = 0
                for j in range(1, nf0):
                    if j == i:
                        continue
                    ox, oy, ow, oh, oarea = sf0[j]
                    if oh >= 50 and oarea >= 2000:
                        h_gap = max(0, max(x - (ox + ow), ox - (x + w)))
                        if abs(y - oy) <= 10 and abs((y + h) - (oy + oh)) <= 10 and h_gap <= 180:
                            same_baseline_peers += 1
                if same_baseline_peers == 0:
                    m_full = (lf0 == i)
                    face_bin[m_full] = 0
                    norm_gray[m_full] = 255
                    icon_seeds.append({
                        "mask": m_full,
                        "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                        "body_y": int(y), "body_h": int(h), "body_cy": float(cf0[i][1]),
                        "cx": float(cf0[i][0]), "cy": float(cf0[i][1]),
                        "text": "✨", "is_icon": True
                    })

        w_bin = cv2.dilate(face_bin, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 5)))
        nw, lw, sw, cw = cv2.connectedComponentsWithStats(w_bin)

        raw_blobs = []
        for i in range(1, nw):
            x, y, w, h, area = sw[i]
            if area < 900:
                continue
            f_area = int(np.sum((lw[y:y + h, x:x + w] == i) & (face_bin[y:y + h, x:x + w] > 0)))
            f_sol = f_area / float(max(1, w * h))
            if not is_flat_mono and 1000 <= area < 14000 and w < 185 and h < 230 and f_sol <= 0.36 and 0.65 <= (w / float(max(1, h))) <= 1.32:
                m_sub_w = (lw[y:y + h, x:x + w] == i).astype(np.uint8)
                cnts_w, _ = cv2.findContours(m_sub_w, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                hull_aw = cv2.contourArea(cv2.convexHull(cnts_w[0])) if cnts_w else float(area)
                hull_sol_w = area / float(max(1.0, hull_aw))
                sym_hw = np.sum(m_sub_w & np.fliplr(m_sub_w)) / float(max(1, np.sum(m_sub_w | np.fliplr(m_sub_w))))
                sym_vw = np.sum(m_sub_w & np.flipud(m_sub_w)) / float(max(1, np.sum(m_sub_w | np.flipud(m_sub_w))))
                if hull_sol_w <= 0.72 and sym_hw >= 0.42 and sym_vw >= 0.42:
                    # Ensure it is not a letter (like 'Y', 'C', 'V') sitting on the exact same top & bottom baseline as adjacent letters
                    same_baseline_peers = 0
                    for j in range(1, nw):
                        if j == i:
                            continue
                        ox, oy, ow, oh, oarea = sw[j]
                        if oh >= 55 and oarea >= 2600:
                            h_gap = max(0, max(x - (ox + ow), ox - (x + w)))
                            if abs(y - oy) <= 10 and abs((y + h) - (oy + oh)) <= 10 and h_gap <= 180:
                                same_baseline_peers += 1
                    if same_baseline_peers == 0:
                        face_bin[lw == i] = 0
                        norm_gray[y:y + h, x:x + w][lw[y:y + h, x:x + w] == i] = 255
                        icon_seeds.append({
                            "mask": (lw == i),
                            "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                            "body_y": int(y), "body_h": int(h), "body_cy": float(cw[i][1]),
                            "cx": float(cw[i][0]), "cy": float(cw[i][1]),
                            "text": "✨", "is_icon": True
                        })
                        continue

            raw_blobs.append({
                "id": i,
                "mask": (lw == i),
                "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                "body_y": int(y), "body_h": int(h), "body_cy": float(cw[i][1]),
                "cx": float(cw[i][0]), "cy": float(cw[i][1]),
                "area": int(area), "f_sol": f_sol
            })

        viet_pred = get_vietocr_predictor()

        # Select main word/character bodies: standard word-seed size, collinear subtitle row, OR AI-verified medium-height word
        std_seed_h = max(48, min(85, int(self.h * 0.14)))
        std_seed_area = max(2500, min(7000, int(self.h * self.w * 0.012)))
        bodies = []
        for b in raw_blobs:
            if b["f_sol"] < min_f_sol:
                continue
            collinear_peers = sum(
                1 for o in raw_blobs
                if o is not b and o["h"] >= 50 and o["area"] >= 2400 and o["f_sol"] >= min_f_sol
                and abs(b["y"] - o["y"]) <= 16 and abs(b["h"] - o["h"]) <= 22
            )
            is_cand = False
            needs_ai_check = False
            if b["h"] >= std_seed_h and b["area"] >= std_seed_area:
                is_cand = True
                if not is_flat_mono and collinear_peers == 0 and b["area"] < 12000 and b["h"] < std_seed_h * 1.15:
                    needs_ai_check = True
            elif b["h"] >= 50 and b["area"] >= 2400 and collinear_peers >= 2:
                is_cand = True
            elif b["h"] >= 45 and b["area"] >= 3500 and b["w"] >= b["h"] * 1.15 and b["f_sol"] >= 0.32:
                is_cand = True
                if not is_flat_mono and collinear_peers == 0:
                    needs_ai_check = True

            if is_cand and b["h"] < 85 and b["area"] < 8000:
                # A blob hugging the top/bottom of a much taller text blob is a diacritic (e.g. the stacked
                # '^' + '`' of 'Ồ', 'Ầ', 'Ề', or upper diacritics on 4K flat text), not a word: skip AI check,
                # mark as diacritic so it attaches to the letter below it in step 6.
                is_mark = False
                for o in raw_blobs:
                    if o is b or o["h"] < max(95, b["h"] * 1.8) or o["f_sol"] < min_f_sol:
                        continue
                    if b["w"] > max(o["w"] * 0.65, 115) or b["area"] > o["area"] * 0.35:
                        continue
                    ov_x = max(0, min(b["x"] + b["w"], o["x"] + o["w"]) - max(b["x"], o["x"]))
                    v_gap = max(b["y"] - (o["y"] + o["h"]), o["y"] - (b["y"] + b["h"]))
                    if ov_x >= b["w"] * 0.45 and -8 <= v_gap <= max(40, int(o["h"] * 0.32)):
                        is_mark = True
                        break
                if is_mark:
                    needs_ai_check = False
                    b["is_mark"] = True

            if is_cand and needs_ai_check and viet_pred is not None:
                # Ask AI (VietOCR) whether this isolated medium-height blob is real text or a background decoration
                bx, by, bw_b, bh_b = b["x"], b["y"], b["w"], b["h"]
                cx1, cy1 = max(0, bx - 10), max(0, by - 25)
                cx2, cy2 = min(self.w, bx + bw_b + 10), min(self.h, by + bh_b + 15)
                crop_b = Image.fromarray(cv2.cvtColor(norm_gray[cy1:cy2, cx1:cx2], cv2.COLOR_GRAY2RGB))
                ai_txt, ai_conf = viet_pred.predict_with_conf(crop_b)
                alnum_txt = re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", ai_txt or "")
                if ai_conf < 0.48 or len(alnum_txt) == 0:
                    face_bin[b["mask"]] = 0
                    norm_gray[b["mask"]] = 255
                    continue

            if is_cand:
                bodies.append(b)

        # Attach narrow same-line punctuation/stems (e.g. '!', '?' next to 'TRỘI!' where area < std_seed_area) directly into their adjacent word body
        body_ids = set(id(b) for b in bodies)
        for b in raw_blobs:
            if id(b) in body_ids or b["f_sol"] < 0.22:
                continue
            if b["h"] >= std_seed_h * 0.65 and b["area"] >= 1800:
                for parent in bodies:
                    v_ov = max(0, min(b["y"] + b["h"], parent["body_y"] + parent["body_h"]) - max(b["y"], parent["body_y"]))
                    h_gap = max(0, max(b["x"] - (parent["x"] + parent["w"]), parent["x"] - (b["x"] + b["w"])))
                    if v_ov >= b["h"] * 0.55 and h_gap <= 36:
                        parent["mask"] = parent["mask"] | b["mask"]
                        nx1 = min(parent["x"], b["x"])
                        ny1 = min(parent["y"], b["y"])
                        nx2 = max(parent["x"] + parent["w"], b["x"] + b["w"])
                        ny2 = max(parent["y"] + parent["h"], b["y"] + b["h"])
                        parent["x"], parent["y"], parent["w"], parent["h"] = nx1, ny1, nx2 - nx1, ny2 - ny1
                        parent["area"] += b["area"]
                        body_ids.add(id(b))
                        break

        # Merge oversized diacritic hats (e.g. 'Ể', 'Ố', 'Ẩ', 'Ế' on 3D titles) into parent word below them
        bodies.sort(key=lambda b: b["h"], reverse=True)
        seeds = []
        for b in bodies:
            is_hat = False
            # A full word sitting on a multi-word text line (with >=1 large word peer at the same y) is not a diacritic hat
            large_row_peers = sum(
                1 for o in bodies
                if o is not b and o["area"] >= 9500 and o["h"] >= 72
                and abs(o["body_cy"] - b["body_cy"]) <= max(b["h"], o["h"]) * 0.35
            )
            if b["h"] <= 140 and b["area"] <= 12500 and large_row_peers == 0:
                for parent in seeds:
                    ov_x = max(0, min(b["x"] + b["w"], parent["x"] + parent["w"]) - max(b["x"], parent["x"]))
                    v_gap_above = parent["body_y"] - (b["y"] + b["h"])
                    if (
                        parent["body_h"] >= max(145, int(b["h"] * 1.55)) and
                        b["h"] <= parent["body_h"] * 0.50 and
                        b["w"] <= parent["body_h"] * 1.05 and
                        b["area"] <= parent["area"] * 0.22 and
                        ov_x >= b["w"] * 0.45 and
                        -25 <= v_gap_above <= parent["body_h"] * 0.32
                    ):
                        parent["mask"] = parent["mask"] | b["mask"]
                        nx1 = min(parent["x"], b["x"])
                        ny1 = min(parent["y"], b["y"])
                        nx2 = max(parent["x"] + parent["w"], b["x"] + b["w"])
                        ny2 = max(parent["y"] + parent["h"], b["y"] + b["h"])
                        parent["x"], parent["y"], parent["w"], parent["h"] = nx1, ny1, nx2 - nx1, ny2 - ny1
                        parent["area"] += b["area"]
                        is_hat = True
                        break
            if not is_hat and not b.get("is_mark"):
                seeds.append(b)

        # Detach small diacritic pieces hanging BELOW a seed's letter band (e.g. the '~' of 'DỄ' fused by w_bin
        # dilation into 'ĐỂ' on the line above). They are re-assigned in step 6 by geometry (above-hat vs below-dot).
        detached_parts = []
        dil_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 5))
        for sd in seeds:
            sx0, sy0, sw0, sh0 = sd["x"], sd["y"], sd["w"], sd["h"]
            sd["full_y2"] = sy0 + sh0
            fm_sd = ((face_bin[sy0:sy0 + sh0, sx0:sx0 + sw0] > 0) & sd["mask"][sy0:sy0 + sh0, sx0:sx0 + sw0]).astype(np.uint8)
            nfc, lfc, sfc, _ = cv2.connectedComponentsWithStats(fm_sd, connectivity=8)
            if nfc <= 2:
                continue
            max_ch = max(int(sfc[k, cv2.CC_STAT_HEIGHT]) for k in range(1, nfc))
            big_k = [k for k in range(1, nfc) if sfc[k, cv2.CC_STAT_HEIGHT] >= max_ch * 0.6]
            band_t = float(np.median([sfc[k, cv2.CC_STAT_TOP] for k in big_k]))
            band_b = float(np.median([sfc[k, cv2.CC_STAT_TOP] + sfc[k, cv2.CC_STAT_HEIGHT] for k in big_k]))
            band_h = max(1.0, band_b - band_t)
            removed = False
            for k in range(1, nfc):
                kx, ky, kw, kh, ka = sfc[k]
                if k in big_k or ka < 8:
                    continue
                if ky >= band_b - band_h * 0.08 and kh <= band_h * 0.40:
                    part_face = np.zeros_like(sd["mask"])
                    part_face[sy0:sy0 + sh0, sx0:sx0 + sw0] = (lfc == k)
                    part_w = cv2.dilate(part_face.astype(np.uint8), dil_k).astype(bool) & sd["mask"]
                    sd["mask"] = sd["mask"] & ~part_w
                    detached_parts.append(part_w)
                    removed = True
            if removed:
                ys_d, xs_d = np.where(sd["mask"])
                if len(xs_d):
                    sd["x"], sd["y"] = int(xs_d.min()), int(ys_d.min())
                    sd["w"], sd["h"] = int(xs_d.max() - sd["x"] + 1), int(ys_d.max() - sd["y"] + 1)
                    sd["cx"], sd["cy"] = float(xs_d.mean()), float(ys_d.mean())
                    sd["body_h"] = min(sd["body_h"], sd["y"] + sd["h"] - sd["body_y"])

        # Also check raw core for any isolated 4-pointed star icons not caught in w_bin
        if not is_flat_mono:
            nc_raw, lc_raw, sc_raw, cc_raw = cv2.connectedComponentsWithStats(core)
            for i in range(1, nc_raw):
                x, y, w, h, area = sc_raw[i]
                sol = area / float(max(1, w * h))
                if 1000 <= area < 14000 and w < 185 and h < 230 and sol <= 0.54 and 0.65 <= (w / float(max(1, h))) <= 1.28:
                    is_diacritic = False
                    for sd in seeds:
                        ov_x = max(0, min(x + w, sd["x"] + sd["w"]) - max(x, sd["x"]))
                        v_gap = max(0, max(y - (sd["y"] + sd["h"]), sd["y"] - (y + h)))
                        if ov_x >= w * 0.35 and v_gap < max(65, int(sd["h"] * 0.35)):
                            is_diacritic = True
                            break
                    if is_diacritic:
                        continue
                    m_sub_c = (lc_raw[y:y + h, x:x + w] == i).astype(np.uint8)
                    cnts_c, _ = cv2.findContours(m_sub_c, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    hull_ac = cv2.contourArea(cv2.convexHull(cnts_c[0])) if cnts_c else float(area)
                    if (area / float(max(1.0, hull_ac))) > 0.72:
                        continue
                    sym_hc = np.sum(m_sub_c & np.fliplr(m_sub_c)) / float(max(1, np.sum(m_sub_c | np.fliplr(m_sub_c))))
                    sym_vc = np.sum(m_sub_c & np.flipud(m_sub_c)) / float(max(1, np.sum(m_sub_c | np.flipud(m_sub_c))))
                    if sym_hc < 0.42 or sym_vc < 0.42:
                        continue
                    m_raw = (lc_raw == i)
                    if not any(np.any(m_raw & ic["mask"]) for ic in icon_seeds) and not any(np.any(m_raw & sd["mask"]) for sd in seeds):
                        face_bin[m_raw] = 0
                        norm_gray[y:y + h, x:x + w][m_raw[y:y + h, x:x + w]] = 255
                        icon_seeds.append({
                            "mask": m_raw,
                            "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                            "body_y": int(y), "body_h": int(h), "body_cy": float(cc_raw[i][1]),
                            "cx": float(cc_raw[i][0]), "cy": float(cc_raw[i][1]),
                            "text": "✨", "is_icon": True
                        })

        # 4. Group word seeds into horizontal/slanted text lines using body_cy & nearest horizontal neighbor
        seeds.sort(key=lambda s: s["body_cy"])
        lines = []
        for s in seeds:
            placed = False
            for ln in lines:
                nearest_in_ln = min(ln, key=lambda item: abs(s["cx"] - item["cx"]))
                ref_h = max(np.mean([x["body_h"] for x in ln]), s["body_h"], 90.0)
                if (
                    abs(s["body_cy"] - nearest_in_ln["body_cy"]) < ref_h * 0.58 or
                    abs(s["body_cy"] - np.mean([x["body_cy"] for x in ln])) < ref_h * 0.52
                ):
                    ln.append(s)
                    placed = True
                    break
            if not placed:
                lines.append([s])

        lines.sort(key=lambda ln: np.mean([x["body_cy"] for x in ln]))

        # Detach small diacritic-like blobs sitting outside a line's letter band (e.g. the '~' of 'DỄ' floating
        # just below 'ĐỂ'). Otherwise the DP merges them into the wrong word; step 6 re-attaches them properly.
        for ln in lines:
            if len(ln) < 2:
                continue
            max_bh = max(x["body_h"] for x in ln)
            big = [x for x in ln if x["body_h"] >= max_bh * 0.6]
            band_top = float(np.median([x["body_y"] for x in big]))
            band_bot = float(np.median([x["body_y"] + x["body_h"] for x in big]))
            keep = []
            for x in ln:
                cy_x = x["body_y"] + x["body_h"] * 0.5
                if x["body_h"] < max_bh * 0.45 and (cy_x < band_top or cy_x > band_bot):
                    continue
                keep.append(x)
            if keep:
                ln[:] = keep

        # 5. Run cross-platform VietOCR on line crops & align with word seeds via DP / Valley Profile
        fname = unicodedata.normalize("NFC", os.path.basename(self.file_path or ""))
        fname_clean = re.split(r"-\s*\d|\d{4,}", os.path.splitext(fname)[0])[0]
        hint_words = [w.upper() for w in re.findall(r"[A-Za-zÀ-ỹ0-9]+", fname_clean)]

        line_crops = []
        for li_c, ln in enumerate(lines):
            ln.sort(key=lambda c: c["x"])
            x1 = max(0, min(c["x"] for c in ln) - 15)
            hat_pad = 55 if self.h >= 450 else 12
            y1 = max(0, min(min(c["y"], c["body_y"] - hat_pad) for c in ln) - 15)
            if self.h < 450 and li_c > 0:
                prev_y2 = max(c["body_y"] + c["body_h"] for c in lines[li_c - 1])
                y1 = max(y1, prev_y2)
            x2 = min(self.w, max(c["x"] + c["w"] for c in ln) + 15)
            y2 = min(self.h, max(max(c["y"] + c["h"], c["body_y"] + c["body_h"], c.get("full_y2", 0)) for c in ln) + 28)
            y1 = min(y1, max(0, y2 - 20))
            crop_g = norm_gray[y1:y2, x1:x2].copy()
            # Blank out letters that belong to OTHER lines so the OCR never reads stray strokes of the line above/below
            for lj, ln_o in enumerate(lines):
                if lj == li_c:
                    continue
                for c_o in ln_o:
                    crop_g[c_o["mask"][y1:y2, x1:x2]] = 255
            crop = cv2.cvtColor(crop_g, cv2.COLOR_GRAY2RGB)
            alt = []
            # Alternative tighter-top crop: tall stacked marks (e.g. 'Ố', 'Ễ', 'Ồ') squash letters when resized to
            # the 32 px OCR height; the AI reads both versions and the more confident reading wins.
            med_bh = float(np.median([c["body_h"] for c in ln]))
            y1b = max(y1, min(c["body_y"] for c in ln) - int(med_bh * 0.30))
            if y1b - y1 >= 12:
                alt.append(Image.fromarray(cv2.cvtColor(crop_g[y1b - y1:, :], cv2.COLOR_GRAY2RGB)))
            line_crops.append((Image.fromarray(crop), alt))

        raw_preds = []
        for main_im, alts in line_crops:
            if viet_pred is None:
                raw_preds.append("")
                continue
            try:
                best_txt, best_conf = viet_pred.predict_with_conf(main_im)
                for a_im in alts:
                    t_a, c_a = viet_pred.predict_with_conf(a_im)
                    if c_a > best_conf + 0.02:
                        best_txt, best_conf = t_a, c_a
                raw_preds.append(best_txt)
            except Exception:
                raw_preds.append("")
        line_crops = [lc[0] for lc in line_crops]

        cleaned_lines = [clean_ocr_line_tokens(pr, hint_words) for pr in raw_preds]

        # Restore trailing '!' / '?' from the filename hint when the OCR drops a stylised 3D punctuation mark
        hint_punct = {}
        for mw, mp in re.findall(r"([A-Za-zÀ-ỹ0-9]+)\s*([!?])", fname_clean):
            hint_punct[strip_accents(mw.upper())] = mp
        if hint_punct:
            for toks in cleaned_lines:
                if toks:
                    last = toks[-1]
                    core_l = re.sub(r"[^A-Za-zÀ-ỹ0-9]", "", last)
                    if core_l and last == core_l and strip_accents(core_l) in hint_punct:
                        toks[-1] = last + hint_punct[strip_accents(core_l)]

        ordered_lines = []
        for li, ln in enumerate(lines):
            words_li = cleaned_lines[li] if (li < len(cleaned_lines) and cleaned_lines[li]) else [f"Chữ {j + 1}" for j in range(len(ln))]
            aligned_ln = self._align_line_blobs_to_words(ln, words_li, face_bin, core)
            aligned_ln = self._verify_and_refine_line_words(aligned_ln, words_li, norm_gray, face_bin, core, viet_pred)
            ordered_lines.append(aligned_ln)

        # Attach icons to their nearest line
        if not ordered_lines and icon_seeds:
            ordered_lines = [icon_seeds[:]]
        else:
            for ic in icon_seeds:
                best_ln = min(ordered_lines, key=lambda ln: abs(ic["cy"] - np.mean([x["cy"] for x in ln])))
                best_ln.append(ic)

        # 6. Assign all diacritics/punctuation as whole connected blobs first, then propagate 3D strokes via Voronoi seed map
        seed_map = np.zeros((self.h, self.w), dtype=np.int32)
        ordered_items = []
        sid = 1
        for line_idx, ln in enumerate(ordered_lines):
            ln.sort(key=lambda b: b["x"])
            for b in ln:
                seed_map[b["mask"]] = sid
                b["final_id"] = sid
                b["line_index"] = line_idx
                ordered_items.append(b)
                sid += 1

        # Whole-blob attachment for all remaining w_bin / face_bin components (diacritics ^, ´, `, ?, ~, . and punctuation dots)
        # Prevents pixel-level Voronoi from slicing diacritics or '!' stems in half between two adjacent lines
        text_items = [b for b in ordered_items if not b.get("is_icon")]
        if text_items:
            att_comps = []
            for comp_i in range(1, nw):
                att_comps.append((lw == comp_i, tuple(int(v) for v in sw[comp_i]), (float(cw[comp_i][0]), float(cw[comp_i][1]))))
            for part_w in detached_parts:
                ys_p, xs_p = np.where(part_w)
                if len(xs_p) == 0:
                    continue
                px, py = int(xs_p.min()), int(ys_p.min())
                att_comps.append((part_w, (px, py, int(xs_p.max() - px + 1), int(ys_p.max() - py + 1), int(len(xs_p))),
                                  (float(xs_p.mean()), float(ys_p.mean()))))
            for _att_pass in range(2):
                for cmask_w, (cx1, cy1, cw_c, ch_c, carea), (ccx, ccy) in att_comps:
                    if carea < 45:
                        continue
                    cmask_i = cmask_w & (face_bin > 0)
                    if not np.any(cmask_i):
                        continue
                    # Skip hollow background arcs or large non-diacritic background elements
                    f_sol_c = int(np.sum(cmask_i)) / float(max(1, cw_c * ch_c))
                    if f_sol_c < 0.22 and carea > 1200 and (cw_c > 260 or ch_c > 160):
                        continue
                    # Skip if already mostly assigned to a seed or icon
                    if np.mean(seed_map[cmask_w] > 0) >= 0.45:
                        continue
                    best_item = None
                    best_cost = 1e12
                    # True pixel distance from this mark to each word's letter faces: decides ownership of marks sitting
                    # between two lines (e.g. the dot of 'Ạ' in 'SẠCH' vs the '`' of 'VÀ' just below it).
                    R = 180
                    wy0, wy1_ = max(0, cy1 - R), min(self.h, cy1 + ch_c + R)
                    wx0, wx1_ = max(0, cx1 - R), min(self.w, cx1 + cw_c + R)
                    comp_face_win = cmask_i[wy0:wy1_, wx0:wx1_]
                    # Solid round blob = dấu nặng dot (belongs to the word ABOVE it); curved/elongated marks
                    # (´ ` ~ ^ ˘ and the hook of 'ả') belong to the word BELOW them.
                    fys, fxs = np.where(cmask_i[cy1:cy1 + ch_c, cx1:cx1 + cw_c])
                    if len(fxs):
                        fbw, fbh = int(fxs.max() - fxs.min() + 1), int(fys.max() - fys.min() + 1)
                        is_round_dot = (len(fxs) / float(fbw * fbh) >= 0.55) and (0.55 <= fbw / float(fbh) <= 1.8)
                    else:
                        fbw, fbh = cw_c, ch_c
                        is_round_dot = False
                    for w_item in text_items:
                        wx1, wy1, ww_w, wh_w = w_item["x"], w_item["y"], w_item["w"], w_item["h"]
                        h_dist = max(0.0, float(max(cx1 - (wx1 + ww_w), wx1 - (cx1 + cw_c))))
                        v_ov = max(0, min(cy1 + ch_c, wy1 + wh_w) - max(cy1, wy1))
                        bias = 0.0
                        h_ov_c = max(0, min(cx1 + cw_c, wx1 + ww_w) - max(cx1, wx1)) / float(max(1, cw_c))
                        hangs_below = h_ov_c >= 0.5 and (cy1 + ch_c) > (wy1 + wh_w) + 4
                        if v_ov >= ch_c * 0.35 and not hangs_below:
                            # Same horizontal line (e.g. '!', '?', '.', '“', '”', or broken stroke) -> must be horizontally adjacent
                            if h_dist > max(42.0, wh_w * 0.38):
                                continue
                        elif (cy1 + ch_c) <= (wy1 + 18):
                            # Component is ABOVE w_item -> Vietnamese upper diacritic (^, ´, `, ?, ~, ˘)
                            v_gap_above = max(0.0, float(wy1 - (cy1 + ch_c)))
                            if h_dist > 24.0 or v_gap_above > max(58.0, wh_w * 0.42):
                                continue
                            if is_round_dot:
                                bias = 10.0
                        else:
                            # Component is BELOW w_item -> Only valid if it is a compact dot (dấu nặng '.' or dot of '!'/'?')
                            v_gap_below = max(0.0, float(cy1 - (wy1 + wh_w)))
                            if h_dist > 20.0 or v_gap_below > max(38.0, wh_w * 0.26):
                                continue
                            # Measured on the undilated letter face (w_bin dilation widens marks by ~14 px)
                            is_compact_dot = (
                                fbw <= max(45, int(wh_w * 0.60)) and
                                fbh <= max(42, int(wh_w * 0.45)) and
                                (fbw / float(max(1, fbh))) <= 1.8
                            )
                            if not is_compact_dot:
                                continue
                            bias = 0.0 if is_round_dot else 4.0
                        item_face_win = w_item["mask"][wy0:wy1_, wx0:wx1_] & (face_bin[wy0:wy1_, wx0:wx1_] > 0)
                        if np.any(item_face_win):
                            d_map = distance_transform_edt(~item_face_win)
                            d_pix = float(d_map[comp_face_win].min())
                        else:
                            d_pix = float(R) + h_dist
                        cost = d_pix + bias + h_dist * 0.5
                        if cost < best_cost:
                            best_cost = cost
                            best_item = w_item
                    # If an icon is much closer to this small component than any word, attach it to the icon instead
                    if best_item is not None:
                        for ic_item in ordered_items:
                            if not ic_item.get("is_icon"):
                                continue
                            ix1, iy1, iw_i, ih_i = ic_item["x"], ic_item["y"], ic_item["w"], ic_item["h"]
                            ih_dist = max(0.0, float(max(cx1 - (ix1 + iw_i), ix1 - (cx1 + cw_c))))
                            iv_dist = max(0.0, float(max(cy1 - (iy1 + ih_i), iy1 - (cy1 + ch_c))))
                            ic_d = math.hypot(ih_dist, iv_dist)
                            if ic_d <= 18.0 and ic_d + 12.0 < best_cost:
                                best_item = ic_item
                                best_cost = ic_d
                    if best_item is not None:
                        unassigned = cmask_w & (seed_map == 0)
                        seed_map[unassigned] = best_item["final_id"]
                        best_item["mask"] = best_item["mask"] | unassigned
                        if np.any(unassigned):
                            nx1 = min(best_item["x"], cx1)
                            ny1 = min(best_item["y"], cy1)
                            nx2 = max(best_item["x"] + best_item["w"], cx1 + cw_c)
                            ny2 = max(best_item["y"] + best_item["h"], cy1 + ch_c)
                            best_item["x"], best_item["y"] = int(nx1), int(ny1)
                            best_item["w"], best_item["h"] = int(nx2 - nx1), int(ny2 - ny1)

        if ordered_items:
            # Propagate from the actual letter FACE pixels (not the dilated w_bin seed) so 3D outlines/extrusions
            # follow the glyph they belong to (e.g. the white outline of the '~' in 'DỄ' never goes to 'ĐỂ' above).
            src_map = np.where(face_bin > 0, seed_map, 0).astype(np.int32)
            present = set(np.unique(src_map).tolist()) - {0}
            for b in ordered_items:
                if b["final_id"] not in present:
                    src_map[(seed_map == b["final_id"]) & (src_map == 0)] = b["final_id"]
            # Use sampling=(1.2, 1.0) and bound 3D stroke propagation distance so distant background graphics (e.g. neon rings) are not absorbed
            dist_to_seed, inds = distance_transform_edt(src_map == 0, sampling=(1.2, 1.0), return_indices=True)
            nearest_seed = src_map[inds[0], inds[1]]
            med_h = float(np.median([b["h"] for b in ordered_items if not b.get("is_icon")] or [100.0]))
            max_stroke_dist = max(32.0, min(58.0, med_h * 0.42))
            valid = (core > 0) & ((dist_to_seed <= max_stroke_dist) | (seed_map > 0))
            lab_map = np.where(valid, nearest_seed, 0).astype(np.int32)

            # Opaque islands (small/medium connected core components such as detached diacritic islands) whose letter
            # faces belong to ONE word are assigned wholly to that word, so a diacritic is never split between words.
            ncc, cc, stats_cc, _ = cv2.connectedComponentsWithStats(core.astype(np.uint8), connectivity=8)
            if ncc > 1:
                cc_flat = cc.ravel()
                src_flat = src_map.ravel()
                sel = (src_flat > 0) & (cc_flat > 0)
                if np.any(sel):
                    pair_keys = cc_flat[sel].astype(np.int64) * (int(src_flat.max()) + 1) + src_flat[sel]
                    uk, uc = np.unique(pair_keys, return_counts=True)
                    nmax = int(src_flat.max()) + 1
                    p_cc, p_src = uk // nmax, uk % nmax
                    face_tot = np.bincount(src_flat[src_flat > 0], minlength=nmax)
                    owner = np.zeros(ncc, dtype=np.int32)
                    max_island_area = max(18000, int(self.h * self.w * 0.035))
                    for ci in np.unique(p_cc):
                        ci_int = int(ci)
                        if stats_cc[ci_int, cv2.CC_STAT_AREA] > max_island_area or stats_cc[ci_int, cv2.CC_STAT_WIDTH] > int(self.w * 0.42):
                            continue
                        msk = p_cc == ci
                        srcs, cnts = p_src[msk], uc[msk]
                        k = int(np.argmax(cnts))
                        major, c_major = int(srcs[k]), int(cnts[k])
                        ok = True
                        for s_o, c_o in zip(srcs, cnts):
                            if int(s_o) == major:
                                continue
                            # A minority word may only lose a tiny speck of itself (e.g. a stray sliver inside
                            # the '~' of 'DỄ'), never a real chunk of its own glyphs
                            if not (c_o < 0.1 * c_major and c_o < 0.05 * face_tot[int(s_o)]):
                                ok = False
                                break
                        if ok:
                            owner[ci_int] = major
                    own_map = owner[cc]
                    fix = (own_map > 0) & (lab_map > 0)
                    lab_map[fix] = own_map[fix]

            # Capture any large unassigned decorative graphic bars (e.g. '===' neon speed-bars beside text lines) as icon blocks
            rem_core = ((core > 0) & (lab_map == 0)).astype(np.uint8)
            if np.any(rem_core):
                nr_c, lr_c, sr_c, cr_c = cv2.connectedComponentsWithStats(rem_core, connectivity=8)
                for ri in range(1, nr_c):
                    rx, ry, rw, rh, rarea = sr_c[ri]
                    if rarea >= 12000 and rw >= 120 and rh >= 45:
                        rmask = (lr_c == ri)
                        rcx, rcy = float(cr_c[ri][0]), float(cr_c[ri][1])
                        near_b = min(ordered_items, key=lambda b: abs(rcy - b["cy"]))
                        new_fid = sid
                        sid += 1
                        lab_map[rmask] = new_fid
                        ordered_items.append({
                            "mask": rmask,
                            "x": int(rx), "y": int(ry), "w": int(rw), "h": int(rh),
                            "cx": rcx, "cy": rcy,
                            "final_id": new_fid,
                            "line_index": near_b["line_index"],
                            "text": "✨", "is_icon": True
                        })
                ordered_items.sort(key=lambda b: (b["line_index"], b["x"]))

            self.label_map = lab_map
        else:
            self.label_map = np.zeros((self.h, self.w), dtype=np.int32)

        self.blocks = []
        for b in ordered_items:
            fid = b["final_id"]
            mask_b = (self.label_map == fid)
            ys, xs = np.where(mask_b)
            if len(xs) == 0:
                mask_b = b["mask"]
                ys, xs = np.where(mask_b)
            if len(xs) == 0:
                continue
            bx, by = int(xs.min()), int(ys.min())
            bw, bh = int(xs.max() - bx + 1), int(ys.max() - by + 1)
            cnts, _ = cv2.findContours(mask_b.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            polys = []
            for cnt in cnts:
                if cv2.contourArea(cnt) >= 30:
                    approx = cv2.approxPolyDP(cnt, 2.5, True)
                    polys.append([[int(p[0][0]), int(p[0][1])] for p in approx])

            self.blocks.append({
                "id": fid,
                "x": bx, "y": by, "w": bw, "h": bh,
                "area": int(len(xs)),
                "cx": round(float(xs.mean()), 1), "cy": round(float(ys.mean()), 1),
                "line_index": b["line_index"],
                "cluster_id": b["line_index"] + 1,
                "is_icon": b["is_icon"],
                "text": b.get("text") or ("✨" if b["is_icon"] else f"Chữ {fid}"),
                "polys": polys
            })

    def apply_preset_mode(self, mode: str):
        self.push_history()
        self.selected_block_id = None
        self.custom_cluster_names = {}
        if mode == "lines":
            for b in self.blocks:
                b["cluster_id"] = b["line_index"] + 1
        elif mode == "words":
            for idx, b in enumerate(self.blocks):
                b["cluster_id"] = idx + 1
        elif mode == "lines_icons":
            max_line = max([b["line_index"] for b in self.blocks], default=0) + 1
            for b in self.blocks:
                b["cluster_id"] = (max_line + 1) if b["is_icon"] else (b["line_index"] + 1)
        self.normalize_cluster_ids()

    def normalize_cluster_ids(self):
        used = sorted(set(b["cluster_id"] for b in self.blocks))
        cmap = {cid: i + 1 for i, cid in enumerate(used)}
        if self.custom_cluster_names:
            new_custom = {}
            for old_cid, new_cid in cmap.items():
                if str(old_cid) in self.custom_cluster_names:
                    new_custom[str(new_cid)] = self.custom_cluster_names[str(old_cid)]
            self.custom_cluster_names = new_custom
        for b in self.blocks:
            b["cluster_id"] = cmap[b["cluster_id"]]

    def get_cluster_auto_name(self, cid: int, fallback_index: int) -> str:
        """Generates a clean layer name from the recognized words of all blocks in cluster `cid`."""
        custom_nm = (self.custom_cluster_names or {}).get(str(cid), "").strip()
        if custom_nm:
            return custom_nm
        c_blocks = [b for b in self.blocks if b["cluster_id"] == cid]
        if not c_blocks:
            return f"Cụm {fallback_index}"
        # Sort top-to-bottom by line_index, then left-to-right by x
        c_blocks.sort(key=lambda b: (b["line_index"], b["x"]))
        word_parts = [b.get("text", "").strip() for b in c_blocks if not b.get("is_icon") and b.get("text", "").strip()]
        if word_parts:
            return " ".join(word_parts)
        icon_parts = [b.get("text", "✨").strip() for b in c_blocks if b.get("is_icon")]
        if icon_parts:
            return "✨ Icon"
        return f"Cụm {fallback_index}"

    def set_cluster_names_from_pipe_str(self, pipe_str: str):
        """Parses a pipe-separated string ('LƯU HƯƠNG | CỰC ĐỈNH') into custom cluster names."""
        if not pipe_str or not pipe_str.strip():
            return
        parts = [p.strip() for p in pipe_str.split("|") if p.strip()]
        used_cids = sorted(set(b["cluster_id"] for b in self.blocks if b["cluster_id"] > 0))
        for idx, cid in enumerate(used_cids):
            if idx < len(parts):
                auto_default = self.get_cluster_auto_name(cid, idx + 1)
                if parts[idx] != auto_default:
                    self.custom_cluster_names[str(cid)] = parts[idx]

    def split_block_at_point(self, ix: float, iy: float) -> bool:
        """Splits a fused word block at or near (ix, iy) along the local vertical valley."""
        if self.label_map is None or not self.blocks:
            return False
        hit_block = None
        for b in self.blocks:
            if (b["x"] - 10) <= ix <= (b["x"] + b["w"] + 10) and (b["y"] - 10) <= iy <= (b["y"] + b["h"] + 10):
                hit_block = b
                break
        if not hit_block:
            return False

        bx, by, bw, bh = hit_block["x"], hit_block["y"], hit_block["w"], hit_block["h"]
        rel_x = int(round(ix - bx))
        if bw > 36 and getattr(self, "face_bin", None) is not None:
            bmask = (self.label_map == hit_block["id"])[by:by+bh, bx:bx+bw]
            f_sub = (self.face_bin[by:by+bh, bx:bx+bw] > 0) & bmask
            col_prof = f_sub.sum(axis=0).astype(np.float32)
            win_lo = max(8, rel_x - max(12, int(bw * 0.14)))
            win_hi = min(bw - 8, rel_x + max(12, int(bw * 0.14)))
            if win_hi > win_lo:
                best_rel = win_lo + int(np.argmin(col_prof[win_lo:win_hi]))
                cut_x = bx + best_rel
            else:
                cut_x = int(round(ix))
        else:
            cut_x = int(round(ix))

        self.split_block_by_line(cut_x, by - 10, cut_x, by + bh + 10)
        # Assign the right-hand split part to a new cluster so the user immediately sees 2 clusters
        right_candidates = [
            b for b in self.blocks
            if b["line_index"] == hit_block["line_index"] and abs(b["x"] - cut_x) <= 25
        ]
        if right_candidates:
            max_cid = max((b["cluster_id"] for b in self.blocks), default=1)
            right_candidates[0]["cluster_id"] = max_cid + 1
            self.normalize_cluster_ids()
        return True

    def smart_click(self, ix: float, iy: float, alt_key: bool = False, shift_key: bool = False):
        """
        Direct-Manipulation Smart Click on Preview Canvas:
        - Alt + Click on a block: Split the block vertically at the local valley around the click point.
        - Shift + Click on a block (or click the already-selected block again): Isolate that block into a new cluster.
        - Click Block A -> Click Block B: Merge Block B into Block A's cluster.
        - Click empty background: Deselect active block.
        """
        if alt_key:
            self.split_block_at_point(ix, iy)
            self.selected_block_id = None
            return

        hit_block = None
        for b in self.blocks:
            if b["x"] <= ix <= b["x"] + b["w"] and b["y"] <= iy <= b["y"] + b["h"]:
                hit_block = b
                break

        if not hit_block:
            self.selected_block_id = None
            return

        if shift_key or (self.selected_block_id == hit_block["id"]):
            # Isolate this block into its own cluster (if it shares its cluster with other blocks)
            same_cluster = [b for b in self.blocks if b["cluster_id"] == hit_block["cluster_id"]]
            if len(same_cluster) > 1:
                self.push_history()
                max_cid = max((b["cluster_id"] for b in self.blocks), default=1)
                hit_block["cluster_id"] = max_cid + 1
                self.custom_cluster_names = {}
                self.normalize_cluster_ids()
            self.selected_block_id = hit_block["id"]
            return

        if self.selected_block_id is not None:
            anchor_block = next((b for b in self.blocks if b["id"] == self.selected_block_id), None)
            if anchor_block and anchor_block["id"] != hit_block["id"]:
                if hit_block["cluster_id"] != anchor_block["cluster_id"]:
                    self.push_history()
                    hit_block["cluster_id"] = anchor_block["cluster_id"]
                    self.custom_cluster_names = {}
                    self.normalize_cluster_ids()
                # Keep anchor_block selected so the user can keep clicking words C, D to merge into the same cluster
                return

        # First click: select this block as the anchor
        self.selected_block_id = hit_block["id"]

    def split_block_by_line(self, x1: float, y1: float, x2: float, y2: float):
        if self.label_map is None:
            return
        self.push_history()
        self.custom_cluster_names = {}
        cut_mask = np.zeros((self.h, self.w), dtype=np.uint8)
        cv2.line(cut_mask, (int(x1), int(y1)), (int(x2), int(y2)), 1, thickness=6)

        hit_ids = set(np.unique(self.label_map[cut_mask > 0])) - {0}
        if not hit_ids:
            return

        new_blocks = []
        new_label_map = np.zeros_like(self.label_map)
        next_id = 1

        for b in self.blocks:
            bid = b["id"]
            bmask = (self.label_map == bid).astype(np.uint8)
            if bid not in hit_ids:
                new_label_map[bmask > 0] = next_id
                b_copy = dict(b)
                b_copy["id"] = next_id
                new_blocks.append(b_copy)
                next_id += 1
            else:
                bmask_cut = bmask.copy()
                bmask_cut[cut_mask > 0] = 0
                num_sub, sub_labels, sub_stats, _ = cv2.connectedComponentsWithStats(bmask_cut)
                valid_subs = [i for i in range(1, num_sub) if sub_stats[i, cv2.CC_STAT_AREA] >= 40]
                if len(valid_subs) <= 1:
                    new_label_map[bmask > 0] = next_id
                    b_copy = dict(b)
                    b_copy["id"] = next_id
                    new_blocks.append(b_copy)
                    next_id += 1
                else:
                    seed_sub = np.zeros_like(sub_labels)
                    for idx_s, sub_i in enumerate(valid_subs, 1):
                        seed_sub[sub_labels == sub_i] = idx_s
                    _, inds = distance_transform_edt(seed_sub == 0, return_indices=True)
                    restored = np.where(bmask > 0, seed_sub[inds[0], inds[1]], 0)

                    sub_parts = []
                    for idx_s in range(1, len(valid_subs) + 1):
                        pmask = (restored == idx_s)
                        ys, xs = np.where(pmask)
                        if len(xs) == 0:
                            continue
                        sub_parts.append((xs.min(), pmask, xs, ys))
                    sub_parts.sort(key=lambda item: item[0])

                    orig_words = (b.get("text") or "").split()
                    for idx_part, (_, pmask, xs, ys) in enumerate(sub_parts):
                        new_label_map[pmask] = next_id
                        bx, by = int(xs.min()), int(ys.min())
                        bw, bh = int(xs.max() - bx + 1), int(ys.max() - by + 1)
                        cnts, _ = cv2.findContours(pmask.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        polys = []
                        for cnt in cnts:
                            if cv2.contourArea(cnt) >= 20:
                                approx = cv2.approxPolyDP(cnt, 2.5, True)
                                polys.append([[int(p[0][0]), int(p[0][1])] for p in approx])
                        if len(orig_words) == len(sub_parts):
                            part_txt = orig_words[idx_part]
                        else:
                            part_txt = f"{b.get('text', 'Chữ')} ({idx_part+1})"
                        new_blocks.append({
                            "id": next_id,
                            "x": bx, "y": by, "w": bw, "h": bh,
                            "area": int(len(xs)),
                            "cx": round(float(xs.mean()), 1),
                            "cy": round(float(ys.mean()), 1),
                            "line_index": b["line_index"],
                            "cluster_id": b["cluster_id"],
                            "is_icon": b["is_icon"],
                            "text": part_txt,
                            "polys": polys
                        })
                        next_id += 1

        self.label_map = new_label_map
        self.blocks = new_blocks

    def compute_cluster_masks(self, assignments: dict = None, outer_padding: int = None,
                              smoothness: float = None, h_anisotropy: float = None) -> list:
        if self.label_map is None or not self.blocks:
            return []

        if outer_padding is not None:
            self.outer_padding = int(outer_padding)
        if smoothness is not None:
            self.smoothness = float(smoothness)
        if h_anisotropy is not None:
            self.h_anisotropy = float(h_anisotropy)

        cluster_blocks = {}
        for b in self.blocks:
            bid = str(b["id"])
            cid = int((assignments or {}).get(bid, b.get("cluster_id", 1)))
            b["cluster_id"] = cid
            if cid > 0:
                cluster_blocks.setdefault(cid, []).append(b["id"])

        sorted_cids = sorted(cluster_blocks.keys())
        if not sorted_cids:
            return []

        max_dim = max(self.w, self.h)
        work_scale = (1800.0 / float(max_dim)) if max_dim > 2400 else 1.0
        if work_scale < 1.0:
            ww = max(1, int(round(self.w * work_scale)))
            wh = max(1, int(round(self.h * work_scale)))
            work_lmap = cv2.resize(self.label_map.astype(np.float32), (ww, wh), interpolation=cv2.INTER_NEAREST).astype(np.int32)
        else:
            work_lmap = self.label_map

        cluster_raw_masks = []
        cluster_cores = []
        cluster_bboxes = []
        for cid in sorted_cids:
            b_ids = cluster_blocks[cid]
            nat_cmask = np.isin(self.label_map, b_ids)
            ys, xs = np.where(nat_cmask)
            if len(xs) > 0:
                cluster_bboxes.append((int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())))
            else:
                cluster_bboxes.append((0, 0, self.w, self.h))

            cmask = np.isin(work_lmap, b_ids).astype(np.uint8)
            cluster_raw_masks.append(cmask)
            other_mask = (work_lmap > 0) & (~cmask.astype(bool))
            closed = cv2.morphologyEx(cmask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (65, 19)))
            closed[other_mask] = 0
            cluster_cores.append(closed)

        # Anisotropic distance (horizontal flare priority) via OpenCV: squeeze X by `aniso`, run the fast exact
        # cv2.distanceTransform, stretch back. ~6-10x faster than scipy EDT per cluster (big win on Windows CPUs).
        aniso = max(0.65, float(self.h_anisotropy))
        wh_, ww_ = work_lmap.shape[:2]
        sw_ = max(1, int(round(ww_ * aniso)))
        dists = []
        for cm in cluster_cores:
            if not np.any(cm):
                dists.append(np.full((wh_, ww_), 1e9, dtype=np.float32))
                continue
            inv = (1 - cm).astype(np.uint8)
            if sw_ != ww_:
                inv_s = cv2.resize(inv, (sw_, wh_), interpolation=cv2.INTER_NEAREST)
                d_s = cv2.distanceTransform(inv_s, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
                dists.append(cv2.resize(d_s, (ww_, wh_), interpolation=cv2.INTER_LINEAR))
            else:
                dists.append(cv2.distanceTransform(inv, cv2.DIST_L2, cv2.DIST_MASK_PRECISE))
        dists_stack = np.stack(dists, axis=0)
        nearest_idx = np.argmin(dists_stack, axis=0)

        inv_scale = 1.0 / work_scale
        results = []
        for k, cid in enumerate(sorted_cids):
            cmask = cluster_raw_masks[k]
            other_mask = ((work_lmap > 0) & (~cmask.astype(bool))).astype(np.uint8)
            territory = ((nearest_idx == k) & (dists[k] <= self.outer_padding)).astype(np.uint8) * 255
            blur_k = max(11, int(round(self.smoothness * 7)) | 1)
            territory = cv2.GaussianBlur(territory, (blur_k, blur_k), 0)
            territory = (territory >= 128).astype(np.uint8) * 255

            # Hard core protection: guarantee GaussianBlur never clips ('lẹm') own letter strokes or bleeds ('dư') into neighbor strokes
            own_safe = cv2.dilate(cmask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
            other_safe = cv2.dilate(other_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
            territory[own_safe > 0] = 255
            territory[other_safe > 0] = 0

            contours, _ = cv2.findContours(territory, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            paths = []
            for cnt in contours:
                if cv2.contourArea(cnt) < 400:
                    continue
                eps = max(1.5, float(self.smoothness))
                approx = cv2.approxPolyDP(cnt, eps, True)
                pts = [[round(float(p[0][0]) * inv_scale, 1), round(float(p[0][1]) * inv_scale, 1)] for p in approx]
                if len(pts) < 3:
                    continue

                n_pts = len(pts)
                in_tangents = []
                out_tangents = []
                tension = 0.14
                for i in range(n_pts):
                    p_prev = pts[(i - 1) % n_pts]
                    p_next = pts[(i + 1) % n_pts]
                    tx = round((p_next[0] - p_prev[0]) * tension, 2)
                    ty = round((p_next[1] - p_prev[1]) * tension, 2)
                    out_tangents.append([tx, ty])
                    in_tangents.append([-tx, -ty])

                paths.append({
                    "vertices": pts,
                    "inTangents": in_tangents,
                    "outTangents": out_tangents
                })

            x1, y1, x2, y2 = cluster_bboxes[k]
            cx = round((x1 + x2) / 2.0, 1)
            cy = round((y1 + y2) / 2.0, 1)
            auto_name = self.get_cluster_auto_name(cid, k + 1)
            results.append({
                "cluster_id": cid,
                "name": auto_name,
                "anchor": [cx, cy],
                "bbox": [x1, y1, x2 - x1, y2 - y1],
                "paths": paths
            })

        return results

    def render_scriptui_preview(self, clusters: list = None, box_w: int = 480, box_h: int = 210) -> str:
        """Renders a crisp visual preview PNG fitted inside (box_w, box_h) with full Vietnamese Unicode labels."""
        if self.img_bgra is None:
            return ""
        if clusters is None:
            clusters = self.compute_cluster_masks()

        alpha_f = self.alpha[:, :, None] / 255.0
        bg = np.full((self.h, self.w, 3), 16, dtype=np.uint8)
        bg[::32, :] = 24
        bg[:, ::32] = 24
        vis = (self.img_bgra[:, :, :3] * alpha_f + bg * (1.0 - alpha_f)).astype(np.uint8)

        for cm in clusters:
            cid = cm["cluster_id"]
            col = BGR_PALETTE[(cid - 1) % len(BGR_PALETTE)]
            for p in cm["paths"]:
                pts = np.array(p["vertices"], dtype=np.int32).reshape((-1, 1, 2))
                cv2.polylines(vis, [pts], True, col, 3, cv2.LINE_AA)
                for pt in pts:
                    px, py = int(pt[0][0]), int(pt[0][1])
                    cv2.circle(vis, (px, py), 5, (255, 255, 255), -1, cv2.LINE_AA)
                    cv2.circle(vis, (px, py), 3, col, -1, cv2.LINE_AA)
            if cm.get("anchor"):
                ax, ay = int(cm["anchor"][0]), int(cm["anchor"][1])
                cv2.circle(vis, (ax, ay), 10, (255, 255, 255), 2, cv2.LINE_AA)
                cv2.line(vis, (ax - 15, ay), (ax + 15, ay), col, 2, cv2.LINE_AA)
                cv2.line(vis, (ax, ay - 15), (ax, ay + 15), col, 2, cv2.LINE_AA)

        for b in self.blocks:
            cid = b["cluster_id"]
            col = BGR_PALETTE[(cid - 1) % len(BGR_PALETTE)]
            bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
            is_sel = (self.selected_block_id == b["id"])
            if is_sel:
                cv2.rectangle(vis, (bx - 4, by - 4), (bx + bw + 4, by + bh + 4), (255, 255, 255), 4, cv2.LINE_AA)
            cv2.rectangle(vis, (bx, by), (bx + bw, by + bh), col, 3 if is_sel else 2, cv2.LINE_AA)

        scale = min(box_w / float(self.w), box_h / float(self.h))
        new_w = max(1, int(round(self.w * scale)))
        new_h = max(1, int(round(self.h * scale)))
        resized = cv2.resize(vis, (new_w, new_h), interpolation=cv2.INTER_AREA)

        canvas_img = np.full((box_h, box_w, 3), 12, dtype=np.uint8)
        off_x = (box_w - new_w) // 2
        off_y = (box_h - new_h) // 2
        canvas_img[off_y:off_y + new_h, off_x:off_x + new_w] = resized

        # Draw crisp Unicode Vietnamese tags AT SCREEN RESOLUTION so labels are always sharp regardless of image size
        try:
            from PIL import ImageDraw, ImageFont
            pil_canvas = Image.fromarray(cv2.cvtColor(canvas_img, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(pil_canvas)
            font_obj = None
            for fp_font in (
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "C:/Windows/Fonts/segoeuib.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
            ):
                if os.path.exists(fp_font):
                    try:
                        font_obj = ImageFont.truetype(fp_font, 11)
                        break
                    except Exception:
                        pass
            if font_obj is None:
                font_obj = ImageFont.load_default()

            for b in self.blocks:
                cid = b["cluster_id"]
                col_bgr = BGR_PALETTE[(cid - 1) % len(BGR_PALETTE)]
                col_rgb = (col_bgr[2], col_bgr[1], col_bgr[0])
                sx = int(round(off_x + b["x"] * scale))
                sy = int(round(off_y + b["y"] * scale))
                is_sel = (self.selected_block_id == b["id"])
                txt_raw = (b.get("text") or "").strip() or "ICON"
                if txt_raw == "✨":
                    txt_raw = "ICON"
                label_str = f"C{cid}: {txt_raw[:12]}"
                try:
                    tb = draw.textbbox((0, 0), label_str, font=font_obj)
                    tw = tb[2] - tb[0]
                    th = tb[3] - tb[1]
                except Exception:
                    tw = len(label_str) * 7
                    th = 11
                px1 = max(2, min(box_w - tw - 10, sx))
                py1 = max(2, sy - th - 7)
                px2 = px1 + tw + 8
                py2 = py1 + th + 5
                if is_sel:
                    draw.rectangle([px1 - 2, py1 - 2, px2 + 2, py2 + 2], fill=(255, 255, 255))
                draw.rectangle([px1, py1, px2, py2], fill=col_rgb)
                draw.text((px1 + 4, py1 + 2), label_str, fill=(12, 14, 20), font=font_obj)

            canvas_img = cv2.cvtColor(np.array(pil_canvas), cv2.COLOR_RGB2BGR)
        except Exception:
            pass

        self.preview_scale = scale
        self.preview_off_x = off_x
        self.preview_off_y = off_y

        self.preview_counter = (self.preview_counter + 1) % 8
        self.preview_png_path = str(Path(tempfile.gettempdir()) / f"ae_splitter_ui_preview_{self.preview_counter}.png")
        cv2.imwrite(self.preview_png_path, canvas_img)
        return self.preview_png_path


STATE = ImageClusterState()


# ============================================================================
# 3. PUSH TO AFTER EFFECTS (EXTENDSCRIPT GENERATOR)
# ============================================================================

def build_push_jsx_code(file_path: str, clusters: list, feather: float = 5.0,
                        expansion: float = 0.0, center_anchor: bool = True,
                        custom_names: dict = None, out_path: str = "",
                        layer_name: str = "", source_name: str = "") -> str:
    file_path_js = file_path.replace("\\", "/")
    base_name = os.path.basename(file_path_js)

    payload_clusters = []
    for idx, c in enumerate(clusters):
        cid = str(c["cluster_id"])
        cname = ((custom_names or {}).get(cid) or c.get("name") or f"Cụm {idx+1}").strip()
        payload_clusters.append({
            "name": cname,
            "anchor": c["anchor"],
            "paths": c["paths"],
            "label": ((idx % 15) + 1)
        })

    payload_json = json.dumps({
        "filePath": file_path_js,
        "baseName": base_name,
        "layerName": layer_name or "",
        "sourceName": source_name or "",
        "feather": float(feather),
        "expansion": float(expansion),
        "centerAnchor": bool(center_anchor),
        "clusters": payload_clusters
    }, ensure_ascii=False)

    return f"""
    (function() {{
        var outPath = "{out_path}";
        var data = {payload_json};

        function matchLayerFile(l) {{
            if (!l || !l.source || !l.source.file) return false;
            var lp = l.source.file.fsName.replace(/\\\\/g, "/");
            var fileMatched = (lp === data.filePath || l.source.file.name === data.baseName);
            if (!fileMatched) return false;
            if (data.sourceName && l.source.name) {{
                return (l.source.name === data.sourceName);
            }}
            if (data.layerName && lp.toLowerCase().indexOf(".psd") !== -1) {{
                return (l.name === data.layerName || (l.source.name && l.source.name.indexOf(data.layerName + "/") === 0));
            }}
            return true;
        }}
        function isSplitLayer(l) {{
            return (l && (l.comment === "AE_TEXT_CLUSTER_SPLIT" || l.name.indexOf("[Cụm ") === 0));
        }}

        app.beginUndoGroup("Auto Split Text Clusters");
        try {{
            var comp = app.project.activeItem;
            var baseLayer = null;

            if (comp && (comp instanceof CompItem)) {{
                if (comp.selectedLayers && comp.selectedLayers.length > 0) {{
                    for (var s = 0; s < comp.selectedLayers.length; s++) {{
                        var sl = comp.selectedLayers[s];
                        if (matchLayerFile(sl) && !isSplitLayer(sl)) {{
                            baseLayer = sl;
                            break;
                        }}
                    }}
                }}
                if (!baseLayer) {{
                    for (var i = 1; i <= comp.numLayers; i++) {{
                        var l = comp.layer(i);
                        if (matchLayerFile(l) && !isSplitLayer(l)) {{
                            baseLayer = l;
                            break;
                        }}
                    }}
                }}
            }}

            if (!baseLayer) {{
                for (var c = 1; c <= app.project.numItems; c++) {{
                    var it = app.project.item(c);
                    if (it instanceof CompItem) {{
                        for (var i2 = 1; i2 <= it.numLayers; i2++) {{
                            var l2 = it.layer(i2);
                            if (matchLayerFile(l2) && !isSplitLayer(l2)) {{
                                comp = it;
                                baseLayer = l2;
                                comp.openInViewer();
                                break;
                            }}
                        }}
                        if (baseLayer) break;
                    }}
                }}
            }}

            if (!baseLayer) {{
                var footageItem = null;
                for (var j = 1; j <= app.project.numItems; j++) {{
                    var item = app.project.item(j);
                    if (item instanceof FootageItem && item.file) {{
                        var ip = item.file.fsName.replace(/\\\\/g, "/");
                        if (ip === data.filePath || item.file.name === data.baseName) {{
                            if (!data.sourceName || item.name === data.sourceName) {{
                                footageItem = item;
                                break;
                            }}
                        }}
                    }}
                }}
                if (!footageItem) {{
                    var io = new ImportOptions(new File(data.filePath));
                    footageItem = app.project.importFile(io);
                }}
                if (!comp || !(comp instanceof CompItem)) {{
                    comp = app.project.items.addComp(
                        footageItem.name + " Comp",
                        Math.max(1080, footageItem.width),
                        Math.max(1920, footageItem.height),
                        1.0, 10.0, 30.0
                    );
                    comp.openInViewer();
                }}
                baseLayer = comp.layers.add(footageItem);
            }}

            // Remove previous split layers belonging to this same source
            for (var k = comp.numLayers; k >= 1; k--) {{
                var lk = comp.layer(k);
                if (lk !== baseLayer && lk.source === baseLayer.source && isSplitLayer(lk)) {{
                    lk.remove();
                }}
            }}

            baseLayer.enabled = true;
            var baseMasks = baseLayer.property("ADBE Mask Parade");
            if (baseMasks) {{
                for (var m = baseMasks.numProperties; m >= 1; m--) {{
                    baseMasks.property(m).remove();
                }}
            }}

            var origAnchor = baseLayer.property("ADBE Transform Group").property("ADBE Anchor Point").value;
            var origPos = baseLayer.property("ADBE Transform Group").property("ADBE Position").value;
            var origScale = baseLayer.property("ADBE Transform Group").property("ADBE Scale").value;

            var createdCount = 0;
            for (var cIdx = 0; cIdx < data.clusters.length; cIdx++) {{
                var cInfo = data.clusters[cIdx];
                var newLayer = baseLayer.duplicate();
                newLayer.moveBefore(baseLayer);
                newLayer.name = cInfo.name;
                newLayer.comment = "AE_TEXT_CLUSTER_SPLIT";
                newLayer.label = cInfo.label;
                newLayer.enabled = true;
                newLayer.selected = true;

                var maskParade = newLayer.property("ADBE Mask Parade");
                for (var m2 = maskParade.numProperties; m2 >= 1; m2--) {{
                    maskParade.property(m2).remove();
                }}
                for (var pIdx = 0; pIdx < cInfo.paths.length; pIdx++) {{
                    var pData = cInfo.paths[pIdx];
                    var maskAtom = maskParade.addProperty("ADBE Mask Atom");
                    maskAtom.name = "Cluster Mask " + (pIdx + 1);
                    maskAtom.maskMode = MaskMode.ADD;

                    var myShape = new Shape();
                    myShape.vertices = pData.vertices;
                    myShape.inTangents = pData.inTangents;
                    myShape.outTangents = pData.outTangents;
                    myShape.closed = true;

                    maskAtom.property("ADBE Mask Shape").setValue(myShape);
                    maskAtom.property("ADBE Mask Feather").setValue([data.feather, data.feather]);
                    maskAtom.property("ADBE Mask Offset").setValue(data.expansion);
                }}

                if (data.centerAnchor && cInfo.anchor && cInfo.anchor.length >= 2) {{
                    var cx = cInfo.anchor[0];
                    var cy = cInfo.anchor[1];
                    var sx = origScale[0] / 100.0;
                    var sy = origScale[1] / 100.0;
                    var newAx = [cx, cy];
                    var newPx = [
                        origPos[0] + (cx - origAnchor[0]) * sx,
                        origPos[1] + (cy - origAnchor[1]) * sy
                    ];
                    if (origAnchor.length > 2) newAx.push(origAnchor[2]);
                    if (origPos.length > 2) newPx.push(origPos[2]);
                    newLayer.property("ADBE Transform Group").property("ADBE Anchor Point").setValue(newAx);
                    newLayer.property("ADBE Transform Group").property("ADBE Position").setValue(newPx);
                }}
                createdCount++;
            }}

            baseLayer.enabled = false;
            baseLayer.selected = false;

            if (outPath) {{
                var outFile = new File(outPath);
                outFile.encoding = "UTF-8";
                outFile.open("w");
                outFile.write('{{"ok": true, "created": ' + createdCount + ', "compName": "' + comp.name.replace(/"/g, '\\\\"') + '"}}');
                outFile.close();
            }}
        }} catch (err) {{
            if (outPath) {{
                var outFile2 = new File(outPath);
                outFile2.encoding = "UTF-8";
                outFile2.open("w");
                outFile2.write('{{"ok": false, "error": "' + err.toString().replace(/"/g, '\\\\"') + '"}}');
                outFile2.close();
            }}
        }} finally {{
            app.endUndoGroup();
        }}
    }})();
    """


def push_clusters_to_ae(file_path: str, clusters: list, feather: float = 5.0,
                        expansion: float = 0.0, center_anchor: bool = True,
                        custom_names: dict = None, layer_name: str = "",
                        source_name: str = "") -> dict:
    out_path = str(Path(tempfile.gettempdir()) / "ae_splitter_out.json").replace("\\", "/")
    jsx_code = build_push_jsx_code(
        file_path=file_path,
        clusters=clusters,
        feather=feather,
        expansion=expansion,
        center_anchor=center_anchor,
        custom_names=custom_names,
        out_path=out_path,
        layer_name=layer_name,
        source_name=source_name
    )
    raw = run_ae_jsx(jsx_code)
    bring_app_to_front("Adobe After Effects")
    if not raw:
        return {"ok": False, "error": "Không nhận được phản hồi từ After Effects."}
    try:
        return json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": f"Lỗi đọc kết quả từ AE: {e}"}


# ============================================================================
# 4. NATIVE AFTER EFFECTS SCRIPTUI PLUGIN GENERATOR
# ============================================================================

def build_native_scriptui_jsx(port: int = 8765) -> str:
    py_exe = str(sys.executable).replace("\\", "/")
    script_py = str(Path(__file__).resolve()).replace("\\", "/")
    req_json_path = str(Path(tempfile.gettempdir()) / "ae_splitter_req.json").replace("\\", "/")

    return f"""// ============================================================================
// AE Text Cluster & Mask Splitter - Native After Effects Plugin Panel
// ============================================================================
(function(thisObj) {{
    var PORT = {port};
    var PY_EXE = "{py_exe}";
    var SCRIPT_PY = "{script_py}";
    var REQ_FILE = "{req_json_path}";
    var IS_MAC = ($.os.indexOf("Mac") !== -1);
    var activeCluster = 1;
    var siblingFiles = [];
    var isUpdatingDropdown = false;

    // In-process HTTP GET over ExtendScript Socket: no curl / cmd.exe process spawn per click (system.callSystem
    // costs ~150-400 ms + a console flash on Windows, and decodes output in the ANSI codepage which garbles Vietnamese).
    function httpGet(pathQuery, timeoutSec) {{
        var s = new Socket();
        s.timeout = timeoutSec || 30;
        s.encoding = "BINARY";
        if (!s.open("127.0.0.1:" + PORT, "BINARY")) return null;
        s.write("GET " + pathQuery + " HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\nConnection: close\\r\\n\\r\\n");
        var raw = "";
        while (s.connected && !s.eof) {{
            var chunk = s.read(65536);
            if (!chunk) break;
            raw += chunk;
        }}
        s.close();
        var sep = raw.indexOf("\\r\\n\\r\\n");
        if (sep === -1) return null;
        var body = raw.substring(sep + 4);
        try {{ body = decodeURIComponent(escape(body)); }} catch (eDec) {{}}
        return body;
    }}

    var serverAlive = false;
    function ensureServer() {{
        if (serverAlive) return;
        var ping = httpGet("/api/ping", 2);
        if (ping && ping.indexOf("ok") !== -1) {{ serverAlive = true; return; }}
        if (IS_MAC) {{
            system.callSystem("'" + PY_EXE + "' '" + SCRIPT_PY + "' --port " + PORT + " --no-browser >/tmp/ae_splitter.log 2>&1 &");
        }} else {{
            // Launch detached through a .bat + File.execute(): system.callSystem('start /b ...') keeps the child's
            // stdout pipe open and can freeze AE until the server exits. pythonw.exe = no console window.
            var pyw = PY_EXE.replace(/python\\.exe$/i, "pythonw.exe");
            if (!new File(pyw).exists) pyw = PY_EXE;
            var bat = new File(Folder.temp.fsName + "/ae_splitter_start.bat");
            bat.encoding = "UTF-8";
            bat.lineFeed = "Windows";
            bat.open("w");
            bat.writeln("@echo off");
            bat.writeln("chcp 65001 >nul");
            bat.writeln('start "" "' + pyw.replace(/\\//g, "\\\\") + '" "' + SCRIPT_PY.replace(/\\//g, "\\\\") + '" --port ' + PORT + ' --no-browser');
            bat.close();
            bat.execute();
        }}
        for (var w = 0; w < 25; w++) {{
            $.sleep(400);
            ping = httpGet("/api/ping", 2);
            if (ping && ping.indexOf("ok") !== -1) {{ serverAlive = true; return; }}
        }}
    }}

    function writeReqJson(objStr) {{
        var f = new File(REQ_FILE);
        f.encoding = "UTF-8";
        f.open("w");
        f.write(objStr);
        f.close();
    }}

    function callApi(endpoint, query) {{
        ensureServer();
        var pq = endpoint + (query ? ("?" + query) : "");
        var res = httpGet(pq, 60);
        if (res === null) {{
            serverAlive = false;
            ensureServer();
            res = httpGet(pq, 60);
        }}
        try {{
            return eval("(" + res + ")");
        }} catch (e) {{
            return {{ ok: false, error: res || "Lỗi kết nối server" }};
        }}
    }}

    function detectSelectedLayerOrItemInAE() {{
        function isImageFile(fileObj) {{
            if (!fileObj) return false;
            var nm = fileObj.name.toLowerCase();
            return (nm.indexOf(".png") !== -1 || nm.indexOf(".psd") !== -1 || nm.indexOf(".jpg") !== -1 || nm.indexOf(".jpeg") !== -1 || nm.indexOf(".webp") !== -1 || nm.indexOf(".tif") !== -1 || nm.indexOf(".tiff") !== -1 || nm.indexOf(".bmp") !== -1);
        }}
        function isImageLayer(l) {{
            return (l && l.source && l.source.file && isImageFile(l.source.file));
        }}
        function isSplitLayer(l) {{
            return (l && (l.comment === "AE_TEXT_CLUSTER_SPLIT" || l.name.indexOf("[Cụm ") === 0));
        }}
        function makeLayerInfo(l, compName) {{
            return {{
                path: l.source.file.fsName,
                layerName: l.name || "",
                sourceName: (l.source && l.source.name) ? l.source.name : "",
                layerWidth: l.width || 0,
                layerHeight: l.height || 0,
                label: l.name + " (" + compName + ")"
            }};
        }}
        function makeFootageInfo(item) {{
            var lName = item.name || "";
            if (lName.indexOf("/") !== -1) lName = lName.split("/")[0];
            return {{
                path: item.file.fsName,
                layerName: lName,
                sourceName: item.name || "",
                layerWidth: item.width || 0,
                layerHeight: item.height || 0,
                label: item.name + " (Project Panel)"
            }};
        }}

        var act = app.project.activeItem;

        if (act && (act instanceof CompItem)) {{
            if (act.selectedLayers && act.selectedLayers.length > 0) {{
                for (var s = 0; s < act.selectedLayers.length; s++) {{
                    var sl = act.selectedLayers[s];
                    if (isImageLayer(sl)) {{
                        return makeLayerInfo(sl, act.name);
                    }}
                    if (sl.source && (sl.source instanceof CompItem)) {{
                        var sub = sl.source;
                        for (var k = 1; k <= sub.numLayers; k++) {{
                            if (isImageLayer(sub.layer(k))) {{
                                return makeLayerInfo(sub.layer(k), sub.name);
                            }}
                        }}
                    }}
                }}
            }}
            for (var i = 1; i <= act.numLayers; i++) {{
                var l = act.layer(i);
                if (isImageLayer(l) && !isSplitLayer(l)) {{
                    return makeLayerInfo(l, act.name);
                }}
            }}
            for (var i2 = 1; i2 <= act.numLayers; i2++) {{
                var l2 = act.layer(i2);
                if (isImageLayer(l2)) {{
                    return makeLayerInfo(l2, act.name);
                }}
            }}
        }}

        if (act && (act instanceof FootageItem) && act.file && isImageFile(act.file)) {{
            return makeFootageInfo(act);
        }}

        if (app.project.selection && app.project.selection.length > 0) {{
            for (var p = 0; p < app.project.selection.length; p++) {{
                var sel = app.project.selection[p];
                if (sel instanceof FootageItem && sel.file && isImageFile(sel.file)) {{
                    return makeFootageInfo(sel);
                }}
                if (sel instanceof CompItem) {{
                    for (var j = 1; j <= sel.numLayers; j++) {{
                        var lj = sel.layer(j);
                        if (isImageLayer(lj) && !isSplitLayer(lj)) {{
                            sel.openInViewer();
                            return makeLayerInfo(lj, sel.name);
                        }}
                    }}
                }}
            }}
        }}

        return null;
    }}

    function buildUI(thisObj) {{
        var win = (thisObj instanceof Panel)
            ? thisObj
            : new Window("palette", "✨ AE Text Cluster & Mask Splitter", undefined, {{ resizeable: true }});

        win.orientation = "column";
        win.alignChildren = ["fill", "fill"];
        win.spacing = 2;
        win.margins = 4;

        // Scrollable Viewport Row (Content + Vertical Scrollbar)
        var mainArea = win.add("group");
        mainArea.orientation = "row";
        mainArea.alignment = ["fill", "fill"];
        mainArea.alignChildren = ["fill", "top"];
        mainArea.spacing = 4;
        mainArea.margins = 0;

        var viewport = mainArea.add("group");
        viewport.orientation = "stack";
        viewport.alignment = ["fill", "fill"];
        viewport.alignChildren = ["left", "top"];
        viewport.margins = 0;

        var contentGrp = viewport.add("group");
        contentGrp.orientation = "column";
        contentGrp.alignChildren = ["fill", "top"];
        contentGrp.spacing = 4;
        contentGrp.margins = 2;

        var vScroll = mainArea.add("scrollbar", undefined, 0, 0, 240);
        vScroll.alignment = ["right", "fill"];
        vScroll.preferredSize = [15, 360];
        vScroll.stepdelta = 25;
        vScroll.jumpdelta = 75;

        // Tier 1A: Source Layer & File Selector
        var selRow = contentGrp.add("group");
        selRow.orientation = "row";
        selRow.alignChildren = ["left", "center"];
        selRow.spacing = 5;

        var btnFetch = selRow.add("button", undefined, "🔄 Lấy Layer AE");
        btnFetch.preferredSize = [125, 25];
        var ddFiles = selRow.add("dropdownlist", undefined, ["(Chọn nhanh file ảnh...)"]);
        ddFiles.preferredSize = [355, 25];

        // Tier 1B: 1-Click Presets + Undo
        var presetRow = contentGrp.add("group");
        presetRow.orientation = "row";
        presetRow.alignChildren = ["fill", "center"];
        presetRow.spacing = 4;

        var btnLines = presetRow.add("button", undefined, "📑 Theo Dòng");
        btnLines.preferredSize.height = 25;
        var btnWords = presetRow.add("button", undefined, "🔤 Từng Từ");
        btnWords.preferredSize.height = 25;
        var btnIcons = presetRow.add("button", undefined, "✨ Tách Icon");
        btnIcons.preferredSize.height = 25;
        var btnUndo = presetRow.add("button", undefined, "↩️ Undo");
        btnUndo.preferredSize = [72, 25];

        // Tier 2: Direct-Manipulation Preview Canvas
        var imgGroup = contentGrp.add("panel", undefined, "Preview (Click 2 từ = Gộp cụm | Click lại/Shift = Tách riêng | Alt+Click = Cắt đôi):");
        imgGroup.orientation = "column";
        imgGroup.alignChildren = ["center", "center"];
        imgGroup.margins = [4, 10, 4, 4];

        var previewImg = imgGroup.add("image", undefined);
        previewImg.preferredSize = [480, 210];

        // Tier 3: 1-Line Editable Layer Names
        var nameRow = contentGrp.add("group");
        nameRow.orientation = "row";
        nameRow.alignChildren = ["left", "center"];
        nameRow.spacing = 5;

        var lblName = nameRow.add("statictext", undefined, "Tên Layer:");
        lblName.preferredSize.width = 60;
        var txtNames = nameRow.add("edittext", undefined, "");
        txtNames.preferredSize = [420, 24];
        txtNames.helpTip = "Tên các Layer sẽ tạo trong AE (ngăn cách bởi dấu |). Bạn có thể sửa trực tiếp tại đây!";

        // Collapsible Advanced Settings (hidden by default to keep UI ultra-clean)
        var paramPanel = contentGrp.add("panel", undefined, "Thông số nâng cao (Mask & Anchor Point):");
        paramPanel.orientation = "column";
        paramPanel.alignChildren = ["fill", "top"];
        paramPanel.spacing = 3;
        paramPanel.margins = [8, 10, 8, 5];
        paramPanel.visible = false;
        paramPanel.maximumSize.height = 0;

        var padRow = paramPanel.add("group");
        padRow.orientation = "row";
        padRow.add("statictext", undefined, "Độ rộng Glow (Padding):");
        var sldPad = padRow.add("slider", undefined, 85, 25, 180);
        sldPad.preferredSize.width = 200;
        var lblPad = padRow.add("statictext", undefined, "85 px");
        lblPad.characters = 6;

        var fthRow = paramPanel.add("group");
        fthRow.orientation = "row";
        fthRow.add("statictext", undefined, "Mask Feather (AE):        ");
        var sldFth = fthRow.add("slider", undefined, 5, 0, 25);
        sldFth.preferredSize.width = 200;
        var lblFth = fthRow.add("statictext", undefined, "5 px");
        lblFth.characters = 6;

        var chkAnchor = paramPanel.add("checkbox", undefined, "Tự động đặt tâm Anchor Point (+) vào chính giữa từng cụm chữ");
        chkAnchor.value = true;

        // Tier 4: Primary Action Row
        var botRow = contentGrp.add("group");
        botRow.orientation = "row";
        botRow.alignChildren = ["fill", "center"];
        botRow.spacing = 5;

        var btnPush = botRow.add("button", undefined, "🚀 TÁCH LAYER VÀO AE");
        btnPush.preferredSize = [370, 30];

        var btnGear = botRow.add("button", undefined, "⚙️");
        btnGear.preferredSize = [46, 30];
        btnGear.helpTip = "Ẩn/Hiện thông số nâng cao (Padding, Feather, Anchor Point)";

        var btnWeb = botRow.add("button", undefined, "🌐");
        btnWeb.preferredSize = [46, 30];
        btnWeb.helpTip = "Mở giao diện Web đầy đủ (Dao cắt tự do)";

        var statusTxt = contentGrp.add("statictext", undefined, "Sẵn sàng. Click chọn Layer trên AE để bắt đầu.");
        statusTxt.characters = 70;

        var natW = 0, natH = 0;

        function applyScrollOffset() {{
            contentGrp.location = [0, -Math.round(vScroll.value)];
        }}

        // Measure the natural (unclipped) content size from the visible children laid out by ScriptUI
        function measureContent() {{
            var h = 0, w = 0;
            for (var i = 0; i < contentGrp.children.length; i++) {{
                var c = contentGrp.children[i];
                if (!c.visible || !c.size) continue;
                h = Math.max(h, c.location[1] + c.size[1]);
                w = Math.max(w, c.location[0] + c.size[0]);
            }}
            natH = h + 6;
            natW = w + 4;
        }}

        // Manually clip the viewport to the docked panel size (the layout manager would otherwise grow it to fit
        // the content, so the panel gets cropped and the scrollbar has nothing to scroll)
        function updateScroll() {{
            if (!win.size) return;
            if (!natH) measureContent();
            var M = 4, SB = 15;
            var W = win.size[0], H = win.size[1];
            var viewW = Math.max(60, W - 2 * M - SB - 3);
            var viewH = Math.max(60, H - 2 * M);
            mainArea.location = [M, M];
            mainArea.size = [W - 2 * M, viewH];
            viewport.location = [0, 0];
            viewport.size = [viewW, viewH];
            contentGrp.size = [Math.max(natW, viewW), natH];
            vScroll.location = [W - 2 * M - SB, 0];
            vScroll.size = [SB, viewH];
            var maxY = Math.max(0, natH - viewH);
            vScroll.maxvalue = Math.max(1, maxY);
            vScroll.jumpdelta = Math.max(30, Math.round(viewH * 0.8));
            vScroll.enabled = (maxY > 0);
            if (vScroll.value > maxY) vScroll.value = maxY;
            if (vScroll.value < 0) vScroll.value = 0;
            applyScrollOffset();
        }}

        vScroll.onChanging = vScroll.onChange = function() {{
            applyScrollOffset();
        }};

        win.onResizing = win.onResize = function() {{
            this.layout.resize();
            updateScroll();
        }};

        try {{
            win.addEventListener("mousewheel", function(e) {{
                var d = (e.detail || e.wheelDelta || 0);
                var step = (d > 0) ? -28 : 28;
                vScroll.value = Math.max(0, Math.min(vScroll.maxvalue, vScroll.value + step));
                applyScrollOffset();
            }});
        }} catch (eWheel) {{}}

        function refreshPreview(res) {{
            if (res && res.ok && res.previewPath) {{
                var f = new File(res.previewPath);
                if (f.exists) {{
                    previewImg.image = f;
                }}
                if (res.files && res.files.length > 0) {{
                    isUpdatingDropdown = true;
                    ddFiles.removeAll();
                    siblingFiles = res.files;
                    var selIdx = 0;
                    for (var i = 0; i < siblingFiles.length; i++) {{
                        var parts = siblingFiles[i].split("/");
                        var bname = parts[parts.length - 1];
                        ddFiles.add("item", bname);
                        if (res.filePath && siblingFiles[i] === res.filePath) {{
                            selIdx = i;
                        }}
                    }}
                    ddFiles.selection = selIdx;
                    isUpdatingDropdown = false;
                }}
                var namesStr = (res.clusterNames && res.clusterNames.length) ? res.clusterNames.join(" | ") : "";
                txtNames.text = namesStr;
                var selHint = res.selectedText ? (" [Đang chọn: " + res.selectedText + " -> Click từ khác để gộp, hoặc click lại để tách riêng]") : "";
                statusTxt.text = "✅ " + (res.clusterCount || 0) + " Cụm (" + (res.blockCount || 0) + " từ)" + selHint;
            }} else if (res && res.error) {{
                statusTxt.text = "⚠️ " + res.error;
            }}
        }}

        function escJson(s) {{
            return String(s || "").replace(/\\\\/g, "/").replace(/"/g, '\\\\"');
        }}

        function loadFileByPath(fp, info) {{
            var cleanPath = escJson(fp);
            var lName = info ? escJson(info.layerName) : "";
            var sName = info ? escJson(info.sourceName) : "";
            var lw = (info && info.layerWidth) ? info.layerWidth : 0;
            var lh = (info && info.layerHeight) ? info.layerHeight : 0;
            writeReqJson('{{"file": "' + cleanPath + '", "layerName": "' + lName + '", "sourceName": "' + sName + '", "layerWidth": ' + lw + ', "layerHeight": ' + lh + '}}');
            var res = callApi("/api/su_init_from_req", "");
            refreshPreview(res);
        }}

        function loadCurrentFromAE() {{
            var info = detectSelectedLayerOrItemInAE();
            if (!info || !info.path) {{
                statusTxt.text = "⚠️ Hãy click chọn 1 Layer ảnh/PSD trên Timeline hoặc trong Project Panel!";
                return;
            }}
            loadFileByPath(info.path, info);
        }}

        btnFetch.onClick = function() {{
            loadCurrentFromAE();
        }};

        ddFiles.onChange = function() {{
            if (isUpdatingDropdown || !ddFiles.selection) return;
            var idx = ddFiles.selection.index;
            if (idx >= 0 && idx < siblingFiles.length) {{
                loadFileByPath(siblingFiles[idx]);
            }}
        }};

        btnLines.onClick = function() {{
            var res = callApi("/api/su_preset", "mode=lines");
            refreshPreview(res);
        }};

        btnWords.onClick = function() {{
            var res = callApi("/api/su_preset", "mode=words");
            refreshPreview(res);
        }};

        btnIcons.onClick = function() {{
            var res = callApi("/api/su_preset", "mode=lines_icons");
            refreshPreview(res);
        }};

        btnUndo.onClick = function() {{
            var res = callApi("/api/su_undo", "");
            refreshPreview(res);
        }};

        btnGear.onClick = function() {{
            var showAdv = !paramPanel.visible;
            paramPanel.visible = showAdv;
            paramPanel.maximumSize.height = showAdv ? 220 : 0;
            // Re-layout ONLY the scrollable content (never win.layout.layout(true): that re-runs the layout
            // manager on the whole panel and overwrites the manually clipped viewport / scrollbar bounds).
            // Give the content group room first so ScriptUI doesn't squeeze rows while re-laying them out.
            var cw = (contentGrp.size && contentGrp.size[0]) ? contentGrp.size[0] : 500;
            contentGrp.size = [cw, 2000];
            contentGrp.layout.layout(true);
            measureContent();
            if (showAdv) {{
                // Scroll so the newly opened settings + action row are visible
                vScroll.value = Math.max(0, natH - ((win.size && win.size[1]) ? win.size[1] - 8 : natH));
            }}
            updateScroll();
        }};

        previewImg.addEventListener("mousedown", function(e) {{
            var x = e.clientX || e.localX || 0;
            var y = e.clientY || e.localY || 0;
            var kb = ScriptUI.environment.keyboardState;
            var alt = (e.altKey || (kb && kb.altKey)) ? "1" : "0";
            var shift = (e.shiftKey || (kb && kb.shiftKey)) ? "1" : "0";
            var res = callApi("/api/su_click", "x=" + x + "&y=" + y + "&alt=" + alt + "&shift=" + shift);
            refreshPreview(res);
        }});

        sldPad.onChanging = function() {{
            lblPad.text = Math.round(this.value) + " px";
        }};
        sldPad.onChange = function() {{
            var res = callApi("/api/su_params", "padding=" + Math.round(this.value));
            refreshPreview(res);
        }};

        sldFth.onChanging = function() {{
            lblFth.text = Math.round(this.value) + " px";
        }};

        btnPush.onClick = function() {{
            statusTxt.text = "⏳ Đang tách Layer và đặt tên theo chữ trong After Effects...";
            var fth = Math.round(sldFth.value);
            var anc = chkAnchor.value ? "1" : "0";
            writeReqJson('{{"customPipeNames": "' + escJson(txtNames.text) + '"}}');
            var res = callApi("/api/su_push_json", "feather=" + fth + "&anchor=" + anc);
            if (res && res.ok && res.jsxPath) {{
                $.evalFile(new File(res.jsxPath));
                var namesStr = (res.clusterNames && res.clusterNames.length) ? res.clusterNames.join(" | ") : (res.clusterCount + " Layer");
                statusTxt.text = "✅ Đã tạo: " + namesStr;
            }} else {{
                statusTxt.text = "❌ Lỗi: " + (res ? res.error : "Không tạo được Mask");
            }}
        }};

        btnWeb.onClick = function() {{
            ensureServer();
            if (IS_MAC) {{
                system.callSystem("open 'http://127.0.0.1:" + PORT + "'");
            }} else {{
                system.callSystem('cmd.exe /c start "" "http://127.0.0.1:' + PORT + '"');
            }}
        }};

        win.layout.layout(true);
        updateScroll();
        loadCurrentFromAE();
        return win;
    }}

    if ($.global.aeTextSplitterWin && $.global.aeTextSplitterWin instanceof Window) {{
        try {{ $.global.aeTextSplitterWin.close(); }} catch (e) {{}}
    }}
    var myWin = buildUI(thisObj);
    if (myWin instanceof Window) {{
        $.global.aeTextSplitterWin = myWin;
        myWin.center();
        myWin.show();
    }}
}})(this);
"""


# ============================================================================
# 5. INTERACTIVE HTML5 CANVAS PREVIEW UI (RESPONSIVE FOR CEP PANEL & WEB)
# ============================================================================

HTML_UI = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<title>AE Text Cluster & Mask Splitter</title>
<style>
  :root {
    --bg: #0f1117;
    --panel: #181b24;
    --panel-2: #212634;
    --border: #2d3346;
    --text: #eef1f8;
    --muted: #949cb2;
    --accent: #3b82f6;
    --accent-hover: #2563eb;
    --success: #10b981;
    --danger: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: #12151e; }
  ::-webkit-scrollbar-thumb { background: #333b52; border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: #4b5678; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    overflow-x: hidden;
    overflow-y: auto;
    user-select: none;
  }
  header {
    min-height: 52px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    padding: 8px 14px;
    gap: 8px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 700;
    font-size: 14px;
  }
  .badge {
    background: rgba(59,130,246,0.18);
    color: #60a5fa;
    border: 1px solid rgba(59,130,246,0.35);
    padding: 2px 8px;
    border-radius: 99px;
    font-size: 11px;
    font-weight: 600;
  }
  .top-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    min-width: 240px;
    max-width: 720px;
  }
  select, input[type="text"] {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 12.5px;
    outline: none;
  }
  select { flex: 1; cursor: pointer; min-width: 120px; }
  button {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 11px;
    border-radius: 6px;
    font-size: 12.5px;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: all 0.15s ease;
    white-space: nowrap;
  }
  button:hover { background: #2b3245; border-color: #434c66; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { background: var(--accent-hover); }
  button.success {
    background: linear-gradient(135deg, #059669, #10b981);
    border-color: #10b981;
    color: #fff;
    padding: 8px 14px;
    font-size: 13px;
    box-shadow: 0 4px 14px rgba(16,185,129,0.28);
  }
  button.success:hover { filter: brightness(1.08); }
  button.active-tool {
    background: rgba(239,68,68,0.2);
    border-color: #ef4444;
    color: #fca5a5;
  }
  main {
    flex: 1;
    display: grid;
    grid-template-columns: 310px 1fr 290px;
    min-height: 0;
  }
  @media (max-width: 920px) {
    body { overflow-y: auto; height: auto; }
    main {
      display: flex;
      flex-direction: column;
    }
    .canvas-wrap { min-height: 360px; order: -1; }
    .sidebar, .sidebar-right { border: none; border-top: 1px solid var(--border); }
  }
  .sidebar {
    background: var(--panel);
    border-right: 1px solid var(--border);
    padding: 14px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .sidebar-right {
    background: var(--panel);
    border-left: 1px solid var(--border);
    padding: 14px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .section-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    font-weight: 700;
    margin-bottom: 8px;
  }
  .preset-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
  }
  .preset-grid button {
    justify-content: center;
    padding: 7px;
    font-size: 12px;
  }
  .cluster-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .cluster-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 9px;
    background: var(--panel-2);
    border: 2px solid transparent;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.12s;
  }
  .cluster-item.active {
    border-color: #fff;
    background: #283044;
    box-shadow: 0 0 0 2px rgba(59,130,246,0.35);
  }
  .color-dot {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    flex-shrink: 0;
  }
  .cluster-item input {
    background: transparent;
    border: 1px solid transparent;
    color: var(--text);
    font-size: 12.5px;
    font-weight: 600;
    padding: 2px 5px;
    width: 100%;
    border-radius: 4px;
  }
  .cluster-item input:focus {
    background: var(--bg);
    border-color: var(--border);
  }
  .cluster-count {
    font-size: 11px;
    color: var(--muted);
    background: rgba(255,255,255,0.06);
    padding: 2px 7px;
    border-radius: 99px;
    flex-shrink: 0;
  }
  .canvas-wrap {
    position: relative;
    background-color: #090b10;
    background-image:
      linear-gradient(45deg, #12151e 25%, transparent 25%),
      linear-gradient(-45deg, #12151e 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #12151e 75%),
      linear-gradient(-45deg, transparent 75%, #12151e 75%);
    background-size: 24px 24px;
    background-position: 0 0, 0 12px, 12px -12px, -12px 0;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }
  canvas {
    max-width: 96%;
    max-height: 94%;
    box-shadow: 0 12px 40px rgba(0,0,0,0.65);
    border: 1px solid #262b3d;
    border-radius: 6px;
    cursor: pointer;
  }
  .canvas-hud {
    position: absolute;
    bottom: 12px;
    left: 50%;
    transform: translateX(-50%);
    background: rgba(15, 17, 23, 0.88);
    backdrop-filter: blur(8px);
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 99px;
    font-size: 11.5px;
    color: var(--muted);
    display: flex;
    gap: 10px;
    pointer-events: none;
    white-space: nowrap;
  }
  .canvas-hud b { color: #fff; }
  .control-group {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin-bottom: 10px;
  }
  .control-header {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
  }
  .control-header span:last-child {
    color: #60a5fa;
    font-weight: 600;
  }
  input[type="range"] {
    width: 100%;
    accent-color: var(--accent);
  }
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    cursor: pointer;
    margin-top: 4px;
  }
  .status-box {
    padding: 10px 12px;
    border-radius: 8px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    font-size: 12px;
    line-height: 1.45;
    color: var(--muted);
  }
  .status-box.ok { border-color: rgba(16,185,129,0.4); color: #6ee7b7; background: rgba(16,185,129,0.08); }
  .status-box.err { border-color: rgba(239,68,68,0.4); color: #fca5a5; background: rgba(239,68,68,0.08); }
  .help-list {
    font-size: 11.5px;
    color: var(--muted);
    line-height: 1.55;
    padding-left: 16px;
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <span>✨ AE Text Cluster Splitter</span>
    <span class="badge">Auto OCR + Voronoi Seam</span>
  </div>
  <div class="top-actions">
    <button onclick="fetchFromAE()" title="Đọc layer PNG đang chọn trong After Effects">🔄 Lấy Layer từ AE</button>
    <select id="fileSelect" onchange="onSelectFile(this.value)"></select>
  </div>
  <div>
    <button class="success" onclick="pushToAE()">🚀 Tách Layer & Đặt Tên Theo Chữ vào AE</button>
  </div>
</header>

<main>
  <div class="sidebar">
    <div>
      <div class="section-title">1. Chế độ gợi ý nhanh (Presets)</div>
      <div class="preset-grid">
        <button onclick="applyPreset('lines')">📑 Gộp theo Dòng</button>
        <button onclick="applyPreset('words')">🔤 Tách từng Từ</button>
        <button onclick="applyPreset('lines_icons')">✨ Dòng + Tách Icon</button>
        <button onclick="reDetect(true)">🔍 Nhận diện lại</button>
      </div>
    </div>

    <div>
      <div class="section-title" style="display:flex;justify-content:space-between;align-items:center;">
        <span>2. Tên các Layer sẽ tạo trong AE</span>
        <button onclick="addNewCluster()" style="padding:3px 8px;font-size:11px;">+ Thêm Cụm</button>
      </div>
      <div id="clusterList" class="cluster-list"></div>
    </div>

    <div>
      <div class="section-title">3. Công cụ tương tác trên hình</div>
      <div style="display:flex;gap:6px;">
        <button id="btnAssignMode" class="primary" style="flex:1;justify-content:center;" onclick="setTool('assign')">👆 Chọn / Quét Cụm</button>
        <button id="btnKnifeMode" style="flex:1;justify-content:center;" onclick="setTool('knife')">✂️ Dao Cắt Khối</button>
      </div>
      <ul class="help-list" style="margin-top:8px;">
        <li><b>Tên Layer tự động cập nhật</b> theo đúng các chữ nằm trong Cụm đó!</li>
        <li><b>Click vào chữ</b> hoặc <b>Kéo chuột quét vùng</b> để gán chữ vào Cụm đang chọn.</li>
        <li><b>Shift + Click</b> vào chữ để tách riêng chữ đó ra Layer mới.</li>
      </ul>
    </div>
  </div>

  <div class="canvas-wrap">
    <canvas id="previewCanvas"></canvas>
    <div class="canvas-hud">
      <span>Chế độ: <b id="hudToolName">Chọn / Quét Cụm</b></span>
      <span>•</span>
      <span>Đang gán: <b id="hudActiveCluster">Cụm 1</b></span>
      <span>•</span>
      <span>Số Layer: <b id="hudClusterCount">0</b></span>
    </div>
  </div>

  <div class="sidebar-right">
    <div>
      <div class="section-title">4. Tinh chỉnh Đường Mask</div>

      <div class="control-group">
        <div class="control-header">
          <span>Độ mở rộng lấy Glow (Padding)</span>
          <span id="valPadding">85 px</span>
        </div>
        <input type="range" id="rngPadding" min="25" max="180" value="85" oninput="onParamChange()">
      </div>

      <div class="control-group">
        <div class="control-header">
          <span>Ưu tiên tia Flare ngang</span>
          <span id="valAniso">0.35</span>
        </div>
        <input type="range" id="rngAniso" min="0.15" max="0.85" step="0.05" value="0.35" oninput="onParamChange()">
      </div>

      <div class="control-group">
        <div class="control-header">
          <span>Độ mượt điểm Mask (Smoothness)</span>
          <span id="valSmooth">3.0</span>
        </div>
        <input type="range" id="rngSmooth" min="1.5" max="8.0" step="0.5" value="3.0" oninput="onParamChange()">
      </div>

      <label class="checkbox-row">
        <input type="checkbox" id="chkShowBlocks" checked onchange="renderCanvas()">
        <span>Hiển thị khung các khối chữ (Blocks)</span>
      </label>
      <label class="checkbox-row">
        <input type="checkbox" id="chkShowMasks" checked onchange="renderCanvas()">
        <span>Hiển thị đường biên Mask & Điểm Neo</span>
      </label>
      <label class="checkbox-row">
        <input type="checkbox" id="chkShowAnchors" checked onchange="renderCanvas()">
        <span>Hiển thị tâm Anchor Point (+)</span>
      </label>
    </div>

    <div>
      <div class="section-title">5. Thiết lập Layer After Effects</div>

      <div class="control-group">
        <div class="control-header">
          <span>Mask Feather trong AE</span>
          <span id="valFeather">5 px</span>
        </div>
        <input type="range" id="rngFeather" min="0" max="25" step="1" value="5" oninput="document.getElementById('valFeather').innerText=this.value+' px'">
      </div>

      <div class="control-group">
        <div class="control-header">
          <span>Mask Expansion trong AE</span>
          <span id="valExpansion">0 px</span>
        </div>
        <input type="range" id="rngExpansion" min="-20" max="30" step="1" value="0" oninput="document.getElementById('valExpansion').innerText=this.value+' px'">
      </div>

      <label class="checkbox-row">
        <input type="checkbox" id="chkCenterAnchor" checked>
        <span>Tự đặt Anchor Point vào tâm từng cụm</span>
      </label>
    </div>

    <div style="margin-top:auto;">
      <div class="section-title">Trạng thái kết nối</div>
      <div id="statusBox" class="status-box">Đang khởi tạo...</div>
    </div>
  </div>
</main>

<script>
const PALETTE = [
  "#38bdf8", "#4ade80", "#fb923c", "#f472b6",
  "#a78bfa", "#facc15", "#2dd4bf", "#f87171",
  "#60a5fa", "#a3e635", "#fbbf24", "#e879f9"
];

let currentFile = "";
let currentLayerName = "";
let currentSourceName = "";
let currentLayerWidth = 0;
let currentLayerHeight = 0;
let imgObj = new Image();
let blocks = [];
let clusterMasks = [];
let clusterNames = {};
let activeClusterId = 1;
let maxClusterId = 1;
let currentTool = "assign";
let isDragging = false;
let dragStart = null;
let dragEnd = null;
let computeTimer = null;

const canvas = document.getElementById("previewCanvas");
const ctx = canvas.getContext("2d");

function setStatus(msg, type = "") {
  const el = document.getElementById("statusBox");
  el.className = "status-box " + type;
  el.innerHTML = msg;
}

function getClusterColor(cid) {
  return PALETTE[(cid - 1) % PALETTE.length];
}

function getAutoClusterName(cid) {
  const cBlocks = blocks.filter(b => b.cluster_id === cid);
  if (cBlocks.length === 0) return `Cụm ${cid}`;
  cBlocks.sort((a, b) => (a.line_index - b.line_index) || (a.x - b.x));
  const words = cBlocks.filter(b => !b.is_icon && b.text).map(b => b.text.trim());
  if (words.length > 0) return words.join(" ");
  if (cBlocks.some(b => b.is_icon)) return "✨ Icon";
  return `Cụm ${cid}`;
}

async function initApp() {
  await fetchFromAE(true);
}

async function fetchFromAE(silentFallback = false) {
  setStatus("Đang đọc layer & nhận diện chữ từ After Effects...");
  const res = await fetch("/api/fetch_ae").then(r => r.json());
  if (res.files && res.files.length > 0) {
    const sel = document.getElementById("fileSelect");
    sel.innerHTML = "";
    res.files.forEach(f => {
      const opt = document.createElement("option");
      opt.value = f;
      opt.innerText = f.split("/").pop();
      if (f === res.currentFile) opt.selected = true;
      sel.appendChild(opt);
    });
  }
  if (res.ok && res.state) {
    loadState(res.state);
    const baseLabel = res.state.layerName
      ? `${res.state.layerName} [${res.state.filePath.split("/").pop()}]`
      : res.state.filePath.split("/").pop();
    setStatus(`Đã tải <b>${baseLabel}</b> (${res.state.blocks.length} từ/icon).`, "ok");
  } else {
    setStatus(res.error || "Không tìm thấy layer PNG/PSD.", "err");
  }
}

async function onSelectFile(filePath) {
  setStatus("Đang phân tích hình ảnh & nhận diện chữ...");
  const res = await fetch("/api/load_file", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({filePath})
  }).then(r => r.json());
  if (res.ok) {
    loadState(res.state);
    const baseLabel = res.state.layerName
      ? `${res.state.layerName} [${filePath.split("/").pop()}]`
      : filePath.split("/").pop();
    setStatus(`Đã phân tích <b>${baseLabel}</b> (${res.state.blocks.length} từ/icon).`, "ok");
  } else {
    setStatus(res.error, "err");
  }
}

async function reDetect(splitPinch) {
  setStatus("Đang nhận diện lại các từ...");
  const res = await fetch("/api/load_file", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      filePath: currentFile,
      layerName: currentLayerName,
      sourceName: currentSourceName,
      layerWidth: currentLayerWidth,
      layerHeight: currentLayerHeight,
      splitPinch
    })
  }).then(r => r.json());
  if (res.ok) {
    loadState(res.state);
    setStatus(`Đã nhận diện lại: ${res.state.blocks.length} khối.`, "ok");
  }
}

function loadState(state) {
  currentFile = state.filePath;
  currentLayerName = state.layerName || "";
  currentSourceName = state.sourceName || "";
  currentLayerWidth = state.layerWidth || 0;
  currentLayerHeight = state.layerHeight || 0;
  blocks = state.blocks;
  clusterMasks = state.clusters || [];
  clusterNames = {};
  maxClusterId = Math.max(1, ...blocks.map(b => b.cluster_id));
  activeClusterId = 1;
  imgObj = new Image();
  imgObj.onload = () => {
    canvas.width = state.w;
    canvas.height = state.h;
    rebuildClusterList();
    renderCanvas();
  };
  imgObj.src = state.preview_b64;
}

function applyPreset(mode) {
  clusterNames = {};
  if (mode === "lines") {
    blocks.forEach(b => { b.cluster_id = b.line_index + 1; });
  } else if (mode === "words") {
    blocks.forEach((b, idx) => { b.cluster_id = idx + 1; });
  } else if (mode === "lines_icons") {
    const maxLine = Math.max(0, ...blocks.map(b => b.line_index)) + 1;
    blocks.forEach(b => {
      if (b.is_icon) {
        b.cluster_id = maxLine + 1;
      } else {
        b.cluster_id = b.line_index + 1;
      }
    });
  }
  normalizeClusterIds();
  rebuildClusterList();
  requestMaskUpdate();
}

function normalizeClusterIds() {
  const used = [...new Set(blocks.map(b => b.cluster_id))].sort((a,b) => a - b);
  const map = {};
  used.forEach((cid, i) => { map[cid] = i + 1; });
  blocks.forEach(b => { b.cluster_id = map[b.cluster_id]; });
  maxClusterId = Math.max(1, used.length);
  if (activeClusterId > maxClusterId) activeClusterId = maxClusterId;
}

function addNewCluster() {
  maxClusterId += 1;
  activeClusterId = maxClusterId;
  rebuildClusterList();
}

function rebuildClusterList() {
  const counts = {};
  blocks.forEach(b => { counts[b.cluster_id] = (counts[b.cluster_id] || 0) + 1; });
  const totalIds = Math.max(maxClusterId, ...Object.keys(counts).map(Number));
  maxClusterId = totalIds;

  const listEl = document.getElementById("clusterList");
  listEl.innerHTML = "";
  let activeLayerCount = 0;

  for (let cid = 1; cid <= totalIds; cid++) {
    const cnt = counts[cid] || 0;
    if (cnt > 0) activeLayerCount++;
    const autoName = getAutoClusterName(cid);
    const displayName = clusterNames[cid] || autoName;

    const item = document.createElement("div");
    item.className = "cluster-item" + (cid === activeClusterId ? " active" : "");
    item.onclick = () => {
      activeClusterId = cid;
      rebuildClusterList();
      renderCanvas();
    };

    const dot = document.createElement("div");
    dot.className = "color-dot";
    dot.style.background = getClusterColor(cid);

    const inp = document.createElement("input");
    inp.value = displayName;
    inp.onclick = (e) => { e.stopPropagation(); activeClusterId = cid; rebuildClusterList(); };
    inp.oninput = (e) => { clusterNames[cid] = e.target.value; };

    const badge = document.createElement("span");
    badge.className = "cluster-count";
    badge.innerText = `${cnt} từ`;

    item.appendChild(dot);
    item.appendChild(inp);
    item.appendChild(badge);
    listEl.appendChild(item);
  }

  document.getElementById("hudActiveCluster").innerText = clusterNames[activeClusterId] || getAutoClusterName(activeClusterId);
  document.getElementById("hudActiveCluster").style.color = getClusterColor(activeClusterId);
  document.getElementById("hudClusterCount").innerText = activeLayerCount;
}

function setTool(tool) {
  currentTool = tool;
  document.getElementById("btnAssignMode").className = tool === "assign" ? "primary" : "";
  document.getElementById("btnKnifeMode").className = tool === "knife" ? "active-tool" : "";
  document.getElementById("hudToolName").innerText = tool === "assign" ? "Chọn / Quét Cụm" : "✂️ Dao Cắt Khối";
  canvas.style.cursor = tool === "knife" ? "crosshair" : "pointer";
}

function onParamChange() {
  document.getElementById("valPadding").innerText = document.getElementById("rngPadding").value + " px";
  document.getElementById("valAniso").innerText = document.getElementById("rngAniso").value;
  document.getElementById("valSmooth").innerText = document.getElementById("rngSmooth").value;
  if (computeTimer) clearTimeout(computeTimer);
  computeTimer = setTimeout(requestMaskUpdate, 120);
}

async function requestMaskUpdate() {
  const assignments = {};
  blocks.forEach(b => { assignments[b.id] = b.cluster_id; });
  const outer_padding = parseInt(document.getElementById("rngPadding").value, 10);
  const h_anisotropy = parseFloat(document.getElementById("rngAniso").value);
  const smoothness = parseFloat(document.getElementById("rngSmooth").value);

  const res = await fetch("/api/compute_masks", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({assignments, outer_padding, h_anisotropy, smoothness})
  }).then(r => r.json());

  if (res.ok) {
    clusterMasks = res.clusters;
    renderCanvas();
  }
}

function getCanvasCoords(e) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: (e.clientX - rect.left) * scaleX,
    y: (e.clientY - rect.top) * scaleY
  };
}

canvas.addEventListener("mousedown", (e) => {
  const pt = getCanvasCoords(e);
  isDragging = true;
  dragStart = pt;
  dragEnd = pt;
});

canvas.addEventListener("mousemove", (e) => {
  if (!isDragging) return;
  dragEnd = getCanvasCoords(e);
  renderCanvas();
});

window.addEventListener("mouseup", async (e) => {
  if (!isDragging) return;
  isDragging = false;
  const dist = Math.hypot(dragEnd.x - dragStart.x, dragEnd.y - dragStart.y);

  if (currentTool === "knife") {
    if (dist > 15) {
      setStatus("Đang cắt khối chữ...");
      const res = await fetch("/api/split_block", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({x1: dragStart.x, y1: dragStart.y, x2: dragEnd.x, y2: dragEnd.y})
      }).then(r => r.json());
      if (res.ok) {
        blocks = res.state.blocks;
        clusterMasks = res.state.clusters;
        rebuildClusterList();
        setTool("assign");
        setStatus(`Đã cắt thành công (${blocks.length} khối).`, "ok");
      }
    }
    dragStart = dragEnd = null;
    renderCanvas();
    return;
  }

  if (dist < 10) {
    const hit = blocks.find(b =>
      dragEnd.x >= b.x && dragEnd.x <= b.x + b.w &&
      dragEnd.y >= b.y && dragEnd.y <= b.y + b.h
    );
    if (hit) {
      if (e.shiftKey) {
        maxClusterId += 1;
        activeClusterId = maxClusterId;
      }
      hit.cluster_id = activeClusterId;
      rebuildClusterList();
      requestMaskUpdate();
    }
  } else {
    const rx1 = Math.min(dragStart.x, dragEnd.x);
    const ry1 = Math.min(dragStart.y, dragEnd.y);
    const rx2 = Math.max(dragStart.x, dragEnd.x);
    const ry2 = Math.max(dragStart.y, dragEnd.y);
    let changed = false;
    blocks.forEach(b => {
      const bx1 = b.x, by1 = b.y, bx2 = b.x + b.w, by2 = b.y + b.h;
      const overlapX = Math.max(0, Math.min(rx2, bx2) - Math.max(rx1, bx1));
      const overlapY = Math.max(0, Math.min(ry2, by2) - Math.max(ry1, by1));
      if (overlapX > 10 && overlapY > 10) {
        b.cluster_id = activeClusterId;
        changed = true;
      }
    });
    if (changed) {
      rebuildClusterList();
      requestMaskUpdate();
    }
  }
  dragStart = dragEnd = null;
  renderCanvas();
});

function renderCanvas() {
  if (!canvas.width) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(imgObj, 0, 0);

  const showBlocks = document.getElementById("chkShowBlocks").checked;
  const showMasks = document.getElementById("chkShowMasks").checked;
  const showAnchors = document.getElementById("chkShowAnchors").checked;

  if (showBlocks) {
    blocks.forEach(b => {
      const col = getClusterColor(b.cluster_id);
      ctx.save();
      ctx.strokeStyle = col;
      ctx.lineWidth = (b.cluster_id === activeClusterId) ? 2.5 : 1.5;
      ctx.setLineDash([6, 4]);
      ctx.strokeRect(b.x, b.y, b.w, b.h);
      ctx.setLineDash([]);

      const tagLabel = `C${b.cluster_id}: ${b.text || ''}`;
      ctx.font = "bold 12px -apple-system, sans-serif";
      const tw = Math.max(52, ctx.measureText(tagLabel).width + 14);
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.roundRect(b.x, Math.max(0, b.y - 22), tw, 20, 4);
      ctx.fill();
      ctx.fillStyle = "#090b10";
      ctx.fillText(tagLabel, b.x + 6, Math.max(14, b.y - 8));
      ctx.restore();
    });
  }

  if (showMasks) {
    clusterMasks.forEach(cm => {
      const col = getClusterColor(cm.cluster_id);
      const isSelected = (cm.cluster_id === activeClusterId);
      cm.paths.forEach(p => {
        const verts = p.vertices;
        if (!verts || verts.length < 3) return;
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(verts[0][0], verts[0][1]);
        for (let i = 1; i < verts.length; i++) {
          ctx.lineTo(verts[i][0], verts[i][1]);
        }
        ctx.closePath();
        ctx.fillStyle = col + (isSelected ? "22" : "10");
        ctx.fill();
        ctx.strokeStyle = col;
        ctx.lineWidth = isSelected ? 3.5 : 2.2;
        ctx.stroke();

        verts.forEach(v => {
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(v[0] - 3.5, v[1] - 3.5, 7, 7);
          ctx.fillStyle = col;
          ctx.fillRect(v[0] - 2, v[1] - 2, 4, 4);
        });
        ctx.restore();
      });

      if (showAnchors && cm.anchor) {
        const [ax, ay] = cm.anchor;
        ctx.save();
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.arc(ax, ay, 9, 0, Math.PI * 2);
        ctx.moveTo(ax - 14, ay); ctx.lineTo(ax + 14, ay);
        ctx.moveTo(ax, ay - 14); ctx.lineTo(ax, ay + 14);
        ctx.stroke();

        ctx.strokeStyle = col;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(ax, ay, 9, 0, Math.PI * 2);
        ctx.moveTo(ax - 14, ay); ctx.lineTo(ax + 14, ay);
        ctx.moveTo(ax, ay - 14); ctx.lineTo(ax, ay + 14);
        ctx.stroke();
        ctx.restore();
      }
    });
  }

  if (isDragging && dragStart && dragEnd) {
    ctx.save();
    if (currentTool === "knife") {
      ctx.strokeStyle = "#ef4444";
      ctx.lineWidth = 4;
      ctx.setLineDash([8, 5]);
      ctx.beginPath();
      ctx.moveTo(dragStart.x, dragStart.y);
      ctx.lineTo(dragEnd.x, dragEnd.y);
      ctx.stroke();
    } else {
      const col = getClusterColor(activeClusterId);
      const rx = Math.min(dragStart.x, dragEnd.x);
      const ry = Math.min(dragStart.y, dragEnd.y);
      const rw = Math.abs(dragEnd.x - dragStart.x);
      const rh = Math.abs(dragEnd.y - dragStart.y);
      ctx.fillStyle = col + "28";
      ctx.strokeStyle = col;
      ctx.lineWidth = 2.5;
      ctx.fillRect(rx, ry, rw, rh);
      ctx.strokeRect(rx, ry, rw, rh);
    }
    ctx.restore();
  }
}

async function pushToAE() {
  if (!clusterMasks || clusterMasks.length === 0) {
    setStatus("Chưa có cụm nào để tách!", "err");
    return;
  }
  setStatus("🚀 Đang tách Layer và đặt tên theo chữ vào After Effects...");
  const feather = parseFloat(document.getElementById("rngFeather").value);
  const expansion = parseFloat(document.getElementById("rngExpansion").value);
  const centerAnchor = document.getElementById("chkCenterAnchor").checked;

  // Fill auto names for any cluster not manually renamed
  const finalNames = {};
  clusterMasks.forEach(cm => {
    const cid = cm.cluster_id;
    finalNames[cid] = clusterNames[cid] || getAutoClusterName(cid);
  });

  const res = await fetch("/api/push_ae", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      filePath: currentFile,
      layerName: currentLayerName,
      sourceName: currentSourceName,
      clusters: clusterMasks,
      feather,
      expansion,
      centerAnchor,
      customNames: finalNames
    })
  }).then(r => r.json());

  if (res.ok) {
    setStatus(`✅ Đã tạo <b>${res.created} Layers</b> (${Object.values(finalNames).join(" | ")}) trong Comp <b>${res.compName}</b>!`, "ok");
  } else {
    setStatus(`❌ Lỗi: ${res.error}`, "err");
  }
}

initApp();
</script>
</body>
</html>
"""


# ============================================================================
# 6. LOCAL HTTP SERVER & API HANDLER
# ============================================================================

def serialize_state() -> dict:
    assignments = {str(b["id"]): b["cluster_id"] for b in STATE.blocks}
    clusters = STATE.compute_cluster_masks(assignments)
    STATE.render_scriptui_preview(clusters)
    return {
        "filePath": STATE.file_path,
        "layerName": STATE.layer_name,
        "sourceName": STATE.source_name,
        "layerWidth": STATE.layer_w,
        "layerHeight": STATE.layer_h,
        "w": STATE.w,
        "h": STATE.h,
        "blocks": STATE.blocks,
        "clusters": clusters,
        "preview_b64": STATE.preview_b64
    }


def list_sibling_pngs(file_path: str) -> list:
    if not file_path:
        return []
    folder = os.path.dirname(file_path)
    if not os.path.isdir(folder):
        return [file_path]
    exts = ("*.png", "*.psd", "*.jpg", "*.jpeg", "*.webp", "*.tif", "*.tiff", "*.bmp")
    files = []
    for pat in exts:
        files.extend(glob.glob(os.path.join(folder, pat)))
    if file_path not in files and os.path.exists(file_path):
        files.append(file_path)
    return sorted(set(files))


_SUMMARY_CACHE = {"mask_key": None, "clusters": None, "dir_key": None, "files": None}


def serialize_scriptui_summary() -> dict:
    # Cluster masks only change when the cluster assignment / mask params change: a plain selection click
    # (1st click of a merge) reuses them, so only the cheap preview redraw runs.
    mask_key = (
        STATE.file_path, id(STATE.label_map),
        tuple((b["id"], b["cluster_id"]) for b in STATE.blocks),
        getattr(STATE, "outer_padding", None), getattr(STATE, "smoothness", None), getattr(STATE, "h_anisotropy", None),
        json.dumps(getattr(STATE, "custom_cluster_names", {}) or {}, sort_keys=True, ensure_ascii=False),
    )
    if _SUMMARY_CACHE["mask_key"] == mask_key and _SUMMARY_CACHE["clusters"] is not None:
        clusters = _SUMMARY_CACHE["clusters"]
    else:
        clusters = STATE.compute_cluster_masks()
        _SUMMARY_CACHE["mask_key"], _SUMMARY_CACHE["clusters"] = mask_key, clusters
    preview_path = STATE.render_scriptui_preview(clusters)
    # Sibling file list: cache per folder + mtime (directory listing is slow on Windows / network drives)
    try:
        d = os.path.dirname(STATE.file_path or "")
        dir_key = (d, os.path.getmtime(d)) if d else None
    except Exception:
        dir_key = None
    if dir_key is None or dir_key != _SUMMARY_CACHE["dir_key"]:
        _SUMMARY_CACHE["files"] = list_sibling_pngs(STATE.file_path)
        _SUMMARY_CACHE["dir_key"] = dir_key
    sel_txt = ""
    if STATE.selected_block_id is not None:
        sb = next((b for b in STATE.blocks if b["id"] == STATE.selected_block_id), None)
        if sb:
            sel_txt = sb.get("text") or f"Chữ {sb['id']}"
    return {
        "ok": True,
        "filePath": STATE.file_path,
        "layerName": STATE.layer_name,
        "sourceName": STATE.source_name,
        "files": _SUMMARY_CACHE["files"],
        "previewPath": preview_path,
        "blockCount": len(STATE.blocks),
        "clusterCount": len(clusters),
        "clusterNames": [c["name"] for c in clusters],
        "selectedBlockId": STATE.selected_block_id,
        "selectedText": sel_txt
    }


class SplitterRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            body = HTML_UI.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/api/ping":
            self._send_json({"ok": True})

        elif parsed.path == "/api/fetch_ae":
            ae_info = get_active_ae_layer_info()
            target_file = ae_info.get("filePath") if ae_info.get("ok") else STATE.file_path
            target_file = resolve_mac_path(target_file)
            if target_file and os.path.exists(target_file):
                STATE.load_image(
                    target_file,
                    layer_name=ae_info.get("layerName", "") if ae_info.get("ok") else STATE.layer_name,
                    source_name=ae_info.get("sourceName", "") if ae_info.get("ok") else STATE.source_name,
                    layer_w=int(ae_info.get("layerWidth", 0) or 0) if ae_info.get("ok") else STATE.layer_w,
                    layer_h=int(ae_info.get("layerHeight", 0) or 0) if ae_info.get("ok") else STATE.layer_h
                )
                self._send_json({
                    "ok": True,
                    "ae": ae_info,
                    "currentFile": target_file,
                    "files": list_sibling_pngs(target_file),
                    "state": serialize_state()
                })
            else:
                self._send_json({
                    "ok": False,
                    "error": ae_info.get("error", "Không tìm thấy layer hình ảnh trong AE."),
                    "files": list_sibling_pngs(STATE.file_path)
                })

        elif parsed.path == "/api/su_init_from_req":
            req_file = Path(tempfile.gettempdir()) / "ae_splitter_req.json"
            fp = ""
            l_name = ""
            s_name = ""
            l_w = 0
            l_h = 0
            if req_file.exists():
                try:
                    req_data = json.loads(req_file.read_text(encoding="utf-8"))
                    fp = resolve_mac_path(req_data.get("file", ""))
                    l_name = req_data.get("layerName", "") or ""
                    s_name = req_data.get("sourceName", "") or ""
                    l_w = int(req_data.get("layerWidth", 0) or 0)
                    l_h = int(req_data.get("layerHeight", 0) or 0)
                except Exception:
                    pass
            if fp and os.path.exists(fp):
                STATE.load_image(fp, layer_name=l_name, source_name=s_name, layer_w=l_w, layer_h=l_h)
            if not STATE.file_path:
                self._send_json({"ok": False, "error": "Không tìm thấy file PNG/PSD."})
                return
            self._send_json(serialize_scriptui_summary())

        elif parsed.path == "/api/su_init":
            fp = resolve_mac_path(qs.get("file", [""])[0])
            l_name = qs.get("layerName", [""])[0]
            s_name = qs.get("sourceName", [""])[0]
            if fp and os.path.exists(fp):
                STATE.load_image(fp, layer_name=l_name, source_name=s_name)
            if not STATE.file_path:
                self._send_json({"ok": False, "error": "Chưa có file PNG/PSD."})
                return
            self._send_json(serialize_scriptui_summary())

        elif parsed.path == "/api/su_preset":
            mode = qs.get("mode", ["lines"])[0]
            STATE.apply_preset_mode(mode)
            self._send_json(serialize_scriptui_summary())

        elif parsed.path == "/api/su_undo":
            STATE.undo()
            self._send_json(serialize_scriptui_summary())

        elif parsed.path == "/api/su_click":
            px = float(qs.get("x", [0])[0])
            py = float(qs.get("y", [0])[0])
            alt_key = qs.get("alt", ["0"])[0] == "1"
            shift_key = qs.get("shift", ["0"])[0] == "1"
            scale = STATE.preview_scale if STATE.preview_scale > 0 else 1.0
            off_x = getattr(STATE, "preview_off_x", 0)
            off_y = getattr(STATE, "preview_off_y", 0)
            ix = (px - off_x) / scale
            iy = (py - off_y) / scale
            if "cluster" in qs:
                cid = int(qs.get("cluster", [1])[0])
                for b in STATE.blocks:
                    if b["x"] <= ix <= b["x"] + b["w"] and b["y"] <= iy <= b["y"] + b["h"]:
                        STATE.push_history()
                        b["cluster_id"] = cid
                        break
            else:
                STATE.smart_click(ix, iy, alt_key=alt_key, shift_key=shift_key)
            self._send_json(serialize_scriptui_summary())

        elif parsed.path == "/api/su_params":
            pad = int(qs.get("padding", [85])[0])
            STATE.outer_padding = pad
            self._send_json(serialize_scriptui_summary())

        elif parsed.path == "/api/su_push_json":
            feather = float(qs.get("feather", [5.0])[0])
            center_anchor = qs.get("anchor", ["1"])[0] == "1"
            req_file = Path(tempfile.gettempdir()) / "ae_splitter_req.json"
            if req_file.exists():
                try:
                    req_data = json.loads(req_file.read_text(encoding="utf-8"))
                    if "customPipeNames" in req_data:
                        STATE.set_cluster_names_from_pipe_str(req_data.get("customPipeNames", ""))
                except Exception:
                    pass
            clusters = STATE.compute_cluster_masks()
            jsx_push_path = Path(tempfile.gettempdir()) / "ae_splitter_direct_push.jsx"
            jsx_push_code = build_push_jsx_code(
                file_path=STATE.file_path or "",
                clusters=clusters,
                feather=feather,
                expansion=0.0,
                center_anchor=center_anchor,
                out_path="",
                layer_name=STATE.layer_name,
                source_name=STATE.source_name
            )
            jsx_push_path.write_text(jsx_push_code, encoding="utf-8")
            self._send_json({
                "ok": True,
                "jsxPath": str(jsx_push_path),
                "clusterCount": len(clusters),
                "clusterNames": [c["name"] for c in clusters]
            })
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        data = json.loads(raw_body)

        if self.path == "/api/load_file":
            fp = resolve_mac_path(data.get("filePath", ""))
            split_pinch = bool(data.get("splitPinch", True))
            if not fp or not os.path.exists(fp):
                self._send_json({"ok": False, "error": f"Không tìm thấy file: {fp}"})
                return
            same_file = (fp == STATE.file_path)
            l_name = data.get("layerName") if "layerName" in data else (STATE.layer_name if same_file else "")
            s_name = data.get("sourceName") if "sourceName" in data else (STATE.source_name if same_file else "")
            l_w = int(data.get("layerWidth", 0) or (STATE.layer_w if same_file else 0))
            l_h = int(data.get("layerHeight", 0) or (STATE.layer_h if same_file else 0))
            STATE.load_image(
                fp,
                split_pinch=split_pinch,
                layer_name=l_name or "",
                source_name=s_name or "",
                layer_w=l_w,
                layer_h=l_h
            )
            self._send_json({"ok": True, "state": serialize_state()})

        elif self.path == "/api/compute_masks":
            assignments = data.get("assignments", {})
            outer_padding = int(data.get("outer_padding", 85))
            h_anisotropy = float(data.get("h_anisotropy", 0.35))
            smoothness = float(data.get("smoothness", 3.0))
            clusters = STATE.compute_cluster_masks(
                assignments=assignments,
                outer_padding=outer_padding,
                smoothness=smoothness,
                h_anisotropy=h_anisotropy
            )
            STATE.render_scriptui_preview(clusters)
            self._send_json({"ok": True, "clusters": clusters})

        elif self.path == "/api/split_block":
            STATE.split_block_by_line(
                float(data.get("x1", 0)), float(data.get("y1", 0)),
                float(data.get("x2", 0)), float(data.get("y2", 0))
            )
            self._send_json({"ok": True, "state": serialize_state()})

        elif self.path == "/api/push_ae":
            res = push_clusters_to_ae(
                file_path=resolve_mac_path(data.get("filePath") or STATE.file_path),
                clusters=data.get("clusters", []),
                feather=float(data.get("feather", 5.0)),
                expansion=float(data.get("expansion", 0.0)),
                center_anchor=bool(data.get("centerAnchor", True)),
                custom_names=data.get("customNames", {}),
                layer_name=data.get("layerName") or STATE.layer_name,
                source_name=data.get("sourceName") or STATE.source_name
            )
            self._send_json(res)
        else:
            self.send_error(404)


def install_ae_plugins(port: int = 8765):
    """Installs both the Native ScriptUI Panel AND the Adobe CEP Extension for After Effects."""
    get_vietocr_predictor()
    jsx_code = build_native_scriptui_jsx(port)
    local_jsx = Path(__file__).resolve().parent / "AE_Text_Cluster_Splitter.jsx"
    local_jsx.write_text(jsx_code, encoding="utf-8")
    copy_to_user_scripts(jsx_code, "AE_Text_Cluster_Splitter.jsx")

    for ver_dir in get_ae_user_script_dirs():
        panel_dir = ver_dir / "Scripts" / "ScriptUI Panels"
        try:
            panel_dir.mkdir(parents=True, exist_ok=True)
            (panel_dir / "AE_Text_Cluster_Splitter.jsx").write_text(jsx_code, encoding="utf-8")
        except Exception:
            pass

    if sys.platform == "darwin":
        cep_dir = Path.home() / "Library" / "Application Support" / "Adobe" / "CEP" / "extensions" / "com.manh.textclustersplitter"
    else:
        cep_dir = Path(os.environ.get("APPDATA", "")) / "Adobe" / "CEP" / "extensions" / "com.manh.textclustersplitter"

    # Unsigned CEP extensions only load with PlayerDebugMode=1 (CSXS 9-12 cover AE 2019-2026)
    for csxs_v in range(9, 13):
        try:
            if sys.platform == "darwin":
                subprocess.run(["defaults", "write", f"com.adobe.CSXS.{csxs_v}", "PlayerDebugMode", "1"], capture_output=True)
            elif sys.platform == "win32":
                import winreg
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Adobe\CSXS.{csxs_v}") as k:
                    winreg.SetValueEx(k, "PlayerDebugMode", 0, winreg.REG_SZ, "1")
        except Exception:
            pass

    try:
        (cep_dir / "CSXS").mkdir(parents=True, exist_ok=True)
        manifest_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ExtensionManifest Version="7.0" ExtensionBundleId="com.manh.textclustersplitter" ExtensionBundleVersion="1.0.0" ExtensionBundleName="Text Cluster Splitter" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <ExtensionList>
        <Extension Id="com.manh.textclustersplitter" Version="1.0.0" />
    </ExtensionList>
    <ExecutionEnvironment>
        <HostList>
            <Host Name="AEFT" Version="[0.0,99.9]" />
        </HostList>
        <LocaleList>
            <Locale Code="All" />
        </LocaleList>
        <RequiredRuntimeList>
            <RequiredRuntime Name="CSXS" Version="9.0" />
        </RequiredRuntimeList>
    </ExecutionEnvironment>
    <DispatchInfoList>
        <Extension Id="com.manh.textclustersplitter">
            <DispatchInfo>
                <Resources>
                    <MainPath>./index.html</MainPath>
                    <CEFCommandLine>
                        <Parameter>--enable-nodejs</Parameter>
                        <Parameter>--mixed-context</Parameter>
                        <Parameter>--allow-file-access</Parameter>
                        <Parameter>--allow-file-access-from-files</Parameter>
                    </CEFCommandLine>
                </Resources>
                <Lifecycle>
                    <AutoVisible>true</AutoVisible>
                </Lifecycle>
                <UI>
                    <Type>Panel</Type>
                    <Menu>Text Cluster Splitter</Menu>
                    <Geometry>
                        <Size>
                            <Height>760</Height>
                            <Width>960</Width>
                        </Size>
                        <MinSize>
                            <Height>420</Height>
                            <Width>360</Width>
                        </MinSize>
                    </Geometry>
                </UI>
            </DispatchInfo>
        </Extension>
    </DispatchInfoList>
</ExtensionManifest>"""
        (cep_dir / "CSXS" / "manifest.xml").write_text(manifest_xml, encoding="utf-8")

        py_exe = str(sys.executable).replace("\\", "/")
        script_py = str(Path(__file__).resolve()).replace("\\", "/")
        cep_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  html, body, iframe {{ margin: 0; padding: 0; width: 100%; height: 100%; border: none; background: #0f1117; color: #eef1f8; font-family: sans-serif; overflow: hidden; }}
  #loader {{ display: flex; align-items: center; justify-content: center; height: 100vh; font-size: 13px; color: #949cb2; }}
</style>
</head>
<body>
<div id="loader">⏳ Đang kết nối Engine xử lý ảnh...</div>
<iframe id="appFrame" style="display:none;"></iframe>
<script>
const URL = "http://127.0.0.1:{port}";
async function boot() {{
  try {{
    const r = await fetch(URL + "/api/ping");
    if (r.ok) {{
      showFrame();
      return;
    }}
  }} catch (e) {{}}
  try {{
    if (window.cep_node || window.require) {{
      const req = window.cep_node ? window.cep_node.require : window.require;
      const cp = req("child_process");
      const child = cp.spawn("{py_exe}", ["{script_py}", "--port", "{port}", "--no-browser"], {{
        detached: true,
        windowsHide: true,
        stdio: "ignore"
      }});
      child.unref();
    }}
  }} catch (err) {{}}
  setTimeout(boot, 600);
}}
function showFrame() {{
  document.getElementById("loader").style.display = "none";
  const fr = document.getElementById("appFrame");
  fr.style.display = "block";
  fr.src = URL;
}}
boot();
</script>
</body>
</html>"""
        (cep_dir / "index.html").write_text(cep_html, encoding="utf-8")
    except Exception:
        pass

    return str(local_jsx)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AE Text Cluster & Mask Splitter")
    parser.add_argument("--file", type=str, default="", help="Initial PNG file path")
    parser.add_argument("--port", type=int, default=8765, help="Local UI server port")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    parser.add_argument("--open-ae-panel", action="store_true", help="Pop open native ScriptUI window inside AE immediately")
    parser.add_argument("--install-only", action="store_true", help="Install AE ScriptUI and CEP plugins for this machine and exit")
    args = parser.parse_args()

    local_jsx_path = install_ae_plugins(args.port)
    if args.install_only:
        print(f"✅ Installed AE ScriptUI & CEP plugins successfully! ({local_jsx_path})")
        return

    init_file = resolve_mac_path(args.file)
    init_layer_name = ""
    init_source_name = ""
    init_lw = 0
    init_lh = 0
    if not init_file:
        ae_info = get_active_ae_layer_info()
        if ae_info.get("ok") and os.path.exists(ae_info.get("filePath", "")):
            init_file = ae_info["filePath"]
            init_layer_name = ae_info.get("layerName", "")
            init_source_name = ae_info.get("sourceName", "")
            init_lw = int(ae_info.get("layerWidth", 0) or 0)
            init_lh = int(ae_info.get("layerHeight", 0) or 0)

    if init_file and os.path.exists(init_file):
        STATE.load_image(
            init_file,
            layer_name=init_layer_name,
            source_name=init_source_name,
            layer_w=init_lw,
            layer_h=init_lh
        )
        STATE.render_scriptui_preview()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), SplitterRequestHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"✨ AE Text Cluster & Mask Splitter running at {url}", flush=True)

    if args.open_ae_panel:
        jsx_path_str = local_jsx_path.replace("\\", "/")
        if sys.platform == "darwin":
            subprocess.Popen([
                "sh", "-c",
                f"sleep 0.5 && osascript -e 'tell application \"Adobe After Effects 2026\" to DoScript \"$.evalFile(\\\"{jsx_path_str}\\\")\"' && osascript -e 'tell application \"Adobe After Effects 2026\" to activate'"
            ])

    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
