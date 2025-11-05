const state = {
  files: [],
  capacity: 0,
  totalSize: 0,
  queue: [],
};

const MAX_FILE_SIZE = 500 * 1024 * 1024; // 500 MB

function redirectToLogin() {
  window.location.href = "/login";
}

function handleUnauthorizedResponse(response) {
  if (response && response.status === 401) {
    redirectToLogin();
    return true;
  }
  return false;
}

function initialise() {
  const initial = window.initialFileData || { files: [], totalSize: 0, capacity: 0 };
  state.files = initial.files || [];
  state.totalSize = initial.totalSize || 0;
  state.capacity = initial.capacity || 0;
  renderFiles();
  updateStats();
  setupEventListeners();
  // Fetch the latest data in the background to ensure we are up to date.
  refreshFiles();
}

document.addEventListener("DOMContentLoaded", initialise);

function setupEventListeners() {
  const fileInput = document.getElementById("file-input");
  const dropzone = document.getElementById("dropzone");
  const startUploadButton = document.getElementById("start-upload");
  const refreshButton = document.getElementById("refresh-files");

  fileInput?.addEventListener("change", (event) => {
    const files = event.target.files;
    if (files && files.length) {
      addFilesToQueue(files);
      fileInput.value = "";
    }
  });

  const preventDefaults = (event) => {
    event.preventDefault();
    event.stopPropagation();
  };

  if (dropzone) {
    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        preventDefaults(event);
        dropzone.classList.add("border-primary", "bg-primary/10");
      });
    });

    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        preventDefaults(event);
        dropzone.classList.remove("border-primary", "bg-primary/10");
      });
    });

    dropzone.addEventListener("drop", (event) => {
      const files = event.dataTransfer?.files;
      if (files && files.length) {
        addFilesToQueue(files);
      }
    });
  }

  startUploadButton?.addEventListener("click", () => {
    const hasPending = state.queue.some((item) => item.status === "pending");
    if (!hasPending) {
      showToast("Keine Dateien in der Warteschlange.", "info");
      return;
    }
    state.queue.forEach((item) => {
      if (item.status === "pending") {
        uploadQueueItem(item);
      }
    });
  });

  refreshButton?.addEventListener("click", () => {
    refreshFiles(true);
  });
}

function addFilesToQueue(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) {
    return;
  }

  let addedCount = 0;
  files.forEach((file) => {
    if (file.size > MAX_FILE_SIZE) {
      showToast(`"${file.name}" ist größer als 500 MB und wurde übersprungen.`, "error");
      return;
    }

    const queueItem = {
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      file,
      status: "pending",
      progress: 0,
      message: "Bereit für Upload",
      element: null,
    };
    state.queue.push(queueItem);
    addedCount += 1;
  });

  renderQueue();
  if (addedCount > 0) {
    showToast(`${addedCount} Datei(en) hinzugefügt. Starte den Upload, wenn du bereit bist.`, "success");
  }
}

