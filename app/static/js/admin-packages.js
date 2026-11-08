"use strict";

document.addEventListener("click", (event) => {
  const addButton = event.target.closest("[data-add-row]");
  if (addButton) {
    const listName = addButton.dataset.addRow;
    const template = document.getElementById(`${listName}-row-template`);
    const list = document.querySelector(`[data-repeatable-list="${listName}"]`);
    if (template && list) {
      list.append(template.content.cloneNode(true));
      const firstInput = list.lastElementChild?.querySelector("input");
      firstInput?.focus();
    }
    return;
  }

  const removeButton = event.target.closest("[data-remove-row]");
  if (removeButton) {
    const row = removeButton.closest(".repeatable-row");
    row?.remove();
  }
});
