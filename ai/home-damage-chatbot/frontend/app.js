/* Zeo Energy service chatbot — front-end for the 4-tab portal:
     Chat tab     -> POST /api/chat
     Emails tab   -> GET  /api/emails, GET /api/emails/{id}
     Mock CRM tab -> GET  /api/crm/cases, GET /api/crm/cases/{id}, POST /api/crm/cases/{id}/status
     Database tab -> GET  /api/customers, POST /api/lookup
*/
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const newId = () =>
    (crypto.randomUUID && crypto.randomUUID()) ||
    "s-" + Math.abs(Date.now() ^ (Math.random() * 1e9 | 0)).toString(36);

  const state = {
    tab: "chat",
    mode: "lookup",          // matches the design default
    sessionId: newId(),
    pendingAttachments: [],
    emails: [],              // cached list (newest first)
    crmCases: [],            // cached CRM cases
    selectedEmailId: null,
    selectedCaseId: null,
    busy: false,
    queueTimer: null,
    lastQueuePos: null,
  };

  // ---------------------------------------------------------------- styling
  const roleFor = (kind) => (kind === "safety" ? "safety" : kind === "system" ? "system" : "bot");

  // ---------------------------------------------------------------- helpers
  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(path + " -> " + r.status);
    return r.json();
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  function fmtFull(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString([], {
      month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
    });
  }

  // ---------------------------------------------------------------- global photo modal
  window.openPhotoModal = (src, title) => {
    $("photoLightboxImg").src = src;
    $("photoLightboxTitle").textContent = title || "Photo Preview";
    $("photoLightboxModal").style.display = "flex";
  };

  // ---------------------------------------------------------------- tabs
  function setTab(tab) {
    state.tab = tab;
    $("chatTab").hidden = tab !== "chat";
    $("emailsTab").hidden = tab !== "emails";
    $("crmTab").hidden = tab !== "crm";
    $("databaseTab").hidden = tab !== "database";
    $("modeWrap").style.visibility = tab === "chat" ? "visible" : "hidden";
    
    for (const [id, t] of [
      ["tabChat", "chat"],
      ["tabEmails", "emails"],
      ["tabCrm", "crm"],
      ["tabDatabase", "database"],
    ]) {
      const el = $(id);
      if (el) el.classList.toggle("on", t === tab);
    }

    if (tab === "emails") loadEmails();
    if (tab === "crm") loadCrmCases();
    if (tab === "database") loadCustomers();
  }

  // ---------------------------------------------------------------- mode
  function applyModeVisuals() {
    const lookup = state.mode === "lookup";
    $("modeToggle").classList.toggle("lookup", lookup);
    $("modeToggle").setAttribute("aria-checked", String(lookup));
    $("optStandard").classList.toggle("on", !lookup);
    $("optLookup").classList.toggle("on", lookup);
    const pill = $("modePill");
    pill.textContent = lookup ? "Account Lookup" : "Standard";
    pill.classList.toggle("standard", !lookup);
  }

  function toggleMode() {
    state.mode = state.mode === "lookup" ? "standard" : "lookup";
    applyModeVisuals();
    restartChat();
  }

  function addBubble(text, role) {
    const el = document.createElement("div");
    el.className = "chat-bubble " + role;
    el.textContent = text;
    $("chatLog").appendChild(el);
    $("chatLog").scrollTop = $("chatLog").scrollHeight;
    return el;
  }

  function renderQuickReplies(replies) {
    const box = $("quickReplies");
    box.innerHTML = "";
    (replies || []).forEach((r) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = r;
      chip.onclick = () => sendMessage(r);
      box.appendChild(chip);
    });
  }

  function renderAttachments() {
    const row = $("attachmentsRow"), list = $("attachmentList");
    if (!state.pendingAttachments.length) { row.hidden = true; list.innerHTML = ""; return; }
    row.hidden = false;
    list.innerHTML = "";
    state.pendingAttachments.forEach((f) => {
      const c = document.createElement("span");
      c.className = "attachment-chip";
      c.textContent = f;
      list.appendChild(c);
    });
  }

  // ---------------------------------------------------------------- queue management
  async function refreshQueueStats() {
    try {
      const stats = await api("/api/queue/stats");
      $("activeUserCount").textContent = stats.active_users;
      $("maxActiveCount").textContent = stats.max_active_users;
      $("queuedUserCount").textContent = stats.queued_users;
      $("queueDot").classList.toggle("busy", stats.active_users >= stats.max_active_users);
    } catch (e) { /* ignore */ }
  }

  function startQueuePolling() {
    if (state.queueTimer) return;
    $("messageInput").disabled = true;
    $("sendBtn").disabled = true;
    state.queueTimer = setInterval(async () => {
      try {
        const st = await api("/api/queue/status?session_id=" + encodeURIComponent(state.sessionId));
        refreshQueueStats();
        if (st.allowed) {
          clearInterval(state.queueTimer);
          state.queueTimer = null;
          state.lastQueuePos = null;
          $("messageInput").disabled = false;
          $("sendBtn").disabled = false;
          addBubble("Connected! An active assistant slot is now open.", "system");
          const resp = await api("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: state.sessionId, mode: state.mode, message: "" }),
          });
          applyTurn(resp);
        } else if (st.position > 0 && st.position !== state.lastQueuePos) {
          state.lastQueuePos = st.position;
          addBubble("Queue update: You are now at position #" + st.position + " in line.", "system");
        }
      } catch (e) {
        /* retry on next interval */
      }
    }, 2000);
  }

  function applyTurn(resp) {
    if (resp.queued) {
      state.lastQueuePos = resp.queue_position;
      (resp.messages || []).forEach((m) => addBubble(m.text, roleFor(m.kind)));
      startQueuePolling();
      return;
    }

    if (state.queueTimer) {
      clearInterval(state.queueTimer);
      state.queueTimer = null;
    }
    $("messageInput").disabled = false;
    $("sendBtn").disabled = false;

    (resp.messages || []).forEach((m) => addBubble(m.text, roleFor(m.kind)));
    if (resp.state !== "safety") {
      renderQuickReplies(resp.quick_replies);
      $("uploadBtn").style.display = resp.allow_upload ? "inline-flex" : "none";
    }
    if (resp.email_id) {
      refreshEmailCount();
      refreshCrmCount();
    }
    refreshQueueStats();

    if (resp.await_step === "damage_pointer") {
      if (resp.latitude != null && resp.longitude != null) {
        openMapModal(resp);
        const btn = document.createElement("button");
        btn.className = "btn-map-primary";
        btn.style.margin = "8px 0";
        btn.textContent = "Drop Pointer on Map";
        btn.onclick = () => openMapModal(resp);
        $("chatLog").appendChild(btn);
        $("chatLog").scrollTop = $("chatLog").scrollHeight;
      } else {
        sendMessage("fallback");
      }
    }
  }

  async function restartChat() {
    state.sessionId = newId();
    state.pendingAttachments = [];
    renderAttachments();
    $("chatLog").innerHTML = "";
    renderQuickReplies([]);
    try {
      const resp = await api("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, mode: state.mode, message: "" }),
      });
      applyTurn(resp);
    } catch (e) {
      addBubble("Couldn't reach the server. Is the backend running?", "safety");
    }
  }

  async function sendMessage(text) {
    if (state.busy) return;
    const msg = (text || "").trim();
    const atts = state.pendingAttachments.slice();
    if (!msg && !atts.length) return;

    if (msg) addBubble(msg, "user");
    if (atts.length) addBubble("Sent " + atts.length + " photo(s): " + atts.join(", "), "user");
    $("messageInput").value = "";
    state.pendingAttachments = [];
    renderAttachments();
    renderQuickReplies([]);
    state.busy = true; $("sendBtn").disabled = true;

    try {
      const resp = await api("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, mode: state.mode, message: msg, attachments: atts }),
      });
      applyTurn(resp);
    } catch (e) {
      addBubble("Couldn't reach the server. Is the backend running?", "safety");
    } finally {
      state.busy = false; $("sendBtn").disabled = false; $("messageInput").focus();
    }
  }

  // ---------------------------------------------------------------- emails
  async function refreshEmailCount() {
    try {
      state.emails = await api("/api/emails");
      $("emailCountTab").textContent = state.emails.length;
      $("inboxCount").textContent = state.emails.length;
    } catch (e) { /* ignore */ }
  }

  async function loadEmails() {
    await refreshEmailCount();
    renderInbox();
    showInboxView();
  }

  function renderInbox() {
    const list = $("inboxList");
    list.innerHTML = "";
    $("inboxRange").textContent = state.emails.length
      ? "1–" + state.emails.length + " of " + state.emails.length
      : "0 of 0";
    if (!state.emails.length) {
      const empty = document.createElement("div");
      empty.style.cssText = "padding:40px 16px;text-align:center;color:#64748b;font-size:14px;font-weight:500;";
      empty.textContent = "No emails yet — complete a request in the Chatbot tab.";
      list.appendChild(empty);
      return;
    }
    state.emails.forEach((e) => {
      const row = document.createElement("div");
      row.className = "email-row";
      row.onclick = () => openMessage(e.id);

      const star = document.createElement("span");
      star.className = "email-row-star";
      star.textContent = "☆";

      const sender = document.createElement("span");
      sender.className = "email-row-sender";
      sender.textContent = e.customer_name || "Unverified Requester";

      const badge = document.createElement("span");
      badge.className = "email-row-badge";
      badge.textContent = e.tier_label;
      badge.style.backgroundColor = e.tier_color;

      const subj = document.createElement("span");
      subj.className = "email-row-subj";
      subj.textContent = e.subject;

      const snip = document.createElement("span");
      snip.className = "email-row-snip";
      snip.textContent = " — " + (e.summary || "");

      const time = document.createElement("span");
      time.className = "email-row-time";
      time.textContent = fmtTime(e.created_at);

      row.appendChild(star);
      row.appendChild(sender);
      row.appendChild(badge);
      row.appendChild(subj);
      row.appendChild(snip);
      row.appendChild(time);
      list.appendChild(row);
    });
  }

  async function openMessage(id) {
    state.selectedEmailId = id;
    try {
      const email = await api("/api/emails/" + id);
      showEmailDetail(email);
    } catch (e) {
      alert("Could not load email: " + e.message);
    }
  }

  function showEmailDetail(email) {
    $("inboxView").hidden = true;
    $("messageView").hidden = false;
    const idx = state.emails.findIndex((e) => e.id === email.id);
    $("msgPos").textContent = (idx >= 0 ? idx + 1 : 1) + " of " + state.emails.length;

    $("messageBody").innerHTML = `
      <div style="border-bottom:1px solid #e2e8f0;padding-bottom:14px;margin-bottom:16px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
          <span style="font-size:18px;font-weight:700;color:#0f172a;">${email.subject}</span>
          <span class="email-row-badge" style="background-color:${email.tier_color};">${email.tier_label}</span>
        </div>
        <div style="font-size:12px;color:#64748b;">
          <strong>From:</strong> Zeo Service Chatbot &lt;service-bot@zeoenergy.com&gt; &middot; 
          <strong>To:</strong> ${email.to} &middot; 
          <strong>Date:</strong> ${fmtFull(email.created_at)}
        </div>
      </div>
      <div>${email.html}</div>
    `;
  }

  function showInboxView() {
    $("inboxView").hidden = false;
    $("messageView").hidden = true;
  }

  // ---------------------------------------------------------------- Mock CRM Cases
  async function refreshCrmCount() {
    try {
      state.crmCases = await api("/api/crm/cases");
      $("crmCountTab").textContent = state.crmCases.length;
    } catch (e) { /* ignore */ }
  }

  async function loadCrmCases() {
    await refreshCrmCount();
    renderCrmCases();
    $("crmListView").style.display = "block";
    $("crmDetailView").style.display = "none";
  }

  function renderCrmCases() {
    const tbody = $("crmCaseList");
    tbody.innerHTML = "";
    const filter = $("crmFilterStatus").value;
    const query = ($("crmSearch").value || "").toLowerCase().trim();

    const filtered = state.crmCases.filter((c) => {
      if (filter !== "all" && c.status !== filter) return false;
      if (query) {
        const text = `${c.case_id} ${c.customer_name} ${c.account_number} ${c.issue_label}`.toLowerCase();
        if (!text.includes(query)) return false;
      }
      return true;
    });

    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:32px; color:#64748b;">No matching CRM cases found.</td></tr>`;
      return;
    }

    filtered.forEach((c) => {
      const tr = document.createElement("tr");
      tr.className = "crm-row";
      tr.onclick = () => openCrmCase(c.case_id);

      const priorityBadgeClass = c.tier_label === "HIGH PRIORITY" ? "badge-priority-high" :
                                 c.tier_label === "Elevated" ? "badge-priority-elevated" : "badge-priority-standard";

      const statusBadgeClass = c.status === "resolved" ? "badge-status-resolved" :
                               c.status === "assigned" ? "badge-status-assigned" :
                               c.status === "in_review" ? "badge-status-review" : "badge-status-pending";

      const statusLabel = c.status === "pending_disposition" ? "Pending Disposition" :
                          c.status === "in_review" ? "In Review" :
                          c.status === "assigned" ? "Assigned" : "Resolved";

      tr.innerHTML = `
        <td style="font-family:monospace; font-weight:700; color:#2563eb;">${c.case_id}</td>
        <td>
          <div style="font-weight:600;">${c.customer_name}</div>
          <div style="font-size:11px; color:#64748b; font-family:monospace;">${c.account_number}</div>
        </td>
        <td>${c.issue_label}</td>
        <td><span class="crm-badge ${priorityBadgeClass}">${c.tier_label} (${c.urgency}/10)</span></td>
        <td><span class="crm-badge ${statusBadgeClass}">${statusLabel}</span></td>
        <td style="font-size:12px; color:#475569;">${c.routed_to}</td>
        <td style="font-size:12px; color:#64748b;">${fmtTime(c.created_at)}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  async function openCrmCase(caseId) {
    state.selectedCaseId = caseId;
    try {
      const c = await api("/api/crm/cases/" + caseId);
      $("crmDetCustName").textContent = c.customer_name;
      $("crmDetAcctNum").textContent = c.account_number;
      $("crmDetAddress").textContent = c.service_address || "Address not on file";
      $("crmDetContact").textContent = c.contact || "None provided";
      $("crmStatusSelect").value = c.status || "pending_disposition";

      $("chatterBody").innerHTML = c.chatter_note;

      // Also render linked email if available
      if (c.email_id) {
        try {
          const em = await api("/api/emails/" + c.email_id);
          $("caseGmailBody").innerHTML = `
            <div style="margin-bottom:12px; font-size:13px; color:#334155;">
              <strong>Subject:</strong> ${em.subject}<br>
              <strong>To:</strong> ${em.to} &middot; <strong>Date:</strong> ${fmtFull(em.created_at)}
            </div>
            ${em.html}
          `;
        } catch(err) {
          $("caseGmailBody").innerHTML = "<p>No linked email found.</p>";
        }
      } else {
        $("caseGmailBody").innerHTML = "<p>No handoff email linked to this case.</p>";
      }

      $("crmListView").style.display = "none";
      $("crmDetailView").style.display = "block";
      switchCrmSubtab("chatter");
    } catch (e) {
      alert("Could not load CRM case: " + e.message);
    }
  }

  function switchCrmSubtab(subtab) {
    $("subtabChatter").classList.toggle("active", subtab === "chatter");
    $("subtabGmail").classList.toggle("active", subtab === "gmail");
    $("chatterView").style.display = subtab === "chatter" ? "block" : "none";
    $("caseGmailView").style.display = subtab === "gmail" ? "block" : "none";
  }

  // ---------------------------------------------------------------- database
  async function loadCustomers() {
    try {
      const list = await api("/api/customers");
      $("customerCount").textContent = list.length;
      const table = $("customerTable");
      table.innerHTML = "";
      list.forEach((c) => {
        const row = document.createElement("div");
        row.className = "db-table-row";
        row.innerHTML = `
          <div><code>${c.account_number}</code></div>
          <div><strong>${c.name}</strong></div>
          <div>${c.service_address}</div>
          <div>${c.email}</div>
          <div>${c.system_size}</div>
        `;
        table.appendChild(row);
      });
    } catch (e) { /* ignore */ }
  }

  // ---------------------------------------------------------------- Map Pointer Modal
  let mapModalSubstep = "verify";
  let mapModalIsTest = false;
  let currentPinX = null;
  let currentPinY = null;
  let currentMapResp = null;

  function openMapModal(resp, isTest = false) {
    mapModalIsTest = isTest;
    currentMapResp = resp;
    mapModalSubstep = "verify";
    currentPinX = null;
    currentPinY = null;

    $("mapModalTitle").textContent = "Verify Your Home";
    $("mapModalInstructions").textContent = "Please verify: Is this your home?";
    $("mapModalAddress").textContent = (resp && resp.account_address) || "100 Solar Way, Tampa, FL 33601";
    $("mapModalVerifyFooter").style.display = "flex";
    $("mapModalPointerFooter").style.display = "none";
    $("mapPin").style.display = "none";
    $("mapModalConfirm").disabled = true;

    $("mapModal").style.display = "flex";
    loadMapImage(resp && resp.latitude, resp && resp.longitude);
  }

  function loadMapImage(lat, lon) {
    const canvas = $("mapCanvas");
    const ctx = canvas.getContext("2d");
    const loading = $("mapLoading");
    const error = $("mapError");

    loading.style.display = "flex";
    error.style.display = "none";

    const img = new Image();
    img.crossOrigin = "anonymous";
    img.src = "/assets/suburban_house.png";

    img.onload = () => {
      loading.style.display = "none";
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    };

    img.onerror = () => {
      loading.style.display = "none";
      error.style.display = "block";
      ctx.fillStyle = "#cbd5e1";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    };
  }

  function drawPinOnCanvas(ctx, x, y) {
    ctx.save();
    ctx.font = "32px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.shadowColor = "rgba(0, 0, 0, 0.4)";
    ctx.shadowBlur = 6;
    ctx.shadowOffsetY = 3;
    ctx.fillText("📍", x, y);
    ctx.restore();
  }

  function switchToPointerStep() {
    mapModalSubstep = "pointer";
    $("mapModalTitle").textContent = "Mark Damage Location";
    $("mapModalInstructions").textContent = "Click on the satellite view below where the damage or leak is located.";
    $("mapModalVerifyFooter").style.display = "none";
    $("mapModalPointerFooter").style.display = "flex";
  }

  // ---------------------------------------------------------------- init
  function init() {
    $("modeToggle").onclick = toggleMode;
    $("modeToggle").onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleMode(); }
    };

    $("tabChat").onclick = () => setTab("chat");
    $("tabEmails").onclick = () => setTab("emails");
    $("tabCrm").onclick = () => setTab("crm");
    $("tabDatabase").onclick = () => setTab("database");

    $("resetChatBtn").onclick = restartChat;

    // Release notes modal
    $("btnReleaseNotes").onclick = () => { $("releaseNotesModal").style.display = "flex"; };
    $("closeReleaseNotesBtn").onclick = () => { $("releaseNotesModal").style.display = "none"; };
    $("closePhotoLightboxBtn").onclick = () => { $("photoLightboxModal").style.display = "none"; };

    // Queue simulator actions
    $("btnSimQueueFill").onclick = async () => {
      const res = await api("/api/queue/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "fill" }),
      });
      refreshQueueStats();
      addBubble("Simulator: Active slots filled! The next incoming user will enter the waiting queue.", "system");
    };

    $("btnSimQueueFree").onclick = async () => {
      const res = await api("/api/queue/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "free" }),
      });
      refreshQueueStats();
      addBubble("Simulator: Freed an active slot. Promoted head of waiting queue.", "system");
    };

    $("btnSimQueueReset").onclick = async () => {
      const res = await api("/api/queue/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "reset" }),
      });
      refreshQueueStats();
      addBubble("Simulator: Concurrency queue state reset.", "system");
    };

    // CRM cases controls
    $("crmRefreshBtn").onclick = loadCrmCases;
    $("crmSearch").oninput = renderCrmCases;
    $("crmFilterStatus").onchange = renderCrmCases;
    $("crmBackBtn").onclick = () => {
      $("crmListView").style.display = "block";
      $("crmDetailView").style.display = "none";
    };

    $("subtabChatter").onclick = () => switchCrmSubtab("chatter");
    $("subtabGmail").onclick = () => switchCrmSubtab("gmail");

    $("crmSaveStatusBtn").onclick = async () => {
      if (!state.selectedCaseId) return;
      const newStat = $("crmStatusSelect").value;
      try {
        await api("/api/crm/cases/" + state.selectedCaseId + "/status", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: newStat }),
        });
        alert("Case status updated to: " + newStat);
        refreshCrmCount();
      } catch (e) {
        alert("Could not update status: " + e.message);
      }
    };

    // Chat form
    $("chatForm").onsubmit = (e) => {
      e.preventDefault();
      sendMessage($("messageInput").value);
    };

    $("uploadBtn").onclick = () => $("fileInput").click();
    $("fileInput").onchange = async (e) => {
      const files = Array.from(e.target.files || []);
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        try {
          const res = await fetch("/api/upload", { method: "POST", body: fd });
          if (!res.ok) throw new Error("Upload rejected (must be PNG/JPEG/WEBP under 5MB)");
          const data = await res.json();
          state.pendingAttachments.push(data.filename);
        } catch (err) {
          alert("Upload failed: " + err.message);
        }
      }
      renderAttachments();
      $("fileInput").value = "";
    };

    // Email navigation
    $("msgBack").onclick = showInboxView;

    // Database lookup demo
    $("runLookup").onclick = async () => {
      const name = $("dbName").value.trim();
      const addr = $("dbAddr").value.trim();
      const email = $("dbEmail").value.trim();
      const box = $("dbResult");
      box.hidden = false;
      try {
        const r = await api("/api/lookup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, address: addr, email }),
        });
        if (r.matched) {
          box.style.background = "#ecfdf5"; box.style.borderColor = "#a7f3d0"; box.style.color = "#065f46";
          box.innerHTML = `<strong>Matched account:</strong> ${r.account.account_number} &middot; ${r.account.name} &middot; ${r.account.service_address}`;
        } else {
          box.style.background = "#fff8e6"; box.style.borderColor = "#f0dca0"; box.style.color = "#854d0e";
          box.textContent = "No confident match found. (Single-field queries are also rejected).";
        }
      } catch (e) {
        box.style.background = "#fef2f2"; box.style.borderColor = "#fecaca"; box.style.color = "#991b1b";
        box.textContent = "Lookup request failed: " + e.message;
      }
    };

    // Map pointer modal
    $("btnVerifyYes").onclick = () => { switchToPointerStep(); };
    $("btnVerifyNo").onclick = () => {
      $("mapModal").style.display = "none";
      if (!mapModalIsTest) sendMessage("fallback");
    };

    $("mapImageContainer").onclick = (e) => {
      if (mapModalSubstep !== "pointer") return;
      const canvas = $("mapCanvas");
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      
      const pin = $("mapPin");
      pin.style.left = x + "px";
      pin.style.top = y + "px";
      pin.style.display = "block";

      currentPinX = x * (canvas.width / rect.width);
      currentPinY = y * (canvas.height / rect.height);
      $("mapModalConfirm").disabled = false;
    };

    $("mapModalConfirm").onclick = async () => {
      $("mapModalConfirm").disabled = true;
      $("mapModalConfirm").textContent = "Uploading…";

      const canvas = $("mapCanvas");
      const ctx = canvas.getContext("2d");
      drawPinOnCanvas(ctx, currentPinX, currentPinY);

      canvas.toBlob(async (blob) => {
        const formData = new FormData();
        const filename = "damage_pointer_" + Date.now() + ".png";
        formData.append("file", blob, filename);

        try {
          const res = await fetch("/api/upload", { method: "POST", body: formData });
          if (!res.ok) throw new Error("Upload failed");
          const data = await res.json();
          $("mapModal").style.display = "none";
          $("mapModalConfirm").textContent = "Confirm Location";
          if (!mapModalIsTest) await sendMessage(data.filename);
        } catch (err) {
          alert("Could not upload map pointer: " + err.message);
          $("mapModalConfirm").disabled = false;
          $("mapModalConfirm").textContent = "Confirm Location";
        }
      }, "image/png");
    };

    const closeMap = () => { $("mapModal").style.display = "none"; };
    $("mapModalClose").onclick = closeMap;
    $("mapModalCancel").onclick = closeMap;

    applyModeVisuals();
    setTab("chat");
    refreshEmailCount();
    refreshCrmCount();
    refreshQueueStats();
    restartChat();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