function renderQueue() {
  const container = document.getElementById("upload-queue");
  if (!container) {
    return;
  }

  container.innerHTML = "";
  if (!state.queue.length) {
    container.innerHTML = `
      <div class="rounded-xl border border-dashed border-gray-300 p-6 text-center text-sm text-gray-500 dark:border-[#324867] dark:text-[#92a9c9]">
        Keine Dateien in der Warteschlange.
      </div>
    `;
    return;
  }

  state.queue.forEach((item) => {
    const row = document.createElement("div");
    row.className = "flex flex-col gap-3 rounded-xl border border-gray-200/60 bg-white/80 p-4 dark:border-[#324867] dark:bg-[#1A1B26]/60";
    row.dataset.id = item.id;
    row.innerHTML = `
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <p class="truncate font-semibold text-gray-900 dark:text-white">${item.file.name}</p>
          <p class="text-sm text-gray-500 dark:text-[#92a9c9]">${formatBytes(item.file.size)}</p>
        </div>
        <div class="flex items-center gap-2">
          <span class="rounded-full px-3 py-1 text-xs font-semibold" data-role="badge">${statusLabel(item.status)}</span>
          <button class="rounded-full border border-gray-200/60 px-2 py-1 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-100 dark:border-white/10 dark:text-white dark:hover:bg-white/10" data-action="remove">Entfernen</button>
        </div>
      </div>
      <div class="h-2 w-full rounded-full bg-gray-200 dark:bg-gray-700">
        <div class="h-2 rounded-full bg-primary" data-role="progress" style="width: ${item.progress}%"></div>
      </div>
      <p class="text-xs text-gray-500 dark:text-[#92a9c9]" data-role="message">${item.message}</p>
    `;

    const removeButton = row.querySelector('[data-action="remove"]');
    removeButton?.addEventListener("click", () => {
      if (item.status !== "pending") {
        showToast("Nur wartende Dateien können entfernt werden.", "info");
        return;
      }
      state.queue = state.queue.filter((queueItem) => queueItem.id !== item.id);
      renderQueue();
    });

    item.element = row;
    updateQueueVisual(item);
    container.appendChild(row);
  });
}

function updateQueueVisual(item) {
  if (!item.element) {
    return;
  }

  const badge = item.element.querySelector('[data-role="badge"]');
  const progressBar = item.element.querySelector('[data-role="progress"]');
  const message = item.element.querySelector('[data-role="message"]');
  const removeButton = item.element.querySelector('[data-action="remove"]');

  if (badge) {
    badge.textContent = statusLabel(item.status);
    badge.className = `rounded-full px-3 py-1 text-xs font-semibold ${statusBadgeClass(item.status)}`;
  }

  if (progressBar) {
    progressBar.style.width = `${Math.round(item.progress)}%`;
    progressBar.className = `h-2 rounded-full ${statusProgressClass(item.status)}`;
  }

  if (message) {
    message.textContent = item.message || "";
  }

  if (removeButton) {
    removeButton.disabled = item.status !== "pending";
    removeButton.classList.toggle("opacity-50", item.status !== "pending");
  }
}

function uploadQueueItem(item) {
  item.status = "uploading";
  item.progress = 0;
  item.message = "Upload läuft...";
  updateQueueVisual(item);

  const formData = new FormData();
  formData.append("files", item.file);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");

  xhr.upload.addEventListener("progress", (event) => {
    if (event.lengthComputable) {
      item.progress = (event.loaded / event.total) * 100;
      updateQueueVisual(item);
    }
  });

  xhr.addEventListener("load", () => {
    if (xhr.status === 401) {
      redirectToLogin();
      return;
    }
    try {
      const response = JSON.parse(xhr.responseText || "{}");
      if (xhr.status >= 200 && xhr.status < 300) {
        const uploadedFile = response.files?.[0];
        item.status = "success";
        item.progress = 100;
        item.message = "Upload abgeschlossen.";
        updateQueueVisual(item);
        showToast(`"${item.file.name}" wurde erfolgreich hochgeladen.`, "success");
        if (uploadedFile) {
          insertOrUpdateFile(uploadedFile);
          updateStats();
          renderFiles();
        } else {
          refreshFiles();
        }
        scheduleQueueRemoval(item);
      } else {
        item.status = "error";
        item.message = response.message || "Der Upload ist fehlgeschlagen.";
        updateQueueVisual(item);
        showToast(item.message, "error");
      }
    } catch (error) {
      item.status = "error";
      item.message = "Der Upload ist fehlgeschlagen.";
      updateQueueVisual(item);
      showToast(item.message, "error");
    }
  });

  xhr.addEventListener("error", () => {
    item.status = "error";
    item.message = "Netzwerkfehler beim Upload.";
    updateQueueVisual(item);
    showToast(item.message, "error");
  });

  xhr.send(formData);
}

function scheduleQueueRemoval(item) {
  setTimeout(() => {
    const index = state.queue.findIndex((entry) => entry.id === item.id);
    if (index !== -1 && state.queue[index].status === "success") {
      state.queue.splice(index, 1);
      renderQueue();
    }
  }, 4000);
}

