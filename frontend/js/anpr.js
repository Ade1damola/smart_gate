(function () {
  const form = document.getElementById("anpr-form");
  const imageInput = document.getElementById("anpr-image");
  const errorEl = document.getElementById("anpr-error");
  const resultEl = document.getElementById("anpr-result");
  const plateEl = document.getElementById("anpr-plate");
  const messageEl = document.getElementById("anpr-message");
  const submitBtn = form.querySelector("button[type=submit]");

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    errorEl.hidden = true;
    resultEl.hidden = true;

    const image = imageInput.files[0];
    if (!image) {
      errorEl.textContent = "Choose an image first.";
      errorEl.hidden = false;
      return;
    }

    const formData = new FormData();
    formData.append("image", image);
    submitBtn.disabled = true;
    submitBtn.textContent = "Scanning...";

    const { ok, data } = await apiFetch("/api/anpr/rapidocr", {
      method: "POST",
      body: formData,
    });

    if (ok && data.success) {
      plateEl.textContent = data.plate || "NO PLATE";
      messageEl.textContent = data.message;
      resultEl.hidden = false;
    } else {
      errorEl.textContent = data.message || "Could not process the image.";
      errorEl.hidden = false;
    }

    submitBtn.disabled = false;
    submitBtn.textContent = "Scan";
  });
})();
