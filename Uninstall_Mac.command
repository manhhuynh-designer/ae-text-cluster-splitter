#!/bin/bash
# ==========================================================
#  Go cai dat AE Text Cluster & Mask Splitter cho macOS
#  - Khong can Python (van chay duoc khi .venv da bi xoa)
# ==========================================================
cd "$(dirname "$0")"
echo "=========================================================="
echo "  Go cai dat AE Text Cluster & Mask Splitter (macOS)"
echo "=========================================================="
echo ""
echo "👉 Hay dong After Effects truoc khi go (de AE khong giu file panel)."
read -p "Tiep tuc go cai dat? (y/N): " ans
case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Da huy."; exit 0 ;;
esac

removed=0

# 1. Dung server nen (port 8765)
if pgrep -f "text_cluster_splitter.py" >/dev/null 2>&1; then
    pkill -f "text_cluster_splitter.py" && echo "✅ Da dung server nen text_cluster_splitter.py"
fi

# 2. Xoa ScriptUI Panel / Script trong moi phien ban After Effects
for base in "$HOME/Library/Preferences/Adobe/After Effects" "$HOME/Library/Application Support/Adobe/After Effects"; do
    [ -d "$base" ] || continue
    for ver in "$base"/2*; do
        for f in "$ver/Scripts/AE_Text_Cluster_Splitter.jsx" "$ver/Scripts/ScriptUI Panels/AE_Text_Cluster_Splitter.jsx"; do
            if [ -f "$f" ]; then
                rm -f "$f" && echo "🗑  $f" && removed=$((removed+1))
            fi
        done
    done
done

# 3. Xoa CEP Extension
CEP_DIR="$HOME/Library/Application Support/Adobe/CEP/extensions/com.manh.textclustersplitter"
if [ -d "$CEP_DIR" ]; then
    rm -rf "$CEP_DIR" && echo "🗑  $CEP_DIR" && removed=$((removed+1))
fi

# 4. Xoa file tam
TMP_BASE="${TMPDIR:-/tmp}"
rm -f "$TMP_BASE"/ae_splitter_* /tmp/ae_splitter_* /tmp/ae_splitter.log 2>/dev/null
echo "🧹 Da don file tam ae_splitter_*"

# 5. (Tuy chon) Tat PlayerDebugMode cua Adobe CEP
echo ""
echo "PlayerDebugMode dung chung cho MOI extension CEP chua ky (khong chi plugin nay)."
read -p "Tat PlayerDebugMode? Chi chon y neu ban khong dung extension CEP nao khac (y/N): " dbg
case "$dbg" in
    y|Y)
        for v in 9 10 11 12 13; do
            defaults delete com.adobe.CSXS.$v PlayerDebugMode >/dev/null 2>&1
        done
        echo "✅ Da tat PlayerDebugMode"
        ;;
esac

# 6. (Tuy chon) Xoa moi truong Python + model ONNX trong thu muc nay
echo ""
read -p "Xoa .venv va model ONNX (~45 MB) trong thu muc nay? (y/N): " purge
case "$purge" in
    y|Y)
        rm -rf ".venv" "__pycache__" models/*.onnx models/*.tmp 2>/dev/null
        echo "✅ Da xoa .venv, __pycache__ va model ONNX"
        ;;
esac

echo ""
echo "✅ GO CAI DAT HOAN TAT! (Da xoa $removed muc khoi After Effects)"
echo "👉 Mo lai After Effects de menu Window khong con hien plugin."
echo "👉 Muon xoa han, ban co the xoa ca thu muc nay: $(pwd)"
read -p "Nhan Enter de dong..."
