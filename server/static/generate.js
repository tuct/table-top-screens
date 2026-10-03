/* The make-a-clip page.
 *
 * Three things happen here that plain forms cannot do: a job is polled while
 * it runs, a finished still is adopted as the reference for the next one
 * without a round trip through the filesystem, and the queue is watched so
 * work started elsewhere in ComfyUI is visible too.
 *
 * With this file blocked the form still submits: the server redirects back and
 * the job appears in the lists with whatever state it has reached. Slower to
 * watch, but nothing is lost. */
(function () {
  var form = document.getElementById("genform");
  if (!form) return;                       // the no-generator page

  var $ = function (id) { return document.getElementById(id); };
  var box = $("resultbox"), empty = $("resultempty"), working = $("working");
  var fill = $("workfill"), line = $("workline"), failed = $("failed");
  var img = $("resultimg"), vid = $("resultvid"), meta = $("resultmeta");
  var keep = $("keepform"), adopt = $("adopt");
  var fromjob = $("fromjob"), file = $("reference"), reflabel = $("reflabel");
  var refshot = $("refshot"), refpreview = $("refpreview"), refnote = $("refnote");
  var polling = null, ticking = null, current = null;

  // The server is asked every POLL_MS -- often enough for work measured in
  // minutes, and it keeps a four-minute clip from costing hundreds of
  // requests. The display ticks every second in between so the counter reads
  // as a running clock instead of jumping in five-second steps.
  var POLL_MS = 5000;
  var TICK_MS = 1000;

  // ---- the reference ------------------------------------------------------
  // Upload, pool pick and adopted still are three answers to one question, so
  // setting any of them retracts the other two.
  function clearRef(keepFile) {
    if (!keepFile && file) file.value = "";
    fromjob.value = "";
    var picked = form.querySelector('input[name="pool_id"]:checked');
    if (picked) picked.checked = false;
    syncPool();
  }

  function showRefImage(src, note) {
    refpreview.src = src;
    refshot.hidden = false;
    refnote.hidden = !note;
  }

  function hideRef() {
    refshot.hidden = true;
    refnote.hidden = true;
    refpreview.removeAttribute("src");
    reflabel.textContent = "Choose a picture, or drop one here";
  }

  if (file) file.addEventListener("change", function () {
    var f = file.files[0];
    if (!f) return;
    clearRef(true);
    reflabel.textContent = f.name;
    showRefImage(URL.createObjectURL(f), false);
  });

  var clear = $("refclear");
  if (clear) clear.addEventListener("click", function () { clearRef(); hideRef(); });

  var drop = $("refdrop");
  if (drop) {
    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); });
    });
    drop.addEventListener("drop", function (e) {
      var f = e.dataTransfer && e.dataTransfer.files[0];
      if (!f) return;
      try { file.files = e.dataTransfer.files; } catch (err) {}
      clearRef(true);
      reflabel.textContent = f.name;
      showRefImage(URL.createObjectURL(f), false);
    });
  }

  function syncPool() {
    var tiles = form.querySelectorAll(".pooltile");
    for (var i = 0; i < tiles.length; i++) {
      var input = tiles[i].querySelector("input");
      tiles[i].classList.toggle("picked", !!(input && input.checked));
    }
  }
  var pool = $("poolpick");
  if (pool) {
    pool.addEventListener("change", function () {
      if (file) file.value = "";
      fromjob.value = "";
      hideRef();
      syncPool();
    });
    syncPool();
  }

  // Adopting is the refining loop: the still just produced becomes the
  // reference for the next run, so each turn starts from the last result
  // rather than from nothing.
  function adoptJob(id) {
    clearRef();
    fromjob.value = id;
    showRefImage("/generate/job/" + id + "/result?t=" + Date.now(), true);
    reflabel.textContent = "adopted from a verify";
    $("prompt").focus();
  }

  if (adopt) adopt.addEventListener("click", function () {
    if (current) adoptJob(current.id);
  });

  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".adoptone");
    if (b) { e.preventDefault(); adoptJob(b.dataset.id); }
  });

  // ---- what a loop will cost ---------------------------------------------
  var lengthSel = $("length"), clipcost = $("clipcost");
  if (lengthSel && clipcost) {
    var mins = { "1s": "~1 min", "2s": "~2 min", "4s": "~4 min" };
    lengthSel.addEventListener("change", function () {
      clipcost.textContent = "animated, " + (mins[lengthSel.value] || "");
    });
  }

  // ---- submitting ---------------------------------------------------------
  form.addEventListener("submit", function (e) {
    // Which button was pressed decides still or loop, and FormData loses that.
    var kind = (e.submitter && e.submitter.value) || "verify";
    e.preventDefault();
    var data = new FormData(form);
    data.set("kind", kind);

    reset();
    working.hidden = false;
    line.textContent = kind === "clip" ? "queued…" : "queued…";

    fetch("/generate/run", { method: "POST", body: data })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(strip(t)); });
        return r.json();
      })
      .then(watch)
      .catch(fail);
  });

  function stopTimers() {
    if (polling) { clearInterval(polling); polling = null; }
    if (ticking) { clearInterval(ticking); ticking = null; }
  }

  function reset() {
    stopTimers();
    current = null;
    box.hidden = true; empty.hidden = true; failed.hidden = true;
    working.hidden = true; fill.style.width = "0%";
    img.hidden = true; vid.hidden = true; adopt.hidden = true;
    img.removeAttribute("src"); vid.removeAttribute("src");
  }

  function watch(job) {
    var estimate = (job.estimate || 60) * 1000;
    var state = "queued", ahead = 0, running = 0, synced = Date.now();

    function paint() {
      if (state === "queued") {
        fill.classList.add("waiting");
        line.textContent = ahead === 0 ? "queued…"
          : ahead === 1 ? "waiting — one job ahead of this one"
          : "waiting — " + ahead + " jobs ahead of this one";
        return;
      }
      fill.classList.remove("waiting");
      // Interpolate between polls so the clock runs smoothly.
      var secs = running + (Date.now() - synced) / 1000;
      // Paced by a measured estimate, because the sampler reports no real
      // progress. It eases toward 95% and waits there, so it never claims to
      // have finished early.
      var ratio = (secs * 1000) / estimate;
      fill.style.width = Math.min(95, Math.round(100 * (1 - Math.exp(-2.2 * ratio)))) + "%";
      line.textContent = job.kind + " — " + Math.round(secs) + "s";
    }

    function poll() {
      fetch("/generate/job/" + job.id)
        .then(function (r) { return r.json(); })
        .then(function (j) {
          state = j.state; ahead = j.ahead || 0;
          running = j.running || 0; synced = Date.now();
          if (j.state === "done") { stopTimers(); done(j); return; }
          if (j.state === "error") { stopTimers(); fail(new Error(j.error)); return; }
          paint();
        })
        .catch(function () { /* a blip; the next poll tries again */ });
    }

    ticking = setInterval(paint, TICK_MS);
    polling = setInterval(poll, POLL_MS);
    poll();                               // don't make the first state wait
  }

  function done(job) {
    current = job;
    fill.style.width = "100%";
    working.hidden = true;
    box.hidden = false;

    var url = "/generate/job/" + job.id + "/result?t=" + Date.now();
    if (job.is_image) {
      img.src = url; img.hidden = false;
      adopt.hidden = false;              // only a still can be a reference
    } else {
      vid.src = url; vid.hidden = false;
      vid.play().catch(function () {});
    }
    meta.textContent = [job.kind, job.size, job.length || null, "seed " + job.seed,
                        Math.round(job.running || job.elapsed) + "s"]
                       .filter(Boolean).join(" · ");
    keep.action = "/generate/job/" + job.id + "/keep";
    keep.querySelector("button").disabled = false;
    heartbeat();
  }

  function fail(err) {
    reset();
    failed.hidden = false;
    failed.textContent = (err && err.message) || "generation failed";
  }

  function strip(t) {
    var m = /<p>(.*?)<\/p>/i.exec(t);     // Flask's abort() pages are HTML
    return m ? m[1] : (t || "").slice(0, 200);
  }

  // ---- keeping ------------------------------------------------------------
  function sendKeep(f) {
    var btn = f.querySelector("button");
    fetch(f.action, { method: "POST", body: new FormData(f) })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(strip(t)); });
        return r.json();
      })
      .then(function () { btn.disabled = true; btn.textContent = "in the pool"; })
      .catch(fail);
  }

  keep.addEventListener("submit", function (e) { e.preventDefault(); sendKeep(keep); });
  document.addEventListener("submit", function (e) {
    if (e.target.classList && e.target.classList.contains("keep")) {
      e.preventDefault(); sendKeep(e.target);
    }
  });

  // ---- the heartbeat ------------------------------------------------------
  // Runs whether or not this page started anything: ComfyUI is shared, and a
  // job queued from its own interface is the reason ours is waiting.
  function heartbeat() {
    fetch("/generate/jobs").then(function (r) { return r.json(); }).then(function (d) {
      paintActive(d.active || []);
      paintGallery("stills", d.stills || [], true);
      paintGallery("clips", d.clips || [], false);
    }).catch(function () {});
  }

  function paintActive(rows) {
    var list = $("activelist"), count = $("activecount");
    if (!list) return;
    if (count) count.textContent = rows.length ? "(" + rows.length + ")" : "";
    list.innerHTML = "";
    if (!rows.length) {
      list.innerHTML = '<li class="note">Idle — nothing queued, here or in ComfyUI.</li>';
      return;
    }
    rows.forEach(function (j) {
      var li = document.createElement("li");
      li.className = j.mine ? "mine" : "foreign";
      li.innerHTML = '<span class="pill ' + (j.state === "running" ? "on" : "off") +
        '"><span class="dot"></span>' + esc(j.state) + '</span>' +
        '<span class="jk mono">' + esc(j.kind) + '</span><span class="jp"></span>' +
        (j.mine ? "" : '<span class="sub">not from this page</span>');
      li.querySelector(".jp").textContent = j.prompt || "(no description)";
      list.appendChild(li);
    });
  }

  function paintGallery(id, rows, isImage) {
    var host = $(id);
    if (!host) return;
    host.innerHTML = "";
    if (!rows.length) {
      host.innerHTML = '<p class="note">No ' + (isImage ? "stills" : "loops") + " yet.</p>";
      return;
    }
    rows.forEach(function (j) {
      var fig = document.createElement("figure");
      fig.className = "shot"; fig.dataset.id = j.id;
      var src = "/generate/job/" + j.id + "/result";
      var media = isImage
        ? '<img src="' + src + '" loading="lazy" alt="">'
        : '<video src="' + src + '" loop muted playsinline preload="metadata" ' +
          'onmouseenter="this.play()" onmouseleave="this.pause()"></video>';
      var extra = isImage
        ? '<button type="button" class="quiet adoptone" data-id="' + j.id + '">Adopt</button>'
        : '<span class="mono sub">' + esc(j.length || "") + "</span>";
      fig.innerHTML = media +
        '<figcaption><span class="jp"></span><span class="acts">' + extra +
        '<form method="post" action="/generate/job/' + j.id +
        '/keep" class="inline keep"><button type="submit" class="quiet"' +
        (j.pool_id ? " disabled" : "") + ">" +
        (j.pool_id ? "in the pool" : "To library") + "</button></form></span></figcaption>";
      fig.querySelector(".jp").textContent = j.prompt || "(no description)";
      fig.querySelector(".jp").title = j.prompt || "";
      host.appendChild(fig);
    });
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  setInterval(heartbeat, POLL_MS);
  heartbeat();
})();
