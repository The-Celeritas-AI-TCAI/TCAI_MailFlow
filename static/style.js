/* =========================================
   ELEMENTS
========================================= */

const uploadForm = document.getElementById("uploadForm");

const dataFile = document.getElementById("dataFile");
const uploadBox = document.getElementById("uploadBox");
const uploadBtn = document.getElementById("uploadBtn");
const uploadStatus = document.getElementById("uploadStatus");

const selectedDataFile = document.getElementById("selectedDataFile");
const selectedFileMeta = document.getElementById("selectedFileMeta");

const fileSummary = document.getElementById("fileSummary");

const emailPreviewSection =
    document.getElementById("emailPreviewSection");

const emailPreview =
    document.getElementById("emailPreview");

const summaryFile =
    document.getElementById("summaryFile");

const summaryColumn =
    document.getElementById("summaryColumn");

const summaryTotal =
    document.getElementById("summaryTotal");

const summaryValid =
    document.getElementById("summaryValid");

const summaryInvalid =
    document.getElementById("summaryInvalid");


const composeCard =
    document.getElementById("composeCard");

const subject =
    document.getElementById("subject");

const body =
    document.getElementById("body");

const attachment =
    document.getElementById("attachment");

const attachmentStatus =
    document.getElementById("attachmentStatus");


const sendCard =
    document.getElementById("sendCard");

const sendBtn =
    document.getElementById("sendBtn");

const sendStatus =
    document.getElementById("sendStatus");

const sendInfoTitle =
    document.getElementById("sendInfoTitle");

const sendInfoText =
    document.getElementById("sendInfoText");


const resultsCard =
    document.getElementById("resultsCard");

const resultTotal =
    document.getElementById("resultTotal");

const resultSent =
    document.getElementById("resultSent");

const resultFailed =
    document.getElementById("resultFailed");

const resultTotalTime =
    document.getElementById("resultTotalTime");

const failedSection =
    document.getElementById("failedSection");

const failedList =
    document.getElementById("failedList");


const topbarStatusText =
    document.getElementById("topbarStatusText");


/* =========================================
   WORKFLOW STATE
========================================= */

let extractedEmails = [];

let currentStep = 1;

let campaignSending = false;


/* =========================================
   PROGRESS ELEMENTS
========================================= */

const progressSteps =
    document.querySelectorAll(".progress-step");

const progressLines =
    document.querySelectorAll(".progress-line");


/* =========================================
   WORKFLOW CONTROLLER
========================================= */

function updateWorkflow(step) {

    currentStep = step;

    progressSteps.forEach((stepElement) => {

        const stepNumber =
            Number(stepElement.dataset.step);

        stepElement.classList.remove(
            "active",
            "completed"
        );

        if (stepNumber < step) {

            stepElement.classList.add(
                "completed"
            );

        } else if (stepNumber === step) {

            stepElement.classList.add(
                "active"
            );
        }
    });


    progressLines.forEach((line) => {

        const lineNumber =
            Number(line.dataset.line);

        line.classList.toggle(
            "completed",
            lineNumber < step
        );
    });


    /*
     * Highlight the current card.
     */

    document
        .querySelectorAll(".workflow-card")
        .forEach((card) => {

            card.classList.remove(
                "current-step"
            );
        });


    if (step === 1) {

        document
            .getElementById("contactsCard")
            .classList.add("current-step");

        topbarStatusText.textContent =
            "Import contacts";

    } else if (step === 2) {

        composeCard.classList.add(
            "current-step"
        );

        topbarStatusText.textContent =
            "Compose your email";

    } else if (step === 3) {

        sendCard.classList.add(
            "current-step"
        );

        topbarStatusText.textContent =
            "Ready to send";

    } else if (step === 4) {

        resultsCard.classList.add(
            "current-step"
        );

        topbarStatusText.textContent =
            "Campaign completed";
    }
}


/* =========================================
   SCROLL HELPER
========================================= */

function scrollToElement(element) {

    if (!element) {
        return;
    }

    setTimeout(() => {

        element.scrollIntoView({
            behavior: "smooth",
            block: "start"
        });

    }, 150);
}


/* =========================================
   STATUS
========================================= */

function showStatus(
    element,
    message,
    type
) {

    element.textContent = message;

    element.className =
        `status ${type}`;
}


function resetStatus(element) {

    element.textContent = "";

    element.className =
        "status hidden";
}


/* =========================================
   BUTTON LOADING
========================================= */

function setButtonLoading(
    button,
    loading,
    loadingText,
    normalText
) {

    if (loading) {

        button.disabled = true;

        button.classList.add(
            "loading"
        );

        button.textContent =
            loadingText;

    } else {

        button.disabled = false;

        button.classList.remove(
            "loading"
        );

        button.textContent =
            normalText;
    }
}


