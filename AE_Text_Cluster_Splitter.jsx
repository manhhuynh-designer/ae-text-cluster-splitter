// ============================================================================
// AE Text Cluster & Mask Splitter - Native After Effects Plugin Panel
// ============================================================================
(function(thisObj) {
    var PORT = 8765;
    var PY_EXE = "/Users/huynhducmanh/Desktop/WORKING/Video-editor-main/.venv/bin/python";
    var SCRIPT_PY = "/Users/huynhducmanh/Desktop/WORKING/Video-editor-main/ae-text-cluster-splitter/text_cluster_splitter.py";
    var REQ_FILE = "/var/folders/sj/1cktqpfx2xb566cpz_76w0k40000gp/T/ae_splitter_req.json";
    var IS_MAC = ($.os.indexOf("Mac") !== -1);
    var activeCluster = 1;
    var siblingFiles = [];
    var isUpdatingDropdown = false;

    function ensureServer() {
        var curlCmd = IS_MAC ? "curl" : "curl.exe";
        var ping = system.callSystem(curlCmd + " -s -m 1 http://127.0.0.1:" + PORT + "/api/ping");
        if (!ping || ping.indexOf("ok") === -1) {
            if (IS_MAC) {
                system.callSystem("'" + PY_EXE + "' '" + SCRIPT_PY + "' --port " + PORT + " --no-browser >/tmp/ae_splitter.log 2>&1 &");
            } else {
                system.callSystem('cmd.exe /c start /b "" "' + PY_EXE + '" "' + SCRIPT_PY + '" --port ' + PORT + ' --no-browser');
            }
            $.sleep(1200);
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
        var curlCmd = IS_MAC ? "curl" : "curl.exe";
        var url = "http://127.0.0.1:" + PORT + endpoint + (query ? ("?" + query) : "");
        var res = system.callSystem(curlCmd + ' -s "' + url + '"');
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
        win.alignChildren = ["fill", "top"];
        win.spacing = 5;
        win.margins = 8;

        // Tier 1A: Source Layer & File Selector
        var selRow = win.add("group");
        selRow.orientation = "row";
        selRow.alignChildren = ["left", "center"];
        selRow.spacing = 6;

        var btnFetch = selRow.add("button", undefined, "🔄 Lấy Layer AE");
        btnFetch.preferredSize.width = 135;
        var ddFiles = selRow.add("dropdownlist", undefined, ["(Chọn nhanh file ảnh...)"]);
        ddFiles.preferredSize.width = 375;

        // Tier 1B: 1-Click Presets + Undo
        var presetRow = win.add("group");
        presetRow.orientation = "row";
        presetRow.alignChildren = ["fill", "center"];
        presetRow.spacing = 5;

        var btnLines = presetRow.add("button", undefined, "📑 Theo Dòng");
        var btnWords = presetRow.add("button", undefined, "🔤 Từng Từ");
        var btnIcons = presetRow.add("button", undefined, "✨ Tách Icon");
        var btnUndo = presetRow.add("button", undefined, "↩️ Undo");
        btnUndo.preferredSize.width = 75;

        // Tier 2: Direct-Manipulation Preview Canvas
        var imgGroup = win.add("panel", undefined, "Preview (Click 2 từ = Gộp cụm | Click lại/Shift = Tách riêng | Alt+Click = Cắt đôi):");
        imgGroup.orientation = "column";
        imgGroup.alignChildren = ["center", "center"];
        imgGroup.margins = [6, 12, 6, 6];

        var previewImg = imgGroup.add("image", undefined);
        previewImg.preferredSize = [520, 340];

        // Tier 3: 1-Line Editable Layer Names
        var nameRow = win.add("group");
        nameRow.orientation = "row";
        nameRow.alignChildren = ["left", "center"];
        nameRow.spacing = 6;

        var lblName = nameRow.add("statictext", undefined, "Tên Layer:");
        lblName.preferredSize.width = 62;
        var txtNames = nameRow.add("edittext", undefined, "");
        txtNames.preferredSize.width = 450;
        txtNames.helpTip = "Tên các Layer sẽ tạo trong AE (ngăn cách bởi dấu |). Bạn có thể sửa trực tiếp tại đây!";

        // Collapsible Advanced Settings (hidden by default to keep UI ultra-clean)
        var paramPanel = win.add("panel", undefined, "Thông số nâng cao (Mask & Anchor Point):");
        paramPanel.orientation = "column";
        paramPanel.alignChildren = ["fill", "top"];
        paramPanel.spacing = 4;
        paramPanel.margins = [10, 12, 10, 6];
        paramPanel.visible = false;
        paramPanel.maximumSize.height = 0;

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

        // Tier 4: Primary Action Row
        var botRow = win.add("group");
        botRow.orientation = "row";
        botRow.alignChildren = ["fill", "center"];
        botRow.spacing = 6;

        var btnPush = botRow.add("button", undefined, "🚀 TÁCH LAYER VÀO AE");
        btnPush.preferredSize.width = 390;
        btnPush.preferredSize.height = 34;

        var btnGear = botRow.add("button", undefined, "⚙️");
        btnGear.preferredSize.width = 48;
        btnGear.preferredSize.height = 34;
        btnGear.helpTip = "Ẩn/Hiện thông số nâng cao (Padding, Feather, Anchor Point)";

        var btnWeb = botRow.add("button", undefined, "🌐");
        btnWeb.preferredSize.width = 48;
        btnWeb.preferredSize.height = 34;
        btnWeb.helpTip = "Mở giao diện Web đầy đủ (Dao cắt tự do)";

        var statusTxt = win.add("statictext", undefined, "Sẵn sàng. Click chọn Layer trên AE để bắt đầu.");
        statusTxt.characters = 74;

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

        function loadFileByPath(fp, info) {
            var cleanPath = escJson(fp);
            var lName = info ? escJson(info.layerName) : "";
            var sName = info ? escJson(info.sourceName) : "";
            var lw = (info && info.layerWidth) ? info.layerWidth : 0;
            var lh = (info && info.layerHeight) ? info.layerHeight : 0;
            writeReqJson('{"file": "' + cleanPath + '", "layerName": "' + lName + '", "sourceName": "' + sName + '", "layerWidth": ' + lw + ', "layerHeight": ' + lh + '}');
            var res = callApi("/api/su_init_from_req", "");
            refreshPreview(res);
        }

        function loadCurrentFromAE() {
            var info = detectSelectedLayerOrItemInAE();
            if (!info || !info.path) {
                statusTxt.text = "⚠️ Hãy click chọn 1 Layer ảnh/PSD trên Timeline hoặc trong Project Panel!";
                return;
            }
            loadFileByPath(info.path, info);
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
            paramPanel.maximumSize.height = showAdv ? 140 : 0;
            win.layout.layout(true);
            win.layout.resize();
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
