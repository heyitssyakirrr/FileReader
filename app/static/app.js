document.addEventListener("DOMContentLoaded", function () {

    var maxUploadMb = Number(document.body.dataset.maxUploadMb || "10");

    // -------------------------------------------------------------------------
    // TAB SWITCHING
    // -------------------------------------------------------------------------
    window.switchTab = function (tab) {
        var panels = {
            summarise: document.getElementById("panelSummarise"),
            batch:     document.getElementById("panelBatch"),
        };
        var tabs = {
            summarise: document.getElementById("tabSummarise"),
            batch:     document.getElementById("tabBatch"),
        };

        Object.keys(panels).forEach(function (key) {
            var panel = panels[key];
            if (!panel) return;
            if (key === tab) {
                panel.style.display = "flex";
                panel.style.flexDirection = "column";
            } else {
                panel.style.display = "none";
            }
        });

        Object.keys(tabs).forEach(function (key) {
            var btn = tabs[key];
            if (!btn) return;
            btn.classList.toggle("active", key === tab);
        });
    };

    // -------------------------------------------------------------------------
    // HELPERS
    // -------------------------------------------------------------------------
    function escHtml(str) {
        return String(str || "")
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function setField(el, value) {
        if (value) {
            el.textContent = value;
            el.classList.add("found");
            el.classList.remove("not-found");
        } else {
            el.textContent = "Not found";
            el.classList.add("not-found");
            el.classList.remove("found");
        }
    }

    // -------------------------------------------------------------------------
    // STATEMENT SUMMARY WIDGET
    // -------------------------------------------------------------------------
    var resOverallMin     = document.getElementById("resOverallMin");
    var resOverallMax     = document.getElementById("resOverallMax");
    var resOverallClosing = document.getElementById("resOverallClosing");
    var resMonthlyTable   = document.getElementById("resMonthlyTable");
    var resDailyTable     = document.getElementById("resDailyTable");

    if (resOverallMin) {
        createUploadWidget({
            dropZoneId:      "dropZoneSummarise",
            fileInputId:     "fileInputSummarise",
            filePillId:      "filePillSummarise",
            fileNameId:      "fileNameSummarise",
            clearFileBtnId:  "clearFileBtnSummarise",
            submitBtnId:     "submitBtnSummarise",
            submitSpinnerId: "submitSpinnerSummarise",
            submitLabelId:   "submitLabelSummarise",
            errorBannerId:   "errorBannerSummarise",
            errorMessageId:  "errorMessageSummarise",
            warnBannerId:    "warnBannerSummarise",
            warnMessageId:   "warnMessageSummarise",
            resetBtnId:      "resetBtnSummarise",
            pageContainerId: "pageContainerSummarise",
            extractUrl:      "/summarise/from-file",
            submitLabel:     "Extract Summary",
            pdfOnly:         false,

            onResults: function (result) {
                var data = result.data || {};
                setField(resOverallMin,     data.overall_min_balance);
                setField(resOverallMax,     data.overall_max_balance);
                setField(resOverallClosing, data.overall_closing_balance);

                var monthly = data.monthly_summaries || [];
                if (monthly.length) {
                    var mHtml = "<table class='summary-table'><thead><tr><th>Month</th><th>Min</th><th>Max</th><th>Closing</th></tr></thead><tbody>";
                    monthly.forEach(function (m) {
                        mHtml += "<tr><td>" + (m.month || "\u2014") + "</td><td>" + (m.min_balance || "\u2014") + "</td><td>" + (m.max_balance || "\u2014") + "</td><td>" + (m.closing_balance || "\u2014") + "</td></tr>";
                    });
                    mHtml += "</tbody></table>";
                    resMonthlyTable.innerHTML = mHtml;
                } else {
                    resMonthlyTable.textContent = "No monthly data.";
                }

                var daily = data.daily_summaries || [];
                if (daily.length) {
                    var dHtml = "<table class='summary-table'><thead><tr><th>Date</th><th>Min</th><th>Max</th><th>Closing</th></tr></thead><tbody>";
                    daily.forEach(function (d) {
                        dHtml += "<tr><td>" + (d.date || "\u2014") + "</td><td>" + (d.min_balance || "\u2014") + "</td><td>" + (d.max_balance || "\u2014") + "</td><td>" + (d.closing_balance || "\u2014") + "</td></tr>";
                    });
                    dHtml += "</tbody></table>";
                    resDailyTable.innerHTML = dHtml;
                } else {
                    resDailyTable.textContent = "No daily data.";
                }
            },

            onReset: function () {
                setField(resOverallMin, null);
                setField(resOverallMax, null);
                setField(resOverallClosing, null);
                resMonthlyTable.innerHTML = "\u2014";
                resDailyTable.innerHTML   = "\u2014";
            }
        });
    }

    // -------------------------------------------------------------------------
    // GENERIC SINGLE-FILE UPLOAD WIDGET (used by summarise tab)
    // -------------------------------------------------------------------------
    function createUploadWidget(cfg) {
        var selectedFile = null;

        var dropZone      = document.getElementById(cfg.dropZoneId);
        var fileInput     = document.getElementById(cfg.fileInputId);
        var filePill      = document.getElementById(cfg.filePillId);
        var fileName      = document.getElementById(cfg.fileNameId);
        var clearFileBtn  = document.getElementById(cfg.clearFileBtnId);
        var submitBtn     = document.getElementById(cfg.submitBtnId);
        var submitSpinner = document.getElementById(cfg.submitSpinnerId);
        var submitLabel   = document.getElementById(cfg.submitLabelId);
        var errorBanner   = document.getElementById(cfg.errorBannerId);
        var errorMessage  = document.getElementById(cfg.errorMessageId);
        var warnBanner    = document.getElementById(cfg.warnBannerId);
        var warnMessage   = document.getElementById(cfg.warnMessageId);
        var resetBtn      = document.getElementById(cfg.resetBtnId);
        var pageContainer = document.getElementById(cfg.pageContainerId);

        if (!dropZone) return {};

        dropZone.addEventListener("dragover", function (e) { e.preventDefault(); dropZone.classList.add("drag-over"); });
        dropZone.addEventListener("dragleave", function () { dropZone.classList.remove("drag-over"); });
        dropZone.addEventListener("drop", function (e) {
            e.preventDefault(); dropZone.classList.remove("drag-over");
            var f = e.dataTransfer.files && e.dataTransfer.files[0];
            if (f) setFile(f);
        });
        fileInput.addEventListener("change", function () {
            var f = fileInput.files && fileInput.files[0];
            if (f) setFile(f);
        });
        clearFileBtn.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); clearFile(); });

        submitBtn.addEventListener("click", function () {
            if (!selectedFile) { showError("Please select a file first."); return; }
            hideBanners();
            setLoading(true);
            var formData = new FormData();
            formData.append("file", selectedFile);
            fetch(cfg.extractUrl, { method: "POST", body: formData })
                .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, status: r.status, result: d }; }); })
                .then(function (obj) {
                    if (!obj.ok || !obj.result.success) { showError(obj.result.detail || obj.result.message || "Server error"); return; }
                    cfg.onResults(obj.result);
                    pageContainer.classList.add("has-results");
                    submitBtn.style.display = "none";
                    resetBtn.style.display  = "flex";
                })
                .catch(function (err) { showError("Could not reach the service."); })
                .finally(function () { setLoading(false); });
        });

        resetBtn.addEventListener("click", function () {
            clearFile(); hideBanners(); cfg.onReset();
            pageContainer.classList.remove("has-results");
            submitBtn.style.display = ""; resetBtn.style.display = "none";
        });

        function setFile(file) {
            hideBanners();
            var lower = file.name.toLowerCase();
            var allowed = cfg.pdfOnly
                ? lower.endsWith(".pdf")
                : (lower.endsWith(".txt") || lower.endsWith(".pdf") || lower.endsWith(".md"));
            if (!allowed) {
                showError(cfg.pdfOnly ? "Only PDF files are supported." : "Only .txt, .pdf, and .md files are supported.");
                return;
            }
            if (file.size > maxUploadMb * 1024 * 1024) { showError("File too large. Max " + maxUploadMb + " MB."); return; }
            selectedFile = file;
            fileName.textContent = file.name;
            filePill.classList.add("visible");
            submitBtn.disabled = false;
        }

        function clearFile() {
            selectedFile = null; fileInput.value = "";
            fileName.textContent = ""; filePill.classList.remove("visible");
            submitBtn.disabled = true; hideBanners();
        }

        function setLoading(on) {
            submitBtn.disabled = on || !selectedFile;
            submitSpinner.classList.toggle("visible", on);
            submitLabel.textContent = on ? "Processing..." : cfg.submitLabel;
        }

        function showError(msg) { errorMessage.textContent = msg; errorBanner.classList.add("visible"); }
        function showWarn(msg) {
            warnMessage.textContent = warnMessage.textContent ? warnMessage.textContent + " " + msg : msg;
            warnBanner.classList.add("visible");
        }
        function hideBanners() {
            errorBanner.classList.remove("visible"); warnBanner.classList.remove("visible");
            warnMessage.textContent = ""; errorMessage.textContent = "";
        }

        return { showWarn: showWarn };
    }

    // =========================================================================
    // BATCH EXTRACTION WIDGET
    // =========================================================================
    (function () {
        var dropZone        = document.getElementById("dropZoneBatch");
        var fileInput       = document.getElementById("fileInputBatch");
        var fileListEl      = document.getElementById("batchFileList");
        var submitBtn       = document.getElementById("submitBtnBatch");
        var resetBtn        = document.getElementById("resetBtnBatch");
        var spinner         = document.getElementById("submitSpinnerBatch");
        var submitLabel     = document.getElementById("submitLabelBatch");
        var errorBanner     = document.getElementById("errorBannerBatch");
        var errorMsg        = document.getElementById("errorMessageBatch");
        var progressBar     = document.getElementById("batchProgressBar");
        var progressFill    = document.getElementById("batchProgressFill");
        var progressText    = document.getElementById("batchProgressText");
        var resultsPanel    = document.getElementById("batchResultsPanel");
        var pageContainer   = document.getElementById("pageContainerBatch");
        var ocrTableSection = document.getElementById("ocrDownloadSection");
        var ocrTableBody    = document.getElementById("ocrTableBody");

        if (!dropZone) return;

        var queue     = [];
        var idCounter = 0;
        var exportBtn = null;
        var resultsByFilename = {};

        // Track which filenames already have a row in the OCR table,
        // so a subsequent LLM error doesn't add a duplicate error row.
        var ocrRowAddedFor = {};

        // ---- drag & drop ----
        dropZone.addEventListener("dragover", function (e) { e.preventDefault(); dropZone.classList.add("drag-over"); });
        dropZone.addEventListener("dragleave", function () { dropZone.classList.remove("drag-over"); });
        dropZone.addEventListener("drop", function (e) {
            e.preventDefault(); dropZone.classList.remove("drag-over");
            addFiles(e.dataTransfer.files);
        });
        fileInput.addEventListener("change", function () { addFiles(fileInput.files); fileInput.value = ""; });

        function addFiles(fileList) {
            var added = 0;
            for (var i = 0; i < fileList.length; i++) {
                var f = fileList[i];
                if (!f.name.toLowerCase().endsWith(".pdf")) continue;
                if (f.size > maxUploadMb * 1024 * 1024) continue;
                var dup = queue.some(function (q) { return q.file.name === f.name && q.file.size === f.size; });
                if (dup) continue;
                var id   = ++idCounter;
                var pill = makePill(f.name, id);
                queue.push({ file: f, id: id, pillEl: pill.el, statusEl: pill.statusEl, card: null });
                fileListEl.appendChild(pill.el);
                added++;
            }
            if (added > 0) {
                fileListEl.style.display = "flex";
                submitBtn.disabled = false;
                hideError();
            }
        }

        function makePill(name, id) {
            var el = document.createElement("div");
            el.className = "batch-file-pill";
            el.dataset.id = id;

            var nameSpan = document.createElement("span");
            nameSpan.className = "pill-name";
            nameSpan.textContent = name;

            var statusSpan = document.createElement("span");
            statusSpan.className = "pill-status pill-status--queued";
            statusSpan.textContent = "Queued";

            var removeBtn = document.createElement("button");
            removeBtn.className = "pill-remove";
            removeBtn.title = "Remove";
            removeBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
            removeBtn.addEventListener("click", function () {
                queue = queue.filter(function (q) { return q.id !== id; });
                el.remove();
                if (queue.length === 0) { fileListEl.style.display = "none"; submitBtn.disabled = true; }
            });

            el.appendChild(nameSpan);
            el.appendChild(statusSpan);
            el.appendChild(removeBtn);
            return { el: el, statusEl: statusSpan };
        }

        function findQueueItem(filename) {
            return queue.find(function (q) { return q.file.name === filename; }) || null;
        }

        // ---- submit ----
        submitBtn.addEventListener("click", function () {
            if (queue.length === 0) return;

            submitBtn.style.display = "none";
            resetBtn.style.display  = "none";
            spinner.classList.add("visible");
            submitLabel.textContent = "Processing\u2026";
            progressBar.style.display = "block";
            hideError();
            pageContainer.classList.add("has-results");
            resultsByFilename = {};
            ocrRowAddedFor = {};

            fileListEl.querySelectorAll(".pill-remove").forEach(function (b) { b.disabled = true; });

            var formData = new FormData();
            queue.forEach(function (item) {
                formData.append("files", item.file);
            });

            updateProgress(0, queue.length);

            fetch("/extract/batch-pipeline", { method: "POST", body: formData })
                .then(function (response) {
                    if (!response.ok) {
                        return response.json().then(function (d) {
                            throw new Error(d.detail || "Server error");
                        });
                    }
                    return readStream(response);
                })
                .catch(function (err) {
                    showError("Batch failed: " + err.message);
                    restoreUi();
                });
        });

        function readStream(response) {
            var reader = response.body.getReader();
            var decoder = new TextDecoder();
            var buffer = "";

            function pump() {
                return reader.read().then(function (chunk) {
                    if (chunk.done) {
                        restoreUi();
                        return;
                    }
                    buffer += decoder.decode(chunk.value, { stream: true });
                    var lines = buffer.split("\n");
                    buffer = lines.pop();
                    lines.forEach(function (line) {
                        line = line.trim();
                        if (!line) return;
                        try {
                            handleEvent(JSON.parse(line));
                        } catch (e) {
                            console.warn("Failed to parse stream line:", line);
                        }
                    });
                    return pump();
                });
            }
            return pump();
        }

        function handleEvent(event) {
            if (event.stage === "batch_done") {
                updateProgress(event.total, event.total);
                renderExportBtn();
                return;
            }

            var item = findQueueItem(event.filename);

            if (event.stage === "ocr_done") {
                if (item) setPillStatus(item.statusEl, "ocr-done", "Extracting\u2026");

                // Mark that this file already has an OCR row so we don't add
                // a second one if the LLM later fails.
                ocrRowAddedFor[event.filename] = true;
                addOcrTableRow(event.filename, event.txt_filename, null);

                var card = makeResultCard(event.filename);
                if (item) item.card = card;
                resultsPanel.appendChild(card.el);
                setTimeout(function () { card.el.scrollIntoView({ behavior: "smooth", block: "nearest" }); }, 50);

            } else if (event.stage === "llm_done") {
                llmDoneCount++;
                updateProgress(llmDoneCount, event.total);
                var result = event.result;
                resultsByFilename[event.filename] = result;

                if (item) {
                    setPillStatus(item.statusEl, "done", "Done");
                    if (item.card) fillResultCard(item.card, result);
                }
                renderExportBtn();

            } else if (event.stage === "error") {
                llmDoneCount++
                updateProgress(llmDoneCount, event.total);
                var errMsg = event.error || "Unknown error";

                if (item) {
                    setPillStatus(item.statusEl, "error", "Error");

                    // Only add an OCR table row if OCR itself failed (no row yet).
                    // If OCR succeeded but LLM failed, the OCR row is already there —
                    // just update its status badge to reflect the LLM error inline
                    // on the result card, not on the OCR table.
                    if (!ocrRowAddedFor[event.filename]) {
                        ocrRowAddedFor[event.filename] = true;
                        addOcrTableRow(event.filename, null, errMsg);
                    }

                    if (!item.card) {
                        var errCard = makeResultCard(event.filename);
                        item.card = errCard;
                        resultsPanel.appendChild(errCard.el);
                    }
                    if (item.card) fillResultCardError(item.card, errMsg);
                }
                renderExportBtn();
            }
        }

        function restoreUi() {
            spinner.classList.remove("visible");
            submitLabel.textContent = "Start Batch Extraction";
            submitBtn.style.display = "";
            submitBtn.disabled = true;
            resetBtn.style.display = "flex";
        }

        // ---- OCR download table ----
        function addOcrTableRow(originalName, txtFilename, errorMsg) {
            if (!ocrTableSection || !ocrTableBody) return;
            ocrTableSection.style.display = "flex";

            var tr = document.createElement("tr");

            if (txtFilename) {
                tr.innerHTML =
                    '<td class="col-name">' + escHtml(originalName) + '</td>' +
                    '<td class="col-status"><span class="badge badge-done">Done</span></td>' +
                    '<td class="col-action"><a class="dl-link" href="/extract/ocr-download/' + encodeURIComponent(txtFilename) + '" download="' + escHtml(txtFilename) + '">Download</a></td>';
            } else {
                tr.innerHTML =
                    '<td class="col-name">' + escHtml(originalName) +
                    (errorMsg ? '<div style="font-size:11px;color:var(--pb-error);">' + escHtml(errorMsg) + '</div>' : '') +
                    '</td>' +
                    '<td class="col-status"><span class="badge badge-error">Error</span></td>' +
                    '<td class="col-action"><span style="font-size:11px;color:#999">\u2014</span></td>';
            }
            ocrTableBody.appendChild(tr);
        }

        // ---- result cards ----
        function makeResultCard(filename) {
            var el = document.createElement("div");
            el.className = "batch-result-card";

            var header = document.createElement("div");
            header.className = "batch-result-header";

            var nameEl = document.createElement("span");
            nameEl.className = "batch-result-filename";
            nameEl.textContent = filename;

            var badge = document.createElement("span");
            badge.className = "batch-result-badge batch-result-badge--processing";
            badge.textContent = "Extracting\u2026";

            var chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
            chevron.setAttribute("viewBox", "0 0 24 24");
            chevron.setAttribute("fill", "none");
            chevron.setAttribute("stroke-width", "2");
            chevron.setAttribute("stroke-linecap", "round");
            chevron.classList.add("batch-result-chevron");
            var path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
            path.setAttribute("points", "6 9 12 15 18 9");
            path.setAttribute("stroke", "currentColor");
            chevron.appendChild(path);

            header.appendChild(nameEl);
            header.appendChild(badge);
            header.appendChild(chevron);

            var body = document.createElement("div");
            body.className = "batch-result-body open";
            var bodyInner = document.createElement("div");
            bodyInner.className = "batch-result-body-inner";
            var bodyContent = document.createElement("div");
            bodyContent.className = "batch-result-body-content";
            bodyContent.innerHTML = '<span style="font-size:13px;color:var(--pb-muted);font-style:italic;">Waiting for LLM response\u2026</span>';

            bodyInner.appendChild(bodyContent);
            body.appendChild(bodyInner);
            el.appendChild(header);
            el.appendChild(body);

            header.addEventListener("click", function () {
                var isOpen = body.classList.contains("open");
                body.classList.toggle("open", !isOpen);
                chevron.style.transform = isOpen ? "" : "rotate(180deg)";
            });

            return { el: el, badge: badge, bodyContent: bodyContent };
        }

        function fillResultCardError(card, msg) {
            card.badge.className = "batch-result-badge batch-result-badge--fail";
            card.badge.textContent = "Error";
            card.bodyContent.innerHTML = '<span style="font-size:13px;color:var(--pb-error);">&#10007; ' + escHtml(msg) + '</span>';
        }

        function fillResultCard(card, result) {
            card.badge.className = "batch-result-badge batch-result-badge--pass";
            card.badge.textContent = "Done";

            var d = result.data || {};

            function fieldVal(key) {
                return (d[key] != null && d[key] !== "") ? d[key] : null;
            }

            function fieldHtml(label, value) {
                var cls  = value ? "field-value found" : "field-value not-found";
                var text = value ? escHtml(value) : "Not found";
                return '<div class="field-row"><div class="field-label">' + label + '</div><div class="' + cls + '">' + text + '</div></div>';
            }

            card.bodyContent.innerHTML =
                '<div class="results-card" style="flex:1;border:none;padding:0;">' +
                '<span class="section-label" style="display:block;margin-bottom:12px;">Extracted Fields</span>' +
                '<div style="display:grid;grid-template-columns:1fr 1fr;gap:0;">' +
                fieldHtml("Bank Name",          fieldVal("bank_name")) +
                fieldHtml("Customer Name",      fieldVal("name")) +
                fieldHtml("Master Account No.", fieldVal("master_account_number")) +
                fieldHtml("Sub Account No.",    fieldVal("sub_account_number")) +
                fieldHtml("FI Number",          fieldVal("fi_num")) +
                '</div></div>';
        }

        // ---- progress ----
        function updateProgress(done, total) {
            var pct = total === 0 ? 0 : Math.round((done / total) * 100);
            progressFill.style.width = pct + "%";
            progressText.textContent = done + " / " + total + " files";
        }

        function setPillStatus(statusEl, type, text) {
            statusEl.className = "pill-status pill-status--" + type;
            statusEl.textContent = text;
        }

        // ---- reset ----
        resetBtn.addEventListener("click", function () {
            queue            = [];
            idCounter        = 0;
            resultsByFilename = {};
            ocrRowAddedFor   = {};
            if (exportBtn) { exportBtn.remove(); exportBtn = null; }
            fileListEl.innerHTML = "";
            fileListEl.style.display = "none";
            resultsPanel.innerHTML = "";
            if (ocrTableSection) ocrTableSection.style.display = "none";
            if (ocrTableBody)    ocrTableBody.innerHTML = "";
            progressBar.style.display = "none";
            progressFill.style.width = "0%";
            submitBtn.disabled = true;
            submitBtn.style.display = "";
            resetBtn.style.display = "none";
            pageContainer.classList.remove("has-results");
            hideError();
        });

        function hideError() {
            errorBanner.classList.remove("visible");
            errorMsg.textContent = "";
        }

        function showError(msg) {
            errorMsg.textContent = msg;
            errorBanner.classList.add("visible");
        }

        // ---- export to CSV (client-side, from stream data) ----
        function renderExportBtn() {
            var anyDone = Object.keys(resultsByFilename).length > 0 ||
                queue.some(function (q) { return q.card && q.card.badge.textContent === "Error"; });
            if (!anyDone || exportBtn) return;
            exportBtn = document.createElement("button");
            exportBtn.className = "submit-btn";
            exportBtn.style.cssText = "margin-top:8px;background:var(--pb-success);font-size:13px;height:42px;";
            exportBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:15px;height:15px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> Export to CSV';
            exportBtn.addEventListener("click", exportToCsv);
            var parent = resetBtn.parentNode;
            var next   = resetBtn.nextSibling;
            if (next) parent.insertBefore(exportBtn, next);
            else parent.appendChild(exportBtn);
        }

        function exportToCsv() {
            var rows = [csvRow(["File Name", "Bank Name", "FI Code", "Master Account No.", "Sub Account No."])];

            queue.forEach(function (item) {
                var filename = item.file.name;
                var result   = resultsByFilename[filename];

                if (!result) {
                    rows.push(csvRow([filename, "ERROR", "ERROR", "ERROR", "ERROR"]));
                    return;
                }

                var d = result.data || {};
                rows.push(csvRow([
                    filename,
                    d.bank_name || "",
                    d.fi_num || "",
                    d.master_account_number || "",
                    d.sub_account_number || "",
                ]));
            });

            var blob = new Blob([rows.join("\r\n")], { type: "text/csv;charset=utf-8;" });
            var url  = URL.createObjectURL(blob);
            var a    = document.createElement("a");
            a.href   = url;
            var now  = new Date();
            a.download = "extraction_" +
                now.getFullYear() +
                String(now.getMonth() + 1).padStart(2, "0") +
                String(now.getDate()).padStart(2, "0") + "_" +
                String(now.getHours()).padStart(2, "0") +
                String(now.getMinutes()).padStart(2, "0") +
                String(now.getSeconds()).padStart(2, "0") + ".csv";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }

        function csvRow(fields) {
            return fields.map(function (v) {
                var s = String(v == null ? "" : v);
                if (s.indexOf(",") !== -1 || s.indexOf('"') !== -1 || s.indexOf("\n") !== -1)
                    s = '"' + s.replace(/"/g, '""') + '"';
                return s;
            }).join(",");
        }

    })();

});