/* =========================================
   VALIDATE EMAIL COMPOSE FORM
========================================= */

function isComposeValid() {

    const hasContacts =
        extractedEmails.length > 0;

    const hasSubject =
        subject.value.trim().length > 0;

    const hasBody =
        body.value.trim().length > 0;

    return (
        hasContacts &&
        hasSubject &&
        hasBody
    );
}


/* =========================================
   UPDATE SEND BUTTON
========================================= */

function updateSendState() {

    if (campaignSending) {
        return;
    }

    const hasContacts =
        extractedEmails.length > 0;

    const hasSubject =
        subject.value.trim().length > 0;

    const hasBody =
        body.value.trim().length > 0;


    /*
     * No contacts yet.
     */

    if (!hasContacts) {

        sendBtn.disabled = true;

        sendInfoTitle.textContent =
            "Upload your contacts first";

        sendInfoText.textContent =
            "Import a contact file before you can send a campaign.";

        return;
    }


    /*
     * Contacts uploaded,
     * but email is incomplete.
     */

    if (!hasSubject || !hasBody) {

        sendBtn.disabled = true;

        sendInfoTitle.textContent =
            "Complete your email first";

        sendInfoText.textContent =
            "Enter both a subject and message to enable sending.";

        return;
    }


    /*
     * Everything is ready.
     */

    sendBtn.disabled = false;

    sendInfoTitle.textContent =
        "Your campaign is ready";

    sendInfoText.textContent =
        `${extractedEmails.length} valid recipient${extractedEmails.length === 1 ? "" : "s"} are ready to receive your email.`;
}


/* =========================================
   FILE DISPLAY
========================================= */

function updateSelectedFile() {

    if (!dataFile.files.length) {

        selectedDataFile.textContent =
            "No file selected";

        selectedFileMeta.textContent =
            "Ready for processing";

        return;
    }


    const file =
        dataFile.files[0];

    selectedDataFile.textContent =
        file.name;


    const sizeMB =
        file.size / (1024 * 1024);


    if (sizeMB < 1) {

        selectedFileMeta.textContent =
            `${Math.max(file.size / 1024, 1).toFixed(0)} KB • Ready for processing`;

    } else {

        selectedFileMeta.textContent =
            `${sizeMB.toFixed(2)} MB • Ready for processing`;
    }
}


/* =========================================
   ATTACHMENT DISPLAY
========================================= */

function updateAttachment() {

    if (!attachment.files.length) {

        attachmentStatus.textContent =
            "No attachment selected";

        attachmentStatus.style.color = "";

        return;
    }


    const file =
        attachment.files[0];

    const sizeMB =
        file.size / (1024 * 1024);


    attachmentStatus.textContent =
        sizeMB < 1
            ? `✓ ${file.name} • ${(file.size / 1024).toFixed(0)} KB`
            : `✓ ${file.name} • ${sizeMB.toFixed(2)} MB`;

    attachmentStatus.style.color =
        "#067647";
}


/* =========================================
   RESET CAMPAIGN
========================================= */

function resetCampaign() {

    extractedEmails = [];

    resultsCard.classList.add(
        "hidden"
    );

    failedSection.classList.add(
        "hidden"
    );

    emailPreviewSection.classList.add(
        "hidden"
    );

    fileSummary.classList.add(
        "hidden"
    );

    resetStatus(uploadStatus);
    resetStatus(sendStatus);

    updateSendState();

    updateWorkflow(1);
}


/* =========================================
   CONTACT FILE CHANGE
========================================= */

dataFile.addEventListener(
    "change",
    () => {

        updateSelectedFile();

        resetCampaign();

        if (dataFile.files.length) {

            showStatus(
                uploadStatus,
                `Selected ${dataFile.files[0].name}. Click "Upload & Extract Emails" to continue.`,
                "info"
            );
        }
    }
);


/* =========================================
   ATTACHMENT CHANGE
========================================= */

attachment.addEventListener(
    "change",
    () => {

        updateAttachment();
    }
);


/* =========================================
   DRAG & DROP
========================================= */

[
    "dragenter",
    "dragover"
].forEach((eventName) => {

    uploadBox.addEventListener(
        eventName,
        (event) => {

            event.preventDefault();
            event.stopPropagation();

            uploadBox.classList.add(
                "drag-over"
            );
        }
    );
});


[
    "dragleave",
    "drop"
].forEach((eventName) => {

    uploadBox.addEventListener(
        eventName,
        (event) => {

            event.preventDefault();
            event.stopPropagation();

            uploadBox.classList.remove(
                "drag-over"
            );
        }
    );
});