function statusLabel(status) {
  switch (status) {
    case "pending":
      return "Bereit";
    case "uploading":
      return "Upload läuft";
    case "success":
      return "Fertig";
    case "error":
      return "Fehler";
    default:
      return status;
  }
}

function statusBadgeClass(status) {
  switch (status) {
    case "pending":
      return "bg-gray-100 text-gray-600 dark:bg-white/5 dark:text-white";
    case "uploading":
      return "bg-primary/10 text-primary dark:bg-primary/20 dark:text-white";
    case "success":
      return "bg-green-100 text-green-700 dark:bg-green-500/20 dark:text-green-200";
    case "error":
      return "bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-200";
    default:
      return "bg-gray-100 text-gray-600";
  }
}

function statusProgressClass(status) {
  switch (status) {
    case "success":
      return "bg-green-500";
    case "error":
      return "bg-red-500";
    default:
      return "bg-primary";
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / Math.pow(1024, exponent);
  return `${value.toFixed(value >= 10 || exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

function formatStorage(bytes) {
  if (bytes >= 1024 ** 3) {
    return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  }
  if (bytes >= 1024 ** 2) {
    return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  }
  if (bytes >= 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }
  return `${bytes} B`;
}

function formatDate(dateString) {
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) {
    return "Unbekannt";
  }
  return date.toLocaleString("de-DE", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

async function refreshFiles(showToastOnSuccess = false) {
  try {
    const response = await fetch("/api/files");
    if (handleUnauthorizedResponse(response)) {
      return;
    }
    if (!response.ok) {
      throw new Error("Fehler beim Laden der Dateien.");
    }
    const data = await response.json();
    state.files = data.files || [];
    state.totalSize = data.total_size || 0;
    state.capacity = data.capacity || 0;
    renderFiles();
    updateStats();
    if (showToastOnSuccess) {
      showToast("Dateiliste aktualisiert.", "success");
    }
  } catch (error) {
    showToast(error.message || "Die Dateiliste konnte nicht geladen werden.", "error");
  }
}

function updateStats() {
  const usedElement = document.getElementById("used-storage");
  const totalElement = document.getElementById("total-storage");
  const progressElement = document.getElementById("storage-progress");
  const percentElement = document.getElementById("storage-percent");
  const fileCountElement = document.getElementById("file-count");
  const shareCountElement = document.getElementById("share-count");

  const used = state.totalSize || 0;
  const capacity = state.capacity || 1;
  const percent = Math.min((used / capacity) * 100, 100);
  const shareCount = state.files.filter((file) => Boolean(file.share_url)).length;

  if (usedElement) {
    usedElement.textContent = formatStorage(used);
  }
  if (totalElement) {
    totalElement.textContent = formatStorage(capacity);
  }
  if (progressElement) {
    progressElement.style.width = `${percent}%`;
    progressElement.className = `h-2 rounded-full ${percent > 90 ? "bg-red-500" : percent > 70 ? "bg-orange-400" : "bg-primary"}`;
  }
  if (percentElement) {
    percentElement.textContent = `${percent.toFixed(1)}%`;
  }
  if (fileCountElement) {
    fileCountElement.textContent = state.files.length;
  }
  if (shareCountElement) {
    shareCountElement.textContent = shareCount;
  }
}

function renderFiles() {
  const grid = document.getElementById("file-grid");
  const emptyState = document.getElementById("empty-state");
  if (!grid) {
    return;
  }

  grid.querySelectorAll(".file-card").forEach((element) => element.remove());

  if (!state.files.length) {
    if (emptyState) {
      emptyState.classList.remove("hidden");
    }
    return;
  }

  if (emptyState) {
    emptyState.classList.add("hidden");
  }

  state.files.forEach((file) => {
    const card = createFileCard(file);
    grid.appendChild(card);
  });
}

function createFileCard(file) {
  const card = document.createElement("div");
  card.className = "file-card flex flex-col gap-4 rounded-xl border border-gray-200/60 bg-white p-5 shadow-sm transition hover:-translate-y-1 hover:shadow-lg dark:border-[#324867] dark:bg-[#1A1B26]/60";

  const ownerBadge =
    file.owner && window.currentUser?.is_admin
      ? `<span class="mt-1 inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-1 text-xs font-semibold text-primary/80">Eigentümer: ${file.owner.username}</span>`
      : "";

  const viewButton = file.view_url
    ? `<a class="action-button" data-action="view" href="${file.view_url}" target="_blank" rel="noopener">
        <span class="material-symbols-outlined text-base">visibility</span>
        Ansehen
      </a>`
    : "";

  const directShareButton = file.share_raw_url
    ? `<button class="action-button" data-action="copy-direct" type="button">
        <span class="material-symbols-outlined text-base">open_in_new</span>
        Direktlink
      </button>`
    : "";

  card.innerHTML = `
    <div class="flex items-center gap-3">
      <span class="material-symbols-outlined text-4xl text-primary/80">${iconForFile(file)}</span>
      <div class="min-w-0">
        <p class="truncate text-lg font-semibold text-gray-900 dark:text-white" title="${file.name}">${file.name}</p>
        <p class="text-sm text-gray-500 dark:text-[#92a9c9]">${formatBytes(file.size)} · ${formatDate(file.uploaded_at)}</p>
        ${ownerBadge}
      </div>
    </div>
    <div class="flex flex-wrap gap-2">
      ${viewButton}
      <a class="action-button" data-action="download" href="${file.download_url}">
        <span class="material-symbols-outlined text-base">download</span>
        Download
      </a>
      <button class="action-button" data-action="share" type="button">
        <span class="material-symbols-outlined text-base">link</span>
        Link kopieren
      </button>
      <button class="action-button" data-action="rename" type="button">
        <span class="material-symbols-outlined text-base">edit</span>
        Umbenennen
      </button>
      <button class="action-button danger" data-action="delete" type="button">
        <span class="material-symbols-outlined text-base">delete</span>
        Löschen
      </button>
    </div>
    <div class="share-area ${file.share_url ? "" : "hidden"}" data-role="share-area">
      <label class="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-[#92a9c9]">Freigabelink</label>
      <div class="flex items-center gap-2">
        <input class="flex-1 rounded-lg border border-gray-200/60 bg-white px-3 py-2 text-sm text-gray-700 dark:border-white/10 dark:bg-[#111822] dark:text-white" data-role="share-input" readonly value="${file.share_url || ""}" />
        <button class="rounded-lg bg-primary px-3 py-2 text-sm font-semibold text-white transition hover:bg-primary/90" data-action="copy">Kopieren</button>
        <button class="rounded-lg border border-gray-200/60 px-3 py-2 text-sm font-semibold text-gray-600 transition hover:bg-gray-100 dark:border-white/10 dark:text-white dark:hover:bg-white/10" data-action="revoke">Entziehen</button>
        ${directShareButton}
      </div>
    </div>
  `;

  card.querySelectorAll(".action-button").forEach((button) => {
    button.classList.add(
      "inline-flex",
      "items-center",
      "gap-2",
      "rounded-lg",
      "border",
      "border-gray-200/60",
      "px-3",
      "py-2",
      "text-sm",
      "font-semibold",
      "text-gray-700",
      "transition",
      "hover:bg-gray-100",
      "dark:border-white/10",
      "dark:text-white",
      "dark:hover:bg-white/10"
    );
  });

  const shareArea = card.querySelector('[data-role="share-area"]');
  const shareInput = card.querySelector('[data-role="share-input"]');
  const shareButton = card.querySelector('[data-action="share"]');
  const copyButton = card.querySelector('[data-action="copy"]');
  const copyDirectButton = card.querySelector('[data-action="copy-direct"]');
  const revokeButton = card.querySelector('[data-action="revoke"]');
  const renameButton = card.querySelector('[data-action="rename"]');
  const deleteButton = card.querySelector('[data-action="delete"]');
  const downloadLink = card.querySelector('[data-action="download"]');
  const viewLink = card.querySelector('[data-action="view"]');

  downloadLink?.setAttribute("download", file.name);
  downloadLink?.setAttribute("target", "_blank");
  downloadLink?.setAttribute("rel", "noopener");
  viewLink?.setAttribute("rel", "noopener");

  const canManage = file.can_manage !== false;
  if (!canManage) {
    shareButton?.classList.add("hidden");
    copyButton?.classList.add("hidden");
    copyDirectButton?.classList.add("hidden");
    revokeButton?.classList.add("hidden");
    renameButton?.classList.add("hidden");
    deleteButton?.classList.add("hidden");
    shareArea?.classList.add("hidden");
  }

  shareButton?.addEventListener("click", async () => {
    await ensureShareLink(file, shareArea, shareInput);
  });

  copyButton?.addEventListener("click", () => {
    if (!file.share_url) {
      showToast("Es gibt noch keinen Freigabelink.", "info");
      return;
    }
    copyToClipboard(file.share_url);
  });

  copyDirectButton?.addEventListener("click", () => {
    if (!file.share_raw_url) {
      showToast("Es gibt noch keinen Direktlink.", "info");
      return;
    }
    copyToClipboard(file.share_raw_url);
  });

  revokeButton?.addEventListener("click", async () => {
    if (!file.share_url) {
      showToast("Es gibt keinen aktiven Link zu entfernen.", "info");
      return;
    }
    const confirmed = confirm("Möchtest du den Freigabelink wirklich entziehen?");
    if (!confirmed) {
      return;
    }
    try {
      const response = await fetch(`/api/files/${file.id}/share`, {
        method: "DELETE",
      });
      if (handleUnauthorizedResponse(response)) {
        return;
      }
      if (!response.ok) {
        throw new Error("Der Freigabelink konnte nicht entfernt werden.");
      }
      file.share_url = null;
      shareInput.value = "";
      shareArea?.classList.add("hidden");
      insertOrUpdateFile(file);
      updateStats();
      renderFiles();
      showToast("Freigabelink entfernt.", "success");
    } catch (error) {
      showToast(error.message || "Der Freigabelink konnte nicht entfernt werden.", "error");
    }
  });

  renameButton?.addEventListener("click", async () => {
    const newName = prompt("Neuen Dateinamen eingeben", file.name);
    if (!newName || newName === file.name) {
      return;
    }
    try {
      const response = await fetch(`/api/files/${file.id}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
      if (handleUnauthorizedResponse(response)) {
        return;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.message || "Der Dateiname konnte nicht geändert werden.");
      }
      const data = await response.json();
      const updatedFile = data.file;
      insertOrUpdateFile(updatedFile);
      updateStats();
      renderFiles();
      showToast("Datei wurde umbenannt.", "success");
    } catch (error) {
      showToast(error.message || "Der Dateiname konnte nicht geändert werden.", "error");
    }
  });

  deleteButton?.addEventListener("click", async () => {
    const confirmed = confirm(`Möchtest du "${file.name}" endgültig löschen?`);
    if (!confirmed) {
      return;
    }
    try {
      const response = await fetch(`/api/files/${file.id}`, {
        method: "DELETE",
      });
      if (handleUnauthorizedResponse(response)) {
        return;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.message || "Die Datei konnte nicht gelöscht werden.");
      }
      state.files = state.files.filter((entry) => entry.id !== file.id);
      showToast("Datei wurde gelöscht.", "success");
      await refreshFiles();
    } catch (error) {
      showToast(error.message || "Die Datei konnte nicht gelöscht werden.", "error");
    }
  });

  return card;
}

