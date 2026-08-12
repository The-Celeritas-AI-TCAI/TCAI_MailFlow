const uploadForm = document.getElementById("uploadForm");
const dataFile = document.getElementById("dataFile");
const uploadBtn = document.getElementById("uploadBtn");
const uploadStatus = document.getElementById("uploadStatus");

const selectedDataFile = document.getElementById("selectedDataFile");
const fileSummary = document.getElementById("fileSummary");
const emailPreviewSection = document.getElementById("emailPreviewSection");
const emailPreview = document.getElementById("emailPreview");

const summaryFile = document.getElementById("summaryFile");
const summaryColumn = document.getElementById("summaryColumn");
const summaryTotal = document.getElementById("summaryTotal");
const summaryValid = document.getElementById("summaryValid");
const summaryInvalid = document.getElementById("summaryInvalid");

const subject = document.getElementById("subject");
const body = document.getElementById("body");
const attachment = document.getElementById("attachment");
const attachmentStatus = document.getElementById("attachmentStatus");

const sendBtn = document.getElementById("sendBtn");
const sendStatus = document.getElementById("sendStatus");

const resultsCard = document.getElementById("resultsCard");
const resultTotal = document.getElementById("resultTotal");
const resultSent = document.getElementById("resultSent");
const resultFailed = document.getElementById("resultFailed");
const failedSection = document.getElementById("failedSection");
const failedList = document.getElementById("failedList");


let extractedEmails = [];


function showStatus(element, message, type) {
    element.textContent = message;
    element.className = `status ${type}`;
}


function resetStatus(element) {
    element.textContent = "";
    element.className = "status hidden";
}


dataFile.addEventListener("change", () => {
    if (dataFile.files.length > 0) {
        selectedDataFile.textContent = `Selected: ${dataFile.files[0].name}`;
    } else {
        selectedDataFile.textContent = "No file selected";
    }

    fileSummary.classList.add("hidden");
    emailPreviewSection.classList.add("hidden");
    sendBtn.disabled = true;
    extractedEmails = [];
});


attachment.addEventListener("change", () => {
    if (attachment.files.length > 0) {
        attachmentStatus.textContent =
            `✅ Attachment selected: ${attachment.files[0].name}`;
    } else {
        attachmentStatus.textContent = "No attachment selected";
    }
});


uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    if (!dataFile.files.length) {
        showStatus(uploadStatus, "Please select a data file.", "error");
        return;
    }

    const formData = new FormData();
    formData.append("file", dataFile.files[0]);

    uploadBtn.disabled = true;
    uploadBtn.textContent = "Processing...";
    showStatus(uploadStatus, "Uploading and extracting email addresses...", "info");

    try {
        const response = await fetch("/upload-file", {
            method: "POST",
            body: formData
        });

        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.message || "Could not process the file.");
        }

        extractedEmails = result.emails || [];

        showStatus(
            uploadStatus,
            `✅ ${result.message} ${result.next_step}`,
            "success"
        );

        summaryFile.textContent = result.filename;
        summaryColumn.textContent = result.email_column;
        summaryTotal.textContent = result.total_records;
        summaryValid.textContent = result.valid_email_count;
        summaryInvalid.textContent = result.invalid_email_count;

        fileSummary.classList.remove("hidden");

        emailPreview.value = extractedEmails.join("\n");
        emailPreviewSection.classList.remove("hidden");

        sendBtn.disabled = false;

    } catch (error) {
        extractedEmails = [];
        sendBtn.disabled = true;
        showStatus(uploadStatus, error.message, "error");
    } finally {
        uploadBtn.disabled = false;
        uploadBtn.textContent = "Upload & Extract Emails";
    }
});


sendBtn.addEventListener("click", async () => {
    if (!extractedEmails.length) {
        showStatus(sendStatus, "No extracted emails are available.", "error");
        return;
    }

    if (!subject.value.trim()) {
        showStatus(sendStatus, "Subject is required.", "error");
        return;
    }

    if (!body.value.trim()) {
        showStatus(sendStatus, "Email body is required.", "error");
        return;
    }

    const formData = new FormData();
    formData.append("emails", extractedEmails.join("\n"));
    formData.append("subject", subject.value.trim());
    formData.append("body", body.value.trim());

    if (attachment.files.length > 0) {
        formData.append("attachment", attachment.files[0]);
    }

    sendBtn.disabled = true;
    sendBtn.textContent = "Sending...";
    resultsCard.classList.add("hidden");
    failedSection.classList.add("hidden");

    showStatus(
        sendStatus,
        `Sending emails to ${extractedEmails.length} recipients...`,
        "info"
    );

    try {
        const response = await fetch("/send-emails", {
            method: "POST",
            body: formData
        });

        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.message || "Email sending failed.");
        }

        resultTotal.textContent = result.total;
        resultSent.textContent = result.sent;
        resultFailed.textContent = result.failed;

        if (result.failed > 0) {
            failedList.innerHTML = "";

            result.failed_details.forEach((item) => {
                const div = document.createElement("div");
                div.className = "failed-item";
                div.textContent = `${item.email} — ${item.error}`;
                failedList.appendChild(div);
            });

            failedSection.classList.remove("hidden");
        }

        resultsCard.classList.remove("hidden");

        showStatus(
            sendStatus,
            `✅ Campaign completed. Sent: ${result.sent}, Failed: ${result.failed}.`,
            result.failed > 0 ? "info" : "success"
        );

    } catch (error) {
        showStatus(sendStatus, error.message, "error");
    } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = "Send Emails";
    }
});