uploadBox.addEventListener(
    "drop",
    (event) => {

        const files =
            event.dataTransfer.files;

        if (!files.length) {
            return;
        }


        const file =
            files[0];

        const allowedExtensions = [
            ".pdf",
            ".csv",
            ".xls",
            ".xlsx"
        ];


        const fileName =
            file.name.toLowerCase();


        const validExtension =
            allowedExtensions.some(
                (extension) =>
                    fileName.endsWith(extension)
            );


        if (!validExtension) {

            showStatus(
                uploadStatus,
                "Please select a PDF, CSV, XLS, or XLSX file.",
                "error"
            );

            return;
        }


        try {

            const dataTransfer =
                new DataTransfer();

            dataTransfer.items.add(file);

            dataFile.files =
                dataTransfer.files;

            updateSelectedFile();

            resetCampaign();

            showStatus(
                uploadStatus,
                `Selected ${file.name}. Click "Upload & Extract Emails" to continue.`,
                "info"
            );

        } catch (error) {

            console.error(
                "Could not attach dropped file:",
                error
            );
        }
    }
);


/* =========================================
   UPLOAD CONTACTS
========================================= */

uploadForm.addEventListener(
    "submit",
    async (event) => {

        event.preventDefault();


        if (!dataFile.files.length) {

            showStatus(
                uploadStatus,
                "Please select a contact file before continuing.",
                "error"
            );

            return;
        }


        const formData =
            new FormData();

        formData.append(
            "file",
            dataFile.files[0]
        );


        setButtonLoading(
            uploadBtn,
            true,
            "Processing...",
            "Upload & Extract Emails"
        );


        showStatus(
            uploadStatus,
            "Uploading your file and extracting email addresses...",
            "info"
        );


        try {

            const response =
                await fetch(
                    "/upload-file",
                    {
                        method: "POST",
                        body: formData
                    }
                );


            let result;


            try {

                result =
                    await response.json();

            } catch {

                throw new Error(
                    "The server returned an invalid response."
                );
            }


            if (
                !response.ok ||
                !result.success
            ) {

                throw new Error(
                    result.message ||
                    "Could not process the file."
                );
            }


            extractedEmails =
                result.emails || [];


            if (!extractedEmails.length) {

                throw new Error(
                    "No valid email addresses were found in the uploaded file."
                );
            }


            /*
             * DISPLAY SUCCESS
             */

            showStatus(
                uploadStatus,
                `✓ ${result.message} ${result.next_step || ""}`,
                "success"
            );


            /*
             * DISPLAY SUMMARY
             */

            summaryFile.textContent =
                result.filename || "-";

            summaryColumn.textContent =
                result.email_column || "-";

            summaryTotal.textContent =
                result.total_records ?? 0;

            summaryValid.textContent =
                result.valid_email_count ?? 0;

            summaryInvalid.textContent =
                result.invalid_email_count ?? 0;


            fileSummary.classList.remove(
                "hidden"
            );


            /*
             * DISPLAY EMAIL PREVIEW
             */

            emailPreview.value =
                extractedEmails.join("\n");

            emailPreviewSection.classList.remove(
                "hidden"
            );


            /*
             * UPDATE SEND STATE
             */

            updateSendState();


            /*
             * IMPORTANT:
             *
             * Move workflow to COMPOSE.
             *
             * DO NOT jump to Send.
             */

            updateWorkflow(2);


            /*
             * Scroll to Compose,
             * NOT Send.
             */

            scrollToElement(
                composeCard
            );


            /*
             * Focus subject so the user
             * naturally continues the flow.
             */

            setTimeout(() => {

                subject.focus();

            }, 500);

        } catch (error) {

            extractedEmails = [];

            updateSendState();

            updateWorkflow(1);

            showStatus(
                uploadStatus,
                error.message ||
                "Something went wrong while processing the file.",
                "error"
            );

        } finally {

            setButtonLoading(
                uploadBtn,
                false,
                "",
                "Upload & Extract Emails"
            );
        }
    }
);


/* =========================================
   SUBJECT INPUT
========================================= */

subject.addEventListener(
    "input",
    () => {

        if (extractedEmails.length > 0) {

            updateWorkflow(2);
        }

        updateSendState();

        resetStatus(sendStatus);
    }
);


/* =========================================
   BODY INPUT
========================================= */

body.addEventListener(
    "input",
    () => {

        if (extractedEmails.length > 0) {

            updateWorkflow(2);
        }

        updateSendState();

        resetStatus(sendStatus);
    }
);


/* =========================================
   SEND CAMPAIGN
========================================= */