async function ensureShareLink(file, shareArea, shareInput) {
  if (file.share_url) {
    copyToClipboard(file.share_url, {
      successMessage: "Freigabelink wurde in die Zwischenablage kopiert.",
    });
    return;
  }
  try {
    const response = await fetch(`/api/files/${file.id}/share`, {
      method: "POST",
    });
    if (handleUnauthorizedResponse(response)) {
      return;
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.message || "Der Freigabelink konnte nicht erstellt werden.");
    }
    const data = await response.json();
    file.share_url = data.share_url;
    file.share_raw_url = data.share_raw_url || null;
    insertOrUpdateFile(file);
    if (shareInput) {
      shareInput.value = file.share_url;
    }
    shareArea?.classList.remove("hidden");
    updateStats();
    renderFiles();
    copyToClipboard(file.share_url, {
      successMessage: "Freigabelink erstellt und kopiert.",
    });
  } catch (error) {
    showToast(error.message || "Der Freigabelink konnte nicht erstellt werden.", "error");
  }
}

function insertOrUpdateFile(file) {
  const existingIndex = state.files.findIndex((entry) => entry.id === file.id);
  if (existingIndex !== -1) {
    state.files[existingIndex] = file;
  } else {
    state.files.unshift(file);
  }
  state.totalSize = state.files.reduce((total, current) => total + (current.size || 0), 0);
}

