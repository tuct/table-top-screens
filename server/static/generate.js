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
    refreshKeep();
  });

  var clear = $("refclear");
  if (clear) clear.addEventListener("click", function () {
    clearRef(); hideRef(); refreshKeep();
  });

  // "Keep reference" only means anything when there is one.
  function refreshKeep() {
    var has = !refshot.hidden;
    var field = $("keepfield"), note = $("keepnote");
    if (field) field.style.opacity = has ? "" : ".45";
    if ($("keep")) $("keep").disabled = !has;
    if (note) note.hidden = !has;
  }

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
      var picked = form.querySelector('input[name="pool_id"]:checked');
      if (picked) {
        // Show it, the same as an upload. Without this the only sign a pool
        // reference was chosen is a thin border on a tile inside a <details>
        // that is usually collapsed -- which reads as nothing being chosen.
        var tile = picked.closest(".pooltile").querySelector("img");
        reflabel.textContent = "from the pool";
        showRefImage(tile.src, false);
      } else {
        hideRef();
      }
      syncPool();
      refreshKeep();
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
    refreshKeep();
    $("prompt").focus();
  }

  if (adopt) adopt.addEventListener("click", function () {
    if (current) adoptJob(current.id);
  });

  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".adoptone");
    if (b) { e.preventDefault(); adoptJob(b.dataset.id); }
  });

  // Example prompts drop their real text into the box, where it can be edited.
  // Appended rather than replacing, so picking a scene and then a motion line
  // builds one prompt instead of throwing the first away.
  document.addEventListener("click", function (e) {
    var c = e.target.closest && e.target.closest(".chip[data-prompt]");
    if (!c) return;
    e.preventDefault();
    var box = $(c.dataset.target || "prompt");
    if (!box) return;
    var had = box.value.trim();
    box.value = had ? had.replace(/[,\s]+$/, "") + ", " + c.dataset.prompt
                    : c.dataset.prompt;
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
    box.dispatchEvent(new Event("input", { bubbles: true }));
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

  // A background goes through exactly the same machinery as a clip: post,
  // watch, show. Without this it was a plain form that redirected, so the
  // picture only appeared after a reload -- you asked for a place and the page
  // told you nothing until you went looking for it.
  var bgform = $("bgform");
  if (bgform) bgform.addEventListener("submit", function (e) {
    e.preventDefault();
    var data = new FormData(bgform);
    data.set("kind", "background");
    reset();
    working.hidden = false;
    line.textContent = "queued…";
    fetch("/generate/run", { method: "POST", body: data })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(strip(t)); });
        return r.json();
      })
      .then(watch)
      .catch(fail);
  });

  // Start the next background from an existing one. The words are carried
  // over too, because refining is "this, but ..." -- retyping the scene from
  // memory is how you lose the bit you liked.
  function refineBackground(id, scene, style, motion) {
    var f = $("bgform");
    if (!f) return;
    $("bgrefine").value = id;
    // Deliberately NOT written into the scene box. That box is yours; the
    // description behind the picture rides along in a hidden field and is
    // shown read-only, so refining never types over what you wrote.
    $("bgbase").value = scene || "";
    var shown = $("bgbasetext");
    if (shown) shown.textContent = scene || "(no description)";
    if (motion) $("bgmotion").value = motion;
    if (style) {
      var sel = $("bgstyle");
      if (sel) sel.value = style;
    }
    var note = $("bgrefnote");
    if (note) note.hidden = false;
    var ch = $("bgchange");
    if (ch) ch.value = "";          // a fresh refine asks a fresh question
    f.scrollIntoView({ behavior: "smooth", block: "center" });
    // Focus what is actually being asked for, not the scene that is only
    // carried along as context.
    (ch || $("bgscene")).focus();
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".refinebg");
    if (!b) return;
    e.preventDefault();
    refineBackground(b.dataset.bg, b.dataset.scene, b.dataset.style, b.dataset.motion);
  });

  // Set a place moving. The job goes through the same queue and preview as
  // everything else; when it lands, the card starts playing.
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".animatebg");
    if (!b) return;
    e.preventDefault();
    var id = b.dataset.bg;
    var card = document.querySelector('.bgcard[data-bg="' + id + '"]');
    var motion = card ? card.querySelector('[name=motion]') : null;
    var data = new FormData();
    if (motion && motion.value.trim()) data.set("motion", motion.value.trim());
    var len = card ? card.querySelector(".looplen") : null;
    if (len) data.set("length", len.value);
    b.disabled = true;
    b.textContent = "...";
    reset();
    working.hidden = false;
    line.textContent = "queued...";
    fetch("/backgrounds/" + id + "/animate", { method: "POST", body: data })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(strip(t)); });
        return r.json();
      })
      .then(function (job) {
        watch(job);
        // Swap the still for the loop once it exists. The poll is cheap and
        // stops either way; a stuck job leaves the still in place.
        var tries = 0;
        var swap = setInterval(function () {
          if (++tries > 200) { clearInterval(swap); b.disabled = false; b.textContent = "animate"; return; }
          fetch("/backgrounds/" + id + "/clip", { method: "HEAD" }).then(function (r) {
            if (!r.ok || !card) return;
            clearInterval(swap);
            var v = document.createElement("video");
            v.src = "/backgrounds/" + id + "/clip?t=" + Date.now();
            v.autoplay = v.loop = v.muted = v.playsInline = true;
            var old = card.querySelector("img, video");
            if (old) card.replaceChild(v, old);
            b.disabled = false;
            b.textContent = "again";
          });
        }, 2000);
      })
      .catch(function (err) {
        fail(err);
        b.disabled = false;
        b.textContent = "animate";
      });
  });

  // A closer look. Pictures and loops both open here; a loop plays, because a
  // loop that does not move tells you nothing about whether it is any good.
  (function () {
    var box = $("lightbox"), inner = $("lightinner"), close = $("lightclose");
    if (!box) return;

    function shut() {
      box.hidden = true;
      inner.innerHTML = "";          // stops the video rather than hiding it
    }

    document.addEventListener("click", function (e) {
      var z = e.target.closest && e.target.closest(".zoom[data-full]");
      if (!z) return;
      e.preventDefault();
      inner.innerHTML = "";
      var el;
      if (z.dataset.kind === "video") {
        el = document.createElement("video");
        el.src = z.dataset.full;
        el.autoplay = el.loop = el.muted = el.playsInline = true;
        el.controls = true;
      } else {
        el = document.createElement("img");
        el.src = z.dataset.full;
      }
      inner.appendChild(el);
      box.hidden = false;
    });

    close.addEventListener("click", shut);
    box.addEventListener("click", function (e) {
      if (e.target === box) shut();        // clicking the backdrop closes it
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !box.hidden) shut();
    });
  })();

  var bgrefclear = $("bgrefclear");
  if (bgrefclear) bgrefclear.addEventListener("click", function () {
    $("bgrefine").value = "";
    $("bgbase").value = "";
    $("bgrefnote").hidden = true;
    var ch = $("bgchange");
    if (ch) ch.value = "";          // or it would be sent on the next plain run
  });

  // Standing a character in a place. No model runs, so this answers in
  // milliseconds and the sliders can be live rather than a submit-and-wait.
  var placeform = $("placeform");
  if (placeform) (function () {
    var img = $("placeimg"), bezel = $("placebezel"), msg = $("placemsg");
    var busy = false, again = false, lastUrl = null;

    function label() {
      $("fillval").textContent = Math.round($("placefill").value * 100) + "%";
      $("sepval").textContent = Math.round($("placesep").value * 100) + "%";
      $("shadowval").textContent = Math.round($("placeshadow").value * 100) + "%";
      $("offsetval").textContent = Math.round($("placeoffset").value * 100);
      $("baselineval").textContent = Math.round($("placebaseline").value * 100) + "%";
      $("trimval").textContent = Math.round($("placetrim").value * 100) + "%";
      var wv = Number($("placewhite").value);
      $("whiteval").textContent = wv === 0 ? "auto" : wv;
    }

    function draw() {
      if (busy) { again = true; return; }       // coalesce: sliders fire fast
      busy = true; again = false;
      var data = new FormData(placeform);
      if (!$("placebase").checked) data.set("drop_base", "0");
      fetch("/compose", { method: "POST", body: data })
        .then(function (r) {
          if (!r.ok) return r.text().then(function (t) { throw new Error(strip(t)); });
          return r.blob();
        })
        .then(function (b) {
          // Revoked, or every slider nudge leaks a few hundred KB.
          if (lastUrl) URL.revokeObjectURL(lastUrl);
          lastUrl = URL.createObjectURL(b);
          img.src = lastUrl;
          bezel.hidden = false;
          msg.textContent = "";
        })
        .catch(function (e) { msg.textContent = e.message; })
        .then(function () {
          busy = false;
          if (again) draw();
        });
    }

    placeform.addEventListener("submit", function (e) { e.preventDefault(); draw(); });
    placeform.addEventListener("input", function () { label(); draw(); });
    placeform.addEventListener("change", draw);
    label();

    $("placekeep").addEventListener("click", function () {
      var data = new FormData(placeform);
      if (!$("placebase").checked) data.set("drop_base", "0");
      data.set("keep", "1");
      fetch("/compose", { method: "POST", body: data })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          msg.textContent = j.kept ? "kept — it is in the pool now" : "could not keep it";
        })
        .catch(function (e) { msg.textContent = e.message; });
    });
  })();

  // Put a finished place on the shelf without a reload. It is filed server
  // side the moment it is made, so this is only catching the page up.
  function addToShelf(job) {
    var shelf = $("shelf");
    if (!shelf || !job.bg_id) return;
    if (shelf.querySelector('[data-bg="' + job.bg_id + '"]')) return;
    var head = $("shelfhead");
    if (head) head.hidden = false;
    var fig = document.createElement("figure");
    fig.className = "bgcard";
    fig.dataset.bg = job.bg_id;
    var img = document.createElement("img");
    img.src = "/backgrounds/" + job.bg_id + "/image";
    img.alt = job.prompt || "";
    img.loading = "lazy";
    var cap = document.createElement("figcaption");
    var f = document.createElement("form");
    f.method = "post";
    f.action = "/backgrounds/" + job.bg_id;
    f.className = "bgedit";
    f.innerHTML = '<input type="text" name="name" value="" placeholder="name it" class="mono">'
                + '<input type="text" name="motion" placeholder="what moves">'
                + '<button type="submit" class="icon">save</button>';
    f.querySelector('[name=motion]').value = job.motion || "";
    var row = document.createElement("div");
    row.className = "bgrow";
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "icon refinebg";
    btn.textContent = "refine";
    btn.dataset.bg = job.bg_id;
    btn.dataset.scene = job.prompt || "";
    btn.dataset.style = job.style || "";
    btn.dataset.motion = job.motion || "";
    row.appendChild(btn);
    cap.appendChild(f);
    cap.appendChild(row);
    fig.appendChild(img);
    fig.appendChild(cap);
    shelf.insertBefore(fig, shelf.firstChild);
  }

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

    if (job.kind === "background") addToShelf(job);
    var rb = $("refinebgbtn");
    if (rb) {
      rb.hidden = !(job.kind === "background" && job.bg_id);
      rb.onclick = function () {
        refineBackground(job.bg_id, job.prompt, job.style, job.motion);
      };
    }

    var url = "/generate/job/" + job.id + "/result?t=" + Date.now();
    if (job.is_image) {
      img.src = url; img.hidden = false;
      // A background is not a reference to refine from -- it is a place, and
      // adopting it would start a character run with a picture of a room.
      adopt.hidden = job.kind === "background";
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

  refreshKeep();
  setInterval(heartbeat, POLL_MS);
  heartbeat();
})();
