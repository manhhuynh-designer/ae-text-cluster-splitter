#!/bin/bash
cd "$(dirname "$0")"
echo "=========================================================="
echo "  Cai dat AE Text Cluster & Mask Splitter cho macOS"
echo "=========================================================="

# 1. Tim Python 3
PY_BIN=$(which python3)
if [ -z "$PY_BIN" ]; then
    echo "❌ Khong tim thay python3! Vui long cai Python 3 (brew install python)."
    read -p "Nhan Enter de dong..."
    exit 1
fi

# 2. Tao moi truong ao .venv ngay trong thu muc nay
if [ ! -d ".venv" ]; then
    echo "⏳ Dang tao moi truong Python (.venv)..."
    "$PY_BIN" -m venv .venv
fi

echo "⏳ Dang cai dat thu vien xu ly anh & ONNXRuntime (opencv, numpy, scipy, pillow, onnxruntime)..."
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q

# 3. Bat PlayerDebugMode cho Adobe CEP
for v in 9 10 11 12 13; do
    defaults write com.adobe.CSXS.$v PlayerDebugMode 1
done

# 4. Cai dat ScriptUI Panel & CEP Extension vao After Effects
./.venv/bin/python text_cluster_splitter.py --install-only

echo ""
echo "✅ CAI DAT HOAN TAT!"
echo "👉 Trong After Effects, hay bat: Settings > Scripting & Expressions > Allow Scripts to Write Files and Access Network."
echo "👉 Mo cong cu tai menu: Window > AE_Text_Cluster_Splitter.jsx"
echo "   hoac: Window > Extensions > Text Cluster Splitter"
read -p "Nhan Enter de hoan tat..."
