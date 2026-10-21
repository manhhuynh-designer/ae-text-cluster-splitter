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
import webbrowser
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt


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
            cmd = f'powershell -Command "(New-Object -ComObject WScript.Shell).AppActivate(\'{app_name}\')"'
            subprocess.run(cmd, shell=True, capture_output=True)
    except Exception:
        pass


def run_ae_jsx(jsx_code: str) -> str:
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
            subprocess.run([ae_exe, "-r", str(jsx_file)], capture_output=True, text=True)

    if out_file.exists():
        try:
            return out_file.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
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
    "VỚ": "VỚI", "GIẶI": "GIẶT", "NHẠNH": "NHANH",
    "AO": "ÁO", "ĐE": "ĐỂ", "ĐỀ": "ĐỂ", "CO": "CÓ",
    "PHỐT": "PHÚT", "PHOT": "PHÚT", "PHUT": "PHÚT", "CUNG": "CÙNG",
    "ĐÀI": "DÀI", "TỐO": "TỐC", "TOO": "TỐC", "TOC": "TỐC",
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

    def predict(self, pil_img) -> str:
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
        for _ in range(64):
            logits, hidden = self.dec_sess.run(None, {"tgt": tgt, "hidden": hidden, "encoder_outputs": enc_out})
            tok = int(np.argmax(logits[0]))
            if tok == 2:
                break
            if tok in VIETOCR_I2C:
                chars.append(VIETOCR_I2C[tok])
            tgt = np.array([tok], dtype=np.int64)
        return "".join(chars)

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
    s = re.sub(r"(\d+)([A-ZÀ-Ỹ]+)", r"\1 \2", s)
    s = re.sub(r"(OMO)([A-ZÀ-Ỹ]+)", r"\1 \2", s)
    s = re.sub(r"([A-ZÀ-Ỹ]+)(OMO)", r"\1 \2", s)
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
                    matched_multi = list(seq)
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
            if hw_base == base or (len(base) >= 3 and len(hw_base) == len(base) and sum(c1 != c2 for c1, c2 in zip(hw_base, base)) <= 1):
                core_w = hw
                break
        out_tokens.append(core_w + punct)
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
        self.label_map = None
        self.blocks = []
        self.preview_b64 = ""
        self.outer_padding = 85
        self.smoothness = 3.0
        self.h_anisotropy = 0.35
        self.preview_scale = 1.0
        self.preview_off_x = 0
        self.preview_off_y = 0
        self.preview_counter = 0
        self.preview_png_path = str(Path(tempfile.gettempdir()) / "ae_splitter_ui_preview_0.png")

    def load_image(self, file_path: str, split_pinch: bool = True,
                   layer_name: str = "", source_name: str = "",
                   layer_w: int = 0, layer_h: int = 0):
        file_path = resolve_mac_path(file_path)
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

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

        self._detect_blocks(split_pinch=split_pinch)

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
        dist = cv2.distanceTransform(core, cv2.DIST_L2, 5)

        # 2. Spatially adaptive polarity normalization (extracts inner letter face regardless of white-on-blue or blue-on-white 3D style)
        inner_core = (dist >= 4).astype(np.uint8)
        deep_mask = (dist >= 6).astype(np.float32)
        local_inner_lum = cv2.blur(lum * deep_mask, (121, 81)) / (cv2.blur(deep_mask, (121, 81)) + 1e-5)
        bright_face = np.clip((lum - 145.0) / 75.0, 0.0, 1.0)
        dark_face = np.clip((145.0 - lum) / 70.0, 0.0, 1.0)
        face = np.where(local_inner_lum > 145.0, bright_face, dark_face) * inner_core
        norm_gray = (255.0 * (1.0 - face)).astype(np.uint8)

        # 3. Detect word seeds and 4-pointed star icons on polarity-normalized letter faces
        face_bin = (norm_gray < 140).astype(np.uint8)
        w_bin = cv2.dilate(face_bin, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 5)))
        nw, lw, sw, cw = cv2.connectedComponentsWithStats(w_bin)

        seeds = []
        icon_seeds = []
        min_seed_h = max(48, min(85, int(self.h * 0.14)))
        min_seed_area = max(2500, min(7000, int(self.h * self.w * 0.012)))
        for i in range(1, nw):
            x, y, w, h, area = sw[i]
            f_area = int(np.sum((lw[y:y + h, x:x + w] == i) & (face_bin[y:y + h, x:x + w] > 0)))
            f_sol = f_area / float(max(1, w * h))
            if 1000 <= area < 14000 and w < 185 and h < 230 and f_sol <= 0.36 and 0.65 <= (w / float(max(1, h))) <= 1.32:
                # Ensure it is not a letter (e.g. 'Y') sitting on the exact same baseline as an adjacent letter
                on_letter_baseline = False
                for j in range(1, nw):
                    if j == i:
                        continue
                    ox, oy, ow, oh, oarea = sw[j]
                    if oh >= min_seed_h and oarea >= min_seed_area:
                        h_gap = max(0, max(x - (ox + ow), ox - (x + w)))
                        if h_gap < 25 and abs((y + h) - (oy + oh)) <= 12 and abs(y - oy) <= 35:
                            on_letter_baseline = True
                            break
                if not on_letter_baseline:
                    norm_gray[y:y + h, x:x + w][lw[y:y + h, x:x + w] == i] = 255
                    icon_seeds.append({
                        "mask": (lw == i),
                        "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                        "cx": float(cw[i][0]), "cy": float(cw[i][1]),
                        "text": "✨", "is_icon": True
                    })
                    continue
            if h >= min_seed_h and area >= min_seed_area:
                seeds.append({
                    "mask": (lw == i),
                    "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                    "cx": float(cw[i][0]), "cy": float(cw[i][1]),
                    "area": int(area)
                })

        # Also check raw core for any isolated 4-pointed star icons not caught in w_bin
        nc_raw, lc_raw, sc_raw, cc_raw = cv2.connectedComponentsWithStats(core)
        for i in range(1, nc_raw):
            x, y, w, h, area = sc_raw[i]
            sol = area / float(max(1, w * h))
            if 1000 <= area < 14000 and w < 185 and h < 230 and sol <= 0.54 and 0.65 <= (w / float(max(1, h))) <= 1.28:
                # Exclude diacritics sitting directly above/below a word seed
                is_diacritic = False
                for sd in seeds:
                    ov_x = max(0, min(x + w, sd["x"] + sd["w"]) - max(x, sd["x"]))
                    v_gap = max(0, max(y - (sd["y"] + sd["h"]), sd["y"] - (y + h)))
                    if ov_x >= w * 0.45 and v_gap < 40:
                        is_diacritic = True
                        break
                if is_diacritic:
                    continue
                m_raw = (lc_raw == i)
                if not any(np.any(m_raw & ic["mask"]) for ic in icon_seeds) and not any(np.any(m_raw & sd["mask"]) for sd in seeds):
                    norm_gray[y:y + h, x:x + w][m_raw[y:y + h, x:x + w]] = 255
                    icon_seeds.append({
                        "mask": m_raw,
                        "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                        "cx": float(cc_raw[i][0]), "cy": float(cc_raw[i][1]),
                        "text": "✨", "is_icon": True
                    })

        # 4. Group word seeds into horizontal text lines (top-to-bottom)
        seeds.sort(key=lambda s: s["cy"])
        lines = []
        for s in seeds:
            placed = False
            for ln in lines:
                if abs(s["cy"] - np.mean([x["cy"] for x in ln])) < max(np.mean([x["h"] for x in ln]), 110) * 0.60:
                    ln.append(s)
                    placed = True
                    break
            if not placed:
                lines.append([s])

        # 5. Run cross-platform VietOCR on line crops & align 1-to-1 with word seeds
        fname = unicodedata.normalize("NFC", os.path.basename(self.file_path or ""))
        fname_clean = re.split(r"-\s*\d|\d{4,}", os.path.splitext(fname)[0])[0]
        hint_words = [w.upper() for w in re.findall(r"[A-Za-zÀ-ỹ0-9]+", fname_clean)]

        line_crops = []
        for ln in lines:
            ln.sort(key=lambda c: c["x"])
            x1 = max(0, min(c["x"] for c in ln) - 15)
            y1 = max(0, min(c["y"] for c in ln) - 55)
            x2 = min(self.w, max(c["x"] + c["w"] for c in ln) + 15)
            y2 = min(self.h, max(c["y"] + c["h"] for c in ln) + 28)
            crop = cv2.cvtColor(norm_gray[y1:y2, x1:x2], cv2.COLOR_GRAY2RGB)
            line_crops.append(Image.fromarray(crop))

        viet_pred = get_vietocr_predictor()
        if viet_pred is not None and line_crops:
            try:
                raw_preds = viet_pred.predict_batch(line_crops)
            except Exception:
                raw_preds = [""] * len(line_crops)
        else:
            raw_preds = [""] * len(line_crops)

        cleaned_lines = [clean_ocr_line_tokens(pr, hint_words) for pr in raw_preds]

        ordered_lines = []
        for li, ln in enumerate(lines):
            words_li = cleaned_lines[li] if (li < len(cleaned_lines) and cleaned_lines[li]) else [f"Chữ {j + 1}" for j in range(len(ln))]
            K = max(1, len(words_li))

            # Merge closest adjacent seeds if len(ln) > K
            while len(ln) > K:
                best_idx, min_gap = 0, 999999
                for j in range(len(ln) - 1):
                    gap = ln[j + 1]["x"] - (ln[j]["x"] + ln[j]["w"])
                    if gap < min_gap:
                        min_gap, best_idx = gap, j
                s1, s2 = ln[best_idx], ln[best_idx + 1]
                merged_mask = s1["mask"] | s2["mask"]
                ys, xs = np.where(merged_mask)
                ln[best_idx] = {
                    "mask": merged_mask,
                    "x": int(xs.min()), "y": int(ys.min()),
                    "w": int(xs.max() - xs.min() + 1), "h": int(ys.max() - ys.min() + 1),
                    "cx": float(xs.mean()), "cy": float(ys.mean())
                }
                ln.pop(best_idx + 1)

            # Split widest seeds at deepest face_bin valley if len(ln) < K
            if len(ln) < K and len(ln) > 0:
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
                        col_sum = np.sum(face_bin[sy:sy + sh_box, sx:sx + sw_box] & s["mask"][sy:sy + sh_box, sx:sx + sw_box], axis=0)
                        char_lens = [max(1, len(w)) for w in sub_words]
                        tot_chars = float(sum(char_lens))
                        cuts = [0]
                        cum = 0
                        for r_i in range(cnt - 1):
                            cum += char_lens[r_i]
                            expected_x = int(sw_box * (cum / tot_chars))
                            s_x1 = max(cuts[-1] + 20, expected_x - 38)
                            s_x2 = min(sw_box - 20, expected_x + 38)
                            if s_x2 > s_x1:
                                cut_rel = s_x1 + int(np.argmin(col_sum[s_x1:s_x2]))
                            else:
                                cut_rel = expected_x
                            cuts.append(cut_rel)
                        cuts.append(sw_box)
                        for r_i in range(cnt):
                            c_x1, c_x2 = sx + cuts[r_i], sx + cuts[r_i + 1]
                            sub_m = np.zeros_like(s["mask"])
                            sub_m[sy:sy + sh_box, c_x1:c_x2] = s["mask"][sy:sy + sh_box, c_x1:c_x2]
                            if not np.any(sub_m):
                                sub_m[sy:sy + sh_box, c_x1:c_x2] = True
                            ys_s, xs_s = np.where(sub_m)
                            new_ln.append({
                                "mask": sub_m,
                                "x": c_x1, "y": sy, "w": max(1, c_x2 - c_x1), "h": sh_box,
                                "cx": float(xs_s.mean()) if len(xs_s) else float(c_x1 + (c_x2 - c_x1) * 0.5),
                                "cy": float(ys_s.mean()) if len(ys_s) else float(sy + sh_box * 0.5),
                                "text": sub_words[r_i], "is_icon": False
                            })
                ln = new_ln
            else:
                for j, s in enumerate(ln):
                    s["text"] = words_li[j]
                    s["is_icon"] = False

            ordered_lines.append(ln)

        # Attach icons to their nearest line
        if not ordered_lines and icon_seeds:
            ordered_lines = [icon_seeds[:]]
        else:
            for ic in icon_seeds:
                best_ln = min(ordered_lines, key=lambda ln: abs(ic["cy"] - np.mean([x["cy"] for x in ln])))
                best_ln.append(ic)

        # 6. Assign all core pixels (including 3D strokes and Vietnamese diacritics) via Voronoi seed map
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

        if ordered_items:
            _, inds = distance_transform_edt(seed_map == 0, sampling=(0.65, 1.0), return_indices=True)
            nearest_seed = seed_map[inds[0], inds[1]]
            self.label_map = np.where(core > 0, nearest_seed, 0).astype(np.int32)
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
        for b in self.blocks:
            b["cluster_id"] = cmap[b["cluster_id"]]

    def get_cluster_auto_name(self, cid: int, fallback_index: int) -> str:
        """Generates a clean layer name from the recognized words of all blocks in cluster `cid`."""
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

    def split_block_by_line(self, x1: float, y1: float, x2: float, y2: float):
        if self.label_map is None:
            return
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

        cluster_cores = []
        cluster_bboxes = []
        for cid in sorted_cids:
            b_ids = cluster_blocks[cid]
            cmask = np.isin(self.label_map, b_ids).astype(np.uint8)
            ys, xs = np.where(cmask > 0)
            if len(xs) > 0:
                cluster_bboxes.append((int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())))
            else:
                cluster_bboxes.append((0, 0, self.w, self.h))

            other_mask = (self.label_map > 0) & (~cmask.astype(bool))
            closed = cv2.morphologyEx(cmask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (65, 9)))
            closed[other_mask] = 0
            cluster_cores.append(closed)

        dists = [distance_transform_edt(1 - cm, sampling=(1.0, self.h_anisotropy)) for cm in cluster_cores]
        dists_stack = np.stack(dists, axis=0)
        nearest_idx = np.argmin(dists_stack, axis=0)

        results = []
        for k, cid in enumerate(sorted_cids):
            territory = ((nearest_idx == k) & (dists[k] <= self.outer_padding)).astype(np.uint8) * 255
            blur_k = max(11, int(round(self.smoothness * 7)) | 1)
            territory = cv2.GaussianBlur(territory, (blur_k, blur_k), 0)
            territory = (territory >= 128).astype(np.uint8) * 255

            contours, _ = cv2.findContours(territory, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            paths = []
            for cnt in contours:
                if cv2.contourArea(cnt) < 400:
                    continue
                eps = max(1.5, float(self.smoothness))
                approx = cv2.approxPolyDP(cnt, eps, True)
                pts = [[round(float(p[0][0]), 1), round(float(p[0][1]), 1)] for p in approx]
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

    def render_scriptui_preview(self, clusters: list = None, box_w: int = 520, box_h: int = 360) -> str:
        """Renders a crisp visual preview PNG fitted inside (box_w, box_h) with aspect ratio preserved."""
        if self.img_bgra is None:
            return ""
        if clusters is None:
            clusters = self.compute_cluster_masks()

        alpha_f = self.alpha[:, :, None] / 255.0
        bg = np.full((self.h, self.w, 3), 16, dtype=np.uint8)
        bg[::32, :] = 24
        bg[:, ::32] = 24
        vis = (self.img_bgra[:, :, :3] * alpha_f + bg * (1.0 - alpha_f)).astype(np.uint8)

        for b in self.blocks:
            cid = b["cluster_id"]
            col = BGR_PALETTE[(cid - 1) % len(BGR_PALETTE)]
            bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
            cv2.rectangle(vis, (bx, by), (bx + bw, by + bh), col, 2, cv2.LINE_AA)
            tag_y = max(24, by - 6)
            ascii_txt = strip_accents(b.get("text", ""))[:10] or "ICON"
            label_str = f"C{cid}:{ascii_txt}"
            pill_w = max(56, len(label_str) * 11 + 12)
            cv2.rectangle(vis, (bx, tag_y - 22), (bx + pill_w, tag_y + 2), col, -1)
            cv2.putText(vis, label_str, (bx + 6, tag_y - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (15, 17, 23), 2, cv2.LINE_AA)

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

        scale = min(box_w / float(self.w), box_h / float(self.h))
        new_w = max(1, int(round(self.w * scale)))
        new_h = max(1, int(round(self.h * scale)))
        resized = cv2.resize(vis, (new_w, new_h), interpolation=cv2.INTER_AREA)

        canvas_img = np.full((box_h, box_w, 3), 12, dtype=np.uint8)
        off_x = (box_w - new_w) // 2
        off_y = (box_h - new_h) // 2
        canvas_img[off_y:off_y + new_h, off_x:off_x + new_w] = resized

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

    function ensureServer() {{
        var curlCmd = IS_MAC ? "curl" : "curl.exe";
        var ping = system.callSystem(curlCmd + " -s -m 1 http://127.0.0.1:" + PORT + "/api/ping");
        if (!ping || ping.indexOf("ok") === -1) {{
            if (IS_MAC) {{
                system.callSystem("'" + PY_EXE + "' '" + SCRIPT_PY + "' --port " + PORT + " --no-browser >/tmp/ae_splitter.log 2>&1 &");
            }} else {{
                system.callSystem('cmd.exe /c start /b "" "' + PY_EXE + '" "' + SCRIPT_PY + '" --port ' + PORT + ' --no-browser');
            }}
            $.sleep(1200);
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
        var curlCmd = IS_MAC ? "curl" : "curl.exe";
        var url = "http://127.0.0.1:" + PORT + endpoint + (query ? ("?" + query) : "");
        var res = system.callSystem(curlCmd + ' -s "' + url + '"');
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
        win.alignChildren = ["fill", "top"];
        win.spacing = 6;
        win.margins = 10;

        var selRow = win.add("group");
        selRow.orientation = "row";
        selRow.alignChildren = ["left", "center"];
        selRow.spacing = 6;

        var btnFetch = selRow.add("button", undefined, "🔄 Lấy Layer đang chọn");
        btnFetch.preferredSize.width = 165;
        var ddFiles = selRow.add("dropdownlist", undefined, ["(Chọn nhanh file PNG...)"]);
        ddFiles.preferredSize.width = 345;

        var presetRow = win.add("group");
        presetRow.orientation = "row";
        presetRow.alignChildren = ["fill", "center"];
        presetRow.spacing = 6;

        var btnLines = presetRow.add("button", undefined, "📑 Gộp theo Dòng");
        var btnWords = presetRow.add("button", undefined, "🔤 Tách từng Từ");
        var btnIcons = presetRow.add("button", undefined, "✨ Dòng + Tách Icon");

        var clusterPanel = win.add("panel", undefined, "Chọn Cụm đang gán (Click vào chữ trên hình bên dưới để đổi Cụm):");
        clusterPanel.orientation = "row";
        clusterPanel.alignChildren = ["left", "center"];
        clusterPanel.spacing = 8;
        clusterPanel.margins = [10, 12, 10, 6];

        for (var k = 1; k <= 6; k++) {{
            var rb = clusterPanel.add("radiobutton", undefined, "Cụm " + k);
            if (k === 1) rb.value = true;
            rb.clusterIdx = k;
            rb.onClick = function() {{
                activeCluster = this.clusterIdx;
            }};
        }}

        var imgGroup = win.add("panel", undefined, "Preview Đường Mask & Tên Chữ (Click vào chữ để gán vào Cụm đang chọn):");
        imgGroup.orientation = "column";
        imgGroup.alignChildren = ["center", "center"];
        imgGroup.margins = [6, 12, 6, 6];

        var previewImg = imgGroup.add("image", undefined);
        previewImg.preferredSize = [520, 360];

        var paramPanel = win.add("panel", undefined, "Thông số Đường Mask & Anchor Point:");
        paramPanel.orientation = "column";
        paramPanel.alignChildren = ["fill", "top"];
        paramPanel.spacing = 4;
        paramPanel.margins = [10, 12, 10, 6];

        var padRow = paramPanel.add("group");
        padRow.orientation = "row";
        padRow.add("statictext", undefined, "Độ rộng Glow (Padding):");
        var sldPad = padRow.add("slider", undefined, 85, 25, 180);
        sldPad.preferredSize.width = 220;
        var lblPad = padRow.add("statictext", undefined, "85 px");
        lblPad.characters = 6;

        var fthRow = paramPanel.add("group");
        fthRow.orientation = "row";
        fthRow.add("statictext", undefined, "Mask Feather (AE):        ");
        var sldFth = fthRow.add("slider", undefined, 5, 0, 25);
        sldFth.preferredSize.width = 220;
        var lblFth = fthRow.add("statictext", undefined, "5 px");
        lblFth.characters = 6;

        var chkAnchor = paramPanel.add("checkbox", undefined, "Tự động đặt tâm Anchor Point (+) vào chính giữa từng cụm chữ");
        chkAnchor.value = true;

        var botRow = win.add("group");
        botRow.orientation = "row";
        botRow.alignChildren = ["fill", "center"];
        botRow.spacing = 8;

        var btnPush = botRow.add("button", undefined, "🚀 TÁCH LAYER & ĐẶT TÊN THEO CHỮ TRONG AE");
        btnPush.preferredSize.height = 34;
        var btnWeb = botRow.add("button", undefined, "🌐 Mở Bảng Web (Dao Cắt)");
        btnWeb.preferredSize.height = 34;

        var statusTxt = win.add("statictext", undefined, "Sẵn sàng.");
        statusTxt.characters = 74;

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
                statusTxt.text = "✅ Layers: " + namesStr;
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
                statusTxt.text = "⚠️ Hãy click chọn 1 Layer PNG/PSD trên Timeline hoặc trong Project Panel!";
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

        previewImg.addEventListener("mousedown", function(e) {{
            var x = e.clientX || e.localX || 0;
            var y = e.clientY || e.localY || 0;
            var res = callApi("/api/su_click", "x=" + x + "&y=" + y + "&cluster=" + activeCluster);
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
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
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


def serialize_scriptui_summary() -> dict:
    clusters = STATE.compute_cluster_masks()
    preview_path = STATE.render_scriptui_preview(clusters)
    return {
        "ok": True,
        "filePath": STATE.file_path,
        "layerName": STATE.layer_name,
        "sourceName": STATE.source_name,
        "files": list_sibling_pngs(STATE.file_path),
        "previewPath": preview_path,
        "blockCount": len(STATE.blocks),
        "clusterCount": len(clusters),
        "clusterNames": [c["name"] for c in clusters]
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

        elif parsed.path == "/api/su_click":
            px = float(qs.get("x", [0])[0])
            py = float(qs.get("y", [0])[0])
            cid = int(qs.get("cluster", [1])[0])
            scale = STATE.preview_scale if STATE.preview_scale > 0 else 1.0
            off_x = getattr(STATE, "preview_off_x", 0)
            off_y = getattr(STATE, "preview_off_y", 0)
            ix = (px - off_x) / scale
            iy = (py - off_y) / scale
            for b in STATE.blocks:
                if b["x"] <= ix <= b["x"] + b["w"] and b["y"] <= iy <= b["y"] + b["h"]:
                    b["cluster_id"] = cid
                    break
            self._send_json(serialize_scriptui_summary())

        elif parsed.path == "/api/su_params":
            pad = int(qs.get("padding", [85])[0])
            STATE.outer_padding = pad
            self._send_json(serialize_scriptui_summary())

        elif parsed.path == "/api/su_push_json":
            feather = float(qs.get("feather", [5.0])[0])
            center_anchor = qs.get("anchor", ["1"])[0] == "1"
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
