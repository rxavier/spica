/* One player for the whole site: the station panel is redrawn on every change, the
   audio element is not. A live stream cannot be paused or seeked, so the control is
   listen / stop; the arc under the card is the level, fed by the stream itself. */
(() => {
  const stream = document.getElementById("stream");
  const statik = document.getElementById("static");

  let ctx = null, analyser = null, wave = null, raf = 0, wanted = false, busy = false;
  let current = null;              // the station currently loaded
  let attempt = 0;                 // a newer listen() cancels an older one
  let level = 0;                   // smoothed 0..1 for the arc

  function playerbar() {
    return document.querySelector("[data-player]");
  }

  function setPill(text, cls) {
    const bar = playerbar();
    const pill = bar?.querySelector(".pill");
    const btn = bar?.querySelector(".play");
    if (!pill) return;
    pill.textContent = text;
    pill.classList.toggle("tuning", cls === "tuning");
    pill.classList.toggle("live", cls === "live");
    if (btn) {
      btn.classList.toggle("is-playing", cls === "live");
      btn.setAttribute("aria-label", cls === "live" ? "stop" : "listen");
    }
    if (cls !== "live") restArc();
  }

  /* ---- the level: six bars, each with its own threshold ---- */

  const BARS = [0.28, 0.44, 0.58, 0.70, 0.82, 0.93];

  function setArc(value) {
    const bars = document.querySelectorAll("#vu i");
    if (!bars.length) return;
    const v = Math.max(0, Math.min(1, value));
    bars.forEach((bar, i) => {
      const from = BARS[i] * 0.7;
      const scale = v <= from ? 0 : Math.min(1, 0.3 + (v - from) * 2.4);
      bar.style.setProperty("--v", scale.toFixed(2));
    });
  }

  function restArc() {
    level = 0;
    document.querySelectorAll("#vu i").forEach((bar) => bar.style.setProperty("--v", "0"));
  }

  function wire() {
    if (ctx) return true;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return false;
    ctx = new Ctx();
    const src = ctx.createMediaElementSource(stream);
    analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.55;
    wave = new Uint8Array(analyser.fftSize);
    src.connect(analyser);
    analyser.connect(ctx.destination);
    return true;
  }

  function draw() {
    if (!analyser) { raf = 0; return; }
    analyser.getByteTimeDomainData(wave);
    let sum = 0;
    for (let i = 0; i < wave.length; i++) {
      const v = (wave[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / wave.length);
    const target = Math.min(1, Math.pow(rms * 3.4, 0.7));
    level += (target - level) * 0.3;          // it breathes rather than flickers
    setArc(level);
    raf = requestAnimationFrame(draw);
  }

  function animate() {
    if (!wire()) return;
    ctx.resume();
    if (!raf) raf = requestAnimationFrame(draw);
  }

  function stopAnimation() {
    cancelAnimationFrame(raf);
    raf = 0;
    restArc();
  }

  /* ---- presence ---- */

  async function isLive(url) {
    try {
      const res = await fetch(url, { cache: "no-store" });
      return Boolean((await res.json()).live);
    } catch {
      return false;
    }
  }

  let beat = null;
  let beatCleanup = null;

  function stopBeat() {
    if (beat) clearInterval(beat);
    beat = null;
    if (beatCleanup) {
      beatCleanup();
      beatCleanup = null;
    }
  }

  // The radio only talks when somebody can hear it, and a socket is not proof of
  // that. A closed tab stops beating, so the DJ stops within a minute.
  function startBeat(station) {
    stopBeat();
    let ticks = 0;
    let seen = 0;
    let stalled = 0;
    // "timeupdate" only fires while audio is actually advancing, which is what
    // "somebody can hear this" means. A closed mount does not always raise
    // "error" — it just stops delivering bytes.
    const onTick = function () { ticks++; };
    stream.addEventListener("timeupdate", onTick);
    const ping = function () {
      if (ticks === seen) {
        if (++stalled >= 2) {            // 40 s of frozen audio
          stopBeat();
          statik.pause();
          stream.pause();
          setPill("off air");
          stopAnimation();
          return;
        }
      } else {
        stalled = 0;
        seen = ticks;
      }
      fetch("/s/" + station + "/hear", { method: "POST", keepalive: true }).catch(function () {});
    };
    beatCleanup = function () { stream.removeEventListener("timeupdate", onTick); };
    ping();
    beat = setInterval(ping, 20000);
  }

  function isPlaying() {
    return playerbar()?.querySelector(".play")?.classList.contains("is-playing") ?? false;
  }

  async function join(src) {
    // The same URL twice is not a reload: a first attempt made while the mount was
    // still closed would leave the element stuck on that failure forever.
    stream.pause();
    stream.src = src + (src.includes("?") ? "&" : "?") + "t=" + Date.now();
    stream.load();
    try {
      await stream.play();
      return true;
    } catch {
      return false;
    }
  }

  async function listen() {
    const bar = playerbar();
    if (!bar) return;
    const mine = ++attempt;
    busy = true;
    wanted = true;
    const { start, state: stateUrl, station } = bar.dataset;
    // Through the relay, so the picker can tell a browser from a VLC.
    const src = "/listen/" + station;
    current = station;

    statik.volume = 0.35;
    statik.play().catch(() => {
      if (mine === attempt) setPill("off air");
    });

    if (await isLive(stateUrl)) {
      if (mine !== attempt) return;
      if (!(await join(src))) setPill("off air");
      else startBeat(station);
      busy = false;
      return;
    }

    setPill("tuning", "tuning");
    fetch(start, { method: "POST" }).catch(() => {});

    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 350));
      if (mine !== attempt) return;
      if (await isLive(stateUrl)) {
        if (mine !== attempt) return;
        if (await join(src)) { startBeat(station); busy = false; return; }
        break;
      }
    }
    if (mine !== attempt) return;
    statik.pause();
    setPill("off air");
    busy = false;
  }

  function stop() {
    attempt++;
    busy = false;
    wanted = false;
    stopBeat();
    statik.pause();
    stream.pause();
    stream.removeAttribute("src");
    stream.load();
    setPill("off air");
    stopAnimation();
  }

  /* ---- the ledger speaks in the listener's own clock ---- */

  function localiseTimes() {
    document.querySelectorAll("time[data-t]").forEach((el) => {
      const raw = el.dataset.t;
      if (!raw) return;
      const when = new Date(raw.includes("T") ? raw : raw.replace(" ", "T"));
      if (Number.isNaN(when.getTime())) return;
      el.textContent =
        String(when.getHours()).padStart(2, "0") + ":" + String(when.getMinutes()).padStart(2, "0");
      el.title = when.toLocaleString();
    });
  }

  /* ---- wiring, re-run whenever the panel is swapped in ---- */

  function wireBar() {
    const bar = playerbar();
    if (!bar || bar.dataset.wired === "1") return;
    bar.dataset.wired = "1";
    bar.querySelector(".play").addEventListener("click", () => {
      if (isPlaying()) stop(); else listen();
    });
    const vol = bar.querySelector(".volume input");
    if (vol) {
      const paint = () => {
        const pct = Number(vol.value);
        vol.style.setProperty(
          "--track",
          `linear-gradient(to right, rgba(239,228,207,.85) 0 ${pct}%, rgba(239,228,207,.22) ${pct}% 100%)`
        );
      };
      vol.value = String(Math.round(stream.volume * 100));
      paint();
      vol.addEventListener("input", () => {
        const v = Number(vol.value) / 100;
        stream.volume = v;
        statik.volume = 0;
        stream.muted = v === 0;
        paint();
      });
    }
  }

  function wireTabs() {
    const tabs = [...document.querySelectorAll("[data-tabs] .tab")];
    const panes = [...document.querySelectorAll(".pane")];
    tabs.forEach((tab) => {
      if (tab.dataset.wired === "1") return;
      tab.dataset.wired = "1";
      tab.addEventListener("click", () => {
        tabs.forEach((other) => other.classList.toggle("is-on", other === tab));
        const name = tab.dataset.pane;
        panes.forEach((pane) => (pane.hidden = pane.id !== "pane-" + name));
        const pane = document.getElementById("pane-" + name);
        if (pane && pane.dataset.src && window.htmx) {
          htmx.ajax("GET", pane.dataset.src, { target: "#" + pane.dataset.target, swap: "innerHTML" });
        }
      });
    });
  }

  /* Arriving on a station opens its mount so the first track is already being
     encoded when play is pressed — but a mount with no listener closes itself
     after IDLE_STOP_S, so a page nobody touches costs ninety seconds of encoding
     and nothing else. No audio element is touched, so nothing plays. */
  let warmed = "";

  function warm(station) {
    if (!station || warmed === station || isPlaying()) return;
    warmed = station;
    setTimeout(() => {
      if (isPlaying() || warmed !== station) return;
      fetch("/s/" + station + "/start", { method: "POST" }).catch(() => {});
    }, 1200);
  }

  function tuneStation() {
    const bar = playerbar();
    if (!bar) return;
    const station = bar.dataset.station;
    if (station === current) return;
    const wasPlaying = isPlaying();
    current = station;
    // Switching while listening follows the listener to the new station; a
    // station change on a silent page stays silent.
    if (wasPlaying) listen();
    else warm(station);
  }

  function init() {
    localiseTimes();
    const bar = playerbar();
    if (!bar) return;
    if (bar.dataset.name) document.title = bar.dataset.name + " · Spica";
    wireBar();
    wireTabs();
    if (!isPlaying()) restArc();
    bar.dataset.ready = "1";
    if (!current) {
      current = bar.dataset.station;
      // No autoplay. Landing only preloads the mount; the listener decides.
      setPill("press play");
      warm(current);
    } else {
      tuneStation();
    }
  }

  stream.addEventListener("playing", () => {
    setPill("live", "live");
    const fade = setInterval(() => {
      statik.volume = Math.max(0, statik.volume - 0.1);
      if (statik.volume <= 0.01) {
        clearInterval(fade);
        statik.pause();
        statik.volume = 0.35;
      }
    }, 45);
    animate();
  });
  stream.addEventListener("pause", () => { setPill("off air"); stopAnimation(); });
  stream.addEventListener("ended", () => {
    setPill("press play");
    stopAnimation();
    stopBeat();  // nothing is being heard any more, so stop claiming otherwise
  });
  stream.addEventListener("error", () => {
    if (!wanted) return;
    statik.pause();
    setPill("off air");
    stopAnimation();
    // The mount closed underneath us. Without this the page keeps posting its
    // heartbeat every 20 s and the station keeps picking for an empty room.
    stopBeat();
  });

  window.initPlayer = init;
  document.body.addEventListener("htmx:afterSettle", init);
  document.addEventListener("pointerdown", () => {
    const bar = playerbar();
    if (wanted && bar && !isPlaying() && !busy) listen();
  }, { once: true, capture: true });

  init();
})();
