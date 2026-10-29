(function () {
  document.querySelectorAll("[data-password-toggle]").forEach(function (toggle) {
    const input = document.getElementById(toggle.getAttribute("aria-controls"));
    if (!input) {
      throw new Error("Password toggle is missing its input.");
    }

    toggle.addEventListener("click", function () {
      const isVisible = input.type === "text";
      input.type = isVisible ? "password" : "text";
      toggle.textContent = isVisible ? "Show" : "Hide";
      toggle.setAttribute("aria-label", isVisible ? "Show password" : "Hide password");
      toggle.setAttribute("aria-pressed", String(!isVisible));
    });
  });
})();
