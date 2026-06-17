const form = document.getElementById("brief-form");
const statusPill = document.getElementById("status-pill");
const latestSummary = document.getElementById("latest-summary");
const resultTime = document.getElementById("result-time");
const messageLog = document.getElementById("message-log");
const generateBtn = document.getElementById("generate-btn");
const refreshBtn = document.getElementById("refresh-btn");
const saveResourcesBtn = document.getElementById("save-resources-btn");
const refreshResourcesBtn = document.getElementById("refresh-resources-btn");
const resourceList = document.getElementById("resource-list");
const resourcesCount = document.getElementById("resources-count");
const txtLink = document.getElementById("txt-link");
const cardLink = document.getElementById("card-link");
const fullLink = document.getElementById("full-link");

function setLog(message) {
  messageLog.textContent = message;
}

function setLinks(result) {
  const links = [
    [txtLink, result?.txt_url, "Text"],
    [cardLink, result?.card_pdf_url, "Card PDF"],
    [fullLink, result?.full_pdf_url, "Full PDF"],
  ];

  links.forEach(([el, href, label]) => {
    if (href) {
      el.href = href;
      el.textContent = label;
      el.classList.remove("disabled");
      el.removeAttribute("aria-disabled");
    } else {
      el.href = "#";
      el.classList.add("disabled");
      el.setAttribute("aria-disabled", "true");
    }
  });
}

function renderResources(resources) {
  if (!resources || resources.length === 0) {
    resourcesCount.textContent = "No resources loaded yet.";
    resourceList.innerHTML = '<div class="resource-empty">Add your 777 flight manual, system notes, and any other reference files here.</div>';
    return;
  }

  resourcesCount.textContent = `${resources.length} resource file${resources.length === 1 ? "" : "s"} available.`;
  resourceList.innerHTML = resources
    .map((item) => {
      const sizeKb = item.size ? `${Math.max(1, Math.round(Number(item.size) / 1024))} KB` : "";
      const meta = [item.display_name || item.name, item.modified, sizeKb].filter(Boolean).join(" • ");
      return `
        <div class="resource-item">
          <div class="meta">
            <div class="name">${item.display_name || item.name}</div>
            <div class="sub">${meta}</div>
          </div>
          <a class="result-link" href="${item.url}" target="_blank" rel="noopener">Open</a>
        </div>
      `;
    })
    .join("");
}

function summarize(result) {
  if (!result) {
    latestSummary.textContent = "No brief has been generated yet.";
    resultTime.textContent = "No generation yet.";
    setLinks(null);
    return;
  }

  latestSummary.textContent = [
    result.source_pdf_name || "Latest brief",
    result.created_at ? `Created ${result.created_at}` : null,
  ]
    .filter(Boolean)
    .join(" • ");
  resultTime.textContent = result.created_at ? `Created ${result.created_at}` : "Latest result loaded.";
  setLinks(result);
}

async function loadHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    if (data.ok) {
      statusPill.textContent = "Online";
      statusPill.style.background = "#e4f3ee";
      statusPill.style.color = "#12805c";
      return;
    }
    throw new Error(data.error || "Offline");
  } catch (error) {
    statusPill.textContent = "Offline";
    statusPill.style.background = "#f7e7e7";
    statusPill.style.color = "#9c2f2f";
  }
}

async function loadLatest() {
  try {
    const res = await fetch("/api/latest");
    const data = await res.json();
    if (data.ok) {
      summarize(data.latest);
      setLog(data.latest ? `Loaded latest brief: ${data.latest.source_pdf_name || "unknown file"}` : "No prior result found.");
      return;
    }
    setLog(data.error || "No saved result found.");
  } catch (error) {
    setLog(`Could not load the latest result. ${error.message}`);
  }
}

async function loadResources() {
  try {
    const res = await fetch("/api/resources");
    const data = await res.json();
    if (data.ok) {
      renderResources(data.resources || []);
      return;
    }
    renderResources([]);
    resourcesCount.textContent = data.error || "Could not load resources.";
  } catch (error) {
    renderResources([]);
    resourcesCount.textContent = `Could not load resources. ${error.message}`;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData();
  const flightPlan = document.getElementById("pdf").files[0];
  if (!flightPlan) {
    setLog("Choose the flight plan PDF first.");
    return;
  }

  data.append("pdf", flightPlan);
  const tripKit = document.getElementById("trip_kit").files[0];
  const pairing = document.getElementById("pairing").files[0];
  const flyingId = document.getElementById("flying_id").value.trim();
  const pickupTime = document.getElementById("pickup_time").value.trim();
  const reportTime = document.getElementById("report_time").value.trim();

  if (tripKit) data.append("trip_kit", tripKit);
  if (pairing) data.append("pairing", pairing);
  if (flyingId) data.append("flying_id", flyingId);
  if (pickupTime) data.append("pickup_time", pickupTime);
  if (reportTime) data.append("report_time", reportTime);

  generateBtn.disabled = true;
  generateBtn.textContent = "Working...";
  setLog("Generating brief...");

  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      body: data,
    });
    const payload = await response.json();
    if (!payload.ok) {
      throw new Error(payload.error || "Generation failed");
    }
    summarize(payload.result);
    setLog(
      [
        payload.message || "Brief generated.",
        payload.result?.full_pdf_url || "",
      ]
        .filter(Boolean)
        .join("\n"),
    );
  } catch (error) {
    setLog(`Generation failed.\n${error.message}`);
  } finally {
    generateBtn.disabled = false;
    generateBtn.textContent = "Generate brief";
  }
});

refreshBtn.addEventListener("click", loadLatest);
refreshResourcesBtn.addEventListener("click", loadResources);

saveResourcesBtn.addEventListener("click", async () => {
  const picker = document.getElementById("resources");
  const files = picker.files;
  if (!files || files.length === 0) {
    setLog("Choose one or more resource files first.");
    return;
  }

  const payload = new FormData();
  Array.from(files).forEach((file) => payload.append("resources", file));

  saveResourcesBtn.disabled = true;
  saveResourcesBtn.textContent = "Saving...";
  setLog("Saving resources...");

  try {
    const response = await fetch("/api/resources", {
      method: "POST",
      body: payload,
    });
    const data = await response.json();
    if (!data.ok) {
      throw new Error(data.error || "Could not save resources");
    }
    setLog(data.message || "Resources saved.");
    picker.value = "";
    await loadResources();
  } catch (error) {
    setLog(`Resource upload failed.\n${error.message}`);
  } finally {
    saveResourcesBtn.disabled = false;
    saveResourcesBtn.textContent = "Save resources";
  }
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

loadHealth();
loadLatest();
loadResources();