function iconForFile(file) {
  const type = (file.content_type || "").toLowerCase();
  if (type.startsWith("image/")) {
    return "image";
  }
  if (type.startsWith("video/")) {
    return "movie";
  }
  if (type.startsWith("audio/")) {
    return "music_note";
  }
  if (type.includes("zip") || type.includes("compress")) {
    return "folder_zip";
  }
  if (type.includes("pdf")) {
    return "picture_as_pdf";
  }
  return "description";
}

function copyToClipboard(text, options = {}) {
  if (!text) {
    return;
  }

  const {
    successMessage = "In die Zwischenablage kopiert.",
    showToastMessage = true,
  } = options;

  const handleSuccess = () => {
    if (showToastMessage) {
      showToast(successMessage, "success");
    }
  };

  if (navigator.clipboard?.writeText) {
    navigator.clipboard
      .writeText(text)
      .then(handleSuccess)
      .catch(() => {
        fallbackCopyToClipboard(text, successMessage, showToastMessage);
      });
  } else {
    fallbackCopyToClipboard(text, successMessage, showToastMessage);
  }
}

function fallbackCopyToClipboard(text, successMessage, showToastMessage) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "absolute";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand("copy");
    if (showToastMessage) {
      showToast(successMessage, "success");
    }
  } catch (error) {
    showToast("Kopieren nicht möglich. Bitte manuell kopieren.", "error");
  }
  document.body.removeChild(textarea);
}

