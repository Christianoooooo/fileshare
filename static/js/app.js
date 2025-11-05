const state = {
  files: [],
  capacity: 0,
  totalSize: 0,
  queue: [],
  preferences: {
    hideMediaDefault: false,
    copyUrlMode: "view",
  },
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
  const prefs = window.userPreferences || {};
  state.preferences.hideMediaDefault = Boolean(prefs.hide_media_default);
  state.preferences.copyUrlMode = prefs.copy_url_mode || "view";
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
    if (data.preferences) {
      state.preferences.hideMediaDefault = Boolean(
        data.preferences.hide_media_default,
      );
      if (data.preferences.copy_url_mode) {
        state.preferences.copyUrlMode = data.preferences.copy_url_mode;
      }
      window.userPreferences = {
        hide_media_default: state.preferences.hideMediaDefault,
        copy_url_mode: state.preferences.copyUrlMode,
      };
      if (window.currentUser) {
        window.currentUser.copy_url_mode = state.preferences.copyUrlMode;
      }
    }
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
  const tableBody = document.getElementById("file-table-body");
  const emptyState = document.getElementById("empty-state");
  if (!tableBody) {
    return;
  }

  tableBody.innerHTML = "";

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
    const row = document.createElement("tr");
    row.className = "file-row border-b border-gray-100 last:border-b-0 dark:border-white/5";
    row.dataset.fileId = file.id;

    const previewCell = document.createElement("td");
    previewCell.className = "px-4 py-3 align-middle";
    previewCell.appendChild(buildPreviewElement(file));
    row.appendChild(previewCell);

    const nameCell = document.createElement("td");
    nameCell.className = "px-4 py-3 align-middle";
    const nameWrapper = document.createElement("div");
    nameWrapper.className = "flex flex-col gap-2";
    const title = document.createElement("p");
    title.className = "truncate text-sm font-semibold text-gray-900 dark:text-white";
    title.textContent = file.name;
    nameWrapper.appendChild(title);
    const typeInfo = document.createElement("p");
    typeInfo.className = "text-xs text-gray-500 dark:text-[#92a9c9]";
    typeInfo.textContent = file.content_type || "Unbekannter Typ";
    nameWrapper.appendChild(typeInfo);

    const actions = document.createElement("div");
    actions.className = "flex flex-wrap gap-2";
    const canManage = file.can_manage !== false;
    const actionDefinitions = [
      { action: "open", label: "Open", icon: "open_in_new" },
      { action: "download", label: "Download", icon: "download" },
      { action: "copy", label: "Copy Link", icon: "link" },
      { action: "hide", label: "Hide", icon: "visibility_off", requireManage: true },
      { action: "custom", label: "Custom URL", icon: "edit_square", requireManage: true },
      { action: "delete", label: "Delete", icon: "delete", requireManage: true, danger: true },
    ];
    actionDefinitions.forEach((definition) => {
      const button = createActionButton(definition);
      if (definition.requireManage && !canManage) {
        button.disabled = true;
        button.classList.add("opacity-50", "cursor-not-allowed");
      }
      if (definition.action === "hide" && (!file.is_public || !canManage)) {
        button.disabled = true;
        button.classList.add("opacity-50", "cursor-not-allowed");
      }
      actions.appendChild(button);
    });
    nameWrapper.appendChild(actions);
    nameCell.appendChild(nameWrapper);
    row.appendChild(nameCell);

    const sizeCell = document.createElement("td");
    sizeCell.className = "px-4 py-3 text-sm text-gray-500 dark:text-[#92a9c9]";
    sizeCell.textContent = formatBytes(file.size);
    row.appendChild(sizeCell);

    const publicCell = document.createElement("td");
    publicCell.className = "px-4 py-3";
    publicCell.appendChild(createPublicBadge(Boolean(file.is_public)));
    row.appendChild(publicCell);

    const ownerCell = document.createElement("td");
    ownerCell.className = "px-4 py-3 text-sm text-gray-500 dark:text-[#92a9c9]";
    ownerCell.textContent = file.owner?.username || window.currentUser?.username || "";
    row.appendChild(ownerCell);

    const dateCell = document.createElement("td");
    dateCell.className = "px-4 py-3 text-sm text-gray-500 dark:text-[#92a9c9]";
    dateCell.textContent = formatDate(file.uploaded_at);
    row.appendChild(dateCell);

    tableBody.appendChild(row);

    actions.querySelectorAll(".action-button").forEach((button) => {
      const action = button.dataset.action;
      button.addEventListener("click", () => {
        if (button.disabled) {
          return;
        }
        handleFileAction(file.id, action);
      });
    });
  });
}

