const $ = (id) => document.getElementById(id);
const uploadForm = $("uploadForm"), dataFile = $("dataFile"), uploadBox = $("uploadBox"), uploadBtn = $("uploadBtn"), uploadStatus = $("uploadStatus");
const selectedDataFile = $("selectedDataFile"), selectedFileMeta = $("selectedFileMeta"), fileSummary = $("fileSummary"), emailPreviewSection = $("emailPreviewSection"), emailPreview = $("emailPreview");
const subject = $("subject"), body = $("body"), attachment = $("attachment"), attachmentStatus = $("attachmentStatus"), attachmentList = $("attachmentList"), resetScheduleBtn = $("resetScheduleBtn"), resetFormBtn = $("resetFormBtn"), sendBtn = $("sendBtn"), cancelBtn = $("cancelBtn"), retryFailedBtn = $("retryFailedBtn");
const sendStatus = $("sendStatus"), sendInfoTitle = $("sendInfoTitle"), sendInfoText = $("sendInfoText"), resultsCard = $("resultsCard"), failedSection = $("failedSection"), failedList = $("failedList");
let extractedEmails = [], selectedAttachments = [], campaignId = null, pollTimer = null, campaignSending = false;

function showStatus(element, message, type) { element.textContent = message; element.className = `status ${type}`; }
function resetStatus(element) { element.textContent = ""; element.className = "status hidden"; }
function updateWorkflow(step) {
    document.querySelectorAll(".progress-step").forEach((item) => { const number = Number(item.dataset.step); item.classList.toggle("completed", number < step); item.classList.toggle("active", number === step); });
    document.querySelectorAll(".progress-line").forEach((item) => item.classList.toggle("completed", Number(item.dataset.line) < step));
    document.querySelectorAll(".workflow-card").forEach((item) => item.classList.remove("current-step"));
    const card = [null, $("contactsCard"), $("composeCard"), $("sendCard"), resultsCard][step]; if (card) card.classList.add("current-step");
    $("topbarStatusText").textContent = ["", "Import contacts", "Compose your email", "Campaign in progress", "Campaign results"][step];
}
function formatSize(size) { return size < 1024 * 1024 ? `${Math.max(1, size / 1024).toFixed(0)} KB` : `${(size / 1024 / 1024).toFixed(2)} MB`; }
function updateSelectedFile() { const file = dataFile.files[0]; selectedDataFile.textContent = file ? file.name : "No file selected"; selectedFileMeta.textContent = file ? `${formatSize(file.size)} • Ready for processing` : "Ready for processing"; }
function attachmentKey(file) { return `${file.name}:${file.size}:${file.lastModified}`; }
function syncAttachmentInput() { const transfer = new DataTransfer(); selectedAttachments.forEach((file) => transfer.items.add(file)); attachment.files = transfer.files; }
function removeAttachment(index) { selectedAttachments.splice(index, 1); syncAttachmentInput(); renderAttachments(); }
function renderAttachments() {
    attachmentList.replaceChildren();
    selectedAttachments.forEach((file, index) => {
        const item = document.createElement("div"); item.className = "attachment-item";
        const details = document.createElement("div"); details.className = "attachment-details";
        const name = document.createElement("strong"); name.textContent = file.name;
        const size = document.createElement("span"); size.textContent = formatSize(file.size);
        details.append(name, size);
        const remove = document.createElement("button"); remove.type = "button"; remove.className = "attachment-remove"; remove.textContent = "Remove"; remove.setAttribute("aria-label", `Remove ${file.name}`); remove.addEventListener("click", () => removeAttachment(index));
        item.append(details, remove); attachmentList.appendChild(item);
    });
    attachmentStatus.textContent = selectedAttachments.length ? `${selectedAttachments.length} attachment${selectedAttachments.length === 1 ? "" : "s"} selected` : "No attachments selected";
    attachmentStatus.style.color = selectedAttachments.length ? "#067647" : "";
}
function clearAttachments() { selectedAttachments = []; attachment.value = ""; syncAttachmentInput(); renderAttachments(); }
function clearSchedule() { $("scheduleDate").value = ""; $("scheduleTime").value = ""; resetStatus(sendStatus); }
function resetComposeForm() {
    subject.value = ""; body.value = ""; clearAttachments(); clearSchedule();
    dataFile.value = ""; updateSelectedFile(); resetCampaign();
}
function updateSendState() { if (campaignSending) return; const valid = extractedEmails.length && subject.value.trim() && body.value.trim(); sendBtn.disabled = !valid; sendInfoTitle.textContent = valid ? "Your campaign is ready" : extractedEmails.length ? "Complete your email first" : "Upload your contacts first"; sendInfoText.textContent = valid ? `${extractedEmails.length} valid recipient${extractedEmails.length === 1 ? "" : "s"} are ready to receive your email.` : "Import contacts and complete the message to enable sending."; }
function resetCampaign() { extractedEmails = []; campaignId = null; clearInterval(pollTimer); resultsCard.classList.add("hidden"); failedSection.classList.add("hidden"); emailPreviewSection.classList.add("hidden"); fileSummary.classList.add("hidden"); cancelBtn.classList.add("hidden"); resetStatus(uploadStatus); resetStatus(sendStatus); updateSendState(); updateWorkflow(1); }

