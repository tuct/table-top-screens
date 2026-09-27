/* Three small things the device page does in the browser:
 * preview a framing before it is applied, reorder the playlist by dragging,
 * and accept a file dropped on the library. Everything else is plain forms,
 * so the page still works with this file blocked. */
(function () {
  // ---- live preview -------------------------------------------------------
  var form = document.getElementById("framing");
  var img = document.getElementById("preview");
  if (form && img) {
    var base = img.dataset.base;          // /d/<device>/image?w=..&h=..&fmt=png
    var status = document.getElementById("pstatus");

    form.addEventListener("change", function () {
      var p = new URLSearchParams(new FormData(form));
      p.delete("reset");
      p.delete("name");                   // not a render parameter
      p.set("prefs", "0");                // preview the selection, not what is stored
      p.set("t", Date.now());             // defeat the browser cache
      img.src = base + "&" + p.toString();
      if (status) status.textContent = "preview — not applied yet";
    });
  }

  // ---- drag to reorder ----------------------------------------------------
  var list = document.getElementById("library");
  if (list) {
    var dragging = null;

    list.addEventListener("dragstart", function (e) {
      dragging = e.target.closest("li");
      if (!dragging) return;
      dragging.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      // Firefox will not start a drag without data set.
      try { e.dataTransfer.setData("text/plain", dragging.dataset.id); } catch (err) {}
    });

    list.addEventListener("dragend", function () {
      if (!dragging) return;
      dragging.classList.remove("dragging");
      dragging = null;
      persist();
    });

    list.addEventListener("dragover", function (e) {
      e.preventDefault();
      var over = e.target.closest("li");
      if (!over || !dragging || over === dragging) return;
      // Insert before or after depending on which half of the row we are over.
      var box = over.getBoundingClientRect();
      var after = (e.clientY - box.top) > box.height / 2;
      list.insertBefore(dragging, after ? over.nextSibling : over);
    });

    function persist() {
      var ids = Array.prototype.map.call(list.children, function (li) {
        return li.dataset.id;
      });
      fetch(list.dataset.orderUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: ids })
      }).then(function (r) {
        var note = document.getElementById("ordernote");
        if (note) note.textContent = r.ok ? "Order saved." : "Could not save order.";
      });
    }
  }

  // ---- overview: switch a screen's picture in place ----------------------
  (function () {
    var grid = document.querySelector(".grid");
    if (!grid) return;

    // Switching a picture can take a scene off screen, or put one back on.
    // The server decides that -- the page only flips the marks it is told to,
    // so there is one definition of "on screen" rather than two.
    function refreshScenes() {
      var list = document.getElementById("scenes");
      var note = document.getElementById("scenenote");
      if (!list && !note) return;

      fetch("/scenes/state", { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (state) {
          (state.scenes || []).forEach(function (s) {
            var row = list && list.querySelector('li[data-id="' + s.id + '"]');
            if (!row) return;
            var star = row.querySelector(".star");
            var pill = row.querySelector(".pill");
            if (star) star.hidden = !s.dirty;
            if (pill) pill.hidden = !s.on_screen;
          });
          if (note && state.note) {
            var who = note.querySelector(".who");
            var name = note.querySelector("b");
            var star = note.querySelector("b .star");
            var tail = note.querySelector(".tail");
            if (who) who.textContent = state.note.who;
            if (name) name.hidden = !state.note.who;
            if (star) star.hidden = !state.note.star;
            if (tail) tail.textContent = state.note.tail;
          }
        })
        .catch(function () { /* the marks stay as they were; no harm */ });
    }

    // Switch in place: no navigation, no reload. Posting JSON (rather than a
    // form body) is what makes the server answer with JSON instead of a 303.
    grid.addEventListener("submit", function (e) {
      var form = e.target.closest("form.pick");
      if (!form) return;
      e.preventDefault();

      var card = form.closest(".card");
      var busy = form.classList.contains("busy");
      if (busy) return;
      form.classList.add("busy");

      fetch(form.action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      })
        .then(function (r) {
          if (!r.ok) throw new Error(r.status);
          return r.json();
        })
        .then(function () {
          form.classList.remove("busy");

          var picks = card.querySelectorAll("form.pick");
          for (var i = 0; i < picks.length; i++) picks[i].classList.remove("now");
          form.classList.add("now");

          var caption = card.querySelector(".nameline .nm");
          if (caption && form.dataset.name) caption.textContent = form.dataset.name;

          refreshScenes();

          var img = card.querySelector(".glass img");
          if (img) {
            // Cache-bust: the URL is unchanged, only what it renders to.
            img.src = img.dataset.base + "&t=" + Date.now();
          } else {
            // The card had nothing on screen and so has no preview element yet.
            location.reload();
          }
        })
        .catch(function () {
          form.classList.remove("busy");
          form.submit();   // last resort: let the browser do the normal post
        });
    });
  })();

  // ---- refresh: spin while the knocking happens ---------------------------
  // The probe takes as long as the slowest screen's timeout, and the page
  // only changes when it comes back, so without this the button looks dead.
  (function () {
    var form = document.querySelector("form.refresh");
    if (!form) return;

    form.addEventListener("submit", function () {
      var button = form.querySelector("button");
      if (!button) return;
      button.classList.add("spinning");
      button.setAttribute("aria-busy", "true");
      // After the submit is away, so the browser still posts the form.
      setTimeout(function () { button.disabled = true; }, 0);
    });
  })();

  // ---- ticking a screen into a scene shows it at once ---------------------
  // The shelf is narrowed to the scene you are in, and every other card is in
  // the page but hidden. You tick a screen because you want to set what it
  // shows, so it appears the moment you tick it -- before the scene is saved.
  (function () {
    var grid = document.querySelector(".grid[data-narrowed]");
    var form = document.querySelector(".sceneform");
    if (!grid || !form) return;

    form.addEventListener("change", function (e) {
      var box = e.target;
      if (box.name !== "screens") return;
      var card = grid.querySelector('.card.screen[data-name="' + box.value + '"]');
      if (card) card.hidden = !box.checked;

      var count = document.querySelector(".filterline .fshown");
      if (count) {
        count.textContent = grid.querySelectorAll(".card.screen:not([hidden])").length;
      }
    });
  })();

  // ---- drop a file on the library ----------------------------------------
  var drop = document.getElementById("drop");
  if (drop) {
    var input = drop.querySelector("input[type=file]");

    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (e) {
        e.preventDefault();
        drop.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.remove("over"); });
    });

    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      if (!e.dataTransfer.files.length || !input) return;
      // Hand the dropped file to the form's own input, so the upload is the
      // same plain POST the browse button makes.
      input.files = e.dataTransfer.files;
      input.form.submit();
    });
  }
})();
