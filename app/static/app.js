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
                // Show: use flex so height chain works correctly for all panels
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
    // GENERIC UPLOAD WIDGET FACTORY
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

        dropZone.addEventListener("dragover", function (e) {
            e.preventDefault();
            dropZone.classList.add("drag-over");
        });
        dropZone.addEventListener("dragleave", function () {
            dropZone.classList.remove("drag-over");
        });
        dropZone.addEventListener("drop", function (e) {
            e.preventDefault();
            dropZone.classList.remove("drag-over");
            var f = e.dataTransfer.files && e.dataTransfer.files[0];
            if (f) setFile(f);
        });
        fileInput.addEventListener("change", function () {
            var f = fileInput.files && fileInput.files[0];
            if (f) setFile(f);
        });
        clearFileBtn.addEventListener("click", function (e) {
            e.preventDefault();
            e.stopPropagation();
            clearFile();
        });

        submitBtn.addEventListener("click", function () {
            if (!selectedFile) {
                showError("Please select a file first.");
                return;
            }
            hideBanners();
            setLoading(true);

            var formData = new FormData();
            formData.append("file", selectedFile);

            fetch(cfg.extractUrl, { method: "POST", body: formData })
                .then(function (response) {
                    return response.json().then(function (result) {
                        return { ok: response.ok, status: response.status, result: result };
                    });
                })
                .then(function (obj) {
                    if (!obj.ok || !obj.result.success) {
                        showError(obj.result.detail || obj.result.message || ("Server error (HTTP " + obj.status + ")"));
                        return;
                    }
                    cfg.onResults(obj.result);
                    pageContainer.classList.add("has-results");
                    submitBtn.style.display = "none";
                    resetBtn.style.display  = "flex";
                })
                .catch(function (err) {
                    console.error(err);
                    showError("Could not reach the service. Please check the server is running.");
                })
                .finally(function () {
                    setLoading(false);
                });
        });

        resetBtn.addEventListener("click", function () {
            clearFile();
            hideBanners();
            cfg.onReset();
            pageContainer.classList.remove("has-results");
            submitBtn.style.display = "";
            resetBtn.style.display  = "none";
        });

        function setFile(file) {
            hideBanners();
            var lower = file.name.toLowerCase();
            if (!lower.endsWith(".txt") && !lower.endsWith(".pdf") && !lower.endsWith(".md")) {
                showError("Only .txt, .pdf, and .md files are supported.");
                return;
            }
            if (file.size > maxUploadMb * 1024 * 1024) {
                showError("File too large. Maximum allowed size is " + maxUploadMb + " MB.");
                return;
            }
            selectedFile         = file;
            fileName.textContent = file.name;
            filePill.classList.add("visible");
            submitBtn.disabled   = false;
        }

        function clearFile() {
            selectedFile         = null;
            fileInput.value      = "";
            fileName.textContent = "";
            filePill.classList.remove("visible");
            submitBtn.disabled   = true;
            hideBanners();
        }

        function setLoading(isLoading) {
            submitBtn.disabled      = isLoading || !selectedFile;
            submitSpinner.classList.toggle("visible", isLoading);
            submitLabel.textContent = isLoading ? "Processing..." : cfg.submitLabel;
        }

        function showError(msg) {
            errorMessage.textContent = msg;
            errorBanner.classList.add("visible");
        }

        function showWarn(msg) {
            var existing = warnMessage.textContent;
            warnMessage.textContent = existing ? existing + " " + msg : msg;
            warnBanner.classList.add("visible");
        }

        function hideBanners() {
            errorBanner.classList.remove("visible");
            warnBanner.classList.remove("visible");
            warnMessage.textContent = "";
            errorMessage.textContent = "";
        }

        return { showWarn: showWarn };
    }

    // -------------------------------------------------------------------------
    // SHARED FIELD HELPER
    // -------------------------------------------------------------------------
    function getExtractedValue(result, key) {
        if (result && result.data && result.data[key] != null && result.data[key] !== "") {
            return result.data[key];
        }
        if (result && result.comparison && result.comparison[key] &&
            result.comparison[key].extracted != null && result.comparison[key].extracted !== "") {
            return result.comparison[key].extracted;
        }
        return null;
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
    // COMPARISON PANEL RENDERER
    // -------------------------------------------------------------------------
    function renderComparison(comparison) {
        var panel = document.getElementById("comparisonPanel");
        if (!panel) return;

        if (!comparison) {
            panel.innerHTML = "";
            return;
        }

        var FIELD_LABELS = {
            bank_name:             "Bank Name",
            fi_num:                "FI Number",
            master_account_number: "Master Account No.",
            sub_account_number:    "Sub Account No.",
        };

        var html = '<span class="section-label">Verification</span>';

        if (!comparison.csv_row_found) {
            html += '<div class="cmp-badge cmp-badge--warn">&#9888; File not found in reference data (key: ' +
                escHtml(comparison.filename_key) + ')</div>';
        } else if (comparison.all_match) {
            html += '<div class="cmp-badge cmp-badge--pass">&#10003; All fields match</div>';
        } else {
            html += '<div class="cmp-badge cmp-badge--fail">&#10007; Mismatch detected</div>';
        }

        var fields = ["bank_name", "fi_num", "master_account_number", "sub_account_number"];
        html += '<div class="cmp-table-wrap"><table class="cmp-table"><thead><tr>' +
            '<th>Field</th><th>Extracted</th><th>Expected</th><th>Status</th>' +
            '</tr></thead><tbody>';

        fields.forEach(function (key) {
            var detail = comparison[key];
            if (!detail) return;

            var statusIcon, statusClass;
            if (!comparison.csv_row_found) {
                statusIcon  = "&#9888;";
                statusClass = "cmp-warn";
            } else {
                statusIcon  = detail.match ? "&#10003;" : "&#10007;";
                statusClass = detail.match ? "cmp-pass" : "cmp-fail";
            }

            html += '<tr class="' + statusClass + '">' +
                '<td class="cmp-field-name">' + FIELD_LABELS[key] + '</td>' +
                '<td>' + escHtml(detail.extracted || "\u2014") + '</td>' +
                '<td>' + escHtml(detail.expected  || "\u2014") + '</td>' +
                '<td class="cmp-status">' + statusIcon + '</td>' +
                '</tr>';
        });

        html += '</tbody></table></div>';
        panel.innerHTML = html;
    }

    function escHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
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
        var summariseWidget = createUploadWidget({
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

            onResults: function (result) {
                var data = result.data || {};

                setField(resOverallMin,     data.overall_min_balance);
                setField(resOverallMax,     data.overall_max_balance);
                setField(resOverallClosing, data.overall_closing_balance);

                var monthly = data.monthly_summaries || [];
                if (monthly.length) {
                    var mHtml = "<table class='summary-table'><thead><tr>" +
                        "<th>Month</th><th>Min Balance</th><th>Max Balance</th><th>Closing Balance</th>" +
                        "</tr></thead><tbody>";
                    for (var i = 0; i < monthly.length; i++) {
                        var m = monthly[i];
                        mHtml += "<tr>" +
                            "<td>" + (m.month           || "\u2014") + "</td>" +
                            "<td>" + (m.min_balance      || "\u2014") + "</td>" +
                            "<td>" + (m.max_balance      || "\u2014") + "</td>" +
                            "<td>" + (m.closing_balance  || "\u2014") + "</td>" +
                            "</tr>";
                    }
                    mHtml += "</tbody></table>";
                    resMonthlyTable.innerHTML = mHtml;
                } else {
                    resMonthlyTable.textContent = "No monthly data found.";
                }

                var daily = data.daily_summaries || [];
                if (daily.length) {
                    var dHtml = "<table class='summary-table'><thead><tr>" +
                        "<th>Date</th><th>Min Balance</th><th>Max Balance</th><th>Closing Balance</th>" +
                        "</tr></thead><tbody>";
                    for (var j = 0; j < daily.length; j++) {
                        var d = daily[j];
                        dHtml += "<tr>" +
                            "<td>" + (d.date            || "\u2014") + "</td>" +
                            "<td>" + (d.min_balance      || "\u2014") + "</td>" +
                            "<td>" + (d.max_balance      || "\u2014") + "</td>" +
                            "<td>" + (d.closing_balance  || "\u2014") + "</td>" +
                            "</tr>";
                    }
                    dHtml += "</tbody></table>";
                    resDailyTable.innerHTML = dHtml;
                } else {
                    resDailyTable.textContent = "No daily data found.";
                }
            },

            onReset: function () {
                setField(resOverallMin,     null);
                setField(resOverallMax,     null);
                setField(resOverallClosing, null);
                resMonthlyTable.innerHTML = "\u2014";
                resDailyTable.innerHTML   = "\u2014";
            }
        });
    }

    // -------------------------------------------------------------------------
    // BATCH EXTRACTION WIDGET (V2)
    // -------------------------------------------------------------------------
    (function () {
        var maxMb         = maxUploadMb;
        var dropZone      = document.getElementById("dropZoneBatch");
        var fileInput     = document.getElementById("fileInputBatch");
        var fileListEl    = document.getElementById("batchFileList");
        var submitBtn     = document.getElementById("submitBtnBatch");
        var resetBtn      = document.getElementById("resetBtnBatch");
        var spinner       = document.getElementById("submitSpinnerBatch");
        var submitLabel   = document.getElementById("submitLabelBatch");
        var errorBanner   = document.getElementById("errorBannerBatch");
        var errorMsg      = document.getElementById("errorMessageBatch");
        var progressBar   = document.getElementById("batchProgressBar");
        var progressFill  = document.getElementById("batchProgressFill");
        var progressText  = document.getElementById("batchProgressText");
        var resultsPanel  = document.getElementById("batchResultsPanel");
        var pageContainer = document.getElementById("pageContainerBatch");

        if (!dropZone) return;

        var queue = [];
        var isRunning = false;
        var idCounter = 0;

        // ---- drag & drop / file input ----
        dropZone.addEventListener("dragover", function (e) {
            e.preventDefault(); dropZone.classList.add("drag-over");
        });
        dropZone.addEventListener("dragleave", function () {
            dropZone.classList.remove("drag-over");
        });
        dropZone.addEventListener("drop", function (e) {
            e.preventDefault(); dropZone.classList.remove("drag-over");
            addFiles(e.dataTransfer.files);
        });
        fileInput.addEventListener("change", function () {
            addFiles(fileInput.files);
            fileInput.value = "";
        });

        function addFiles(fileList) {
            var added = 0;
            for (var i = 0; i < fileList.length; i++) {
                var f = fileList[i];
                var lower = f.name.toLowerCase();
                if (!lower.endsWith(".txt") && !lower.endsWith(".pdf") && !lower.endsWith(".md")) continue;
                if (f.size > maxMb * 1024 * 1024) continue;
                // Deduplicate by name+size
                var dup = false;
                for (var j = 0; j < queue.length; j++) {
                    if (queue[j].file.name === f.name && queue[j].file.size === f.size) { dup = true; break; }
                }
                if (dup) continue;

                var id = ++idCounter;
                var pill = makePill(f.name, id);
                queue.push({ file: f, id: id, pillEl: pill.el, statusEl: pill.statusEl, resultCardEl: null });
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
                if (isRunning) return;
                queue = queue.filter(function (q) { return q.id !== id; });
                el.remove();
                if (queue.length === 0) {
                    fileListEl.style.display = "none";
                    submitBtn.disabled = true;
                }
            });

            el.appendChild(nameSpan);
            el.appendChild(statusSpan);
            el.appendChild(removeBtn);
            return { el: el, statusEl: statusSpan };
        }

        // ---- submit ----
        submitBtn.addEventListener("click", function () {
            if (queue.length === 0 || isRunning) return;
            isRunning = true;
            submitBtn.style.display = "none";
            resetBtn.style.display = "none";
            spinner.classList.add("visible");
            submitLabel.textContent = "Processing\u2026";
            progressBar.style.display = "block";
            hideError();

            pageContainer.classList.add("has-results");

            var removes = fileListEl.querySelectorAll(".pill-remove");
            for (var i = 0; i < removes.length; i++) removes[i].disabled = true;

            processQueue(0);
        });

        function processQueue(index) {
            var total = queue.length;
            updateProgress(index, total);

            if (index >= total) {
                isRunning = false;
                spinner.classList.remove("visible");
                submitLabel.textContent = "Start Batch Extraction";
                submitBtn.style.display = "";
                submitBtn.disabled = true;
                resetBtn.style.display = "flex";
                updateProgress(total, total);
                return;
            }

            var item = queue[index];
            setPillStatus(item.statusEl, "running", "Processing\u2026");

            var card = makeResultCard(item.file.name);
            item.resultCardEl = card.el;
            resultsPanel.appendChild(card.el);

            // Scroll the results panel to show the new card
            // Use a tiny delay so the DOM has painted
            setTimeout(function () {
                card.el.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }, 50);

            var formData = new FormData();
            formData.append("file", item.file);

            fetch("/extract/from-file", { method: "POST", body: formData })
                .then(function (response) {
                    return response.json().then(function (result) {
                        return { ok: response.ok, status: response.status, result: result };
                    });
                })
                .then(function (obj) {
                    if (!obj.ok || !obj.result.success) {
                        var msg = obj.result.detail || obj.result.message || ("HTTP " + obj.status);
                        setPillStatus(item.statusEl, "error", "Error");
                        item.extractError = msg;
                        fillResultCard(card, null, null, msg);
                        renderExportBtn();
                    } else {
                        var allMatch = obj.result.comparison && obj.result.comparison.all_match;
                        var csvFound = obj.result.comparison && obj.result.comparison.csv_row_found;
                        var statusStr = !csvFound ? "warn" : (allMatch ? "pass" : "fail");
                        setPillStatus(item.statusEl, "done", "Done");
                        item.extractResult = obj.result;
                        item.extractStatus = statusStr;
                        fillResultCard(card, obj.result, statusStr, null);
                        renderExportBtn();
                    }
                })
                .catch(function (err) {
                    setPillStatus(item.statusEl, "error", "Error");
                    fillResultCard(card, null, null, "Network error: " + err.message);
                })
                .finally(function () {
                    processQueue(index + 1);
                });
        }

        function updateProgress(done, total) {
            var pct = total === 0 ? 0 : Math.round((done / total) * 100);
            progressFill.style.width = pct + "%";
            progressText.textContent = done + " / " + total + " files";
        }

        function setPillStatus(statusEl, type, text) {
            statusEl.className = "pill-status pill-status--" + type;
            statusEl.textContent = text;
        }

        // ---- result card builders ----
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
            badge.textContent = "Processing\u2026";

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

            // Toggle collapse on header click
            header.addEventListener("click", function () {
                var isOpen = body.classList.contains("open");
                body.classList.toggle("open", !isOpen);
                chevron.style.transform = isOpen ? "" : "rotate(180deg)";
            });

            return { el: el, badge: badge, bodyContent: bodyContent, chevron: chevron };
        }

        function fillResultCard(card, result, statusStr, errorText) {
            if (errorText) {
                card.badge.className = "batch-result-badge batch-result-badge--fail";
                card.badge.textContent = "Error";
                card.bodyContent.innerHTML = '<span style="font-size:13px;color:var(--pb-error);">&#10007; ' + escHtml(errorText) + '</span>';
                return;
            }

            // Badge labels hidden - verification removed
            // var badgeLabels = { pass: "\u2713 All Match", fail: "\u2717 Mismatch", warn: "\u26A0 No Reference" };
            // var badgeClass  = { pass: "pass", fail: "fail", warn: "warn" };
            // card.badge.className = "batch-result-badge batch-result-badge--" + (badgeClass[statusStr] || "warn");
            // card.badge.textContent = badgeLabels[statusStr] || "Done";
            card.badge.className = "batch-result-badge batch-result-badge--done";
            card.badge.textContent = "Done";

            var d = result.data || {};

            function fieldVal(key) {
                if (d[key] != null && d[key] !== "") return d[key];
                if (result.comparison && result.comparison[key] && result.comparison[key].extracted != null)
                    return result.comparison[key].extracted;
                return null;
            }

            function fieldHtml(label, value) {
                var valClass = value ? "field-value found" : "field-value not-found";
                var valText  = value ? escHtml(value) : "Not found";
                return '<div class="field-row"><div class="field-label">' + label +
                    '</div><div class="' + valClass + '">' + valText + '</div></div>';
            }

            var fieldsHtml = '<div class="results-card" style="flex:1; border:none; padding:0;">' +
                '<span class="section-label" style="display:block;margin-bottom:12px;">Extracted Fields</span>' +
                fieldHtml("Bank Name",           fieldVal("bank_name")) +
                fieldHtml("Customer Name",       fieldVal("name")) +
                fieldHtml("Master Account No.",  fieldVal("master_account_number")) +
                fieldHtml("Sub Account No.",     fieldVal("sub_account_number")) +
                fieldHtml("FI Number",           fieldVal("fi_num")) +
                '</div>';

            // var cmpHtml = buildComparisonHtml(result.comparison); // verification hidden

            card.bodyContent.innerHTML =
                '<div class="batch-fields-row">' + fieldsHtml + '</div>';
        }
        
        // buildComparisonHtml hidden - verification section not shown
        // function buildComparisonHtml(comparison) {
        //     if (!comparison) return "";

        //     var LABELS = {
        //         bank_name: "Bank Name", fi_num: "FI Number",
        //         master_account_number: "Master Account No.", sub_account_number: "Sub Account No."
        //     };

        //     var html = '<div style="flex:1; border:none; padding:0;">' +
        //         '<span class="section-label" style="display:block;margin-bottom:12px;">Verification</span>';

        //     if (!comparison.csv_row_found) {
        //         html += '<div class="cmp-badge cmp-badge--warn">&#9888; Not in reference data</div>';
        //     } else if (comparison.all_match) {
        //         html += '<div class="cmp-badge cmp-badge--pass">&#10003; All fields match</div>';
        //     } else {
        //         html += '<div class="cmp-badge cmp-badge--fail">&#10007; Mismatch detected</div>';
        //     }

        //     html += '<div class="cmp-table-wrap"><table class="cmp-table"><thead><tr>' +
        //         '<th>Field</th><th>Extracted</th><th>Expected</th><th></th></tr></thead><tbody>';

        //     ["bank_name","fi_num","master_account_number","sub_account_number"].forEach(function (key) {
        //         var detail = comparison[key];
        //         if (!detail) return;
        //         var statusIcon, rowClass;
        //         if (!comparison.csv_row_found) {
        //             statusIcon = "&#9888;"; rowClass = "cmp-warn";
        //         } else {
        //             statusIcon = detail.match ? "&#10003;" : "&#10007;";
        //             rowClass   = detail.match ? "cmp-pass" : "cmp-fail";
        //         }
        //         html += '<tr class="' + rowClass + '">' +
        //             '<td class="cmp-field-name">' + LABELS[key] + '</td>' +
        //             '<td>' + escHtml(detail.extracted || "\u2014") + '</td>' +
        //             '<td>' + escHtml(detail.expected  || "\u2014") + '</td>' +
        //             '<td class="cmp-status">' + statusIcon + '</td></tr>';
        //     });

        //     html += '</tbody></table></div></div>';
        //     return html;
        // }
        // function buildComparisonHtml(comparison) { ... }

        // ---- reset ----
        resetBtn.addEventListener("click", function () {
            queue = [];
            idCounter = 0;
            if (exportBtn) { exportBtn.remove(); exportBtn = null; }
            isRunning = false;
            fileListEl.innerHTML = "";
            fileListEl.style.display = "none";
            resultsPanel.innerHTML = "";
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

        function escHtml(str) {
            return String(str || "")
                .replace(/&/g, "&amp;").replace(/</g, "&lt;")
                .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
        }

        // ---- export to CSV ----
        var exportBtn = null;

        function renderExportBtn() {
            var anyDone = queue.some(function (q) { return q.extractResult || q.extractError; });
            if (!anyDone) return;

            if (!exportBtn) {
                exportBtn = document.createElement("button");
                exportBtn.className = "submit-btn";
                exportBtn.style.cssText = "margin-top:8px; background:var(--pb-success); font-size:13px; height:42px;";
                exportBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:15px;height:15px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> Export Verification to CSV';
                exportBtn.addEventListener("click", exportToCsv);
                var parent = resetBtn.parentNode;
                var next = resetBtn.nextSibling;
                if (next) {
                    parent.insertBefore(exportBtn, next);
                } else {
                    parent.appendChild(exportBtn);
                }
            }
        }

        function exportToCsv() {
            var FIELDS = [
                { key: "bank_name",             label: "Bank Name" },
                { key: "fi_num",                label: "FI Code" },
                { key: "master_account_number", label: "Master Account No." },
                { key: "sub_account_number",    label: "Sub Account No." },
            ];

            var rows = [];
            rows.push(csvRow(["Filename", "Field", "Extracted"]));

            queue.forEach(function (item) {
                var filename = item.file.name;

                if (item.extractError || !item.extractResult) {
                    FIELDS.forEach(function (f) {
                        // rows.push(csvRow([filename, f.label, "ERROR", "", "FAIL"]));
                        rows.push(csvRow([filename, f.label, "ERROR", ""]));
                    });
                    return;
                }

                var cmp = item.extractResult.comparison || null;

                FIELDS.forEach(function (f) {
                    var extracted = "";
                    var expected  = "";
                    var match     = "";

                    if (cmp && cmp[f.key]) {
                        extracted = cmp[f.key].extracted != null ? cmp[f.key].extracted : "";
                        expected  = cmp[f.key].expected  != null ? cmp[f.key].expected  : "";
                        if (!cmp.csv_row_found) {
                            match = "NO REFERENCE";
                        } else {
                            match = cmp[f.key].match ? "PASS" : "FAIL";
                        }
                    } else {
                        var d = item.extractResult.data || {};
                        extracted = d[f.key] != null ? d[f.key] : "";
                        match = "NO REFERENCE";
                    }

                    rows.push(csvRow([filename, f.label, extracted]));
                });
            });

            var blob = new Blob([rows.join("\r\n")], { type: "text/csv;charset=utf-8;" });
            var url  = URL.createObjectURL(blob);
            var a    = document.createElement("a");
            a.href   = url;

            var now   = new Date();
            var stamp = now.getFullYear() +
                String(now.getMonth() + 1).padStart(2, "0") +
                String(now.getDate()).padStart(2, "0") + "_" +
                String(now.getHours()).padStart(2, "0") +
                String(now.getMinutes()).padStart(2, "0") +
                String(now.getSeconds()).padStart(2, "0");
            a.download = "verification_" + stamp + ".csv";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }

        function csvRow(fields) {
            return fields.map(function (v) {
                var s = String(v == null ? "" : v);
                if (s.indexOf(",") !== -1 || s.indexOf('"') !== -1 || s.indexOf("\n") !== -1) {
                    s = '"' + s.replace(/"/g, '""') + '"';
                }
                return s;
            }).join(",");
        }

    })();

});
