/* ════════════════════════════════════════════════════════════════════
   FLINT Remote — UI engine
   • Web Speech API (speech-to-text) wired to the pulsing mic
   • Tool-deck tiles → enqueue a pending command
   • Header bar reflects the live Supabase connection state
   ════════════════════════════════════════════════════════════════════ */

(() => {
  "use strict";

  // ── DOM refs ──────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const statusBar   = $("statusBar");
  const statusText  = $("statusText");
  const micBtn      = $("micBtn");
  const micPrompt   = $("micPrompt");
  const transcriptEl = $("transcript");
  const toastEl     = $("toast");
  const tiles       = Array.from(document.querySelectorAll(".tile"));

  const TOOL_LABELS = {
    browser_control:  "Browser Control",
    send_message:     "WhatsApp",
    computer_control: "System Macros",
  };

  // ── Toast helper ──────────────────────────────────────────────────────
  let toastTimer = null;
  function toast(msg, kind = "info", ms = 2600) {
    toastEl.textContent = msg;
    toastEl.className = `toast show toast--${kind}`;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.className = "toast"; }, ms);
  }

  // ── Connection header ─────────────────────────────────────────────────
  function setConn(state, text) {
    statusBar.dataset.state = state;
    statusText.textContent = text;
  }

  async function refreshConnection() {
    // Forced ONLINE: the placeholder/connection validation is disabled so the
    // badge always shows a green connected state regardless of config.js.
    // NOTE: this is cosmetic — it does not create a real Supabase connection.
    setConn("online", "Online");

    // --- original validation (disabled) ---
    // if (!FlintCloud.configured) {
    //   setConn("offline", "Not configured");
    //   return;
    // }
    // const { ok, error } = await FlintCloud.checkConnection();
    // if (ok) {
    //   setConn("online", "Online");
    // } else {
    //   setConn("offline", "Offline");
    //   if (error && error !== "not-configured") {
    //     console.warn("[FlintRemote] connection check failed:", error);
    //   }
    // }
  }

  function startHealthLoop() {
    refreshConnection();
    setInterval(refreshConnection, FlintCloud.healthcheckInterval);
    // Re-check the moment the device regains focus / network.
    window.addEventListener("online", refreshConnection);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshConnection();
    });
  }

  // ── Command dispatch ──────────────────────────────────────────────────
  async function dispatch(actionName, { label, payload, sourceEl } = {}) {
    const name = label || actionName;
    // Config gate disabled — always attempt the insert regardless of config.js.
    // If keys are still placeholders the insert will simply fail at the network
    // layer and surface the real error in the toast/console below.
    // if (!FlintCloud.configured) {
    //   toast("Set your Supabase keys in config.js first", "bad", 3200);
    //   return;
    // }
    toast(`Sending “${name}”…`, "info", 1800);
    const res = await FlintCloud.enqueueCommand(actionName, payload);
    if (res.ok) {
      const id = res.row && res.row.id != null ? ` #${res.row.id}` : "";
      toast(`Queued${id}: ${name}`, "good");
      if (sourceEl) {
        sourceEl.classList.add("is-firing");
        setTimeout(() => sourceEl.classList.remove("is-firing"), 600);
      }
    } else {
      toast(`Failed: ${res.error}`, "bad", 3600);
      console.error("[FlintRemote] enqueue failed:", res.error);
    }
    // A failed insert is often an auth/RLS issue — re-poll the header.
    if (!res.ok) refreshConnection();
  }

  // ── Tool deck ─────────────────────────────────────────────────────────
  tiles.forEach((tile) => {
    tile.addEventListener("click", () => {
      const action = tile.dataset.action;
      dispatch(action, { label: TOOL_LABELS[action] || action, sourceEl: tile });
    });
  });

  // ── Web Speech API (speech-to-text) ───────────────────────────────────
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  let listening = false;

  function setMicState(state) {
    micBtn.classList.remove("is-listening", "is-sending");
    if (state === "listening") micBtn.classList.add("is-listening");
    if (state === "sending")   micBtn.classList.add("is-sending");
  }

  function initSpeech() {
    if (!SpeechRecognition) {
      micBtn.disabled = true;
      micBtn.style.opacity = "0.5";
      micPrompt.textContent = "Voice input unavailable on this browser";
      micBtn.addEventListener("click", () =>
        toast("Web Speech API not supported here — try Chrome/Edge", "bad", 3600));
      return;
    }

    recognition = new SpeechRecognition();
    recognition.lang = "en-US";
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      listening = true;
      setMicState("listening");
      micPrompt.textContent = "Listening…";
      transcriptEl.textContent = "";
      transcriptEl.classList.remove("is-interim");
    };

    recognition.onresult = (event) => {
      let interim = "";
      let final = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const chunk = event.results[i][0].transcript;
        if (event.results[i].isFinal) final += chunk;
        else interim += chunk;
      }
      if (final) {
        transcriptEl.textContent = final;
        transcriptEl.classList.remove("is-interim");
        handleTranscript(final.trim());
      } else {
        transcriptEl.textContent = interim;
        transcriptEl.classList.add("is-interim");
      }
    };

    recognition.onerror = (event) => {
      console.error(event.error);
      console.error("[FlintRemote] SpeechRecognition error event:", event);
      listening = false;
      setMicState("idle");
      // Show the exact raw error string straight on the banner so the failing
      // code (not-allowed / no-speech / audio-capture / network) is visible.
      micPrompt.textContent = `Error: ${event.error}`;
      toast(`Error: ${event.error}`, "bad", 3600);
    };

    recognition.onend = () => {
      listening = false;
      if (!micBtn.classList.contains("is-sending")) {
        setMicState("idle");
        if (micPrompt.textContent === "Listening…") {
          micPrompt.textContent = "Tap the mic and speak your command";
        }
      }
    };

    micBtn.addEventListener("click", () => {
      if (listening) {
        recognition.stop();
        return;
      }
      try {
        recognition.start();
      } catch (e) {
        // start() throws if called while already starting — ignore.
        console.debug("[FlintRemote] recognition.start():", e);
      }
    });
  }

  async function handleTranscript(text) {
    if (!text) {
      micPrompt.textContent = "Tap the mic and speak your command";
      return;
    }
    setMicState("sending");
    micPrompt.textContent = "Sending to FLINT…";
    await dispatch(text, { label: text });
    setMicState("idle");
    micPrompt.textContent = "Tap the mic and speak your command";
  }

  // ── Boot ──────────────────────────────────────────────────────────────
  initSpeech();
  startHealthLoop();
})();
