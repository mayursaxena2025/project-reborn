(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = (path, options = {}) => fetch(new URL(`/api/${path}`, location.origin), options);
  const state = {
    id: "",
    editToken: "",
    deck: null,
    sceneIndex: 0,
    history: [],
    pending: false,
    savingFact: false,
    presenting: false,
    speaking: false,
    utterance: null,
  };
  function setStatus(message, isError = false) {
    $("uploadStatus").textContent = message;
    $("uploadStatus").classList.toggle("error", isError);
  }

  function setProgress(value, label) {
    $("progressFill").style.width = `${Math.max(0, Math.min(100, value))}%`;
    $("progressLabel").textContent = label;
  }

  function paymentUnlocked() {
    return new URLSearchParams(location.search).get("payment") === "success";
  }

  function updatePaymentUI() {
    const checked = $("paidOption").checked;
    $("paymentRow").classList.toggle("visible", checked);
    $("paymentLink").setAttribute("aria-disabled", String(paymentUnlocked()));
    $("dropzone").classList.toggle("disabled", !paymentUnlocked());
    $("paymentStatus").textContent = paymentUnlocked()
      ? "Payment complete · unlocked"
      : "Complete payment to unlock the builder";
  }

  async function readResponse(response) {
    const text = await response.text();
    let payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      payload = { detail: "The server returned an unreadable response." };
    }
    if (!response.ok) {
      throw new Error(payload.detail || payload.error || "The request could not be completed.");
    }
    return payload;
  }

  function authHeaders() {
    return { "Content-Type": "application/json", "X-Story-Editor-Token": state.editToken };
  }

  function clearSession() {
    if (state.id) {
      sessionStorage.removeItem(`rp-edit:${state.id}`);
      sessionStorage.removeItem(`rp-chat:${state.id}`);
    }
    sessionStorage.removeItem("rp-current-id");
    stopNarration();
    state.id = "";
    state.editToken = "";
    state.deck = null;
    state.history = [];
    state.sceneIndex = 0;
  }

  function beginStudio(result) {
    state.id = result.id;
    state.editToken = result.edit_token;
    state.deck = result.deck;
    state.sceneIndex = 0;
    sessionStorage.setItem(`rp-edit:${state.id}`, state.editToken);
    sessionStorage.setItem("rp-current-id", state.id);
    state.history = [];
    const firstGap = (state.deck.profile?.gaps || [])[0];
    const opening = firstGap
      ? `I’ve mapped the facts from your resume. Let’s make the story more specific. ${firstGap.question}`
      : "I’ve mapped your resume into a story. What would you like people to remember most about your work?";
    state.history.push({ role: "assistant", content: opening });
    sessionStorage.setItem(`rp-chat:${state.id}`, JSON.stringify(state.history));
    $("uploadView").classList.add("hidden");
    $("studioView").classList.remove("hidden");
    $("storyName").textContent = state.deck.name || "Your career story";
    $("anchorName").textContent = state.deck.name || "Career presenter";
    $("shareAddress").value = result.share_url || state.deck.share_url || "";
    renderStudio();
    addChatBubble("assistant", opening);
    $("coachInput").focus();
  }

  function addChatBubble(role, text) {
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${role}`;
    bubble.textContent = text;
    $("chatLog").append(bubble);
    $("chatLog").scrollTop = $("chatLog").scrollHeight;
    return bubble;
  }

  function appendBadge(parent, label, className = "") {
    const badge = document.createElement("span");
    badge.className = `badge ${className}`.trim();
    badge.textContent = label;
    parent.append(badge);
  }

  function sourceName(source) {
    if (source === "user_chat") return ["Added by you", "user"];
    if (source === "ai_inference") return ["AI interpretation", "inference"];
    return ["From resume", "resume"];
  }

  function renderProfile() {
    const profile = state.deck.profile || { facts: [], gaps: [], insights: [] };
    const facts = Array.isArray(profile.facts) ? profile.facts : [];
    const userFacts = facts.filter((fact) => fact.source === "user_chat");
    const resumeFacts = facts.filter((fact) => fact.source === "resume");
    const inferences = facts.filter((fact) => fact.source === "ai_inference");
    const insights = Array.isArray(profile.insights) ? profile.insights : [];
    $("factCount").textContent = facts.length;
    $("addedCount").textContent = userFacts.length;
    $("gapCount").textContent = (profile.gaps || []).length;
    $("factList").replaceChildren();

    const groups = [
      ["Resume facts", resumeFacts],
      ["Added by you", userFacts],
    ];
    for (const [label, group] of groups) {
      if (!group.length) continue;
      const heading = document.createElement("div");
      heading.className = "fact-section";
      heading.textContent = label;
      $("factList").append(heading);
      for (const fact of group.slice(0, 12)) {
        const card = document.createElement("article");
        card.className = "fact-card";
        const [sourceLabel, sourceClass] = sourceName(fact.source);
        appendBadge(card, sourceLabel, sourceClass);
        const text = document.createElement("p");
        text.textContent = fact.text || "";
        card.append(text);
        $("factList").append(card);
      }
      if (group.length > 12) {
        const more = document.createElement("p");
        more.className = "privacy-note";
        more.textContent = `And ${group.length - 12} more details`;
        $("factList").append(more);
      }
    }

    if (inferences.length || insights.length) {
      const heading = document.createElement("div");
      heading.className = "fact-section";
      heading.textContent = "AI interpretations";
      $("factList").append(heading);
      for (const entry of inferences.slice(0, 12)) {
        const card = document.createElement("article");
        card.className = "fact-card";
        appendBadge(card, "AI interpretation", "inference");
        const text = document.createElement("p");
        text.textContent = entry.text || "";
        card.append(text);
        $("factList").append(card);
      }
      for (const insight of insights.slice(0, 5)) {
        const card = document.createElement("article");
        card.className = "fact-card";
        appendBadge(card, "AI interpretation", "inference");
        const text = document.createElement("p");
        text.textContent = insight;
        card.append(text);
        $("factList").append(card);
      }
    }

    $("gapList").replaceChildren();
    for (const gap of (profile.gaps || []).slice(0, 5)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "gap-question";
      button.textContent = gap.question;
      button.addEventListener("click", () => {
        $("coachInput").value = `Help me explore this part of my story: ${gap.question}`;
        $("coachInput").focus();
      });
      $("gapList").append(button);
    }
  }

  function makeItem(label, detail, factIds = []) {
    const item = document.createElement("div");
    item.className = "scene-item";
    const title = document.createElement("strong");
    title.textContent = label || "";
    item.append(title);
    if (detail) {
      const description = document.createElement("span");
      description.textContent = detail;
      item.append(description);
    }
    const userFactIds = new Set(
      (state.deck.profile?.facts || [])
        .filter((fact) => fact.source === "user_chat")
        .map((fact) => fact.id),
    );
    if (factIds.some((id) => userFactIds.has(id))) appendBadge(item, "Added by you", "user");
    return item;
  }

  function renderScene(scene, target, isBroadcast = false) {
    target.replaceChildren();
    if (!scene) return;
    const type = document.createElement("div");
    type.className = "scene-type";
    type.textContent = scene.t === "title" ? "Career story" : scene.h || "Career story";
    target.append(type);
    const title = document.createElement(isBroadcast ? "h1" : "h2");
    title.textContent = scene.h || "Career story";
    target.append(title);
    if (scene.s) {
      const subtitle = document.createElement("p");
      subtitle.className = "scene-subtitle";
      subtitle.textContent = scene.s;
      target.append(subtitle);
    }
    const items = Array.isArray(scene.items) ? scene.items : [];
    if (items.length) {
      const list = document.createElement("div");
      list.className = isBroadcast ? "broadcast-items" : "scene-items";
      const factIds = Array.isArray(scene.fact_ids) ? scene.fact_ids : [];
      for (const entry of items) {
        if (!Array.isArray(entry)) continue;
        const label = String(entry[0] ?? "");
        const detail = entry.length > 1 ? String(entry[1] ?? "") : "";
        list.append(makeItem(label, detail, factIds));
      }
      target.append(list);
    }
  }

  function currentScene() {
    return state.deck?.slides?.[state.sceneIndex] || null;
  }

  function renderSceneViews() {
    const slides = state.deck?.slides || [];
    state.sceneIndex = Math.max(0, Math.min(state.sceneIndex, slides.length - 1));
    const scene = currentScene();
    renderScene(scene, $("previewScene"));
    $("sceneCount").textContent = `${slides.length ? state.sceneIndex + 1 : 0} / ${slides.length}`;
    $("previousScene").disabled = state.sceneIndex === 0;
    $("nextScene").disabled = state.sceneIndex >= slides.length - 1;
    $("previewNarration").textContent = scene?.n || "";
    $("broadcastTicker").textContent = `NOW REPORTING · ${scene?.h || "CAREER PROFILE"}`;
    renderScene(scene, $("broadcastScreen"), true);
    $("broadcastCaption").textContent = scene?.n || "";
  }

  function renderStudio() {
    if (!state.deck) return;
    $("storyName").textContent = state.deck.name || "Your career story";
    renderProfile();
    renderSceneViews();
  }

  function renderProposal(proposal) {
    const card = document.createElement("article");
    card.className = "proposal";
    appendBadge(card, "Proposed · not saved", "");
    const info = document.createElement("p");
    info.textContent = proposal.text;
    card.append(info);
    const section = document.createElement("p");
    section.className = "chat-hint";
    section.textContent = `Presentation scene: ${proposal.presentation_section || "Career story"}`;
    card.append(section);
    const actions = document.createElement("div");
    actions.className = "proposal-actions";
    const add = document.createElement("button");
    add.className = "button small";
    add.dataset.addFact = "true";
    add.type = "button";
    add.textContent = "Add to my story";
    add.addEventListener("click", () => saveProposal(proposal, add, card));
    const dismiss = document.createElement("button");
    dismiss.className = "button secondary small";
    dismiss.type = "button";
    dismiss.textContent = "Not now";
    dismiss.addEventListener("click", () => card.remove());
    actions.append(add, dismiss);
    card.append(actions);
    $("chatLog").append(card);
    $("chatLog").scrollTop = $("chatLog").scrollHeight;
  }

  async function saveProposal(proposal, button, card) {
    if (state.savingFact) return;
    state.savingFact = true;
    $("sendMessage").disabled = true;
    document.querySelectorAll("[data-add-fact]").forEach((addButton) => { addButton.disabled = true; });
    button.disabled = true;
    button.textContent = "Updating story…";
    try {
      const response = await api(`deck/${encodeURIComponent(state.id)}/story-facts`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(proposal),
      });
      const result = await readResponse(response);
      state.deck = result.deck;
      const updatedSceneIndex = state.deck.slides.findIndex((scene) =>
        Array.isArray(scene.fact_ids) && scene.fact_ids.includes(result.fact?.id)
      );
      if (updatedSceneIndex >= 0) state.sceneIndex = updatedSceneIndex;
      const confirmation = result.message || "Added to your story and presentation.";
      state.history.push({ role: "assistant", content: confirmation });
      sessionStorage.setItem(`rp-chat:${state.id}`, JSON.stringify(state.history));
      card.remove();
      addChatBubble("assistant", `✓ ${confirmation}\nThis detail is now shown as candidate-added in your presentation.`);
      renderStudio();
    } catch (error) {
      button.disabled = false;
      button.textContent = "Add to my story";
      addChatBubble("assistant", error.message);
    } finally {
      state.savingFact = false;
      $("sendMessage").disabled = state.pending;
      document.querySelectorAll("[data-add-fact]").forEach((addButton) => { addButton.disabled = false; });
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    if (state.pending || state.savingFact) return;
    const input = $("coachInput");
    const message = input.value.trim();
    if (!message) return;
    if (message.length > 4000) {
      addChatBubble("assistant", "Please keep a message under 4,000 characters.");
      return;
    }
    input.value = "";
    addChatBubble("user", message);
    state.pending = true;
    $("sendMessage").disabled = true;
    const waiting = addChatBubble("assistant", "Thinking through your story…");
    try {
      const response = await api(`deck/${encodeURIComponent(state.id)}/interview`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ message, history: state.history }),
      });
      const result = await readResponse(response);
      waiting.remove();
      state.history.push({ role: "user", content: message });
      state.history.push({ role: "assistant", content: result.reply });
      sessionStorage.setItem(`rp-chat:${state.id}`, JSON.stringify(state.history.slice(-40)));
      addChatBubble("assistant", result.reply);
      for (const proposal of result.proposals || []) renderProposal(proposal);
      for (const insight of result.insights || []) {
        addChatBubble("assistant", `AI interpretation (not a resume fact): ${insight}`);
      }
    } catch (error) {
      waiting.textContent = error.message;
    } finally {
      state.pending = false;
      $("sendMessage").disabled = false;
      input.focus();
    }
  }

  function narrationText(scene) {
    const pronouns = { F: ["she", "her", "She"], M: ["he", "his", "He"], N: ["they", "their", "They"] };
    const [she, her, Her] = pronouns[state.deck.gender] || pronouns.N;
    return (scene?.n || scene?.h || "")
      .replace(/\{name\}/g, state.deck.name || "")
      .replace(/\{she\}/g, she)
      .replace(/\{her\}/g, her)
      .replace(/\{Her\}/g, Her);
  }

  function stopNarration() {
    state.speaking = false;
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    $("avatar")?.classList.remove("talking");
    $("playBroadcast").textContent = "▶ Play presentation";
  }

  function speakCurrentScene() {
    const scene = currentScene();
    if (!scene || !state.speaking) return;
    const utterance = new SpeechSynthesisUtterance(narrationText(scene));
    const voices = window.speechSynthesis.getVoices().filter((voice) => /^en/i.test(voice.lang));
    if (voices.length) utterance.voice = voices[0];
    state.utterance = utterance;
    utterance.onstart = () => $("avatar")?.classList.add("talking");
    utterance.onend = () => {
      $("avatar")?.classList.remove("talking");
      if (!state.speaking) return;
      if (state.sceneIndex < state.deck.slides.length - 1) {
        state.sceneIndex += 1;
        renderSceneViews();
        window.setTimeout(speakCurrentScene, 250);
      } else {
        state.speaking = false;
        $("playBroadcast").textContent = "↻ Replay presentation";
        $("broadcastStatus").textContent = "Presentation complete";
      }
    };
    utterance.onerror = (event) => {
      $("avatar")?.classList.remove("talking");
      if (event.error !== "canceled" && event.error !== "interrupted") {
        state.speaking = false;
        $("playBroadcast").textContent = "▶ Play presentation";
        $("broadcastStatus").textContent = "Narration could not play. Try another browser voice or continue manually.";
      }
    };
    window.speechSynthesis.speak(utterance);
  }

  function toggleNarration() {
    if (!("speechSynthesis" in window) || typeof SpeechSynthesisUtterance === "undefined") {
      $("broadcastStatus").textContent = "Speech playback is not supported in this browser.";
      return;
    }
    if (state.speaking && window.speechSynthesis.speaking) {
      window.speechSynthesis.pause();
      $("playBroadcast").textContent = "▶ Resume presentation";
      return;
    }
    if (state.speaking && window.speechSynthesis.paused) {
      window.speechSynthesis.resume();
      $("playBroadcast").textContent = "⏸ Pause presentation";
      return;
    }
    if (state.sceneIndex === state.deck.slides.length - 1 && $("playBroadcast").textContent.includes("Replay")) {
      state.sceneIndex = 0;
      renderSceneViews();
    }
    state.speaking = true;
    $("broadcastStatus").textContent = "";
    $("playBroadcast").textContent = "⏸ Pause presentation";
    speakCurrentScene();
  }

  function openPresenter() {
    state.presenting = true;
    $("broadcast").classList.remove("hidden");
    $("broadcast").setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    renderSceneViews();
    $("exitBroadcast").focus();
  }

  function closePresenter() {
    stopNarration();
    state.presenting = false;
    $("broadcast").classList.add("hidden");
    $("broadcast").setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    $("presentStory").focus();
  }

  async function uploadFile(file) {
    $("uploadStatus").classList.remove("error");
    if (!paymentUnlocked()) {
      setStatus("Complete payment to unlock resume upload.", true);
      return;
    }
    if (!file || !/\.(pdf|docx)$/i.test(file.name)) {
      setStatus("Choose a PDF or DOCX resume.", true);
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      setStatus("This file is over the 5 MB limit.", true);
      return;
    }
    const form = new FormData();
    form.append("file", file);
    form.append("use_env", "true");
    $("dropzone").disabled = true;
    setStatus("Reading your resume and building a source-grounded first draft…");
    setProgress(30, "Reading resume");
    try {
      const response = await api("upload", { method: "POST", body: form });
      const result = await readResponse(response);
      if (!result.id || !result.edit_token || !result.deck) {
        throw new Error("The story draft was incomplete. Please try uploading again.");
      }
      setProgress(100, "Ready to build");
      beginStudio(result);
    } catch (error) {
      setStatus(error.message || "The resume could not be processed.", true);
      setProgress(0, "Needs attention");
    } finally {
      $("dropzone").disabled = false;
      $("fileInput").value = "";
    }
  }

  function newResume() {
    clearSession();
    $("studioView").classList.add("hidden");
    $("uploadView").classList.remove("hidden");
    $("chatLog").replaceChildren();
    $("coachInput").value = "";
    $("uploadStatus").classList.remove("error");
    setStatus("No resume uploaded yet. Choose a PDF or DOCX to begin.");
    setProgress(0, "Ready");
    updatePaymentUI();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function exportPptx() {
    if (state.id) location.href = new URL(`/api/deck/${encodeURIComponent(state.id)}/pptx`, location.origin);
  }

  async function copyShareLink() {
    const url = $("shareAddress").value;
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      $("copyShare").textContent = "Copied";
      window.setTimeout(() => { $("copyShare").textContent = "Copy link"; }, 1600);
    } catch {
      $("shareAddress").select();
      document.execCommand("copy");
      $("copyShare").textContent = "Copied";
      window.setTimeout(() => { $("copyShare").textContent = "Copy link"; }, 1600);
    }
  }

  function shareTo(platform) {
    const url = encodeURIComponent($("shareAddress").value);
    const message = encodeURIComponent(`Meet my career story: ${state.deck.name || ""}`);
    const targets = {
      linkedin: `https://www.linkedin.com/sharing/share-offsite/?url=${url}`,
      x: `https://twitter.com/intent/tweet?text=${message}&url=${url}`,
      whatsapp: `https://wa.me/?text=${message}%20${url}`,
    };
    if (targets[platform]) window.open(targets[platform], "_blank", "noopener,noreferrer");
  }

  async function restoreSession() {
    const id = sessionStorage.getItem("rp-current-id");
    if (!id) return;
    const token = sessionStorage.getItem(`rp-edit:${id}`);
    if (!token) return;
    try {
      const response = await api(`deck/${encodeURIComponent(id)}`);
      const deck = await readResponse(response);
      state.id = id;
      state.editToken = token;
      state.deck = deck;
      try {
        const history = JSON.parse(sessionStorage.getItem(`rp-chat:${id}`) || "[]");
        state.history = Array.isArray(history) ? history.filter((turn) =>
          turn && ["user", "assistant"].includes(turn.role) && typeof turn.content === "string"
        ).slice(-40) : [];
      } catch {
        state.history = [];
      }
      $("uploadView").classList.add("hidden");
      $("studioView").classList.remove("hidden");
      $("shareAddress").value = deck.share_url || new URL(`/share/${id}`, location.origin).href;
      $("anchorName").textContent = deck.name || "Career presenter";
      renderStudio();
      for (const turn of state.history) addChatBubble(turn.role, turn.content);
    } catch {
      sessionStorage.removeItem(`rp-edit:${id}`);
      sessionStorage.removeItem(`rp-chat:${id}`);
      sessionStorage.removeItem("rp-current-id");
    }
  }

  function bindEvents() {
    $("paidOption").addEventListener("change", updatePaymentUI);
    $("dropzone").addEventListener("click", () => $("fileInput").click());
    $("dropzone").addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        $("fileInput").click();
      }
    });
    $("fileInput").addEventListener("change", (event) => uploadFile(event.target.files?.[0]));
    $("coachForm").addEventListener("submit", sendMessage);
    $("previousScene").addEventListener("click", () => {
      stopNarration();
      state.sceneIndex = Math.max(0, state.sceneIndex - 1);
      renderSceneViews();
    });
    $("nextScene").addEventListener("click", () => {
      stopNarration();
      state.sceneIndex = Math.min(state.deck.slides.length - 1, state.sceneIndex + 1);
      renderSceneViews();
    });
    $("presentStory").addEventListener("click", openPresenter);
    $("exitBroadcast").addEventListener("click", closePresenter);
    $("playBroadcast").addEventListener("click", toggleNarration);
    $("broadcastPrev").addEventListener("click", () => {
      stopNarration();
      state.sceneIndex = Math.max(0, state.sceneIndex - 1);
      renderSceneViews();
    });
    $("broadcastNext").addEventListener("click", () => {
      stopNarration();
      state.sceneIndex = Math.min(state.deck.slides.length - 1, state.sceneIndex + 1);
      renderSceneViews();
    });
    $("exportPptx").addEventListener("click", exportPptx);
    $("copyShare").addEventListener("click", copyShareLink);
    $("newResume").addEventListener("click", newResume);
    document.querySelectorAll("[data-share]").forEach((button) => {
      button.addEventListener("click", () => shareTo(button.dataset.share));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.presenting) closePresenter();
    });
  }

  bindEvents();
  updatePaymentUI();
  setProgress(0, "Ready");
  setStatus("No resume uploaded yet. Choose a PDF or DOCX to begin.");
  restoreSession();
})();