dataFile.addEventListener("change", () => { updateSelectedFile(); resetCampaign(); if (dataFile.files.length) showStatus(uploadStatus, `Selected ${dataFile.files[0].name}. Click "Upload & Extract Emails" to continue.`, "info"); });
attachment.addEventListener("change", () => {
    const known = new Set(selectedAttachments.map(attachmentKey));
    [...attachment.files].forEach((file) => { if (!known.has(attachmentKey(file))) { selectedAttachments.push(file); known.add(attachmentKey(file)); } });
    syncAttachmentInput(); renderAttachments();
});
resetScheduleBtn.addEventListener("click", clearSchedule);
resetFormBtn.addEventListener("click", resetComposeForm);
[subject, body].forEach((field) => field.addEventListener("input", () => { updateSendState(); resetStatus(sendStatus); if (extractedEmails.length) updateWorkflow(2); }));
["dragenter", "dragover"].forEach((name) => uploadBox.addEventListener(name, (event) => { event.preventDefault(); uploadBox.classList.add("drag-over"); }));
["dragleave", "drop"].forEach((name) => uploadBox.addEventListener(name, (event) => { event.preventDefault(); uploadBox.classList.remove("drag-over"); }));
uploadBox.addEventListener("drop", (event) => { const file = event.dataTransfer.files[0]; if (!file) return; const transfer = new DataTransfer(); transfer.items.add(file); dataFile.files = transfer.files; dataFile.dispatchEvent(new Event("change")); });

uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault(); if (!dataFile.files.length) return showStatus(uploadStatus, "Please select a contact file before continuing.", "error");
    uploadBtn.disabled = true; uploadBtn.textContent = "Processing..."; showStatus(uploadStatus, "Uploading your file and extracting email addresses...", "info");
    try {
        const data = new FormData(); data.append("file", dataFile.files[0]); const response = await fetch("/upload-file", { method: "POST", body: data }); const result = await response.json(); if (!response.ok || !result.success) throw new Error(result.message || "Could not process the file.");
        extractedEmails = result.emails || []; $("summaryFile").textContent = result.filename; $("summaryColumn").textContent = result.email_column; $("summaryTotal").textContent = result.total_records; $("summaryValid").textContent = result.valid_email_count; $("summaryInvalid").textContent = result.invalid_email_count;
        emailPreview.value = extractedEmails.join("\n"); fileSummary.classList.remove("hidden"); emailPreviewSection.classList.remove("hidden"); showStatus(uploadStatus, `✓ ${result.message}`, "success"); updateSendState(); updateWorkflow(2); $("composeCard").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) { resetCampaign(); showStatus(uploadStatus, error.message || "Could not process the file.", "error"); }
    finally { uploadBtn.disabled = false; uploadBtn.textContent = "Upload & Extract Emails"; }
});

