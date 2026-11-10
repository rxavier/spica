// A multiselect that is just a <details> with checkboxes inside: no library, no
// phantom values, and the browser submits exactly the boxes that are ticked.
(function () {
  function refresh(box) {
    const count = box.querySelector(".ms-count");
    if (!count) return;
    const picked = Array.from(box.querySelectorAll('input[type="checkbox"]:checked')).map(
      (b) => b.closest("label").textContent.replace(/\s*·.*$/, "").trim()
    );
    const n = picked.length;
    const empty = box.dataset.empty || "choose…";
    // Two names fit in a summary line; past that the count is the honest answer.
    count.textContent = n === 0 ? empty : n <= 2 ? picked.join(", ") : n + " selected";
    count.classList.toggle("on", n > 0);
  }

  document.querySelectorAll("[data-multiselect]").forEach(function (box) {
    const details = box.querySelector("details");
    const filter = box.querySelector(".ms-filter");
    const rows = Array.from(box.querySelectorAll(".ms-panel label"));

    box.addEventListener("change", function () { refresh(box); });

    if (filter) {
      filter.addEventListener("input", function () {
        const q = filter.value.trim().toLowerCase();
        rows.forEach(function (row) {
          row.hidden = Boolean(q) && row.textContent.toLowerCase().indexOf(q) < 0;
        });
      });
      // Typing should not jump to the first ticked box.
      filter.addEventListener("keydown", function (event) {
        if (event.key === "Enter") { event.preventDefault(); }
      });
    }

    const clear = box.querySelector(".ms-clear");
    if (clear) {
      clear.addEventListener("click", function () {
        box.querySelectorAll('input[type="checkbox"]').forEach(function (b) { b.checked = false; });
        refresh(box);
      });
    }

    document.addEventListener("click", function (event) {
      if (details && details.open && !box.contains(event.target)) details.open = false;
    });

    refresh(box);
  });
})();