sendBtn.addEventListener(
    "click",
    async () => {

        /*
         * Safety check.
         */

        if (!extractedEmails.length) {

            showStatus(
                sendStatus,
                "No extracted email addresses are available.",
                "error"
            );

            updateWorkflow(1);

            return;
        }


        const subjectValue =
            subject.value.trim();

        const bodyValue =
            body.value.trim();


        /*
         * Subject validation.
         */

        if (!subjectValue) {

            showStatus(
                sendStatus,
                "Please enter an email subject.",
                "error"
            );

            updateWorkflow(2);

            subject.focus();

            scrollToElement(
                composeCard
            );

            return;
        }


        /*
         * Body validation.
         */

        if (!bodyValue) {

            showStatus(
                sendStatus,
                "Please enter your email message.",
                "error"
            );

            updateWorkflow(2);

            body.focus();

            scrollToElement(
                composeCard
            );

            return;
        }


        /*
         * Move to SEND step.
         */

        updateWorkflow(3);


        /*
         * Prepare request.
         */

        const formData =
            new FormData();


        formData.append(
            "emails",
            extractedEmails.join("\n")
        );


        formData.append(
            "subject",
            subjectValue
        );


        formData.append(
            "body",
            bodyValue
        );


        if (attachment.files.length > 0) {

            formData.append(
                "attachment",
                attachment.files[0]
            );
        }


        campaignSending = true;

        sendBtn.disabled = true;

        sendBtn.classList.add(
            "loading"
        );

        sendBtn.textContent =
            "Sending...";


        resultsCard.classList.add(
            "hidden"
        );

        failedSection.classList.add(
            "hidden"
        );


        sendInfoTitle.textContent =
            "Campaign is being sent";

        sendInfoText.textContent =
            `Sending to ${extractedEmails.length} recipient${extractedEmails.length === 1 ? "" : "s"}...`;


        showStatus(
            sendStatus,
            `Sending emails to ${extractedEmails.length} recipients...`,
            "info"
        );


        /*
         * Keep user on Send while
         * campaign is running.
         */

        scrollToElement(
            sendCard
        );


        try {

            const response =
                await fetch(
                    "/send-emails",
                    {
                        method: "POST",
                        body: formData
                    }
                );


            let result;


            try {

                result =
                    await response.json();

            } catch {

                throw new Error(
                    "The server returned an invalid response."
                );
            }


            if (
                !response.ok ||
                !result.success
            ) {

                throw new Error(
                    result.message ||
                    "Email sending failed."
                );
            }


            /*
             * RESULTS
             */

            resultTotal.textContent =
                result.total ?? 0;

            resultSent.textContent =
                result.sent ?? 0;

            resultFailed.textContent =
                result.failed ?? 0;


            const totalTime =
                Number(result.total_time);


            resultTotalTime.textContent =
                Number.isFinite(totalTime)
                    ? `${totalTime.toFixed(2)} sec`
                    : `${result.total_time || 0} sec`;


            /*
             * FAILED EMAILS
             */

            failedList.innerHTML = "";


            if (
                Number(result.failed) > 0 &&
                Array.isArray(
                    result.failed_details
                )
            ) {

                result.failed_details.forEach(
                    (item) => {

                        const div =
                            document.createElement(
                                "div"
                            );

                        div.className =
                            "failed-item";


                        const email =
                            item.email ||
                            "Unknown email";


                        const error =
                            item.error ||
                            "Unknown error";


                        div.textContent =
                            `${email} — ${error}`;


                        failedList.appendChild(
                            div
                        );
                    }
                );


                failedSection.classList.remove(
                    "hidden"
                );

            } else {

                failedSection.classList.add(
                    "hidden"
                );
            }


            /*
             * SHOW RESULTS
             */

            resultsCard.classList.remove(
                "hidden"
            );


            if (Number(result.failed) > 0) {

                showStatus(
                    sendStatus,
                    `Campaign completed. ${result.sent} emails sent and ${result.failed} failed.`,
                    "info"
                );

            } else {

                showStatus(
                    sendStatus,
                    `Campaign completed successfully. All ${result.sent} emails were sent.`,
                    "success"
                );
            }


            /*
             * Move progress to RESULTS.
             */

            updateWorkflow(4);


            sendInfoTitle.textContent =
                "Campaign completed";

            sendInfoText.textContent =
                "Your campaign delivery report is available below.";


            /*
             * Now scroll to Results.
             *
             * This only happens AFTER
             * the campaign has completed.
             */

            scrollToElement(
                resultsCard
            );

        } catch (error) {

            /*
             * Sending failed.
             *
             * Keep the workflow at Send.
             */

            updateWorkflow(3);

            showStatus(
                sendStatus,
                error.message ||
                "Unable to send the campaign.",
                "error"
            );


            sendInfoTitle.textContent =
                "Campaign could not be completed";

            sendInfoText.textContent =
                "Fix the issue and try sending the campaign again.";
        }


        finally {

            campaignSending = false;

            sendBtn.disabled =
                !isComposeValid();

            sendBtn.classList.remove(
                "loading"
            );

            sendBtn.textContent =
                "Send Emails";

            updateSendState();
        }
    }
);


/* =========================================
   INITIAL STATE
========================================= */

updateSelectedFile();

updateAttachment();

updateSendState();

updateWorkflow(1);