function buildPreviewElement(file) {
  const wrapper = document.createElement("div");
  wrapper.className = "flex h-14 w-20 items-center justify-center overflow-hidden rounded-lg border border-gray-200/60 bg-white/60 text-primary dark:border-white/10 dark:bg-[#1A1B26]/60";

  const hideMedia = Boolean(state.preferences.hideMediaDefault);
  if (!hideMedia && file.preview_type === "image") {
    const image = document.createElement("img");
    image.src = `/files/${file.id}/raw`;
    image.alt = file.name;
    image.className = "h-full w-full object-cover";
    wrapper.appendChild(image);
    return wrapper;
  }

  const icon = document.createElement("span");
  icon.className = "material-symbols-outlined text-2xl";
  icon.textContent = iconForFile(file);
  wrapper.appendChild(icon);
  return wrapper;
}

function createActionButton({ action, label, icon, danger = false }) {
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.action = action;
  const baseClasses = [
    "action-button",
    "inline-flex",
    "items-center",
    "gap-2",
    "rounded-lg",
    "border",
    "px-3",
    "py-1.5",
    "text-sm",
    "font-semibold",
    "transition",
  ];
  if (danger) {
    baseClasses.push(
      "border-red-200/60",
      "text-red-600",
      "hover:bg-red-50",
      "dark:border-red-400/40",
      "dark:text-red-300",
      "dark:hover:bg-red-500/20",
    );
  } else {
    baseClasses.push(
      "border-gray-200/60",
      "text-gray-700",
      "hover:bg-gray-100",
      "dark:border-white/10",
      "dark:text-white",
      "dark:hover:bg-white/10",
    );
  }
  button.className = baseClasses.join(" ");

  button.innerHTML = `
    <span class="material-symbols-outlined text-base">${icon}</span>
    <span>${label}</span>
  `;

  return button;
}

function createPublicBadge(isPublic) {
  const badge = document.createElement("span");
  badge.className = "inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs font-semibold";
  if (isPublic) {
    badge.classList.add("bg-green-100", "text-green-700", "dark:bg-green-500/20", "dark:text-green-200");
    badge.textContent = "Ja";
  } else {
    badge.classList.add("bg-gray-100", "text-gray-600", "dark:bg-white/10", "dark:text-[#92a9c9]");
    badge.textContent = "Nein";
  }
  return badge;
}

async function handleFileAction(fileId, action) {
  const file = state.files.find((entry) => entry.id === fileId);
  if (!file) {
    showToast("Datei wurde nicht gefunden.", "error");
    return;
  }

  switch (action) {
    case "open":
      if (file.view_url) {
        window.open(file.view_url, "_blank", "noopener");
      } else {
        showToast("Kein Vorschaulink verfügbar.", "error");
      }
      break;
    case "download":
      if (file.download_url) {
        window.open(file.download_url, "_blank", "noopener");
      } else {
        showToast("Kein Downloadlink verfügbar.", "error");
      }
      break;
    case "copy":
      await copyLinkForFile(fileId);
      break;
    case "hide":
      await hideFileFromPublic(fileId);
      break;
    case "custom":
      await promptCustomUrl(fileId);
      break;
    case "delete":
      await deleteFile(fileId);
      break;
    default:
      break;
  }
}

