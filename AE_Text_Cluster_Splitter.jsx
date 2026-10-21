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
            return (nm.indexOf(".png") !== -1 || nm.indexOf(".jpg") !== -1 || nm.indexOf(".jpeg") !== -1 || nm.indexOf(".webp") !== -1);
        }
        function isImageLayer(l) {
            return (l && l.source && l.source.file && isImageFile(l.source.file));
        }
        function isSplitLayer(l) {
            return (l && (l.comment === "AE_TEXT_CLUSTER_SPLIT" || l.name.indexOf("[Cụm ") === 0));
        }

        var act = app.project.activeItem;

        if (act && (act instanceof CompItem)) {
            if (act.selectedLayers && act.selectedLayers.length > 0) {
                for (var s = 0; s < act.selectedLayers.length; s++) {
                    var sl = act.selectedLayers[s];
                    if (isImageLayer(sl)) {
                        return { path: sl.source.file.fsName, label: sl.name + " (" + act.name + ")" };
                    }
                    if (sl.source && (sl.source instanceof CompItem)) {
                        var sub = sl.source;
                        for (var k = 1; k <= sub.numLayers; k++) {
                            if (isImageLayer(sub.layer(k))) {
                                return { path: sub.layer(k).source.file.fsName, label: sub.layer(k).name + " (" + sub.name + ")" };
                            }
                        }
                    }
                }
            }
        }

        if (app.project.selection && app.project.selection.length > 0) {
            for (var p = 0; p < app.project.selection.length; p++) {
                var sel = app.project.selection[p];
                if (sel instanceof FootageItem && sel.file && isImageFile(sel.file)) {
                    return { path: sel.file.fsName, label: sel.name + " (Project Panel)" };
                }
                if (sel instanceof CompItem) {
                    for (var j = 1; j <= sel.numLayers; j++) {
                        var lj = sel.layer(j);
                        if (isImageLayer(lj) && !isSplitLayer(lj)) {
                            sel.openInViewer();
                            return { path: lj.source.file.fsName, label: lj.name + " (" + sel.name + ")" };
                        }
                    }
                }
            }
        }

        if (act && (act instanceof FootageItem) && act.file && isImageFile(act.file)) {
            return { path: act.file.fsName, label: act.name + " (Project Panel)" };
        }

        if (act && (act instanceof CompItem)) {
            for (var i = 1; i <= act.numLayers; i++) {
                var l = act.layer(i);
                if (isImageLayer(l) && !isSplitLayer(l)) {
                    return { path: l.source.file.fsName, label: l.name + " (" + act.name + ")" };
                }
            }
            for (var i2 = 1; i2 <= act.numLayers; i2++) {
                var l2 = act.layer(i2);
                if (isImageLayer(l2)) {
                    return { path: l2.source.file.fsName, label: l2.name + " (" + act.name + ")" };
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

        for (var k = 1; k <= 6; k++) {
            var rb = clusterPanel.add("radiobutton", undefined, "Cụm " + k);
            if (k === 1) rb.value = true;
            rb.clusterIdx = k;
            rb.onClick = function() {
                activeCluster = this.clusterIdx;
            };
        }

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
                statusTxt.text = "✅ Layers: " + namesStr;
            } else if (res && res.error) {
                statusTxt.text = "⚠️ " + res.error;
            }
        }

        function loadFileByPath(fp) {
            var cleanPath = fp.replace(/\\/g, "/").replace(/"/g, '\\"');
            writeReqJson('{"file": "' + cleanPath + '"}');
            var res = callApi("/api/su_init_from_req", "");
            refreshPreview(res);
        }

        function loadCurrentFromAE() {
            var info = detectSelectedLayerOrItemInAE();
            if (!info || !info.path) {
                statusTxt.text = "⚠️ Hãy click chọn 1 Layer PNG trên Timeline hoặc trong Project Panel!";
                return;
            }
            loadFileByPath(info.path);
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

        previewImg.addEventListener("mousedown", function(e) {
            var x = e.clientX || e.localX || 0;
            var y = e.clientY || e.localY || 0;
            var res = callApi("/api/su_click", "x=" + x + "&y=" + y + "&cluster=" + activeCluster);
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