function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  if (!container) {
    console.log(message);
    return;
  }

  const options = {
    info: {
      icon: "info",
      classes:
        "border-gray-200/60 bg-white text-gray-800 dark:border-white/10 dark:bg-[#1A1B26] dark:text-white",
    },
    success: {
      icon: "check_circle",
      classes:
        "border-green-300 bg-green-50 text-green-900 dark:border-green-500/40 dark:bg-green-500/20 dark:text-green-100",
    },
    error: {
      icon: "error",
      classes:
        "border-red-300 bg-red-50 text-red-900 dark:border-red-500/40 dark:bg-red-500/20 dark:text-red-100",
    },
  };

  const config = options[type] || options.info;

  const toast = document.createElement("div");
  toast.className = `flex items-start gap-3 rounded-lg border px-4 py-3 shadow-2xl backdrop-blur transition-all duration-300 ${config.classes}`;
  toast.style.opacity = "0";
  toast.style.transform = "translateY(8px)";
  toast.innerHTML = `
    <span class="material-symbols-outlined mt-0.5 flex-shrink-0">${config.icon}</span>
    <p class="text-sm leading-relaxed">${message}</p>
  `;

  container.appendChild(toast);

  requestAnimationFrame(() => {
    toast.style.opacity = "1";
    toast.style.transform = "translateY(0)";
  });

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(12px)";
    setTimeout(() => {
      toast.remove();
    }, 300);
  }, 4000);
}
