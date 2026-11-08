"use strict";

const navToggle = document.querySelector(".nav-toggle");
const siteNav = document.querySelector("#site-nav");

if (navToggle && siteNav) {
  navToggle.addEventListener("click", () => {
    const isExpanded = navToggle.getAttribute("aria-expanded") === "true";
    navToggle.setAttribute("aria-expanded", String(!isExpanded));
    siteNav.classList.toggle("is-open", !isExpanded);
  });

  siteNav.addEventListener("click", (event) => {
    const clickedLink = event.target instanceof Element ? event.target.closest("a") : null;
    if (clickedLink && siteNav.contains(clickedLink)) {
      navToggle.setAttribute("aria-expanded", "false");
      siteNav.classList.remove("is-open");
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      navToggle.setAttribute("aria-expanded", "false");
      siteNav.classList.remove("is-open");
    }
  });
}

const travelMonth = document.querySelector("#travel-month");
const travelYear = document.querySelector("#travel-year");

if (travelMonth && travelYear) {
  travelMonth.addEventListener("change", () => {
    travelYear.disabled = travelMonth.value === "";
    if (travelYear.disabled) {
      travelYear.value = "";
    }
  });
}
