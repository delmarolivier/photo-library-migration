// Keyboard shortcuts for the review gallery: j/k move between cards, and each card's
// buttons carry data-key (a accept, r reject, k keep, 1-9 pick, s save). "?" shows help.
(function () {
  "use strict";
  function cards() { return Array.from(document.querySelectorAll("article.card")); }
  function current() {
    var active = document.activeElement;
    return (active && active.closest && active.closest("article.card")) || cards()[0];
  }
  function move(step) {
    var all = cards(), index = all.indexOf(current());
    var next = all[Math.min(Math.max(index + step, 0), all.length - 1)];
    if (next) { next.focus(); next.scrollIntoView({ block: "nearest" }); }
  }
  document.addEventListener("keydown", function (event) {
    if (event.target.closest && event.target.closest("input, textarea, select")) { return; }
    if (event.metaKey || event.ctrlKey || event.altKey) { return; }
    if (event.key === "j" || event.key === "ArrowDown") { move(1); event.preventDefault(); return; }
    if (event.key === "k" && !current().querySelector('[data-key="k"]')) { move(-1); return; }
    if (event.key === "ArrowUp") { move(-1); event.preventDefault(); return; }
    if (event.key === "?") { document.getElementById("help").hidden ^= true; return; }
    var card = current();
    if (!card) { return; }
    var button = card.querySelector('[data-key="' + event.key + '"]');
    if (!button && /^[1-9]$/.test(event.key)) {
      button = card.querySelectorAll(".answers button")[Number(event.key)];
    }
    if (button) { button.click(); event.preventDefault(); }
  });
  document.body.addEventListener("htmx:afterSwap", function (event) {
    var card = event.detail.target && document.getElementById(event.detail.target.id);
    var all = cards(), index = all.indexOf(card);
    var next = all.slice(index + 1).find(function (c) { return !c.classList.contains("decided"); });
    if (next) { next.focus(); }
  });
})();
