// ============================================================================
// AE Text Cluster & Mask Splitter - Native After Effects Plugin Panel
// ============================================================================
(function(thisObj) {
    var PORT = 8765;
    var PY_EXE = "/Users/huynhducmanh/Desktop/WORKING/Video-editor-main/ae-text-cluster-splitter/../.venv/bin/python";
    var SCRIPT_PY = "/Users/huynhducmanh/Desktop/WORKING/Video-editor-main/ae-text-cluster-splitter/text_cluster_splitter.py";
    var REQ_FILE = "/var/folders/sj/1cktqpfx2xb566cpz_76w0k40000gp/T/ae_splitter_req.json";
    var RENDER_FILE = "/var/folders/sj/1cktqpfx2xb566cpz_76w0k40000gp/T/ae_splitter_src_render.png";
    var IS_MAC = ($.os.indexOf("Mac") !== -1);
    var activeCluster = 1;
    var siblingFiles = [];
    var isUpdatingDropdown = false;

    // In-process HTTP GET over ExtendScript Socket: no curl / cmd.exe process spawn per click (system.callSystem
    // costs ~150-400 ms + a console flash on Windows, and decodes output in the ANSI codepage which garbles Vietnamese).
    function httpGet(pathQuery, timeoutSec) {
        var tmo = timeoutSec || 30;
        try {
            var s = new Socket();
            s.timeout = tmo;
            if (!s.open("127.0.0.1:" + PORT, "BINARY")) return null;
            s.write("GET " + pathQuery + " HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
            // Read until the body reaches Content-Length (the server may close the socket before our first read,
            // so never gate the loop on s.connected: buffered data must still be drained).
            var raw = "";
            var t0 = new Date().getTime();
            while (true) {
                var chunk = s.read(65536);
                if (chunk && chunk.length) raw += chunk;
                var hs = raw.indexOf("\r\n\r\n");
                if (hs !== -1) {
                    var m = raw.substring(0, hs).match(/Content-Length:\s*(\d+)/i);
                    if (m && (raw.length - hs - 4) >= parseInt(m[1], 10)) break;
                }
                if (s.eof) break;
                if (!chunk && !s.connected) break;
                if (new Date().getTime() - t0 > tmo * 1000) break;
            }
            s.close();
            var sep = raw.indexOf("\r\n\r\n");
            if (sep === -1) return null;
            var body = raw.substring(sep + 4);
            try { body = decodeURIComponent(escape(body)); } catch (eDec) {}
            return body;
        } catch (eSock) {
            return null;
        }
    }

    // Fallback transport (old behaviour) if Socket is blocked on this machine
    function curlGet(pathQuery, timeoutSec) {
        var curlCmd = IS_MAC ? "curl" : "curl.exe";
        var res = system.callSystem(curlCmd + " -s -m " + (timeoutSec || 30) + ' "http://127.0.0.1:' + PORT + pathQuery + '"');
        return (res && res.length) ? res : null;
    }

    var useCurl = false;
    function apiGet(pathQuery, timeoutSec) {
        if (!useCurl) {
            var r = httpGet(pathQuery, timeoutSec);
            if (r !== null) return r;
        }
        var rc = curlGet(pathQuery, timeoutSec);
        if (rc !== null) useCurl = true;
        return rc;
    }

    var serverAlive = false;
    function ensureServer() {
        if (serverAlive) return;
        var ping = apiGet("/api/ping", 2);
        if (ping && ping.indexOf("ok") !== -1) { serverAlive = true; return; }
        if (IS_MAC) {
            system.callSystem("'" + PY_EXE + "' '" + SCRIPT_PY + "' --port " + PORT + " --no-browser >/tmp/ae_splitter.log 2>&1 &");
        } else {
            // Launch detached through a .bat + File.execute(): system.callSystem('start /b ...') keeps the child's
            // stdout pipe open and can freeze AE until the server exits. pythonw.exe = no console window.
            var pyw = PY_EXE.replace(/python\.exe$/i, "pythonw.exe");
            if (!new File(pyw).exists) pyw = PY_EXE;
            var bat = new File(Folder.temp.fsName + "/ae_splitter_start.bat");
            bat.encoding = "UTF-8";
            bat.lineFeed = "Windows";
            bat.open("w");
            bat.writeln("@echo off");
            bat.writeln("chcp 65001 >nul");
            bat.writeln('start "" "' + pyw.replace(/\//g, "\\") + '" "' + SCRIPT_PY.replace(/\//g, "\\") + '" --port ' + PORT + ' --no-browser');
            bat.close();
            bat.execute();
        }
        for (var w = 0; w < 25; w++) {
            $.sleep(400);
            ping = apiGet("/api/ping", 2);
            if (ping && ping.indexOf("ok") !== -1) { serverAlive = true; return; }
        }
    }

    function writeReqJson(objStr) {
        var f = new File(REQ_FILE);
        f.encoding = "UTF-8";
        f.open("w");
        f.write(objStr);
        f.close();
    }

    function callApi(endpoint, query) {
        ensureServer();
        var pq = endpoint + (query ? ("?" + query) : "");
        var res = apiGet(pq, 60);
        if (res === null) {
            serverAlive = false;
            ensureServer();
            res = apiGet(pq, 60);
        }
        try {
            return eval("(" + res + ")");
        } catch (e) {
            return { ok: false, error: res || "Lỗi kết nối server" };
        }
    }

    function detectSelectedLayerOrItemInAE() {
        function isImageFile(fileObj) {
            if (!fileObj) return false;
            var nm = fileObj.name.toLowerCase();
            return (nm.indexOf(".png") !== -1 || nm.indexOf(".psd") !== -1 || nm.indexOf(".jpg") !== -1 || nm.indexOf(".jpeg") !== -1 || nm.indexOf(".webp") !== -1 || nm.indexOf(".tif") !== -1 || nm.indexOf(".tiff") !== -1 || nm.indexOf(".bmp") !== -1);
        }
        function isImageLayer(l) {
            return (l && l.source && l.source.file && isImageFile(l.source.file));
        }
        function isSplitLayer(l) {
            return (l && (l.comment === "AE_TEXT_CLUSTER_SPLIT" || l.name.indexOf("[Cụm ") === 0));
        }
        function makeLayerInfo(l, compName) {
            return {
                path: l.source.file.fsName,
                layerName: l.name || "",
                sourceName: (l.source && l.source.name) ? l.source.name : "",
                layerWidth: l.width || 0,
                layerHeight: l.height || 0,
                item: l.source,
                label: l.name + " (" + compName + ")"
            };
        }
        function makeFootageInfo(item) {
            var lName = item.name || "";
            if (lName.indexOf("/") !== -1) lName = lName.split("/")[0];
            return {
                path: item.file.fsName,
                layerName: lName,
                sourceName: item.name || "",
                layerWidth: item.width || 0,
                layerHeight: item.height || 0,
                item: item,
                label: item.name + " (Project Panel)"
            };
        }

        var act = app.project.activeItem;

        if (act && (act instanceof CompItem)) {
            if (act.selectedLayers && act.selectedLayers.length > 0) {
                for (var s = 0; s < act.selectedLayers.length; s++) {
                    var sl = act.selectedLayers[s];
                    if (isImageLayer(sl)) {
                        return makeLayerInfo(sl, act.name);
                    }
                    if (sl.source && (sl.source instanceof CompItem)) {
                        var sub = sl.source;
                        for (var k = 1; k <= sub.numLayers; k++) {
                            if (isImageLayer(sub.layer(k))) {
                                return makeLayerInfo(sub.layer(k), sub.name);
                            }
                        }
                    }
                }
            }
            for (var i = 1; i <= act.numLayers; i++) {
                var l = act.layer(i);
                if (isImageLayer(l) && !isSplitLayer(l)) {
                    return makeLayerInfo(l, act.name);
                }
            }
            for (var i2 = 1; i2 <= act.numLayers; i2++) {
                var l2 = act.layer(i2);
                if (isImageLayer(l2)) {
                    return makeLayerInfo(l2, act.name);
                }
            }
        }

        if (act && (act instanceof FootageItem) && act.file && isImageFile(act.file)) {
            return makeFootageInfo(act);
        }

        if (app.project.selection && app.project.selection.length > 0) {
            for (var p = 0; p < app.project.selection.length; p++) {
                var sel = app.project.selection[p];
                if (sel instanceof FootageItem && sel.file && isImageFile(sel.file)) {
                    return makeFootageInfo(sel);
                }
                if (sel instanceof CompItem) {
                    for (var j = 1; j <= sel.numLayers; j++) {
                        var lj = sel.layer(j);
                        if (isImageLayer(lj) && !isSplitLayer(lj)) {
                            sel.openInViewer();
                            return makeLayerInfo(lj, sel.name);
                        }
                    }
                }
            }
        }

        return null;
    }

    function buildUI(thisObj) {
        var win = (thisObj instanceof Panel)
            ? thisObj
            : new Window("palette", "✨ AE Text Cluster & Mask Splitter", undefined, { resizeable: true });

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
        ddFiles.preferredSize = [285, 25];
        var chkAuto = selRow.add("checkbox", undefined, "Tự nạp");
        chkAuto.value = true;
        chkAuto.helpTip = "Tự động nạp lại khi bạn click chọn 1 Layer ảnh/PSD khác trên Timeline hoặc Project Panel";

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

        function applyScrollOffset() {
            contentGrp.location = [0, -Math.round(vScroll.value)];
        }

        // Measure the natural (unclipped) content size from the visible children laid out by ScriptUI
        function measureContent() {
            var h = 0, w = 0;
            for (var i = 0; i < contentGrp.children.length; i++) {
                var c = contentGrp.children[i];
                if (!c.visible || !c.size) continue;
                h = Math.max(h, c.location[1] + c.size[1]);
                w = Math.max(w, c.location[0] + c.size[0]);
            }
            natH = h + 6;
            natW = w + 4;
        }

        // Manually clip the viewport to the docked panel size (the layout manager would otherwise grow it to fit
        // the content, so the panel gets cropped and the scrollbar has nothing to scroll)
        function updateScroll() {
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
        }

        vScroll.onChanging = vScroll.onChange = function() {
            applyScrollOffset();
        };

        win.onResizing = win.onResize = function() {
            this.layout.resize();
            updateScroll();
        };

        try {
            win.addEventListener("mousewheel", function(e) {
                var d = (e.detail || e.wheelDelta || 0);
                var step = (d > 0) ? -28 : 28;
                vScroll.value = Math.max(0, Math.min(vScroll.maxvalue, vScroll.value + step));
                applyScrollOffset();
            });
        } catch (eWheel) {}

        function refreshPreview(res) {
            if (res && res.ok && res.previewPath) {
                var f = new File(res.previewPath);
                if (f.exists) {
                    previewImg.image = f;
                }
                if (res.files && res.files.length > 0) {
                    isUpdatingDropdown = true;
                    ddFiles.removeAll();
                    siblingFiles = res.files;
                    var selIdx = 0;
                    for (var i = 0; i < siblingFiles.length; i++) {
                        var parts = siblingFiles[i].split("/");
                        var bname = parts[parts.length - 1];
                        ddFiles.add("item", bname);
                        if (res.filePath && siblingFiles[i] === res.filePath) {
                            selIdx = i;
                        }
                    }
                    ddFiles.selection = selIdx;
                    isUpdatingDropdown = false;
                }
                var namesStr = (res.clusterNames && res.clusterNames.length) ? res.clusterNames.join(" | ") : "";
                txtNames.text = namesStr;
                var selHint = res.selectedText ? (" [Đang chọn: " + res.selectedText + " -> Click từ khác để gộp, hoặc click lại để tách riêng]") : "";
                statusTxt.text = "✅ " + (res.clusterCount || 0) + " Cụm (" + (res.blockCount || 0) + " từ)" + selHint;
            } else if (res && res.error) {
                statusTxt.text = "⚠️ " + res.error;
            }
        }

        function escJson(s) {
            return String(s || "").replace(/\\/g, "/").replace(/"/g, '\\"');
        }

        // Render a PSD footage item exactly as AE draws it (Layer Styles, smart objects, AE's own layer bounds), so the
        // masks are computed in the same pixel frame as the AE layer. psd-tools can't reproduce layer effects and may
        // not find renamed / nested layers.
        function renderFootageToPng(fi) {
            var out = new File(RENDER_FILE);
            var tmpComp = null;
            try {
                if (out.exists) out.remove();
                app.beginUndoGroup("Text Splitter preview render");
                tmpComp = app.project.items.addComp("__TCS_RENDER__", Math.max(4, fi.width), Math.max(4, fi.height), 1, 1, 25);
                tmpComp.layers.add(fi);
                tmpComp.saveFrameToPng(0, out);
                // saveFrameToPng writes asynchronously
                var waited = 0;
                while ((!out.exists || out.length === 0) && waited < 8000) { $.sleep(50); waited += 50; }
                $.sleep(60);
            } catch (eR) {
            } finally {
                try { if (tmpComp) tmpComp.remove(); } catch (eRm) {}
                try { app.endUndoGroup(); } catch (eU) {}
            }
            return (out.exists && out.length > 0) ? out.fsName : "";
        }

        function loadFileByPath(fp, info) {
            var cleanPath = escJson(fp);
            var lName = info ? escJson(info.layerName) : "";
            var sName = info ? escJson(info.sourceName) : "";
            var lw = (info && info.layerWidth) ? info.layerWidth : 0;
            var lh = (info && info.layerHeight) ? info.layerHeight : 0;
            var renderPath = "";
            if (info && info.item && info.item.footageMissing) {
                statusTxt.text = "⚠️ Footage '" + info.item.name + "' đang bị mất (offline) trong AE. Hãy Relink file trước khi tách.";
                return;
            }
            if (info && info.item && /\.psd$/i.test(String(fp))) {
                statusTxt.text = "⏳ Đang render layer PSD từ After Effects...";
                renderPath = escJson(renderFootageToPng(info.item));
            }
            writeReqJson('{"file": "' + cleanPath + '", "layerName": "' + lName + '", "sourceName": "' + sName + '", "layerWidth": ' + lw + ', "layerHeight": ' + lh + ', "renderPath": "' + renderPath + '"}');
            var res = callApi("/api/su_init_from_req", "");
            refreshPreview(res);
        }

        function loadCurrentFromAE() {
            var info = detectSelectedLayerOrItemInAE();
            if (!info || !info.path) {
                statusTxt.text = "⚠️ Hãy click chọn 1 Layer ảnh/PSD trên Timeline hoặc trong Project Panel!";
                return;
            }
            lastAutoKey = info.path + "|" + (info.sourceName || info.layerName || "");
            loadFileByPath(info.path, info);
        }

        // ---- Auto-follow: reload when the user selects a DIFFERENT image layer while the panel is open ----
        var lastAutoKey = "";
        function hasExplicitSelection() {
            var act = app.project ? app.project.activeItem : null;
            if (act && (act instanceof CompItem) && act.selectedLayers && act.selectedLayers.length > 0) return true;
            if (act && (act instanceof FootageItem)) return true;
            return !!(app.project && app.project.selection && app.project.selection.length > 0);
        }
        function autoFollowTick() {
            try {
                if (!win || !win.visible) return;
                if (!chkAuto.value || !hasExplicitSelection()) return;
                var info = detectSelectedLayerOrItemInAE();
                if (!info || !info.path) return;
                // Same source file + same PSD sub-layer = same key, so the split layers created by
                // "TÁCH LAYER" (duplicates of the same source) never reset the user's clusters.
                var key = info.path + "|" + (info.sourceName || info.layerName || "");
                if (key === lastAutoKey) return;
                lastAutoKey = key;
                statusTxt.text = "⏳ Đang nạp layer: " + (info.layerName || info.path) + " ...";
                loadFileByPath(info.path, info);
            } catch (eAuto) {}
        }
        if ($.global.aeTCSAutoTask) {
            try { app.cancelTask($.global.aeTCSAutoTask); } catch (eC) {}
        }
        $.global.aeTCSAutoTick = autoFollowTick;
        $.global.aeTCSAutoTask = app.scheduleTask("if ($.global.aeTCSAutoTick) $.global.aeTCSAutoTick();", 700, true);
        if (win instanceof Window) {
            win.onClose = function() {
                try { app.cancelTask($.global.aeTCSAutoTask); } catch (eX) {}
                $.global.aeTCSAutoTask = null;
                $.global.aeTCSAutoTick = null;
            };
        }

        btnFetch.onClick = function() {
            loadCurrentFromAE();
        };

        ddFiles.onChange = function() {
            if (isUpdatingDropdown || !ddFiles.selection) return;
            var idx = ddFiles.selection.index;
            if (idx >= 0 && idx < siblingFiles.length) {
                loadFileByPath(siblingFiles[idx]);
            }
        };

        btnLines.onClick = function() {
            var res = callApi("/api/su_preset", "mode=lines");
            refreshPreview(res);
        };

        btnWords.onClick = function() {
            var res = callApi("/api/su_preset", "mode=words");
            refreshPreview(res);
        };

        btnIcons.onClick = function() {
            var res = callApi("/api/su_preset", "mode=lines_icons");
            refreshPreview(res);
        };

        btnUndo.onClick = function() {
            var res = callApi("/api/su_undo", "");
            refreshPreview(res);
        };

        btnGear.onClick = function() {
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
            if (showAdv) {
                // Scroll so the newly opened settings + action row are visible
                vScroll.value = Math.max(0, natH - ((win.size && win.size[1]) ? win.size[1] - 8 : natH));
            }
            updateScroll();
        };

        previewImg.addEventListener("mousedown", function(e) {
            var x = e.clientX || e.localX || 0;
            var y = e.clientY || e.localY || 0;
            var kb = ScriptUI.environment.keyboardState;
            var alt = (e.altKey || (kb && kb.altKey)) ? "1" : "0";
            var shift = (e.shiftKey || (kb && kb.shiftKey)) ? "1" : "0";
            var res = callApi("/api/su_click", "x=" + x + "&y=" + y + "&alt=" + alt + "&shift=" + shift);
            refreshPreview(res);
        });

        sldPad.onChanging = function() {
            lblPad.text = Math.round(this.value) + " px";
        };
        sldPad.onChange = function() {
            var res = callApi("/api/su_params", "padding=" + Math.round(this.value));
            refreshPreview(res);
        };

        sldFth.onChanging = function() {
            lblFth.text = Math.round(this.value) + " px";
        };

        btnPush.onClick = function() {
            statusTxt.text = "⏳ Đang tách Layer và đặt tên theo chữ trong After Effects...";
            var fth = Math.round(sldFth.value);
            var anc = chkAnchor.value ? "1" : "0";
            writeReqJson('{"customPipeNames": "' + escJson(txtNames.text) + '"}');
            var res = callApi("/api/su_push_json", "feather=" + fth + "&anchor=" + anc);
            if (res && res.ok && res.jsxPath) {
                $.evalFile(new File(res.jsxPath));
                var namesStr = (res.clusterNames && res.clusterNames.length) ? res.clusterNames.join(" | ") : (res.clusterCount + " Layer");
                statusTxt.text = "✅ Đã tạo: " + namesStr;
            } else {
                statusTxt.text = "❌ Lỗi: " + (res ? res.error : "Không tạo được Mask");
            }
        };

        btnWeb.onClick = function() {
            ensureServer();
            if (IS_MAC) {
                system.callSystem("open 'http://127.0.0.1:" + PORT + "'");
            } else {
                system.callSystem('cmd.exe /c start "" "http://127.0.0.1:' + PORT + '"');
            }
        };

        win.layout.layout(true);
        updateScroll();
        loadCurrentFromAE();
        return win;
    }

    if ($.global.aeTextSplitterWin && $.global.aeTextSplitterWin instanceof Window) {
        try { $.global.aeTextSplitterWin.close(); } catch (e) {}
    }
    var myWin = buildUI(thisObj);
    if (myWin instanceof Window) {
        $.global.aeTextSplitterWin = myWin;
        myWin.center();
        myWin.show();
    }
})(this);