function renderCampaign(campaign) {
    $("resultTotal").textContent = campaign.total ?? 0; $("resultSent").textContent = campaign.sent ?? 0; $("resultFailed").textContent = campaign.failed ?? 0; $("resultTotalTime").textContent = `${Number(campaign.duration || 0).toFixed(2)} sec`;
    failedList.innerHTML = ""; (campaign.failed_details || []).forEach((item) => { const row = document.createElement("div"); row.className = "failed-item"; row.textContent = `${item.email} — ${item.last_error || "Unknown error"}`; failedList.appendChild(row); });
    failedSection.classList.toggle("hidden", !campaign.failed); retryFailedBtn.classList.toggle("hidden", !campaign.failed); resultsCard.classList.remove("hidden");
}
async function pollCampaign() {
    if (!campaignId) return;
    try {
        const response = await fetch(`/campaign/${campaignId}/status`); const result = await response.json(); if (!response.ok) throw new Error(result.message); const campaign = result.campaign;
        const progress = campaign.total ? Math.round(((campaign.sent + campaign.failed + campaign.cancelled) / campaign.total) * 100) : 0;
        sendInfoTitle.textContent = campaign.status === "scheduled" ? "Campaign scheduled" : `Campaign ${campaign.status}`; sendInfoText.textContent = `Queued: ${campaign.queued} • Sending: ${campaign.sending} • Sent: ${campaign.sent} • Failed: ${campaign.failed} (${progress}%)`; showStatus(sendStatus, `Live status: ${campaign.status}. ${progress}% processed.`, "info");
        if (["completed", "partially_failed", "failed", "cancelled"].includes(campaign.status)) { clearInterval(pollTimer); campaignSending = false; cancelBtn.classList.add("hidden"); renderCampaign(campaign); updateWorkflow(4); showStatus(sendStatus, `Campaign ${campaign.status.replace("_", " ")}.`, campaign.failed ? "info" : "success"); $("resultsCard").scrollIntoView({ behavior: "smooth", block: "start" }); updateSendState(); } }
    catch (error) { showStatus(sendStatus, error.message || "Could not refresh campaign status.", "error"); }
}

sendBtn.addEventListener("click", async () => {
    if (!extractedEmails.length || !subject.value.trim() || !body.value.trim()) return updateSendState();
    const date = $("scheduleDate").value, time = $("scheduleTime").value; if ((date && !time) || (!date && time)) return showStatus(sendStatus, "Choose both a schedule date and time, or leave both blank to send now.", "error");
    const data = new FormData(); data.append("emails", extractedEmails.join("\n")); data.append("subject", subject.value.trim()); data.append("body", body.value.trim()); selectedAttachments.forEach((file) => data.append("attachments[]", file)); if (date) { data.append("schedule_date", date); data.append("schedule_time", time); data.append("timezone", $("scheduleTimezone").value.trim() || "Asia/Kolkata"); }
    campaignSending = true; sendBtn.disabled = true; sendBtn.textContent = date ? "Scheduling..." : "Starting..."; resultsCard.classList.add("hidden"); failedSection.classList.add("hidden"); updateWorkflow(3);
    try { const response = await fetch("/send-emails", { method: "POST", body: data }); const result = await response.json(); if (!response.ok || !result.success) throw new Error(result.message || "Could not create campaign."); campaignId = result.campaign_id; cancelBtn.classList.remove("hidden"); showStatus(sendStatus, result.message, "info"); sendInfoTitle.textContent = date ? "Campaign scheduled" : "Campaign queued"; sendInfoText.textContent = `Campaign ID: ${campaignId}`; await pollCampaign(); pollTimer = setInterval(pollCampaign, 1000); if (result.status === "scheduled") { campaignSending = false; sendBtn.textContent = "Send Emails"; updateSendState(); } }
    catch (error) { campaignSending = false; showStatus(sendStatus, error.message || "Unable to start campaign.", "error"); updateSendState(); }
    finally { if (campaignSending) sendBtn.textContent = "Campaign Running"; }
});
cancelBtn.addEventListener("click", async () => { if (!campaignId) return; const response = await fetch(`/campaign/${campaignId}/cancel`, { method: "POST" }); const result = await response.json(); if (!response.ok) return showStatus(sendStatus, result.message || "Could not cancel campaign.", "error"); await pollCampaign(); });
retryFailedBtn.addEventListener("click", async () => { if (!campaignId) return; const response = await fetch(`/campaign/${campaignId}/retry-failed`, { method: "POST" }); const result = await response.json(); if (!response.ok) return showStatus(sendStatus, result.message || "Could not retry failures.", "error"); campaignId = result.campaign_id; campaignSending = true; cancelBtn.classList.remove("hidden"); updateWorkflow(3); pollTimer = setInterval(pollCampaign, 1000); await pollCampaign(); });
updateSelectedFile(); renderAttachments(); updateSendState(); updateWorkflow(1);