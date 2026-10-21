# ✨ AE Text Cluster & Mask Splitter

Plugin & Script tự động nhận diện chữ Tiếng Việt có dấu (**Cross-Platform `VietOCR` ONNXRuntime** chạy độc lập trên cả Windows, macOS và Linux, tự động tải model ONNX nhẹ 42.5 MB từ [Hugging Face `manhhuynhsd/ae-text-cluster-splitter-onnx`](https://huggingface.co/manhhuynhsd/ae-text-cluster-splitter-onnx)), tự động đặt tên Layer theo đúng nội dung chữ tách được, tính toán **đường biên Bezier Mask (Voronoi Seam)** lách qua khe giữa các dòng/dấu câu (giữ trọn hiệu ứng Outer Glow & tia Flare ngang) và tách thành từng Layer độc lập kèm tâm **Anchor Point (`+`)** ngay trong **Adobe After Effects**.

---

## 🚀 Cài đặt nhanh trên máy mới (macOS & Windows)

1. Yêu cầu máy đã cài đặt **Python 3** và **Adobe After Effects**.
2. Chạy file cài đặt tự động trong thư mục này:
   - **macOS**: Nhấp đúp chạy `Install_Mac.command` *(Chuột phải chọn `Open` nếu macOS hỏi quyền bảo mật lần đầu)*.
   - **Windows**: Nhấp đúp chạy `Install_Windows.bat`.
3. Trong After Effects, bật quyền cho phép Script ghi file & kết nối mạng (chỉ cần bật 1 lần):
   - Vào **`Settings` (hoặc `Edit > Preferences`) -> `Scripting & Expressions`**
   - Tích chọn **`Allow Scripts to Write Files and Access Network`**.
4. Mở bảng điều khiển trực tiếp trong After Effects:
   - **Native ScriptUI Panel**: `Window` -> `AE_Text_Cluster_Splitter.jsx` *(hoặc `File` -> `Scripts` -> `AE_Text_Cluster_Splitter.jsx`)*
   - **HTML5 CEP Extension**: `Window` -> `Extensions` -> `Text Cluster Splitter`
   - **Giao diện Web Canvas nâng cao**: `http://127.0.0.1:8765`

---

## 📖 Hướng dẫn sử dụng

1. **Chọn Layer cần tách (Hỗ trợ cả `.png` và Layer trong file `.psd`)**:
   - Click chọn Layer `.png` hoặc Layer thuộc file Photoshop `.psd` (ví dụ `COPY/file.psd` được import dạng *Composition - Retain Layer Sizes* hoặc *Document Size*) trên Timeline / Project Panel của After Effects, sau đó bấm **`🔄 Lấy Layer đang chọn`**.
2. **Chia cụm nhanh (1-Click Presets)**:
   - **`📑 Gộp theo Dòng`**: Tự động gom các chữ cùng hàng ngang thành từng Layer dòng.
   - **`🔤 Tách từng Từ`**: Mỗi từ / icon thành 1 Layer riêng.
   - **`✨ Dòng + Tách Icon`**: Các dòng chữ giữ nguyên theo dòng, riêng các ngôi sao / icon trang trí (`✨`) gom thành 1 Layer riêng.
3. **Tự chọn & gộp cụm tùy ý**:
   - Chọn **`Cụm 1..6`** (hoặc bấm `+ Thêm Cụm` trên bảng Web) rồi **click trực tiếp vào chữ trên hình Preview** (hoặc kéo chuột quét vùng chọn trên bảng Web) để gộp các chữ đó vào chung 1 Layer.
   - Công cụ **`✂️ Dao Cắt Khối`** (trên bảng Web): Kẻ 1 đường cắt qua 2 chữ dính sát nhau để tách đôi thành 2 khối độc lập.
4. **Đẩy vào After Effects (Tự động đặt tên Layer theo chữ)**:
   - Tinh chỉnh `Độ rộng Glow (Padding)` và `Mask Feather` nếu cần, sau đó bấm **`🚀 TÁCH LAYER & TẠO MASK NGAY TRONG AE`**.
   - Layer gốc sẽ tự động được ẩn đi làm backup, các Layer mới sẽ **tự động được đặt tên theo đúng chữ Tiếng Việt nhận diện được trong cụm đó** (ví dụ: `VÀ LÊN ĐỒ`, `ĐI CHƠI CÙNG`, `HỘI MẸ BỈM?`, `✨ Icon`) kèm Bezier Mask mượt và Anchor Point đặt đúng tâm cụm chữ.