async function copyLinkForFile(fileId) {
  const file = state.files.find((entry) => entry.id === fileId);
  if (!file) {
    showToast("Datei wurde nicht gefunden.", "error");
    return;
  }
  const mode = state.preferences.copyUrlMode || "view";
  try {
    if (mode === "download") {
      if (!file.download_url) {
        throw new Error("Kein Downloadlink verfügbar.");
      }
      copyToClipboard(file.download_url, { successMessage: "Downloadlink kopiert." });
      return;
    }

    if (mode === "view") {
      if (!file.view_url) {
        throw new Error("Kein Vorschaulink verfügbar.");
      }
      copyToClipboard(file.view_url, { successMessage: "Link kopiert." });
      return;
    }

    const updatedFile = await ensureShareLink(fileId, mode === "raw");
    const shareLink = mode === "raw" ? updatedFile.share_raw_url || updatedFile.share_url : updatedFile.share_url;
    if (!shareLink) {
      throw new Error("Es konnte kein Freigabelink erzeugt werden.");
    }
    copyToClipboard(shareLink, { successMessage: "Freigabelink kopiert." });
  } catch (error) {
    showToast(error.message || "Link konnte nicht kopiert werden.", "error");
  }
}

async function hideFileFromPublic(fileId) {
  const file = state.files.find((entry) => entry.id === fileId);
  if (!file) {
    showToast("Datei wurde nicht gefunden.", "error");
    return;
  }
  try {
    const response = await fetch(`/api/files/${fileId}/share`, { method: "DELETE" });
    if (handleUnauthorizedResponse(response)) {
      return;
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.message || "Der Link konnte nicht versteckt werden.");
    }
    file.share_url = null;
    file.share_raw_url = null;
    file.share_token = null;
    file.is_public = false;
    insertOrUpdateFile(file);
    renderFiles();
    updateStats();
    showToast("Datei ist nun privat.", "success");
  } catch (error) {
    showToast(error.message || "Der Link konnte nicht versteckt werden.", "error");
  }
}

async function promptCustomUrl(fileId) {
  const file = state.files.find((entry) => entry.id === fileId);
  if (!file) {
    showToast("Datei wurde nicht gefunden.", "error");
    return;
  }
  const currentSlug = file.share_token || "";
  const slug = prompt("Benutzerdefinierte URL festlegen", currentSlug);
  if (!slug) {
    return;
  }
  try {
    const response = await fetch(`/api/files/${fileId}/custom-url`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug }),
    });
    if (handleUnauthorizedResponse(response)) {
      return;
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.message || "Die benutzerdefinierte URL konnte nicht gespeichert werden.");
    }
    const updatedFile = data.file;
    insertOrUpdateFile(updatedFile);
    renderFiles();
    updateStats();
    showToast(data.message || "Benutzerdefinierte URL gespeichert.", "success");
  } catch (error) {
    showToast(error.message || "Die benutzerdefinierte URL konnte nicht gespeichert werden.", "error");
  }
}

async function deleteFile(fileId) {
  const file = state.files.find((entry) => entry.id === fileId);
  if (!file) {
    showToast("Datei wurde nicht gefunden.", "error");
    return;
  }
  const confirmed = confirm(`Möchtest du "${file.name}" endgültig löschen?`);
  if (!confirmed) {
    return;
  }
  try {
    const response = await fetch(`/api/files/${fileId}`, { method: "DELETE" });
    if (handleUnauthorizedResponse(response)) {
      return;
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.message || "Die Datei konnte nicht gelöscht werden.");
    }
    state.files = state.files.filter((entry) => entry.id !== fileId);
    renderFiles();
    updateStats();
    showToast("Datei wurde gelöscht.", "success");
  } catch (error) {
    showToast(error.message || "Die Datei konnte nicht gelöscht werden.", "error");
  }
}

async function ensureShareLink(fileId, needsRawLink = false) {
  let file = state.files.find((entry) => entry.id === fileId);
  if (!file) {
    throw new Error("Datei wurde nicht gefunden.");
  }
  if (file.share_url && (!needsRawLink || file.share_raw_url)) {
    return file;
  }
  const response = await fetch(`/api/files/${fileId}/share`, {
    method: "POST",
  });
  if (handleUnauthorizedResponse(response)) {
    throw new Error("Authentifizierung erforderlich.");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.message || "Der Freigabelink konnte nicht erstellt werden.");
  }
  const data = await response.json();
  file.share_url = data.share_url;
  file.share_raw_url = data.share_raw_url || data.share_url;
  file.share_token = data.share_token || file.share_token;
  file.is_public = true;
  insertOrUpdateFile(file);
  renderFiles();
  updateStats();
  return state.files.find((entry) => entry.id === fileId) || file;
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
